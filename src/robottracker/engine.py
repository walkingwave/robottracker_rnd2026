"""The processing engine: owns the video source, tracker and running trial.

One background thread reads frames, tracks the rover, writes the trial and
renders the operator view. Every public method is thread-safe; the web
server calls them from its request threads.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from .config import AppConfig
from .detection import MotionDetector
from .geometry import Calibration, MazeGeometry
from .storage import TrialRecorder, TrialStore
from .tracker import Tracker, TrackPoint, TrackState
from .video import CameraSource, FileSource

VIEWS = ("camera", "topdown", "mask")
FT_MM = 304.8

# BGR colours for the overlay
_YELLOW = (0, 215, 255)
_GREEN = (80, 220, 80)
_ORANGE = (0, 150, 255)
_CYAN = (255, 220, 0)
_MAGENTA = (255, 0, 255)
_GRID = (110, 110, 110)
_WALL = (230, 230, 230)


class Engine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.store = TrialStore(cfg.trials_dir, cfg.metrics, cfg.scoring)
        self._lock = threading.RLock()
        self._messages: deque[dict] = deque(maxlen=12)
        self._msg_seq = 0

        self._source: CameraSource | FileSource | None = None
        self._thread: threading.Thread | None = None
        self._thread_stop = threading.Event()

        self._cal_path = cfg.setup_dir / "calibration.json"
        self._bg_path = cfg.setup_dir / "background.png"
        self.calibration: Calibration | None = None
        try:
            self.calibration = Calibration.load(self._cal_path)
        except Exception as exc:  # corrupt file: start uncalibrated rather than crash
            self._msg("warn", f"Ignoring unreadable calibration file: {exc}")
        self._background: np.ndarray | None = (
            cv2.imread(str(self._bg_path)) if self._bg_path.exists() else None
        )

        self.geometry: MazeGeometry | None = None
        self.tracker: Tracker | None = None
        self._frame_size: tuple[int, int] | None = None
        self._last_frame: np.ndarray | None = None
        self._last_point: TrackPoint | None = None
        self._last_t: float | None = None
        self._fps = 0.0
        self._trail: deque[tuple[float, float]] = deque(maxlen=120)
        self._hint: tuple[float, float] | None = None
        self._bg_frames: list[np.ndarray] | None = None
        self._bg_needed = 0

        self.recorder: TrialRecorder | None = None
        self._trial_t0: float | None = None
        self._trial_t = 0.0
        self.last_trial_id: str | None = None

        self.view = "camera"
        self._version = 0
        self._status_cache: tuple[int, dict] | None = None
        self._jpeg: bytes | None = None
        self._jpeg_version = 0
        self._last_encode = 0.0
        self._viewers_until = 0.0

        for trial_id in self.store.recover():
            self._msg("warn", f"Recovered interrupted trial {trial_id}")

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect the source named in the config, if any."""
        cam = self.cfg.camera
        try:
            if cam.source == "camera":
                self.connect("camera", index=cam.index)
            elif cam.source == "file" and cam.file:
                self.connect("file", file=str(self.cfg.resolve(cam.file)))
        except (RuntimeError, ValueError) as exc:
            self._msg("warn", f"{exc} Pick a source in the operator panel.")

    def close(self) -> None:
        with self._lock:
            if self.recorder is not None:
                self.stop_trial(None, "Stopped automatically: server shut down.")
        self._stop_thread()
        with self._lock:
            if self._source is not None:
                self._source.close()
                self._source = None

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread_stop.set()
            self._thread.join(timeout=3)
            self._thread = None

    # ── source ───────────────────────────────────────────────────────────────

    def connect(self, kind: str, index: int | None = None, file: str | None = None) -> str:
        with self._lock:
            if self.recorder is not None:
                raise ValueError("Stop the running trial before changing the source.")
        # Opening a camera can take seconds; don't hold the lock meanwhile.
        if kind == "camera":
            cam = self.cfg.camera
            src = CameraSource(index if index is not None else cam.index, cam.width, cam.height, cam.fps)
        elif kind == "file":
            if not file:
                raise ValueError("Choose a video file.")
            src = FileSource(file, realtime=self.cfg.camera.realtime)
        else:
            raise ValueError(f"Unknown source type: {kind}")

        self._stop_thread()
        with self._lock:
            if self._source is not None:
                self._source.close()
            self._source = src
            self._last_frame = src.first_frame
            self._last_point = None
            self._last_t = None
            self._fps = 0.0
            self._trail.clear()
            self._set_frame_size(src.size)
            self._bump(encode=True)
            self._msg("info", f"Connected: {src.label}")
        self._thread_stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(src, self._thread_stop), daemon=True, name="engine"
        )
        self._thread.start()
        return src.label

    def disconnect(self) -> None:
        with self._lock:
            if self.recorder is not None:
                raise ValueError("Stop the running trial first.")
        self._stop_thread()
        with self._lock:
            if self._source is not None:
                self._source.close()
                self._source = None
            self._last_frame = None
            self._bump(encode=True)

    def _run(self, src, stop: threading.Event) -> None:
        end_handled = False
        while not stop.is_set():
            item = src.read(timeout=0.5)
            if item is None:
                if src.ended and not end_handled:
                    end_handled = True
                    self._on_source_ended(src)
                    if src.kind == "camera":
                        return
                elif not src.ended:
                    end_handled = False
                continue
            frame, t = item
            with self._lock:
                if stop.is_set():
                    return
                self._process(frame, t)

    def _on_source_ended(self, src) -> None:
        with self._lock:
            if src is not self._source:
                return
            if src.kind == "camera":
                self._msg("err", "Camera stopped sending frames. Check the cable, then reconnect.")
            if self.recorder is not None:
                note = "Video ended." if src.kind == "file" else "Camera disconnected."
                self.stop_trial(None, note)
            if src.kind == "file":
                src.paused = True
            self._bump()

    # ── geometry / setup ─────────────────────────────────────────────────────

    def _set_frame_size(self, size: tuple[int, int]) -> None:
        self._frame_size = size
        self.geometry = None
        self.tracker = None
        if self.calibration is None:
            return
        t = self.cfg.tracker
        self.geometry = MazeGeometry(
            self.calibration.scaled_to(size), self.cfg.maze.length_mm, self.cfg.maze.width_mm,
            t.mm_per_px,
        )
        self.tracker = Tracker(t, self.geometry, self._background, size)

    def set_calibration(self, corners_frac: list[tuple[float, float]]) -> None:
        """Corners as fractions (0-1) of the image width/height, TL TR BR BL."""
        with self._lock:
            if self.recorder is not None:
                raise ValueError("Stop the running trial before recalibrating.")
            if self._frame_size is None:
                raise ValueError("Connect a video source first.")
            w, h = self._frame_size
            cal = Calibration([(fx * w, fy * h) for fx, fy in corners_frac], (w, h))
            cal.save(self._cal_path)
            self.calibration = cal
            # The old background was warped with the old calibration.
            self._background = None
            self._bg_path.unlink(missing_ok=True)
            self._set_frame_size(self._frame_size)
            self._trail.clear()
            if self.cfg.tracker.mode == "motion":
                self._msg("info", "Calibrated. Next: clear the maze and click Capture background.")
            else:
                self._msg("info", "Calibrated.")
            self._bump(encode=True)

    def capture_background(self) -> None:
        with self._lock:
            if self.tracker is None or self.tracker.motion is None:
                raise ValueError("Calibrate first (background is only used in motion mode).")
            if self.recorder is not None:
                raise ValueError("Stop the running trial first.")
            src = self._source
            if src is None or src.paused or src.ended:
                # Paused video: a single still is all there is. The opening frame is
                # the best bet for an empty maze (the current one may show the rover).
                still = src.first_frame if src is not None else self._last_frame
                self._finish_background([self.geometry.warp(still)])
                return
            self._bg_frames = []
            self._bg_needed = max(15, int(round((self._fps or src.nominal_fps) * 2)))
            self._msg("info", "Capturing background (2 s): keep the maze empty…")
            self._bump()

    def _finish_background(self, frames: list[np.ndarray]) -> None:
        bg = MotionDetector.build_background(frames)
        self._bg_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(self._bg_path), bg)
        self._background = bg
        self._bg_frames = None
        if self.tracker is not None and self.tracker.motion is not None:
            self.tracker.motion.set_background(bg)
            self.tracker.reset()
        self._msg("info", "Background captured. Place the rover in the maze.")
        self._bump(encode=True)

    def _to_mm(self, fx: float, fy: float, view: str) -> tuple[float, float]:
        g = self.geometry
        if view in ("topdown", "mask"):
            return fx * g.length_mm, fy * g.width_mm
        w, h = self._frame_size
        x, y = g.px_to_mm((fx * w, fy * h))[0]
        return float(x), float(y)

    def reacquire(self, fx: float, fy: float, view: str | None = None) -> None:
        """Operator clicked on the rover in the feed (coordinates as image fractions)."""
        with self._lock:
            if self.tracker is None:
                raise ValueError("Calibrate first.")
            x, y = self._to_mm(fx, fy, view or self.view)
            if not self.geometry.contains(x, y, margin_mm=100):
                raise ValueError("That point is outside the maze.")
            self.tracker.set_hint(x, y)
            self._hint = (x, y)
            self._trail.clear()
            self._bump(encode=True)

    def set_view(self, view: str) -> None:
        if view not in VIEWS:
            raise ValueError(f"Unknown view: {view}")
        with self._lock:
            self.view = view
            self._bump(encode=True)

    # ── trials ───────────────────────────────────────────────────────────────

    def start_trial(self, team: str) -> dict:
        with self._lock:
            team = (team or "").strip()
            if not team:
                raise ValueError("Enter a team name.")
            if self.recorder is not None:
                raise ValueError("A trial is already running.")
            if self._source is None:
                raise ValueError("Connect a video source first.")
            if self.tracker is None:
                raise ValueError("Calibrate the maze first.")
            src = self._source
            self.recorder = TrialRecorder(
                self.cfg.trials_dir, team, self._maze_dict(), self.cfg.metrics,
                self.cfg.paths.record_video, src.nominal_fps,
            )
            self._trial_t0 = None
            self._trial_t = 0.0
            self._trail.clear()
            if src.kind == "file":
                src.rewind()
                self.tracker.reset()
                src.play()
            self._msg("info", f"Trial started: {team}")
            self._bump()
            return dict(self.recorder.info)

    def stop_trial(self, outcome: str | None, notes: str = "") -> dict:
        with self._lock:
            rec = self.recorder
            if rec is None:
                raise ValueError("No trial is running.")
            self.recorder = None
            info = rec.finish(self._trial_t, outcome, notes)
            self.last_trial_id = info["id"]
            src = self._source
            if src is not None and src.kind == "file":
                src.paused = True
            label = {"finished": "finished", "dnf": "DNF"}.get(info["outcome"], "stopped")
            self._msg("info", f"Trial {label}: {info['team']} ({self._trial_t:.1f} s)")
            self._bump(encode=True)
            return info

    def log_event(self, kind: str, severity: int | None = None) -> dict:
        if kind not in ("collision", "intervention"):
            raise ValueError(f"Unknown event kind: {kind}")
        if kind == "collision" and severity not in (1, 2, 3):
            raise ValueError("Collision class must be 1, 2 or 3.")
        with self._lock:
            if self.recorder is None:
                raise ValueError("Start a trial first.")
            ev = self.recorder.add_event(kind, self._trial_t, severity if kind == "collision" else None)
            self._bump()
            return ev

    def undo_event(self, event_id: int | None = None) -> bool:
        with self._lock:
            if self.recorder is None:
                raise ValueError("No trial is running.")
            events = self.recorder.info["events"]
            if not events:
                return False
            ok = self.recorder.remove_event(event_id if event_id is not None else events[-1]["id"])
            self._bump()
            return ok

    def _maze_dict(self) -> dict:
        m = self.cfg.maze
        return {"length_mm": m.length_mm, "width_mm": m.width_mm, "walls": [list(w) for w in m.walls]}

    # ── per-frame ────────────────────────────────────────────────────────────

    def _process(self, frame: np.ndarray, t: float) -> None:
        size = (frame.shape[1], frame.shape[0])
        if size != self._frame_size:
            self._set_frame_size(size)
        if self._last_t is not None and t > self._last_t:
            inst = 1.0 / (t - self._last_t)
            self._fps = inst if self._fps == 0 else 0.9 * self._fps + 0.1 * inst
        self._last_t = t
        self._last_frame = frame

        point = None
        if self.tracker is not None:
            point = self.tracker.process(frame, t)
            if point is not None:
                self._hint = None
                self._trail.append((point.x_mm, point.y_mm))
        self._last_point = point

        if self._bg_frames is not None and self.geometry is not None:
            self._bg_frames.append(self.geometry.warp(frame))
            if len(self._bg_frames) >= self._bg_needed:
                self._finish_background(self._bg_frames)

        if self.recorder is not None:
            if self._trial_t0 is None:
                self._trial_t0 = t
            self._trial_t = t - self._trial_t0
            self.recorder.add_frame(frame)
            self.recorder.add_point(self._trial_t, point)

        self._bump()

    def _bump(self, encode: bool = False) -> None:
        self._version += 1
        now = time.monotonic()
        due = now - self._last_encode >= 1.0 / max(1, self.cfg.server.stream_fps)
        if encode or (due and now < self._viewers_until):
            self._encode()

    # ── status / stream ──────────────────────────────────────────────────────

    def _msg(self, level: str, text: str) -> None:
        self._msg_seq += 1
        self._messages.append({"id": self._msg_seq, "level": level, "text": text, "at": time.time()})
        self._version += 1

    def status(self) -> tuple[int, dict]:
        with self._lock:
            if self._status_cache and self._status_cache[0] == self._version:
                return self._status_cache
            src = self._source
            p = self._last_point
            tr = self.tracker
            trial = None
            if self.recorder is not None:
                info = self.recorder.info
                trial = {
                    "id": info["id"], "team": info["team"], "started_at": info["started_at"],
                    "t": self._trial_t, "events": list(info["events"]), "live": self.recorder.live(),
                }
            st = {
                "version": self._version,
                "source": None if src is None else {
                    "kind": src.kind, "label": src.label, "paused": src.paused,
                    "ended": src.ended, "fps": round(self._fps, 1),
                },
                "mode": self.cfg.tracker.mode,
                "calibrated": self.calibration is not None,
                "has_background": self._background is not None,
                "capturing_background": self._bg_frames is not None,
                "view": self.view,
                "tracker": {
                    "state": tr.state.value if tr else "off",
                    "detections": len(tr.last_detections) if tr else 0,
                    "point": None if p is None else {
                        "x_mm": round(p.x_mm, 1), "y_mm": round(p.y_mm, 1),
                        "speed_mms": round(p.speed_mms, 1), "heading_deg": p.heading_deg,
                        "track_id": p.track_id,
                    },
                },
                "trial": trial,
                "last_trial_id": self.last_trial_id,
                "messages": list(self._messages),
                "maze": self._maze_dict(),
            }
            self._status_cache = (self._version, st)
            return self._status_cache

    def jpeg(self) -> tuple[int, bytes | None]:
        """Latest rendered operator view. Calling this keeps rendering switched on."""
        with self._lock:
            now = time.monotonic()
            if now >= self._viewers_until:
                self._viewers_until = now + 3.0
                self._encode()  # first viewer after idle: render immediately
            self._viewers_until = now + 3.0
            return self._jpeg_version, self._jpeg

    def _encode(self) -> None:
        img = self._render()
        q = [cv2.IMWRITE_JPEG_QUALITY, self.cfg.server.jpeg_quality]
        ok, buf = cv2.imencode(".jpg", img, q)
        if ok:
            self._jpeg = buf.tobytes()
            self._jpeg_version += 1
        self._last_encode = time.monotonic()

    # ── overlay rendering ────────────────────────────────────────────────────

    def _render(self) -> np.ndarray:
        frame = self._last_frame
        if frame is None:
            img = np.zeros((360, 640, 3), np.uint8)
            _text(img, "No video source", (20, 190), 1.0, (200, 200, 200), 2)
            return img
        g = self.geometry
        view = self.view if g is not None else "camera"
        if view == "camera":
            img = frame.copy()
            to_px = g.mm_to_px if g is not None else None
        else:
            top = g.warp(frame)
            if view == "mask":
                m = self.tracker.motion.last_mask if self.tracker and self.tracker.motion else None
                m = np.zeros(top.shape[:2], np.uint8) if m is None else m
                img = cv2.addWeighted(top, 0.35, cv2.cvtColor(m, cv2.COLOR_GRAY2BGR), 0.65, 0)
            else:
                img = top
            mpp = g.mm_per_px
            to_px = lambda pts: np.asarray(pts, dtype=np.float64).reshape(-1, 2) / mpp  # noqa: E731

        # Scale the canvas for display before drawing so line widths stay crisp.
        max_w = self.cfg.server.stream_max_width
        scale = 1.0
        if view == "camera" and img.shape[1] > max_w:
            scale = max_w / img.shape[1]
        elif view != "camera":
            scale = min(3.0, max_w / img.shape[1])
        if scale != 1.0:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        th = max(1, round(img.shape[1] / 700))

        def P(pts) -> np.ndarray:
            return (to_px(pts) * scale).round().astype(np.int32)

        if g is not None:
            self._draw_maze(img, P, th)
            self._draw_tracking(img, P, th)
        self._draw_hud(img, th)
        return img

    def _draw_maze(self, img, P, th) -> None:
        g = self.geometry
        L, W = g.length_mm, g.width_mm
        x = FT_MM
        while x < L - 1:
            cv2.polylines(img, [P([(x, 0), (x, W)])], False, _GRID, 1, cv2.LINE_AA)
            x += FT_MM
        y = FT_MM
        while y < W - 1:
            cv2.polylines(img, [P([(0, y), (L, y)])], False, _GRID, 1, cv2.LINE_AA)
            y += FT_MM
        for x0, y0, x1, y1 in self.cfg.maze.walls:
            cv2.polylines(img, [P([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])], True, _WALL, th, cv2.LINE_AA)
        cv2.polylines(img, [P([(0, 0), (L, 0), (L, W), (0, W)])], True, _YELLOW, th + 1, cv2.LINE_AA)
        corner = P([(0, 0)])[0]
        _text(img, "origin", (int(corner[0]) + 6, int(corner[1]) + 18), 0.5, _YELLOW, 1)

    def _draw_tracking(self, img, P, th) -> None:
        tr = self.tracker
        for d in tr.last_detections:
            c = P([(d.x_mm, d.y_mm)])[0]
            cv2.drawMarker(img, tuple(int(v) for v in c), _CYAN, cv2.MARKER_CROSS, 14, th, cv2.LINE_AA)
        if len(self._trail) >= 2:
            cv2.polylines(img, [P(list(self._trail))], False, _GREEN, th, cv2.LINE_AA)
        p = self._last_point
        if p is not None:
            r = self.cfg.tracker.rover_size_mm / 2
            c, e = P([(p.x_mm, p.y_mm), (p.x_mm + r, p.y_mm)])
            radius = max(6, int(math.hypot(*(e - c))))
            col = _GREEN if p.state == TrackState.TRACKING else _ORANGE
            center = (int(c[0]), int(c[1]))
            cv2.circle(img, center, radius, col, th + 1, cv2.LINE_AA)
            if p.heading_deg is not None:
                a = math.radians(p.heading_deg)
                tip = P([(p.x_mm + 1.3 * r * math.cos(a), p.y_mm - 1.3 * r * math.sin(a))])[0]
                tip = (int(tip[0]), int(tip[1]))
                cv2.arrowedLine(img, center, tip, col, th + 1, cv2.LINE_AA, tipLength=0.3)
        if self._hint is not None:
            c = P([self._hint])[0]
            cv2.drawMarker(img, (int(c[0]), int(c[1])), _MAGENTA, cv2.MARKER_TILTED_CROSS, 24, th + 1)

    def _draw_hud(self, img, th) -> None:
        lines = []
        if self.calibration is None:
            lines.append(("NOT CALIBRATED - click Calibrate", _ORANGE))
        elif self.tracker is not None:
            state = self.tracker.state
            col = {TrackState.TRACKING: _GREEN, TrackState.COASTING: _ORANGE}.get(state, (80, 80, 255))
            lines.append((f"{state.value.upper()}  {self._fps:4.1f} fps", col))
            if self.tracker.motion is not None and not self.tracker.motion.has_background:
                lines.append(("No background - clear maze, click Capture background", _ORANGE))
        if self._bg_frames is not None:
            lines.append((f"Capturing background {len(self._bg_frames)}/{self._bg_needed}", _CYAN))
        if self.recorder is not None:
            m, s = divmod(self._trial_t, 60)
            lines.append((f"REC {int(m):02d}:{s:04.1f}  {self.recorder.info['team']}", (60, 60, 255)))
        y = 26
        for text, col in lines:
            _text(img, text, (10, y), 0.6, col, max(1, th))
            y += 24


def _text(img, text, org, scale, color, thickness) -> None:
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def ensure_dirs(cfg: AppConfig) -> None:
    for p in (cfg.trials_dir, cfg.setup_dir):
        Path(p).mkdir(parents=True, exist_ok=True)
