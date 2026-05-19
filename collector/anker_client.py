import asyncio
import aiohttp
from api.api import AnkerSolixApi
import logging
from datetime import datetime, timezone
COUNTRY = "DE"
async def get_snapshot(email, password):
    async with aiohttp.ClientSession() as session:
        api = AnkerSolixApi(email, password, COUNTRY, websession=session, logger=logging.getLogger())
        await api.update_sites()
        
        for site_id, site in api.sites.items():
            sb = site.get('solarbank_info', {})
            grid = site.get('grid_info', {})
            
            soc = 0
            solarbank_list = sb.get('solarbank_list', [])
            if solarbank_list:
                soc = float(solarbank_list[0].get('battery_power', 0) or 0)

            # Anker Statistiken (Gesamtwerte seit Gerätestart)
            statistics = site.get('statistics', [])
            anker_total_eur = 0.0
            for s in statistics:
                if str(s.get('type')) == '3':
                    anker_total_eur = float(s.get('total', 0) or 0)
                    break

            aiems_profit = site.get('aiems_profit', {})
            aiems_total_eur = float(aiems_profit.get('aiems_profit_total', 0) or 0) if aiems_profit else 0.0

            return {
                "timestamp":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                # grid_to_battery_power ist ein Anker API Bug – immer 0
                # Echter Wert = total_charging_power - total_photovoltaic_power
                "total_charging_w":  float(sb.get('total_charging_power', 0) or 0),
                "grid_to_bat_w":    max(float(sb.get('total_charging_power', 0) or 0) - float(sb.get('total_photovoltaic_power', 0) or 0), 0.0),
                "pv_production_w":   float(sb.get('total_photovoltaic_power', 0) or 0),
                "bat_to_home_w":    float(sb.get('to_home_load', 0) or 0),
                "grid_to_home_w":   float(grid.get('grid_to_home_power', 0) or 0),
                "soc_percent":      soc,
                "bat_discharge_w":  float(sb.get('battery_discharge_power', 0) or 0),
                "pv1_w":            float(sb.get('solar_power_1', 0) or 0),
                "pv2_w":            float(sb.get('solar_power_2', 0) or 0),
                "pv3_w":            float(sb.get('solar_power_3', 0) or 0),
                "pv4_w":            float(sb.get('solar_power_4', 0) or 0),
                "anker_total_eur":  anker_total_eur,
                "aiems_total_eur":  aiems_total_eur,
            }
    return None
