import pandas as pd
import fastf1
import collections
import re
import os
import time

CSV_PATH = "/Users/ankurmathur/Documents/F1-Watchability/calibration/calibration_data.csv"
df_csv = pd.read_csv(CSV_PATH)

# Initialize new columns if missing
new_cols = ['championship_active', 'gap_before', 'gap_after', 'title_clinched', 'leader_changed', 'championship_tension']
for col in new_cols:
    if col not in df_csv.columns:
        df_csv[col] = None

def normalize_name(name):
    n = name.lower()
    n = n.replace("grand prix", "").replace("gp", "").strip()
    n = re.sub(r'\s+', ' ', n)
    n = n.replace("são paulo", "sao paulo")
    n = n.replace("emilia-romagna", "emilia romagna")
    return n

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

years = sorted(df_csv['year'].unique())

for year in years:
    # Check if year is already processed
    df_year_existing = df_csv[df_csv['year'] == year]
    if not df_year_existing.empty and df_year_existing['championship_tension'].notna().all():
        print(f"Season {year} already enriched. Skipping...")
        continue

    print(f"Loading data for season {year}...")
    df_year = df_csv[df_csv['year'] == year].copy()
    
    # Enable cache
    cache_dir = os.path.join(os.path.dirname(CSV_PATH), 'cache')
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    fastf1.Cache.enable_cache(cache_dir)
    
    try:
        schedule = fastf1.get_event_schedule(year)
        schedule = schedule[schedule['RoundNumber'] > 0]
    except Exception as e:
        print(f"Error loading schedule for {year}: {e}")
        continue
    
    # Map event names to round numbers
    schedule_map = {}
    for _, event in schedule.iterrows():
        r_num = int(event['RoundNumber'])
        gp_name = event['EventName']
        is_sprint = 'sprint' in str(event['EventFormat']).lower()
        schedule_map[normalize_name(gp_name)] = {
            "round": r_num,
            "gp_name": gp_name,
            "is_sprint": is_sprint
        }
        
    driver_points = collections.defaultdict(float)
    
    def get_standings_info(points_dict):
        if not points_dict:
            return 0.0, None
        sorted_standings = sorted(points_dict.items(), key=lambda x: x[1], reverse=True)
        leader = sorted_standings[0][0]
        leader_pts = sorted_standings[0][1]
        second_pts = sorted_standings[1][1] if len(sorted_standings) > 1 else 0.0
        return leader_pts - second_pts, leader

    ordered_events = sorted(schedule_map.values(), key=lambda x: x['round'])
    standings_at_session = {}
    
    success = True
    for ev in ordered_events:
        r_num = ev['round']
        sessions = ['Sprint', 'R'] if ev['is_sprint'] else ['R']
        
        for s_type in sessions:
            gap_before, leader_before = get_standings_info(driver_points)
            rem_pts_before = get_remaining_points(schedule, r_num, s_type)
            if s_type == 'Sprint':
                rem_pts_before += 8
            else:
                rem_pts_before += 26
                
            championship_active = 1
            if len(driver_points) >= 2:
                championship_active = 1 if gap_before <= rem_pts_before else 0
                
            try:
                session = fastf1.get_session(year, r_num, s_type)
                session.load(laps=False, telemetry=False, weather=False, messages=False)
                results = session.results
                if results is not None and not results.empty:
                    for _, res_row in results.iterrows():
                        driver = res_row['Abbreviation']
                        pts = float(res_row['Points'])
                        driver_points[driver] += pts
            except Exception as e:
                print(f"Error loading {year} Round {r_num} {s_type}: {e}")
                success = False
                break
                
            gap_after, leader_after = get_standings_info(driver_points)
            rem_pts_after = get_remaining_points(schedule, r_num, s_type)
            
            title_clinched = 0
            if championship_active == 1 and gap_after > rem_pts_after:
                title_clinched = 1
                    
            leader_changed = 0
            if leader_before is not None and leader_after != leader_before:
                leader_changed = 1
                
            standings_at_session[(r_num, s_type)] = {
                "championship_active": championship_active,
                "gap_before": gap_before,
                "gap_after": gap_after,
                "title_clinched": title_clinched,
                "leader_changed": leader_changed,
                "remaining_points_before": rem_pts_before
            }
        
        if not success:
            break
        # Avoid rate limits
        time.sleep(0.5)
            
    if not success:
        print(f"Skipping saving for season {year} due to load errors.")
        continue

    # Assign back to DataFrame rows in df_csv
    for idx, row in df_year.iterrows():
        csv_gp_name = row['gp_name']
        is_csv_sprint = "sprint" in csv_gp_name.lower()
        norm_csv_gp = normalize_name(csv_gp_name)
        
        matched_round = None
        for k, v in schedule_map.items():
            if k in norm_csv_gp or norm_csv_gp in k:
                matched_round = v['round']
                break
                
        if matched_round is None:
            continue
            
        s_type = 'Sprint' if is_csv_sprint else 'R'
        standings_info = standings_at_session.get((matched_round, s_type))
        if standings_info is None:
            continue
            
        df_csv.at[idx, 'championship_active'] = standings_info['championship_active']
        df_csv.at[idx, 'gap_before'] = standings_info['gap_before']
        df_csv.at[idx, 'gap_after'] = standings_info['gap_after']
        df_csv.at[idx, 'title_clinched'] = standings_info['title_clinched']
        df_csv.at[idx, 'leader_changed'] = standings_info['leader_changed']
        
        if standings_info['championship_active'] == 1:
            rem_pts_before = standings_info['remaining_points_before']
            tension = 1.0 - (standings_info['gap_before'] / rem_pts_before) if rem_pts_before > 0 else 1.0
            df_csv.at[idx, 'championship_tension'] = round(tension, 4)
        else:
            df_csv.at[idx, 'championship_tension'] = 0.0

    # Save progress after each successful year!
    df_csv.to_csv(CSV_PATH, index=False)
    print(f"Season {year} processed and saved.")

print("Calibration CSV enrichment check completed!")
