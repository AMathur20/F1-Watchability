import os
import sys
import re
import json
import math
import time
import logging
from datetime import datetime

import pandas as pd
import numpy as np
import requests
from sklearn.linear_model import LinearRegression

# Import shared OpenF1 + Jolpica-F1 client from ../docker
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(SCRIPT_DIR, '..', 'docker')))
from f1_data import (
    F1DataClient,
    calculate_session_metrics,
    calculate_standings_metrics,
)

# Setup logging
log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)

warning_handler = logging.FileHandler(os.path.join(SCRIPT_DIR, 'calibration_warnings.log'))
warning_handler.setLevel(logging.WARNING)
warning_handler.setFormatter(logging.Formatter(log_format))
logging.getLogger().addHandler(warning_handler)

# Constants
CACHE_DIR = os.path.join(SCRIPT_DIR, 'cache')
WEIGHTS_FILE = os.path.join(SCRIPT_DIR, 'weights.json')
DATA_FILE = os.path.join(SCRIPT_DIR, 'calibration_data.csv')
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'calibration_config.json')
HTML_CACHE_DIR = os.path.join(CACHE_DIR, 'html')
os.makedirs(HTML_CACHE_DIR, exist_ok=True)


def load_config():
    """Load configuration or return defaults."""
    default_config = {"years": [2023, 2024, 2025, 2026]}
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


class F1HotOrNotScraper:
    API_URL = "https://api.f1hotornot.com/v0/races"

    def get_all_ratings(self, years):
        """
        Scrape all ratings for the specified years from F1HotOrNot.
        Returns a dictionary: {(year, gp_name_normalized): rating}
        """
        ratings_map = {}
        target_years = set(years)
        limit = 50
        page = 1

        while True:
            logging.info(f"Scraping F1HotOrNot - Page {page}")
            url = f"{self.API_URL}?page={page}&limit={limit}&sport=F1"
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

                            name = re.sub(r'\s+-\s+(Race|Sprint Qualifying|Sprint)', '', raw_name)
                            name = name.replace(" GP", " Grand Prix")

                            if "Emilia Romagna" in name or "Emilia-Romagna" in name:
                                name = "Emilia Romagna Grand Prix"
                            elif "São Paulo" in name or "Sao Paulo" in name:
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


def normalize_gp_name(name):
    """Normalize GP name for matching across Jolpica, F1HotOrNot, and existing CSV."""
    n = str(name).lower()
    is_sprint = "sprint" in n
    n = n.replace("sprint", "").replace("grand prix", "").replace("gp", "")
    n = n.replace("são paulo", "brazilian").replace("sao paulo", "brazilian")
    n = n.replace("brazil", "brazilian").replace("brazilianian", "brazilian")
    n = n.replace("emilia-romagna", "emilia romagna")
    n = n.replace("mexico city", "mexican").replace("mexico", "mexican").replace("mexicanan", "mexican")
    n = re.sub(r'\s+', ' ', n).strip()
    return f"{n} sprint" if is_sprint else n


def get_telemetry_metrics(client, year, event, session_type='R'):
    """
    Calculate race/sprint metrics via OpenF1 + Jolpica-F1 and map to calibration CSV column names.
    """
    m = calculate_session_metrics(client, year, event, session_type)
    if not m:
        return None
    return {
        "overtakes_per_lap": m["overtakes_per_lap"],
        "lead_changes_per_lap": m["lead_changes"],
        "weather_volatility": m["weather_volatility_index"],
        "safety_car_freq": m["safety_car_laps_ratio"],
        "pit_stop_intensity": m["pit_stop_intensity"],
        "retirement_rate": m["retirement_rate"],
    }


def main():
    client = F1DataClient(cache_dir=CACHE_DIR)
    logging.info(f"OpenF1 + Jolpica-F1 cache enabled at {CACHE_DIR}")

    config = load_config()
    years = config.get('years', [])

    df_existing = load_existing_data()
    new_data = []

    existing_norm_lookup = {}
    if not df_existing.empty:
        df_existing['year'] = df_existing['year'].astype(int)
        for idx, row in df_existing.iterrows():
            existing_norm_lookup[(int(row['year']), normalize_gp_name(row['gp_name']))] = idx

    # 1. Scrape Ratings FIRST (Batch)
    logging.info("Step 1: Scraping Ratings from F1HotOrNot...")
    scraper = F1HotOrNotScraper()
    ratings_map = scraper.get_all_ratings(years)
    logging.info(f"Found ratings for {len(ratings_map)} races.")

    lookup_map = {}
    for (y, raw_name), rating in ratings_map.items():
        lookup_map[(y, normalize_gp_name(raw_name))] = rating

    # 2. Process Telemetry via OpenF1 + Jolpica-F1
    logging.info("Step 2: Processing Telemetry for matching races...")
    now_utc = datetime.utcnow()

    for year in sorted(years):
        logging.info(f"Processing Season {year}...")
        try:
            schedule = client.get_schedule(year)
            completed_races = [
                ev for ev in schedule
                if ev.get('race_datetime') and ev['race_datetime'] < now_utc
            ]

            for event in completed_races:
                gp_name = event['gp_name']
                round_num = event['round']
                norm_name = normalize_gp_name(gp_name)

                existing_gp_idx = existing_norm_lookup.get((year, norm_name))
                existing_row = df_existing.loc[[existing_gp_idx]] if existing_gp_idx is not None else pd.DataFrame()

                skip_gp = False
                if not existing_row.empty:
                    row = existing_row.iloc[0]
                    has_metrics = (
                        'pit_stop_intensity' in row
                        and 'retirement_rate' in row
                        and 'championship_tension' in row
                        and not pd.isna(row['pit_stop_intensity'])
                        and not pd.isna(row['championship_tension'])
                    )
                    if has_metrics:
                        skip_gp = True
                    else:
                        logging.info(f"  > {gp_name} ({year}) found in CSV but missing metrics. Backfilling...")

                sprint_name = f"{gp_name} Sprint"
                sprint_norm_name = normalize_gp_name(sprint_name)
                existing_sp_idx = existing_norm_lookup.get((year, sprint_norm_name))
                existing_sprint = df_existing.loc[[existing_sp_idx]] if existing_sp_idx is not None else pd.DataFrame()

                skip_sprint = (not event.get('has_sprint')) or (not existing_sprint.empty)

                if skip_gp and skip_sprint:
                    logging.info(f"  > Skipping {gp_name} ({year}) - GP and Sprint already in data.")
                    continue

                if year < 2023:
                    logging.warning(
                        f"  > Skipping {gp_name} ({year}): OpenF1 telemetry is only available from 2023 onward."
                    )
                    continue

                rating = lookup_map.get((year, norm_name))
                if rating is None and not existing_row.empty:
                    rating = existing_row.iloc[0]['rating']
                    logging.info(f"    Using existing rating from CSV: {rating:.2f}")

                sprint_rating = lookup_map.get((year, sprint_norm_name))
                if sprint_rating is None and not existing_sprint.empty:
                    sprint_rating = existing_sprint.iloc[0]['rating']

                if rating is None:
                    logging.warning(f"  > No rating found for {gp_name} ({year}). (Normalized: {norm_name})")
                    continue

                logging.info(f"  > Processing {gp_name} ({year}) - Rating: {rating:.2f}")

                try:
                    if not skip_gp:
                        metrics_r = get_telemetry_metrics(client, year, event, 'R')
                        if metrics_r:
                            st_r = calculate_standings_metrics(
                                client, year, round_num, 'R', schedule=schedule, include_gaps=True
                            )
                            metrics_r.update(st_r)
                            metrics_r['rating'] = rating
                            metrics_r['gp_name'] = gp_name
                            metrics_r['year'] = year
                            new_data.append(metrics_r)
                            logging.info(f"    Added Race data: {metrics_r}")

                            temp_df = pd.DataFrame(new_data)
                            temp_combined = (
                                pd.concat([df_existing, temp_df], ignore_index=True)
                                if not df_existing.empty
                                else temp_df
                            )
                            temp_combined.drop_duplicates(subset=['year', 'gp_name'], keep='last', inplace=True)
                            temp_combined.to_csv(DATA_FILE, index=False)

                    if not skip_sprint and event.get('has_sprint'):
                        metrics_s = get_telemetry_metrics(client, year, event, 'Sprint')
                        if metrics_s:
                            st_s = calculate_standings_metrics(
                                client, year, round_num, 'Sprint', schedule=schedule, include_gaps=True
                            )
                            metrics_s.update(st_s)
                            s_rating = sprint_rating if sprint_rating is not None else rating
                            metrics_s['rating'] = s_rating
                            metrics_s['gp_name'] = sprint_name
                            metrics_s['year'] = year
                            new_data.append(metrics_s)
                            logging.info(f"    Added Sprint data: {metrics_s}")

                            temp_df = pd.DataFrame(new_data)
                            temp_combined = (
                                pd.concat([df_existing, temp_df], ignore_index=True)
                                if not df_existing.empty
                                else temp_df
                            )
                            temp_combined.drop_duplicates(subset=['year', 'gp_name'], keep='last', inplace=True)
                            temp_combined.to_csv(DATA_FILE, index=False)

                except Exception as e:
                    logging.error(f"    Failed to get session for {gp_name}: {e}")

        except Exception as e:
            logging.error(f"Error getting schedule for {year}: {e}")

    # Final Merge and Regression
    df_final = load_existing_data()

    if len(df_final) < 5:
        logging.error("Not enough data points for regression (need at least 5). Aborting.")
        return

    df_race = df_final[~df_final['gp_name'].str.contains('Sprint')]
    df_sprint = df_final[df_final['gp_name'].str.contains('Sprint')]

    if len(df_race) < 5:
        logging.error("Not enough RACE data points for regression (need at least 5). Aborting.")
        return

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
        'leader_changed',
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
            "base_score": round(model_race.intercept_, 4),
        },
        "thresholds": {
            "full_race": 8.0,
            "race_in_30": 5.0,
            "highlights_only": 0.0,
        },
        "metadata": {
            "normalization_factor": "total_laps",
            "description": f"Calibrated on {len(df_race)} GP races and {len(df_sprint)} Sprint races.",
        },
    }

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
            "base_score": round(model_sprint.intercept_, 4),
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
