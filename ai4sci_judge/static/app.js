"use strict";
const $ = id => document.getElementById(id);
const isDisplay = document.body.dataset.view === "display";
const query = new URLSearchParams(location.search);
const challengeKeys = ["1", "2", "3", "4"];
const rankingKeys = [...challengeKeys, "overall"];
const rotationMs = 5000;
const pageSize = 10;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
// The outer region still scrolls horizontally in the narrow Overall view.
// This inner clip contains only the table, not the page header or paging controls.
const tableClip = document.createElement("div");
tableClip.className = "page-table-clip";
const scoreTable = document.querySelector("table");
scoreTable.before(tableClip);
tableClip.append(scoreTable);
let board = null, page = 0, pageCount = 1, lastSuccess = 0;
let fetchingBoard = false, connectionFailed = false;
let lifecycleClock = null;
let autoRotate = query.has("rotate") ? query.get("rotate") === "1" : isDisplay && !reducedMotion.matches;
let nextRotation = Date.now() + rotationMs;
if (rankingKeys.includes(query.get("ranking"))) $("ranking").value = query.get("ranking");

async function loadBrandFonts() {
  // Original WOFF2 bytes travel with this page: no third-party request or
  // font URL is needed. The font payload has its own NVIDIA license (NOTICE).
  document.body.dataset.font = "loading";
  try {
    if (!globalThis.FontFace || !document.fonts) throw new Error("Font loading unavailable");
    const assets = JSON.parse($("brand-fonts").textContent);
    const faces = await Promise.all(assets.map(async ({weight, data}) => {
      const bytes = Uint8Array.from(atob(data), value => value.charCodeAt(0));
      const face = new FontFace("NVIDIA Sans", bytes, {weight: String(weight), style: "normal", display: "swap"});
      await face.load();
      return face;
    }));
    for (const face of faces) document.fonts.add(face);
    await document.fonts.ready;
    document.body.dataset.font = "nvidia";
    drawBoard();
  } catch (_) {
    document.body.dataset.font = "fallback";
    // Font availability must never prevent standings or polling from working.
  }
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(path, {cache: "no-store", ...options, signal: controller.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    return result;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The response timed out. Please check again shortly.");
    throw error;
  } finally { clearTimeout(timeout); }
}
function format(value) { return value === null || value === undefined ? "-" : value.toFixed(2); }
function stale() { return connectionFailed || (lastSuccess > 0 && Date.now() - lastSuccess > 15000); }
function standingsSignature(payload) {
  if (!payload) return "";
  const {server_time, lifecycle, ...standings} = payload;
  return JSON.stringify(standings);
}
function lifecycleRemaining(deadline) {
  if (!Number.isFinite(deadline) || !lifecycleClock || !Number.isFinite(lifecycleClock.serverTime)) return null;
  return Math.max(0, Math.ceil(deadline - lifecycleClock.serverTime - (performance.now() - lifecycleClock.received) / 1000));
}
function lifecycleLabel(item) {
  if (!item) return "Checking status";
  if (item.state === "open") return "Submissions open";
  if (item.state === "closing") return "Closing soon";
  if (item.state === "closed") return item.queued > 0 || item.running > 0 ? "Submissions closed · Grading" : item.system_error > 0 ? "Submissions closed · Reviewing errors" : "Closed · Awaiting finalization";
  if (item.state === "finalized") return "Final results";
  return "Checking status";
}
function updateLifecycle() {
  const strip = $("lifecycle-strip"), stage = $("result-stage");
  // Old board fixtures remain usable without inferring an open or final state.
  if (!strip || !stage) return;
  const key = $("ranking").value, keys = key === "overall" ? challengeKeys : [key];
  const hasLifecycle = board && keys.some(value => board.lifecycle?.[value]);
  strip.hidden = !hasLifecycle;
  if (!hasLifecycle) { strip.replaceChildren(); stage.textContent = "Preview · provisional scoring"; return; }
  const disconnected = stale();
  const values = keys.map(value => {
    const item = board.lifecycle?.[value], state = lifecycleLabel(item);
    let countdown = "";
    if (item?.state === "closing" && !disconnected) {
      const remaining = lifecycleRemaining(item.deadline);
      countdown = remaining === null ? "Server time unavailable" : remaining === 0 ? (key === "overall" ? "Checking status" : "Deadline reached · Checking status") : `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")} remaining`;
    }
    return {key: value, state: disconnected ? "unknown" : item?.state || "unknown", label: disconnected ? `Last known status: ${state} · Reconnecting` : state, countdown};
  });
  // Countdown updates must not recreate the standings or disturb score animation.
  for (const value of values) {
    let card = $("lifecycle-" + value.key);
    if (!card) {
      card = document.createElement("p"); card.id = "lifecycle-" + value.key; card.className = "lifecycle-item";
      for (const name of ["name", "state", "countdown"]) {
        const span = document.createElement("span"); span.className = "lifecycle-" + name; card.append(span);
      }
      strip.append(card);
    }
    card.dataset.state = value.state;
    const texts = [key === "overall" ? `C${value.key}` : `Challenge ${value.key}`, value.label, value.countdown];
    [...card.children].forEach((element, index) => { if (element.textContent !== texts[index]) element.textContent = texts[index]; });
  }
  strip.dataset.view = key === "overall" ? "overall" : "challenge";
  const allFinal = keys.every(value => board.lifecycle?.[value]?.state === "finalized");
  stage.textContent = disconnected ? "Last received results · Status unverified" : allFinal ? "Final results confirmed" : "Provisional results";
}
function clearBoardEffects() {
  $("standings").classList.remove("page-slide-next", "page-slide-previous");
  clearScoreEffects();
}
function clearScoreEffects() {
  for (const cell of $("standings").querySelectorAll(".score-updated")) cell.classList.remove("score-updated");
}
function motionAllowed() { return !reducedMotion.matches && !document.hidden && !$("board-panel").hidden && !stale(); }
function scoreValue(row, key) {
  if (!row) return null;
  if (key !== "total") return row.scores[key] ?? null;
  return challengeKeys.some(value => row.scores[value] !== null && row.scores[value] !== undefined) ? row.total : null;
}
function changedScores(previous, next, key) {
  const changed = new Map();
  if (!previous || (previous.challenge ?? "overall") !== key || stale()) return changed;
  const before = new Map(previous.participants.map(row => [row.nickname, row]));
  const keys = key === "overall" ? [...challengeKeys, "total"] : [key];
  for (const row of next.participants) {
    // A first-seen nickname can also be a rename, not a newly earned score.
    if (!before.has(row.nickname)) continue;
    const fields = keys.filter(field => Number.isFinite(scoreValue(row, field))
      && scoreValue(row, field) !== scoreValue(before.get(row.nickname), field));
    if (fields.length) changed.set(row.nickname, new Set(fields));
  }
  return changed;
}
// No clones, delayed values or cleanup timers: CSS owns the short visual effect.
$("standings").addEventListener("animationend", event => {
  if (["page-slide-next", "page-slide-previous", "score-updated"].includes(event.animationName)) {
    // An old completion event must not remove a rapid restart on the same tbody.
    if (event.target.getAnimations().some(animation => animation.animationName === event.animationName && animation.playState === "running")) return;
    event.target.classList.remove(event.animationName);
  }
});
reducedMotion.addEventListener("change", clearBoardEffects);
function rememberView() {
  const url = new URL(location.href);
  url.search = "";
  url.searchParams.set("ranking", $("ranking").value);
  url.searchParams.set("rotate", autoRotate ? "1" : "0");
  history.replaceState(null, "", url);
}
function renderHeadings(key, rules = {challenge_max: 100, overall_max: 400}) {
  const overall = key === "overall";
  document.body.dataset.ranking = key;
  $("board-eyebrow").textContent = overall ? "Overall leaderboard" : "Challenge leaderboard";
  $("ranking-title").textContent = overall ? "Overall standings" : `Challenge ${key} standings`;
  $("ranking-description").textContent = overall
    ? `Sum of each Challenge's best score · out of ${rules.overall_max}`
    : `Best submission in this Challenge · out of ${rules.challenge_max}`;
  $("ranking-note").textContent = overall
    ? "Ties share a rank. A dash means no score yet; missing Challenges add 0 to the total."
    : "Ties share a rank. A dash means no score yet.";
  const label = overall ? "Overall results and Challenge scores" : `Challenge ${key} scores`;
  document.querySelector("table").setAttribute("aria-label", label);
  document.querySelector(".table-scroll").setAttribute("aria-label", label);
  document.querySelector(".stats").setAttribute("aria-label", overall
    ? "Participation and judging status across all Challenges"
    : `Participation and judging status for Challenge ${key}`);
  const columns = ["Rank", "Participant", ...(overall ? [...challengeKeys.map(value => `C${value}`), "Total"] : [`C${key} score`])];
  const fragment = document.createDocumentFragment();
  columns.forEach((title, index) => {
    const th = document.createElement("th"); th.scope = "col";
    const span = document.createElement("span"); span.textContent = title;
    if (index === columns.length - 1) {
      th.className = "score-active"; span.id = "score-heading";
    }
    th.append(span);
    if (index >= 2) {
      const maximum = document.createElement("small");
      maximum.textContent = `/ ${overall && index === columns.length - 1 ? rules.overall_max : rules.challenge_max}`;
      th.append(maximum);
    }
    fragment.append(th);
  });
  $("score-columns").replaceChildren(fragment);
}
function showLoading() {
  clearBoardEffects();
  tableClip.classList.remove("is-paged");
  const key = $("ranking").value;
  board = null; page = 0; pageCount = 1; lastSuccess = 0; connectionFailed = false; lifecycleClock = null;
  renderHeadings(key);
  $("challenge-title").textContent = key === "overall" ? "Overall results" : `Challenge ${key}`;
  const tr = document.createElement("tr"), td = document.createElement("td");
  td.colSpan = key === "overall" ? 7 : 3; td.className = "empty";
  td.textContent = key === "overall" ? "Waiting for overall results..." : `Waiting for Challenge ${key} results...`;
  tr.append(td); $("standings").replaceChildren(tr);
  for (const status of ["people", "completed", "running", "queued"]) $("stat-" + status).textContent = "-";
  $("page-label").textContent = "Page 1 / 1";
  $("range-label").textContent = "Waiting for results";
  $("previous-page").disabled = true; $("next-page").disabled = true;
  updateConnection();
}
function drawBoard({direction = 0, scoreChanges = null, preservePageMotion = false} = {}) {
  if (preservePageMotion && motionAllowed()) clearScoreEffects();
  else clearBoardEffects();
  const key = $("ranking").value;
  if (!board || (board.challenge ?? "overall") !== key) { clearBoardEffects(); return; }
  const overall = key === "overall";
  renderHeadings(key, board.rules);
  const rank = row => overall ? row.rank : row.challenge_ranks[key];
  const rows = [...board.participants].sort((a, b) => (rank(a) ?? Infinity) - (rank(b) ?? Infinity) || a.nickname.localeCompare(b.nickname, "en"));
  const size = pageSize;
  pageCount = Math.max(1, Math.ceil(rows.length / size));
  const previousPage = page;
  page = Math.min(page, pageCount - 1);
  if (page !== previousPage) clearBoardEffects();
  tableClip.classList.toggle("is-paged", pageCount > 1);
  const start = page * size;
  const fragment = document.createDocumentFragment();
  for (const row of rows.slice(start, start + size)) {
    const tr = document.createElement("tr");
    const position = rank(row);
    if (position === null) tr.classList.add("unranked");
    else if (position <= 3) tr.classList.add("leading");
    const hasScore = challengeKeys.some(value => row.scores[value] !== null && row.scores[value] !== undefined);
    const values = [position ?? "-", row.nickname, ...(overall
      ? [...challengeKeys.map(value => format(row.scores[value])), format(hasScore ? row.total : null)]
      : [format(row.scores[key])])];
    values.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 0) {
        const badge = document.createElement("span"); badge.className = "rank"; badge.textContent = value; td.append(badge);
      } else { td.textContent = value; }
      if (index === 1) td.title = row.nickname;
      if (index === values.length - 1) td.classList.add("score-active");
      else if (index >= 2) td.classList.add("score-secondary");
      if (index >= 2 && scoreChanges && motionAllowed()) {
        const field = overall ? [...challengeKeys, "total"][index - 2] : key;
        if (scoreChanges.get(row.nickname)?.has(field)) td.classList.add("score-updated");
      }
      tr.append(td);
    });
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = document.createElement("tr"), td = document.createElement("td");
    td.colSpan = overall ? 7 : 3; td.className = "empty";
    td.textContent = "Waiting for participants. Register a nickname in your Challenge notebook.";
    tr.append(td); fragment.append(tr);
  }
  $("standings").replaceChildren(fragment);
  if (direction && motionAllowed()) {
    // Commit a cancelled prior animation before a rapid repeated Next/Prev.
    void $("standings").offsetWidth;
    $("standings").classList.add(direction > 0 ? "page-slide-next" : "page-slide-previous");
  }
  $("challenge-title").textContent = overall ? "Overall results" : `Challenge ${key} · ${board.challenges[key].title}`;
  $("stat-people").textContent = rows.length;
  for (const status of ["completed", "running", "queued"]) $("stat-" + status).textContent = board.queue[status] || 0;
  $("page-label").textContent = `Page ${page + 1} / ${pageCount}`;
  $("range-label").textContent = rows.length ? `Participants ${start + 1}–${Math.min(start + size, rows.length)} of ${rows.length}` : "Waiting for registration";
  $("previous-page").disabled = page === 0;
  $("next-page").disabled = page === pageCount - 1;
  updateRotation();
}
function updateConnection() {
  const disconnected = stale();
  const changed = $("stale-warning").hidden === disconnected;
  document.body.dataset.connection = disconnected ? "stale" : lastSuccess ? "live" : "waiting";
  const message = disconnected ? "Connection interrupted" : lastSuccess ? "Live · refreshes every 5s" : "Connecting";
  // Do not repeatedly announce an unchanged live region to screen readers.
  if ($("connection").textContent !== message) $("connection").textContent = message;
  $("stale-warning").hidden = !disconnected;
  $("updated").textContent = lastSuccess ? `Updated ${new Date(lastSuccess).toLocaleTimeString("en-GB", {hour12:false})}` : "Waiting for an update";
  if (changed && board) drawBoard();
  updateLifecycle();
  updateRotation();
}
function updateRotation() {
  $("rotate-pages").setAttribute("aria-pressed", String(autoRotate));
  $("rotate-pages").textContent = autoRotate ? "Auto paging on" : "Auto paging off";
  $("rotation-note").textContent = stale() ? "Paging paused until reconnected" : pageCount === 1 ? "All participants shown" : autoRotate ? `Pages advance every ${rotationMs / 1000} seconds` : "Manual paging";
}
async function refreshBoard() {
  if (fetchingBoard) return;
  fetchingBoard = true;
  const key = $("ranking").value;
  try {
    // Fetch totals only when the instructor explicitly chooses the overall view.
    const next = await api(key === "overall" ? "/api/board" : `/api/board?challenge=${encodeURIComponent(key)}`);
    if (key !== $("ranking").value) return;
    const matchesView = key === "overall"
      ? next.challenge === undefined && challengeKeys.every(value => next.challenges?.[value]) && Number.isFinite(next.rules?.overall_max)
      : next.challenge === key && next.challenges?.[key];
    if (!matchesView || !Array.isArray(next.participants) || !next.rules || !next.queue) throw new Error("Invalid board response");
    const scoreChanges = changedScores(board, next, key);
    const dataChanged = standingsSignature(board) !== standingsSignature(next);
    board = next;
    lifecycleClock = {serverTime: Number.isFinite(next.server_time) ? next.server_time : null, received: performance.now()};
    if (stale()) nextRotation = Date.now() + rotationMs;
    lastSuccess = Date.now(); connectionFailed = false;
    // Identical polling must not restart a pulse or cut off an auto-page slide.
    if (dataChanged) drawBoard({scoreChanges, preservePageMotion: true});
  } catch (_) { if (key === $("ranking").value) connectionFailed = true; }
  finally {
    fetchingBoard = false;
    if (key !== $("ranking").value) refreshBoard();
    else updateConnection();
  }
}
function changePage(delta, wrap = false) {
  const next = wrap ? (page + delta + pageCount) % pageCount : Math.max(0, Math.min(pageCount - 1, page + delta));
  nextRotation = Date.now() + rotationMs;
  if (next === page) return;
  page = next;
  drawBoard({direction: Math.sign(delta)});
}
$("ranking").addEventListener("change", () => { nextRotation = Date.now() + rotationMs; rememberView(); showLoading(); refreshBoard(); });
$("previous-page").addEventListener("click", () => changePage(-1));
$("next-page").addEventListener("click", () => changePage(1));
$("rotate-pages").addEventListener("click", () => { autoRotate = !autoRotate; nextRotation = Date.now() + rotationMs; rememberView(); updateRotation(); });
window.addEventListener("resize", drawBoard);
document.addEventListener("visibilitychange", () => {
  clearBoardEffects();
  if (!document.hidden) { nextRotation = Date.now() + rotationMs; refreshBoard(); }
});

if (isDisplay) {
  // This page requests public board data only. No credentials or personal results.
  $("fullscreen").addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
      else throw new Error("unsupported");
      $("display-notice").textContent = "";
    } catch (_) { $("display-notice").textContent = "Use your browser menu to enter full screen."; }
  });
  document.addEventListener("fullscreenchange", () => { $("fullscreen").textContent = document.fullscreenElement ? "Exit full screen" : "Full screen"; drawBoard(); });
  document.addEventListener("keydown", event => {
    if (event.target.closest("button, select, input, a") || event.altKey || event.ctrlKey || event.metaKey) return;
    const scrollRegion = event.target.closest(".table-scroll");
    if (scrollRegion && scrollRegion.scrollWidth > scrollRegion.clientWidth) return;
    if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); changePage(event.key === "ArrowRight" ? 1 : -1); }
  });
}
showLoading();
refreshBoard();
loadBrandFonts();
setInterval(() => {
  updateConnection();
  if (!autoRotate || !board || stale() || document.hidden || $("board-panel").hidden || pageCount < 2) return;
  if (Date.now() >= nextRotation) changePage(1, true);
}, 1000);
setInterval(async () => {
  if (document.hidden) return;
  await refreshBoard();
}, 5000);
