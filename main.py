import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Battery composition state (im Memory gehalten)
_bat_state = {"solar": None, "netz": None, "low_soc_count": 0}

from database.db import init_db, save_snapshot, save_price, init_baseline, get_baseline, save_baseline, init_goe, save_goe_snapshot, get_goe_baseline, save_goe_baseline, init_battery_composition, get_last_battery_composition, save_battery_composition
from engine.savings_calculator import aggregate_completed_days
from collector.goe_client import get_goe_snapshot
from collector.anker_client import get_snapshot
from collector.tibber_client import get_current_price
load_dotenv('config.env')
TIBBER_TOKEN   = os.getenv('TIBBER_TOKEN')
ANKER_EMAIL    = os.getenv('ANKER_EMAIL')
ANKER_PASSWORD = os.getenv('ANKER_PASSWORD')
async def collect_anker():
    try:
        snap = await get_snapshot(ANKER_EMAIL, ANKER_PASSWORD)
        if snap:
            # Baseline einmalig beim ersten Snapshot speichern
            if get_baseline() is None:
                save_baseline(snap['anker_total_eur'], snap['aiems_total_eur'])
                print(f"📌 Baseline gespeichert: Anker={snap['anker_total_eur']}€ | AIEMS={snap['aiems_total_eur']}€")
            save_snapshot(
                snap['timestamp'],
                snap['grid_to_bat_w'],
                snap['pv_production_w'],
                snap['bat_to_home_w'],
                snap['grid_to_home_w'],
                snap['soc_percent'],
                snap['bat_discharge_w'],
                snap['pv1_w'],
                snap['pv2_w'],
                snap['pv3_w'],
                snap['pv4_w'],
                snap.get('total_charging_w', 0)
            )
            # Battery Composition fortschreiben
            # Preis aus DB lesen (bereits alle 15min aktualisiert)
            from database.db import get_connection as _get_conn
            try:
                _conn = _get_conn()
                _c = _conn.cursor()
                _c.execute('''SELECT price_eur_kwh FROM tibber_prices
                              WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1''',
                           (snap['timestamp'],))
                _row = _c.fetchone()
                current_price = _row[0] if _row else 0.0
                _conn.close()
            except:
                current_price = 0.0

            if _bat_state["solar"] is None:
                last = get_last_battery_composition()
                _bat_state["solar"] = last['solar_kwh_in_bat'] if last else 0.0
                _bat_state["netz"]  = last['netz_kwh_in_bat']  if last else 0.0
            solar_kwh_in_bat = _bat_state["solar"]
            netz_kwh_in_bat  = _bat_state["netz"]
            _interval_s  = int(os.getenv('ANKER_INTERVAL_S', 15))
            # solar_to_bat_netto = total_charging - grid_to_bat (echter SOC-Aufbau Solar)
            solar_to_bat_netto = max((snap.get('total_charging_w', 0) or 0) - (snap['grid_to_bat_w'] or 0), 0)
            solar_kwh    = solar_to_bat_netto    * _interval_s / 3_600_000
            grid_kwh     = snap['grid_to_bat_w'] * _interval_s / 3_600_000
            discharge_kwh = snap['bat_discharge_w'] * _interval_s / 3_600_000
            # Laden: Solar- und Netzanteil akkumulieren
            if grid_kwh > 0 or solar_kwh > 0:
                solar_kwh_in_bat += solar_kwh
                netz_kwh_in_bat  += grid_kwh

            # Entladen: proportional abziehen
            if discharge_kwh > 0:
                total_tracked = solar_kwh_in_bat + netz_kwh_in_bat
                if total_tracked > 0:
                    r = solar_kwh_in_bat / total_tracked
                    # Discharge darf total_tracked nicht übersteigen (verhindert Ratio-Sprung durch max(0,...)-Asymmetrie)
                    discharge_kwh = min(discharge_kwh, total_tracked)
                    solar_kwh_in_bat = max(solar_kwh_in_bat - discharge_kwh * r, 0)
                    netz_kwh_in_bat  = max(netz_kwh_in_bat  - discharge_kwh * (1 - r), 0)
            # SOC auf Minimum → Batterie leer, Ratio zurücksetzen
            # Erst nach 3 aufeinanderfolgenden Readings ≤ 5% resetten (Schutz vor API-Glitches)
            if snap['soc_percent'] <= 5:
                _bat_state["low_soc_count"] = _bat_state.get("low_soc_count", 0) + 1
            else:
                _bat_state["low_soc_count"] = 0
            if _bat_state["low_soc_count"] >= 20:
                solar_kwh_in_bat = 0.0
                netz_kwh_in_bat  = 0.0

            # Ratio aus akkumulierten Werten (kein SOC-Normierung)
            total_tracked = solar_kwh_in_bat + netz_kwh_in_bat
            if total_tracked > 0:
                solar_ratio = solar_kwh_in_bat / total_tracked
            else:
                solar_ratio = 0.0
            if solar_kwh_in_bat + netz_kwh_in_bat < (_bat_state.get("solar") or 0) + (_bat_state.get("netz") or 0) - 0.1:
                print(f"⚠️  BAT_STATE SPRUNG: {_bat_state} -> solar={solar_kwh_in_bat} netz={netz_kwh_in_bat}")
            _bat_state["solar"] = solar_kwh_in_bat
            _bat_state["netz"]  = netz_kwh_in_bat
            save_battery_composition(snap["timestamp"], snap["soc_percent"], solar_ratio, solar_kwh_in_bat=solar_kwh_in_bat, netz_kwh_in_bat=netz_kwh_in_bat)
            _gc = __import__("database.db", fromlist=["get_connection"]).get_connection
            _c = _gc()
            _cur = _c.cursor()
            _cur.execute('''INSERT OR REPLACE INTO anker_live_cache (id, timestamp, anker_total_eur, aiems_total_eur)
                VALUES (1, ?, ?, ?)''', (snap['timestamp'], snap['anker_total_eur'], snap['aiems_total_eur']))
            _c.commit()
            _c.close()
            print(f"[{snap['timestamp']}] ⚡ SOC={snap['soc_percent']}% | Grid→Bat={snap['grid_to_bat_w']}W | Solar={snap['pv_production_w']}W (PV1={snap['pv1_w']}W PV2={snap['pv2_w']}W PV3={snap['pv3_w']}W) | Entladen={snap['bat_discharge_w']}W | SolarRatio={round(solar_ratio*100,1)}%")
    except Exception as e:
        print(f"❌ Anker Fehler: {e}")
def collect_tibber():
    try:
        price_info = get_current_price(TIBBER_TOKEN)
        current = price_info['current']
        save_price(current['startsAt'], current['total'], current['level'])
        for price in price_info.get('today', []) + price_info.get('tomorrow', []):
            save_price(price['startsAt'], price['total'], price['level'])
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 💰 Tibber: {current['total']} €/kWh ({current['level']})")
    except Exception as e:
        print(f"❌ Tibber Fehler: {e}")
async def collect_goe():
    try:
        snap = get_goe_snapshot()
        if snap:
            if get_goe_baseline() is None:
                save_goe_baseline(snap['total_kwh'])
                print(f"📌 Go-e Baseline gespeichert: {snap['total_kwh']:.2f} kWh")
            # Nur speichern wenn total_kwh valide (nicht 0)
            if snap['total_kwh'] > 0:
                save_goe_snapshot(
                    snap['timestamp'], snap['car_status'], snap['power_w'],
                    snap['session_wh'], snap['total_kwh'], snap['allowed']
                )
            car_labels = {1: "bereit", 2: "lädt", 3: "wartet", 4: "fertig"}
            print(f"[{snap['timestamp']}] 🚗 Go-e: {car_labels.get(snap['car_status'], '?')} | {snap['power_w']:.0f}W | Session={snap['session_wh']:.0f}Wh | Gesamt={snap['total_kwh']:.2f}kWh")
    except Exception as e:
        print(f"❌ Go-e Fehler: {e}")

def backup_database():
    import shutil, datetime, glob, os
    datum = datetime.date.today().strftime('%Y-%m-%d')
    src = '/home/david_r_szal/solix-savings/data/savings.db'
    dst = f'/home/david_r_szal/solix-savings/data/backups/savings_{datum}.db'
    shutil.copy2(src, dst)
    # Nur die letzten 52 Backups behalten
    backups = sorted(glob.glob('/home/david_r_szal/solix-savings/data/backups/savings_*.db'))
    for old_backup in backups[:-52]:
        os.remove(old_backup)
    print(f'Backup erstellt: {dst}')

def summarize_yesterday():
    """Berechnet den gestrigen Tag und speichert ihn in daily_summary."""
    try:
        from datetime import datetime, timedelta
        yesterday = (datetime.utcnow() + timedelta(hours=1) - timedelta(days=1)).strftime('%Y-%m-%d')
        from engine.savings_calculator import calculate_savings
        from database.db import save_daily_summary
        d = calculate_savings(only_date=yesterday)["daily"][0]
        d["snapshots"] = 0
        save_daily_summary(yesterday, d)
        result = d
        if result:
            print(f"📅 Tageszusammenfassung gespeichert: {yesterday} | Ersparnis={result['gesamt_ersparnis']}€")
        else:
            print(f"⚠️ Keine Daten für {yesterday}")
    except Exception as e:
        print(f"❌ summarize_yesterday Fehler: {e}")

async def main():
    print("🚀 Solix Savings Collector startet...")
    init_db()
    init_baseline()
    init_goe()
    init_battery_composition()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(collect_anker, 'interval', seconds=int(os.getenv('ANKER_INTERVAL_S', 15)))
    scheduler.add_job(collect_tibber, 'interval', minutes=int(os.getenv('TIBBER_INTERVAL_MIN', 15)))
    scheduler.add_job(collect_goe, 'interval', seconds=int(os.getenv('GOE_INTERVAL_S', 15)))
    scheduler.add_job(summarize_yesterday, 'cron', hour=0, minute=5)  # täglich 00:05
    scheduler.add_job(backup_database, 'cron', day_of_week='sun', hour=3, minute=0)  # wöchentlich So 03:00
    scheduler.start()
    try:
        await collect_anker()
    except Exception as e:
        print(f"⚠️ Anker Start-Fehler (ignoriert): {e}")
    try:
        collect_tibber()
    except Exception as e:
        print(f"⚠️ Tibber Start-Fehler (ignoriert): {e}")
    try:
        await collect_goe()
    except Exception as e:
        print(f"⚠️ Go-e Start-Fehler (ignoriert): {e}")
    try:
        aggregate_completed_days()
        summarize_yesterday()
    except Exception as e:
        print(f"⚠️ Aggregation Start-Fehler (ignoriert): {e}")
    print("✅ Collector läuft!")
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n⏹ Collector gestoppt.")
if __name__ == "__main__":
    asyncio.run(main())
