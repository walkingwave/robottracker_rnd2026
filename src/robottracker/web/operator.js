import {
  $, $$, el, api, subscribe, toast, units, fmtDist, fmtSpeed, fmtTime, fmtDate,
  totalCollisions, eventStyle,
} from "./common.js";

const CORNERS = ["top-left", "top-right", "bottom-right", "bottom-left"];
let S = null; // latest engine state
let mode = "none"; // none | calibrate | relock
let calPts = [];
let lastMsgId = null;
let lastTrialKey = "";
let srcKind = "camera";

// ── feed ────────────────────────────────────────────────────────────────────

const feed = $("#feed");
function startFeed() {
  feed.src = `/video.mjpg?ts=${Date.now()}`;
}
feed.addEventListener("error", () => setTimeout(startFeed, 2000));
startFeed();

$("#feed-wrap").addEventListener("click", async (e) => {
  if (mode === "none") return;
  const r = feed.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width;
  const y = (e.clientY - r.top) / r.height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return;
  if (mode === "calibrate") {
    calPts.push([x, y]);
    if (calPts.length === 4) {
      try {
        await api("/api/calibration", { method: "POST", body: { corners: calPts } });
        setMode("none");
      } catch (err) {
        toast(err.message, "err", 7000);
        calPts = [];
      }
    }
    drawCalibration();
  } else if (mode === "relock") {
    try {
      await api("/api/reacquire", { method: "POST", body: { x, y, view: S?.view } });
      setMode("none");
    } catch (err) { toast(err.message, "err"); }
  }
});

function drawCalibration() {
  const svg = $("#cal-svg");
  svg.innerHTML = "";
  if (mode !== "calibrate") return;
  const NS = "http://www.w3.org/2000/svg";
  const ratio = feed.clientHeight / Math.max(1, feed.clientWidth);
  if (calPts.length > 1) {
    const poly = document.createElementNS(NS, "polyline");
    poly.setAttribute("points", calPts.map((p) => p.join(",")).join(" "));
    poly.setAttribute("fill", "none");
    poly.setAttribute("stroke", "#facc15");
    poly.setAttribute("stroke-width", "0.003");
    svg.append(poly);
  }
  calPts.forEach(([x, y]) => {
    const c = document.createElementNS(NS, "ellipse");
    c.setAttribute("cx", x); c.setAttribute("cy", y);
    c.setAttribute("rx", 0.008); c.setAttribute("ry", 0.008 / ratio);
    c.setAttribute("fill", "#facc15"); c.setAttribute("stroke", "#000"); c.setAttribute("stroke-width", "0.002");
    svg.append(c);
  });
  updateBanner();
}

function updateBanner() {
  const b = $("#feed-banner");
  b.innerHTML = "";
  if (mode === "calibrate") {
    const n = calPts.length;
    b.append(
      el("span", {}, `Click corner ${n + 1} of 4: `, el("b", {}, CORNERS[n] || "")),
      n ? el("button", { class: "btn", onclick: (e) => { e.stopPropagation(); calPts.pop(); drawCalibration(); } }, "Undo") : null,
      el("button", { class: "btn", onclick: (e) => { e.stopPropagation(); setMode("none"); } }, "Cancel"),
    );
  } else if (mode === "relock") {
    b.append(
      el("span", {}, "Click the rover to lock on"),
      el("button", { class: "btn", onclick: (e) => { e.stopPropagation(); setMode("none"); } }, "Cancel"),
    );
  }
  b.classList.toggle("hidden", mode === "none");
  $("#feed-wrap").classList.toggle("clickable", mode !== "none");
  $("#btn-cal").classList.toggle("active", mode === "calibrate");
  $("#btn-relock").classList.toggle("active", mode === "relock");
}

async function setMode(m) {
  if (m === "calibrate") {
    if (S?.trial) return toast("Stop the trial before recalibrating.", "warn");
    if (!S?.source) return toast("Connect a video source first.", "warn");
    if (S.view !== "camera") await api("/api/view", { method: "POST", body: { view: "camera" } });
  }
  if (m === "relock" && !S?.calibrated) return toast("Calibrate first.", "warn");
  mode = m;
  calPts = [];
  drawCalibration();
  updateBanner();
}

$("#btn-cal").onclick = () => setMode(mode === "calibrate" ? "none" : "calibrate");
$("#btn-relock").onclick = () => setMode(mode === "relock" ? "none" : "relock");
$("#btn-bg").onclick = () => api("/api/background", { method: "POST" }).catch((e) => toast(e.message, "err"));
$$("#views button").forEach((b) => {
  b.onclick = () => api("/api/view", { method: "POST", body: { view: b.dataset.view } }).catch((e) => toast(e.message, "err"));
});

// ── source ──────────────────────────────────────────────────────────────────

function setSrcKind(k) {
  srcKind = k;
  $$("#src-kind button").forEach((b) => b.classList.toggle("on", b.dataset.k === k));
  $("#src-camera").classList.toggle("hidden", k !== "camera");
  $("#src-file").classList.toggle("hidden", k !== "file");
}
$$("#src-kind button").forEach((b) => { b.onclick = () => setSrcKind(b.dataset.k); });
setSrcKind("camera");

$("#btn-scan").onclick = async () => {
  const btn = $("#btn-scan");
  btn.disabled = true; btn.textContent = "Scanning…";
  try {
    const cams = await api("/api/cameras");
    const sel = $("#cam-select");
    sel.innerHTML = "";
    if (!cams.length) toast("No cameras found. Check the USB cable and camera permissions.", "warn");
    for (const c of cams) sel.append(el("option", { value: c.index, disabled: !c.ok }, c.label));
  } catch (e) { toast(e.message, "err"); }
  btn.disabled = false; btn.textContent = "Scan";
};

$("#btn-connect").onclick = async () => {
  const btn = $("#btn-connect");
  btn.disabled = true; btn.textContent = "Connecting…";
  try {
    const body = srcKind === "camera"
      ? { kind: "camera", index: Number($("#cam-select").value) }
      : { kind: "file", file: $("#file-input").value.trim() };
    await api("/api/source", { method: "POST", body });
  } catch (e) { toast(e.message, "err", 7000); }
  btn.disabled = false; btn.textContent = "Connect";
};

api("/api/videos").then((files) => {
  $("#file-list").replaceChildren(...files.map((f) => el("option", { value: f })));
}).catch(() => {});

// ── trial ───────────────────────────────────────────────────────────────────

async function startTrial() {
  const team = $("#team").value.trim();
  if (!team) { $("#team").focus(); return toast("Enter a team name first.", "warn"); }
  try { await api("/api/trial/start", { method: "POST", body: { team } }); }
  catch (e) { toast(e.message, "err"); }
}
async function stopTrial(outcome) {
  try { await api("/api/trial/stop", { method: "POST", body: { outcome } }); }
  catch (e) { toast(e.message, "err"); }
}
async function logEvent(kind, severity = null) {
  try { await api("/api/trial/events", { method: "POST", body: { kind, severity } }); }
  catch (e) { toast(e.message, "warn"); }
}
async function undo(id = null) {
  try { await api(id == null ? "/api/trial/events/last" : `/api/trial/events/${id}`, { method: "DELETE" }); }
  catch (e) { toast(e.message, "warn"); }
}

$("#btn-start").onclick = startTrial;
$("#btn-finish").onclick = () => stopTrial("finished");
$("#btn-dnf").onclick = () => stopTrial("dnf");
$("#btn-stop-none").onclick = () => stopTrial(null);
$("#btn-undo").onclick = () => undo();
$$("[data-ev]").forEach((b) => {
  b.onclick = () => logEvent(b.dataset.ev, b.dataset.sev ? Number(b.dataset.sev) : null);
});

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
    if (e.key === "Enter" && document.activeElement.id === "team" && !S?.trial) startTrial();
    if (e.key === "Escape") document.activeElement.blur();
    return;
  }
  const running = !!S?.trial;
  const k = e.key.toLowerCase();
  const actions = {
    " ": () => !running && startTrial(),
    f: () => running && stopTrial("finished"),
    d: () => running && stopTrial("dnf"),
    1: () => logEvent("collision", 1),
    2: () => logEvent("collision", 2),
    3: () => logEvent("collision", 3),
    i: () => logEvent("intervention"),
    z: () => undo(),
    r: () => setMode(mode === "relock" ? "none" : "relock"),
    escape: () => setMode("none"),
  };
  if (actions[k]) { e.preventDefault(); actions[k](); }
});

// ── trials table ────────────────────────────────────────────────────────────

async function loadTrials() {
  let trials;
  try { trials = await api("/api/trials"); } catch { return; }
  const tbody = $("#trials");
  tbody.replaceChildren();
  $("#trials-empty").classList.toggle("hidden", trials.length > 0);
  const teams = [...new Set(trials.map((t) => t.team))];
  $("#team-list").replaceChildren(...teams.map((t) => el("option", { value: t })));
  for (const t of trials.slice(0, 30)) {
    const s = t.summary || {};
    const running = t.status === "running";
    const result = el("select", { disabled: running, "aria-label": "Result" },
      el("option", { value: "none" }, "—"),
      el("option", { value: "finished", selected: t.outcome === "finished" }, "Finished"),
      el("option", { value: "dnf", selected: t.outcome === "dnf" }, "DNF"));
    result.onchange = () => patchTrial(t.id, { outcome: result.value });
    const team = el("input", { value: t.team, disabled: running, "aria-label": "Team" });
    team.onchange = () => patchTrial(t.id, { team: team.value });
    const [d, du] = fmtDist(s.distance_mm);
    tbody.append(el("tr", {},
      el("td", {}, team),
      el("td", { class: "muted small" }, fmtDate(t.started_at)),
      el("td", { class: "num mono" }, running ? "live" : fmtTime(s.duration_s ?? t.duration_s)),
      el("td", { class: "num" }, s.distance_mm != null ? `${d} ${du}` : "–"),
      el("td", { class: "num" }, totalCollisions(s.collisions)),
      el("td", { class: "num" }, s.interventions ?? 0),
      el("td", { class: "num" }, s.tracked_fraction != null ? `${Math.round(s.tracked_fraction * 100)}%` : "–"),
      el("td", {}, result),
      el("td", {}, running ? "" : el("button", {
        class: "btn small", title: "Move to trials/_discarded",
        onclick: async () => {
          if (!confirm(`Discard the trial for ${t.team}? It is moved to trials/_discarded.`)) return;
          try { await api(`/api/trials/${t.id}`, { method: "DELETE" }); loadTrials(); }
          catch (e) { toast(e.message, "err"); }
        },
      }, "Discard")),
    ));
  }
}
async function patchTrial(id, body) {
  try { await api(`/api/trials/${id}`, { method: "PATCH", body }); toast("Saved", "info", 1500); }
  catch (e) { toast(e.message, "err"); }
  loadTrials();
}
loadTrials();

// ── render state ────────────────────────────────────────────────────────────

function pill(id, cls, text) {
  const p = $(id);
  p.className = `pill ${cls}`;
  p.lastElementChild.textContent = text;
}
function step(id, done) { $(id).classList.toggle("done", !!done); }

let srcSynced = false;

function render() {
  const src = S.source;
  if (src && !srcSynced) {
    srcSynced = true;
    setSrcKind(src.kind);
  }
  pill("#pill-source", src ? (src.ended ? "warn" : "ok") : "bad",
    src ? `${src.label}${src.paused ? " · paused" : ` · ${src.fps} fps`}` : "No source");
  const ts = S.tracker.state;
  const trackCls = { tracking: "ok", coasting: "warn", searching: "bad" }[ts] || "";
  const trackText = !S.calibrated ? "Not calibrated" : { tracking: "Tracking", coasting: "Coasting (lost briefly)", searching: "Searching for rover" }[ts] || ts;
  pill("#pill-track", trackCls, trackText);

  step("#step-source", src);
  step("#step-cal", S.calibrated);
  step("#step-bg", S.has_background || S.mode === "aruco");
  $("#step-bg").classList.toggle("hidden", S.mode === "aruco");
  step("#step-lock", ts === "tracking");
  $("#btn-bg").classList.toggle("hidden", S.mode === "aruco");
  $("#btn-bg").disabled = !S.calibrated || !!S.trial || S.capturing_background;
  $("#btn-bg").textContent = S.capturing_background ? "Capturing…" : "Capture background";
  $$("#views button").forEach((b) => {
    b.classList.toggle("on", b.dataset.view === S.view);
    b.disabled = !S.calibrated && b.dataset.view !== "camera";
  });
  if (S.mode === "aruco") $('#views [data-view="mask"]').classList.add("hidden");

  const tr = S.trial;
  $("#idle-controls").classList.toggle("hidden", !!tr);
  $("#run-controls").classList.toggle("hidden", !tr);
  $("#btn-start").disabled = !src || !S.calibrated;
  $("#team").disabled = !!tr;
  if (tr && document.activeElement !== $("#team")) $("#team").value = tr.team;
  $("#timer").textContent = fmtTime(tr ? tr.t : 0);
  $("#timer").classList.toggle("rec", !!tr);
  $$("[data-ev], #btn-undo").forEach((b) => { b.disabled = !tr; });

  const live = tr?.live;
  const [d, du] = fmtDist(live?.distance_mm);
  const [sp, su] = fmtSpeed(live ? live.current_speed_mms : null);
  $("#ms-dist").textContent = live ? `${d} ${du}` : "–";
  $("#ms-speed").textContent = live ? `${sp} ${su}` : "–";
  $("#ms-coll").textContent = live ? totalCollisions(live.collisions) : "0";
  $("#ms-int").textContent = live ? live.interventions : "0";
  $("#ms-speed").title = su;

  const list = $("#events");
  const events = tr ? [...tr.events].reverse() : [];
  list.replaceChildren(...events.map((e) => {
    const st = eventStyle(e);
    return el("li", {},
      el("span", { class: "t" }, `${e.t.toFixed(1)}s`),
      el("span", { class: "swatch", style: `background:var(${st.color})` }),
      el("span", {}, st.label),
      el("button", { class: "x", title: "Remove", "aria-label": "Remove event", onclick: () => undo(e.id) }, "×"));
  }));

  // notifications
  const ids = S.messages.map((m) => m.id);
  if (lastMsgId === null) lastMsgId = ids.length ? Math.max(...ids) : 0;
  for (const m of S.messages) if (m.id > lastMsgId) toast(m.text, m.level);
  if (ids.length) lastMsgId = Math.max(lastMsgId, ...ids);

  const key = `${tr?.id || ""}|${S.last_trial_id || ""}`;
  if (key !== lastTrialKey) { lastTrialKey = key; loadTrials(); }
}

subscribe((st) => { S = st; render(); }, (ok) => {
  pill("#pill-conn", ok ? "ok" : "bad", ok ? "Server connected" : "Server offline – retrying");
  if (ok && !feed.complete) startFeed();
});

// units
function renderUnits() {
  $$("#units button").forEach((b) => b.classList.toggle("on", b.dataset.u === units.get()));
  if (S) render();
  loadTrials();
}
$$("#units button").forEach((b) => { b.onclick = () => { units.set(b.dataset.u); renderUnits(); }; });
renderUnits();
new ResizeObserver(drawCalibration).observe(feed);
