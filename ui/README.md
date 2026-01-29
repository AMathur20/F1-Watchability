# F1 Watchability - High-Fidelity UI

This folder contains the configuration for a premium, neon-styled F1 dashboard card.

## Requirements

1.  **Home Assistant Frontend**:
    -   [`custom:button-card`](https://github.com/custom-cards/button-card) (Available via HACS).
    -   [`custom:layout-card`](https://github.com/thomasloven/lovelace-layout-card) (Optional, but standard stacks are used here).

2.  **Fonts**:
    -   For the best look, install a font like **Formula1-Display-Bold** or **Inter**.
    -   You can add fonts via `Lovlace Dashboard > Resources` or simple CSS injection card-mod.

## Installation

### Step 1: Add Templates
The dashboard relies on **templates** to avoid 500 lines of duplicate code. 
You must add the templates from `dashboard.yaml` (inside the commented section) to your **Raw Configuration Editor** in Home Assistant.

1.  Edit your Dashboard.
2.  Click the three dots (top right) -> **Raw Configuration Editor**.
3.  Add the `button_card_templates:` block at the top level (see `dashboard.yaml` comments).
4.  Save.

### Step 2: Add the Card
1.  Click **Add Card** -> **Manual**.
2.  Copy the **non-commented** part of `dashboard.yaml` (the `vertical-stack` part).
3.  Paste it into the card editor.

## Features

-   **Neon Glow**: Borders glow Red/Gold/Grey based on score.
-   **SVG Gauges**: Animated circular progress bars for high-impact scores.
-   **Animations**: Pulse effects for "Full Race" and rotating timers for "Race in 30".
-   **Grid Layout**: Top 2 races displayed prominently; older races stacked compactly.
