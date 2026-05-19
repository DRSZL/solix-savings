import sqlite3
import bisect
import os
from datetime import datetime, timezone, timedelta, date as date_type
from database.db import (get_connection, save_daily_summary, get_daily_summaries)

from functools import lru_cache

@lru_cache(maxsize=4096)
def utc_to_local(ts_str):
    utc = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
    offset = timedelta(hours=2) if 3 <= utc.month <= 10 else timedelta(hours=1)
    return utc + offset

def aggregate_completed_days():
    now_utc = datetime.now(timezone.utc)
    offset_h = 2 if 3 <= now_utc.month <= 10 else 1
    today = (now_utc + timedelta(hours=offset_h)).strftime('%Y-%m-%d')
    conn = get_connection()
    c = conn.cursor()
    c.execute(f'''SELECT DISTINCT DATE(timestamp, '+{offset_h} hours') as day
                 FROM anker_snapshots
                 WHERE DATE(timestamp, '+{offset_h} hours') < ?
                 ORDER BY day ASC''', (today,))
    days = [r[0] for r in c.fetchall()]
    c.execute('SELECT date FROM daily_summary')
    done = {r[0] for r in c.fetchall()}
    conn.close()
    for day in days:
        if day not in done:
            result = calculate_savings(only_date=day)
            if result and result['daily']:
                for d in result['daily']:
                    if d['date'] == day:
                        d['snapshots'] = 0
                        save_daily_summary(day, d)
                        print(f"✅ Tag aggregiert: {day} → {d['gesamt_ersparnis']}€")

def calculate_savings(only_date=None):
    conn = get_connection()
    c = conn.cursor()
    now_utc = datetime.now(timezone.utc)
    dst_offset = timedelta(hours=2) if 3 <= now_utc.month <= 10 else timedelta(hours=1)
    today = (now_utc + dst_offset).strftime('%Y-%m-%d')
    target = only_date or today

    c.execute('''SELECT timestamp, price_eur_kwh FROM tibber_prices
                 WHERE timestamp >= datetime(?, '-1 day') AND timestamp <= datetime(?, '+2 days')
                 ORDER BY timestamp ASC''', (target, target))
    tibber_rows = c.fetchall()
    tibber_ts_list    = [r[0] for r in tibber_rows]
    tibber_price_list = [r[1] for r in tibber_rows]
    def get_price(ts):
        idx = bisect.bisect_right(tibber_ts_list, ts) - 1
        return tibber_price_list[idx] if idx >= 0 else None

    c.execute('''SELECT timestamp, power_w FROM goe_snapshots
                 WHERE timestamp >= datetime(?, '-1 hour') AND timestamp < datetime(?, '+23 hours')
                 ORDER BY timestamp ASC''', (target, target))
    goe_rows = c.fetchall()
    goe_ts_list = [r[0] for r in goe_rows]
    goe_pw_list = [r[1] for r in goe_rows]
    def get_goe_power(ts):
        idx = bisect.bisect_right(goe_ts_list, ts) - 1
        return goe_pw_list[idx] if idx >= 0 else 0.0

    c.execute('''SELECT timestamp, solar_kwh_in_bat, netz_kwh_in_bat, solar_ratio
                 FROM battery_composition
                 WHERE timestamp >= datetime(?, '-1 hour') AND timestamp < datetime(?, '+23 hours')
                 ORDER BY timestamp ASC''', (target, target))
    bc_rows = c.fetchall()
    bc_ts_list    = [r[0] for r in bc_rows]
    bc_solar_list = [r[1] for r in bc_rows]
    bc_ratio_list = [r[3] for r in bc_rows]
    bc_netz_list  = [r[2] for r in bc_rows]
    def get_solar_ratio(ts):
        idx = bisect.bisect_right(bc_ts_list, ts) - 1
        if idx < 0: return 0.0
        return bc_ratio_list[idx]

    c.execute('''SELECT timestamp, grid_to_bat_w, pv_production_w,
                 bat_discharge_w, bat_to_home_w, grid_to_home_w,
                 COALESCE(total_charging_w, 0), soc_percent
                 FROM anker_snapshots
                 WHERE timestamp >= datetime(?, '-1 hour') AND timestamp < datetime(?, '+23 hours')
                 ORDER BY timestamp ASC''', (target, target))
    raw_snaps = c.fetchall()
    conn.close()

    default_interval = int(os.getenv("ANKER_INTERVAL_S", 15))
    prev_ts = None
    daily = {}

    for snap in raw_snaps:
        ts, grid_to_bat_w, pv_production_w, bat_discharge_w, bat_to_home_w, \
            grid_to_home_w, total_charging_w, soc = snap

        if prev_ts is not None:
            diff = (datetime.fromisoformat(ts) -
                    datetime.fromisoformat(prev_ts)).total_seconds()
            if diff < 8:
                continue
            interval_s = diff if diff <= 60 else default_interval
        else:
            interval_s = default_interval
        prev_ts = ts

        price = get_price(ts)
        if price is None:
            continue

        solar_direct_kwh       = max((bat_to_home_w or 0) - (bat_discharge_w or 0), 0) * interval_s / 3_600_000
        solar_to_bat_netto_kwh = max((total_charging_w or 0) - (grid_to_bat_w or 0), 0) * interval_s / 3_600_000
        bat_discharge_kwh      = max(bat_discharge_w or 0, 0) * interval_s / 3_600_000
        goe_kwh                = get_goe_power(ts) * interval_s / 3_600_000
        solar_ratio            = get_solar_ratio(ts)
        solar_bat_home_kwh     = bat_discharge_kwh * solar_ratio
        netz_bat_home_kwh      = bat_discharge_kwh * (1 - solar_ratio)

        # Echter Netzstrom zur Batterie = maximal was der Netzzähler anzeigt.
        # Alles darüber ist indirektes Dach-PV das durch das Hausnetz zur Batterie fließt.
        real_netz_to_bat_w     = min(max(grid_to_bat_w or 0, 0), max(grid_to_home_w or 0, 0))
        indirect_pv_to_bat_w   = max(grid_to_bat_w or 0, 0) - real_netz_to_bat_w
        netz_to_bat_kwh        = real_netz_to_bat_w   * interval_s / 3_600_000
        indirect_pv_to_bat_kwh = indirect_pv_to_bat_w * interval_s / 3_600_000

        grid_home_real_kwh     = max((grid_to_home_w or 0) * interval_s / 3_600_000 - goe_kwh - netz_to_bat_kwh, 0)
        pv_produziert_kwh      = solar_direct_kwh + solar_to_bat_netto_kwh + indirect_pv_to_bat_kwh
        pv_genutzt_kwh         = solar_direct_kwh + solar_bat_home_kwh + indirect_pv_to_bat_kwh
        hausverbrauch_kwh      = solar_direct_kwh + solar_bat_home_kwh + netz_bat_home_kwh + grid_home_real_kwh
        pv_ersparnis_snap      = pv_genutzt_kwh * price
        ladekosten_snap        = netz_to_bat_kwh * price
        entladeerloes_snap     = netz_bat_home_kwh * price
        arbitrage_snap         = entladeerloes_snap - ladekosten_snap

        day = utc_to_local(ts).strftime('%Y-%m-%d')
        if day not in daily:
            daily[day] = {
                "date": day,
                "pv_ersparnis": 0.0, "netz_kosten": 0.0, "netz_erloes": 0.0,
                "netz_arbitrage": 0.0, "gesamt_ersparnis": 0.0,
                "solar_pv_kwh": 0.0, "solar_kwh": 0.0, "netz_to_bat_kwh": 0.0,
                "discharge_kwh": 0.0, "bat_home_kwh": 0.0, "grid_home_kwh": 0.0,
                "total_home_kwh": 0.0, "solar_bat_home_kwh": 0.0,
                "solar_to_home_direct_kwh": 0.0, "autarkie_pct": 0.0,
            }

        daily[day]["pv_ersparnis"]             += pv_ersparnis_snap
        daily[day]["netz_kosten"]              += ladekosten_snap
        daily[day]["netz_erloes"]              += entladeerloes_snap
        daily[day]["netz_arbitrage"]           += arbitrage_snap
        daily[day]["solar_pv_kwh"]             += pv_produziert_kwh
        daily[day]["solar_kwh"]                += pv_genutzt_kwh
        daily[day]["netz_to_bat_kwh"]          += netz_to_bat_kwh
        daily[day]["discharge_kwh"]            += bat_discharge_kwh
        daily[day]["bat_home_kwh"]             += bat_discharge_kwh
        daily[day]["grid_home_kwh"]            += grid_home_real_kwh
        daily[day]["total_home_kwh"]           += hausverbrauch_kwh
        daily[day]["solar_bat_home_kwh"]       += solar_bat_home_kwh
        daily[day]["solar_to_home_direct_kwh"] += solar_direct_kwh

    today_data = daily.get(target, {
        "date": target, "pv_ersparnis": 0.0, "netz_kosten": 0.0, "netz_erloes": 0.0,
        "netz_arbitrage": 0.0, "gesamt_ersparnis": 0.0, "solar_pv_kwh": 0.0,
        "solar_kwh": 0.0, "netz_to_bat_kwh": 0.0, "discharge_kwh": 0.0,
        "bat_home_kwh": 0.0, "grid_home_kwh": 0.0, "total_home_kwh": 0.0,
        "solar_bat_home_kwh": 0.0, "solar_to_home_direct_kwh": 0.0, "autarkie_pct": 0.0,
    })

    today_data["gesamt_ersparnis"] = today_data["pv_ersparnis"] + today_data["netz_arbitrage"]
    if today_data["total_home_kwh"] > 0:
        today_data["autarkie_pct"] = round(today_data["solar_kwh"] / today_data["total_home_kwh"] * 100, 1)
    else:
        today_data["autarkie_pct"] = 0.0

    for key in ["pv_ersparnis", "netz_kosten", "netz_erloes", "netz_arbitrage",
                "gesamt_ersparnis", "solar_pv_kwh", "solar_kwh", "netz_to_bat_kwh",
                "discharge_kwh", "bat_home_kwh", "grid_home_kwh", "total_home_kwh",
                "solar_bat_home_kwh", "solar_to_home_direct_kwh"]:
        today_data[key] = round(today_data.get(key, 0), 4)

    past_days  = get_daily_summaries(exclude_date=today)
    daily_list = [today_data] + past_days

    total_pv_ersparnis = sum(d["pv_ersparnis"]  for d in daily_list)
    total_arbitrage    = sum(d["netz_arbitrage"] for d in daily_list)
    total_ladekosten   = sum(d["netz_kosten"]    for d in daily_list)
    total_erloes       = sum(d["netz_erloes"]    for d in daily_list)
    total_gesamt       = total_pv_ersparnis + total_arbitrage

    return {
        "netz_kosten":      round(total_ladekosten, 4),
        "netz_erloes":      round(total_erloes, 4),
        "netz_arbitrage":   round(total_arbitrage, 4),
        "pv_ersparnis":     round(total_pv_ersparnis, 4),
        "gesamt_ersparnis": round(total_gesamt, 4),
        "daily":            daily_list
    }

def get_stats(today_data=None):
    """today_data: bereits berechnetes calculate_savings()-Ergebnis für heute (vermeidet Doppelberechnung)."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM anker_snapshots")
    total_snapshots = c.fetchone()[0]
    c.execute("SELECT MIN(timestamp), MAX(timestamp) FROM anker_snapshots")
    oldest, newest = c.fetchone()
    c.execute("SELECT AVG(price_eur_kwh) FROM tibber_prices")
    avg_price = c.fetchone()[0]
    interval_switch_ts = os.getenv("INTERVAL_SWITCH_TS", "2026-03-17 13:00:00")
    interval_before_s  = int(os.getenv("INTERVAL_BEFORE_S", 30))
    interval_after_s   = int(os.getenv("INTERVAL_AFTER_S",  15))
    _iswitch = f"CASE WHEN timestamp < '{interval_switch_ts}' THEN {interval_before_s} ELSE {interval_after_s} END"
    c.execute(f"""SELECT
        SUM((pv1_w+pv2_w+pv3_w+pv4_w) * {_iswitch}) / 3600000.0,
        SUM(grid_to_bat_w               * {_iswitch}) / 3600000.0,
        SUM(bat_discharge_w             * {_iswitch}) / 3600000.0
        FROM anker_snapshots""")
    _row = c.fetchone()
    total_pv_produziert_kwh = _row[0] or 0
    total_grid_to_bat_kwh   = _row[1] or 0
    total_discharge_kwh     = _row[2] or 0
    c.execute('''SELECT COALESCE(SUM(solar_bat_home_kwh),0), COALESCE(SUM(total_home_kwh),0),
                        COALESCE(SUM(bat_home_kwh),0), COALESCE(SUM(grid_home_kwh),0),
                        COALESCE(SUM(solar_to_home_direct_kwh),0), COALESCE(SUM(solar_kwh),0)
                 FROM daily_summary''')
    row = c.fetchone()
    conn.close()
    total_solar_bat_home       = row[0] or 0
    total_home_summary         = row[1] or 0
    total_bat_home             = row[2] or 0
    total_grid_home            = row[3] or 0
    total_solar_direct_summary = row[4] or 0
    total_solar_used_summary   = row[5] or 0
    _today = (today_data['daily'][0] if today_data and today_data.get('daily') else {})
    total_solar_bat_home  += _today.get('solar_bat_home_kwh', 0)
    total_home_summary    += _today.get('total_home_kwh', 0)
    total_bat_home        += _today.get('bat_home_kwh', 0)
    total_grid_home       += _today.get('grid_home_kwh', 0)
    total_solar_used       = total_solar_used_summary + _today.get('solar_kwh', 0)
    autarkie_pct = round(total_solar_used / total_home_summary * 100, 1) if total_home_summary > 0 else 0.0
    return {
        "total_snapshots":          total_snapshots,
        "oldest":                   oldest,
        "newest":                   newest,
        "avg_price":                round(avg_price, 4) if avg_price else 0,
        "total_solar_kwh":          round(total_pv_produziert_kwh, 4),
        "total_grid_kwh":           round(total_grid_to_bat_kwh, 4),
        "total_discharge_kwh":      round(total_discharge_kwh, 4),
        "autarkie_pct":             autarkie_pct,
        "total_bat_home_kwh":       round(total_bat_home, 4),
        "total_grid_home_kwh":      round(total_grid_home, 4),
        "total_home_kwh":           round(total_home_summary, 4),
        "total_solar_bat_home_kwh": round(total_solar_bat_home, 4),
    }

if __name__ == "__main__":
    print("📊 Berechne Ersparnis...")
    result = calculate_savings()
    print(f"Gesamtersparnis: {result['gesamt_ersparnis']} €")
    if result['daily']:
        for d in result['daily']:
            print(f"  {d['date']}: gesamt={d['gesamt_ersparnis']}€ | pv={d['pv_ersparnis']}€ | arbitrage={d['netz_arbitrage']}€ | pv_prod={d['solar_pv_kwh']}kWh | pv_genutzt={d['solar_kwh']}kWh | autarkie={d['autarkie_pct']}%")
