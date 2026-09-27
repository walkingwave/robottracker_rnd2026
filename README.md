# RobotTracker

Overhead camera tracking and analytics system for MIE444 autonomous rover competitions.

An instructor runs the **Operator Panel** (desktop app) to start/stop trials and log events. Students watch a live **Dashboard** (web browser) that updates in real time with their rover's path, distance, speed, and event timeline.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Hardware Requirements](#hardware-requirements)
3. [Installation](#installation)
4. [First-Time Setup – Camera Calibration](#first-time-setup--camera-calibration)
5. [Running the System](#running-the-system)
   - [Step 1 – Start the Student Dashboard](#step-1--start-the-student-dashboard)
   - [Step 2 – Launch the Operator Panel](#step-2--launch-the-operator-panel)
   - [Step 3 – Run a Trial](#step-3--run-a-trial)
6. [Operator Panel Reference](#operator-panel-reference)
7. [Student Dashboard Reference](#student-dashboard-reference)
8. [Configuration Reference](#configuration-reference)
9. [Logging Modes – Auto vs Manual](#logging-modes--auto-vs-manual)
10. [Trial Output Files](#trial-output-files)
11. [Troubleshooting](#troubleshooting)
12. [Project Structure](#project-structure)

---

## System Overview

```
Overhead USB Camera
        │
        ▼
 Operator Panel  ──────────────────────────────────────────┐
 (operator_ui.py)                                          │
  • Live camera feed with green tracking box               │
  • Start / Stop trial                                     │  writes to
  • Log collisions (Class 1 / 2 / 3)                      ▼
  • Log manual interventions                          trials/
  • Auto or Manual-only logging modes                 └── RoverName_20260315_143022/
                                                           ├── trajectory.csv
                                                           ├── events.json
                                                           ├── config_snapshot.yaml
                                                           └── recording.lock  ← live indicator
                                                                │
                                                                ▼
                                                      Student Dashboard
                                                      (dashboard.py)
                                                       • Live path map
                                                       • Distance / speed
                                                       • Event timeline
```

---

## Hardware Requirements

| Component | Recommendation |
|---|---|
| Overhead camera | Any USB webcam that can cover the full maze; 1080p preferred |
| Camera mount | ceiling mount directly above the maze centre |
| Computer | Any machine capable of running Python 3.10+ and OpenCV |
| Network | Both operator laptop and student display must be on the same network for dashboard sharing |

**Maze dimensions configured in the system:** 8 ft × 4 ft (2438.4 mm × 1219.2 mm)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/BidoCodeHub/RobotTracker.git
cd RobotTracker
```

### 2. Create and activate a virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Verify installation

```bash
python3 -c "import cv2, streamlit, PIL; print('OK')"
```

---

## First-Time Setup – Camera Calibration

The system maps pixel positions from the camera to real-world millimetre coordinates using a homography matrix. You must calibrate once whenever the camera is moved.

### Run the interactive calibration tool

```bash
python3 main.py --calibrate
```

A window will open showing the live camera feed. **Click the four corners of the maze in this exact order:**

```
1 ── top-left          2 ── top-right
│                               │
4 ── bottom-left       3 ── bottom-right
```

The calibration tool will print four pixel coordinate pairs to the terminal and write them to `config.yaml` under `homography.pixel_points`. The `world_points` (real-world mm coordinates) are already set correctly for an 8 ft × 4 ft maze and do not need to change.



---

## Running the System

Open **two terminal windows** (or two tabs). Both must be in the project directory with the virtual environment activated.

### Step 1 – Start the Student Dashboard

```bash
streamlit run dashboard.py
```

Streamlit will print a local URL (e.g. `http://localhost:8501`). Open this URL in any browser. To share with students on the same network, use the **Network URL** printed below it.

The dashboard will display a "Waiting for trial…" message until the operator starts a trial.

### Step 2 – Launch the Operator Panel

```bash
python3 operator_ui.py
```

A desktop window will open – this is the instructor interface.

### Step 3 – Run a Trial

Follow the steps in the Operator Panel (details in the next section).

---

## Operator Panel Reference

### Layout

```
┌─────────────────────────────────┬──────────────────────┐
│                                 │  🏁 RobotTracker      │
│         LIVE FEED               │  Operator Panel       │
│   (green box = tracked rover)   ├──────────────────────┤
│                                 │  VIDEO SOURCE         │
│                                 │  ● Live Camera        │
│                                 │  ○ Video File         │
│                                 │  Camera [0 (1280x720)▾] [Scan] │
│                                 │  [ Connect ]          │
│                                 ├──────────────────────┤
│                                 │  ROVER / TEAM NAME    │
│                                 │  [ TeamName_______ ]  │
│                                 ├──────────────────────┤
│                                 │  00:00.0              │
│                                 │  [ ▶ START TRIAL ]    │
│                                 │  [ ⏹ STOP TRIAL  ]    │
│                                 ├──────────────────────┤
│                                 │  LOGGING MODE         │
│                                 │  Collisions: Auto     │
│                                 │  Interventions: Auto  │
│                                 ├──────────────────────┤
│                                 │  LOG COLLISION        │
│                                 │  [Class1][Class2][C3] │
│                                 │  [🖐 MANUAL INTERV. ] │
│                                 ├──────────────────────┤
│                                 │  EVENT LOG            │
│                                 │  (scrollable list)    │
└─────────────────────────────────┴──────────────────────┘
```

### Running a Trial – Step by Step

| Step | Action |
|---|---|
| **1** | Select **Live Camera** or **Video File**. For a camera, click **Scan** to list attached cameras (built-in, USB webcam, iPhone Continuity Camera) and pick one from the dropdown. For a file, click **Browse**. Then click **Connect** |
| **2** | Wait for the live feed to appear in the left panel |
| **3** | Type the rover/team name in the **ROVER / TEAM NAME** field |
| **4** | Click **▶ START TRIAL** – the timer starts, recording begins |
| **5** | Monitor the green bounding box in the feed; log collisions/interventions as needed |
| **6** | Click **⏹ STOP TRIAL** when the run is complete |

> When using a video file: the video stays paused on the first frame after Connect. It only starts playing when you click **START TRIAL**. The trial ends automatically when the video finishes.

### Collision Logging

Click the appropriate button when the rover hits a wall:

| Button | Colour | Severity |
|---|---|---|
| **Class 1** | Orange | Minor graze – rover self-recovers immediately |
| **Class 2** | Red | Moderate impact – rover briefly stalls |
| **Class 3** | Purple | Hard collision – rover gets stuck or requires recovery |

### Manual Intervention Logging

Click **🖐 MANUAL INTERVENTION** any time the instructor physically touches or repositions the rover.

---

## Student Dashboard Reference

The student dashboard auto-updates every 2 seconds while a trial is live.

### Sidebar

- **Trial selector** – automatically follows the live trial; after recording stops the last completed trial is shown. Students can also select any past trial from the dropdown.

### Path Tab

- **Map** – full-width overhead view of the maze with the rover's trajectory drawn in blue. Collision and intervention markers are shown on the path.
- **Speedometer** – current instantaneous speed of the rover.
- **Speed Over Time** – chart of rover speed across the trial duration.

### Events Tab

- Timeline of all logged events (auto-detected and manual) with timestamps.

### Stats shown

| Metric | Description |
|---|---|
| Duration | Total elapsed time of the trial |
| Distance Travelled | Net path length in feet – jitter, rotation-in-place, and tracker glitches are all excluded (see [Distance Filtering](#distance-filtering)) |
| Top Speed | Peak speed recorded during the trial |
| Avg Speed | Mean speed across all frames |
| Collisions | Count of wall collision events |
| Interventions | Count of manual intervention events |

---

## Distance Filtering

Raw per-frame position data contains two types of noise that would inflate the distance metric if accumulated directly:

| Noise Type | Cause | Effect without filtering |
|---|---|---|
| **Jitter** | Sub-pixel centroid noise from background subtraction | ~300–600 mm of fake distance per second at 30 fps |
| **Rotation-in-place** | Rover spinning without translating; centroid orbits the rover centre | Accumulates as apparent travel even though the rover hasn't moved |

### How it is filtered

Distance is computed in three layers – in the tracker, in the logger, and in the dashboard – all using the same approach:

1. **1-second time-window chunking** – all frames within each second are averaged into a single position. Random jitter and rotation both cancel out over ~1 second, leaving only genuine net displacement.
2. **30 mm dead-band** – chunk-to-chunk steps smaller than 30 mm are ignored (residual noise after averaging).
3. **400 mm teleport cap** – chunk-to-chunk steps larger than 400 mm are also ignored (tracker glitch / lost tracking).

The dashboard adds a 15-frame median pre-filter and uses 2-second chunks for an extra layer of smoothing.

---

## Configuration Reference

All tuneable parameters live in `config.yaml`. Do not hardcode values in Python files.

```yaml
maze:
  length_ft: 8.0           # Physical maze length
  width_ft: 4.0            # Physical maze width

homography:
  pixel_points:            # Four corners in pixel space – set by --calibrate
    - [x1, y1]             # top-left
    - [x2, y2]             # top-right
    - [x3, y3]             # bottom-right
    - [x4, y4]             # bottom-left

perception:
  bgs_algorithm: MOG2      # Background subtractor: MOG2 or KNN
  bgs_history: 200         # Frames used to build background model
  bgs_var_threshold: 40    # Sensitivity – lower = more sensitive
  min_contour_area_px: 500 # Minimum blob size to consider as rover (pixels²)
  max_contour_area_px: 50000
  max_jump_mm: 200         # Max allowed single-frame displacement (mm)
  max_miss_frames: 10      # Frames lost before track is reset
  position_smoothing_alpha: 0.4  # EMA smoothing – lower = smoother, more lag
  max_velocity_mms: 1500.0 # Physical speed cap (~5 ft/s)

events:
  collision_margin_mm: 50.0     # Distance from wall to trigger collision
  collision_debounce_s: 1.0     # Minimum gap between auto-detected collisions
  stop_velocity_threshold_mms: 5.0   # Speed below which rover is "stopped"
  stop_min_duration_s: 1.0      # How long stopped before logging a stop event

data:
  output_root: trials           # Directory where trial folders are saved
  trajectory_flush_every: 30    # Write CSV to disk every N frames (~1 second)

sensor:
  source: file             # "file" or "camera"
  file_path: data_/WIN_20260225_15_07_32_Pro.mp4
  camera_index: 0          # USB camera index (0 = first camera)
```

### Tuning Tips

- **Rover not detected / box appears on wrong object** → increase `min_contour_area_px` or check lighting and camera angle.
- **Tracker loses the rover mid-run** → decrease `bgs_var_threshold` (more sensitive) or increase `max_miss_frames`.
- **Distance is higher than expected** → decrease `position_smoothing_alpha` for more aggressive EMA smoothing; the 1-second chunking + 30 mm dead-band automatically filters jitter and rotation-in-place.
- **Auto-collision events fire incorrectly** → increase `collision_debounce_s` or switch to **Manual-only** collision mode.

---

## Logging Modes – Auto vs Manual

The Operator Panel offers two independent mode toggles:

| Toggle | Auto mode | Manual only mode |
|---|---|---|
| **Collisions** | System detects wall proximity and logs automatically | Only collisions you click with Class 1/2/3 buttons are recorded |
| **Interventions** | System detects sudden velocity jumps and logs automatically | Only interventions you click 🖐 are recorded |

These can be mixed (e.g. auto collisions + manual interventions). The mode can be changed at any time, including mid-trial.

---

## Trial Output Files

Each trial produces a folder under `trials/` named `<RoverName>_<YYYYMMDD_HHMMSS>/`:

```
trials/
└── TeamAlpha_20260315_143022/
    ├── trajectory.csv        ← per-frame rover state
    ├── events.json           ← timestamped event log
    └── config_snapshot.yaml  ← exact config used for this trial
```

### trajectory.csv columns

| Column | Description |
|---|---|
| `frame_idx` | Frame number |
| `timestamp_s` | Elapsed time in seconds |
| `x_mm` | Rover X position (0 = left wall) |
| `y_mm` | Rover Y position (0 = top wall) |
| `velocity_mms` | Instantaneous speed (mm/s) |
| `heading_deg` | Heading angle (degrees, CCW from East) |
| `event_flags` | Bitmask of active event flags |

### events.json structure

```json
[
  {
    "event_type": "wall_collision",
    "timestamp_s": 14.3,
    "x_mm": 52.1,
    "y_mm": 610.4,
    "metadata": { "source": "manual", "class": "Class 2" }
  }
]
```

---

## Troubleshooting

### Operator Panel window never appears or is blank (macOS)

The macOS system Python (`/usr/bin/python3`, 3.9) bundles Tk 8.5, whose event loop stalls on recent macOS releases, so the window never draws. Use a Python 3.10+ build with Tk 8.6 or newer, then recreate the virtual environment:

```bash
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Check with `python3 -c "import tkinter; print(tkinter.TkVersion)"` (should print 8.6 or 9.0).

### Green tracking box disappears during the run

The tracker lost the rover. Common causes and fixes:

- Camera is moving or vibrating → secure the mount
- Lighting changed (e.g. someone walked past a window) → use consistent artificial lighting
- Rover moved very fast → tracker should recover within `max_miss_frames` (default 10) frames
- `bgs_history` too low → the background model hasn't fully learned the static scene; increase to 300–500

### "Could not open file" error on Connect

- Check that the file path is correct and the video file exists
- Supported formats: `.mp4`, `.avi`, `.mov`, `.mkv`

### Camera stuck on "Connecting…"

- Click **Scan** in the Operator Panel and pick the right camera from the dropdown (or set `camera_index` in `config.yaml`)
- On Linux: check `ls /dev/video*` to find available camera devices
- Ensure no other application is using the camera

### Dashboard not updating

- Confirm `streamlit run dashboard.py` is running in a separate terminal
- The dashboard auto-refreshes every 2 seconds – wait a moment after starting a trial
- Ensure both terminals are pointing to the same project directory (same `trials/` folder)
- **Stuck on a stale "live" trial** – if the Operator Panel was force-quit mid-trial, a `recording.lock` file may have been left behind. The dashboard treats any lock file older than 30 seconds as stale and ignores it automatically. If the dashboard is still stuck, delete the lock file manually: `rm trials/<trial_name>/recording.lock`

### Distance is unrealistically high

- The system automatically filters out jitter and rotation-in-place using 1-second time-window chunking with a 30 mm dead-band (see [Distance Filtering](#distance-filtering))
- If distance is still inflated, reduce `position_smoothing_alpha` in `config.yaml` (e.g. to 0.25) for more aggressive EMA smoothing on the raw centroid position

### Calibration / homography looks wrong (path is skewed or stretched)

- Re-run `python3 main.py --calibrate` and click the corners more precisely
- Ensure the camera is as close to directly overhead as possible; oblique angles introduce perspective error that the homography partially but not fully corrects

---

## Project Structure

```
robottracker/
├── operator_ui.py          ← Instructor desktop app (run this to start)
├── dashboard.py            ← Student web dashboard (streamlit)
├── main.py                 ← CLI entry point (calibration, headless processing)
├── config.yaml             ← All configuration parameters
├── requirements.txt
│
├── rover_tracker/
│   ├── perception/
│   │   ├── tracker.py      ← MOG2/KNN background subtraction, MeanShift tracking,
│   │   │                      adaptive EMA smoothing; reference images pre-loaded at
│   │   │                      startup; BGS model is fully reset between trials
│   │   ├── homography.py   ← Pixel ↔ world coordinate transform
│   │   ├── background1new.jpg  ← Reference background for initial detection
│   │   └── background_mat.jpg  ← Maze boundary mask for initial detection
│   ├── events/
│   │   └── event_detector.py   ← Collision, stop, intervention detection
│   ├── state/
│   │   └── rover_state.py  ← RoverState dataclass; StateHistory with
│   │                          noise-filtered total_distance_mm()
│   ├── data/
│   │   └── trial_logger.py ← CSV/JSON logging, recording.lock management;
│   │                          distance computed with 1-second chunking
│   ├── visualization/
│   │   └── dashboard.py    ← Streamlit dashboard; stale-lock detection;
│   │                          2-second chunking + dead-band distance filtering
│   └── sensor/             ← Camera / video file input abstraction
│
├── tests/                  ← pytest unit tests (24 tests, all passing)
│   ├── test_event_detector.py
│   ├── test_homography.py
│   ├── test_state.py
│   └── test_trial_logger.py
│
├── data_/                  ← Sample reference videos (not committed to git)
└── trials/                 ← Trial output folders (not committed to git)
```

---

## Running Tests

```bash
python3 -m pytest tests/ -v
```

---

*RobotTracker – MIE491 Capstone Design (built for MIE444 Mechatronics Engineering Design rover competitions)*
