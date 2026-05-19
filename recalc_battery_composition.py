#!/usr/bin/env python3
import sqlite3, sys, os
from datetime import datetime

DB_PATH = os.path.expanduser('~/solix-savings/data/savings.db')
START_DATE = '2026-03-21'
DEFAULT_INTERVAL = 15

def get_conn():
    return sqlite3.connect(DB_PATH)

def recalc_battery_composition():
    conn = get_conn()
    c = conn.cursor()
    print("=== Rekalkulation battery_composition ===")
    c.execute("DELETE FROM battery_composition WHERE timestamp >= ?", (START_DATE,))
    print(f"✓ {c.rowcount} alte Einträge gelöscht")
    conn.commit()
    c.execute('''SELECT timestamp, grid_to_bat_w, pv_production_w, total_charging_w,
               bat_discharge_w, bat_to_home_w, soc_percent
        FROM anker_snapshots WHERE timestamp >= ? ORDER BY timestamp ASC''', (START_DATE,))
    snapshots = c.fetchall()
    print(f"✓ {len(snapshots)} Snapshots geladen")
    solar_kwh_in_bat = netz_kwh_in_bat = 0.0
    prev_ts = None
    inserted = resets = 0
    low_soc_count = 0
    batch = []
    for row in snapshots:
        ts, grid_to_bat_w, pv_production_w, total_charging_w, bat_discharge_w, bat_to_home_w, soc_percent = row
        grid_to_bat_w = grid_to_bat_w or 0.0
        total_charging_w = total_charging_w or 0.0
        bat_discharge_w = bat_discharge_w or 0.0
        soc_percent = soc_percent or 0.0
        if prev_ts is not None:
            diff = (datetime.fromisoformat(ts) - datetime.fromisoformat(prev_ts)).total_seconds()
            if diff < 8:
                continue
            interval_s = diff if diff <= 60 else DEFAULT_INTERVAL
        else:
            interval_s = DEFAULT_INTERVAL
        prev_ts = ts
        solar_to_bat_netto = max(total_charging_w - grid_to_bat_w, 0)
        solar_kwh = solar_to_bat_netto * interval_s / 3_600_000
        grid_kwh = grid_to_bat_w * interval_s / 3_600_000
        discharge_kwh = bat_discharge_w * interval_s / 3_600_000
        if solar_kwh > 0 or grid_kwh > 0:
            solar_kwh_in_bat += solar_kwh
            netz_kwh_in_bat += grid_kwh
        if discharge_kwh > 0:
            total_tracked = solar_kwh_in_bat + netz_kwh_in_bat
            if total_tracked > 0:
                discharge_kwh = min(discharge_kwh, total_tracked)
                r = solar_kwh_in_bat / total_tracked
                solar_kwh_in_bat = max(solar_kwh_in_bat - discharge_kwh * r, 0)
                netz_kwh_in_bat = max(netz_kwh_in_bat - discharge_kwh * (1 - r), 0)
        # Reset erst nach 20 aufeinanderfolgenden Readings ≤ 5% (= 5 Minuten, Schutz vor API-Glitches)
        if soc_percent <= 5:
            low_soc_count += 1
        else:
            low_soc_count = 0
        if low_soc_count >= 20:
            solar_kwh_in_bat = netz_kwh_in_bat = 0.0
            resets += 1
        total_tracked = solar_kwh_in_bat + netz_kwh_in_bat
        solar_ratio = solar_kwh_in_bat / total_tracked if total_tracked > 0 else 0.0
        batch.append((ts, soc_percent, solar_ratio, solar_kwh_in_bat, netz_kwh_in_bat))
        inserted += 1
        if len(batch) >= 1000:
            c.executemany('''INSERT OR IGNORE INTO battery_composition
                (timestamp, soc_percent, solar_ratio, solar_kwh_in_bat, netz_kwh_in_bat)
                VALUES (?, ?, ?, ?, ?)''', batch)
            conn.commit()
            batch = []
            print(f"  ... {inserted} geschrieben", end='\r')
    if batch:
        c.executemany('''INSERT OR IGNORE INTO battery_composition
            (timestamp, soc_percent, solar_ratio, solar_kwh_in_bat, netz_kwh_in_bat)
            VALUES (?, ?, ?, ?, ?)''', batch)
        conn.commit()
    print(f"\n✓ {inserted} Einträge geschrieben, {resets} SOC-Resets")
    conn.close()

def recalc_daily_summary():
    print("\n=== Rekalkulation daily_summary ===")
    sys.path.insert(0, os.path.expanduser('~/solix-savings'))
    from engine.savings_calculator import calculate_savings
    from database.db import save_daily_summary
    conn = get_conn()
    c = conn.cursor()
    c.execute('''SELECT DISTINCT date(timestamp) FROM anker_snapshots
        WHERE timestamp >= ? AND date(timestamp) < date('now') ORDER BY 1''', (START_DATE,))
    days = [r[0] for r in c.fetchall()]
    conn.close()
    print(f"Berechne {len(days)} Tage...")
    for day in days:
        try:
            result = calculate_savings(only_date=day)
            if result and result.get('daily'):
                d = result['daily'][0]
                d['snapshots'] = 0
                save_daily_summary(day, d)
                print(f"  ✓ {day}: gesamt={round(d.get('gesamt_ersparnis',0),2)}€ | arbitrage={round(d.get('netz_arbitrage',0),2)}€ | autarkie={d.get('autarkie_pct',0)}%")
            else:
                print(f"  ⚠ {day}: keine Daten")
        except Exception as e:
            print(f"  ✗ {day}: {e}")
    print("✓ daily_summary fertig")

if __name__ == '__main__':
    confirm = input("Fortfahren? (ja/nein): ").strip().lower()
    if confirm != 'ja':
        sys.exit(0)
    recalc_battery_composition()
    recalc_daily_summary()
    print("\n✅ Fertig! Bitte Service neustarten.")
