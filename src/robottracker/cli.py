"""Command line: `robottracker [serve|demo|process|cameras|marker]`."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packets sent; picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return None


def _serve(cfg, open_browser: bool) -> None:
    import uvicorn

    from .engine import Engine, ensure_dirs
    from .server import create_app

    ensure_dirs(cfg)
    engine = Engine(cfg)
    app = create_app(engine)
    port = cfg.server.port
    print(f"\n  RobotTracker {__version__}")
    print(f"  Operator panel:  http://localhost:{port}/operator")
    ip = _lan_ip()
    if ip and cfg.server.host in ("0.0.0.0", "::"):
        print(f"  Dashboard:       http://{ip}:{port}/   (share this with students)")
    else:
        print(f"  Dashboard:       http://localhost:{port}/")
    print(f"  Trials saved in: {cfg.trials_dir}\n  Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=(f"http://localhost:{port}/operator",)).start()
    uvicorn.run(app, host=cfg.server.host, port=port, log_level="warning")


def cmd_serve(args) -> None:
    from .config import load_config

    cfg = load_config(args.config)
    if args.port:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host
    _serve(cfg, not args.no_browser)


def cmd_demo(args) -> None:
    from .config import load_config
    from .synthetic import write_demo

    out = Path(args.dir).resolve()
    print(f"Preparing demo workspace in {out}...")
    info = write_demo(out)
    cfg = load_config(info["config"])
    if args.port:
        cfg.server.port = args.port
    print("Demo ready. In the operator panel: Capture background -> enter a team -> Start.")
    _serve(cfg, not args.no_browser)


def cmd_cameras(args) -> None:
    from .video import list_cameras

    cams = list_cameras()
    if not cams:
        print("No cameras found.")
    for c in cams:
        print(c["label"])


def cmd_process(args) -> None:
    """Track a recorded video offline and save it as a trial."""
    import cv2

    from .config import load_config
    from .geometry import Calibration, MazeGeometry
    from .storage import TrialRecorder
    from .tracker import Tracker
    from .video import FileSource

    cfg = load_config(args.config)
    cal = Calibration.load(cfg.setup_dir / "calibration.json")
    if cal is None:
        sys.exit("No calibration found. Calibrate in the operator panel first.")
    src = FileSource(args.video, realtime=False)
    geom = MazeGeometry(cal.scaled_to(src.size), cfg.maze.length_mm, cfg.maze.width_mm,
                        cfg.tracker.mm_per_px)
    bg_path = cfg.setup_dir / "background.png"
    bg = cv2.imread(str(bg_path)) if bg_path.exists() else None
    if bg is None and cfg.tracker.mode == "motion":
        print("No captured background: estimating one from the footage...")
        bg = _background_from_video(args.video, geom)
    tracker = Tracker(cfg.tracker, geom, bg, src.size)
    maze = {"length_mm": cfg.maze.length_mm, "width_mm": cfg.maze.width_mm,
            "walls": [list(w) for w in cfg.maze.walls]}
    rec = TrialRecorder(cfg.trials_dir, args.team, maze, cfg.metrics, False, src.nominal_fps)
    src.play()
    t = 0.0
    n = 0
    while True:
        item = src.read()
        if item is None:
            break
        frame, t = item
        rec.add_point(t, tracker.process(frame, t))
        n += 1
        if n % 300 == 0:
            print(f"  {n} frames...", end="\r")
    src.close()
    info = rec.finish(t, args.outcome, f"Processed offline from {Path(args.video).name}")
    s = info["summary"]
    print(f"\nSaved trial {info['id']}")
    print(f"  duration {s['duration_s']:.1f} s, distance {s['distance_mm'] / 1000:.2f} m, "
          f"tracked {s['tracked_fraction'] * 100:.0f}% of frames")


def _background_from_video(path, geom, samples: int = 31):
    """Median of frames spread across the clip: the moving rover disappears."""
    import cv2

    from .detection import MotionDetector

    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i * max(n - 1, 0) / max(samples - 1, 1)))
        ok, frame = cap.read()
        if ok:
            frames.append(geom.warp(frame))
    cap.release()
    return MotionDetector.build_background(frames) if frames else None


def cmd_marker(args) -> None:
    """Write a printable ArUco marker PNG."""
    import cv2
    import numpy as np

    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, args.dictionary))
    px = int(args.size_mm / 25.4 * 300)  # 300 dpi
    img = cv2.aruco.generateImageMarker(d, args.id, px)
    border = px // 8
    img = cv2.copyMakeBorder(img, border, border * 3, border, border, cv2.BORDER_CONSTANT, value=255)
    label = f"id {args.id}  {args.dictionary}  {args.size_mm:.0f} mm  ^ front of rover ^"
    cv2.putText(img, label, (border, img.shape[0] - border), cv2.FONT_HERSHEY_SIMPLEX,
                px / 900, 0, max(1, px // 300), cv2.LINE_AA)
    out = Path(args.out or f"marker_{args.id}.png")
    cv2.imwrite(str(out), np.ascontiguousarray(img))
    print(f"Wrote {out}. Print at 100% scale (300 dpi), top edge toward the rover's front.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="robottracker", description="Overhead rover tracking.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--config", default="config.yaml", help="config file (default: config.yaml)")

    sp = sub.add_parser("serve", help="run the tracker, operator panel and dashboard (default)")
    common(sp)
    sp.add_argument("--port", type=int)
    sp.add_argument("--host")
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("demo", help="try it with a generated video, no camera needed")
    sp.add_argument("--dir", default="demo")
    sp.add_argument("--port", type=int)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=cmd_demo)

    sp = sub.add_parser("process", help="track a recorded video offline and save a trial")
    common(sp)
    sp.add_argument("video")
    sp.add_argument("--team", required=True)
    sp.add_argument("--outcome", choices=["finished", "dnf"])
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("cameras", help="list connected cameras")
    sp.set_defaults(fn=cmd_cameras)

    sp = sub.add_parser("marker", help="generate a printable ArUco marker")
    sp.add_argument("--id", type=int, default=0)
    sp.add_argument("--size-mm", type=float, default=120)
    sp.add_argument("--dictionary", default="DICT_4X4_50")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_marker)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "serve")  # `robottracker` alone runs the server
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
