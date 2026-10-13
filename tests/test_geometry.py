import numpy as np
import pytest

from robottracker.geometry import Calibration, MazeGeometry, validate_corners
from robottracker.synthetic import DEMO_CORNERS, MAZE_LENGTH_MM, MAZE_WIDTH_MM


def test_corners_map_to_maze_corners(geometry):
    mm = geometry.px_to_mm(DEMO_CORNERS)
    expected = [[0, 0], [MAZE_LENGTH_MM, 0], [MAZE_LENGTH_MM, MAZE_WIDTH_MM], [0, MAZE_WIDTH_MM]]
    np.testing.assert_allclose(mm, expected, atol=1e-6)


def test_roundtrip(geometry):
    pts = np.array([[100.0, 200.0], [1500.0, 900.0], [2400.0, 10.0]])
    np.testing.assert_allclose(geometry.px_to_mm(geometry.mm_to_px(pts)), pts, atol=1e-6)


def test_top_down_size(geometry):
    w, h = geometry.top_size
    assert w == int(np.ceil(MAZE_LENGTH_MM / 3.0))
    assert h == int(np.ceil(MAZE_WIDTH_MM / 3.0))
    frame = np.zeros((720, 1280, 3), np.uint8)
    assert geometry.warp(frame).shape == (h, w, 3)


def test_rejects_counter_clockwise_order():
    ccw = [DEMO_CORNERS[0], DEMO_CORNERS[3], DEMO_CORNERS[2], DEMO_CORNERS[1]]
    with pytest.raises(ValueError, match="wrong order"):
        validate_corners(ccw)


def test_rejects_crossed_corners():
    crossed = [DEMO_CORNERS[0], DEMO_CORNERS[2], DEMO_CORNERS[1], DEMO_CORNERS[3]]
    with pytest.raises(ValueError):
        validate_corners(crossed)


def test_calibration_scales_with_resolution(tmp_path):
    cal = Calibration(DEMO_CORNERS, (1280, 720))
    cal.save(tmp_path / "cal.json")
    loaded = Calibration.load(tmp_path / "cal.json")
    scaled = loaded.scaled_to((1920, 1080))
    np.testing.assert_allclose(scaled, np.array(DEMO_CORNERS) * 1.5)
    g = MazeGeometry(scaled, MAZE_LENGTH_MM, MAZE_WIDTH_MM, 3.0)
    np.testing.assert_allclose(g.px_to_mm(scaled[2]), [[MAZE_LENGTH_MM, MAZE_WIDTH_MM]], atol=1e-6)
