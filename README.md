# F1 Watchability Worker (Docker)

This service runs the F1 Watchability inference engine in a standalone Docker container.

## Overview

-   **Worker**: `worker.py` (Python 3.9+)
-   **Dependencies**: `fastf1`, `pandas`, `paho-mqtt`
-   **Communication**: Publishes race scores to an MQTT broker.

## Configuration

1.  Copy `config.json` (if not already mapped):
    ```json
    {
        "mqtt_broker": "192.168.1.X",
        "mqtt_port": 1883,
        "mqtt_topic": "f1/watchability/data",
        "mqtt_username": "user",
        "mqtt_password": "password",
        "check_interval_seconds": 3600,
        "fastf1_cache_dir": "/app/f1_cache"
    }
    ```

2.  **Weights**: Ensure `weights.json` is present. It is usually copied from the Calibration step or included in the build.

## Running

### With Docker Compose (Recommended)

```bash
docker-compose up -d --build
```

### With Portainer

1.  **Create a Stack**:
    -   Go to **Stacks** -> **Add stack**.
    -   Name: `f1-watchability`.
    -   **Build Method**: Upload the `docker` folder or point to a repository if/when you push this.
    -   **Web Editor**: Paste the contents of `docker-compose.yml`.

2.  **Environment Variables**:
    -   You can define `MQTT_BROKER`, `MQTT_USERNAME`, etc., directly in the stack environment variables section.

3.  **Persistence**:
    -   The stack uses a single named volume `f1_data` mounted to `/data` to store both `f1_history.json` and the FastF1 cache.

4.  **Weights**:
    -   Ensure `weights.json` is included in the build context (folder you upload) OR bind-mount it if you want to update it frequently.

### Manual Run

```bash
docker build -t f1-watchability .
docker run -d \
  -v $(pwd)/config.json:/app/config.json \
  -v $(pwd)/f1_cache:/app/f1_cache \
  f1-watchability
```

## MQTT Payload

The worker publishes JSON to `f1/watchability/data`:

```json
{
    "last_updated": "2023-10-01T12:00:00",
    "current_race": {
        "gp": "Qatar Grand Prix",
        "score": 8.5,
        "icon": "🏎️",
        "recommendation": "Watch Full Race",
        "metrics": { ... }
    },
    "history": [ ... ]
}
```
