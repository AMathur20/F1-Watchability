import os
import json
import time
import logging
import datetime
import paho.mqtt.client as mqtt
import fastf1
import pandas as pd
import numpy as np

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), 'weights.json')

# Persistent Data Directory
DATA_DIR = os.getenv("DATA_DIR", "/data")
if not os.path.exists(DATA_DIR):
    try:
         os.makedirs(DATA_DIR)
    except Exception as e:
         logger.warning(f"Could not create {DATA_DIR}, falling back to current dir: {e}")
         DATA_DIR = os.path.dirname(__file__)

HISTORY_PATH = os.path.join(DATA_DIR, 'f1_history.json')
CACHE_DIR = os.path.join(DATA_DIR, 'f1_cache')

# Defaults
DEFAULT_CONFIG = {
    "mqtt_broker": "localhost",
    "mqtt_port": 1883,
    "mqtt_topic": "f1/watchability/data",
    "mqtt_username": "",
    "mqtt_password": "",
    "check_interval_seconds": 3600,
    "fastf1_cache_dir": CACHE_DIR,
    "timezone": "UTC"
}

def load_config():
    # Start with defaults
    config = DEFAULT_CONFIG.copy()

    # Override with config.json if present
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            logger.error(f"Failed to load config file: {e}")

    # Override with Environment Variables
    if os.getenv("MQTT_BROKER"): config["mqtt_broker"] = os.getenv("MQTT_BROKER")
    if os.getenv("MQTT_PORT"): config["mqtt_port"] = int(os.getenv("MQTT_PORT"))
    if os.getenv("MQTT_TOPIC"): config["mqtt_topic"] = os.getenv("MQTT_TOPIC")
    if os.getenv("MQTT_USERNAME"): config["mqtt_username"] = os.getenv("MQTT_USERNAME")
    if os.getenv("MQTT_PASSWORD"): config["mqtt_password"] = os.getenv("MQTT_PASSWORD")
    if os.getenv("CHECK_INTERVAL"): config["check_interval_seconds"] = int(os.getenv("CHECK_INTERVAL"))
    if os.getenv("TZ"): config["timezone"] = os.getenv("TZ")
    
    return config

def setup_fastf1(cache_dir):
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    fastf1.Cache.enable_cache(cache_dir)
    logger.info(f"FastF1 cache enabled at {cache_dir}")

def publish_mqtt(config, payload):
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if config.get("mqtt_username"):
            client.username_pw_set(config["mqtt_username"], config["mqtt_password"])
        
        client.connect(config["mqtt_broker"], config["mqtt_port"], 60)
        client.publish(config["mqtt_topic"], json.dumps(payload), retain=True)
        client.disconnect()
        logger.info(f"Published data to {config['mqtt_topic']}")
    except Exception as e:
        logger.error(f"Failed to publish to MQTT: {e}")

def calculate_metrics(session):
    """
    Calculate telemetry metrics for a given race session.
    mirrors the logic in calibrate_weights.py and pyscript
    """
    try:
        session.load(laps=True, telemetry=True, weather=True, messages=True)
        laps = session.laps
        
        if laps.empty:
            return None

        total_laps = session.total_laps
        if not total_laps or total_laps == 0:
            total_laps = laps['LapNumber'].max()
        
        # 1. Weather Volatility
        wet_laps = laps[laps['Compound'].isin(['INTERMEDIATE', 'WET'])].shape[0]
        weather_score = (wet_laps / laps.shape[0]) if laps.shape[0] > 0 else 0
        
        # 2. Safety Car / VSC
        msgs = session.race_control_messages
        safety_score = 0
        if msgs is not None:
             sc = msgs[msgs['Message'].str.contains('SAFETY CAR', case=False, na=False)].shape[0]
             vsc = msgs[msgs['Message'].str.contains('VIRTUAL SAFETY CAR', case=False, na=False)].shape[0]
             safety_score = (sc + vsc) / total_laps

        # 3. Lead Changes
        leaders = laps[laps['Position'] == 1].sort_values('LapNumber')
        lap_leaders = leaders.drop_duplicates(subset=['LapNumber'], keep='last')
        
        lead_changes = 0
        previous_leader = None
        for _, row in lap_leaders.iterrows():
            curr_leader = row['Driver']
            if previous_leader is not None and curr_leader != previous_leader:
                lead_changes += 1
            previous_leader = curr_leader
        
        lead_changes_normalized = lead_changes / total_laps

        # 4. Overtakes (Approximation)
        laps_sorted = laps.sort_values(['Driver', 'LapNumber'])
        
        # Identify pit laps
        is_pit_lap = (~pd.isna(laps_sorted['PitInTime'])) | (~pd.isna(laps_sorted['PitOutTime']))
        
        laps_sorted['PrevPos'] = laps_sorted.groupby('Driver')['Position'].shift(1)
        laps_sorted['PrevPitIn'] = laps_sorted.groupby('Driver')['PitInTime'].shift(1)
        laps_sorted['PrevPitOut'] = laps_sorted.groupby('Driver')['PitOutTime'].shift(1)
        
        valid_transition = (
            (pd.isna(laps_sorted['PitInTime'])) & 
            (pd.isna(laps_sorted['PitOutTime'])) &
            (pd.isna(laps_sorted['PrevPitIn'])) &
            (pd.isna(laps_sorted['PrevPitOut']))
        )
        
        laps_sorted['PosChange'] = (laps_sorted['Position'] - laps_sorted['PrevPos']).fillna(0).abs()
        
        total_pos_change = laps_sorted.loc[valid_transition, 'PosChange'].sum()
        overtakes_proxy = total_pos_change / total_laps

        # 5. Pit Stop Intensity
        pit_stops = laps['PitInTime'].notna().sum()
        pit_intensity = pit_stops / total_laps if total_laps > 0 else 0

        # 6. Retirement Rate
        results = session.results
        if results is not None and not results.empty:
            started = len(results)
            finished = results[results['Status'].str.contains('Finished|\\+1 Lap|\\+2 Laps|\\+3 Laps', case=False, na=False)].shape[0]
            retirement_rate = (started - finished) / started if started > 0 else 0
        else:
            retirement_rate = 0

        return {
            "overtakes_per_lap": overtakes_proxy,
            "lead_changes": lead_changes_normalized,
            "weather_volatility_index": weather_score,
            "safety_car_laps_ratio": safety_score,
            "pit_stop_intensity": pit_intensity,
            "retirement_rate": retirement_rate
        }

    except Exception as e:
        logger.error(f"Error processing metrics: {e}")
        return None

def get_remaining_points(schedule, current_round, session_type):
    future_rounds = schedule[schedule['RoundNumber'] > current_round]
    remaining_gps = 0
    remaining_sprints = 0
    for _, row in future_rounds.iterrows():
        remaining_gps += 1
        if 'sprint' in str(row['EventFormat']).lower():
            remaining_sprints += 1
    if session_type == 'Sprint':
        remaining_gps += 1
    return (remaining_gps * 26) + (remaining_sprints * 8)

def calculate_standings_metrics(year, round_num, session_type, schedule):
    import collections
    driver_points = collections.defaultdict(float)
    schedule_races = schedule[schedule['RoundNumber'] > 0]
    
    gap_before, leader_before = 0.0, None
    
    for _, event in schedule_races.iterrows():
        r_num = int(event['RoundNumber'])
        if r_num > round_num:
            break
            
        is_sprint_weekend = 'sprint' in str(event['EventFormat']).lower()
        sessions = ['Sprint', 'R'] if is_sprint_weekend else ['R']
        
        for s_type in sessions:
            if r_num == round_num:
                if session_type == 'Sprint' and s_type == 'R':
                    break
                    
            if r_num == round_num and s_type == session_type:
                sorted_st = sorted(driver_points.items(), key=lambda x: x[1], reverse=True)
                gap_before = sorted_st[0][1] - sorted_st[1][1] if len(sorted_st) >= 2 else 0.0
                leader_before = sorted_st[0][0] if len(sorted_st) >= 1 else None
                
            try:
                session = fastf1.get_session(year, r_num, s_type)
                session.load(laps=False, telemetry=False, weather=False, messages=False)
                results = session.results
                if results is not None and not results.empty:
                    for _, row in results.iterrows():
                        driver = row['Abbreviation']
                        pts = float(row['Points'])
                        driver_points[driver] += pts
            except Exception as e:
                logger.error(f"Error loading {year} Round {r_num} {s_type} in standings calc: {e}")
                
            if r_num == round_num and s_type == session_type:
                break
                
    sorted_after = sorted(driver_points.items(), key=lambda x: x[1], reverse=True)
    gap_after = sorted_after[0][1] - sorted_after[1][1] if len(sorted_after) >= 2 else 0.0
    leader_after = sorted_after[0][0] if len(sorted_after) >= 1 else None
    
    rem_pts_before = get_remaining_points(schedule, round_num, session_type)
    rem_pts_before += 8 if session_type == 'Sprint' else 26
    rem_pts_after = get_remaining_points(schedule, round_num, session_type)
    
    championship_active = 1
    if len(driver_points) >= 2:
        championship_active = 1 if gap_before <= rem_pts_before else 0
        
    title_clinched = 0
    if championship_active == 1 and gap_after > rem_pts_after:
        title_clinched = 1
        
    leader_changed = 0
    if leader_before is not None and leader_after != leader_before:
        leader_changed = 1
        
    tension = 0.0
    if championship_active == 1 and rem_pts_before > 0:
        tension = 1.0 - (gap_before / rem_pts_before)
        
    return {
        "championship_active": championship_active,
        "championship_tension": round(tension, 4),
        "title_clinched": title_clinched,
        "leader_changed": leader_changed
    }

def process_race(year, gp_name, round_num):
    """
    Fetch and score a specific race (and Sprint, if applicable).
    Returns a list of result dictionaries.
    """
    results = []
    
    try:
        # Load Weights
        if not os.path.exists(WEIGHTS_PATH):
            logger.error(f"Weights file not found at {WEIGHTS_PATH}")
            return []
            
        with open(WEIGHTS_PATH, 'r') as f:
            weights_data = json.load(f)
            
        w = weights_data.get('weights', {})
        t = weights_data.get('thresholds', {})
        sw = weights_data.get('sprint_weights', w) # Fallback to standard weights

        # Fetch schedule for standings
        schedule = fastf1.get_event_schedule(year)

        # Process Main Race ('R')
        logger.info(f"Analyzing {year} {gp_name} (Round {round_num})...")
        session = fastf1.get_session(year, round_num, 'R')
        metrics = calculate_metrics(session)
        
        if metrics:
            st = calculate_standings_metrics(year, round_num, 'R', schedule)
            
            score = (
                (metrics['overtakes_per_lap'] * w.get('overtakes_per_lap', 0)) +
                (metrics['lead_changes'] * w.get('lead_changes', 0)) +
                (metrics['weather_volatility_index'] * w.get('weather_volatility_index', 0)) +
                (metrics['safety_car_laps_ratio'] * w.get('safety_car_laps_ratio', 0)) +
                (metrics['pit_stop_intensity'] * w.get('pit_stop_intensity', 0)) +
                (metrics['retirement_rate'] * w.get('retirement_rate', 0)) +
                (st['championship_active'] * w.get('championship_active', 0)) +
                (st['championship_tension'] * w.get('championship_tension', 0)) +
                (st['title_clinched'] * w.get('title_clinched', 0)) +
                (st['leader_changed'] * w.get('leader_changed', 0)) +
                w.get('base_score', 0)
            )
            
            score = max(0, min(10, score))
            score = round(score, 1)
            
            icon = "📺"
            recommendation = "Watch Highlights"
            if score >= t.get('full_race', 8.0):
                icon = "🏎️"
                recommendation = "Watch Full Race"
            elif score >= t.get('race_in_30', 5.0):
                icon = "⏱️"
                recommendation = "Watch Race in 30"
                
            metrics.update(st)
                
            results.append({
                "gp": gp_name,
                "score": score,
                "icon": icon,
                "recommendation": recommendation,
                "date": session.date.strftime("%Y-%m-%d"),
                "round": round_num,
                "metrics": metrics
            })
        else:
             logger.warning(f"Could not calculate metrics for {year} {gp_name} Race.")

        # Process Sprint ('S')
        try:
             sprint_session = fastf1.get_session(year, round_num, 'Sprint')
             # FastF1 raises exception if session doesn't exist, but if it exists we process it
             logger.info(f"Analyzing Sprint for {year} {gp_name} (Round {round_num})...")
             sprint_metrics = calculate_metrics(sprint_session)
             
             if sprint_metrics:
                 st_s = calculate_standings_metrics(year, round_num, 'Sprint', schedule)
                 
                 s_score = (
                     (sprint_metrics['overtakes_per_lap'] * sw.get('overtakes_per_lap', 0)) +
                     (sprint_metrics['lead_changes'] * sw.get('lead_changes', 0)) +
                     (sprint_metrics['weather_volatility_index'] * sw.get('weather_volatility_index', 0)) +
                     (sprint_metrics['safety_car_laps_ratio'] * sw.get('safety_car_laps_ratio', 0)) +
                     (sprint_metrics['pit_stop_intensity'] * sw.get('pit_stop_intensity', 0)) +
                     (sprint_metrics['retirement_rate'] * sw.get('retirement_rate', 0)) +
                     (st_s['championship_active'] * sw.get('championship_active', 0)) +
                     (st_s['championship_tension'] * sw.get('championship_tension', 0)) +
                     (st_s['title_clinched'] * sw.get('title_clinched', 0)) +
                     (st_s['leader_changed'] * sw.get('leader_changed', 0)) +
                     sw.get('base_score', 0)
                 )
                 
                 s_score = max(0, min(10, s_score))
                 s_score = round(s_score, 1)
                 
                 sprint_icon = "📺"
                 sprint_recommendation = "Watch Highlights"
                 if s_score >= t.get('full_race', 8.0):
                     sprint_icon = "🏎️"
                     sprint_recommendation = "Watch Full Race"
                 elif s_score >= t.get('race_in_30', 5.0):
                     sprint_icon = "⏱️"
                     sprint_recommendation = "Watch Race in 30"
                     
                 sprint_metrics.update(st_s)
                     
                 results.append({
                     "gp": f"{gp_name} Sprint",
                     "score": s_score,
                     "icon": sprint_icon,
                     "recommendation": sprint_recommendation,
                     "date": sprint_session.date.strftime("%Y-%m-%d"),
                     "round": round_num,
                     "metrics": sprint_metrics
                 })
             else:
                  logger.warning(f"Could not calculate sprint metrics for {year} {gp_name}.")
        except Exception as e:
             if 'ValueError' not in str(type(e)) and 'Not Found' not in str(e): # Hack for older fastf1 versions rejecting 'Sprint'
                 logger.debug(f"No sprint found for {gp_name}: {e}")
        
        return results
        
    except Exception as e:
        logger.error(f"Error processing race {year} {gp_name}: {e}")
        return results

def fetch_recent_races(config, limit=5):
    """
    Fetch the last N races, spanning across year boundaries if necessary.
    """
    # Silence FastF1
    logging.getLogger('fastf1').setLevel(logging.WARNING)
    setup_fastf1(config['fastf1_cache_dir'])
    
    now = datetime.datetime.now()
    year = now.year
    results = []
    
    while len(results) < limit and year > 2000: # Safety cap
        logger.info(f"Fetching races for schedule year {year}...")
        try:
            schedule = fastf1.get_event_schedule(year)
            event_dates = pd.to_datetime(schedule['EventDate']).dt.tz_localize(None)
            # Filter for completed events excluding testing
            past_events = schedule[
                (event_dates < now) & 
                (~schedule['EventName'].str.contains('Testing|Presse', case=False, na=False))
            ]
            
            if past_events.empty:
                year -= 1
                continue
            
            # Iterate in reverse (newest first)
            for _, row in past_events.iloc[::-1].iterrows():
                gp_name = row['EventName']
                round_num = int(row['RoundNumber'])
                
                batch_results = process_race(year, gp_name, round_num)
                if batch_results:
                    # process_race returns newest session first (Race, then Sprint usually)
                    # We want to add them and check if we hit the limit
                    for res in batch_results:
                        results.append(res)
                        if len(results) >= limit:
                            break
                
                if len(results) >= limit:
                    break
            
            year -= 1 # Next iteration checks previous year
            
        except Exception as e:
            logger.error(f"Error fetching schedule for {year}: {e}")
            year -= 1
            
    # Final sort (though they should be mostly in order)
    results.sort(key=lambda x: x['date'], reverse=True)
    return results[:limit]

def fetch_next_race(config):
    """
    Fetch the next upcoming race.
    """
    # Silence FastF1
    logging.getLogger('fastf1').setLevel(logging.WARNING)
    setup_fastf1(config['fastf1_cache_dir'])
    
    now = datetime.datetime.now()
    year = now.year
    
    try:
        schedule = fastf1.get_event_schedule(year)
        event_dates = pd.to_datetime(schedule['EventDate']).dt.tz_localize(None)
        # Filter for future events excluding testing
        future_events = schedule[
            (event_dates >= now) & 
            (~schedule['EventName'].str.contains('Testing|Presse', case=False, na=False))
        ]
        
        if future_events.empty:
            year += 1
            schedule = fastf1.get_event_schedule(year)
            event_dates = pd.to_datetime(schedule['EventDate']).dt.tz_localize(None)
            future_events = schedule[
                (event_dates >= now) & 
                (~schedule['EventName'].str.contains('Testing|Presse', case=False, na=False))
            ]
            
        if not future_events.empty:
            next_event = future_events.iloc[0]
            
            # Try to get Session5Date (Race), fallback to EventDate
            race_date = next_event['EventDate']
            if 'Session5Date' in next_event and not pd.isnull(next_event['Session5Date']):
                race_date = next_event['Session5Date']
                
            return {
                "gp_name": next_event['EventName'],
                "round": int(next_event['RoundNumber']),
                "date": next_event['EventDate'].isoformat() if not pd.isnull(next_event['EventDate']) else None,
                "race_date": race_date.isoformat() if not pd.isnull(race_date) else None
            }
    except Exception as e:
        logger.error(f"Error fetching next race for {year}: {e}")
        
    return None

def update_history(new_results):
    """
    Merge new results into history.
    """
    history = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, 'r') as f:
                history = json.load(f)
        except:
            pass
            
    # Merge logic
    # Create dict for fast lookup
    history_map = {(x['date'], x['gp']): x for x in history}
    
    changed = False
    for res in new_results:
        key = (res['date'], res['gp'])
        if key not in history_map:
            history_map[key] = res
            changed = True
        # Could update if force refresh needed, but assume immutable for now
            
    if changed:
        # Re-list and sort
        history = list(history_map.values())
        history.sort(key=lambda x: x['date'], reverse=True)
        history = history[:5] # Keep 5
        
        with open(HISTORY_PATH, 'w') as f:
            json.dump(history, f, indent=4)
            
    return history

def main():
    logger.info("Starting F1 Watchability Worker...")
    config = load_config()
    
    # Set timezone for the process if provided
    if config.get("timezone"):
        os.environ["TZ"] = config["timezone"]
        try:
            time.tzset()
            logger.info(f"Timezone set to {config['timezone']}")
        except AttributeError:
            # tzset is only available on Unix
            pass
            
    logger.info(f"Current local time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    
    # Initial Check: Do we have history?
    history = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, 'r') as f:
                history = json.load(f)
        except:
            pass
            
    if len(history) < 5:
        logger.info(f"History has {len(history)} items. Fetching recent races to fill...")
        new_results = fetch_recent_races(config, limit=5)
        history = update_history(new_results)
        
    # Publish init
    if history:
         next_race = fetch_next_race(config)
         payload = {
            "last_updated": datetime.datetime.now().isoformat(),
            "current_race": history[0],
            "history": history,
            "next_race": next_race
        }
         publish_mqtt(config, payload)
    
    while True:
        try:
            # Normal periodic check (just check latest)
            # Actually, `fetch_recent_races` with limit=1 is fine
            new_results = fetch_recent_races(config, limit=1)
            
            next_race = fetch_next_race(config)
            
            if new_results:
                history = update_history(new_results)
                
            if history:
                payload = {
                    "last_updated": datetime.datetime.now().isoformat(),
                    "current_race": history[0],
                    "history": history,
                    "next_race": next_race
                }
                
                publish_mqtt(config, payload)
                
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            
        logger.info(f"Sleeping for {config['check_interval_seconds']} seconds...")
        time.sleep(config['check_interval_seconds'])

if __name__ == "__main__":
    main()