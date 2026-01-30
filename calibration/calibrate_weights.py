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

class RaceFansScraper:
    BASE_URL = "https://www.racefans.net/category/regular-features/rate-the-race/page/{}/"
    
    def __init__(self):
        pass

    def get_all_ratings(self, years):
        """
        Scrape all ratings for the specified years by iterating category pages.
        Returns a dictionary: {(year, gp_name_normalized): rating}
        """
        ratings_map = {}
        target_years = set(years)
        min_year = min(target_years)
        
        page = 1
        found_older_year = False
        
        while not found_older_year:
            logging.info(f"Scraping Rate the Race Category - Page {page}")
            url = self.BASE_URL.format(page)
            html = safe_request_html(url, f"category_page_{page}")
            
            if not html:
                break
                
            soup = BeautifulSoup(html, 'lxml')
            articles = soup.find_all('article')
            
            if not articles:
                logging.info("No articles found on this page. Stopping.")
                break
                
            page_has_relevant_year = False
            
            for article in articles:
                title_tag = article.find('h2', class_='entry-title')
                if not title_tag:
                    continue
                
                link_tag = title_tag.find('a')
                if not link_tag:
                    continue
                    
                title = link_tag.get_text().strip()
                link = link_tag['href']
                
                # Title format: "Rate the race: [Year] [Grand Prix Name]"
                # Regex to extract year
                match = re.search(r'Rate the race:\s*(\d{4})\s*(.*)', title, re.IGNORECASE)
                if match:
                    year = int(match.group(1))
                    gp_name_raw = match.group(2).strip() # "Abu Dhabi Grand Prix"
                    
                    if year < min_year:
                        # Found an article older than we care about
                        # But wait, page might have mixed years (Jan 2025 and Dec 2024)?
                        # Yes, but if we see e.g. 2021 and we only want 2024+, we can probably stop soon.
                        # To be safe, we just mark found_older_year if it's significantly older?
                        # Or just ignore it.
                        pass
                    
                    if year in target_years:
                        page_has_relevant_year = True
                        logging.info(f"Found Race: {title} ({link})")
                        
                        # Fetch the rating from the article
                        rating = self._scrape_poll_rating(link, year, gp_name_raw)
                        if rating is not None:
                            # Normalize GP Name for mapping
                            # FastF1 often uses "Abu Dhabi Grand Prix" or similar.
                            # We'll use a simplified key: (year, gp_name_raw)
                            # Ideally we match loosely later.
                            ratings_map[(year, gp_name_raw)] = rating
            
            # Optimization: If the whole page only has years older than min_year, stop.
            # But simpler logic: iterate until we see a year < min_year - 1 ensuring we are safely past?
            # Or just check logical stopping.
            # For now, let's limit page depth for safety (e.g. 50 pages is ~500 races, plenty for multiple years)
            if page > 50:
                break
            
            # Heuristic: If we found years < min_year, we might be done.
            # Check the LAST article on page.
            last_article_title = articles[-1].find('h2', class_='entry-title').get_text()
            last_match = re.search(r'Rate the race:\s*(\d{4})', last_article_title)
            if last_match and int(last_match.group(1)) < min_year:
                logging.info(f"reached year {last_match.group(1)} < {min_year}, stopping.")
                found_older_year = True
                
            page += 1
            
        return ratings_map

    def _scrape_poll_rating(self, url, year, gp_name):
        """Extract rating from individual race page."""
        # Cache key based on URL hash or simplified name
        cache_key = f"poll_{year}_{gp_name.replace(' ', '_')}"
        html = safe_request_html(url, cache_key)
        
        if not html:
            return None
            
        soup = BeautifulSoup(html, 'lxml')
        
        # Look for wp-polls container
        # Pattern: div with id starting with polls- and ending with -ans
        poll_container = soup.find('div', id=re.compile(r'polls-\d+-ans'))
        
        if not poll_container:
            # logging.warning(f"No poll container found for {year} {gp_name}")
            return None
            
        vote_data = []
        
        for li in poll_container.find_all('li'):
            text = li.get_text(strip=True)
            # Regex to extract Score and Percentage
            # Matches: "10(4%)" or "10 (4%)"
            # Some formats might be slightly different
            match = re.match(r'^(\d+)\s*\((\d+)%\)', text)
            if not match:
                # Try fallback: find small tag
                score_node = li.contents[0] if li.contents else None
                small_node = li.find('small')
                if score_node and small_node:
                    try:
                        score_text = str(score_node).strip()
                        if score_text.isdigit():
                            score = int(score_text)
                            percent_text = small_node.get_text(strip=True).replace('(', '').replace('%)', '').replace('%', '')
                            percent = int(percent_text)
                            vote_data.append((score, percent))
                            continue
                    except ValueError:
                        pass
                continue
                
            score = int(match.group(1))
            percent = int(match.group(2))
            vote_data.append((score, percent))
            
        if not vote_data:
            return None
            
        # Calculate Weighted Average
        weighted_sum = sum(score * percent for score, percent in vote_data)
        total_percent_sum = sum(percent for _, percent in vote_data)
        
        if total_percent_sum == 0:
            return 0
            
        final_rating = weighted_sum / total_percent_sum
        # logging.info(f"  > Scraped Rating for {gp_name}: {final_rating:.2f}")
        return final_rating

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

        return {
            "overtakes_per_lap": overtakes_proxy,
            "lead_changes_per_lap": lead_changes_normalized,
            "weather_volatility": weather_score,
            "safety_car_freq": safety_score
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
    
    if notdf_existing.empty:
        # Key: (Year, GP Name normalized)
        existing_keys = set(zip(df_existing['year'], df_existing['gp_name']))
    else:
        existing_keys = set()
    
    # 1. Scrape Ratings FIRST (Batch)
    logging.info("Step 1: Scraping Ratings from RaceFans...")
    scraper = RaceFansScraper()
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
                
                # Check if already processed
                if (year, gp_name) in existing_keys:
                    logging.info(f"  > Skipping {gp_name} ({year}) - Already in data.")
                    continue
                
                # Find Rating
                norm_name = normalize_gp_name(gp_name)
                rating = lookup_map.get((year, norm_name))
                
                if rating is None:
                    # Try fuzzy match or just log warning
                    # Sometimes names differ: "São Paulo" vs "Sao Paulo"
                    # Simple fix: try replacing special chars?
                    # For now just log
                    logging.warning(f"  > No rating found for {gp_name} ({year}). (Normalized: {norm_name})")
                    continue
                
                logging.info(f"  > Processing {gp_name} ({year}) - Rating: {rating:.2f}")
                
                # Get Telemetry
                try:
                    # We utilize the event name or round number?
                    # fastf1.get_session(year, gp_name, 'R') works well usually.
                    session = fastf1.get_session(year, gp_name, 'R')
                    metrics = get_telemetry_metrics(session)
                    
                    if metrics:
                        metrics['rating'] = rating
                        metrics['gp_name'] = gp_name
                        metrics['year'] = year
                        new_data.append(metrics)
                        logging.info(f"    Added data: {metrics}")
                        
                        # Save incrementally
                        temp_df = pd.DataFrame(new_data)
                        if not df_existing.empty:
                            temp_combined = pd.concat([df_existing, temp_df], ignore_index=True)
                        else:
                            temp_combined = temp_df
                        temp_combined.drop_duplicates(subset=['year', 'gp_name'], keep='last', inplace=True)
                        temp_combined.to_csv(DATA_FILE, index=False)

                except Exception as e:
                    logging.error(f"    Failed to get session for {gp_name}: {e}")

        except Exception as e:
            logging.error(f"Error getting schedule for {year}: {e}")

    # Final Merge and Regression
    df_final = load_existing_data() # Reload to get everything including incremental saves
    
    if len(df_final) < 5:
        logging.error("Not enough data points for regression (need at least 5). Aborting.")
        return

    # Regression
    logging.info("Step 3: Calculating Weights...")
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
    logging.info(f"Weights: {weights['weights']}")

if __name__ == "__main__":
    main()
