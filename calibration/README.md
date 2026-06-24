# F1 Watchability - Calibration

This folder contains the calibration logic for the F1 Watchability Engine.

## Setup

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Years (Optional)**:
    -   Edit `calibration_config.json` to specify which seasons to process.
    -   Default: `[2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018]`

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
-   **Telemetry Features**:
    -   *Overtakes*: Approximated via total position changes per lap.
    -   *Lead Changes*: Distinct lap leaders.
    -   *Weather*: Fraction of race on Wet/Intermediate tires.
    -   *Safety Cars*: Frequency of SC/VSC interventions (ratio of safety car laps to total laps).
    -   *Pit Stop Intensity*: Average number of pit stops per driver.
    -   *Retirement Rate*: Fraction of drivers who retired (DNF).
-   **Driver Championship Standings Context Features**:
    -   *Championship Active* (`championship_active`): Binary (0 or 1) indicating if the championship was mathematically undecided *before* the session started.
    -   *Championship Tension* (`championship_tension`): Float (0.0 to 1.0) indicating closeness of the title fight before the session: $1.0 - \frac{\text{Gap}_{1\text{st vs }2\text{nd}}}{\text{Max Points Remaining}}$.
    -   *Title Clinched* (`title_clinched`): Binary (0 or 1) indicating if the champion mathematically clinched the title *during* this session.
    -   *Leader Changed* (`leader_changed`): Binary (0 or 1) indicating if the leader of the driver standings changed as a result of this session.
-   **Model**: Linear Regression (Rating ~ Overtakes + LeadChanges + Weather + SafetyCars + PitStops + DNFs + ChampionshipActive + ChampionshipTension + TitleClinched + LeaderChanged). Trained separately for Grand Prix races and Sprint sessions.
