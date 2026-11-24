"use strict";

// Only in-memory UI state. Never persist prompts, tokens, or model responses.
let bootstrap = null;
let pending = null;
let busy = false;
const el = (id) => document.getElementById(id);
const backendName = {
  codex_cli: "Codex CLI",
  openai_api: "OpenAI API",
  copilot_cli: "GitHub Copilot CLI",
};

function alertText(message, kind = "") {
  const area = el("alert");
  area.textContent = message;
  area.className = kind;
}
function resultJSON(payload) {
  el("resultText").textContent = JSON.stringify(payload, null, 2);
}
function setState(id, value, variant = "") {
  const target = el(id);
  target.textContent = value;
  target.className = "stage-state" + (variant ? " " + variant : "");
}
function selectBackend() {
  const selected = document.querySelector("input[name=backend]:checked");
  return selected ? selected.value : "codex_cli";
}
function updateLiveControls() {
  const allowed = Boolean(bootstrap && bootstrap.allow_live && pending && !busy);
  el("liveApproved").disabled = !allowed;
  el("confirmInput").disabled = !allowed;
  el("runBtn").disabled = !(allowed && el("liveApproved").checked
    && el("confirmInput").value === "SEND");
}
function setBusy(value) {
  busy = value;
  el("preflightBtn").disabled = value;
  el("mockBtn").disabled = value || !pending;
  el("refreshBtn").disabled = value;
  updateLiveControls();
}
function invalidatePreflight() {
  pending = null;
  el("preflightDetails").classList.add("hidden");
  el("mockBtn").disabled = true;
  el("liveApproved").checked = false;
  el("confirmInput").value = "";
  setState("preflightState", "未実行");
  setState("mockState", "未実行");
  setState("liveState", "未実行");
  updateLiveControls();
}
function readinessRow(name, item) {
  const row = document.createElement("div");
  row.className = "readiness-row";
  const left = document.createElement("div");
  const title = document.createElement("div");
  title.className = "readiness-name";
  title.textContent = name;
  const detail = document.createElement("div");
  detail.className = "readiness-detail";
  // CLIの存在だけで認証が済んでいると誤表示しない。
  const available = item.installed && item.key_configured;
  if (name === "OpenAI API") {
    detail.textContent = item.key_configured ? "APIキー設定あり" : "APIキー未設定";
  } else {
    detail.textContent = item.installed
      ? (item.key_configured ? "CLIあり / キー設定あり" : "CLIあり / 保存済み認証は未検証")
      : "CLIがPATH上にありません";
  }
  left.append(title, detail);
  const badge = document.createElement("span");
  badge.className = "readiness-state "
    + (available ? "state-ready" : item.installed ? "state-warn" : "state-off");
  badge.textContent = available ? "設定あり" : item.installed ? "要確認" : "未準備";
  row.append(left, badge);
  return row;
}
function renderReadiness(status) {
  const area = el("environment");
  area.replaceChildren();
  area.append(
    readinessRow("Codex CLI", status.readiness.codex_cli),
    readinessRow("OpenAI API", status.readiness.openai_api),
    readinessRow("Copilot CLI", status.readiness.copilot_cli),
  );
  el("modeLabel").textContent = status.allow_live
    ? "個別承認による実行可"
    : "オフライン・テストモード";
  el("modeDetail").textContent = status.allow_live
    ? "送信先と本文を確認して、1回だけ実行"
    : "デフォルトでは外部モデルに送信しません";
  el("liveHelp").textContent = status.allow_live
    ? "実行すると、選択したモデルへ事前確認済みの内容を1回送信します。"
    : "安全のため無効です。起動時に --allow-live が必要です。";
}
async function loadStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("status");
    const status = await response.json();
    bootstrap = status;
    renderReadiness(status);
    // Mock-onlyでは実際のモデルIDがなくても検証を始められる。
    // Live modeはモデルIDを手動で指定する必要がある。
    if (!status.allow_live && !el("modelId").value) {
      el("modelId").value = "synthetic-demo-model";
    }
    updateLiveControls();
  } catch (_) {
    alertText("ローカルサーバーへの接続を確認してください。", "error");
  }
}
async function postAction(path, data) {
  if (!bootstrap || !bootstrap.csrf_token) throw new Error("画面を再読み込みしてください。");
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Local-UI-Token": bootstrap.csrf_token,
    },
    cache: "no-store",
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "実行できませんでした。");
  return body;
}
function values() {
  return {
    backend: selectBackend(),
    model_id: el("modelId").value.trim(),
    provider_family: el("modelFamily").value,
    track: el("track").value,
    role: el("role").value,
    system: el("system").value,
    prompt: el("prompt").value,
    reviewed: el("reviewed").checked,
  };
}
async function preflight(event) {
  event.preventDefault();
  if (busy) return;
  if (!el("reviewed").checked) {
    alertText("最初に、個人・合成データであることを確認してください。", "error");
    return;
  }
  invalidatePreflight();
  setBusy(true);
  alertText("機密情報・送信先・モデル指定の事前検査を実行中です。");
  try {
    const data = await postAction("/api/preflight", values());
    pending = { id: data.pending_id, sha: data.scope_sha256 };
    el("preflightDetails").classList.remove("hidden");
    el("scopeBackend").textContent = backendName[data.backend] || data.backend;
    el("scopeModel").textContent = data.model_id;
    el("scopeSize").textContent = String(data.prompt_bytes) + " bytes";
    el("scopeHash").textContent = data.scope_sha256;
    setState("preflightState", "PASS", "good");
    alertText("事前検査PASS。まだ外部送信していません。", "success");
    resultJSON({
      status: data.status,
      backend: data.backend,
      model_id: data.model_id,
      scope_sha256: data.scope_sha256,
      prompt_bytes: data.prompt_bytes,
      external_send_performed: false,
      expires_in_seconds: data.expires_in_seconds,
    });
  } catch (error) {
    alertText(error.message, "error");
    setState("preflightState", "要確認", "bad");
  } finally {
    setBusy(false);
  }
}
async function mockRun() {
  if (!pending || busy) return;
  setBusy(true);
  try {
    const result = await postAction("/api/mock", {
      pending_id: pending.id, scope_sha256: pending.sha,
    });
    resultJSON(result);
    setState("mockState", "PASS（模擬）", "good");
    alertText("モック完了。実モデル・API・CLIには送信していません。", "success");
  } catch (error) {
    setState("mockState", "要確認", "bad");
    alertText(error.message, "error");
  } finally {
    setBusy(false);
  }
}
async function liveRun() {
  if (!pending || busy || !bootstrap || !bootstrap.allow_live) return;
  if (!el("liveApproved").checked || el("confirmInput").value !== "SEND") {
    alertText("送信承認のチェックとSENDの入力が必要です。", "error");
    return;
  }
  setBusy(true);
  const item = pending;
  pending = null;
  el("liveApproved").checked = false;
  el("confirmInput").value = "";
  alertText("実モデルに1回送信中です。エラーの場合も自動再送しません。");
  try {
    const result = await postAction("/api/run", {
      pending_id: item.id, scope_sha256: item.sha, confirmation: "SEND",
    });
    resultJSON(result);
    setState("liveState", "応答受信（未承認）", "good");
    alertText("モデル応答を取得しました。内容・品質・業務完了は未検証です。", "success");
  } catch (error) {
    setState("liveState", "状態未確定", "bad");
    alertText(error.message + " 再試行する場合は、再度事前検査してください。", "error");
  } finally {
    setBusy(false);
    el("mockBtn").disabled = true;
    updateLiveControls();
  }
}
function resetForm() {
  el("testForm").reset();
  el("system").value = "Return one JSON object with summary and suggestions. Do not run any tools.";
  el("prompt").value = "Synthetic example: the assistant reported a task complete before checking tests. Suggest one concrete verification step.";
  el("modelId").value = bootstrap && !bootstrap.allow_live ? "synthetic-demo-model" : "";
  invalidatePreflight();
  resultJSON({ status: "ready_for_local_test" });
  alertText("入力をリセットしました。実モデルへの送信は行いません。");
}
async function copyResult() {
  try {
    await navigator.clipboard.writeText(el("resultText").textContent);
    alertText("表示されているJSONをコピーしました。", "success");
  } catch (_) {
    alertText("コピーできませんでした。ブラウザのアクセス権限を確認してください。", "error");
  }
}
document.addEventListener("DOMContentLoaded", () => {
  resetForm();
  el("testForm").addEventListener("submit", preflight);
  el("mockBtn").addEventListener("click", mockRun);
  el("runBtn").addEventListener("click", liveRun);
  el("refreshBtn").addEventListener("click", loadStatus);
  el("copyBtn").addEventListener("click", copyResult);
  el("resetBtn").addEventListener("click", resetForm);
  el("liveApproved").addEventListener("change", updateLiveControls);
  el("confirmInput").addEventListener("input", updateLiveControls);
  for (const control of el("testForm").querySelectorAll("input, textarea, select")) {
    control.addEventListener("input", invalidatePreflight);
    control.addEventListener("change", invalidatePreflight);
  }
  for (const radio of document.querySelectorAll("input[name=backend]")) {
    radio.addEventListener("change", () => {
      el("modelFamily").value = radio.value === "copilot_cli" ? "other" : "openai";
    });
  }
  loadStatus();
});
