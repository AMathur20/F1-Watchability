import os
import sys
import re
import collections

import pandas as pd

# Import shared OpenF1 + Jolpica-F1 client from ../docker
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(SCRIPT_DIR, '..', 'docker')))
from f1_data import F1DataClient, get_remaining_points

CSV_PATH = os.path.join(SCRIPT_DIR, "calibration_data.csv")
CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")


def normalize_name(name):
    n = str(name).lower()
    n = n.replace("sprint", "").replace("grand prix", "").replace("gp", "")
    n = n.replace("são paulo", "brazilian").replace("sao paulo", "brazilian")
    n = n.replace("brazil", "brazilian").replace("brazilianian", "brazilian")
    n = n.replace("emilia-romagna", "emilia romagna")
    n = n.replace("mexico city", "mexican").replace("mexico", "mexican").replace("mexicanan", "mexican")
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def get_standings_info(points_dict):
    if not points_dict:
        return 0.0, None
    sorted_standings = sorted(points_dict.items(), key=lambda x: x[1], reverse=True)
    leader = sorted_standings[0][0]
    leader_pts = sorted_standings[0][1]
    second_pts = sorted_standings[1][1] if len(sorted_standings) > 1 else 0.0
    return leader_pts - second_pts, leader


def main():
    df_csv = pd.read_csv(CSV_PATH)
    client = F1DataClient(cache_dir=CACHE_DIR)

    new_cols = [
        'championship_active',
        'gap_before',
        'gap_after',
        'title_clinched',
        'leader_changed',
        'championship_tension',
    ]
    for col in new_cols:
        if col not in df_csv.columns:
            df_csv[col] = None

    years = sorted(df_csv['year'].unique())

    for year in years:
        year = int(year)
        df_year_existing = df_csv[df_csv['year'] == year]
        if not df_year_existing.empty and df_year_existing['championship_tension'].notna().all():
            print(f"Season {year} already enriched. Skipping...")
            continue

        print(f"Loading standings data for season {year} from Jolpica-F1...")
        df_year = df_csv[df_csv['year'] == year].copy()

        schedule = client.get_schedule(year)
        if not schedule:
            print(f"Error loading schedule for {year}")
            continue

        schedule_map = {}
        for event in schedule:
            schedule_map[normalize_name(event['gp_name'])] = event

        driver_points = collections.defaultdict(float)
        standings_at_session = {}
        success = True

        for ev in schedule:
            r_num = ev['round']
            sessions = ['Sprint', 'R'] if ev['has_sprint'] else ['R']

            for s_type in sessions:
                gap_before, leader_before = get_standings_info(driver_points)
                rem_pts_before = get_remaining_points(schedule, r_num, s_type)
                rem_pts_before += 8 if s_type == 'Sprint' else 26

                championship_active = 1
                if len(driver_points) >= 2:
                    championship_active = 1 if gap_before <= rem_pts_before else 0

                results = client.get_session_results(year, r_num, s_type)
                if not results:
                    print(f"No results for {year} Round {r_num} {s_type}")
                    success = False
                    break

                for res_row in results:
                    driver_points[res_row['driver_code']] += float(res_row['points'])

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
                    "remaining_points_before": rem_pts_before,
                }

            if not success:
                break

        if not success:
            print(f"Skipping saving for season {year} due to load errors.")
            continue

        for idx, row in df_year.iterrows():
            csv_gp_name = row['gp_name']
            is_csv_sprint = "sprint" in str(csv_gp_name).lower()
            norm_csv_gp = normalize_name(csv_gp_name)

            matched_round = None
            for k, ev in schedule_map.items():
                if k in norm_csv_gp or norm_csv_gp in k:
                    matched_round = ev['round']
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

        df_csv.to_csv(CSV_PATH, index=False)
        print(f"Season {year} processed and saved.")

    print("Calibration CSV enrichment check completed!")


if __name__ == "__main__":
    main()
