import math

import numpy as np

from robottracker.config import MetricsConfig, ScoringConfig
from robottracker.kalman import ConstantVelocityKF
from robottracker.metrics import StatsAccumulator, penalty_s, rank_key


def feed(stats, ts, xs, ys, track_ids=None):
    for i, (t, x, y) in enumerate(zip(ts, xs, ys)):
        stats.tick(True)
        stats.add(t, x, y, 1 if track_ids is None else track_ids[i])


def test_straight_line_distance_with_jitter():
    rng = np.random.default_rng(0)
    t = np.arange(0, 10, 1 / 30)
    x = 200 + 300 * t + rng.normal(0, 4, t.size)  # 300 mm/s for 10 s = 3000 mm
    y = 600 + rng.normal(0, 4, t.size)
    s = StatsAccumulator(MetricsConfig())
    feed(s, t, x, y)
    assert abs(s.distance_mm - 3000) < 60
    assert 270 < s.top_speed_mms < 340


def test_stationary_jitter_adds_no_distance():
    rng = np.random.default_rng(1)
    t = np.arange(0, 30, 1 / 30)
    s = StatsAccumulator(MetricsConfig())
    feed(s, t, 500 + rng.normal(0, 5, t.size), 500 + rng.normal(0, 5, t.size))
    assert s.distance_mm < 30
    assert s.moving_time_s < 1


def test_spinning_in_place_adds_no_distance():
    # A centroid that wobbles 15 mm around a fixed point while the rover spins.
    t = np.arange(0, 10, 1 / 30)
    x = 800 + 15 * np.cos(t * 2 * math.pi)
    y = 400 + 15 * np.sin(t * 2 * math.pi)
    s = StatsAccumulator(MetricsConfig())
    feed(s, t, x, y)
    assert s.distance_mm < 40


def test_relock_jump_is_not_counted():
    t = np.arange(0, 2, 1 / 30)
    s = StatsAccumulator(MetricsConfig())
    feed(s, t, np.full(t.size, 300.0), np.full(t.size, 300.0))
    # Tracker re-acquires 1.5 m away: new track id, no phantom distance.
    feed(s, t + 2, np.full(t.size, 1800.0), np.full(t.size, 300.0), [2] * t.size)
    assert s.distance_mm == 0
    assert s.top_speed_mms == 0


def test_summary_counts_events():
    s = StatsAccumulator(MetricsConfig())
    events = [
        {"kind": "collision", "severity": 1}, {"kind": "collision", "severity": 3},
        {"kind": "collision", "severity": 3}, {"kind": "intervention"},
    ]
    summary = s.summary(60.0, events)
    assert summary.collisions == {"1": 1, "2": 0, "3": 2}
    assert summary.interventions == 1


def test_scoring_order():
    sc = ScoringConfig()
    fast_dirty = {"outcome": "finished", "summary": {"duration_s": 60, "collisions": {"3": 3}, "interventions": 0}}
    slow_clean = {"outcome": "finished", "summary": {"duration_s": 80, "collisions": {}, "interventions": 0}}
    dnf_far = {"outcome": "dnf", "summary": {"duration_s": 30, "distance_mm": 5000}}
    dnf_near = {"outcome": "dnf", "summary": {"duration_s": 30, "distance_mm": 1000}}
    unscored = {"outcome": None, "summary": {"duration_s": 10, "distance_mm": 9000}}
    assert penalty_s(fast_dirty["summary"], sc) == 30
    ranked = sorted([unscored, dnf_near, fast_dirty, dnf_far, slow_clean], key=lambda t: rank_key(t, sc))
    assert ranked == [slow_clean, fast_dirty, dnf_far, dnf_near, unscored]


def test_kalman_tracks_constant_velocity():
    rng = np.random.default_rng(2)
    kf = ConstantVelocityKF(0, 0, accel_noise=1000, meas_noise=8)
    errs, vxs, vys = [], [], []
    for i in range(1, 240):
        t = i / 30
        kf.predict(1 / 30)
        kf.update(250 * t + rng.normal(0, 8), 100 + rng.normal(0, 8))
        if i > 60:
            errs.append(math.hypot(kf.position[0] - 250 * t, kf.position[1] - 100))
            vxs.append(kf.velocity[0])
            vys.append(kf.velocity[1])
    assert abs(np.mean(vxs) - 250) < 10 and abs(np.mean(vys)) < 10
    assert np.std(vxs) < 50
    assert np.mean(errs) < 8  # smoother than the raw 8 mm noise
