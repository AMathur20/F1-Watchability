import os
import json
import time
import logging
import datetime
import paho.mqtt.client as mqtt

from f1_data import (
    F1DataClient,
    calculate_session_metrics,
    calculate_standings_metrics,
)

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')
WEIGHTS_PATH = os.path.join(APP_DIR, 'weights.json')

# Persistent Data Directory
ENV_DATA_DIR = os.getenv("DATA_DIR")
if ENV_DATA_DIR:
    DATA_DIR = ENV_DATA_DIR
elif os.path.exists(os.path.join(APP_DIR, 'f1_history.json')):
    DATA_DIR = APP_DIR
else:
    DATA_DIR = "/data"

if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR)
    except Exception as e:
        logger.warning(f"Could not create {DATA_DIR}, falling back to current dir: {e}")
        DATA_DIR = APP_DIR

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
    "cache_dir": CACHE_DIR,
    "timezone": "UTC",
}


def load_config():
    config = DEFAULT_CONFIG.copy()

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            logger.error(f"Failed to load config file: {e}")

    # Backward compatibility: honor legacy fastf1_cache_dir if cache_dir wasn't explicitly customized
    if "fastf1_cache_dir" in config and config["cache_dir"] == CACHE_DIR:
        config["cache_dir"] = config["fastf1_cache_dir"]

    if os.getenv("MQTT_BROKER"):
        config["mqtt_broker"] = os.getenv("MQTT_BROKER")
    if os.getenv("MQTT_PORT"):
        config["mqtt_port"] = int(os.getenv("MQTT_PORT"))
    if os.getenv("MQTT_TOPIC"):
        config["mqtt_topic"] = os.getenv("MQTT_TOPIC")
    if os.getenv("MQTT_USERNAME"):
        config["mqtt_username"] = os.getenv("MQTT_USERNAME")
    if os.getenv("MQTT_PASSWORD"):
        config["mqtt_password"] = os.getenv("MQTT_PASSWORD")
    if os.getenv("CHECK_INTERVAL"):
        config["check_interval_seconds"] = int(os.getenv("CHECK_INTERVAL"))
    if os.getenv("CACHE_DIR"):
        config["cache_dir"] = os.getenv("CACHE_DIR")
    if os.getenv("TZ"):
        config["timezone"] = os.getenv("TZ")

    return config


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


def load_weights():
    if not os.path.exists(WEIGHTS_PATH):
        logger.error(f"Weights file not found at {WEIGHTS_PATH}")
        return None
    with open(WEIGHTS_PATH, 'r') as f:
        return json.load(f)


def score_session(metrics, standings, weights_data, is_sprint=False):
    w = weights_data.get('sprint_weights', weights_data.get('weights', {})) if is_sprint else weights_data.get('weights', {})
    t = weights_data.get('thresholds', {})

    score = (
        (metrics['overtakes_per_lap'] * w.get('overtakes_per_lap', 0))
        + (metrics['lead_changes'] * w.get('lead_changes', 0))
        + (metrics['weather_volatility_index'] * w.get('weather_volatility_index', 0))
        + (metrics['safety_car_laps_ratio'] * w.get('safety_car_laps_ratio', 0))
        + (metrics['pit_stop_intensity'] * w.get('pit_stop_intensity', 0))
        + (metrics['retirement_rate'] * w.get('retirement_rate', 0))
        + (standings['championship_active'] * w.get('championship_active', 0))
        + (standings['championship_tension'] * w.get('championship_tension', 0))
        + (standings['title_clinched'] * w.get('title_clinched', 0))
        + (standings['leader_changed'] * w.get('leader_changed', 0))
        + w.get('base_score', 0)
    )
    score = round(max(0.0, min(10.0, score)), 1)

    icon = "📺"
    recommendation = "Watch Highlights"
    if score >= t.get('full_race', 8.0):
        icon = "🏎️"
        recommendation = "Watch Full Race"
    elif score >= t.get('race_in_30', 5.0):
        icon = "⏱️"
        recommendation = "Watch Race in 30"

    return score, icon, recommendation


def process_session(client, year, event, session_type, schedule, weights_data):
    """Fetch and score a single Race ('R') or Sprint ('Sprint') session."""
    is_sprint = session_type == 'Sprint'
    gp_name = event['gp_name']
    round_num = event['round']
    label = f"{gp_name} Sprint" if is_sprint else gp_name

    logger.info(f"Analyzing {year} {label} (Round {round_num})...")
    metrics = calculate_session_metrics(client, year, event, session_type)
    if not metrics:
        logger.warning(f"Could not calculate metrics for {year} {label}.")
        return None

    standings = calculate_standings_metrics(client, year, round_num, session_type, schedule=schedule)
    score, icon, recommendation = score_session(metrics, standings, weights_data, is_sprint=is_sprint)

    combined_metrics = dict(metrics)
    combined_metrics.update(standings)

    session_date = event['sprint_date'] if is_sprint else event['date']
    return {
        "gp": label,
        "score": score,
        "icon": icon,
        "recommendation": recommendation,
        "date": session_date,
        "round": round_num,
        "metrics": combined_metrics,
    }


def _history_entry_key(entry):
    """Stable identity key for deduplicating history entries across data sources."""
    is_sprint = "sprint" in str(entry.get("gp", "")).lower()
    date_str = str(entry.get("date", ""))
    year_str = date_str[:4]
    round_num = entry.get("round")
    if year_str and round_num is not None:
        return (year_str, int(round_num), is_sprint)
    return (date_str, entry.get("gp", ""), is_sprint)


def _session_sort_key(entry):
    """Sort newest first; on identical dates/rounds, place Main Race ahead of Sprint."""
    is_sprint = "sprint" in str(entry.get("gp", "")).lower()
    return (str(entry.get("date", "")), int(entry.get("round") or 0), 0 if is_sprint else 1)


def fetch_missing_recent_races(client, existing_history, limit=5):
    """
    Inspect the last `limit` completed sessions (Races & Sprints) across season boundaries
    and fetch any that are missing from `existing_history`.
    """
    weights_data = load_weights()
    if not weights_data:
        return []

    existing_keys = {_history_entry_key(item) for item in (existing_history or [])}
    now_utc = datetime.datetime.utcnow()
    year = now_utc.year

    new_results = []
    accounted_count = 0

    while accounted_count < limit and year >= 2023:
        logger.info(f"Checking completed sessions for schedule year {year}...")
        try:
            schedule = client.get_schedule(year)
            if not schedule:
                year -= 1
                continue

            # Build chronological list of completed sessions (newest first)
            completed_sessions = []
            for event in schedule:
                race_dt = event.get("race_datetime")
                if race_dt and race_dt < now_utc:
                    completed_sessions.append((race_dt, event, "R"))
                if event.get("has_sprint"):
                    sprint_dt = event.get("sprint_datetime")
                    if sprint_dt and sprint_dt < now_utc:
                        completed_sessions.append((sprint_dt, event, "Sprint"))

            completed_sessions.sort(key=lambda x: x[0], reverse=True)

            for _, event, s_type in completed_sessions:
                if accounted_count >= limit:
                    break

                is_sprint = s_type == "Sprint"
                session_date = event["sprint_date"] if is_sprint else event["date"]
                key = (str(year), int(event["round"]), is_sprint)

                if key in existing_keys:
                    accounted_count += 1
                    continue

                res = process_session(client, year, event, s_type, schedule, weights_data)
                if res:
                    new_results.append(res)
                    existing_keys.add(key)
                    accounted_count += 1

            year -= 1
        except Exception as e:
            logger.error(f"Error fetching schedule for {year}: {e}")
            year -= 1

    new_results.sort(key=_session_sort_key, reverse=True)
    return new_results


def fetch_next_race(client):
    """Fetch the next upcoming Grand Prix from Jolpica-F1."""
    now_utc = datetime.datetime.utcnow()
    year = now_utc.year

    try:
        for check_year in (year, year + 1):
            schedule = client.get_schedule(check_year)
            future_events = [
                ev for ev in schedule
                if ev.get("race_datetime") and ev["race_datetime"] >= now_utc
            ]
            if future_events:
                next_event = future_events[0]
                race_dt = next_event["race_datetime"]
                return {
                    "gp_name": next_event["gp_name"],
                    "round": int(next_event["round"]),
                    "date": next_event["date"],
                    "race_date": f"{race_dt.isoformat()}Z" if race_dt else None,
                }
    except Exception as e:
        logger.error(f"Error fetching next race for {year}: {e}")

    return None


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.warning(f"Failed to load history from {HISTORY_PATH}: {e}")
    return []


def update_history(new_results):
    """Merge new results into history, keeping the 5 most recent sessions."""
    history = load_history()
    history_map = {_history_entry_key(x): x for x in history}

    changed = False
    for res in new_results:
        key = _history_entry_key(res)
        if key not in history_map:
            history_map[key] = res
            changed = True

    if changed:
        history = list(history_map.values())
        history.sort(key=_session_sort_key, reverse=True)
        history = history[:5]

        with open(HISTORY_PATH, 'w') as f:
            json.dump(history, f, indent=4)
        logger.info(f"Updated history ({len(history)} sessions) at {HISTORY_PATH}")

    return history


def main():
    logger.info("Starting F1 Watchability Worker (OpenF1 + Jolpica-F1)...")
    config = load_config()

    if config.get("timezone"):
        os.environ["TZ"] = config["timezone"]
        try:
            time.tzset()
            logger.info(f"Timezone set to {config['timezone']}")
        except AttributeError:
            pass

    logger.info(f"Current local time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    client = F1DataClient(cache_dir=config["cache_dir"])
    logger.info(f"API cache enabled at {config['cache_dir']}")

    # Initial check: backfill any missing races among the 5 most recent completed sessions
    history = load_history()
    new_results = fetch_missing_recent_races(client, history, limit=5)
    if new_results:
        history = update_history(new_results)

    if history:
        next_race = fetch_next_race(client)
        payload = {
            "last_updated": datetime.datetime.now().isoformat(),
            "current_race": history[0],
            "history": history,
            "next_race": next_race,
        }
        publish_mqtt(config, payload)

    while True:
        logger.info(f"Sleeping for {config['check_interval_seconds']} seconds...")
        time.sleep(config['check_interval_seconds'])

        try:
            history = load_history()
            new_results = fetch_missing_recent_races(client, history, limit=5)
            if new_results:
                history = update_history(new_results)

            next_race = fetch_next_race(client)

            if history:
                payload = {
                    "last_updated": datetime.datetime.now().isoformat(),
                    "current_race": history[0],
                    "history": history,
                    "next_race": next_race,
                }
                publish_mqtt(config, payload)

        except Exception as e:
            logger.error(f"Main loop error: {e}")


if __name__ == "__main__":
    main()
