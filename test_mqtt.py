import paho.mqtt.client as mqtt
import json
import os
import time

# Config from Env or Defaults
BROKER = os.getenv("MQTT_BROKER", "localhost")
PORT = int(os.getenv("MQTT_PORT", 1883))
TOPIC = os.getenv("MQTT_TOPIC", "f1/watchability/data")
USERNAME = os.getenv("MQTT_USERNAME", "")
PASSWORD = os.getenv("MQTT_PASSWORD", "")

def publish_test():
    payload = {
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "current_race": {
            "gp": "TEST Grand Prix",
            "score": 10.0,
            "icon": "🏎️",
            "recommendation": "Watch Full Race (TEST)",
            "date": "2025-01-01",
            "round": 99,
            "metrics": {"overtakes_per_lap": 99}
        },
        "history": []
    }
    
    try:
        client = mqtt.Client()
        if USERNAME:
            client.username_pw_set(USERNAME, PASSWORD)
            
        print(f"Connecting to {BROKER}:{PORT}...")
        client.connect(BROKER, PORT, 60)
        
        print(f"Publishing to {TOPIC}...")
        client.publish(TOPIC, json.dumps(payload), retain=True)
        
        print("Done! Check Home Assistant.")
        client.disconnect()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    publish_test()
