"use strict";
const $ = id => document.getElementById(id);
const isDisplay = document.body.dataset.view === "display";
const query = new URLSearchParams(location.search);
const rankingKeys = ["overall", "1", "2", "3", "4"];
const rotationMs = 15000;
let board = null, page = 0, pageCount = 1, lastSuccess = 0;
let fetchingBoard = false, connectionFailed = false, mineRequest = 0;
let autoRotate = query.has("rotate") ? query.get("rotate") === "1" : isDisplay && !matchMedia("(prefers-reduced-motion: reduce)").matches;
let nextRotation = Date.now() + rotationMs;
if (rankingKeys.includes(query.get("ranking"))) $("ranking").value = query.get("ranking");

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
function rowsPerPage() {
  if (!isDisplay || window.innerWidth < 900) return 10;
  const top = document.querySelector("thead").getBoundingClientRect().bottom;
  const footer = document.querySelector(".board-footer").offsetHeight + document.querySelector(".footnote").offsetHeight + 50;
  const height = parseFloat(getComputedStyle(document.body).getPropertyValue("--row-height"));
  return Math.max(3, Math.min(10, Math.floor((window.innerHeight - top - footer) / height)));
}
function rememberView() {
  const url = new URL(location.href);
  url.search = "";
  url.searchParams.set("ranking", $("ranking").value);
  url.searchParams.set("rotate", autoRotate ? "1" : "0");
  history.replaceState(null, "", url);
}
function drawBoard() {
  if (!board) return;
  const key = $("ranking").value;
  const rank = row => key === "overall" ? row.rank : row.challenge_ranks[key];
  const rows = [...board.participants].sort((a, b) => (rank(a) ?? Infinity) - (rank(b) ?? Infinity) || a.nickname.localeCompare(b.nickname, "en"));
  const size = rowsPerPage();
  pageCount = Math.max(1, Math.ceil(rows.length / size));
  page = Math.min(page, pageCount - 1);
  const start = page * size;
  const fragment = document.createDocumentFragment();
  for (const row of rows.slice(start, start + size)) {
    const tr = document.createElement("tr");
    const position = rank(row);
    if (position === null) tr.classList.add("unranked");
    else if (position <= 3) tr.classList.add("leading");
    const values = [position ?? "-", row.nickname, ...Object.keys(board.challenges).map(id => format(row.scores[id])), row.rank === null ? "-" : format(row.total)];
    values.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 0) {
        const badge = document.createElement("span"); badge.className = "rank"; badge.textContent = value; td.append(badge);
      } else { td.textContent = value; }
      if (index === 1) td.title = row.nickname;
      const scoreKey = index === values.length - 1 ? "overall" : Object.keys(board.challenges)[index - 2];
      if (index >= 2 && scoreKey === key) td.classList.add("score-active");
      tr.append(td);
    });
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = document.createElement("tr"), td = document.createElement("td");
    td.colSpan = Object.keys(board.challenges).length + 3; td.className = "empty";
    td.textContent = "No participants registered yet. Standings will appear after registration.";
    tr.append(td); fragment.append(tr);
  }
  $("standings").replaceChildren(fragment);
  document.querySelectorAll("th[data-score]").forEach(th => th.classList.toggle("score-active", th.dataset.score === key));
  $("ranking-title").textContent = key === "overall" ? "Overall standings" : `Challenge ${key} · ${board.challenges[key].title}`;
  $("ranking-description").textContent = key === "overall" ? `Four Challenges · out of ${board.rules.overall_max}` : `Best submission in this Challenge · out of ${board.rules.challenge_max}`;
  $("stat-people").textContent = rows.length;
  for (const status of ["completed", "running", "queued"]) $("stat-" + status).textContent = board.queue[status] || 0;
  $("page-label").textContent = `Page ${page + 1} / ${pageCount}`;
  $("range-label").textContent = rows.length ? `Participants ${start + 1}–${Math.min(start + size, rows.length)} of ${rows.length}` : "Waiting for registration";
  $("previous-page").disabled = page === 0;
  $("next-page").disabled = page === pageCount - 1;
  if (!isDisplay) {
    $("rules").textContent = `${board.rules.description} Fixed training: ${board.rules.steps} steps, seed ${board.rules.seed}. Provisional error scale: ${board.rules.quality_error_scale}.`;
    $("revision").textContent = `Scoring version: ${board.rules.rubric} · ${board.fingerprint.slice(0, 12)}`;
    expectedFiles();
  }
  updateRotation();
}
function updateConnection() {
  const disconnected = stale();
  const changed = $("stale-warning").hidden === disconnected;
  document.body.dataset.connection = disconnected ? "stale" : lastSuccess ? "live" : "waiting";
  const message = disconnected ? "Connection delayed · last received scores" : lastSuccess ? "Connected · updates every 5 seconds" : "Connecting";
  // Do not repeatedly announce an unchanged live region to screen readers.
  if ($("connection").textContent !== message) $("connection").textContent = message;
  $("stale-warning").hidden = !disconnected;
  $("updated").textContent = lastSuccess ? `Last received ${new Date(lastSuccess).toLocaleTimeString("en-GB", {hour12:false})}` : "No results received yet.";
  if (changed && board) drawBoard();
  updateRotation();
}
function updateRotation() {
  $("rotate-pages").setAttribute("aria-pressed", String(autoRotate));
  $("rotate-pages").textContent = autoRotate ? "Auto paging on" : "Auto paging off";
  $("rotation-note").textContent = stale() ? "Auto paging paused until reconnection" : pageCount === 1 ? "All participants are on this page." : autoRotate ? "Next page every 15 seconds · returns to the first page" : "Use the arrows to change page.";
}
async function refreshBoard() {
  if (fetchingBoard) return;
  fetchingBoard = true;
  try {
    const next = await api("/api/board");
    if (!Array.isArray(next.participants) || !next.challenges || !next.rules || !next.queue) throw new Error("Invalid board response");
    board = next;
    if (stale()) nextRotation = Date.now() + rotationMs;
    lastSuccess = Date.now(); connectionFailed = false;
    drawBoard();
  } catch (_) { connectionFailed = true; }
  finally { fetchingBoard = false; updateConnection(); }
}
function changePage(delta) {
  page = Math.max(0, Math.min(pageCount - 1, page + delta));
  nextRotation = Date.now() + rotationMs;
  drawBoard();
}
$("ranking").addEventListener("change", () => { page = 0; nextRotation = Date.now() + rotationMs; rememberView(); drawBoard(); });
$("previous-page").addEventListener("click", () => changePage(-1));
$("next-page").addEventListener("click", () => changePage(1));
$("rotate-pages").addEventListener("click", () => { autoRotate = !autoRotate; nextRotation = Date.now() + rotationMs; rememberView(); updateRotation(); });
window.addEventListener("resize", drawBoard);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { nextRotation = Date.now() + rotationMs; refreshBoard(); } });

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
    if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); changePage(event.key === "ArrowRight" ? 1 : -1); }
  });
} else {
  installSubmission();
}
function expectedFiles() {
  if (board && !isDisplay) $("expected-files").textContent = "Files: " + board.challenges[$("challenge").value].files.join(", ");
}
async function refreshMine() {
  const token = $("token").value.trim(), request = ++mineRequest;
  const data = await api("/api/me", {headers: {Authorization: `Bearer ${token}`}});
  if (request !== mineRequest || token !== $("token").value.trim()) return;
  $("my-heading").textContent = `${data.nickname} · My submissions`;
  $("history").replaceChildren();
  const statuses = {queued: "Queued", running: "Running", completed: "Completed", time_limit: "Time limit", system_error: "Server error"};
  for (const item of data.submissions) {
    const details = document.createElement("details"), summary = document.createElement("summary"), text = document.createElement("pre");
    summary.textContent = `Challenge ${item.challenge} · ${statuses[item.status] || item.status} · ${item.score === null ? "Not scored" : format(item.score) + " / 100"} · ${new Date(item.created * 1000).toLocaleString("en-GB")}`;
    text.textContent = JSON.stringify({submission_id:item.id, source_hash:item.source_hash, result:item.result, error:item.error}, null, 2);
    details.append(summary, text); $("history").append(details);
  }
  if (!data.submissions.length) $("history").textContent = "No submissions yet.";
}
function installSubmission() {
  $("token").addEventListener("input", () => { mineRequest++; $("history").replaceChildren(); $("my-heading").textContent = "My submissions"; $("submission-status").textContent = ""; });
  $("submission-form").addEventListener("submit", async event => {
    event.preventDefault(); $("submit-button").disabled = true;
    let accepted = false;
    try {
      const files = [...$("files").files];
      if (!files.length) throw new Error("Choose saved .py files or one exported JSON file.");
      if (files.some(file => file.size > 1024 * 1024)) throw new Error("A selected file exceeds 1 MiB.");
      let payload = {challenge:$("challenge").value, sources:{}};
      if (files.length === 1 && files[0].name.endsWith(".json")) {
        payload = JSON.parse(await files[0].text());
        if (payload.challenge !== $("challenge").value) throw new Error("Select the Challenge matching the exported file.");
      } else {
        for (const file of files) {
          if (Object.hasOwn(payload.sources, file.name)) throw new Error("Duplicate filename.");
          payload.sources[file.name] = await file.text();
        }
      }
      const result = await api("/api/submissions", {method:"POST", headers:{Authorization:`Bearer ${$("token").value.trim()}`, "Content-Type":"application/json"}, body:JSON.stringify(payload)});
      accepted = true;
      $("submission-status").textContent = `Accepted: ${result.submission_id}. Waiting for evaluation; this is not a score yet.`;
      await refreshMine(); await refreshBoard();
    } catch (error) {
      $("submission-status").textContent = (accepted ? "Submission accepted. Could not refresh history: " : "") + error.message;
    } finally { $("submit-button").disabled = false; }
  });
  $("refresh-mine").addEventListener("click", () => refreshMine().catch(error => { $("submission-status").textContent = error.message; }));
  $("challenge").addEventListener("change", expectedFiles);
  for (const view of ["board", "submit"]) $(view + "-tab").addEventListener("click", () => {
    for (const other of ["board", "submit"]) { $(other + "-panel").hidden = other !== view; $(other + "-tab").setAttribute("aria-pressed", String(other === view)); }
    if (view === "board") drawBoard();
  });
}
updateRotation();
refreshBoard();
setInterval(() => {
  updateConnection();
  if (!autoRotate || !board || stale() || document.hidden || $("board-panel").hidden || pageCount < 2) return;
  if (Date.now() >= nextRotation) { page = (page + 1) % pageCount; nextRotation = Date.now() + rotationMs; drawBoard(); }
}, 1000);
setInterval(async () => {
  if (document.hidden) return;
  await refreshBoard();
  if (!isDisplay && !$("submit-panel").hidden && $("token").value.trim()) await refreshMine().catch(error => { $("submission-status").textContent = error.message; });
}, 5000);
