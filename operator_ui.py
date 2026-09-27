"""MIE491 Operator UI — instructor panel for running rover trials.

Run with:
    python operator_ui.py
    python operator_ui.py --config config.yaml
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, font as tkfont
from pathlib import Path

import cv2
import yaml

try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

from rover_tracker.data.trial_logger import TrialLogger
from rover_tracker.events.event_detector import DetectedEvent, EventDetector
from rover_tracker.perception.homography import HomographyTransform
from rover_tracker.perception.tracker import RoverTracker
from rover_tracker.state.rover_state import StateHistory


# ── colour palette ────────────────────────────────────────────────────────────
BG      = "#0d1117"
BG2     = "#161b22"
BORDER  = "#30363d"
FG      = "#e6edf3"
FG_DIM  = "#8b949e"
GREEN   = "#238636"
RED     = "#da3633"
ORANGE  = "#d29922"
PURPLE  = "#6e40c9"
BLUE    = "#1f6feb"
YELLOW  = "#f0c040"

CLS_COLORS = {
    "Class 1": ("#e67e22", "white"),
    "Class 2": ("#c0392b", "white"),
    "Class 3": ("#6c3483", "white"),
}


def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _sanitize(name: str) -> str:
    safe = re.sub(r"[^\w\-]", "_", name.strip()).strip("_")
    return safe or "unnamed"


# ─────────────────────────────────────────────────────────────────────────────

def _macos_camera_names() -> list[str]:
    """Camera names from system_profiler (macOS only). Order matches AVFoundation index order."""
    if sys.platform != "darwin":
        return []
    try:
        out = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return [d.get("_name", "?") for d in json.loads(out).get("SPCameraDataType", [])]
    except Exception:
        return []


class OperatorApp:
    _FEED_W = 680
    _FEED_H = 383

    def __init__(self, cfg: dict):
        self._cfg = cfg

        # Trial state
        self._running    = threading.Event()
        self._trial_t0   = 0.0
        self._logger:   TrialLogger | None = None
        self._history   = StateHistory()
        self._detector: EventDetector | None = None

        # Camera thread control
        self._frame_q:  queue.Queue = queue.Queue(maxsize=2)
        self._stop_cam  = threading.Event()
        self._cam_thread: threading.Thread | None = None
        self._cam_fps   = 30.0

        # Callables queued by worker threads, executed on the Tk thread in _poll().
        # (root.after() is not safe to call from other threads on macOS / Tk 9:
        #  the callback silently never fires.)
        self._ui_q: queue.Queue = queue.Queue()

        self._build_ui()
        self._root.after(33, self._poll)

    def _call_ui(self, fn, *args):
        """Thread-safe: schedule fn(*args) to run on the Tk main thread."""
        self._ui_q.put((fn, args))

    # ══════════════════════════════════════════════════════════════════════════
    # UI Construction
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        self._root = tk.Tk()
        self._root.title("RobotTracker — Operator Panel")
        self._root.configure(bg=BG)
        self._root.geometry("1060x560")
        self._root.resizable(True, True)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        fb = tkfont.Font(family="Helvetica", size=13, weight="bold")
        fh = tkfont.Font(family="Helvetica", size=15, weight="bold")
        fl = tkfont.Font(family="Helvetica", size=10)
        fs = tkfont.Font(family="Helvetica", size=9)
        fc = tkfont.Font(family="Courier",   size=10)
        ft = tkfont.Font(family="Courier",   size=24, weight="bold")

        outer = tk.Frame(self._root, bg=BG)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        # ── LEFT: feed ───────────────────────────────────────────────────────
        left = tk.Frame(outer, bg=BG)
        left.pack(side="left", fill="both", expand=True)

        tk.Label(left, text="LIVE FEED", font=fh, bg=BG, fg=BLUE).pack(anchor="w")

        feed_wrap = tk.Frame(left, bg=BORDER, bd=2)
        feed_wrap.pack(pady=(4, 0))

        # Fixed-size black canvas as placeholder until first frame arrives
        self._feed_canvas = tk.Canvas(
            feed_wrap, width=self._FEED_W, height=self._FEED_H,
            bg="black", highlightthickness=0,
        )
        self._feed_canvas.pack()

        # Placeholder text on canvas
        self._canvas_text = self._feed_canvas.create_text(
            self._FEED_W // 2, self._FEED_H // 2,
            text="No source connected\nSelect a source and click  Connect  →",
            fill=FG_DIM, font=fl, justify="center",
        )

        self._status_var = tk.StringVar(value="○  No source")
        tk.Label(left, textvariable=self._status_var,
                 font=fs, bg=BG, fg=FG_DIM).pack(anchor="w", pady=(4, 0))

        # ── RIGHT: controls ──────────────────────────────────────────────────
        right = tk.Frame(outer, bg=BG, width=290)
        right.pack(side="right", fill="y", padx=(14, 0))
        right.pack_propagate(False)

        tk.Label(right, text="🏁 RobotTracker", font=fh, bg=BG, fg=YELLOW).pack(pady=(0, 2))
        tk.Label(right, text="Operator Panel", font=fl, bg=BG, fg=FG_DIM).pack()
        self._div(right)

        # ── Source selection ─────────────────────────────────────────────────
        tk.Label(right, text="VIDEO SOURCE", font=fl, bg=BG, fg=FG_DIM).pack(anchor="w")

        self._source_var = tk.StringVar(value="file")
        src_row = tk.Frame(right, bg=BG)
        src_row.pack(fill="x", pady=(4, 6))

        tk.Radiobutton(
            src_row, text="📷  Live Camera", variable=self._source_var,
            value="camera", bg=BG, fg=FG, selectcolor=BG2,
            activebackground=BG, activeforeground=FG,
            font=fl, command=self._on_source_change,
        ).pack(side="left")

        tk.Radiobutton(
            src_row, text="📁  Video File", variable=self._source_var,
            value="file", bg=BG, fg=FG, selectcolor=BG2,
            activebackground=BG, activeforeground=FG,
            font=fl, command=self._on_source_change,
        ).pack(side="left", padx=(10, 0))

        # Camera picker row (shown when "camera" selected)
        self._cam_frame = tk.Frame(right, bg=BG)
        tk.Label(self._cam_frame, text="Camera", font=fl, bg=BG, fg=FG_DIM).pack(side="left")

        cfg_idx = int(self._cfg.get("sensor", {}).get("camera_index", 0))
        self._cam_var = tk.StringVar(value=str(cfg_idx))
        self._cam_menu = tk.OptionMenu(self._cam_frame, self._cam_var, str(cfg_idx))
        self._cam_menu.config(
            font=fl, bg=BG2, fg=FG, activebackground=BORDER, activeforeground=FG,
            relief="flat", highlightthickness=0, bd=0, cursor="hand2",
        )
        self._cam_menu["menu"].config(bg=BG2, fg=FG, activebackground=BORDER, font=fl)
        self._cam_menu.pack(side="left", fill="x", expand=True, padx=(6, 0))

        self._scan_btn = tk.Button(
            self._cam_frame, text="Scan",
            font=fl, bg=BORDER, fg=FG,
            activebackground=BG2, relief="flat",
            padx=6, cursor="hand2",
            command=self._scan_cameras,
        )
        self._scan_btn.pack(side="right", padx=(4, 0))

        # File path row (shown when "file" selected)
        self._file_frame = tk.Frame(right, bg=BG)
        self._file_frame.pack(fill="x", pady=(0, 6))

        self._file_var = tk.StringVar(
            value=self._cfg.get("sensor", {}).get("file_path", "")
        )
        self._file_entry = tk.Entry(
            self._file_frame, textvariable=self._file_var,
            font=tkfont.Font(family="Helvetica", size=9),
            bg=BG2, fg=FG_DIM, insertbackground=FG,
            relief="flat", bd=4,
        )
        self._file_entry.pack(side="left", fill="x", expand=True)

        tk.Button(
            self._file_frame, text="Browse",
            font=fl, bg=BORDER, fg=FG,
            activebackground=BG2, relief="flat",
            padx=6, cursor="hand2",
            command=self._browse_file,
        ).pack(side="right", padx=(4, 0))

        self._connect_btn = tk.Button(
            right, text="⚡  Connect",
            font=fb, bg=BLUE, fg="white",
            activebackground="#1158b0",
            relief="flat", pady=8, cursor="hand2",
            command=self._connect_source,
        )
        self._connect_btn.pack(fill="x")
        self._div(right)

        # ── Rover name ───────────────────────────────────────────────────────
        tk.Label(right, text="ROVER / TEAM NAME", font=fl, bg=BG, fg=FG_DIM).pack(anchor="w")
        self._name_var = tk.StringVar()
        self._name_entry = tk.Entry(
            right, textvariable=self._name_var,
            font=tkfont.Font(family="Helvetica", size=14, weight="bold"),
            bg=BG2, fg=YELLOW, insertbackground=YELLOW,
            relief="flat", bd=8,
        )
        self._name_entry.pack(fill="x", pady=(4, 0))
        self._div(right)

        # ── Timer + Start/Stop ───────────────────────────────────────────────
        self._timer_var = tk.StringVar(value="00:00.0")
        tk.Label(right, textvariable=self._timer_var,
                 font=ft, bg=BG, fg=FG).pack()

        self._start_btn = tk.Button(
            right, text="▶  START TRIAL",
            font=fb, bg=GREEN, fg="white",
            activebackground="#196127",
            relief="flat", pady=12, cursor="hand2",
            command=self._start_trial,
        )
        self._start_btn.pack(fill="x", pady=(6, 3))

        self._stop_btn = tk.Button(
            right, text="⏹  STOP TRIAL",
            font=fb, bg=BORDER, fg=FG_DIM,
            relief="flat", pady=12, cursor="hand2",
            state="disabled", command=self._stop_trial,
        )
        self._stop_btn.pack(fill="x")
        self._div(right)

        # ── Logging mode toggles ─────────────────────────────────────────────
        tk.Label(right, text="LOGGING MODE", font=fl, bg=BG, fg=FG_DIM).pack(anchor="w")

        self._collision_mode     = tk.StringVar(value="auto")
        self._intervention_mode  = tk.StringVar(value="auto")

        for label, var in [("Collisions", self._collision_mode),
                           ("Interventions", self._intervention_mode)]:
            row = tk.Frame(right, bg=BG)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"{label}:", font=fl, bg=BG, fg=FG,
                     width=13, anchor="w").pack(side="left")
            for val, txt in [("auto", "Auto"), ("manual", "Manual only")]:
                tk.Radiobutton(
                    row, text=txt, variable=var, value=val,
                    bg=BG, fg=FG, selectcolor=BG2,
                    activebackground=BG, activeforeground=FG,
                    font=fl,
                ).pack(side="left", padx=(0, 6))

        self._div(right)

        # ── Collision buttons ────────────────────────────────────────────────
        tk.Label(right, text="LOG COLLISION", font=fl, bg=BG, fg=FG_DIM).pack(anchor="w")
        cls_row = tk.Frame(right, bg=BG)
        cls_row.pack(fill="x", pady=(4, 6))

        for cls, (bg_col, fg_col) in CLS_COLORS.items():
            tk.Button(
                cls_row, text=cls,
                font=fb, bg=bg_col, fg=fg_col,
                activebackground=bg_col,
                relief="flat", pady=8, cursor="hand2",
                command=lambda c=cls: self._log_collision(c),
            ).pack(side="left", expand=True, fill="x", padx=2)

        tk.Button(
            right, text="🖐  MANUAL INTERVENTION",
            font=fb, bg=PURPLE, fg="white",
            activebackground="#5a2fa0",
            relief="flat", pady=10, cursor="hand2",
            command=self._log_intervention,
        ).pack(fill="x")
        self._div(right)

        # ── Event log ─────────────────────────────────────────────────────────
        tk.Label(right, text="EVENT LOG", font=fl, bg=BG, fg=FG_DIM).pack(anchor="w")
        log_wrap = tk.Frame(right, bg=BORDER, bd=1)
        log_wrap.pack(fill="both", expand=True, pady=(4, 0))

        self._log = tk.Text(
            log_wrap, font=fc, bg=BG2, fg=FG,
            relief="flat", state="disabled", width=34,
        )
        sb = tk.Scrollbar(log_wrap, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log.pack(fill="both", expand=True, padx=1, pady=1)

        self._log.tag_config("collision",    foreground=ORANGE)
        self._log.tag_config("intervention", foreground="#bc8cff")
        self._log.tag_config("auto",         foreground=FG_DIM)
        self._log.tag_config("info",         foreground=BLUE)
        self._log.tag_config("warn",         foreground=RED)
        self._log.tag_config("err",          foreground=RED)

    def _div(self, parent):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=8)

    # ══════════════════════════════════════════════════════════════════════════
    # Source selection
    # ══════════════════════════════════════════════════════════════════════════

    def _on_source_change(self):
        if self._source_var.get() == "camera":
            self._file_frame.pack_forget()
            self._cam_frame.pack(fill="x", pady=(0, 6),
                                 before=self._connect_btn)
        else:
            self._cam_frame.pack_forget()
            self._file_frame.pack(fill="x", pady=(0, 6),
                                  before=self._connect_btn)

    @staticmethod
    def _parse_cam_index(label: str) -> int:
        """'1  (1280x720)' -> 1"""
        m = re.match(r"\s*(\d+)", label)
        return int(m.group(1)) if m else 0

    def _scan_cameras(self, max_index: int = 6):
        """Probe camera indices in a background thread and fill the dropdown."""
        self._scan_btn.config(state="disabled", text="Scanning…")
        self._write_log("🔍  Scanning for cameras…", "info")

        def probe(i: int, out: dict):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                ret, frame = cap.read()
                out["res"] = frame.shape[1::-1] if ret and frame is not None else None
                out["ok"] = True
            cap.release()

        def worker():
            names = _macos_camera_names()
            limit = min(max_index, len(names)) if names else max_index
            found = []
            for i in range(limit):
                out: dict = {}
                # Probe each index in its own thread so a wedged camera
                # (permission prompt, device busy) can't hang the scan.
                t = threading.Thread(target=probe, args=(i, out), daemon=True)
                t.start()
                t.join(timeout=5.0)
                if t.is_alive():
                    found.append(f"{i}  (timeout: busy or no permission)")
                    if not names:
                        break
                    continue
                if not out.get("ok"):
                    if not names:
                        break          # AVFoundation indices are contiguous
                    continue
                name = f" {names[i]}" if i < len(names) else ""
                res  = f"{out['res'][0]}x{out['res'][1]}" if out.get("res") else "no signal"
                found.append(f"{i} {name} ({res})".replace("  ", " "))
            self._call_ui(self._scan_done, found)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_done(self, found: list[str]):
        self._scan_btn.config(state="normal", text="Scan")
        menu = self._cam_menu["menu"]
        menu.delete(0, "end")
        if not found:
            self._write_log("⚠  No cameras found (check USB / camera permission)", "warn")
            found = [self._cam_var.get()]
        for label in found:
            menu.add_command(label=label, command=lambda v=label: self._cam_var.set(v))
        current = self._parse_cam_index(self._cam_var.get())
        match = next((f for f in found if self._parse_cam_index(f) == current), found[0])
        self._cam_var.set(match)
        self._write_log(f"📷  Found {len(found)} camera(s): " + ", ".join(found), "info")

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select video file",
            filetypes=[("Video files", "*.mp4 *.avi *.mov *.mkv"), ("All files", "*.*")],
        )
        if path:
            self._file_var.set(path)

    def _connect_source(self):
        # Stop any existing camera thread
        if self._cam_thread and self._cam_thread.is_alive():
            self._stop_cam.set()
            self._cam_thread.join(timeout=2)

        self._stop_cam.clear()
        # Drain old frames
        while not self._frame_q.empty():
            try:
                self._frame_q.get_nowait()
            except queue.Empty:
                break

        source = self._source_var.get()
        self._cfg.setdefault("sensor", {})["source"] = source
        if source == "file":
            path = self._file_var.get().strip()
            if not path:
                self._write_log("⚠  No file selected", "warn")
                return
            self._cfg["sensor"]["file_path"] = path
        else:
            self._cfg["sensor"]["camera_index"] = self._parse_cam_index(self._cam_var.get())

        self._status_var.set("⏳  Connecting…")
        self._feed_canvas.itemconfig(self._canvas_text, text="Connecting…")
        self._connect_btn.config(state="disabled")

        self._cam_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self._cam_thread.start()

    # ══════════════════════════════════════════════════════════════════════════
    # Camera / processing thread
    # ══════════════════════════════════════════════════════════════════════════

    def _camera_loop(self):
        scfg   = self._cfg.get("sensor", {})
        source = scfg.get("source", "file")

        if source == "camera":
            cap = cv2.VideoCapture(scfg.get("camera_index", 0))
        else:
            cap = cv2.VideoCapture(scfg.get("file_path", ""))

        if not cap.isOpened():
            msg = (f"Camera index {scfg.get('camera_index', 0)} not found\n(try Scan, or check camera permission)"
                   if source == "camera"
                   else f"Could not open file:\n{scfg.get('file_path','')}")
            self._call_ui(self._cam_error, msg)
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._cam_fps = fps

        homography = HomographyTransform.from_config(self._cfg)
        tracker    = RoverTracker(self._cfg, homography)

        # Signal UI that we're live
        self._call_ui(self._cam_connected)

        # For file source: show first frame as frozen preview, wait for Start Trial
        preview = None
        if source == "file":
            ret, preview = cap.read()
            if ret:
                self._push_display_frame(preview)
            # Hold here until the trial starts (or user disconnects)
            while not self._running.is_set() and not self._stop_cam.is_set():
                time.sleep(0.05)
            # Reset to beginning so trial plays from frame 0
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        else:
            # Live camera: read one frame to seed initialDetection
            ret, preview = cap.read()

        frame_n = 0
        if preview is not None:
            tracker.initialDetection(preview, 0)
        while not self._stop_cam.is_set():
            # For file: pause between trials (after a trial ends, hold on last frame)
            if source == "file" and not self._running.is_set():
                while not self._running.is_set() and not self._stop_cam.is_set():
                    time.sleep(0.05)
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                frame_n = 0
                tracker.reset()

            ret, frame = cap.read()
            if not ret:
                if source == "file":
                    # End of file — stop the trial automatically if running
                    if self._running.is_set():
                        self._call_ui(self._stop_trial)
                    time.sleep(0.05)
                    continue
                else:
                    time.sleep(0.03)
                    continue

            frame_n += 1
            ts = frame_n / fps

            state = tracker.process_frame(frame, ts)
            _dbg = tracker.get_debug_frame()
            debug = _dbg if _dbg is not None else frame

            if self._running.is_set() and state is not None:
                events = self._detector.update(state, self._history)
                self._history.append(state)
                self._logger.log_state(state)
                for ev in events:
                    # Skip auto-detected events when operator has chosen manual-only mode
                    if (ev.event_type == "wall_collision"
                            and self._collision_mode.get() == "manual"):
                        continue
                    if (ev.event_type == "manual_intervention"
                            and self._intervention_mode.get() == "manual"):
                        continue
                    self._logger.log_event(ev)
                    label = f"{ts:6.1f}s  ⚡ {ev.event_type}"
                    self._call_ui(self._write_log, label, "auto")

            self._push_display_frame(debug)

        cap.release()

    def _push_display_frame(self, frame):
        """Resize and queue a single frame for display (called from camera thread)."""
        h, w   = frame.shape[:2]
        scale  = min(self._FEED_W / w, self._FEED_H / h)
        dw, dh = int(w * scale), int(h * scale)
        disp   = cv2.resize(frame, (dw, dh))
        disp   = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
        try:
            self._frame_q.put_nowait(disp)
        except queue.Full:
            try:
                self._frame_q.get_nowait()
            except queue.Empty:
                pass
            self._frame_q.put_nowait(disp)

    def _cam_connected(self):
        self._connect_btn.config(state="normal", text="↺  Reconnect")
        self._feed_canvas.itemconfig(self._canvas_text, text="")
        self._write_log("⚡  Source connected", "info")

    def _cam_error(self, msg: str):
        self._connect_btn.config(state="normal")
        self._status_var.set("✗  Connection failed")
        self._feed_canvas.itemconfig(self._canvas_text, text=f"⚠  {msg}")
        self._write_log(f"✗  {msg}", "err")

    # ══════════════════════════════════════════════════════════════════════════
    # UI poll (main thread, every 33 ms)
    # ══════════════════════════════════════════════════════════════════════════

    def _poll(self):
        while True:
            try:
                fn, args = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as exc:          # never let a worker callback kill the poll loop
                print(f"UI callback error: {exc!r}")

        try:
            frame = self._frame_q.get_nowait()
            if _PIL:
                img = ImageTk.PhotoImage(Image.fromarray(frame))
                self._feed_canvas.delete("frame")
                # Centre image on canvas
                x = self._FEED_W // 2
                y = self._FEED_H // 2
                self._feed_canvas.create_image(x, y, image=img, anchor="center", tags="frame")
                self._feed_canvas._img = img
            recording = self._running.is_set()
            self._status_var.set(
                f"{'● RECORDING' if recording else '○ Standby'}"
                f"  |  {self._cam_fps:.0f} fps"
            )
        except queue.Empty:
            pass

        if self._running.is_set():
            elapsed = time.time() - self._trial_t0
            m = int(elapsed // 60)
            s = elapsed % 60
            self._timer_var.set(f"{m:02d}:{s:04.1f}")

        self._root.after(33, self._poll)

    # ══════════════════════════════════════════════════════════════════════════
    # Trial control
    # ══════════════════════════════════════════════════════════════════════════

    def _start_trial(self):
        if self._cam_thread is None or not self._cam_thread.is_alive():
            self._write_log("⚠  Connect a source first", "warn")
            return

        name = self._name_var.get().strip()
        if not name:
            self._write_log("⚠  Enter a rover name first", "warn")
            self._name_entry.config(bg="#3d1010")
            self._root.after(700, lambda: self._name_entry.config(bg=BG2))
            return

        trial_id = f"{_sanitize(name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cfg_copy = dict(self._cfg)
        cfg_copy.setdefault("trial", {})["rover_name"] = name

        self._logger   = TrialLogger(cfg_copy, trial_id=trial_id)
        self._logger.open(cfg_copy)
        self._history  = StateHistory()
        self._detector = EventDetector(cfg_copy)
        self._trial_t0 = time.time()
        self._running.set()

        self._start_btn.config(state="disabled", bg=BORDER, fg=FG_DIM)
        self._stop_btn.config(state="normal", bg=RED, fg="white",
                              activebackground="#7a0000")
        self._name_entry.config(state="disabled")
        self._write_log(f"▶  {name}  —  trial started", "info")
        
    def _stop_trial(self):
        if not self._running.is_set():
            return
        self._running.clear()

        summary = self._logger.close()
        self._logger = None

        self._start_btn.config(state="normal", bg=GREEN, fg="white")
        self._stop_btn.config(state="disabled", bg=BORDER, fg=FG_DIM)
        self._name_entry.config(state="normal")
        self._timer_var.set("00:00.0")

        elapsed = time.time() - self._trial_t0
        self._write_log(
            f"⏹  Done  {elapsed:.1f}s  →  {summary.output_dir.name}", "info"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Manual events
    # ══════════════════════════════════════════════════════════════════════════

    def _elapsed(self) -> float:
        return time.time() - self._trial_t0 if self._running.is_set() else 0.0

    def _last_position(self) -> tuple[float, float]:
        recent = self._history.last(1)
        return (recent[0].x_mm, recent[0].y_mm) if recent else (0.0, 0.0)

    def _log_collision(self, cls: str):
        if not self._running.is_set():
            self._write_log("⚠  Start a trial first", "warn")
            return
        t = self._elapsed()
        x, y = self._last_position()
        self._logger.log_event(DetectedEvent(
            event_type="wall_collision", timestamp_s=t, frame_idx=0,
            x_mm=x, y_mm=y, metadata={"source": "manual", "class": cls},
        ))
        self._write_log(f"{t:6.1f}s  💥 Collision {cls}", "collision")

    def _log_intervention(self):
        if not self._running.is_set():
            self._write_log("⚠  Start a trial first", "warn")
            return
        t = self._elapsed()
        x, y = self._last_position()
        self._logger.log_event(DetectedEvent(
            event_type="manual_intervention", timestamp_s=t, frame_idx=0,
            x_mm=x, y_mm=y, metadata={"source": "manual"},
        ))
        self._write_log(f"{t:6.1f}s  🖐  Manual intervention", "intervention")

    # ══════════════════════════════════════════════════════════════════════════
    # Log widget
    # ══════════════════════════════════════════════════════════════════════════

    def _write_log(self, msg: str, tag: str = ""):
        self._log.config(state="normal")
        self._log.insert("end", msg + "\n", tag)
        self._log.see("end")
        self._log.config(state="disabled")

    # ══════════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ══════════════════════════════════════════════════════════════════════════

    def _on_close(self):
        if self._running.is_set():
            self._stop_trial()
        self._stop_cam.set()
        self._root.destroy()

    def run(self):
        self._root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MIE491 Operator UI")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    if not _PIL:
        print("\n⚠  Pillow not found — install it for the live feed:")
        print("   pip install Pillow\n")

    cfg = _load_config(args.config)
    OperatorApp(cfg).run()


if __name__ == "__main__":
    main()