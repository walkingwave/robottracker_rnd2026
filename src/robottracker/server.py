"""Web server: public dashboard at /, operator panel at /operator."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import Engine
from .video import list_cameras

WEB_DIR = Path(__file__).parent / "web"
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_COOKIE = "rt_operator"


class SourceBody(BaseModel):
    kind: Literal["camera", "file"]
    index: int | None = None
    file: str | None = None


class StartBody(BaseModel):
    team: str


class StopBody(BaseModel):
    outcome: Literal["finished", "dnf"] | None = None
    notes: str = ""


class EventBody(BaseModel):
    kind: Literal["collision", "intervention"]
    severity: int | None = None


class PointBody(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    view: str | None = None


class CalibrationBody(BaseModel):
    corners: list[tuple[float, float]] = Field(min_length=4, max_length=4)


class ViewBody(BaseModel):
    view: Literal["camera", "topdown", "mask"]


class TrialEditBody(BaseModel):
    team: str | None = None
    outcome: Literal["finished", "dnf", "none"] | None = None
    notes: str | None = None


def create_app(engine: Engine, start_engine: bool = True) -> FastAPI:
    cfg = engine.cfg

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_engine:
            await asyncio.to_thread(engine.start)
        yield
        await asyncio.to_thread(engine.close)

    app = FastAPI(title="RobotTracker", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        # Always revalidate UI files so an update is picked up without a hard refresh.
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path in ("/", "/operator"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def is_operator(request: Request) -> bool:
        host = request.client.host if request.client else ""
        if host in _LOCAL_HOSTS:
            return True
        token = cfg.server.operator_token
        if not token:
            return False
        supplied = (
            request.cookies.get(_COOKIE)
            or request.headers.get("x-operator-token")
            or request.query_params.get("token")
        )
        return supplied == token

    def operator(request: Request) -> None:
        if not is_operator(request):
            raise HTTPException(
                403,
                "Operator controls only work on the tracking computer "
                "(or set server.operator_token in config.yaml).",
            )

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, f"Not found: {exc}") from None
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from None

    # ── pages ────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def dashboard_page():
        return FileResponse(WEB_DIR / "dashboard.html")

    @app.get("/operator", response_class=HTMLResponse)
    def operator_page(request: Request):
        if not is_operator(request):
            return HTMLResponse(
                "<h1>Operator panel</h1><p>Open this page on the tracking computer, "
                "or set <code>server.operator_token</code> and open "
                "<code>/operator?token=…</code>.</p><p><a href='/'>Go to the dashboard</a></p>",
                status_code=403,
            )
        resp = FileResponse(WEB_DIR / "operator.html")
        token = request.query_params.get("token")
        if token and token == cfg.server.operator_token:
            resp.set_cookie(_COOKIE, token, httponly=True, samesite="strict")
        return resp

    # ── live state ───────────────────────────────────────────────────────────

    @app.get("/api/config")
    def public_config(request: Request):
        return {
            "maze": engine._maze_dict(),
            "scoring": {
                "collision_penalty_s": cfg.scoring.collision_penalty_s,
                "intervention_penalty_s": cfg.scoring.intervention_penalty_s,
            },
            "public_video": cfg.server.public_video,
            "operator": is_operator(request),
            "mode": cfg.tracker.mode,
        }

    @app.get("/api/state")
    def state():
        return engine.status()[1]

    @app.get("/api/stream")
    async def stream(request: Request):
        async def events():
            last = -1
            while not await request.is_disconnected():
                version, st = await asyncio.to_thread(engine.status)
                if version != last:
                    last = version
                    yield f"data: {json.dumps(st)}\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/video.mjpg")
    async def video(request: Request):
        if not cfg.server.public_video:
            operator(request)
        interval = 1.0 / max(1, cfg.server.stream_fps)

        async def frames():
            last, sent_at = -1, 0.0
            loop = asyncio.get_running_loop()
            while not await request.is_disconnected():
                version, jpg = await asyncio.to_thread(engine.jpeg)
                # Browsers only draw an MJPEG part once the next one starts, so a
                # still image (paused video) has to be repeated to appear at all.
                if jpg is not None and (version != last or loop.time() - sent_at > 0.5):
                    last, sent_at = version, loop.time()
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                await asyncio.sleep(interval / 2)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/snapshot.jpg")
    def snapshot(request: Request):
        if not cfg.server.public_video:
            operator(request)
        _, jpg = engine.jpeg()
        if jpg is None:
            raise HTTPException(404, "No image yet")
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ── operator controls ────────────────────────────────────────────────────

    op = [Depends(operator)]

    @app.get("/api/cameras", dependencies=op)
    def cameras():
        return list_cameras()

    @app.get("/api/videos", dependencies=op)
    def videos():
        """Video files near the config, to pick from instead of typing a path."""
        base = cfg.base_dir
        exts = {".mp4", ".avi", ".mov", ".mkv"}
        found = []
        for pattern in ("*", "*/*"):
            for p in sorted(base.glob(pattern)):
                if p.suffix.lower() in exts and p.is_file():
                    found.append(str(p))
        return found[:200]

    @app.post("/api/source", dependencies=op)
    def source(body: SourceBody):
        return {"label": run(engine.connect, body.kind, index=body.index, file=body.file)}

    @app.delete("/api/source", dependencies=op)
    def disconnect():
        run(engine.disconnect)
        return {"ok": True}

    @app.post("/api/calibration", dependencies=op)
    def calibrate(body: CalibrationBody):
        run(engine.set_calibration, body.corners)
        return {"ok": True}

    @app.post("/api/background", dependencies=op)
    def background():
        run(engine.capture_background)
        return {"ok": True}

    @app.post("/api/reacquire", dependencies=op)
    def reacquire(body: PointBody):
        run(engine.reacquire, body.x, body.y, body.view)
        return {"ok": True}

    @app.post("/api/view", dependencies=op)
    def view(body: ViewBody):
        run(engine.set_view, body.view)
        return {"ok": True}

    @app.post("/api/trial/start", dependencies=op)
    def start(body: StartBody):
        return run(engine.start_trial, body.team)

    @app.post("/api/trial/stop", dependencies=op)
    def stop(body: StopBody):
        return run(engine.stop_trial, body.outcome, body.notes)

    @app.post("/api/trial/events", dependencies=op)
    def add_event(body: EventBody):
        return run(engine.log_event, body.kind, body.severity)

    @app.delete("/api/trial/events/last", dependencies=op)
    def undo_last():
        return {"removed": run(engine.undo_event)}

    @app.delete("/api/trial/events/{event_id}", dependencies=op)
    def undo(event_id: int):
        return {"removed": run(engine.undo_event, event_id)}

    @app.patch("/api/trials/{trial_id}", dependencies=op)
    def edit_trial(trial_id: str, body: TrialEditBody):
        outcome = False if body.outcome is None else (None if body.outcome == "none" else body.outcome)
        return run(engine.store.update, trial_id, team=body.team, outcome=outcome, notes=body.notes)

    @app.delete("/api/trials/{trial_id}", dependencies=op)
    def discard_trial(trial_id: str):
        run(engine.store.discard, trial_id)
        return {"ok": True}

    # ── results (public) ─────────────────────────────────────────────────────

    @app.get("/api/trials")
    def trials():
        return [{k: v for k, v in t.items() if k != "events"} for t in engine.store.list()]

    @app.get("/api/trials/{trial_id}")
    def trial(trial_id: str):
        info = run(engine.store.get, trial_id)
        try:
            traj = engine.store.trajectory(trial_id)
        except KeyError:
            traj = {}
        return {"info": info, "trajectory": traj}

    @app.get("/api/trials/{trial_id}/trajectory.csv")
    def trial_csv(trial_id: str):
        run(engine.store.get, trial_id)
        return FileResponse(engine.store.dir / trial_id / "trajectory.csv", media_type="text/csv",
                            filename=f"{trial_id}.csv")

    @app.get("/api/trials/{trial_id}/video.mp4")
    def trial_video(trial_id: str, request: Request):
        if not cfg.server.public_video:
            operator(request)
        path = run(engine.store.video_path, trial_id)
        if path is None:
            raise HTTPException(404, "No video recorded for this trial")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/leaderboard")
    def leaderboard(best_per_team: bool = False):
        return engine.store.leaderboard(best_per_team)

    @app.get("/api/leaderboard.csv")
    def leaderboard_csv(best_per_team: bool = False):
        rows = engine.store.leaderboard(best_per_team)
        buf = io.StringIO()
        fields = ["rank", "team", "outcome", "duration_s", "penalty_s", "adjusted_s", "distance_mm",
                  "avg_speed_mms", "top_speed_mms", "collisions_1", "collisions_2", "collisions_3",
                  "interventions", "tracked_fraction", "started_at", "id"]
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            c = r["collisions"]
            w.writerow({**r, "collisions_1": c.get("1", 0), "collisions_2": c.get("2", 0),
                        "collisions_3": c.get("3", 0)})
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=leaderboard.csv"})

    return app
