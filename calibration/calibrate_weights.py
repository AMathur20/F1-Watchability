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
log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)

import logging
warning_handler = logging.FileHandler('calibration_warnings.log')
warning_handler.setLevel(logging.WARNING)
warning_handler.setFormatter(logging.Formatter(log_format))
logging.getLogger().addHandler(warning_handler)

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
                # logging.info(f"Using cached HTML for {cache_key}")
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

class F1HotOrNotScraper:
    API_URL = "https://api.f1hotornot.com/v0/races"

    def __init__(self):
        pass

    def get_all_ratings(self, years):
        """
        Scrape all ratings for the specified years from F1HotOrNot
        Returns a dictionary: {(year, gp_name_normalized): rating}
        """
        ratings_map = {}
        target_years = set(years)
        limit = 50
        page = 1
        
        while True:
            logging.info(f"Scraping F1HotOrNot - Page {page}")
            url = f"{self.API_URL}?page={page}&limit={limit}&sport=F1"
            import math
            try:
                response = requests.get(url, timeout=10)
                if response.status_code != 200:
                    logging.error(f"Failed to fetch page {page}: Status {response.status_code}")
                    break
                
                data = response.json()
                races = data.get("races", [])
                
                if not races:
                    break
                    
                for race in races:
                    year = race.get("season_year")
                    if year not in target_years:
                        continue
                        
                    raw_name = race.get("race_name")
                    is_sprint = race.get("is_sprint", False)
                    summary_str = race.get("race_summary")
                    
                    if not summary_str:
                        continue
                        
                    try:
                        summary_data = json.loads(summary_str)
                        avg = summary_data.get("avg")
                        if avg is not None:
                            normalized_score = (avg + 2) * 2.5
                            
                            # Clean up old F1HotOrNot suffixes
                            import re
                            name = re.sub(r'\s+-\s+(Race|Sprint Qualifying|Sprint)', '', raw_name)
                            name = name.replace(" GP", " Grand Prix")
                            
                            if "Emilia Romagna" in name or "Emilia-Romagna" in name:
                                name = "Emilia Romagna Grand Prix"
                            elif "São Paulo" in name:
                                name = "Sao Paulo Grand Prix"
                                
                            if is_sprint:
                                name += " Sprint"
                                
                            ratings_map[(year, name)] = normalized_score
                    except Exception as e:
                        logging.warning(f"Failed to extract score for {year} {raw_name}: {e}")
                
                total_pages = math.ceil(data.get("total", 0) / limit)
                if page >= total_pages:
                    break
                page += 1
            except Exception as e:
                logging.error(f"Error fetching page {page}: {e}")
                break
                
        return ratings_map

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
        
        # 1. Weather Volatility (Improved: includes compound changes)
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
            "lead_changes_per_lap": lead_changes_normalized,
            "weather_volatility": weather_score,
            "safety_car_freq": safety_score,
            "pit_stop_intensity": pit_intensity,
            "retirement_rate": retirement_rate
        }

    except Exception as e:
        logging.error(f"Error processing telemetry for {session.event['EventName']}: {e}")
        return None

def normalize_gp_name(name):
    """Normalize GP name for matching."""
    # "Abu Dhabi Grand Prix" -> "Abu Dhabi"
    # "Rolex Belgian Grand Prix" -> "Belgian" ?
    # Keep simple: lower case, remove "grand prix"
    return name.lower().replace(" grand prix", "").strip()

def main():
    setup_fastf1()
    config = load_config()
    years = config.get('years', [])
    
    # Load existing data
    df_existing = load_existing_data()
    
    # Track new data
    new_data = []
    
    if not df_existing.empty:
        # Key: (Year, GP Name normalized)
        df_existing['year'] = df_existing['year'].astype(int) # Ensure year is int
        existing_keys = set(zip(df_existing['year'], df_existing['gp_name']))
    else:
        existing_keys = set()
    
    # 1. Scrape Ratings FIRST (Batch)
    logging.info("Step 1: Scraping Ratings from F1HotOrNot...")
    scraper = F1HotOrNotScraper()
    ratings_map = scraper.get_all_ratings(years)
    logging.info(f"Found ratings for {len(ratings_map)} races.")
    
    # Normalize keys in ratings_map for easier lookup
    # {(2025, "Abu Dhabi Grand Prix"): 7.5}
    # -> {(2025, "abu dhabi"): 7.5}
    # But wait, FastF1 names might be slightly different ("Abu Dhabi Grand Prix" vs "Abu Dhabi")
    # Let's create a lookup based on year + normalized name
    lookup_map = {}
    for (y, raw_name), rating in ratings_map.items():
        lookup_map[(y, normalize_gp_name(raw_name))] = rating
        
    
    # 2. Process Telemetry
    logging.info("Step 2: Processing Telemetry for matching races...")
    
    # Sort years to process in order
    for year in sorted(years):
        logging.info(f"Processing Season {year}...")
        try:
            schedule = fastf1.get_event_schedule(year)
            completed_races = schedule[schedule['EventDate'] < datetime.now()]
            
            for _, event in completed_races.iterrows():
                gp_name = event['EventName']
                start_date = event['EventDate']
                
                # Skip pre-season testing
                if "Presse" in gp_name or "Testing" in gp_name:
                    continue
                
                # Find Rating and existing data
                norm_name = normalize_gp_name(gp_name)
                
                if not df_existing.empty and 'year' in df_existing.columns and 'gp_name' in df_existing.columns:
                    existing_row = df_existing[(df_existing['year'] == year) & (df_existing['gp_name'] == gp_name)]
                else:
                    existing_row = pd.DataFrame()
                
                # Check if we should skip GP
                skip_gp = False
                if not existing_row.empty:
                    row = existing_row.iloc[0]
                    # Check if all new metric columns exist and are not null
                    has_metrics = 'pit_stop_intensity' in row and 'retirement_rate' in row and not pd.isna(row['pit_stop_intensity'])
                    if has_metrics:
                        skip_gp = True
                    else:
                        logging.info(f"  > {gp_name} ({year}) gp found in CSV but missing metrics. Backfilling...")
                
                # Check Sprint
                sprint_name = f"{gp_name} Sprint"
                if not df_existing.empty and 'year' in df_existing.columns and 'gp_name' in df_existing.columns:
                    existing_sprint = df_existing[(df_existing['year'] == year) & (df_existing['gp_name'] == sprint_name)]
                else:
                    existing_sprint = pd.DataFrame()
                
                skip_sprint = (not existing_sprint.empty)
                
                if skip_gp and skip_sprint:
                    logging.info(f"  > Skipping {gp_name} ({year}) - GP and Sprint already in data.")
                    continue
                
                # Determine rating: Priority 1: Scraped Map, Priority 2: Existing CSV
                rating = lookup_map.get((year, norm_name))
                if rating is None and not existing_row.empty:
                    rating = existing_row.iloc[0]['rating']
                    logging.info(f"    Using existing rating from CSV: {rating:.2f}")
                    
                sprint_norm_name = normalize_gp_name(sprint_name)
                sprint_rating = lookup_map.get((year, sprint_norm_name))
                if sprint_rating is None and not existing_sprint.empty:
                    sprint_rating = existing_sprint.iloc[0]['rating']
                
                if rating is None:
                    logging.warning(f"  > No rating found for {gp_name} ({year}). (Normalized: {norm_name})")
                    continue
                
                logging.info(f"  > Processing {gp_name} ({year}) - Rating: {rating:.2f}")
                
                # Get Telemetry
                try:
                    if not skip_gp:
                        # R Session
                        session_r = fastf1.get_session(year, gp_name, 'R')
                        metrics_r = get_telemetry_metrics(session_r)
                        
                        if metrics_r:
                            metrics_r['rating'] = rating
                            metrics_r['gp_name'] = gp_name
                            metrics_r['year'] = year
                            new_data.append(metrics_r)
                            logging.info(f"    Added Race data: {metrics_r}")
                            
                            # Save incrementally
                            temp_df = pd.DataFrame(new_data)
                            if not df_existing.empty:
                                temp_combined = pd.concat([df_existing, temp_df], ignore_index=True)
                            else:
                                temp_combined = temp_df
                            temp_combined.drop_duplicates(subset=['year', 'gp_name'], keep='last', inplace=True)
                            temp_combined.to_csv(DATA_FILE, index=False)

                    if not skip_sprint:
                        # Sprint Session
                        try:
                            session_s = fastf1.get_session(year, gp_name, 'Sprint')
                            metrics_s = get_telemetry_metrics(session_s)
                            if metrics_s:
                                # Apply specific sprint rating if available
                                s_rating = sprint_rating if sprint_rating is not None else rating
                                metrics_s['rating'] = s_rating
                                metrics_s['gp_name'] = f"{gp_name} Sprint"
                                metrics_s['year'] = year
                                new_data.append(metrics_s)
                                logging.info(f"    Added Sprint data: {metrics_s}")
                                
                                temp_df = pd.DataFrame(new_data)
                                if not df_existing.empty:
                                    temp_combined = pd.concat([df_existing, temp_df], ignore_index=True)
                                else:
                                    temp_combined = temp_df
                                temp_combined.drop_duplicates(subset=['year', 'gp_name'], keep='last', inplace=True)
                                temp_combined.to_csv(DATA_FILE, index=False)
                        except Exception as e:
                            if 'ValueError' not in str(type(e)) and 'Not Found' not in str(e):
                                logging.debug(f"    No sprint session found for {gp_name}: {e}")

                except Exception as e:
                    logging.error(f"    Failed to get session for {gp_name}: {e}")

        except Exception as e:
            logging.error(f"Error getting schedule for {year}: {e}")

    # Final Merge and Regression
    df_final = load_existing_data() # Reload to get everything including incremental saves
    
    if len(df_final) < 5:
        logging.error("Not enough data points for regression (need at least 5). Aborting.")
        return

    # Separate Data
    df_race = df_final[~df_final['gp_name'].str.contains('Sprint')]
    df_sprint = df_final[df_final['gp_name'].str.contains('Sprint')]
    
    if len(df_race) < 5:
        logging.error("Not enough RACE data points for regression (need at least 5). Aborting.")
        return

    # Regression - Main Race
    logging.info("Step 3: Calculating Race Weights...")
    features = [
        'overtakes_per_lap', 
        'lead_changes_per_lap', 
        'weather_volatility', 
        'safety_car_freq',
        'pit_stop_intensity',
        'retirement_rate',
        'championship_active',
        'championship_tension',
        'title_clinched',
        'leader_changed'
    ]
    X_race = df_race[features]
    y_race = df_race['rating']

    model_race = LinearRegression()
    model_race.fit(X_race, y_race)

    weights = {
        "formula_version": "4.0",
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "weights": {
            "overtakes_per_lap": round(model_race.coef_[0], 4),
            "lead_changes": round(model_race.coef_[1], 4),
            "weather_volatility_index": round(model_race.coef_[2], 4),
            "safety_car_laps_ratio": round(model_race.coef_[3], 4),
            "pit_stop_intensity": round(model_race.coef_[4], 4),
            "retirement_rate": round(model_race.coef_[5], 4),
            "championship_active": round(model_race.coef_[6], 4),
            "championship_tension": round(model_race.coef_[7], 4),
            "title_clinched": round(model_race.coef_[8], 4),
            "leader_changed": round(model_race.coef_[9], 4),
            "base_score": round(model_race.intercept_, 4)
        },
        "thresholds": {
             "full_race": 8.0,
             "race_in_30": 5.0,
             "highlights_only": 0.0
        },
        "metadata": {
            "normalization_factor": "total_laps",
            "description": f"Calibrated on {len(df_race)} GP races and {len(df_sprint)} Sprint races."
        }
    }

    # Regression - Sprint
    if len(df_sprint) >= 5:
        logging.info("Step 4: Calculating Sprint Weights...")
        X_sprint = df_sprint[features]
        y_sprint = df_sprint['rating']
        
        model_sprint = LinearRegression()
        model_sprint.fit(X_sprint, y_sprint)
        
        weights["sprint_weights"] = {
            "overtakes_per_lap": round(model_sprint.coef_[0], 4),
            "lead_changes": round(model_sprint.coef_[1], 4),
            "weather_volatility_index": round(model_sprint.coef_[2], 4),
            "safety_car_laps_ratio": round(model_sprint.coef_[3], 4),
            "pit_stop_intensity": round(model_sprint.coef_[4], 4),
            "retirement_rate": round(model_sprint.coef_[5], 4),
            "championship_active": round(model_sprint.coef_[6], 4),
            "championship_tension": round(model_sprint.coef_[7], 4),
            "title_clinched": round(model_sprint.coef_[8], 4),
            "leader_changed": round(model_sprint.coef_[9], 4),
            "base_score": round(model_sprint.intercept_, 4)
        }
    else:
        logging.warning(f"Not enough SPRINT data points for regression ({len(df_sprint)}). Falling back to race weights.")
        weights["sprint_weights"] = weights["weights"]

    with open(WEIGHTS_FILE, 'w') as f:
        json.dump(weights, f, indent=4)
    
    logging.info("Calibration complete. Weights saved to weights.json")
    logging.info(f"GP Weights: {weights['weights']}")
    logging.info(f"Sprint Weights: {weights['sprint_weights']}")

if __name__ == "__main__":
    main()
