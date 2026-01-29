# F1 Watchability - Calibration

This folder contains the calibration logic for the F1 Watchability Engine.

## Setup

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Years (Optional)**:
    -   Edit `calibration_config.json` to specify which seasons to process.
    -   Default: `[2022, 2023, 2024, 2025]`

3.  **Run Calibration**:
    ```bash
    python calibrate_weights.py
    ```
    *Note: The script supports incremental updates. If `calibration_data.csv` exists, it will only process new races found in the config years.*

## Output

The script will generate:
-   `cache/`: FastF1 cache directory.
-   `calibration_data.csv`: Raw metrics and ratings for debugging.
-   `weights.json`: The calculated regression coefficients and thresholds.

## Methodology

-   **Ratings**: Scraped from RaceFans.net "Rate the Race" feature.
-   **Telemetry**:
    -   *Overtakes*: Approximated via total position changes per lap.
    -   *Lead Changes*: Distinct lap leaders.
    -   *Weather*: Fraction of race on Wet/Intermediate tires.
    -   *Safety Cars*: Frequency of SC/VSC interventions.
-   **Model**: Linear Regression (Rating ~ Overtakes + LeadChanges + Weather + SafetyCars).
