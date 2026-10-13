"""Tracking accuracy on synthetic footage with known ground truth."""

import math

import cv2
import numpy as np
import pytest

from robottracker.metrics import StatsAccumulator
from robottracker.synthetic import FRAME_SIZE, Pose, SceneRenderer, route_length_mm
from robottracker.tracker import Tracker, TrackState


def run(tracker, renderer, poses, **render_kw):
    errs, points = [], []
    for p in poses:
        pt = tracker.process(renderer.render(p, p.t, **render_kw), p.t)
        points.append(pt)
        if pt is not None:
            errs.append(math.hypot(pt.x_mm - p.x, pt.y_mm - p.y))
    return np.array(errs), points


def test_full_run_accuracy(cfg, geometry, background, renderer, poses):
    tracker = Tracker(cfg.tracker, geometry, background, FRAME_SIZE)
    errs, points = run(tracker, renderer, poses)
    assert len(errs) >= len(poses) - cfg.tracker.confirm_frames
    assert np.sqrt(np.mean(errs**2)) < 5
    assert errs.max() < 20
    assert tracker.track_id == 1  # never lost the rover

    stats = StatsAccumulator(cfg.metrics)
    for p in points:
        if p is not None:
            stats.add(p.t, p.x_mm, p.y_mm, p.track_id)
    truth = route_length_mm()
    assert abs(stats.distance_mm - truth) / truth < 0.03
    assert 250 < stats.top_speed_mms < 360  # true speed 300 mm/s


def test_works_at_other_resolutions(cfg, renderer, poses, background):
    from robottracker.geometry import MazeGeometry
    from robottracker.synthetic import DEMO_CORNERS, MAZE_LENGTH_MM, MAZE_WIDTH_MM

    size = (1920, 1080)
    g = MazeGeometry([(x * 1.5, y * 1.5) for x, y in DEMO_CORNERS], MAZE_LENGTH_MM, MAZE_WIDTH_MM, 3.0)
    tracker = Tracker(cfg.tracker, g, background, size)
    errs = []
    for p in poses[:150]:
        frame = cv2.resize(renderer.render(p, p.t), size)
        pt = tracker.process(frame, p.t)
        if pt is not None:
            errs.append(math.hypot(pt.x_mm - p.x, pt.y_mm - p.y))
    assert np.sqrt(np.mean(np.square(errs))) < 6


def test_hand_touching_rover_does_not_drag_position(cfg, geometry, background, renderer):
    tracker = Tracker(cfg.tracker, geometry, background, FRAME_SIZE)
    park = Pose(0, 1676.4, 457.2, 0)
    t = 0.0
    for _ in range(30):
        tracker.process(renderer.render(park, t), t)
        t += 1 / 30
    worst = 0.0
    for i in range(60):
        hand = (park.x, max(1219 - i * 15, 540))  # arm reaches in until it touches the rover
        pt = tracker.process(renderer.render(park, t, hand=hand), t)
        t += 1 / 30
        if pt is not None:
            worst = max(worst, math.hypot(pt.x_mm - park.x, pt.y_mm - park.y))
    assert worst < 40
    assert tracker.state != TrackState.SEARCHING


def test_lost_then_reacquired_elsewhere(cfg, geometry, background, renderer):
    tracker = Tracker(cfg.tracker, geometry, background, FRAME_SIZE)
    a, b = Pose(0, 457.2, 1066.8, 90), Pose(0, 1676.4, 457.2, 0)
    t = 0.0
    for _ in range(20):
        tracker.process(renderer.render(a, t), t)
        t += 1 / 30
    for _ in range(int(30 * (cfg.tracker.max_coast_s + 0.5))):  # rover picked up
        tracker.process(renderer.render(None, t), t)
        t += 1 / 30
    assert tracker.state == TrackState.SEARCHING
    pt = None
    for _ in range(10):  # put down somewhere else
        pt = tracker.process(renderer.render(b, t), t)
        t += 1 / 30
    assert pt is not None and tracker.track_id == 2
    assert math.hypot(pt.x_mm - b.x, pt.y_mm - b.y) < 10


def test_operator_hint_picks_the_right_blob(cfg, geometry, background, renderer):
    tracker = Tracker(cfg.tracker, geometry, background, FRAME_SIZE)
    rover = Pose(0, 1676.4, 457.2, 0)
    tracker.set_hint(1650, 470)
    pt = tracker.process(renderer.render(rover, 0), 0)  # locks immediately, no confirmation wait
    assert pt is not None and math.hypot(pt.x_mm - rover.x, pt.y_mm - rover.y) < 10


def test_adaptive_fallback_without_background(cfg, geometry, renderer, poses):
    tracker = Tracker(cfg.tracker, geometry, None, FRAME_SIZE)
    errs, _ = run(tracker, renderer, poses[:300])
    assert len(errs) > 150  # usable, if less precise, once the rover moves
    assert np.median(errs) < 40


class MarkerRenderer(SceneRenderer):
    """Rover with a printed ArUco marker on top, top edge facing forward."""

    def __init__(self):
        super().__init__()
        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self.marker = cv2.aruco.generateImageMarker(d, 3, 60)

    def _draw_rover(self, img, pose):
        super()._draw_rover(img, pose)
        m = self.mpp
        side = int(150 / m)
        mk = cv2.resize(self.marker, (side, side), interpolation=cv2.INTER_NEAREST)
        mk = cv2.copyMakeBorder(mk, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
        mk = cv2.cvtColor(mk, cv2.COLOR_GRAY2BGR).astype(np.float32)
        n = mk.shape[0]
        big = int(n * 1.5)
        rot = cv2.getRotationMatrix2D((n / 2, n / 2), pose.heading_deg - 90, 1)
        rot[:, 2] += (big - n) / 2
        warped = cv2.warpAffine(mk, rot, (big, big), borderValue=(-1, -1, -1))
        x0, y0 = int(pose.x / m) - big // 2, int(pose.y / m) - big // 2
        roi = img[y0:y0 + big, x0:x0 + big]
        keep = warped[..., 0] >= 0
        roi[keep] = warped[keep]


@pytest.mark.parametrize("marker_id,expect", [(3, True), (7, False)])
def test_aruco_mode(cfg, geometry, poses, marker_id, expect):
    cfg.tracker.mode = "aruco"
    cfg.tracker.aruco_id = marker_id
    tracker = Tracker(cfg.tracker, geometry, None, FRAME_SIZE)
    renderer = MarkerRenderer()
    errs, heading_errs = [], []
    for p in poses[:400:2]:
        pt = tracker.process(renderer.render(p, p.t), p.t)
        if pt is None:
            continue
        errs.append(math.hypot(pt.x_mm - p.x, pt.y_mm - p.y))
        if pt.measured:
            heading_errs.append(abs((pt.heading_deg - p.heading_deg + 180) % 360 - 180))
    if not expect:
        assert not errs  # a different team's marker is ignored
        return
    assert np.sqrt(np.mean(np.square(errs))) < 6
    assert np.median(heading_errs) < 5
