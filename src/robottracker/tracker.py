"""Single-rover tracker: detector + Kalman filter + lock/lose/re-acquire logic."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np

from .config import TrackerConfig
from .detection import Detection, MarkerDetector, MotionDetector
from .geometry import MazeGeometry
from .kalman import ConstantVelocityKF


class TrackState(str, Enum):
    SEARCHING = "searching"  # no lock; looking for the rover
    TRACKING = "tracking"  # matched a detection this frame
    COASTING = "coasting"  # briefly lost; position is a prediction


@dataclass
class TrackPoint:
    t: float
    x_mm: float
    y_mm: float
    vx_mms: float
    vy_mms: float
    heading_deg: float | None
    state: TrackState
    track_id: int  # increments every time the tracker (re)locks on
    measured: bool  # True if backed by a detection this frame

    @property
    def speed_mms(self) -> float:
        return math.hypot(self.vx_mms, self.vy_mms)


class Tracker:
    def __init__(
        self,
        cfg: TrackerConfig,
        geometry: MazeGeometry,
        background: np.ndarray | None = None,
        frame_size: tuple[int, int] | None = None,
    ):
        self.cfg = cfg
        self.geometry = geometry
        self.motion: MotionDetector | None = None
        self.markers: MarkerDetector | None = None
        if cfg.mode == "aruco":
            self.markers = MarkerDetector(cfg, geometry)
        else:
            self.motion = MotionDetector(cfg, geometry, background)
        self._nadir = geometry.nadir_mm(frame_size)
        self.track_id = 0
        self.last_detections: list[Detection] = []
        self.last_top: np.ndarray | None = None
        self.reset()

    # ── control ──────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._kf: ConstantVelocityKF | None = None
        self.state = TrackState.SEARCHING
        self._last_t: float | None = None
        self._coast_s = 0.0
        self._candidate: Detection | None = None
        self._candidate_n = 0
        self._hint: tuple[float, float] | None = None
        self._area_ref: float | None = None
        self._heading: float | None = None

    def set_hint(self, x_mm: float, y_mm: float) -> None:
        """Operator says 'the rover is here': drop the current lock and re-acquire near it."""
        self._kf = None
        self.state = TrackState.SEARCHING
        self._hint = (x_mm, y_mm)
        self._candidate = None
        self._candidate_n = 0

    @property
    def position(self) -> tuple[float, float] | None:
        return None if self._kf is None else self._kf.position

    # ── per frame ────────────────────────────────────────────────────────────

    def process(self, frame: np.ndarray, t: float) -> TrackPoint | None:
        top = None
        if self.motion is not None:
            top = self.geometry.warp(frame)
            dets = self.motion.detect(top)
        else:
            dets = self.markers.detect(frame)
        dets = [self._correct_parallax(d) for d in dets]
        dets = [d for d in dets if self.geometry.contains(d.x_mm, d.y_mm, margin_mm=50)]
        self.last_detections = dets
        self.last_top = top

        dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t

        point = self._track(dets, dt, t) if self._kf is not None else self._acquire(dets, t)

        if self.motion is not None and top is not None:
            exclude = [(d.x_mm, d.y_mm) for d in dets]
            if self._kf is not None:
                exclude.append(self._kf.position)
            self.motion.adapt(top, exclude)
        return point

    def _parallax_k(self) -> float:
        H = self.cfg.camera_height_mm
        h = self.cfg.target_height_mm
        if not H or h <= 0 or h >= H:
            return 1.0
        # A point at height h appears pushed away from the nadir by H / (H - h).
        return (H - h) / H

    def _correct_parallax(self, d: Detection) -> Detection:
        k = self._parallax_k()
        if k != 1.0:
            nx, ny = self._nadir
            d.x_mm = nx + (d.x_mm - nx) * k
            d.y_mm = ny + (d.y_mm - ny) * k
        return d

    def _local_measurement(self, px: float, py: float) -> Detection | None:
        """Rover-sized window around the prediction (see MotionDetector.local_measurement)."""
        k = self._parallax_k()
        nx, ny = self._nadir
        rx, ry = nx + (px - nx) / k, ny + (py - ny) / k  # back to uncorrected coordinates
        d = self.motion.local_measurement(rx, ry, 0.75 * self.cfg.rover_size_mm)
        return None if d is None else self._correct_parallax(d)

    def _track(self, dets: list[Detection], dt: float, t: float) -> TrackPoint | None:
        kf = self._kf
        kf.predict(dt)
        px, py = kf.position
        best, best_d = None, self.cfg.gate_mm
        for d in dets:
            dist = math.hypot(d.x_mm - px, d.y_mm - py)
            if dist <= best_d:
                best, best_d = d, dist

        ref = self._area_ref
        if best is not None and ref is not None and self.motion is not None:
            if best.area_mm2 > 1.3 * ref:
                # Something merged with the rover (a hand, an arm). Measure only
                # the rover-sized area around the prediction, or coast.
                local = self._local_measurement(px, py)
                ok = local is not None and 0.5 * ref <= local.area_mm2 <= 1.4 * ref
                best = local if ok else None
            elif best.area_mm2 < 0.3 * ref:
                best = None

        if best is not None:
            kf.update(best.x_mm, best.y_mm)
            self._coast_s = 0.0
            self.state = TrackState.TRACKING
            if ref is not None and 0.7 * ref <= best.area_mm2 <= 1.3 * ref:
                self._area_ref = 0.95 * ref + 0.05 * best.area_mm2
            if best.heading_deg is not None:
                self._heading = best.heading_deg
        else:
            self._coast_s += dt
            if self._coast_s > self.cfg.max_coast_s:
                self._kf = None
                self.state = TrackState.SEARCHING
                return None
            self.state = TrackState.COASTING
            kf.damp_velocity(math.exp(-dt / 0.3))

        vx, vy = kf.velocity
        if self.markers is None and math.hypot(vx, vy) > 60:
            self._heading = math.degrees(math.atan2(-vy, vx))
        x, y = kf.position
        return TrackPoint(t, x, y, vx, vy, self._heading, self.state, self.track_id, best is not None)

    def _acquire(self, dets: list[Detection], t: float) -> TrackPoint | None:
        chosen = None
        if self._hint is not None:
            hx, hy = self._hint
            near = [d for d in dets if math.hypot(d.x_mm - hx, d.y_mm - hy) <= 2 * self.cfg.gate_mm]
            if near:
                chosen = min(near, key=lambda d: math.hypot(d.x_mm - hx, d.y_mm - hy))
        elif dets:
            best = max(dets, key=lambda d: d.area_mm2)
            c = self._candidate
            if c is not None and math.hypot(best.x_mm - c.x_mm, best.y_mm - c.y_mm) <= self.cfg.gate_mm:
                self._candidate_n += 1
            else:
                self._candidate_n = 1
            self._candidate = best
            if self._candidate_n >= self.cfg.confirm_frames:
                chosen = best
        else:
            self._candidate, self._candidate_n = None, 0

        if chosen is None:
            return None
        self._hint = None
        self._candidate, self._candidate_n = None, 0
        self._kf = ConstantVelocityKF(
            chosen.x_mm, chosen.y_mm, self.cfg.accel_noise_mms2, self.cfg.measurement_noise_mm
        )
        self._area_ref = None if self.markers is not None else chosen.area_mm2
        self._heading = chosen.heading_deg
        self._coast_s = 0.0
        self.track_id += 1
        self.state = TrackState.TRACKING
        return TrackPoint(
            t, chosen.x_mm, chosen.y_mm, 0.0, 0.0, self._heading, self.state, self.track_id, True
        )
