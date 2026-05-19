from datetime import datetime
import sqlite3
import os

DB_PATH = os.path.expanduser("~/solix-savings/data/savings.db")

def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS tibber_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME NOT NULL UNIQUE,
        price_eur_kwh REAL NOT NULL,
        price_level TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS anker_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME NOT NULL,
        grid_to_bat_w REAL DEFAULT 0,
        pv_production_w REAL DEFAULT 0,
        bat_to_home_w REAL DEFAULT 0,
        grid_to_home_w REAL DEFAULT 0,
        soc_percent REAL DEFAULT 0,
        bat_discharge_w REAL DEFAULT 0,
        pv1_w REAL DEFAULT 0,
        pv2_w REAL DEFAULT 0,
        pv3_w REAL DEFAULT 0,
        pv4_w REAL DEFAULT 0,
        total_charging_w REAL DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        charge_start DATETIME,
        charge_end DATETIME,
        charge_kwh_grid REAL DEFAULT 0,
        charge_kwh_pv REAL DEFAULT 0,
        avg_charge_price REAL DEFAULT 0,
        discharge_start DATETIME,
        discharge_end DATETIME,
        discharge_kwh REAL DEFAULT 0,
        avg_discharge_price REAL DEFAULT 0,
        saving_eur REAL DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS daily_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE NOT NULL UNIQUE,
        netz_kosten REAL DEFAULT 0,
        netz_erloes REAL DEFAULT 0,
        netz_arbitrage REAL DEFAULT 0,
        pv_ersparnis REAL DEFAULT 0,
        gesamt_ersparnis REAL DEFAULT 0,
        solar_kwh REAL DEFAULT 0,
        grid_kwh REAL DEFAULT 0,
        discharge_kwh REAL DEFAULT 0,
        bat_home_kwh REAL DEFAULT 0,
        grid_home_kwh REAL DEFAULT 0,
        total_home_kwh REAL DEFAULT 0,
        solar_bat_home_kwh REAL DEFAULT 0,
        autarkie_pct REAL DEFAULT 0,
        snapshots INTEGER DEFAULT 0,
        solar_to_home_direct_kwh REAL DEFAULT 0,
        netz_to_bat_kwh REAL DEFAULT 0,
        solar_pv_kwh REAL DEFAULT 0
    )''')
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_anker_ts       ON anker_snapshots(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tibber_ts      ON tibber_prices(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_daily_date     ON daily_summary(date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_batcomp_ts     ON battery_composition(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_goe_ts         ON goe_snapshots(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_pvforecast_dt  ON pv_forecast(date, timestamp)')

    conn.commit()
    conn.close()
    print("✅ Datenbank initialisiert!")

def save_snapshot(timestamp, grid_to_bat, pv_production, bat_to_home,
                  grid_to_home, soc, bat_discharge, pv1=0, pv2=0, pv3=0, pv4=0, total_charging=0):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT INTO anker_snapshots
        (timestamp, grid_to_bat_w, pv_production_w, bat_to_home_w,
         grid_to_home_w, soc_percent, bat_discharge_w, pv1_w, pv2_w, pv3_w, pv4_w, total_charging_w)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (timestamp, grid_to_bat, pv_production, bat_to_home,
         grid_to_home, soc, bat_discharge, pv1, pv2, pv3, pv4, total_charging))
    conn.commit()
    conn.close()

def save_price(timestamp, price, level):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO tibber_prices (timestamp, price_eur_kwh, price_level)
        VALUES (?, ?, ?)''', (timestamp, price, level))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()

def init_baseline():
    """Erstellt die Baseline-Tabelle falls nicht vorhanden."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS anker_baseline (
        id INTEGER PRIMARY KEY,
        timestamp DATETIME NOT NULL,
        anker_total_eur REAL DEFAULT 0,
        aiems_total_eur REAL DEFAULT 0
    )''')
    conn.commit()
    conn.close()

def get_baseline():
    """Gibt den gespeicherten Startwert zurück, oder None."""
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT anker_total_eur, aiems_total_eur, timestamp FROM anker_baseline WHERE id = 1')
        row = c.fetchone()
        return {"anker_total_eur": row[0], "aiems_total_eur": row[1], "timestamp": row[2]} if row else None
    finally:
        conn.close()

def save_baseline(anker_total_eur, aiems_total_eur):
    """Speichert den Startwert einmalig."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO anker_baseline (id, timestamp, anker_total_eur, aiems_total_eur)
        VALUES (1, ?, ?, ?)''',
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), anker_total_eur, aiems_total_eur))
    conn.commit()
    conn.close()

def save_daily_summary(date, data):
    """Speichert oder aktualisiert die Tageszusammenfassung."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO daily_summary
        (date, netz_kosten, netz_erloes, netz_arbitrage, pv_ersparnis, gesamt_ersparnis,
         solar_kwh, grid_kwh, discharge_kwh, bat_home_kwh, grid_home_kwh,
         total_home_kwh, solar_bat_home_kwh, autarkie_pct, snapshots, solar_to_home_direct_kwh,
         netz_to_bat_kwh, solar_pv_kwh)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (date, data['netz_kosten'], data['netz_erloes'], data['netz_arbitrage'],
         data['pv_ersparnis'], data['gesamt_ersparnis'], data['solar_kwh'],
         data.get('netz_to_bat_kwh', 0),
         data['discharge_kwh'], data['bat_home_kwh'],
         data['grid_home_kwh'], data['total_home_kwh'], data['solar_bat_home_kwh'],
         data['autarkie_pct'], data['snapshots'], data.get('solar_to_home_direct_kwh', 0),
         data.get('netz_to_bat_kwh', 0), data.get('solar_pv_kwh', 0)))
    conn.commit()
    conn.close()

def get_daily_summaries(exclude_date=None):
    """Gibt alle gespeicherten Tageszusammenfassungen zurück, optional ohne ein Datum."""
    conn = get_connection()
    c = conn.cursor()
    if exclude_date:
        c.execute('''SELECT date, netz_kosten, netz_erloes, netz_arbitrage, pv_ersparnis,
                     gesamt_ersparnis, solar_kwh, grid_kwh, discharge_kwh, bat_home_kwh,
                     grid_home_kwh, total_home_kwh, solar_bat_home_kwh, autarkie_pct, snapshots,
                     solar_to_home_direct_kwh, netz_to_bat_kwh, solar_pv_kwh
                     FROM daily_summary WHERE date != ? ORDER BY date DESC''', (exclude_date,))
    else:
        c.execute('''SELECT date, netz_kosten, netz_erloes, netz_arbitrage, pv_ersparnis,
                     gesamt_ersparnis, solar_kwh, grid_kwh, discharge_kwh, bat_home_kwh,
                     grid_home_kwh, total_home_kwh, solar_bat_home_kwh, autarkie_pct, snapshots,
                     solar_to_home_direct_kwh, netz_to_bat_kwh, solar_pv_kwh
                     FROM daily_summary ORDER BY date DESC''')
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "netz_kosten": r[1], "netz_erloes": r[2], "netz_arbitrage": r[3],
             "pv_ersparnis": r[4], "gesamt_ersparnis": r[5], "solar_kwh": r[6],
             "grid_kwh": r[7], "discharge_kwh": r[8], "bat_home_kwh": r[9],
             "grid_home_kwh": r[10], "total_home_kwh": r[11], "solar_bat_home_kwh": r[12],
             "autarkie_pct": r[13], "snapshots": r[14],
             "solar_to_home_direct_kwh": r[15] or 0,
             "netz_to_bat_kwh": r[16] or 0,
             "solar_pv_kwh": r[17] or 0} for r in rows]

def get_oldest_unsummarized_date():
    """Gibt das älteste Datum zurück das noch nicht in daily_summary ist."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''SELECT MIN(DATE(timestamp, '+1 hour')) FROM anker_snapshots''')
    oldest = c.fetchone()[0]
    conn.close()
    return oldest

def init_anomaly_log():
    """Persistente Anomalie-Datenbank."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS anomaly_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        details TEXT,
        action TEXT,
        detected_at DATETIME NOT NULL DEFAULT (datetime('now')),
        acknowledged_at DATETIME,
        resolved_at DATETIME
    )''')
    conn.commit()
    conn.close()

def init_battery_composition():
    """Trackt Solar/Netz-Verhältnis in der Batterie pro Snapshot."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS battery_composition (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME NOT NULL UNIQUE,
        soc_percent REAL DEFAULT 0,
        solar_ratio REAL DEFAULT 0,
        solar_kwh_in_bat REAL DEFAULT 0,
        netz_kwh_in_bat REAL DEFAULT 0
    )''')
    conn.commit()
    conn.close()

def get_last_battery_composition():
    """Gibt den letzten bekannten Batteriezustand zurück."""
    conn = get_connection()
    c = conn.cursor()
    try:
        # Letzten validen Wert nehmen (solar > 0 und netz > 0)
        c.execute('''SELECT timestamp, soc_percent, solar_ratio,
                     solar_kwh_in_bat, netz_kwh_in_bat
                     FROM battery_composition
                     WHERE solar_kwh_in_bat > 0 OR netz_kwh_in_bat > 0
                     ORDER BY id DESC LIMIT 1''')
        row = c.fetchone()
        if row:
            return {"timestamp": row[0], "soc_percent": row[1], "solar_ratio": row[2],
                    "solar_kwh_in_bat": row[3], "netz_kwh_in_bat": row[4]}
        return None
    finally:
        conn.close()

def save_battery_composition(timestamp, soc_percent, solar_ratio, avg_charge_price=0.0, solar_kwh_in_bat=0.0, netz_kwh_in_bat=0.0):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO battery_composition
        (timestamp, soc_percent, solar_ratio, solar_kwh_in_bat, netz_kwh_in_bat)
        VALUES (?, ?, ?, ?, ?)''',
        (timestamp, soc_percent, solar_ratio, solar_kwh_in_bat, netz_kwh_in_bat))
    conn.commit()
    conn.close()

def init_pv_forecast():
    """Erstellt pv_forecast Tabelle falls nicht vorhanden."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS pv_forecast (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE NOT NULL,
        timestamp DATETIME NOT NULL,
        pv_score REAL DEFAULT 0,
        description TEXT,
        icon TEXT,
        UNIQUE(date, timestamp)
    )''')
    conn.commit()
    conn.close()

def save_pv_forecast(date, entries):
    """Speichert Forecast-Einträge für einen Tag – nur wenn noch keiner existiert."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM pv_forecast WHERE date = ?', (date,))
    if c.fetchone()[0] == 0:
        for e in entries:
            c.execute('''INSERT OR IGNORE INTO pv_forecast (date, timestamp, pv_score, description, icon)
                VALUES (?, ?, ?, ?, ?)''',
                (date, e['timestamp'], e['pv_score'], e.get('description',''), e.get('icon','')))
        conn.commit()
    conn.close()

def get_pv_forecast(date):
    """Gibt gespeicherten Forecast für ein Datum zurück."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''SELECT timestamp, pv_score, description, icon 
                 FROM pv_forecast WHERE date = ? ORDER BY timestamp''', (date,))
    rows = c.fetchall()
    conn.close()
    return [{"timestamp": r[0], "pv_score": r[1], "description": r[2], "icon": r[3]} for r in rows]

def init_goe():
    """Erstellt Go-e Tabellen falls nicht vorhanden."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS goe_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME NOT NULL,
        car_status INTEGER DEFAULT 0,
        power_w REAL DEFAULT 0,
        session_wh REAL DEFAULT 0,
        total_kwh REAL DEFAULT 0,
        allowed INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS goe_baseline (
        id INTEGER PRIMARY KEY,
        timestamp DATETIME NOT NULL,
        total_kwh REAL DEFAULT 0
    )''')
    conn.commit()
    conn.close()

def save_goe_snapshot(timestamp, car_status, power_w, session_wh, total_kwh, allowed):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT INTO goe_snapshots
        (timestamp, car_status, power_w, session_wh, total_kwh, allowed)
        VALUES (?, ?, ?, ?, ?, ?)''',
        (timestamp, car_status, power_w, session_wh, total_kwh, allowed))
    conn.commit()
    conn.close()

def get_goe_baseline():
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('SELECT total_kwh, timestamp FROM goe_baseline WHERE id = 1')
        row = c.fetchone()
        return {"total_kwh": row[0], "timestamp": row[1]} if row else None
    finally:
        conn.close()

def save_goe_baseline(total_kwh):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO goe_baseline (id, timestamp, total_kwh)
        VALUES (1, ?, ?)''',
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total_kwh))
    conn.commit()
    conn.close()
