from __future__ import annotations

import cv2
import pytest

from robottracker.config import AppConfig
from robottracker.detection import MotionDetector
from robottracker.geometry import MazeGeometry
from robottracker.synthetic import (
    DEFAULT_WALLS,
    DEMO_CORNERS,
    FRAME_SIZE,
    MAZE_LENGTH_MM,
    MAZE_WIDTH_MM,
    SceneRenderer,
    simulate_poses,
)


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    c = AppConfig()
    c.tracker.rover_size_mm = 240
    c.maze.walls = list(DEFAULT_WALLS)
    c._base_dir = tmp_path
    return c


@pytest.fixture(scope="session")
def renderer() -> SceneRenderer:
    return SceneRenderer()


@pytest.fixture(scope="session")
def poses():
    return simulate_poses()


@pytest.fixture
def geometry(cfg) -> MazeGeometry:
    return MazeGeometry(DEMO_CORNERS, MAZE_LENGTH_MM, MAZE_WIDTH_MM, cfg.tracker.mm_per_px)


@pytest.fixture
def background(renderer, geometry):
    return MotionDetector.build_background([geometry.warp(renderer.render(None, i / 30)) for i in range(10)])


@pytest.fixture(scope="session")
def demo_video(tmp_path_factory, renderer):
    """A short clip: 1 s empty maze, then ~3 s of driving."""
    path = tmp_path_factory.mktemp("video") / "clip.mp4"
    poses = simulate_poses()[:90]
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, FRAME_SIZE)
    for _ in range(30):
        w.write(renderer.render(None, 0))
    for p in poses:
        w.write(renderer.render(p, p.t))
    w.release()
    return path
