// Shared helpers for the operator panel and dashboard.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "style") n.style.cssText = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

// ── API ─────────────────────────────────────────────────────────────────────

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ")
      : `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return data;
}

/** Subscribe to live engine state. Reconnects automatically. */
export function subscribe(onState, onConnection = () => {}) {
  let es;
  const open = () => {
    es = new EventSource("/api/stream");
    es.onopen = () => onConnection(true);
    es.onmessage = (e) => onState(JSON.parse(e.data));
    es.onerror = () => {
      onConnection(false);
      es.close();
      setTimeout(open, 1500);
    };
  };
  open();
}

// ── toasts ──────────────────────────────────────────────────────────────────

export function toast(text, level = "info", ms = 4500) {
  let area = $(".toast-area");
  if (!area) document.body.append((area = el("div", { class: "toast-area", "aria-live": "polite" })));
  const t = el("div", { class: `toast ${level}` }, text);
  area.append(t);
  setTimeout(() => t.remove(), ms);
}

// ── units & formatting ──────────────────────────────────────────────────────

const UNIT_KEY = "rt-units";
export const units = {
  get() { try { return localStorage.getItem(UNIT_KEY) || "ft"; } catch { return "ft"; } },
  set(u) { try { localStorage.setItem(UNIT_KEY, u); } catch { /* storage unavailable */ } },
};

export function fmtDist(mm, u = units.get()) {
  if (mm == null) return ["–", ""];
  return u === "m" ? [(mm / 1000).toFixed(2), "m"] : [(mm / 304.8).toFixed(1), "ft"];
}
export function fmtSpeed(mms, u = units.get()) {
  if (mms == null) return ["–", ""];
  return u === "m" ? [(mms / 1000).toFixed(2), "m/s"] : [(mms / 304.8).toFixed(2), "ft/s"];
}
export function fmtTime(s) {
  if (s == null) return "–";
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return `${String(m).padStart(2, "0")}:${r.toFixed(1).padStart(4, "0")}`;
}
export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
export function totalCollisions(c) {
  return c ? Object.values(c).reduce((a, b) => a + b, 0) : 0;
}

export const EVENT_STYLE = {
  "collision-1": { label: "Collision · class 1", color: "--c1" },
  "collision-2": { label: "Collision · class 2", color: "--c2" },
  "collision-3": { label: "Collision · class 3", color: "--c3" },
  intervention: { label: "Intervention", color: "--interv" },
};
export function eventStyle(e) {
  return EVENT_STYLE[e.kind === "collision" ? `collision-${e.severity || 1}` : e.kind]
    || { label: e.kind, color: "--muted" };
}
export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Speed colour ramp (slow -> fast), readable on light and dark floors.
const RAMP = [[59, 130, 246], [16, 185, 129], [234, 179, 8], [239, 68, 68]];
export function speedColor(frac) {
  const f = Math.max(0, Math.min(1, frac)) * (RAMP.length - 1);
  const i = Math.min(RAMP.length - 2, Math.floor(f));
  const k = f - i;
  const c = RAMP[i].map((v, j) => Math.round(v + (RAMP[i + 1][j] - v) * k));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function setupCanvas(canvas, cssHeight) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  canvas.style.height = `${cssHeight}px`;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: cssHeight };
}

// ── maze map ────────────────────────────────────────────────────────────────

export class MazeView {
  constructor(canvas, maze) {
    this.canvas = canvas;
    this.maze = maze;
    this.path = { t: [], x: [], y: [], s: [], id: [] };
    this.events = [];
    this.rover = null; // {x, y, heading}
    this.cursorT = null; // replay: draw path up to this time
    this.speedMax = 500;
    new ResizeObserver(() => this.draw()).observe(canvas);
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.draw());
  }

  setPath(traj) {
    this.path = {
      t: traj.t || [], x: traj.x_mm || [], y: traj.y_mm || [], s: traj.speed_mms || [],
      id: traj.track_id || [],
    };
    this.speedMax = Math.max(500, ...this.path.s.slice(-5000));
  }

  appendPoint(t, x, y, s, id) {
    const p = this.path;
    if (p.t.length && t <= p.t[p.t.length - 1]) return;
    p.t.push(t); p.x.push(x); p.y.push(y); p.s.push(s); p.id.push(id);
    if (s > this.speedMax) this.speedMax = s;
  }

  draw() {
    const { length_mm: L, width_mm: W, walls = [] } = this.maze;
    const pad = 14;
    const cw = this.canvas.clientWidth;
    if (!cw) return;
    const scale = (cw - 2 * pad) / L;
    const { ctx } = setupCanvas(this.canvas, Math.round(W * scale + 2 * pad));
    const X = (x) => pad + x * scale;
    const Y = (y) => pad + y * scale;

    ctx.fillStyle = cssVar("--floor");
    ctx.fillRect(X(0), Y(0), L * scale, W * scale);
    ctx.strokeStyle = cssVar("--grid");
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = 304.8; x < L - 1; x += 304.8) { ctx.moveTo(X(x), Y(0)); ctx.lineTo(X(x), Y(W)); }
    for (let y = 304.8; y < W - 1; y += 304.8) { ctx.moveTo(X(0), Y(y)); ctx.lineTo(X(L), Y(y)); }
    ctx.stroke();
    ctx.fillStyle = cssVar("--wall");
    for (const [x0, y0, x1, y1] of walls) ctx.fillRect(X(x0), Y(y0), (x1 - x0) * scale, (y1 - y0) * scale);
    ctx.strokeStyle = cssVar("--wall");
    ctx.lineWidth = 3;
    ctx.strokeRect(X(0), Y(0), L * scale, W * scale);

    // path, coloured by speed; never joins separate track segments
    const p = this.path;
    const end = this.cursorT == null ? p.t.length : upperBound(p.t, this.cursorT);
    ctx.lineWidth = Math.max(2.5, scale * 14);
    ctx.lineCap = "round";
    for (let i = 1; i < end; i++) {
      if (p.id[i] !== p.id[i - 1]) continue;
      ctx.strokeStyle = speedColor(p.s[i] / this.speedMax);
      ctx.beginPath();
      ctx.moveTo(X(p.x[i - 1]), Y(p.y[i - 1]));
      ctx.lineTo(X(p.x[i]), Y(p.y[i]));
      ctx.stroke();
    }

    for (const e of this.events) {
      if (e.x_mm == null) continue;
      if (this.cursorT != null && e.t > this.cursorT) continue;
      const st = eventStyle(e);
      ctx.fillStyle = cssVar(st.color);
      ctx.strokeStyle = cssVar("--panel");
      ctx.lineWidth = 2;
      ctx.beginPath();
      const r = Math.max(6, scale * 40);
      if (e.kind === "collision") {
        ctx.moveTo(X(e.x_mm), Y(e.y_mm) - r);
        ctx.lineTo(X(e.x_mm) + r, Y(e.y_mm));
        ctx.lineTo(X(e.x_mm), Y(e.y_mm) + r);
        ctx.lineTo(X(e.x_mm) - r, Y(e.y_mm));
        ctx.closePath();
      } else {
        ctx.arc(X(e.x_mm), Y(e.y_mm), r * 0.8, 0, Math.PI * 2);
      }
      ctx.fill();
      ctx.stroke();
    }

    let rover = this.rover;
    if (this.cursorT != null && end > 0) {
      const i = end - 1;
      rover = { x: p.x[i], y: p.y[i], heading: null };
    }
    if (rover) {
      const r = Math.max(6, scale * 45);
      ctx.fillStyle = cssVar("--accent");
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.arc(X(rover.x), Y(rover.y), r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      if (rover.heading != null) {
        const a = (rover.heading * Math.PI) / 180;
        ctx.beginPath();
        ctx.moveTo(X(rover.x), Y(rover.y));
        ctx.lineTo(X(rover.x) + Math.cos(a) * r * 1.6, Y(rover.y) - Math.sin(a) * r * 1.6);
        ctx.stroke();
      }
    }
  }
}

export function upperBound(arr, v) {
  let lo = 0;
  let hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] <= v) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// ── speed chart ─────────────────────────────────────────────────────────────

export class SpeedChart {
  constructor(canvas, height = 170) {
    this.canvas = canvas;
    this.height = height;
    this.t = [];
    this.s = [];
    this.events = [];
    this.cursorT = null;
    this.duration = null;
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  set(t, s, events = [], duration = null) {
    this.t = t; this.s = s; this.events = events; this.duration = duration;
  }

  draw() {
    if (!this.canvas.clientWidth) return;
    const { ctx, w, h } = setupCanvas(this.canvas, this.height);
    const u = units.get();
    const k = u === "m" ? 1 / 1000 : 1 / 304.8;
    const padL = 40, padR = 10, padT = 10, padB = 24;
    const tMax = Math.max(this.duration || 0, this.t[this.t.length - 1] || 0, 10);
    // smooth for display (0.5 s moving average)
    const sm = movingAvg(this.t, this.s, 0.5);
    const yMax = niceMax(Math.max(0.5 / k, ...sm) * k);
    const X = (t) => padL + (t / tMax) * (w - padL - padR);
    const Y = (v) => h - padB - (v / yMax) * (h - padT - padB);

    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = cssVar("--muted");
    ctx.strokeStyle = cssVar("--border");
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = (yMax * i) / 4;
      ctx.beginPath(); ctx.moveTo(padL, Y(v)); ctx.lineTo(w - padR, Y(v)); ctx.stroke();
      ctx.fillText(v.toFixed(1), 4, Y(v) + 4);
    }
    const step = niceStep(tMax / 6);
    for (let t = 0; t <= tMax + 1e-6; t += step) ctx.fillText(`${Math.round(t)}s`, X(t) - 8, h - 6);
    ctx.fillText(u === "m" ? "m/s" : "ft/s", padL + 4, padT + 10);

    for (const e of this.events) {
      ctx.strokeStyle = cssVar(eventStyle(e).color);
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(X(e.t), padT); ctx.lineTo(X(e.t), h - padB); ctx.stroke();
    }

    ctx.strokeStyle = cssVar("--accent");
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < this.t.length; i++) {
      const x = X(this.t[i]);
      const y = Y(sm[i] * k);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();

    if (this.cursorT != null) {
      ctx.strokeStyle = cssVar("--text");
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(X(this.cursorT), padT); ctx.lineTo(X(this.cursorT), h - padB); ctx.stroke();
    }
  }
}

function movingAvg(t, s, win) {
  const out = new Array(s.length);
  let j = 0;
  let sum = 0;
  for (let i = 0; i < s.length; i++) {
    sum += s[i];
    while (t[i] - t[j] > win) sum -= s[j++];
    out[i] = sum / (i - j + 1);
  }
  return out;
}
function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
function niceStep(v) {
  for (const s of [1, 2, 5, 10, 15, 20, 30, 60, 120, 300]) if (s >= v) return s;
  return 600;
}
