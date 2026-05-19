from flask import Flask, render_template, jsonify, make_response, request
import sys, os, requests, time
sys.path.insert(0, os.path.expanduser('~/solix-savings'))
from database.db import get_connection, get_baseline, get_goe_baseline, init_anomaly_log
init_anomaly_log()
from collector.weather_client import get_current_weather, get_forecast
from engine.savings_calculator import calculate_savings, get_stats
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/solix-savings/config.env'))
app = Flask(__name__)

# ── Einfacher In-Memory Cache ────────────────────────────────────
_cache = {}
def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() < entry['exp']:
        return entry['val']
    return None
def cache_set(key, val, ttl):
    _cache[key] = {'val': val, 'exp': time.time() + ttl}

@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")
TIBBER_TOKEN = os.getenv('TIBBER_TOKEN')
ANKER_INTERVAL_S = int(os.getenv('ANKER_INTERVAL_S', 15))
GOE_INTERVAL_S   = int(os.getenv('GOE_INTERVAL_S', 15))

@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
def get_recent_snapshots(limit=100):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''SELECT timestamp, grid_to_bat_w, pv_production_w,
                 bat_to_home_w, grid_to_home_w, soc_percent, bat_discharge_w,
                 pv1_w, pv2_w, pv3_w, pv4_w
                 FROM anker_snapshots
                 ORDER BY timestamp DESC LIMIT ?''', (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"timestamp": r[0], "grid_to_bat": r[1], "pv_production": r[2],
             "bat_to_home": r[3], "grid_to_home": r[4], "soc": r[5],
             "bat_discharge": r[6], "pv1": r[7], "pv2": r[8],
             "pv3": r[9], "pv4": r[10]} for r in rows]
def get_recent_prices(limit=192):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''SELECT timestamp, price_eur_kwh, price_level
                 FROM tibber_prices ORDER BY timestamp DESC LIMIT ?''', (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"timestamp": r[0], "price": r[1], "level": r[2]} for r in rows]
def get_pv_totals():
    conn = get_connection()
    c = conn.cursor()
    sw  = os.getenv("INTERVAL_SWITCH_TS", "2026-03-17 13:00:00")
    sb  = int(os.getenv("INTERVAL_BEFORE_S", 30))
    sa  = int(os.getenv("INTERVAL_AFTER_S",  15))
    _sw = f"CASE WHEN timestamp >= '{sw}' THEN {sa} ELSE {sb} END"
    c.execute(f'''SELECT
        SUM(pv1_w * {_sw}) / 3600000.0,
        SUM(pv2_w * {_sw}) / 3600000.0,
        SUM(pv3_w * {_sw}) / 3600000.0,
        SUM(pv4_w * {_sw}) / 3600000.0
        FROM anker_snapshots''')
    row = c.fetchone()
    conn.close()
    return {
        "pv1_kwh_total": round(row[0] or 0, 4),
        "pv2_kwh_total": round(row[1] or 0, 4),
        "pv3_kwh_total": round(row[2] or 0, 4),
        "pv4_kwh_total": round(row[3] or 0, 4),
    }
def get_live_price():
    cached = cache_get('live_price')
    if cached is not None:
        return cached
    query = "{viewer{homes{currentSubscription{priceInfo{current{total energy tax level startsAt}}}}}}"
    try:
        r = requests.post('https://api.tibber.com/v1-beta/gql',
            json={'query': query},
            headers={'Authorization': f'Bearer {TIBBER_TOKEN}'},
            timeout=5)
        result = r.json()['data']['viewer']['homes'][0]['currentSubscription']['priceInfo']['current']
        cache_set('live_price', result, 120)  # 2 Min Cache — Preise ändern sich stündlich
        return result
    except:
        return None
def get_anker_live_cached():
    """Liest letzte Anker-Statistiken aus DB (vom Collector geschrieben)."""
    from database.db import get_connection as _gc
    conn = _gc()
    c = conn.cursor()
    try:
        c.execute('SELECT anker_total_eur, aiems_total_eur FROM anker_live_cache WHERE id=1')
        row = c.fetchone()
        if row:
            return {'anker_total_eur': row[0], 'aiems_total_eur': row[1]}
    except:
        pass
    finally:
        conn.close()
    return None

def get_anker_live():
    """Holt aktuelle Anker-Statistiken direkt von der API."""
    import asyncio, aiohttp, logging
    from api.api import AnkerSolixApi
    ANKER_EMAIL    = os.getenv('ANKER_EMAIL')
    ANKER_PASSWORD = os.getenv('ANKER_PASSWORD')
    async def _fetch():
        async with aiohttp.ClientSession() as session:
            api = AnkerSolixApi(ANKER_EMAIL, ANKER_PASSWORD, 'DE',
                                websession=session, logger=logging.getLogger())
            await api.update_sites()
            for site_id, site in api.sites.items():
                statistics = site.get('statistics', [])
                anker_total_eur = 0.0
                for s in statistics:
                    if str(s.get('type')) == '3':
                        anker_total_eur = float(s.get('total', 0) or 0)
                        break
                aiems = site.get('aiems_profit', {})
                aiems_total_eur = float(aiems.get('aiems_profit_total', 0) or 0) if aiems else 0.0
                return {"anker_total_eur": anker_total_eur, "aiems_total_eur": aiems_total_eur}
        return None
    try:
        return asyncio.run(_fetch())
    except Exception as e:
        print(f"❌ Anker Live Fehler: {e}")
        return None
@app.route('/')
def index():
    resp = make_response(render_template('index.html', dashboard_refresh_s=int(os.getenv('DASHBOARD_REFRESH_S', 15))))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp
@app.route('/api/stats')
def api_stats():
    cached = cache_get('stats')
    if cached:
        return jsonify(cached)
    savings   = calculate_savings()              # berechnet heute + liest past days
    stats     = get_stats(today_data=savings)    # nutzt bereits berechnetes Ergebnis
    stats['netz_kosten']      = savings['netz_kosten']
    stats['netz_erloes']      = savings['netz_erloes']
    stats['netz_arbitrage']   = savings['netz_arbitrage']
    stats['pv_ersparnis']     = savings['pv_ersparnis']
    stats['gesamt_ersparnis'] = savings['gesamt_ersparnis']
    stats['daily']            = savings['daily']
    pv_totals = get_pv_totals()
    stats.update(pv_totals)
    live_price = get_live_price()
    if live_price:
        stats['live_price'] = live_price['total']
        stats['live_level'] = live_price['level']
    # Anker Differenz seit DB-Start
    baseline = get_baseline()
    anker_live = get_anker_live_cached()
    if baseline and anker_live:
        stats['anker_diff_eur']  = round(anker_live['anker_total_eur'] - baseline['anker_total_eur'], 2)
        stats['aiems_diff_eur']  = round(anker_live['aiems_total_eur'] - baseline['aiems_total_eur'], 2)
        stats['anker_baseline_ts'] = baseline['timestamp']
    cache_set('stats', stats, 13)  # 13s Cache — kürzer als 15s Refresh-Intervall
    return jsonify(stats)
@app.route('/api/snapshots')
def api_snapshots():
    date = request.args.get('date')
    if date:
        conn = get_connection()
        c = conn.cursor()
        c.execute('''SELECT MIN(timestamp), AVG(grid_to_bat_w), AVG(pv_production_w),
                     AVG(bat_to_home_w), AVG(grid_to_home_w), AVG(soc_percent), AVG(bat_discharge_w),
                     AVG(pv1_w), AVG(pv2_w), AVG(pv3_w), AVG(pv4_w)
                     FROM anker_snapshots
                     WHERE timestamp >= ? AND timestamp < date(?, '+1 day')
                     GROUP BY strftime('%H:%M', timestamp)
                     ORDER BY 1 ASC''', (date, date))
        rows = c.fetchall()
        conn.close()
        data = [{"timestamp": r[0], "grid_to_bat": r[1], "pv_production": r[2],
                 "bat_to_home": r[3], "grid_to_home": r[4], "soc": r[5],
                 "bat_discharge": r[6], "pv1": r[7], "pv2": r[8],
                 "pv3": r[9], "pv4": r[10]} for r in rows]
        resp = make_response(jsonify(data))
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    hours = int(request.args.get('hours', 12))
    limit = hours * 240
    resp = make_response(jsonify(get_recent_snapshots(limit)))
    resp.headers['Cache-Control'] = 'no-store'
    return resp
@app.route('/api/prices')
def api_prices():
    from flask import request as _req
    offset = int(_req.args.get('offset', 0))
    conn = get_connection()
    c = conn.cursor()
    from datetime import date, timedelta
    # Letzten verfügbaren Tag ermitteln
    c.execute("SELECT MAX(date(timestamp)) FROM tibber_prices")
    max_day = c.fetchone()[0]
    from datetime import datetime
    max_date = datetime.strptime(max_day, '%Y-%m-%d').date()
    day2 = (max_date - timedelta(days=offset)).isoformat()
    day1 = (max_date - timedelta(days=1+offset)).isoformat()
    c.execute("""SELECT timestamp, price_eur_kwh, price_level
                 FROM tibber_prices
                 WHERE date(timestamp) IN (?, ?)
                 ORDER BY timestamp ASC""", (day1, day2))
    rows = c.fetchall()
    conn.close()
    return jsonify([{"timestamp": r[0], "price": r[1], "level": r[2]} for r in rows])
@app.route('/api/pv_today')
def api_pv_today():
    from flask import request as _req
    date = _req.args.get('date')
    conn = get_connection()
    c = conn.cursor()
    if date:
        c.execute('''SELECT timestamp, pv1_w, pv2_w, pv3_w, pv4_w
                     FROM anker_snapshots
                     WHERE date(timestamp) = ?
                     ORDER BY timestamp ASC''', (date,))
    else:
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        offset_h = 2 if 3 <= now_utc.month <= 10 else 1
        local_today = (now_utc + timedelta(hours=offset_h)).strftime('%Y-%m-%d')
        c.execute(f'''SELECT timestamp, pv1_w, pv2_w, pv3_w, pv4_w
                     FROM anker_snapshots
                     WHERE date(timestamp, '+{offset_h} hours') >= ?
                     ORDER BY timestamp ASC''', (local_today,))
    rows = c.fetchall()
    conn.close()
    return jsonify([{"timestamp": r[0], "pv1": r[1], "pv2": r[2],
                     "pv3": r[3], "pv4": r[4]} for r in rows])
@app.route('/api/goe')
def api_goe():
    cached = cache_get('goe')
    if cached:
        return jsonify(cached)
    conn = get_connection()
    c = conn.cursor()

    # Letzter Snapshot
    c.execute('''SELECT timestamp, car_status, power_w, session_wh, total_kwh
                 FROM goe_snapshots ORDER BY id DESC LIMIT 1''')
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "keine Daten"})

    ts, car_status, power_w, session_wh, total_kwh = row

    # Baseline
    baseline = get_goe_baseline()
    diff_kwh = round(total_kwh - baseline['total_kwh'], 3) if baseline else 0.0

    # Tibber-Preise einmalig laden + per Bisect matchen (statt korrelierter Subquery)
    c.execute('SELECT timestamp, price_eur_kwh FROM tibber_prices ORDER BY timestamp ASC')
    t_rows = c.fetchall()
    t_ts_list = [r[0] for r in t_rows]
    t_pr_list = [r[1] for r in t_rows]
    import bisect
    def goe_price(ts):
        idx = bisect.bisect_right(t_ts_list, ts) - 1
        return t_pr_list[idx] if idx >= 0 else None

    c.execute('SELECT timestamp, power_w FROM goe_snapshots WHERE power_w > 0 ORDER BY timestamp ASC')
    charging_rows = c.fetchall()

    from datetime import datetime as _dt2
    total_cost = 0.0
    session_cost = 0.0
    last_ts = None

    for r_ts, r_power in charging_rows:
        r_price = goe_price(r_ts)
        if r_price is None:
            continue
        kwh = (r_power * GOE_INTERVAL_S) / 3_600_000.0
        cost = kwh * r_price
        total_cost += cost

        if last_ts is None:
            session_cost = cost
        else:
            diff = (_dt2.strptime(r_ts, '%Y-%m-%d %H:%M:%S') -
                    _dt2.strptime(last_ts, '%Y-%m-%d %H:%M:%S')).total_seconds()
            if diff > 180:
                session_cost = cost
            else:
                session_cost += cost
        last_ts = r_ts

    car_labels = {1: "Bereit", 2: "Lädt 🔋", 3: "Wartet", 4: "Fertig / Kein Auto"}

    conn.close()
    result = {
        "timestamp":      ts,
        "car_status":     car_status,
        "car_label":      car_labels.get(car_status, "Unbekannt"),
        "power_w":        round(power_w, 0),
        "session_wh":     round(session_wh, 0),
        "total_kwh":      round(total_kwh, 3),
        "diff_kwh":       diff_kwh,
        "baseline_ts":    baseline['timestamp'] if baseline else None,
        "session_cost":   round(session_cost, 4),
        "total_cost":     round(total_cost, 4),
    }
    # Beim aktiven Laden kürzer cachen (Session-Kosten ändern sich),
    # sonst länger (Gesamtkosten-Scan ist teuer)
    ttl = 14 if power_w > 0 else 60
    cache_set('goe', result, ttl)
    return jsonify(result)

@app.route('/api/weather')
def api_weather():
    cached = cache_get('weather')
    if cached:
        return jsonify(cached)
    import datetime as dt, math
    from database.db import get_pv_forecast, save_pv_forecast
    pv1_max = float(os.getenv('PV1_MAX_W', 450))
    pv2_max = float(os.getenv('PV2_MAX_W', 450))
    pv3_max = float(os.getenv('PV3_MAX_W', 400))
    pv_peak_factor = float(os.getenv('PV_PEAK_FACTOR', 0.6))
    pv_max  = (pv1_max + pv2_max + pv3_max) * pv_peak_factor
    current  = get_current_weather()
    forecast = get_forecast()
    if current:
        current['sunrise_str'] = dt.datetime.fromtimestamp(current['sunrise']).strftime('%H:%M')
        current['sunset_str']  = dt.datetime.fromtimestamp(current['sunset']).strftime('%H:%M')
    # Forecast: heute aus DB laden (stabil), nur wenn leer → live speichern
    today_str = dt.date.today().isoformat()
    db_forecast = get_pv_forecast(today_str)
    if not db_forecast and forecast:
        save_pv_forecast(today_str, forecast)
        db_forecast = get_pv_forecast(today_str)
    if not db_forecast:
        db_forecast = forecast  # Fallback: live
    # Sinus-Faktor auf Forecast anwenden
    sunrise_ts = current['sunrise'] if current else None
    sunset_ts  = current['sunset']  if current else None
    for f in db_forecast:
        f_ts = dt.datetime.strptime(f['timestamp'], '%Y-%m-%d %H:%M:%S').timestamp()
        if sunrise_ts and sunset_ts and sunrise_ts <= f_ts <= sunset_ts:
            day_length = sunset_ts - sunrise_ts
            angle = math.pi * (f_ts - sunrise_ts) / day_length
            sun_factor = math.sin(angle)
            f['pv_forecast_w'] = round(f['pv_score'] * pv_max * sun_factor, 0)
            f['sun_factor'] = round(sun_factor, 2)
        else:
            f['pv_forecast_w'] = 0.0
            f['sun_factor'] = 0.0
    result = {"current": current, "forecast": db_forecast, "pv_max": pv_max}
    cache_set('weather', result, 600)  # 10 Min — Wetter ändert sich nicht sekündlich
    return jsonify(result)

@app.route('/api/breakeven')
def api_breakeven():
    cached = cache_get('breakeven')
    if cached:
        return jsonify(cached)
    from database.db import get_connection as _gc
    import datetime as _dt
    conn = _gc()
    c = conn.cursor()
    # Anker Baseline
    c.execute('SELECT anker_total_eur, aiems_total_eur, timestamp FROM anker_baseline WHERE id=1')
    b = c.fetchone()
    anker_gesamt = b[0] if b else 0.0
    baseline_ts  = b[2] if b else None
    # Eigene Ersparnis: abgeschlossene Tage aus daily_summary + heute live
    import datetime as _dt2
    today = _dt2.date.today().isoformat()
    c.execute("SELECT COALESCE(SUM(gesamt_ersparnis),0) FROM daily_summary WHERE date < ?", (today,))
    eigen_gesamt = c.fetchone()[0] or 0.0
    conn.close()
    # Heute live hinzufügen
    from engine.savings_calculator import calculate_savings
    today_data = calculate_savings(only_date=today)
    eigen_gesamt += today_data['daily'][0]['gesamt_ersparnis'] if today_data and today_data.get('daily') else 0.0
    conn = _gc()
    c = conn.cursor()
    # Tage seit DB-Start
    c.execute('SELECT MIN(timestamp), MAX(timestamp) FROM anker_snapshots')
    r = c.fetchone()
    conn.close()
    if r[0] and r[1]:
        t_start = _dt.datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S')
        t_end   = _dt.datetime.strptime(r[1], '%Y-%m-%d %H:%M:%S')
        tage_db = max((t_end - t_start).total_seconds() / 86400, 0.01)
    else:
        tage_db = 0.01
    # Anker Ø pro Tag (seit DB-Start): Delta aus live_cache - baseline
    kauf = _dt.datetime(2025, 6, 1)
    tage_seit_kauf = max((_dt.datetime.now() - kauf).days, 1)
    conn2 = _gc()
    c2 = conn2.cursor()
    c2.execute("SELECT anker_total_eur FROM anker_live_cache ORDER BY timestamp DESC LIMIT 1")
    _live = c2.fetchone()
    conn2.close()
    anker_aktuell = _live[0] if _live else anker_gesamt
    anker_delta = max(anker_aktuell - anker_gesamt, 0.0)
    anker_pro_tag = anker_delta / tage_db if tage_db > 0 else 0
    # Eigen Ø pro Tag (seit DB-Start)
    eigen_pro_tag  = eigen_gesamt / tage_db if tage_db > 0 else 0
    # Restbetrag nach bereits amortisiertem Anker-Wert
    kaufpreis = 1806.0
    bereits_amortisiert = anker_gesamt  # Anker-Wert bei DB-Start (Baseline)
    restbetrag = max(kaufpreis - bereits_amortisiert, 0.0)
    result = {
        "kaufpreis":           kaufpreis,
        "bereits_amortisiert": round(bereits_amortisiert, 2),
        "restbetrag":          round(restbetrag, 2),
        "anker_gesamt":        round(anker_gesamt, 2),
        "eigen_gesamt":        round(eigen_gesamt, 4),
        "anker_pro_tag":       round(anker_pro_tag, 4),
        "eigen_pro_tag":       round(eigen_pro_tag, 4),
        "tage_db":             round(tage_db, 1),
        "tage_seit_kauf":      tage_seit_kauf,
        "baseline_ts":         baseline_ts,
    }
    cache_set('breakeven', result, 300)
    return jsonify(result)


@app.route('/api/bat_ratio')
def api_bat_ratio():
    from database.db import get_connection as _gc
    conn = _gc()
    c = conn.cursor()
    c.execute("SELECT solar_kwh_in_bat, netz_kwh_in_bat, solar_ratio FROM battery_composition ORDER BY timestamp DESC LIMIT 1")
    r = c.fetchone()
    conn.close()
    if r:
        return jsonify({"solar_ratio": round(r[2], 4), "solar_kwh": round(r[0], 3), "netz_kwh": round(r[1], 3)})
    return jsonify({"solar_ratio": 0.0, "solar_kwh": 0.0, "netz_kwh": 0.0})


@app.route('/api/bat_composition')
def api_bat_composition():
    from database.db import get_connection as _gc
    date = request.args.get('date')
    conn = _gc()
    c = conn.cursor()
    if date:
        c.execute('''SELECT timestamp, solar_ratio, solar_kwh_in_bat, netz_kwh_in_bat
                     FROM battery_composition
                     WHERE timestamp >= ? AND timestamp < date(?, '+1 day')
                     ORDER BY timestamp ASC''', (date, date))
    else:
        hours = int(request.args.get('hours', 12))
        limit = hours * 240
        c.execute('''SELECT timestamp, solar_ratio, solar_kwh_in_bat, netz_kwh_in_bat
                     FROM battery_composition
                     ORDER BY timestamp DESC LIMIT ?''', (limit,))
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        ts, ratio, solar_kwh, netz_kwh = r
        # Wenn Batterie leer (beide kWh = 0): null zurückgeben → Chart zeigt Lücke statt 0%-Linie
        if (solar_kwh or 0) == 0 and (netz_kwh or 0) == 0:
            sr = None
        else:
            sr = round(ratio * 100, 1)
        result.append({"timestamp": ts, "solar_ratio": sr})
    return jsonify(result)

@app.route('/api/anomalies')
def api_anomalies():
    from database.db import get_connection as _gc
    from datetime import datetime as _dt
    conn = _gc()
    c = conn.cursor()
    now = _dt.utcnow()

    # ── Checks: aktuelle Anomalien ermitteln ──────────────────────
    detected = []

    # 1. Datenlücke
    c.execute('SELECT MAX(timestamp) FROM anker_snapshots')
    row = c.fetchone()
    if row and row[0]:
        gap_min = (now - _dt.fromisoformat(row[0])).total_seconds() / 60
        if gap_min > 2:
            detected.append(("data_gap", "crit",
                "Datenlücke – Collector antwortet nicht",
                f"Kein Snapshot seit {int(gap_min)} Minuten. Letzter Wert: {row[0][11:16]}.",
                "sudo systemctl status solix-savings"))

    # 2. Kein Tibber-Preis
    c.execute('SELECT MAX(timestamp) FROM tibber_prices')
    row = c.fetchone()
    if row and row[0]:
        if (now - _dt.fromisoformat(row[0])).total_seconds() / 3600 > 1:
            detected.append(("no_price", "warn",
                "Kein Tibber-Preis verfügbar",
                f"Letzter Preis: {row[0][11:16]}. Ersparnis-Berechnung ungenau.",
                None))

    # 3. Composition-Reset während SOC > 10% (letzte 24 Stunden)
    c.execute('''
        SELECT bc.timestamp,
               (SELECT a.soc_percent FROM anker_snapshots a
                WHERE a.timestamp BETWEEN datetime(bc.timestamp, '-2 minutes')
                  AND datetime(bc.timestamp, '+2 minutes')
                  AND a.soc_percent > 10
                LIMIT 1) AS soc_at_reset
        FROM battery_composition bc
        WHERE bc.timestamp >= datetime('now', '-24 hours')
          AND bc.solar_kwh_in_bat = 0 AND bc.netz_kwh_in_bat = 0
          AND soc_at_reset IS NOT NULL
        ORDER BY bc.timestamp DESC LIMIT 1
    ''')
    row = c.fetchone()
    if row:
        detected.append(("composition_reset", "warn",
            "Solar-Ratio zurückgesetzt",
            f"battery_composition zeigt 0% obwohl SOC bei ~{int(row[1] or 0)}%. Möglicher API-Glitch ({row[0][5:16]}).",
            "python3 recalc_battery_composition.py"))

    # ── Log: neue Anomalien eintragen, gelöste auto-resolven ──────
    active_types = {d[0] for d in detected}

    for typ, sev, title, desc, action in detected:
        # Bereits offener Eintrag, oder innerhalb der letzten 60 Min manuell resolved?
        c.execute('''SELECT id FROM anomaly_log
                     WHERE type = ?
                       AND (resolved_at IS NULL OR resolved_at > datetime('now', '-24 hours'))
                     ORDER BY detected_at DESC LIMIT 1''', (typ,))
        if not c.fetchone():
            c.execute('''INSERT INTO anomaly_log (type, severity, title, details, action, detected_at)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (typ, sev, title, desc, action, now.isoformat()))

    # Auto-resolve: Anomalien die nicht mehr aktiv sind
    c.execute('''SELECT id, type FROM anomaly_log WHERE resolved_at IS NULL''')
    for log_id, typ in c.fetchall():
        if typ not in active_types:
            c.execute('''UPDATE anomaly_log SET resolved_at = ?
                         WHERE id = ? AND resolved_at IS NULL''',
                      (now.isoformat(), log_id))

    conn.commit()

    # ── Aktive (unresolved) Einträge zurückgeben ──────────────────
    c.execute('''SELECT id, type, severity, title, details, action,
                        detected_at, acknowledged_at
                 FROM anomaly_log
                 WHERE resolved_at IS NULL
                 ORDER BY
                     CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     detected_at ASC''')
    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        result.append({
            "id":             r[0],
            "type":           r[1],
            "severity":       r[2],
            "title":          r[3],
            "desc":           r[4],
            "action":         r[5],
            "detected_at":    r[6][5:16] if r[6] else None,
            "acknowledged":   r[7] is not None,
        })
    return jsonify({"anomalies": result, "checked_at": now.strftime('%H:%M:%S')})


@app.route('/api/anomalies/<int:anom_id>/ack', methods=['POST'])
def api_anomaly_ack(anom_id):
    from database.db import get_connection as _gc
    from datetime import datetime as _dt
    conn = _gc()
    conn.execute('UPDATE anomaly_log SET acknowledged_at = ? WHERE id = ? AND acknowledged_at IS NULL',
                 (_dt.utcnow().isoformat(), anom_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route('/api/anomalies/<int:anom_id>/resolve', methods=['POST'])
def api_anomaly_resolve(anom_id):
    from database.db import get_connection as _gc
    from datetime import datetime as _dt
    conn = _gc()
    conn.execute('UPDATE anomaly_log SET resolved_at = ? WHERE id = ?',
                 (_dt.utcnow().isoformat(), anom_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.route('/api/day_detail')
def api_day_detail():
    date = request.args.get('date')
    if not date:
        return jsonify({"error": "date required"}), 400
    from datetime import datetime as _dt
    try:
        dt = _dt.strptime(date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    offset_h = 2 if 3 <= dt.month <= 10 else 1
    conn = get_connection()
    c = conn.cursor()
    c.execute(f'''
        SELECT
            CAST(strftime('%H', datetime(timestamp, '+{offset_h} hours')) AS INTEGER) AS hour,
            AVG(pv1_w + pv2_w + pv3_w + pv4_w) AS avg_pv_w,
            AVG(grid_to_home_w + bat_to_home_w)  AS avg_home_w,
            AVG(bat_discharge_w)                  AS avg_bat_dis_w,
            AVG(soc_percent)                      AS avg_soc
        FROM anker_snapshots
        WHERE date(timestamp, '+{offset_h} hours') = ?
        GROUP BY hour ORDER BY hour
    ''', (date,))
    pv_rows = c.fetchall()
    c.execute(f'''
        SELECT
            CAST(strftime('%H', datetime(timestamp, '+{offset_h} hours')) AS INTEGER) AS hour,
            AVG(price_eur_kwh * 100) AS price_ct
        FROM tibber_prices
        WHERE date(timestamp, '+{offset_h} hours') = ?
        GROUP BY hour ORDER BY hour
    ''', (date,))
    price_rows = c.fetchall()
    c.execute(f'''
        SELECT
            CAST(strftime('%H', datetime(timestamp, '+{offset_h} hours')) AS INTEGER) AS hour,
            AVG(power_w) AS avg_goe_w
        FROM goe_snapshots
        WHERE date(timestamp, '+{offset_h} hours') = ?
        GROUP BY hour ORDER BY hour
    ''', (date,))
    goe_rows = c.fetchall()
    conn.close()
    pv_map    = {r[0]: (r[1], r[2], r[3], r[4]) for r in pv_rows}
    price_map = {r[0]: r[1] for r in price_rows}
    goe_map   = {r[0]: (r[1] or 0) for r in goe_rows}
    hours = [{"h": h,
              "pv_w":      round(pv_map.get(h, (0,0,0,0))[0] or 0, 1),
              "home_w":    round(max(0, (pv_map.get(h, (0,0,0,0))[1] or 0) - goe_map.get(h, 0)), 1),
              "bat_dis_w": round(pv_map.get(h, (0,0,0,0))[2] or 0, 1),
              "soc":       round(pv_map.get(h, (0,0,0,0))[3] or 0, 1),
              "price_ct":  round(price_map.get(h, 0) or 0, 2)} for h in range(24)]
    return jsonify({"hours": hours})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
