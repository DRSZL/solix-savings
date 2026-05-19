import requests
from datetime import datetime

WEATHER_KEY = 'c7e75eb43a08bba054171d0e85554c59'
LAT = 52.2956  # Rangsdorf
LON = 13.4167

def get_current_weather():
    try:
        r = requests.get('https://api.openweathermap.org/data/2.5/weather',
            params={'lat': LAT, 'lon': LON, 'appid': WEATHER_KEY, 'units': 'metric', 'lang': 'de'},
            timeout=10)
        d = r.json()
        return {
            "temp":        round(d['main']['temp'], 1),
            "temp_min":    round(d['main']['temp_min'], 1),
            "temp_max":    round(d['main']['temp_max'], 1),
            "humidity":    d['main']['humidity'],
            "clouds":      d['clouds']['all'],
            "wind_speed":  round(d['wind']['speed'], 1),
            "description": d['weather'][0]['description'],
            "icon":        d['weather'][0]['icon'],
            "sunrise":     d['sys']['sunrise'],
            "sunset":      d['sys']['sunset'],
        }
    except Exception as e:
        print(f"❌ Wetter Fehler: {e}")
        return None

def get_forecast():
    try:
        r = requests.get('https://api.openweathermap.org/data/2.5/forecast',
            params={'lat': LAT, 'lon': LON, 'appid': WEATHER_KEY, 'units': 'metric', 'lang': 'de', 'cnt': 8},
            timeout=10)
        d = r.json()
        forecast = []
        for item in d['list']:
            forecast.append({
                "timestamp":   item['dt_txt'],
                "temp":        round(item['main']['temp'], 1),
                "clouds":      item['clouds']['all'],
                "description": item['weather'][0]['description'],
                "icon":        item['weather'][0]['icon'],
                "pv_score":    round((100 - item['clouds']['all']) / 100, 2)
            })
        return forecast
    except Exception as e:
        print(f"❌ Vorhersage Fehler: {e}")
        return []
