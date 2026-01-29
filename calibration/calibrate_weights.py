import fastf1
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
import re
import json
import time
import os
from sklearn.linear_model import LinearRegression
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
CACHE_DIR = os.path.join(os.getcwd(), 'cache')
WEIGHTS_FILE = os.path.join(os.getcwd(), 'weights.json')
DATA_FILE = os.path.join(os.getcwd(), 'calibration_data.csv')
CONFIG_FILE = os.path.join(os.getcwd(), 'calibration_config.json')

# Additional Constants
HTML_CACHE_DIR = os.path.join(os.getcwd(), 'cache', 'html')
if not os.path.exists(HTML_CACHE_DIR):
    os.makedirs(HTML_CACHE_DIR)

def setup_fastf1():
    """Enable FastF1 caching."""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    fastf1.Cache.enable_cache(CACHE_DIR)
    logging.info(f"FastF1 cache enabled at {CACHE_DIR}")

def load_config():
    """Load configuration or return defaults."""
    default_config = {"years": [2022, 2023, 2024, 2025]}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to load config, using defaults: {e}")
    return default_config

def load_existing_data():
    """Load existing calibration data from CSV."""
    if os.path.exists(DATA_FILE):
        try:
            df = pd.read_csv(DATA_FILE)
            logging.info(f"Loaded {len(df)} existing races from {DATA_FILE}")
            return df
        except Exception as e:
            logging.error(f"Error loading existing data: {e}")
    return pd.DataFrame()

def safe_request_html(url, cache_key):
    """
    Fetch HTML with local caching and retry logic.
    """
    # 1. Check Cache
    safe_key = re.sub(r'[^a-zA-Z0-9]', '_', cache_key)
    cache_path = os.path.join(HTML_CACHE_DIR, f"{safe_key}.html")
    
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                logging.info(f"Using cached HTML for {cache_key}")
                return f.read()
        except Exception:
            pass # Fallback to fetch
    
    # 2. Fetch with Retry
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                # Save to cache
                with open(cache_path, 'w', encoding='utf-8') as f:
                    f.write(response.text)
                return response.text
            elif response.status_code == 404:
                return None # Don't retry 404
            
            logging.warning(f"Request failed {url} (Status: {response.status_code}). Retrying...")
            time.sleep(2 * (attempt + 1))
            
        except requests.RequestException as e:
            logging.warning(f"Request error {url} ({e}). Retrying...")
            time.sleep(2 * (attempt + 1))
            
    logging.error(f"Failed to fetch {url} after retries.")
    return None

def get_racefans_rating(year, gp_name):
    """
    Search RaceFans.net for the rating of a specific race.
    """
    # Clean GP name for search
    search_term = gp_name.replace(" Grand Prix", "").strip()
    query = f"{search_term} Grand Prix rated out of ten {year}"
    search_url = f"https://www.racefans.net/?s={requests.utils.quote(query)}"
    
    # Use cached/safe request
    html = safe_request_html(search_url, f"{year}_{gp_name}_search")
    if not html:
        return None
    
    try:
        soup = BeautifulSoup(html, 'lxml')
        articles = soup.find_all('article')
        
        for article in articles:
            title_tag = article.find('h2', class_='entry-title')
            if not title_tag:
                continue
            title = title_tag.get_text().strip()
            
            # Look for patterns like "Bahrain Grand Prix rated 6.5 out of ten"
            if str(year) in title and "rated" in title and "out of ten" in title:
                match = re.search(r'rated (\d+(\.\d+)?) out of ten', title)
                if match:
                    rating = float(match.group(1))
                    logging.info(f"Found Rating for {year} {gp_name}: {rating}")
                    return rating
        
        # Fallback: Try a less specific search? Or just log warning.
        logging.warning(f"No rating found for {year} {gp_name}")
        return None

    except Exception as e:
        logging.error(f"Error scraping {year} {gp_name}: {e}")
        return None

def get_telemetry_metrics(session):
    """
    Calculate telemetry metrics for a given race session.
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
        # Filters:
        # - Remove Pit laps (PitInTime not NaT OR PitOutTime not NaT)
        # - This removes the massive position loss/gain from entering/exiting pits
        
        laps_sorted = laps.sort_values(['Driver', 'LapNumber'])
        
        # Mark pit laps
        # Check if PitInTime or PitOutTime is set
        # 'PitInTime' means they pitted AT THE END of this lap
        # 'PitOutTime' means they pitted AT THE START of this lap (so previous lap was pit in)
        # We should exclude any lap where a pit interaction occurred to be safe?
        # Or just where they lost position?
        # Safest: Exclude laps where PitInTime is not NaT (entering pits) 
        # AND laps where PitOutTime is not NaT (leaving pits).
        
        is_pit_lap = (~pd.isna(laps_sorted['PitInTime'])) | (~pd.isna(laps_sorted['PitOutTime']))
        clean_laps = laps_sorted[~is_pit_lap].copy()
        
        # We need to re-calculate previous position based on CLEAN laps? 
        # No, because that would skip the gap.
        # We want: Delta = Pos(L) - Pos(L-1). If L or L-1 was a pit lap, ignore this delta.
        
        laps_sorted['PrevPos'] = laps_sorted.groupby('Driver')['Position'].shift(1)
        laps_sorted['PrevPitIn'] = laps_sorted.groupby('Driver')['PitInTime'].shift(1)
        laps_sorted['PrevPitOut'] = laps_sorted.groupby('Driver')['PitOutTime'].shift(1)
        
        # Row is valid for overtake calculation if:
        # 1. Current lap is NOT a pit lap
        # 2. Previous lap was NOT a pit lap
        # (If previous lap was pit lap, the delta is "recovery" or "loss" from pit)
        
        valid_transition = (
            (pd.isna(laps_sorted['PitInTime'])) & 
            (pd.isna(laps_sorted['PitOutTime'])) &
            (pd.isna(laps_sorted['PrevPitIn'])) &
            (pd.isna(laps_sorted['PrevPitOut']))
        )
        
        laps_sorted['PosChange'] = (laps_sorted['Position'] - laps_sorted['PrevPos']).fillna(0).abs()
        
        # Only sum changes for valid transitions
        total_pos_change = laps_sorted.loc[valid_transition, 'PosChange'].sum()
        overtakes_proxy = total_pos_change / total_laps

        return {
            "overtakes_per_lap": overtakes_proxy,
            "lead_changes_per_lap": lead_changes_normalized,
            "weather_volatility": weather_score,
            "safety_car_freq": safety_score
        }

    except Exception as e:
        logging.error(f"Error processing telemetry for {session.event['EventName']}: {e}")
        return None

def main():
    setup_fastf1()
    config = load_config()
    years = config.get('years', [])
    
    # Load existing data
    df_existing = load_existing_data()
    
    # Track new data
    new_data = []
    
    # Create a composite key for checking existence (Year + GP Name)
    if not df_existing.empty:
        existing_keys = set(zip(df_existing['year'], df_existing['gp_name']))
    else:
        existing_keys = set()
    
    for year in years:
        logging.info(f"Processing Season {year}...")
        try:
            schedule = fastf1.get_event_schedule(year)
            completed_races = schedule[schedule['EventDate'] < datetime.now()]
            
            for _, event in completed_races.iterrows():
                gp_name = event['EventName']
                # Skip pre-season testing
                if "Presse" in gp_name or "Testing" in gp_name:
                    continue
                
                # Check if already processed
                if (year, gp_name) in existing_keys:
                    logging.info(f"  > Skipping {gp_name} ({year}) - Already in data.")
                    continue

                logging.info(f"  > Processing {gp_name} ({year})")
                
                # 1. Get Rating
                rating = get_racefans_rating(year, gp_name)
                if rating is None:
                    logging.info(f"    Skipping {gp_name} - No rating found.")
                    continue
                
                # 2. Get Telemetry
                try:
                    session = fastf1.get_session(year, gp_name, 'R')
                    metrics = get_telemetry_metrics(session)
                    
                    if metrics:
                        metrics['rating'] = rating
                        metrics['gp_name'] = gp_name
                        metrics['year'] = year
                        new_data.append(metrics)
                        logging.info(f"    Added data for {gp_name}: Rating={rating}, Metrics={metrics}")
                        
                        # Save intermediate progress
                        # (Optional, but good for long runs)
                    
                except Exception as e:
                    logging.error(f"    Failed to get session for {gp_name}: {e}")

        except Exception as e:
            logging.error(f"Error getting schedule for {year}: {e}")

    # Merge and Save
    if new_data:
        df_new = pd.DataFrame(new_data)
        if not df_existing.empty:
            df_final = pd.concat([df_existing, df_new], ignore_index=True)
        else:
            df_final = df_new
        
        # Deduplicate just in case
        df_final.drop_duplicates(subset=['year', 'gp_name'], keep='last', inplace=True)
        
        df_final.to_csv(DATA_FILE, index=False)
        logging.info(f"Saved merged data ({len(df_final)} races) to {DATA_FILE}")
    else:
        logging.info("No new data found. Using existing data.")
        df_final = df_existing

    if len(df_final) < 5:
        logging.error("Not enough data points for regression (need at least 5). Aborting.")
        return

    # Regression
    features = ['overtakes_per_lap', 'lead_changes_per_lap', 'weather_volatility', 'safety_car_freq']
    X = df_final[features]
    y = df_final['rating']

    model = LinearRegression()
    model.fit(X, y)

    weights = {
        "formula_version": "2.0-calibrated",
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "weights": {
            "overtakes_per_lap": round(model.coef_[0], 4),
            "lead_changes": round(model.coef_[1], 4),
            "weather_volatility_index": round(model.coef_[2], 4),
            "safety_car_laps_ratio": round(model.coef_[3], 4),
            "base_score": round(model.intercept_, 4)
        },
        "thresholds": {
             "full_race": 8.0,
             "race_in_30": 5.0,
             "highlights_only": 0.0
        },
        "metadata": {
            "normalization_factor": "total_laps",
            "description": f"Calibrated on {len(df_final)} races."
        }
    }

    with open(WEIGHTS_FILE, 'w') as f:
        json.dump(weights, f, indent=4)
    
    logging.info("Calibration complete. Weights saved to weights.json")

if __name__ == "__main__":
    main()
