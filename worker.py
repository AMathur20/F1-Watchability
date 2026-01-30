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
HISTORY_PATH = os.path.join(os.path.dirname(__file__), 'f1_history.json')
CACHE_DIR = os.path.join(os.path.dirname(__file__), 'f1_cache')

# Defaults
DEFAULT_CONFIG = {
    "mqtt_broker": "localhost",
    "mqtt_port": 1883,
    "mqtt_topic": "f1/watchability/data",
    "mqtt_username": "",
    "mqtt_password": "",
    "check_interval_seconds": 3600,
    "fastf1_cache_dir": CACHE_DIR
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
    
    return config

def setup_fastf1(cache_dir):
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    fastf1.Cache.enable_cache(cache_dir)
    logger.info(f"FastF1 cache enabled at {cache_dir}")

def publish_mqtt(config, payload):
    try:
        client = mqtt.Client()
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

        return {
            "overtakes_per_lap": overtakes_proxy,
            "lead_changes": lead_changes_normalized,
            "weather_volatility_index": weather_score,
            "safety_car_laps_ratio": safety_score
        }

    except Exception as e:
        logger.error(f"Error processing metrics: {e}")
        return None

def fetch_and_score_latest_race(config):
    # Silence FastF1
    logging.getLogger('fastf1').setLevel(logging.WARNING)
    
    setup_fastf1(config['fastf1_cache_dir'])
    
    now = datetime.datetime.now()
    year = now.year
    
    try:
        schedule = fastf1.get_event_schedule(year)
        past_races = schedule[schedule['EventDate'] < now]
        if past_races.empty:
            logger.info("No past races found for this year.")
            return None
            
        last_race = past_races.iloc[-1]
        gp_name = last_race['EventName']
        round_num = int(last_race['RoundNumber'])
        
        logger.info(f"Analyzing {gp_name} (Round {round_num})...")
        
        session = fastf1.get_session(year, round_num, 'R')
        metrics = calculate_metrics(session)
        
        if not metrics:
            logger.warning("Could not calculate metrics.")
            return None
            
        # Load Weights
        if not os.path.exists(WEIGHTS_PATH):
            logger.error(f"Weights file not found at {WEIGHTS_PATH}")
            return None
            
        with open(WEIGHTS_PATH, 'r') as f:
            weights_data = json.load(f)
            
        w = weights_data.get('weights', {})
        t = weights_data.get('thresholds', {})
        
        # Calculate Score
        # Formula: sum(metric * weight) + base_score
        score = (
            (metrics['overtakes_per_lap'] * w.get('overtakes_per_lap', 0)) +
            (metrics['lead_changes'] * w.get('lead_changes', 0)) +
            (metrics['weather_volatility_index'] * w.get('weather_volatility_index', 0)) +
            (metrics['safety_car_laps_ratio'] * w.get('safety_car_laps_ratio', 0)) +
            w.get('base_score', 0)
        )
        
        score = max(0, min(10, score))
        score = round(score, 1)
        
        # Determine Icon/Recommendation
        icon = "📺"
        recommendation = "Watch Highlights"
        if score >= t.get('full_race', 8.0):
            icon = "🏎️"
            recommendation = "Watch Full Race"
        elif score >= t.get('race_in_30', 5.0):
            icon = "⏱️"
            recommendation = "Watch Race in 30"
            
        result = {
            "gp": gp_name,
            "score": score,
            "icon": icon,
            "recommendation": recommendation,
            "date": session.date.strftime("%Y-%m-%d"),
            "round": round_num,
            "metrics": metrics
        }
        
        return result
        
    except Exception as e:
        logger.error(f"Error fetching/scoring race: {e}")
        return None

def update_history(result):
    history = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, 'r') as f:
                history = json.load(f)
        except:
            pass
            
    # Deduplicate
    existing_idx = next((i for i, x in enumerate(history) if x['gp'] == result['gp'] and x['date'] == result['date']), None)
    
    if existing_idx is not None:
        history[existing_idx] = result
    else:
        history.append(result)
        
    # Sort
    history.sort(key=lambda x: x['date'], reverse=True)
    history = history[:5]
    
    with open(HISTORY_PATH, 'w') as f:
        json.dump(history, f, indent=4)
        
    return history

def main():
    logger.info("Starting F1 Watchability Worker...")
    config = load_config()
    
    while True:
        try:
            result = fetch_and_score_latest_race(config)
            
            if result:
                logger.info(f" scored {result['gp']}: {result['score']}")
                history = update_history(result)
                
                # Payload for MQTT
                payload = {
                    "last_updated": datetime.datetime.now().isoformat(),
                    "current_race": result,
                    "history": history
                }
                
                publish_mqtt(config, payload)
                
            else:
                logger.info("No result obtained. Using cached history if available.")
                if os.path.exists(HISTORY_PATH):
                    with open(HISTORY_PATH, 'r') as f:
                        history = json.load(f)
                    
                    if history:
                         payload = {
                            "last_updated": datetime.datetime.now().isoformat(),
                            "current_race": history[0],
                            "history": history
                        }
                         publish_mqtt(config, payload)
        
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            
        logger.info(f"Sleeping for {config['check_interval_seconds']} seconds...")
        time.sleep(config['check_interval_seconds'])

if __name__ == "__main__":
    main()
