# F1 Watchability Engine

An open-source tool for Home Assistant to calculate and display a spoiler-free "Watchability Score" for Formula 1 races.

## Overview

This system helps you decide whether to watch a full race, a condensed version, or just the highlights, based on objective telemetry data correlated with historical fan sentiment.

The project is divided into three components:

1.  **Calibration** (`/calibration`):
    -   Scrapes historical fan ratings.
    -   Analyzes race telemetry (FastF1).
    -   Generates a weighted model (`weights.json`).

2.  **Inference** (`/docker`):
    -   Standalone Docker container.
    -   Runs periodically (default 1h) and checks for race completions.
    -   Calculates score and publishes to Home Assistant via MQTT.
    -   Updates `sensor.f1_watchability`.

3.  **UI** (`/ui`):
    -   Lovelace Dashboard card.
    -   Displays the score, recommendation (🏎️/⏱️/📺), and history.

## Getting Started

### Prerequisites

-   **Home Assistant** with MQTT broker (e.g., Mosquitto).
-   **Docker** (for running the inference worker).
-   Python 3.9+ (for local calibration).

### Installation

1.  **Generate Weights (Optional)**:
    -   If you want to recalibrate the model, go to `/calibration`.
    -   Update `calibration_config.json` if needed.
    -   Run `python calibrate_weights.py`.
    -   Copy `weights.json` to `/docker/weights.json`.

2.  **Setup Inference (Docker)**:
    -   Go to `/docker`.
    -   **Option A (Docker Compose)**: Copy `config.json`, edit it, and run `docker-compose up -d --build`.
    -   **Option B (Portainer)**: Use `docker-compose.yml` as a Stack and configure Environment Variables directly in Portainer.

3.  **Setup Home Assistant**:
    -   Ensure your HA is connected to the same MQTT broker.
    -   Add the sensor configuration from `/ui/mqtt_config.yaml` to your `configuration.yaml` or `sensors.yaml`.
    -   Restart HA to load the new sensor.

4.  **Setup UI**:
    -   Open `/ui/button_card_templates.yaml` and copy the templates to your Dashboard's **Raw Configuration Editor**.
    -   Open `/ui/dashboard.yaml` and copy the card configuration to a "Manual" card on your dashboard.
    -   See `/ui/README.md` for detailed instructions.

5.  **Run Tests**:
    -   To verify the logic and scraper:
        ```bash
        python3 -m unittest discover tests
        ```

## License

Apache 2.0
