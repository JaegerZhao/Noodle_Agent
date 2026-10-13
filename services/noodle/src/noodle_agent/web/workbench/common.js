// common workbench responsibilities. State is shared explicitly through imports.
import { $, $$, stageLabels, state, terminalStatuses } from './state.js';

export function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export async function request(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* response is not JSON */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

export function toast(message, kind = "info") {
  const node = create("div", `toast ${kind === "error" ? "error" : ""}`, message);
  $("#toast-region").append(node);
  window.setTimeout(() => node.remove(), 3600);
}

export function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "—";
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function parseApiTimestamp(value) {
  if (!value) return Number.NaN;
  const text = String(value).trim();
  const explicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  return Date.parse(explicitZone ? text : `${text}Z`);
}

export function formatDate(value) {
  if (!value) return "—";
  const timestamp = parseApiTimestamp(value);
  if (!Number.isFinite(timestamp)) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(new Date(timestamp));
}

export function formatDuration(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return "—";
  const total = Math.max(0, Math.floor(Number(ms) / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ${String(seconds).padStart(2, "0")}s`;
}

export function formatTokens(value) {
  const count = Number(value || 0);
  if (!count) return "—";
  if (count < 1000) return String(count);
  return `${(count / 1000).toFixed(count >= 10000 ? 0 : 1)}K`;
}

export function jobElapsedMs(job, now = Date.now()) {
  const started = parseApiTimestamp(job?.created_at);
  if (!Number.isFinite(started)) return Number(job?.duration_ms || 0);
  const ended = terminalStatuses.has(job?.status) ? parseApiTimestamp(job?.updated_at) : now;
  return Math.max(0, (Number.isFinite(ended) ? ended : now) - started);
}

export function createElapsedClock(job, className = "", tagName = "span") {
  const clock = create(tagName, `live-duration ${className}`.trim(), formatDuration(jobElapsedMs(job)));
  clock.dataset.startedAt = job?.created_at || "";
  clock.dataset.endedAt = terminalStatuses.has(job?.status) ? (job?.updated_at || "") : "";
  return clock;
}

export function refreshElapsedClocks() {
  const now = Date.now();
  for (const clock of $$(".live-duration")) {
    const started = parseApiTimestamp(clock.dataset.startedAt);
    const ended = parseApiTimestamp(clock.dataset.endedAt);
    if (!Number.isFinite(started)) continue;
    clock.textContent = formatDuration(Math.max(0, (Number.isFinite(ended) ? ended : now) - started));
  }
}

export function statusMeta(job) {
  if (!job) return { label: "等待处理", className: "" };
  if (job.status === "FAILED") return { label: "处理失败", className: "failed" };
  if (job.status === "CANCELLED") return { label: "已取消", className: "failed" };
  if (job.status === "COMPLETED_WITH_WARNINGS") return { label: "带警告完成", className: "completed" };
  if (job.status === "COMPLETED") return { label: "已完成", className: "completed" };
  return { label: stageLabels[job.stage] || "处理中", className: "running" };
}

export function renderJobWarnings(job) {
  const warnings = Array.isArray(job?.warnings) ? job.warnings : [];
  if (!warnings.length) return null;

  const materialWarnings = warnings.filter((warning) => (warning?.severity || "WARNING") !== "INFO");

  const panel = create("section", `job-warnings ${materialWarnings.length ? "" : "info-only"}`.trim());
  const heading = create("div", "job-warnings-heading");
  heading.append(
    create("strong", "", materialWarnings.length ? "警告明细" : "处理说明"),
    create("span", "job-warning-count", String(warnings.length)),
  );
  const list = create("div", "job-warning-list");
  warnings.forEach((warning, index) => {
    const severity = String(warning?.severity || "WARNING").toUpperCase();
    const severityLabel = { INFO: "提示", WARNING: "警告", ERROR: "错误" }[severity] || "警告";
    const item = create("article", `job-warning-item severity-${severity.toLowerCase()}`);
    const header = create("div", "job-warning-header");
    header.append(
      create("span", "job-warning-severity", severityLabel),
      create("code", "job-warning-code", warning?.code || `WARNING_${index + 1}`),
    );
    if (warning?.section) {
      header.append(create("span", "job-warning-section", `章节：${warning.section}`));
    }
    const detail = warning?.detail || warning?.reason || "任务已完成，但没有提供进一步说明。";
    item.append(header, create("p", "job-warning-detail", detail));
    list.append(item);
  });
  panel.append(heading, list);
  return panel;
}

export function folderName(folderId) {
  if (!folderId) return "未分类";
  return state.folders.find((folder) => folder.id === folderId)?.name || "未知文件夹";
}

export function folderOptions(selected, includeUnfiled = true) {
  const options = [];
  if (includeUnfiled) options.push(`<option value=""${!selected ? " selected" : ""}>未分类</option>`);
  for (const folder of state.folders) {
    const depth = folderDepth(folder);
    options.push(`<option value="${folder.id}"${folder.id === selected ? " selected" : ""}>${"　".repeat(depth)}${escapeHtml(folder.name)}</option>`);
  }
  return options.join("");
}

export function folderDepth(folder) {
  let depth = 0;
  let parent = folder.parent_id;
  const seen = new Set();
  while (parent && !seen.has(parent) && depth < 2) {
    seen.add(parent);
    depth += 1;
    parent = state.folders.find((item) => item.id === parent)?.parent_id;
  }
  return depth;
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]));
}
