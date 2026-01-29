# F1 Watchability Engine

An open-source tool for Home Assistant to calculate and display a spoiler-free "Watchability Score" for Formula 1 races.

## Overview

This system helps you decide whether to watch a full race, a condensed version, or just the highlights, based on objective telemetry data correlated with historical fan sentiment.

The project is divided into three components:

1.  **Calibration** (`/calibration`):
    -   Scrapes historical fan ratings.
    -   Analyzes race telemetry (FastF1).
    -   Generates a weighted model (`weights.json`).

2.  **Inference** (`/pyscript`):
    -   Home Assistant Pyscript.
    -   Runs automatically 5 minutes after a race finishes.
    -   Calculates the score using real-time data and the calibrated model.
    -   Updates `sensor.f1_watchability`.

3.  **UI** (`/ui`):
    -   Lovelace Dashboard card.
    -   Displays the score, recommendation (🏎️/⏱️/📺), and history.

## Getting Started

### Prerequisites

-   **Home Assistant** with [Pyscript](https://github.com/custom-components/pyscript) installed.
-   Python 3.9+ (for local calibration).

### Installation

1.  **Generate Weights (Optional)**:
    -   If you want to recalibrate the model, go to `/calibration`.
    -   Update `calibration_config.json` if needed (e.g., to add a new season).
    -   Run `python calibrate_weights.py`.
    -   Otherwise, use the provided default `weights.json`.

2.  **Setup Inference**:
    -   Copy `pyscript/f1_watchability.py`, `pyscript/weights.json`, and `pyscript/f1_history.json` to your Home Assistant `config/pyscript/` folder.
    -   Ensure `fastf1` is installed in your Home Assistant environment.

3.  **Setup UI**:
    -   Open `/ui/dashboard.yaml` and copy the code.
    -   Add a "Manual" card to your Home Assistant dashboard and paste the code.

4.  **Run Tests**:
    -   To verify the logic and scraper:
        ```bash
        python3 -m unittest discover tests
        ```

## License

Apache 2.0
