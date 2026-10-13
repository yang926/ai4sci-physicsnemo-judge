"use strict";

const $ = id => document.getElementById(id);
const demoPage = location.pathname.replace(/\/$/, "") === "/demo-admin";
const apiPrefix = demoPage ? "/api/demo" : "/api";
let currentStatus = null, csrfToken = "", busy = false, pendingAction = null, statusSequence = 0;
const challengeKeys = ["1", "2", "3", "4"];
const lifecycleStates = {open: "접수 중", closing: "마감 예정", closed: "접수 마감", finalized: "최종 결과"};
let lifecycleClock = null, pendingFinalize = null;

async function loadBrandFonts() {
  try {
    const response = await fetch("/brand-fonts.json", {cache: "force-cache"});
    if (!response.ok || !globalThis.FontFace || !document.fonts) return;
    const faces = await Promise.all((await response.json()).map(async ({weight, data}) => {
      const face = new FontFace("NVIDIA Sans", Uint8Array.from(atob(data), value => value.charCodeAt(0)), {weight: String(weight), display: "swap"});
      return face.load();
    }));
    for (const face of faces) document.fonts.add(face);
  } catch (_) { /* Management remains usable with system fonts. */ }
}

function message(text = "", error = false) {
  $("message").textContent = error ? "" : text;
  $("error").textContent = error ? text : "";
  $("error").hidden = !error || !text;
}

function showLogin(expired = false) {
  currentStatus = null; csrfToken = ""; pendingAction = null; pendingFinalize = null; lifecycleClock = null; statusSequence += 1;
  $("workspace").hidden = true; $("logout").hidden = true; $("login-panel").hidden = false;
  $("mode-badge").textContent = "로그인 전";
  if ($("confirm-dialog").open) $("confirm-dialog").close();
  if ($("finalize-dialog").open) $("finalize-dialog").close();
  if ($("password-dialog").open) $("password-dialog").close();
  $("password-settings").hidden = true;
  $("password").value = "";
  clearPasswordForm();
  if (expired) message("관리자 세션이 종료되었습니다. 다시 로그인해 주세요.", true);
}

function clearPasswordForm() {
  for (const id of ["current-password", "new-password", "confirm-password"]) {
    $(id).value = "";
    $(id).removeAttribute("aria-invalid");
  }
  $("password-error").textContent = "";
  $("password-error").hidden = true;
}

function passwordChangeEnabled() {
  return !demoPage && currentStatus?.password_change_enabled === true;
}

async function request(path, payload) {
  const options = {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}};
  if (payload !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    if (csrfToken) options.headers["X-CSRF-Token"] = csrfToken;
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); } catch (_) { result = {}; }
  if (!response.ok) {
    const error = new Error(typeof result.error === "string" ? result.error : `요청을 처리하지 못했습니다. (${response.status})`);
    error.status = response.status;
    error.reauthenticate = result.reauthenticate === true;
    throw error;
  }
  return result;
}

function quiet() {
  return currentStatus && currentStatus.maintenance === true && currentStatus.queued === 0 && currentStatus.running === 0;
}

function updateControls() {
  $("intake-toggle").disabled = busy || !currentStatus;
  $("create-backup").disabled = busy || !currentStatus;
  $("refresh").disabled = busy;
  $("logout").disabled = busy;
  $("password-settings").hidden = !passwordChangeEnabled();
  $("password-settings").disabled = busy || !passwordChangeEnabled();
  for (const id of ["current-password", "new-password", "confirm-password", "password-submit"]) $(id).disabled = busy || !passwordChangeEnabled();
  $("password-cancel").disabled = busy;
  $("reset-scores").disabled = busy || !quiet();
  $("reset-event").disabled = busy || !quiet();
  for (const button of document.querySelectorAll("[data-restore]")) button.disabled = busy || !quiet() || button.dataset.compatible !== "true";
  $("confirm-submit").disabled = busy || !quiet() || !pendingAction || $("confirm-text").value !== pendingAction.confirmation;
  $("confirm-cancel").disabled = busy;
  $("confirm-text").disabled = busy;
  for (const button of document.querySelectorAll("[data-challenge-action]")) {
    button.disabled = busy || !challengeActionAllowed(button.dataset.challenge, button.dataset.challengeAction);
  }
  $("finalize-submit").disabled = busy || !pendingFinalize || !challengeActionAllowed(pendingFinalize, "finalize");
  $("finalize-cancel").disabled = busy;
}

function lifecycleFresh() {
  return currentStatus && lifecycleClock && performance.now() - lifecycleClock.received < 25000;
}

function challengeActionAllowed(key, action) {
  if (!lifecycleFresh()) return false;
  const item = currentStatus.lifecycle?.[key];
  if (!item) return false;
  if (action === "schedule_close") return item.state === "open";
  if (action === "close") return item.state === "open" || item.state === "closing";
  if (action === "retry_errors") return ["closing", "closed"].includes(item.state) && item.system_error > 0;
  return action === "finalize" && item.state === "closed" && item.queued === 0 && item.running === 0 && item.system_error === 0;
}

function lifecycleCountdown(deadline) {
  if (!Number.isFinite(deadline) || !lifecycleClock || !Number.isFinite(lifecycleClock.serverTime)) return "서버 시각 확인 필요";
  const seconds = Math.max(0, Math.ceil(deadline - lifecycleClock.serverTime - (performance.now() - lifecycleClock.received) / 1000));
  return seconds ? `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")} 남음` : "마감 시각 도달 · 상태 확인 중";
}

function makeChallengeCards() {
  const cards = challengeKeys.map(key => {
    const card = node("section", undefined, "challenge-control");
    card.id = `challenge-${key}`;
    card.setAttribute("aria-labelledby", `challenge-${key}-title`);
    const heading = node("div", undefined, "challenge-control-heading"), title = node("h3", `Challenge ${key}`), state = node("span", "상태 확인 중", "challenge-state");
    title.id = `challenge-${key}-title`; state.id = `challenge-${key}-state`;
    heading.append(title, state);
    const timing = node("p", "", "challenge-timing"), counts = node("p", "", "challenge-counts"), note = node("p", "", "challenge-detail"), buttons = node("div", undefined, "challenge-actions");
    timing.id = `challenge-${key}-timing`; counts.id = `challenge-${key}-counts`; note.id = `challenge-${key}-detail`;
    for (const [action, label] of [["schedule_close", "5분 뒤 마감"], ["close", "즉시 마감"], ["finalize", "결과 확정"], ["retry_errors", "시스템 오류 재채점"]]) {
      const button = node("button", label, action === "finalize" ? "primary" : undefined);
      button.type = "button"; button.disabled = true; button.dataset.challenge = key; button.dataset.challengeAction = action;
      button.hidden = action === "retry_errors";
      button.setAttribute("aria-label", `Challenge ${key} ${label}`);
      button.addEventListener("click", () => challengeAction(key, action));
      buttons.append(button);
    }
    card.append(heading, timing, counts, note, buttons);
    return card;
  });
  $("challenge-controls").replaceChildren(...cards);
}

function renderLifecycles() {
  const fresh = lifecycleFresh();
  for (const key of challengeKeys) {
    const item = currentStatus?.lifecycle?.[key];
    const known = item && Object.hasOwn(lifecycleStates, item.state);
    $(`challenge-${key}`).dataset.state = fresh && known ? item.state : "unknown";
    $(`challenge-${key}-state`).textContent = fresh && known ? lifecycleStates[item.state] : "상태 확인 필요";
    $(`challenge-${key}-timing`).textContent = !fresh || !known ? "최신 상태를 확인한 뒤 작업할 수 있습니다." : item.state === "closing" ? lifecycleCountdown(item.deadline) : item.state === "finalized" ? `확정 ${timestamp(item.finalized_at)}` : item.state === "open" ? "마감 예약 없음" : "새 제출을 받지 않습니다.";
    $(`challenge-${key}-counts`).textContent = known ? `대기 ${item.queued ?? "-"} · 채점 중 ${item.running ?? "-"} · 시스템 오류 ${item.system_error ?? "-"}` : "대기 - · 채점 중 - · 시스템 오류 -";
    $(`challenge-${key}-detail`).textContent = !fresh || !known ? "" : item.state === "finalized" ? "결과 확정 완료 · 정답 공개 가능" : item.system_error > 0 ? (item.state === "closed" ? "시스템 오류가 난 기존 제출만 재채점합니다. 새 제출은 받지 않습니다." : "시스템 오류를 확인하세요. 마감 예약 후 기존 제출을 재채점할 수 있습니다.") : item.state === "closed" ? (item.queued > 0 || item.running > 0 ? "접수 마감 · 채점 중" : "접수 마감 · 확정 대기") : "마감 후 채점이 끝나면 결과를 확정할 수 있습니다.";
    $(`challenge-${key}`).querySelector('[data-challenge-action="retry_errors"]').hidden = !known || !["closing", "closed"].includes(item.state) || !(item.system_error > 0);
  }
}

function challengeAction(key, action) {
  if (busy || !challengeActionAllowed(key, action)) return;
  if (action === "finalize") {
    pendingFinalize = key;
    $("finalize-mode").textContent = demoPage ? "시범 데이터만 변경" : "실제 행사 데이터 변경";
    $("finalize-title").textContent = `Challenge ${key} 결과를 확정할까요?`;
    $("finalize-error").hidden = true; $("finalize-error").textContent = "";
    updateControls(); $("finalize-dialog").showModal(); $("finalize-cancel").focus();
    return;
  }
  const success = action === "schedule_close" ? `Challenge ${key}를 5분 뒤 마감하도록 예약했습니다.` : action === "retry_errors" ? `Challenge ${key}의 시스템 오류 제출을 재채점 대기열에 넣었습니다.` : `Challenge ${key} 접수를 마감했습니다.`;
  operate(`${apiPrefix}/challenges/action`, {challenge: key, action}, success);
}

function timestamp(value) {
  if (value === null || value === undefined || value === "") return "-";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("ko-KR", {hour12: false});
}

function bytes(value) {
  if (!Number.isFinite(value) || value < 0) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function renderBackups(backups) {
  const fragment = document.createDocumentFragment();
  for (const backup of backups) {
    if (typeof backup.id !== "string") continue;
    const row = node("tr"), name = node("td"), actions = node("td"), controls = node("div", undefined, "row-actions");
    name.append(node("strong", backup.id), node("small", backup.reason || "백업"));
    const download = node("a", "내려받기", "button");
    download.href = `${apiPrefix}/backups/${encodeURIComponent(backup.id)}`;
    download.setAttribute("download", "");
    download.setAttribute("aria-label", `${backup.id} 백업 내려받기`);
    const restore = node("button", "복원");
    restore.type = "button"; restore.dataset.restore = backup.id; restore.dataset.compatible = String(backup.compatible === true);
    restore.setAttribute("aria-label", `${backup.id} 백업 복원`);
    restore.addEventListener("click", () => confirmAction("restore", backup));
    controls.append(download, restore); actions.append(controls);
    if (backup.compatible !== true) actions.append(node("small", "대기·채점 중인 작업이 포함되었거나 현재 상태와 맞지 않아 복원할 수 없습니다."));
    row.append(name, node("td", timestamp(backup.created_at ?? backup.created)), node("td", bytes(backup.bytes ?? backup.size)), actions);
    fragment.append(row);
  }
  $("backups").replaceChildren(fragment);
  $("backup-empty").hidden = backups.length > 0;
  $("backup-table-wrap").hidden = backups.length === 0;
}

function renderAudit(entries) {
  const fragment = document.createDocumentFragment();
  for (const entry of entries.slice(0, 12)) {
    const item = node("li");
    let label = entry.action || entry.event || "관리 작업";
    if (["challenge_action_requested", "challenge_action_completed"].includes(entry.action)) {
      const transitions = {schedule_close: "5분 뒤 마감 예약", close: "즉시 마감", finalize: "결과 확정", retry_errors: "시스템 오류 재채점"};
      label = `Challenge ${entry.challenge} · ${transitions[entry.transition] || entry.transition} · ${entry.action.endsWith("completed") ? "완료" : "요청"}`;
      if (Number.isFinite(entry.retried)) label += ` (${entry.retried}건)`;
    }
    item.append(node("time", timestamp(entry.at ?? entry.created ?? entry.time ?? entry.timestamp)), node("span", label));
    if (entry.result) item.append(node("span", entry.result));
    fragment.append(item);
  }
  $("audit").replaceChildren(fragment);
  $("audit-empty").hidden = entries.length > 0;
}

function renderStatus(status) {
  const expectedMode = demoPage ? "demo" : "live";
  if (status.mode !== expectedMode) throw new Error("관리 화면과 데이터 종류가 일치하지 않습니다. 작업을 중단하고 관리자 설정을 확인하세요.");
  currentStatus = status;
  lifecycleClock = {serverTime: Number.isFinite(status.server_time) ? status.server_time : null, received: performance.now()};
  if (typeof status.csrf_token === "string") csrfToken = status.csrf_token;
  document.body.dataset.mode = status.mode;
  $("login-panel").hidden = true; $("workspace").hidden = false; $("logout").hidden = false;
  $("mode-badge").textContent = demoPage ? "시범 데이터" : "실제 행사";
  $("mode-title").textContent = demoPage ? "시범 데이터 · 실제 행사와 분리됨" : "실제 행사 데이터";
  $("mode-description").textContent = demoPage ? "가상 참가자와 점수로 관리 작업을 연습합니다. GPU 학습·채점은 실행하지 않습니다." : "여기서 초기화·복원하면 참가자들의 실제 기록이 변경됩니다.";
  $("live-nav").toggleAttribute("aria-current", false); $("demo-nav").toggleAttribute("aria-current", false);
  $(demoPage ? "demo-nav" : "live-nav").setAttribute("aria-current", "page");
  $("state-name").textContent = typeof status.state === "string" ? status.state : "-";
  for (const key of ["participants", "submissions", "queued", "running", "completed"]) $(key).textContent = Number.isFinite(status[key]) ? status[key].toLocaleString("ko-KR") : "-";
  $("named-participants").textContent = Number.isFinite(status.named_participants) ? `닉네임 등록 ${status.named_participants.toLocaleString("ko-KR")}명` : "";
  $("intake-description").textContent = status.maintenance ? "모든 Challenge의 새 제출 접수가 일시 중단되어 있습니다." : "접수 중인 Challenge의 새 제출을 받고 있습니다.";
  $("intake-toggle").textContent = status.maintenance ? "접수 재개" : "접수 중단";
  $("maintenance-requirement").textContent = quiet() ? "접수 중단 · 대기 0 · 채점 중 0. 확인 문구를 입력하면 초기화·복원을 실행할 수 있습니다." : "접수를 중단하고 대기·채점 중인 작업이 모두 끝난 뒤 사용할 수 있습니다.";
  $("status-updated").textContent = `마지막 확인 ${new Date().toLocaleTimeString("ko-KR", {hour12: false})} · 10초마다 갱신`;
  $("projector-link").hidden = true;
  if (!demoPage && typeof status.projector_url === "string") {
    try {
      const target = new URL(status.projector_url, location.href);
      if (["http:", "https:"].includes(target.protocol) && !target.username && !target.password) {
        $("projector-link").href = target.href; $("projector-link").hidden = false;
      }
    } catch (_) { /* An invalid configured link is not rendered. */ }
  }
  renderBackups(Array.isArray(status.backups) ? status.backups : []);
  renderAudit(Array.isArray(status.audit) ? status.audit : []);
  renderLifecycles();
  updateControls();
}

async function refreshStatus(silent = false) {
  const sequence = ++statusSequence;
  try {
    const status = await request(`${apiPrefix}/status`);
    if (sequence === statusSequence) renderStatus(status);
  } catch (error) {
    if (sequence !== statusSequence) return;
    if (error.status === 401) { showLogin(Boolean(currentStatus)); return; }
    if (currentStatus) {
      currentStatus = null; renderLifecycles(); updateControls();
      $("status-updated").textContent = "상태 확인에 실패했습니다. 관리 작업을 잠갔습니다.";
    }
    if (!silent || !$("workspace").hidden) message(error.message, true);
  }
}

async function operate(path, payload, success) {
  if (busy) return;
  busy = true; statusSequence += 1; updateControls(); message("처리 중입니다. 완료될 때까지 기다려 주세요.");
  try {
    const result = await request(path, payload);
    if ($("confirm-dialog").open) $("confirm-dialog").close();
    if ($("finalize-dialog").open) $("finalize-dialog").close();
    pendingAction = null;
    const warning = result?.result?.warning;
    if (typeof warning === "string" && payload.scope === "event") {
      message(`${success} 기존 VM은 재연결될 수 있으며 비밀번호·연결 비밀키는 교체되지 않았습니다.`);
    } else if (typeof warning === "string" && path.endsWith("/restore")) {
      message(`${success} 등록·접근 차단 기록도 백업 시점으로 돌아갔습니다. 외부 연결 비밀키는 복원되지 않았습니다.`);
    } else message(success);
    await refreshStatus();
  } catch (error) {
    if (error.status === 401) { showLogin(true); return; }
    if ($("confirm-dialog").open) {
      $("confirm-error").textContent = error.message; $("confirm-error").hidden = false;
    } else if ($("finalize-dialog").open) {
      $("finalize-error").textContent = error.message; $("finalize-error").hidden = false;
    } else message(error.message, true);
    await refreshStatus(true);
  } finally { busy = false; updateControls(); }
}

function confirmAction(kind, backup) {
  if (busy || !quiet() || (kind === "restore" && backup?.compatible !== true)) return;
  const actions = {
    scores: {title: "점수·제출 기록을 초기화할까요?", confirmation: "RESET SCORES", path: "/reset", payload: {scope: "scores"}, scope: ["삭제: 제출 코드·채점 결과·점수·순위 기록·확정 결과", "모든 Challenge의 마감 예약·마감·확정을 초기화하고 새 행사로 접수를 엽니다.", "유지: 참가자·닉네임·개인 인증 정보·인스턴스 연결 설정"], success: "점수·제출·확정 결과를 초기화하고 모든 Challenge를 새 행사로 열었습니다. 참가자 정보는 유지했습니다."},
    event: {title: "행사 전체를 초기화할까요?", confirmation: "RESET EVENT", path: "/reset", payload: {scope: "event"}, scope: ["삭제: 모든 참가자·닉네임·등록·접근 차단 기록·제출·점수·확정 결과", "모든 Challenge의 마감 예약·마감·확정을 초기화하고 새 행사로 접수를 엽니다.", "기존 VM은 재연결될 수 있으며 비밀번호·연결 비밀키는 교체되지 않습니다.", "Brev 인스턴스나 학생의 로컬 실습 파일은 삭제하지 않습니다."], success: "행사 데이터를 초기화하고 모든 Challenge를 새 행사로 열었습니다."},
    restore: {title: "선택한 백업으로 복원할까요?", confirmation: "RESTORE BACKUP", path: "/restore", payload: {backup_id: backup?.id}, scope: [`선택한 백업: ${backup?.id || ""}`, "현재 참가자·인증 정보·제출·점수를 백업 시점의 기록으로 교체합니다.", "등록·접근 차단 기록도 되돌아갑니다. 백업 이후 차단한 참가자의 접근이 다시 허용될 수 있습니다.", "외부 연결 비밀키는 백업에 포함되지 않으며 교체·복원하지 않습니다."], success: "선택한 백업으로 복원했습니다. 참가자 인증 상태를 확인해 주세요."}
  };
  pendingAction = actions[kind];
  $("confirm-mode").textContent = demoPage ? "시범 데이터만 변경" : "실제 행사 데이터 변경";
  $("confirm-title").textContent = pendingAction.title;
  $("confirm-intro").textContent = demoPage ? "이 작업은 시범 데이터에만 적용됩니다." : "다음 범위를 확인한 뒤 진행하세요.";
  $("confirm-scope").replaceChildren(...pendingAction.scope.map(text => node("li", text)));
  $("confirm-phrase").textContent = pendingAction.confirmation;
  $("confirm-text").value = ""; $("confirm-error").hidden = true; $("confirm-error").textContent = "";
  $("confirm-dialog").showModal(); updateControls(); $("confirm-text").focus();
}

$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  busy = true; statusSequence += 1; $("login-submit").disabled = true; message();
  try {
    const result = await request("/api/login", {password: $("password").value});
    csrfToken = typeof result.csrf_token === "string" ? result.csrf_token : "";
    $("password").value = "";
    await refreshStatus();
  } catch (error) { message(error.message, true); }
  finally { busy = false; $("login-submit").disabled = false; updateControls(); }
});
$("logout").addEventListener("click", async () => {
  if (busy) return;
  busy = true; updateControls();
  try { await request("/api/logout", {}); showLogin(); message("로그아웃했습니다."); }
  catch (error) { if (error.status === 401) showLogin(true); else message(error.message, true); }
  finally { busy = false; updateControls(); }
});
$("refresh").addEventListener("click", () => refreshStatus());
$("password-settings").addEventListener("click", () => {
  if (busy || !passwordChangeEnabled()) return;
  clearPasswordForm();
  $("password-dialog").showModal();
  $("current-password").focus();
});
$("password-cancel").addEventListener("click", () => $("password-dialog").close());
$("password-dialog").addEventListener("cancel", event => { if (busy) event.preventDefault(); });
$("password-dialog").addEventListener("close", clearPasswordForm);
$("password-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy || !passwordChangeEnabled()) return;
  $("password-error").hidden = true;
  $("confirm-password").removeAttribute("aria-invalid");
  if ($("new-password").value !== $("confirm-password").value) {
    $("password-error").textContent = "새 비밀번호가 서로 다릅니다. 다시 확인해 주세요.";
    $("password-error").hidden = false;
    $("confirm-password").setAttribute("aria-invalid", "true");
    $("confirm-password").focus();
    return;
  }
  busy = true; statusSequence += 1; updateControls();
  $("password-submit").textContent = "변경 중…";
  try {
    await request("/api/password/change", {current_password: $("current-password").value, new_password: $("new-password").value, confirmation: $("confirm-password").value});
    showLogin();
    message("비밀번호를 변경했습니다. 모든 관리자 세션이 종료되었습니다. 새 비밀번호로 다시 로그인해 주세요.");
    $("password").focus();
  } catch (error) {
    if (error.reauthenticate) { showLogin(); message(error.message, true); $("password").focus(); return; }
    if (error.status === 401) { showLogin(true); return; }
    $("current-password").value = "";
    $("password-error").textContent = error.message;
    $("password-error").hidden = false;
  } finally {
    busy = false;
    $("password-submit").textContent = "변경 후 다시 로그인";
    updateControls();
  }
});
$("intake-toggle").addEventListener("click", () => {
  if (!currentStatus || busy) return;
  const resume = currentStatus.maintenance;
  operate(`${apiPrefix}/${resume ? "resume" : "pause"}`, {}, resume ? "제출 접수를 재개했습니다." : "새 제출 접수를 중단했습니다. 대기·채점 중인 작업 수를 확인하세요.");
});
$("create-backup").addEventListener("click", () => operate(`${apiPrefix}/backup`, {}, "백업을 만들었습니다."));
$("reset-scores").addEventListener("click", () => confirmAction("scores"));
$("reset-event").addEventListener("click", () => confirmAction("event"));
$("confirm-text").addEventListener("input", updateControls);
$("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close());
$("confirm-dialog").addEventListener("cancel", event => { if (busy) event.preventDefault(); });
$("confirm-dialog").addEventListener("close", () => { pendingAction = null; $("confirm-text").value = ""; });
$("confirm-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!pendingAction || busy || !quiet() || $("confirm-text").value !== pendingAction.confirmation) return;
  const action = pendingAction;
  operate(`${apiPrefix}${action.path}`, {...action.payload, confirmation: $("confirm-text").value}, action.success);
});
$("finalize-cancel").addEventListener("click", () => $("finalize-dialog").close());
$("finalize-dialog").addEventListener("cancel", event => { if (busy) event.preventDefault(); });
$("finalize-dialog").addEventListener("close", () => { pendingFinalize = null; });
$("finalize-form").addEventListener("submit", event => {
  event.preventDefault();
  if (busy || !pendingFinalize || !challengeActionAllowed(pendingFinalize, "finalize")) return;
  operate(`${apiPrefix}/challenges/action`, {challenge: pendingFinalize, action: "finalize"}, `Challenge ${pendingFinalize} 결과를 확정했습니다. 정답을 공개할 수 있습니다.`);
});

makeChallengeCards();
$("password").disabled = false;
$("login-submit").disabled = false;
document.body.dataset.adminReady = "true";
loadBrandFonts();
refreshStatus(true);
setInterval(() => { if (currentStatus && !busy && !document.hidden) refreshStatus(true); }, 10000);
// Only the presentation clock ticks each second; status still uses the existing poll.
setInterval(() => { if (!$("workspace").hidden) { renderLifecycles(); updateControls(); } }, 1000);
