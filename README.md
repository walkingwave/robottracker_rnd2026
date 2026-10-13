# RobotTracker

Overhead-camera tracking for maze rover competitions (MIE444). One program
tracks the rover, records each trial, and serves two web pages:

- **Operator panel** (`/operator`, on the tracking computer): camera feed with
  tracking overlay, setup, start/stop, hotkeys for logging collisions and
  interventions.
- **Dashboard** (`/`, for anyone on the network): live map of the run, speed,
  events, replays of past runs and a leaderboard.

No second terminal, no desktop toolkit, nothing to install on the viewing devices.

---

## Install

Python 3.10 or newer.

```bash
git clone https://github.com/walkingwave/robottracker_rnd2026.git
cd robottracker_rnd2026
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -e .
```

## Try it without a camera

```bash
robottracker demo
```

This generates a synthetic overhead video of a rover driving the maze (already
calibrated) and opens the operator panel. Click **Capture background**, type a
team name, press **Space**, and open the dashboard link from the header.

## Running a competition

```bash
robottracker            # same as `robottracker serve`
```

The terminal prints two links: the operator panel (opens automatically) and
the dashboard address to share with students.

### One-time setup (repeat if the camera moves)

The panel's **Setup** checklist walks through these:

1. **Connect** a camera (click **Scan** to list them) or a video file.
2. **Calibrate**: click the four maze corners in the feed, clockwise from
   top-left. The yellow outline and 1 ft grid should then sit exactly on the
   maze. Wrong click order is rejected with an explanation.
3. **Capture background**: take the rover out and click it. RobotTracker
   records about 2 s of the empty maze and compares every frame against it.
4. Put the rover in. It locks on after a few frames and the circle turns green.
   If it locks onto the wrong thing, press **R** and click the rover.

Calibration and background are saved in `setup/` and reused next time.

### Running a trial

| Key | Action |
|---|---|
| `Space` | Start trial (after typing the team name) |
| `F` / `D` | Stop: **Finished** / **DNF** |
| `1` `2` `3` | Log a collision of class 1, 2 or 3 |
| `I` | Log a manual intervention |
| `Z` | Undo the last event (or click × next to any event) |
| `R` | Re-lock: click the rover in the feed |
| `Esc` | Cancel calibrate / re-lock |

Events are stamped with the trial clock and the rover's position at that
moment. With a video file, the clip plays from the start when the trial starts
and the trial stops by itself at the end.

Past trials are listed under the feed. You can fix a team name, set or change
the result, or **Discard** a false start (it moves to `trials/_discarded/`, so
nothing is lost).

### Scoring

Finished runs rank by **run time + penalties**, then DNFs by distance
travelled, then runs with no result. Penalties are set in `config.yaml`
(default: collision class 1/2/3 = +2/+5/+10 s, intervention = +15 s). The
leaderboard can show every run or each team's best, and exports to CSV.

---

## How tracking works

1. **Calibration** maps camera pixels to millimetres on the maze floor
   (homography from the four corners).
2. Each frame is **warped to a top-down view** of the maze, so sizes and
   distances are in real units whatever the camera, resolution or mounting.
3. **Detection** compares the frame with the captured empty-maze background.
   Blobs are filtered by real-world size. Slow lighting drift is absorbed by
   gradually updating the background everywhere except around the rover.
4. A **Kalman filter** (constant velocity) smooths position, estimates
   velocity, and predicts through short occlusions. Detections far from the
   prediction are ignored. If a hand or arm touches the rover, only the
   rover-sized area around the prediction is measured, so the position isn't
   dragged. After `max_coast_s` without the rover, the tracker drops the lock
   and searches again. A re-lock starts a new path segment, so the jump is
   never counted as distance.
5. **Distance** only grows once the rover is `min_step_mm` away from where it
   was last counted. Jitter and spinning in place add nothing. **Top speed**
   is measured over a 0.5 s window, so single-frame spikes can't inflate it.

On synthetic footage the median position error is under 1 mm (95 % of frames
within 9 mm), and measured
distance is within 1 % of the true route (`tests/test_tracker.py`).

### ArUco mode (most robust)

For heading and immunity to shadows and people, put a printed marker on each
rover:

```bash
robottracker marker --id 0 --size-mm 120     # writes marker_0.png
```

Print it at 100 % and mount it flat on top, with the printed top edge toward
the rover's front. Then set `tracker.mode: aruco` (and optionally
`tracker.aruco_id`) in `config.yaml`. No background capture is needed.

### Parallax

The camera sees the top of the rover, which appears shifted away from the
image centre compared with its footprint. For tall rovers far from the centre,
set `tracker.camera_height_mm` and `tracker.target_height_mm` to correct it.

---

## Other commands

```bash
robottracker cameras                         # list camera indices
robottracker process run.mp4 --team "Team A" # track a recorded video offline
robottracker marker --id 3                   # printable ArUco marker
robottracker --config other.yaml --port 9000
```

## Trial files

Each trial is a folder in `trials/`, for example `20261004-143022_Team-A/`:

| File | Contents |
|---|---|
| `trial.json` | Team, result, notes, events, summary statistics |
| `trajectory.csv` | `t, x_mm, y_mm, vx_mms, vy_mms, heading_deg, state, track_id, measured` per frame |
| `video.mp4` | Raw camera footage (turn off with `paths.record_video: false`) |

Coordinates are millimetres from the calibration origin. `heading_deg` is
counter-clockwise from +x, with y pointing up on the map. If the program
crashes mid-trial, the trial is recovered and marked *interrupted* on the next
start.

## Configuration

Everything is in `config.yaml`, which is commented. Every key is optional and
misspelled keys are rejected on start. The tuning knobs you're most likely to
touch:

| Symptom | Setting |
|---|---|
| Shadows or glare detected as the rover | raise `tracker.diff_threshold` |
| Rover not detected (dark rover on dark floor) | lower `tracker.diff_threshold`, re-capture background |
| Locks onto small debris | raise `tracker.min_area_ratio` or set `rover_size_mm` correctly |
| Loses the rover behind tall walls | raise `tracker.max_coast_s` |

The **Detection mask** view in the operator panel shows exactly what the
detector sees.

## Troubleshooting

- **Camera won't open**: close other apps using it (Zoom, Teams, Camera app),
  click **Scan**, and on macOS allow camera access for your terminal.
- **Outline doesn't match the maze**: re-run **Calibrate**. The background is
  cleared automatically and must be re-captured.
- **Students can't open the dashboard**: both devices must be on the same
  network. Allow Python through the firewall, and use the address the
  terminal prints (not `localhost`).
- **Operator panel from a second laptop**: set `server.operator_token` and
  open `http://<tracker-ip>:8000/operator?token=<token>` once.

## Development

```bash
pip install -e ".[dev]"
pytest              # includes accuracy tests on synthetic footage
ruff check src tests
```

```
src/robottracker/
  cli.py         commands
  config.py      config schema and defaults
  geometry.py    calibration, pixel <-> mm, top-down warp
  detection.py   background-difference and ArUco detectors
  kalman.py      constant-velocity Kalman filter
  tracker.py     lock / coast / lose / re-acquire logic
  metrics.py     distance, speed, scoring (single source of truth)
  storage.py     trial recording, listing, leaderboard, crash recovery
  video.py       camera and video-file sources
  engine.py      processing thread, trial control, overlay rendering
  server.py      FastAPI app: pages, live stream (SSE + MJPEG), API
  synthetic.py   synthetic maze footage for the demo and tests
  web/           operator panel and dashboard (plain HTML/JS, no build step)
```

---

*RobotTracker, MIE491 Capstone Design, built for MIE444 rover competitions.*
