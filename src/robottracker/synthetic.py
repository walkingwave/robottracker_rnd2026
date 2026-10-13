"""Synthetic overhead-camera footage with known ground truth.

Used by `robottracker demo` (try everything without hardware) and by the
tests to measure tracking accuracy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

CELL_MM = 304.8
MAZE_LENGTH_MM = 8 * CELL_MM
MAZE_WIDTH_MM = 4 * CELL_MM
# The MIE444 practice maze: blocked 1 ft cells as [x0, y0, x1, y1] mm.
DEFAULT_WALLS = [
    (1219.2, 0.0, 1524.0, 304.8),
    (1828.8, 0.0, 2133.6, 304.8),
    (609.6, 304.8, 914.4, 609.6),
    (304.8, 609.6, 609.6, 914.4),
    (914.4, 609.6, 1524.0, 914.4),
    (1828.8, 609.6, 2133.6, 1219.2),
]
FRAME_SIZE = (1280, 720)
# Where the maze corners land in the synthetic camera image (TL, TR, BR, BL).
DEMO_CORNERS = [(180.0, 110.0), (1110.0, 95.0), (1150.0, 630.0), (140.0, 610.0)]
# Route through open cells (column, row), cell centres.
DEMO_ROUTE = [(0, 3), (0, 0), (3, 0), (3, 1), (5, 1), (5, 3), (2, 3), (2, 2)]


@dataclass
class Pose:
    t: float
    x: float
    y: float
    heading_deg: float  # CCW from +x, y-up convention


def _cell(c: int, r: int) -> tuple[float, float]:
    return ((c + 0.5) * CELL_MM, (r + 0.5) * CELL_MM)


def route_length_mm(route=DEMO_ROUTE) -> float:
    pts = [_cell(*c) for c in route]
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def simulate_poses(fps: float = 30.0, speed_mms: float = 300.0, route=DEMO_ROUTE,
                   spin_at: int | None = 3, stop_at: int | None = 5) -> list[Pose]:
    """Drive the route: turn in place at corners, one 360° spin, one pause."""
    dt = 1.0 / fps
    pts = [_cell(*c) for c in route]
    poses: list[Pose] = []
    t = 0.0
    x, y = pts[0]
    first = pts[1]
    heading = math.degrees(math.atan2(-(first[1] - y), first[0] - x))

    def hold(seconds: float, spin_deg: float = 0.0):
        nonlocal t, heading
        n = max(1, int(round(seconds * fps)))
        h0 = heading
        for i in range(n):
            heading = h0 + spin_deg * (i + 1) / n
            poses.append(Pose(t, x, y, heading))
            t += dt

    hold(1.0)
    for i, (tx, ty) in enumerate(pts[1:], start=1):
        target = math.degrees(math.atan2(-(ty - y), tx - x))
        turn = (target - heading + 180) % 360 - 180
        if abs(turn) > 1:
            hold(abs(turn) / 180.0, turn)
        heading = target
        seg = math.hypot(tx - x, ty - y)
        n = max(1, int(round(seg / speed_mms * fps)))
        x0, y0 = x, y
        for k in range(1, n + 1):
            x = x0 + (tx - x0) * k / n
            y = y0 + (ty - y0) * k / n
            poses.append(Pose(t, x, y, heading))
            t += dt
        if i == spin_at:
            hold(2.0, 360.0)
        if i == stop_at:
            hold(1.5)
    hold(1.0)
    return poses


class SceneRenderer:
    """Renders the maze and rover from above, then projects it into a tilted camera."""

    def __init__(self, walls=DEFAULT_WALLS, corners=DEMO_CORNERS, frame_size=FRAME_SIZE,
                 mm_per_px: float = 2.0, seed: int = 0):
        self.mpp = mm_per_px
        self.frame_size = frame_size
        w = int(MAZE_LENGTH_MM / mm_per_px)
        h = int(MAZE_WIDTH_MM / mm_per_px)
        rng = np.random.default_rng(seed)
        floor = np.full((h, w, 3), (178, 186, 190), np.float32)
        floor += rng.normal(0, 6, (h, w, 1)).astype(np.float32)
        floor = cv2.GaussianBlur(floor, (0, 0), 1.5)
        for _ in range(12):  # scuffs and tape marks
            cx, cy = int(rng.integers(0, w)), int(rng.integers(0, h))
            cv2.ellipse(floor, (cx, cy), (int(rng.integers(5, 30)), int(rng.integers(2, 8))),
                        float(rng.integers(0, 180)), 0, 360, (150, 160, 165), -1)
        for x0, y0, x1, y1 in walls:
            p0 = (int(x0 / mm_per_px), int(y0 / mm_per_px))
            p1 = (int(x1 / mm_per_px), int(y1 / mm_per_px))
            cv2.rectangle(floor, p0, p1, (70, 95, 125), -1)
            cv2.rectangle(floor, p0, p1, (40, 60, 85), 3)
        cv2.rectangle(floor, (0, 0), (w - 1, h - 1), (40, 60, 85), 6)
        self.floor = floor

        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        self.H = cv2.getPerspectiveTransform(src, np.float32(corners))
        self._noise_rng = np.random.default_rng(seed + 1)

    def _draw_rover(self, img: np.ndarray, pose: Pose) -> None:
        m = self.mpp
        a = math.radians(pose.heading_deg)
        center = (pose.x / m, pose.y / m)
        angle_img = -pose.heading_deg  # image y points down
        body = cv2.boxPoints((center, (240 / m, 200 / m), angle_img))
        cv2.fillPoly(img, [np.int32(np.round(body))], (40, 40, 165))
        fx, fy = math.cos(a), -math.sin(a)
        nose = (int(center[0] + fx * 85 / m), int(center[1] + fy * 85 / m))
        cv2.circle(img, nose, int(30 / m), (235, 235, 235), -1)
        for side in (-1, 1):  # wheels
            wx = center[0] - fy * side * 105 / m
            wy = center[1] + fx * side * 105 / m
            wheel = cv2.boxPoints(((wx, wy), (90 / m, 25 / m), angle_img))
            cv2.fillPoly(img, [np.int32(np.round(wheel))], (25, 25, 25))

    def render(self, pose: Pose | None, t: float = 0.0, lighting: bool = True,
               hand: tuple[float, float] | None = None) -> np.ndarray:
        top = self.floor.copy()
        if pose is not None:
            self._draw_rover(top, pose)
        if hand is not None:  # a skin-coloured arm reaching in from the bottom wall
            hx, hy = hand[0] / self.mpp, hand[1] / self.mpp
            cv2.line(top, (int(hx), int(hy)), (int(hx + 40), top.shape[0] + 50), (120, 160, 210),
                     int(70 / self.mpp))
            cv2.circle(top, (int(hx), int(hy)), int(55 / self.mpp), (120, 160, 210), -1)
        frame = cv2.warpPerspective(top, self.H, self.frame_size, borderValue=(55, 58, 60))
        if lighting:
            frame *= 1.0 + 0.05 * math.sin(t * 0.7)
            frame += self._noise_rng.normal(0, 3, frame.shape).astype(np.float32)
        return np.clip(frame, 0, 255).astype(np.uint8)


def write_demo(out_dir: Path, fps: float = 30.0) -> dict:
    """Create a self-contained demo workspace: video, config and calibration."""
    from .geometry import Calibration

    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = SceneRenderer()
    video = out_dir / "demo_run.mp4"
    if not video.exists():
        poses = simulate_poses(fps)
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), fps, FRAME_SIZE)
        for _ in range(int(fps)):  # 1 s of empty maze first
            writer.write(renderer.render(None, 0.0))
        for p in poses:
            writer.write(renderer.render(p, p.t))
        writer.release()

    setup = out_dir / "setup"
    Calibration(DEMO_CORNERS, FRAME_SIZE).save(setup / "calibration.json")
    walls = "\n".join(f"    - [{x0}, {y0}, {x1}, {y1}]" for x0, y0, x1, y1 in DEFAULT_WALLS)
    (out_dir / "config.yaml").write_text(
        "# Demo workspace generated by `robottracker demo`.\n"
        "maze:\n"
        f"  length_mm: {MAZE_LENGTH_MM}\n"
        f"  width_mm: {MAZE_WIDTH_MM}\n"
        f"  walls:\n{walls}\n"
        "camera:\n"
        "  source: file\n"
        f"  file: {video.name}\n"
        "tracker:\n"
        "  rover_size_mm: 240\n"
        "server:\n"
        "  public_video: true\n",
        encoding="utf-8",
    )
    return {"dir": out_dir, "video": video, "config": out_dir / "config.yaml"}
