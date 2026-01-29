import json
import os
import datetime
import logging

# We import fastf1 inside the function to avoid blocking import time
# and to ensure it runs in the executor if needed.
# pyscript handles imports dynamically usually, but fastf1 is heavy.

@service
def f1_watchability_check(force=False):
    """
    Manually trigger a watchability check.
    action: f1_watchability_check
    """
    log.info("F1 Watchability: Manual check triggered")
    calculate_watchability()

@state_trigger("sensor.f1_session_status == 'Finished'")
def on_race_finish():
    """
    Trigger 5 minutes after race finishes.
    """
    log.info("F1 Watchability: Race finished. Waiting 5 minutes...")
    task.sleep(300) # 5 minutes
    calculate_watchability()

@time_trigger("startup")
def on_startup():
    """
    Initialize sensor on startup.
    """
    update_sensor_from_history()

def calculate_watchability():
    """
    Main logic: Fetch data, calculate score, update history.
    """
    log.info("F1 Watchability: Starting calculation...")
    
    # Retry configuration
    retries = [0, 300, 600, 1800, 3600] # Immediately, +5m, +10m, +30m, +1h (deltas)
    
    success = False
    for wait_time in retries:
        if wait_time > 0:
            log.info(f"F1 Watchability: Retrying in {wait_time} seconds...")
            task.sleep(wait_time)
            
        try:
            # Run heavy lifting in executor
            result = task.executor(fetch_and_score_latest_race)
            
            if result:
                log.info(f"F1 Watchability: Success! Score: {result['score']}")
                update_history(result)
                success = True
                break
            else:
                log.warning("F1 Watchability: No data returned from fetch.")
                
        except Exception as e:
            log.error(f"F1 Watchability: Error during calculation: {e}")
            
    if not success:
        log.error("F1 Watchability: Failed to calculate score after all retries.")

def fetch_and_score_latest_race():
    """
    Blocking function to run in executor.
    Fetches data using FastF1 and computes score.
    """
    try:
        import fastf1
        import pandas as pd
        import numpy as np
        
        # --- OPTIMIZATION: Silence FastF1 Logs ---
        # FastF1 is very verbose, which matches HA logs.
        # We suppress it to specific levels.
        # (Assuming fastf1 uses standard logging)
        logging.getLogger('fastf1').setLevel(logging.WARNING)
        
        # --- OPTIMIZATION: Configurable Cache ---
        # Try to find a 'pyscript_config.json' or use default
        current_dir = os.path.dirname(__file__)
        cache_path = os.path.join(current_dir, 'f1_cache')
        
        # If user provides a specific path in 'f1_config.json' (optional)
        config_path = os.path.join(current_dir, 'f1_config.json')
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    uconf = json.load(f)
                    cache_path = uconf.get('cache_dir', cache_path)
            except:
                pass

        try:
            if not os.path.exists(cache_path):
                os.makedirs(cache_path)
            fastf1.Cache.enable_cache(cache_path) 
        except Exception as e:
            # Fallback if we cannot write to disk (rare in HA config dir but possible)
            print(f"F1 Watchability: Could not enable cache at {cache_path}: {e}")
            
        # Get current year
        now = datetime.datetime.now()
        year = now.year
        
        # Get schedule
        schedule = fastf1.get_event_schedule(year)
        past_races = schedule[schedule['EventDate'] < now]
        if past_races.empty:
            return None
            
        last_race = past_races.iloc[-1]
        gp_name = last_race['EventName']
        round_num = last_race['RoundNumber']
        
        # Load session
        session = fastf1.get_session(year, round_num, 'R')
        session.load(laps=True, telemetry=True, weather=True, messages=True)
        
        # --- CALCULATE METRICS (Mirroring Optimized Calibration Logic) ---
        laps = session.laps
        total_laps = session.total_laps
        if not total_laps or total_laps == 0:
            total_laps = laps['LapNumber'].max()
            
        # 1. Weather
        wet_laps = laps[laps['Compound'].isin(['INTERMEDIATE', 'WET'])].shape[0]
        weather_score = (wet_laps / laps.shape[0]) if laps.shape[0] > 0 else 0
        
        # 2. Safety Car
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
        prev = None
        for _, row in lap_leaders.iterrows():
            curr = row['Driver']
            if prev is not None and curr != prev:
                lead_changes += 1
            prev = curr
        lead_changes_norm = lead_changes / total_laps
        
        # 4. Overtakes (Optimization: Filter Pit Stops)
        laps_sorted = laps.sort_values(['Driver', 'LapNumber'])
        
        # Identify pit laps (In or Out)
        is_pit_lap = (~pd.isna(laps_sorted['PitInTime'])) | (~pd.isna(laps_sorted['PitOutTime']))
        
        # Shift logic to find valid transitions
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
        
        # Only sum valid non-pit transitions
        total_pos_change = laps_sorted.loc[valid_transition, 'PosChange'].sum()
        overtakes_proxy = total_pos_change / total_laps

        # --- LOAD WEIGHTS ---
        # Assuming weights.json is in the same directory as this script
        weights_path = os.path.join(os.path.dirname(__file__), 'weights.json')
        with open(weights_path, 'r') as f:
            weights_data = json.load(f)
            
        w = weights_data.get('weights', {})
        t = weights_data.get('thresholds', {})
        
        # --- SCORE CALCULATION ---
        # Formula: sum(metric * weight) + base_score
        # Keys correspond to weights.json provided by user
        score = (
            (overtakes_proxy * w.get('overtakes_per_lap', 0)) +
            (lead_changes_norm * w.get('lead_changes', 0)) +
            (weather_score * w.get('weather_volatility_index', 0)) +
            (safety_score * w.get('safety_car_laps_ratio', 0)) +
            w.get('base_score', 0)
        )
        
        # Clamp Score
        score = max(0, min(10, score))
        score = round(score, 1)
        
        # Determine Icon
        icon = "📺" # Default low
        if score >= t.get('full_race', 8.0):
            icon = "🏎️"
        elif score >= t.get('race_in_30', 5.0):
            icon = "⏱️"
            
        return {
            "gp": gp_name,
            "score": score,
            "icon": icon,
            "date": session.date.strftime("%Y-%m-%d"),
            "round": int(round_num)
        }
        
    except Exception as e:
        # We can't log using pyscript log inside executor easily unless we capture it?
        # Standard print goes to logs usually.
        print(f"F1 Watchability Worker Error: {e}")
        return None

def update_history(result):
    """
    Update the f1_history.json file and the sensor state.
    """
    history_file = os.path.join(os.path.dirname(__file__), 'f1_history.json')
    history = []
    
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r') as f:
                history = json.load(f)
        except:
            history = []
            
    # Add new result (avoid duplicates based on round/date if possible)
    # Check if this GP is already in history (by date/name)
    existing_idx = next((i for i, x in enumerate(history) if x['gp'] == result['gp'] and x['date'] == result['date']), None)
    
    if existing_idx is not None:
        history[existing_idx] = result
    else:
        history.append(result)
        
    # Sort by date descending (Newest first)
    # Date format is YYYY-MM-DD, so string sort works.
    try:
        history.sort(key=lambda x: x['date'], reverse=True)
    except Exception as e:
        print(f"F1 Watchability: Error sorting history: {e}")
        
    # Keep last 5
    history = history[:5]
    
    # Save
    with open(history_file, 'w') as f:
        json.dump(history, f, indent=4)
        
    # Update Sensor
    update_sensor_state(history)

def update_sensor_from_history():
    """
    Read history and update sensor on startup.
    """
    history_file = os.path.join(os.path.dirname(__file__), 'f1_history.json')
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r') as f:
                history = json.load(f)
            update_sensor_state(history)
        except:
            pass

def update_sensor_state(history):
    """
    Set sensor.f1_watchability state and attributes.
    """
    if not history:
        state.set("sensor.f1_watchability", "Unknown", {"icon": "mdi:help", "history": []})
        return
        
    latest = history[0]
    
    # Text recommendation
    rec = "Watch Highlights"
    if latest['icon'] == "🏎️":
        rec = "Watch Full Race"
    elif latest['icon'] == "⏱️":
        rec = "Watch Race in 30"
        
    attributes = {
        "score": latest['score'],
        "icon": latest['icon'],
        "gp_name": latest['gp'],
        "recommendation": rec,
        "history": history, # Full history for finding via list
        "friendly_name": "F1 Watchability"
    }
    
    state.set("sensor.f1_watchability", latest['score'], attributes)
