"""Trial files on disk.

Each trial is one folder:
    trial.json      metadata, events, summary (rewritten atomically on change)
    trajectory.csv  one row per tracked frame
    video.mp4       raw camera footage (optional), for review or re-processing
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .config import MetricsConfig, ScoringConfig
from .geometry import atomic_write_text
from .metrics import StatsAccumulator, penalty_s, rank_key
from .tracker import TrackPoint

TRAJ_FIELDS = ["t", "x_mm", "y_mm", "vx_mms", "vy_mms", "heading_deg", "state", "track_id", "measured"]
OUTCOMES = ("finished", "dnf")
_ID_RE = re.compile(r"^[\w\-]+$")


def slugify(name: str) -> str:
    return re.sub(r"[^\w\-]+", "-", name.strip()).strip("-")[:40] or "team"


class TrialRecorder:
    """Writes one trial while it is running. Thread-safe."""

    def __init__(
        self,
        trials_dir: Path,
        team: str,
        maze: dict,
        metrics_cfg: MetricsConfig,
        record_video: bool,
        video_fps: float,
    ):
        started = datetime.now()
        base = f"{started:%Y%m%d-%H%M%S}_{slugify(team)}"
        trial_id, n = base, 1
        while (trials_dir / trial_id).exists():
            n += 1
            trial_id = f"{base}-{n}"
        self.dir = trials_dir / trial_id
        self.dir.mkdir(parents=True)
        self._lock = threading.RLock()
        self._stats = StatsAccumulator(metrics_cfg)
        self._next_event = 1
        self._record_video = record_video
        self._video_fps = video_fps or 30.0
        self._video: cv2.VideoWriter | None = None
        self._video_frames = 0
        self._csv_file = open(self.dir / "trajectory.csv", "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(TRAJ_FIELDS)
        self._last_flush_t = 0.0
        self.last_point: TrackPoint | None = None
        self.info = {
            "version": 2,
            "id": trial_id,
            "team": team.strip(),
            "started_at": started.isoformat(timespec="seconds"),
            "status": "running",
            "outcome": None,
            "notes": "",
            "duration_s": 0.0,
            "events": [],
            "summary": None,
            "maze": maze,
            "video": None,
        }
        self._write_info()

    @property
    def id(self) -> str:
        return self.info["id"]

    def _write_info(self) -> None:
        atomic_write_text(self.dir / "trial.json", json.dumps(self.info, indent=2))

    def add_frame(self, frame: np.ndarray) -> None:
        if not self._record_video:
            return
        if self._video is None:
            h, w = frame.shape[:2]
            path = self.dir / "video.mp4"
            self._video = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*"mp4v"), self._video_fps, (w, h)
            )
            if not self._video.isOpened():
                self._record_video = False
                self._video = None
                return
            self.info["video"] = path.name
        self._video.write(frame)
        self._video_frames += 1

    def add_point(self, trial_t: float, p: TrackPoint | None) -> None:
        with self._lock:
            self._stats.tick(p is not None and p.measured)
            self.info["duration_s"] = trial_t
            if p is None:
                return
            self.last_point = p
            self._stats.add(trial_t, p.x_mm, p.y_mm, p.track_id)
            self._csv.writerow([
                f"{trial_t:.3f}", f"{p.x_mm:.1f}", f"{p.y_mm:.1f}", f"{p.vx_mms:.1f}",
                f"{p.vy_mms:.1f}", "" if p.heading_deg is None else f"{p.heading_deg:.1f}",
                p.state.value, p.track_id, int(p.measured),
            ])
            if trial_t - self._last_flush_t >= 1.0:
                self._csv_file.flush()
                self._last_flush_t = trial_t

    def add_event(self, kind: str, t: float, severity: int | None = None, source: str = "manual") -> dict:
        with self._lock:
            p = self.last_point
            event = {
                "id": self._next_event,
                "kind": kind,
                "t": round(t, 3),
                "severity": severity,
                "x_mm": None if p is None else round(p.x_mm, 1),
                "y_mm": None if p is None else round(p.y_mm, 1),
                "source": source,
            }
            self._next_event += 1
            self.info["events"].append(event)
            self._write_info()
            return event

    def remove_event(self, event_id: int) -> bool:
        with self._lock:
            before = len(self.info["events"])
            self.info["events"] = [e for e in self.info["events"] if e["id"] != event_id]
            if len(self.info["events"]) == before:
                return False
            self._write_info()
            return True

    def live(self) -> dict:
        with self._lock:
            s = self._stats.summary(self.info["duration_s"], self.info["events"]).to_dict()
            s["current_speed_mms"] = self._stats.current_speed_mms
            return s

    def finish(self, duration_s: float, outcome: str | None, notes: str = "") -> dict:
        with self._lock:
            self._csv_file.close()
            if self._video is not None:
                self._video.release()
                self._video = None
            self.info["duration_s"] = duration_s
            self.info["status"] = "done"
            self.info["outcome"] = outcome if outcome in OUTCOMES else None
            self.info["notes"] = notes
            if self._video_frames and duration_s > 0:
                self.info["video_fps"] = self._video_frames / duration_s
            self.info["summary"] = self._stats.summary(duration_s, self.info["events"]).to_dict()
            self._write_info()
            return dict(self.info)


def read_trajectory(path: Path) -> dict[str, list]:
    keys = ("t", "x_mm", "y_mm", "speed_mms", "heading_deg", "track_id", "measured")
    cols: dict[str, list] = {k: [] for k in keys}
    if not path.exists():
        return cols
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                t, x, y = float(row["t"]), float(row["x_mm"]), float(row["y_mm"])
                vx, vy = float(row["vx_mms"]), float(row["vy_mms"])
            except (KeyError, TypeError, ValueError):
                continue  # partially written last line of a live trial
            cols["t"].append(t)
            cols["x_mm"].append(x)
            cols["y_mm"].append(y)
            cols["speed_mms"].append(round((vx * vx + vy * vy) ** 0.5, 1))
            h = row.get("heading_deg") or ""
            cols["heading_deg"].append(float(h) if h else None)
            cols["track_id"].append(int(row.get("track_id") or 0))
            cols["measured"].append(row.get("measured") == "1")
    return cols


class TrialStore:
    """Read side: listing, loading, editing and ranking trials."""

    def __init__(self, trials_dir: Path, metrics_cfg: MetricsConfig, scoring: ScoringConfig):
        self.dir = trials_dir
        self.metrics_cfg = metrics_cfg
        self.scoring = scoring
        self._lock = threading.Lock()
        self._info_cache: dict[str, tuple[float, dict]] = {}
        self._traj_cache: dict[str, tuple[float, dict]] = {}

    def _path(self, trial_id: str) -> Path:
        if not _ID_RE.match(trial_id) or trial_id.startswith("_"):
            raise KeyError(trial_id)
        return self.dir / trial_id

    def get(self, trial_id: str) -> dict:
        path = self._path(trial_id) / "trial.json"
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            raise KeyError(trial_id) from None
        with self._lock:
            cached = self._info_cache.get(trial_id)
            if cached and cached[0] == mtime:
                return cached[1]
        info = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._info_cache[trial_id] = (mtime, info)
        return info

    def list(self) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for d in self.dir.iterdir():
            if d.is_dir() and not d.name.startswith("_") and (d / "trial.json").exists():
                try:
                    out.append(self.get(d.name))
                except (KeyError, json.JSONDecodeError):
                    continue
        out.sort(key=lambda i: i.get("started_at", ""), reverse=True)
        return out

    def trajectory(self, trial_id: str) -> dict[str, list]:
        path = self._path(trial_id) / "trajectory.csv"
        if not path.exists():
            raise KeyError(trial_id)
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._traj_cache.get(trial_id)
            if cached and cached[0] == mtime:
                return cached[1]
        data = read_trajectory(path)
        with self._lock:
            self._traj_cache[trial_id] = (mtime, data)
        return data

    def video_path(self, trial_id: str) -> Path | None:
        info = self.get(trial_id)
        if not info.get("video"):
            return None
        p = self._path(trial_id) / info["video"]
        return p if p.exists() else None

    def update(self, trial_id: str, team: str | None = None, outcome: str | None | bool = False,
               notes: str | None = None) -> dict:
        """Edit a finished trial. outcome=False means 'leave unchanged'."""
        info = dict(self.get(trial_id))
        if info.get("status") == "running":
            raise ValueError("Stop the trial before editing it.")
        if team is not None and team.strip():
            info["team"] = team.strip()
        if outcome is not False:
            info["outcome"] = outcome if outcome in OUTCOMES else None
        if notes is not None:
            info["notes"] = notes
        atomic_write_text(self._path(trial_id) / "trial.json", json.dumps(info, indent=2))
        return info

    def discard(self, trial_id: str) -> None:
        """Move a trial to trials/_discarded (recoverable by moving it back)."""
        src = self._path(trial_id)
        if not src.exists():
            raise KeyError(trial_id)
        if self.get(trial_id).get("status") == "running":
            raise ValueError("Stop the trial before discarding it.")
        dst_root = self.dir / "_discarded"
        dst_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst_root / trial_id))
        with self._lock:
            self._info_cache.pop(trial_id, None)
            self._traj_cache.pop(trial_id, None)

    def recover(self) -> list[str]:
        """Close out trials left 'running' by a crash, rebuilding their summary from disk."""
        fixed = []
        for info in self.list():
            if info.get("status") != "running":
                continue
            traj = read_trajectory(self._path(info["id"]) / "trajectory.csv")
            stats = StatsAccumulator(self.metrics_cfg)
            for t, x, y, tid, m in zip(traj["t"], traj["x_mm"], traj["y_mm"],
                                       traj["track_id"], traj["measured"]):
                stats.tick(m)
                stats.add(t, x, y, tid)
            duration = traj["t"][-1] if traj["t"] else info.get("duration_s", 0.0)
            info = dict(info, status="interrupted", duration_s=duration)
            info["summary"] = stats.summary(duration, info.get("events", [])).to_dict()
            atomic_write_text(self._path(info["id"]) / "trial.json", json.dumps(info, indent=2))
            fixed.append(info["id"])
        return fixed

    def leaderboard(self, best_per_team: bool = False) -> list[dict]:
        trials = [t for t in self.list() if t.get("status") != "running" and t.get("summary")]
        trials.sort(key=lambda t: rank_key(t, self.scoring))
        if best_per_team:
            seen, best = set(), []
            for t in trials:
                key = t.get("team", "").lower()
                if key not in seen:
                    seen.add(key)
                    best.append(t)
            trials = best
        rows = []
        for i, t in enumerate(trials, 1):
            s = t["summary"]
            pen = penalty_s(s, self.scoring)
            rows.append({
                "rank": i,
                "id": t["id"],
                "team": t.get("team", ""),
                "started_at": t.get("started_at"),
                "outcome": t.get("outcome"),
                "status": t.get("status"),
                "duration_s": s["duration_s"],
                "penalty_s": pen,
                "adjusted_s": s["duration_s"] + pen if t.get("outcome") == "finished" else None,
                "distance_mm": s["distance_mm"],
                "avg_speed_mms": s["avg_speed_mms"],
                "top_speed_mms": s["top_speed_mms"],
                "collisions": s["collisions"],
                "interventions": s["interventions"],
                "tracked_fraction": s["tracked_fraction"],
            })
        return rows
