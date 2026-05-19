import requests
from datetime import datetime

GOE_TOKEN  = "9je6uR58lSJVgsn9G9208HrZaJifG78B"
GOE_SERIAL = "278768"
GOE_URL    = f"https://{GOE_SERIAL}.api.v3.go-e.io/api/status"

_goe_cache = None

def get_goe_snapshot():
    global _goe_cache
    try:
        r = requests.get(GOE_URL, params={
            "token": GOE_TOKEN,
            "filter": "nrg,wh,car,alw,eto"
        }, timeout=3)
        d = r.json()
        nrg = d.get("nrg", [0]*16)
        power_w = float(nrg[11]) if len(nrg) > 11 else 0.0
        _goe_cache = {
            "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "car_status": int(d.get("car", 0)),
            "power_w":    power_w,
            "session_wh": float(d.get("wh", 0)),
            "total_kwh":  float(d.get("eto", 0)) / 1000.0,
            "allowed":    1 if d.get("alw") else 0,
        }
        return _goe_cache
    except Exception as e:
        print(f"❌ Go-e Fehler: {e}")
        return _goe_cache
