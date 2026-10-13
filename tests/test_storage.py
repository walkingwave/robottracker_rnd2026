import json

import pytest

from robottracker.storage import TrialRecorder, TrialStore, read_trajectory
from robottracker.tracker import TrackPoint, TrackState

MAZE = {"length_mm": 2438.4, "width_mm": 1219.2, "walls": []}


def point(t, x, y=600.0, track_id=1):
    return TrackPoint(t, x, y, 300.0, 0.0, 0.0, TrackState.TRACKING, track_id, True)


def record(cfg, team="Team A", seconds=4.0, outcome="finished", events=()):
    rec = TrialRecorder(cfg.trials_dir, team, MAZE, cfg.metrics, False, 30)
    n = int(seconds * 30)
    for i in range(n):
        t = i / 30
        rec.add_point(t, point(t, 100 + 300 * t))
    for kind, sev in events:
        rec.add_event(kind, 1.0, sev)
    return rec, rec.finish((n - 1) / 30, outcome)


def test_recorder_writes_files(cfg):
    rec, info = record(cfg, events=[("collision", 2), ("intervention", None)])
    data = json.loads((rec.dir / "trial.json").read_text())
    assert data["status"] == "done" and data["outcome"] == "finished"
    assert data["summary"]["collisions"]["2"] == 1 and data["summary"]["interventions"] == 1
    assert abs(data["summary"]["distance_mm"] - 1200) < 40
    traj = read_trajectory(rec.dir / "trajectory.csv")
    assert len(traj["t"]) == 120 and traj["x_mm"][0] == 100.0


def test_event_positions_and_undo(cfg):
    rec = TrialRecorder(cfg.trials_dir, "B", MAZE, cfg.metrics, False, 30)
    rec.add_point(0.5, point(0.5, 700))
    e1 = rec.add_event("collision", 0.5, 1)
    e2 = rec.add_event("intervention", 0.6)
    assert e1["x_mm"] == 700.0
    assert rec.remove_event(e1["id"]) and not rec.remove_event(e1["id"])
    assert [e["id"] for e in rec.info["events"]] == [e2["id"]]
    rec.finish(1.0, None)


def test_unique_ids_for_same_team_same_second(cfg):
    a = TrialRecorder(cfg.trials_dir, "Same", MAZE, cfg.metrics, False, 30)
    b = TrialRecorder(cfg.trials_dir, "Same", MAZE, cfg.metrics, False, 30)
    assert a.id != b.id
    a.finish(0, None)
    b.finish(0, None)


def test_recover_interrupted_trial(cfg):
    rec = TrialRecorder(cfg.trials_dir, "Crashy", MAZE, cfg.metrics, False, 30)
    for i in range(90):
        rec.add_point(i / 30, point(i / 30, 100 + 300 * i / 30))
    rec._csv_file.flush()  # simulate a crash: never call finish()
    store = TrialStore(cfg.trials_dir, cfg.metrics, cfg.scoring)
    assert store.recover() == [rec.id]
    info = store.get(rec.id)
    assert info["status"] == "interrupted"
    assert info["summary"]["distance_mm"] > 700


def test_leaderboard_edit_and_discard(cfg):
    _, slow = record(cfg, "Slow", seconds=6)
    _, fast = record(cfg, "Fast", seconds=3)
    _, fast2 = record(cfg, "Fast", seconds=5)
    _, dnf = record(cfg, "Quitter", seconds=2, outcome="dnf")
    store = TrialStore(cfg.trials_dir, cfg.metrics, cfg.scoring)
    rows = store.leaderboard()
    assert [r["team"] for r in rows] == ["Fast", "Fast", "Slow", "Quitter"]
    assert [r["team"] for r in store.leaderboard(best_per_team=True)] == ["Fast", "Slow", "Quitter"]

    store.update(slow["id"], team="Slow Pokes", outcome="dnf")
    assert store.get(slow["id"])["team"] == "Slow Pokes"
    store.discard(fast["id"])
    assert fast["id"] not in [t["id"] for t in store.list()]
    assert (cfg.trials_dir / "_discarded" / fast["id"]).exists()
    with pytest.raises(KeyError):
        store.get("../etc")
