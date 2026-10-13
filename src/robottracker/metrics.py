"""Trial statistics and scoring. The only place these numbers are computed."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field

from .config import MetricsConfig, ScoringConfig


@dataclass
class Summary:
    duration_s: float = 0.0
    distance_mm: float = 0.0
    avg_speed_mms: float = 0.0  # distance / duration
    top_speed_mms: float = 0.0  # fastest sustained speed over the speed window
    moving_time_s: float = 0.0
    tracked_fraction: float = 0.0  # share of frames with a confirmed detection
    collisions: dict[str, int] = field(default_factory=lambda: {"1": 0, "2": 0, "3": 0})
    interventions: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class StatsAccumulator:
    """Incremental stats, fed one tracked point at a time.

    Distance uses an anchor: it only grows once the rover is min_step_mm away
    from where distance was last counted. Jitter and spinning in place never
    reach the threshold; real driving always does. Points from different
    track_ids (lost, then re-acquired) are never joined, so a re-lock can't
    add a phantom jump.
    """

    def __init__(self, cfg: MetricsConfig):
        self.cfg = cfg
        self.distance_mm = 0.0
        self.top_speed_mms = 0.0
        self.current_speed_mms = 0.0
        self.moving_time_s = 0.0
        self.frames = 0
        self.measured_frames = 0
        self._track_id: int | None = None
        self._anchor: tuple[float, float] | None = None
        self._window: deque[tuple[float, float, float]] = deque()
        self._last_t: float | None = None

    def tick(self, measured: bool) -> None:
        """Count a processed frame (whether or not the rover was found)."""
        self.frames += 1
        if measured:
            self.measured_frames += 1

    def add(self, t: float, x: float, y: float, track_id: int) -> None:
        if track_id != self._track_id:
            self._track_id = track_id
            self._anchor = (x, y)
            self._window.clear()
            self._last_t = None
        ax, ay = self._anchor
        step = math.hypot(x - ax, y - ay)
        if step >= self.cfg.min_step_mm:
            self.distance_mm += step
            self._anchor = (x, y)

        win = self._window
        win.append((t, x, y))
        while len(win) > 2 and t - win[1][0] >= self.cfg.speed_window_s:
            win.popleft()
        t0, x0, y0 = win[0]
        span = t - t0
        if span >= 0.8 * self.cfg.speed_window_s:
            self.current_speed_mms = math.hypot(x - x0, y - y0) / span
            self.top_speed_mms = max(self.top_speed_mms, self.current_speed_mms)
        if self._last_t is not None and self.current_speed_mms >= self.cfg.moving_speed_mms:
            self.moving_time_s += t - self._last_t
        self._last_t = t

    def summary(self, duration_s: float, events: list[dict]) -> Summary:
        s = Summary(
            duration_s=duration_s,
            distance_mm=self.distance_mm,
            avg_speed_mms=self.distance_mm / duration_s if duration_s > 0 else 0.0,
            top_speed_mms=self.top_speed_mms,
            moving_time_s=self.moving_time_s,
            tracked_fraction=self.measured_frames / self.frames if self.frames else 0.0,
        )
        for e in events:
            if e["kind"] == "collision":
                key = str(e.get("severity") or 1)
                s.collisions[key] = s.collisions.get(key, 0) + 1
            elif e["kind"] == "intervention":
                s.interventions += 1
        return s


def penalty_s(summary: dict, scoring: ScoringConfig) -> float:
    p = summary.get("interventions", 0) * scoring.intervention_penalty_s
    for sev, n in summary.get("collisions", {}).items():
        p += n * scoring.collision_penalty_s.get(int(sev), 0.0)
    return p


def rank_key(trial: dict, scoring: ScoringConfig) -> tuple:
    """Finished runs first by adjusted time, then DNFs by distance, then unscored."""
    s = trial.get("summary") or {}
    outcome = trial.get("outcome")
    if outcome == "finished":
        return (0, s.get("duration_s", 0) + penalty_s(s, scoring))
    if outcome == "dnf":
        return (1, -s.get("distance_mm", 0))
    return (2, -s.get("distance_mm", 0))
