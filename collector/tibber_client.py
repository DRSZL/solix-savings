import requests
from datetime import datetime, timezone, timedelta
import re
TOKEN = ""
def parse_tibber_timestamp(ts):
    ts_clean = re.sub(r'\.\d+', '', ts)
    dt = datetime.fromisoformat(ts_clean)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime('%Y-%m-%d %H:%M:%S')
def get_current_price(token):
    query = (
        "{ viewer { homes { currentSubscription { "
        "priceInfo(resolution: QUARTER_HOURLY) { "
        "current { total level startsAt } "
        "today { total level startsAt } "
        "tomorrow { total level startsAt } "
        "} "
        "priceInfoRange(resolution: QUARTER_HOURLY, last: 192) { "
        "nodes { total level startsAt } } } } } }"
    )
    response = requests.post(
        "https://api.tibber.com/v1-beta/gql",
        json={"query": query},
        headers={"Authorization": f"Bearer {token}"}
    )
    data = response.json()
    sub = data["data"]["viewer"]["homes"][0]["currentSubscription"]
    price_info = sub["priceInfo"]
    # Current normalisieren
    if price_info.get('current'):
        price_info['current']['startsAt'] = parse_tibber_timestamp(price_info['current']['startsAt'])
    # today + tomorrow normalisieren
    for key in ['today', 'tomorrow']:
        price_info[key] = [
            {**p, 'startsAt': parse_tibber_timestamp(p['startsAt'])}
            for p in price_info.get(key, [])
        ]
    return price_info
