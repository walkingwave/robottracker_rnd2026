"""End-to-end: web API driving the engine on a synthetic video."""

import time

import pytest
from fastapi.testclient import TestClient

from robottracker.engine import Engine
from robottracker.server import create_app
from robottracker.synthetic import DEMO_CORNERS, FRAME_SIZE

TOKEN = {"x-operator-token": "secret"}


@pytest.fixture
def client(cfg):
    cfg.server.operator_token = "secret"
    cfg.camera.source = "none"
    engine = Engine(cfg)
    with TestClient(create_app(engine)) as c:
        c.engine = engine
        yield c


def wait_for(fn, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def test_operator_endpoints_need_token(client):
    assert client.post("/api/trial/start", json={"team": "x"}).status_code == 403
    assert client.get("/operator").status_code == 403
    assert client.get("/").status_code == 200
    assert client.get("/api/state").status_code == 200


def test_full_trial_flow(client, demo_video):
    st = lambda: client.get("/api/state").json()  # noqa: E731
    r = client.post("/api/trial/start", json={"team": "Early"}, headers=TOKEN)
    assert r.status_code == 400 and "source" in r.json()["detail"]

    assert client.post("/api/source", json={"kind": "file", "file": str(demo_video)}, headers=TOKEN).is_success
    assert st()["source"]["paused"] is True

    w, h = FRAME_SIZE
    corners = [[x / w, y / h] for x, y in DEMO_CORNERS]
    bad = client.post("/api/calibration", json={"corners": corners[::-1]}, headers=TOKEN)
    assert bad.status_code == 400
    assert client.post("/api/calibration", json={"corners": corners}, headers=TOKEN).is_success
    assert client.post("/api/background", headers=TOKEN).is_success  # paused on the empty first frame
    s = st()
    assert s["calibrated"] and s["has_background"]

    assert client.post("/api/trial/start", json={"team": "Rover Rangers"}, headers=TOKEN).is_success
    assert wait_for(lambda: (st()["trial"] or {}).get("t", 0) > 1.5)
    assert client.post("/api/trial/events", json={"kind": "collision", "severity": 2}, headers=TOKEN).is_success
    assert client.post("/api/trial/events", json={"kind": "intervention"}, headers=TOKEN).is_success
    assert client.delete("/api/trial/events/last", headers=TOKEN).json()["removed"]
    bad_ev = client.post("/api/trial/events", json={"kind": "collision", "severity": 9}, headers=TOKEN)
    assert bad_ev.status_code == 400

    # The clip ends by itself and the trial is closed automatically.
    assert wait_for(lambda: st()["trial"] is None)
    trial_id = st()["last_trial_id"]
    detail = client.get(f"/api/trials/{trial_id}").json()
    info, traj = detail["info"], detail["trajectory"]
    assert info["team"] == "Rover Rangers" and info["status"] == "done"
    assert [e["kind"] for e in info["events"]] == ["collision"]
    assert len(traj["t"]) > 60
    assert info["summary"]["tracked_fraction"] > 0.65  # first second of the clip is an empty maze
    assert info["video"] == "video.mp4"

    assert client.patch(f"/api/trials/{trial_id}", json={"outcome": "finished"}, headers=TOKEN).is_success
    board = client.get("/api/leaderboard").json()
    assert board[0]["team"] == "Rover Rangers" and board[0]["penalty_s"] == 5.0
    csv_text = client.get("/api/leaderboard.csv").text
    assert "Rover Rangers" in csv_text.splitlines()[1]
    assert client.get(f"/api/trials/{trial_id}/trajectory.csv").status_code == 200


def test_snapshot_and_views(client, demo_video):
    client.post("/api/source", json={"kind": "file", "file": str(demo_video)}, headers=TOKEN)
    snap = client.get("/snapshot.jpg", headers=TOKEN)
    assert snap.status_code == 200 and snap.content[:2] == b"\xff\xd8"
    w, h = FRAME_SIZE
    corners = [[x / w, y / h] for x, y in DEMO_CORNERS]
    client.post("/api/calibration", json={"corners": corners}, headers=TOKEN)
    for view in ("topdown", "mask", "camera"):
        assert client.post("/api/view", json={"view": view}, headers=TOKEN).is_success
        assert client.get("/snapshot.jpg", headers=TOKEN).status_code == 200
