import {
  $, $$, el, api, subscribe, units, fmtDist, fmtSpeed, fmtTime, fmtDate, totalCollisions,
  eventStyle, MazeView, SpeedChart,
} from "./common.js";

let CFG = null;
let S = null;
let map, chart;
let follow = true; // follow the live trial (or the latest run when idle)
let shown = null; // { id, live, info, traj }
let lastFinishedKey = null;
let playing = false;

const DEFAULT_MAZE = { length_mm: 2438.4, width_mm: 1219.2, walls: [] };

// ── tabs ────────────────────────────────────────────────────────────────────

function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  for (const t of ["live", "board", "runs"]) $(`#tab-${t}`).classList.toggle("hidden", t !== name);
  if (name === "board") loadBoard();
  if (name === "runs") loadRuns();
  if (name === "live") { map.draw(); chart.draw(); }
}
$$("#tabs button").forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });

// ── showing a run ───────────────────────────────────────────────────────────

async function showTrial(id, live) {
  let data;
  try { data = await api(`/api/trials/${id}`); } catch { return; }
  shown = { id, live, info: data.info, traj: data.trajectory };
  map.setPath(data.trajectory);
  map.events = data.info.events || [];
  map.rover = null;
  map.cursorT = null;
  stopPlay();
  $("#scrub").value = 1000;
  renderRun();
}

function renderRun() {
  $("#waiting").classList.toggle("hidden", !!shown);
  $("#run-view").classList.toggle("hidden", !shown);
  if (!shown) return;
  const live = shown.live && S?.trial?.id === shown.id;
  const info = shown.info;
  const liveStats = live ? S.trial.live : null;
  const s = liveStats || info.summary || {};
  const events = live ? S.trial.events : info.events || [];

  $("#run-team").textContent = live ? S.trial.team : info.team;
  const outcome = { finished: "Finished", dnf: "Did not finish" }[info.outcome]
    || (info.status === "interrupted" ? "Interrupted" : "");
  $("#run-sub").textContent = live ? "Run in progress" : [fmtDate(info.started_at), outcome].filter(Boolean).join(" · ");
  $("#pill-live").className = live ? "pill live" : "pill hidden";

  $("#st-time").textContent = fmtTime(live ? S.trial.t : s.duration_s);
  const [d, du] = fmtDist(s.distance_mm);
  $("#st-dist").textContent = d; $("#st-dist-u").textContent = du;
  const [sp, su] = fmtSpeed(live ? s.current_speed_mms : s.avg_speed_mms);
  $("#st-speed-label").textContent = live ? "Speed" : "Avg speed";
  $("#st-speed").textContent = sp; $("#st-speed-u").textContent = su;
  const [tp, tu] = fmtSpeed(s.top_speed_mms);
  $("#st-top").textContent = tp; $("#st-top-u").textContent = tu;
  $("#st-coll").textContent = totalCollisions(s.collisions);
  $("#st-int").textContent = s.interventions ?? 0;

  $("#events").replaceChildren(...[...events].reverse().map((e) => {
    const st = eventStyle(e);
    return el("li", {}, el("span", { class: "t" }, `${e.t.toFixed(1)}s`),
      el("span", { class: "swatch", style: `background:var(${st.color})` }), el("span", {}, st.label));
  }));
  $("#events-empty").classList.toggle("hidden", events.length > 0);

  $("#replay-bar").classList.toggle("hidden", live || !shown.traj?.t?.length);
  $("#back-live").classList.toggle("hidden", follow);
  const csv = $("#run-csv");
  csv.classList.toggle("hidden", live);
  csv.href = `/api/trials/${shown.id}/trajectory.csv`;

  map.events = events;
  const p = map.path;
  chart.set(p.t, p.s, events, live ? S.trial.t : s.duration_s);
  map.draw();
  chart.cursorT = map.cursorT;
  chart.draw();
}

// ── replay ──────────────────────────────────────────────────────────────────

function setCursor(frac) {
  const t = map.path.t;
  if (!t.length) return;
  const end = shown?.info?.duration_s || t[t.length - 1];
  map.cursorT = frac >= 1 ? null : frac * end;
  chart.cursorT = map.cursorT;
  $("#scrub-t").textContent = fmtTime(frac * end);
  map.draw();
  chart.draw();
}
$("#scrub").oninput = (e) => { stopPlay(); setCursor(e.target.value / 1000); };

function stopPlay() {
  playing = false;
  $("#play").textContent = "▶ Replay";
}
$("#play").onclick = () => {
  if (playing) return stopPlay();
  const t = map.path.t;
  if (!t.length) return;
  const end = shown.info.duration_s || t[t.length - 1];
  let frac = Number($("#scrub").value) / 1000;
  if (frac >= 1) frac = 0;
  playing = true;
  $("#play").textContent = "❚❚ Pause";
  let last = performance.now();
  const speedup = Math.max(1, end / 30); // a full replay takes at most ~30 s
  const tick = (now) => {
    if (!playing) return;
    frac += ((now - last) / 1000) * speedup / end;
    last = now;
    if (frac >= 1) { frac = 1; stopPlay(); }
    $("#scrub").value = Math.round(frac * 1000);
    setCursor(frac);
    if (playing) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
};

$("#back-live").onclick = () => { follow = true; shown = null; syncFollow(true); };

// ── live updates ────────────────────────────────────────────────────────────

async function syncFollow(force = false) {
  if (!follow || !S) return;
  if (S.trial) {
    if (force || shown?.id !== S.trial.id || !shown.live) await showTrial(S.trial.id, true);
    return;
  }
  const latest = S.last_trial_id;
  if (latest && (force || shown?.id !== latest || shown.live)) {
    await showTrial(latest, false);
  } else if (!latest && !shown) {
    const trials = await api("/api/trials").catch(() => []);
    const done = trials.find((t) => t.status !== "running");
    if (done) await showTrial(done.id, false); else renderRun();
  }
}

function onState(st) {
  const first = S === null;
  const prevTrial = S?.trial?.id;
  S = st;
  if (first || prevTrial !== S.trial?.id) syncFollow();
  if (S.trial && shown?.live && shown.id === S.trial.id) {
    const p = S.tracker.point;
    if (p) {
      map.appendPoint(S.trial.t, p.x_mm, p.y_mm, p.speed_mms, p.track_id);
      map.rover = { x: p.x_mm, y: p.y_mm, heading: p.heading_deg };
    } else {
      map.rover = null;
    }
    renderRun();
  }
  const key = S.last_trial_id;
  if (key !== lastFinishedKey) {
    lastFinishedKey = key;
    loadMiniBoard();
    if (!$("#tab-board").classList.contains("hidden")) loadBoard();
    if (!$("#tab-runs").classList.contains("hidden")) loadRuns();
  }
}

// ── leaderboard & runs ──────────────────────────────────────────────────────

function outcomeTag(o, status) {
  if (o === "finished") return el("span", { class: "tag finished" }, "Finished");
  if (o === "dnf") return el("span", { class: "tag dnf" }, "DNF");
  if (status === "running") return el("span", { class: "tag" }, "Live");
  return el("span", { class: "tag" }, "—");
}

function openRun(id) {
  follow = false;
  showTab("live");
  showTrial(id, false);
}

async function loadBoard() {
  const best = $("#best-only").checked;
  $("#board-csv").href = `/api/leaderboard.csv?best_per_team=${best}`;
  const rows = await api(`/api/leaderboard?best_per_team=${best}`).catch(() => []);
  $("#board").replaceChildren(...rows.map((r) => {
    const [d, du] = fmtDist(r.distance_mm);
    const [tp, tu] = fmtSpeed(r.top_speed_mms);
    return el("tr", { class: "clickable", onclick: () => openRun(r.id) },
      el("td", { class: `rank r${r.rank}` }, r.rank),
      el("td", {}, el("b", {}, r.team)),
      el("td", {}, outcomeTag(r.outcome, r.status)),
      el("td", { class: "num mono" }, fmtTime(r.duration_s)),
      el("td", { class: "num" }, r.penalty_s ? `+${r.penalty_s.toFixed(0)}s` : "–"),
      el("td", { class: "num mono" }, r.adjusted_s != null ? fmtTime(r.adjusted_s) : "–"),
      el("td", { class: "num" }, `${d} ${du}`),
      el("td", { class: "num" }, `${tp} ${tu}`),
      el("td", { class: "num" }, totalCollisions(r.collisions)),
      el("td", { class: "num" }, r.interventions));
  }));
  if (CFG) {
    const c = CFG.scoring.collision_penalty_s;
    $("#scoring-note").textContent = `Score = run time + penalties (collision class 1/2/3: +${c[1] ?? 0}/${c[2] ?? 0}/${c[3] ?? 0} s, `
      + `intervention: +${CFG.scoring.intervention_penalty_s} s). Finished runs rank first, then DNFs by distance.`;
  }
}
$("#best-only").onchange = loadBoard;

async function loadMiniBoard() {
  const rows = await api("/api/leaderboard?best_per_team=true").catch(() => []);
  $("#mini-board-card").classList.toggle("hidden", !rows.length);
  $("#mini-board").replaceChildren(...rows.slice(0, 5).map((r) => el("tr", { class: "clickable", onclick: () => openRun(r.id) },
    el("td", { class: `rank r${r.rank}` }, r.rank),
    el("td", {}, r.team),
    el("td", { class: "num mono" }, r.adjusted_s != null ? fmtTime(r.adjusted_s) : fmtDist(r.distance_mm).join(" ")))));
}

async function loadRuns() {
  const trials = await api("/api/trials").catch(() => []);
  $("#runs").replaceChildren(...trials.map((t) => {
    const s = t.summary || {};
    const [d, du] = fmtDist(s.distance_mm);
    const [sp, su] = fmtSpeed(s.avg_speed_mms);
    return el("tr", { class: "clickable", onclick: () => openRun(t.id) },
      el("td", {}, el("b", {}, t.team)),
      el("td", { class: "muted" }, fmtDate(t.started_at)),
      el("td", {}, outcomeTag(t.outcome, t.status)),
      el("td", { class: "num mono" }, t.status === "running" ? "live" : fmtTime(s.duration_s)),
      el("td", { class: "num" }, s.distance_mm != null ? `${d} ${du}` : "–"),
      el("td", { class: "num" }, s.avg_speed_mms != null ? `${sp} ${su}` : "–"),
      el("td", { class: "num" }, totalCollisions(s.collisions)),
      el("td", { class: "num" }, s.interventions ?? "–"));
  }));
}

// ── units ───────────────────────────────────────────────────────────────────

function renderUnits() {
  $$("#units button").forEach((b) => b.classList.toggle("on", b.dataset.u === units.get()));
  renderRun();
  loadMiniBoard();
  if (!$("#tab-board").classList.contains("hidden")) loadBoard();
  if (!$("#tab-runs").classList.contains("hidden")) loadRuns();
}
$$("#units button").forEach((b) => { b.onclick = () => { units.set(b.dataset.u); renderUnits(); }; });

// ── boot ────────────────────────────────────────────────────────────────────

(async () => {
  CFG = await api("/api/config").catch(() => null);
  map = new MazeView($("#map"), CFG?.maze || DEFAULT_MAZE);
  chart = new SpeedChart($("#chart"));
  if (CFG?.public_video) {
    $("#video-card").classList.remove("hidden");
    $("#video").src = "/video.mjpg";
  }
  $$("#units button").forEach((b) => b.classList.toggle("on", b.dataset.u === units.get()));
  subscribe(onState);
})();
