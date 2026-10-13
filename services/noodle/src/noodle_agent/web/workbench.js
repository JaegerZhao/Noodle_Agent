const API = "/internal/v1";

const state = {
  config: { harness_web_url: "http://127.0.0.1:3080", harness_chat_api: "/internal/v1/harness", poll_interval_ms: 2500 },
  folders: [],
  sources: [],
  jobs: [],
  syntheses: [],
  synthesis: null,
  concept: null,
  synthesisEditor: null,
  conceptRegistry: [],
  conceptRegistryTimer: null,
  selectedFolder: localStorage.getItem("noodle.ui.folder") || "all",
  selectedSource: null,
  sourceNote: null,
  sourceNoteRefreshing: false,
  contentMode: "note",
  pdfPage: 1,
  pdfBlobUrl: null,
  pdfBlobSourceId: null,
  pdfBlobSize: 0,
  pdfData: null,
  pdfLoadPromise: null,
  pdfAbortController: null,
  pdfJsPromise: null,
  pdfDocument: null,
  pdfDocumentSourceId: null,
  pdfLoadingTask: null,
  pdfPagesPromise: null,
  pdfRenderObserver: null,
  pdfRenderTasks: new Map(),
  pdfPageElements: new Map(),
  pdfScrollFrame: 0,
  navigationIndex: null,
  readingAnchor: null,
  noteScrollFrame: 0,
  navigationGuard: null,
  navigationSequence: 0,
  selectedJobId: localStorage.getItem("noodle.ui.job") || null,
  search: "",
  sort: localStorage.getItem("noodle.ui.sort") || "updated",
  readerMode: "outline",
  drawerOpen: localStorage.getItem("noodle.ui.drawer") === "true",
  polling: false,
  timelinePolling: false,
  agentSessions: [],
  agentSessionId: localStorage.getItem("noodle.agent.session") || null,
  agentHistorySignature: "",
  agentRunning: false,
  agentBusy: false,
  agentSending: false,
  agentTargetNote: null,
  agentAttachments: [],
  agentApprovalBusy: new Set(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const terminalStatuses = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"]);
const runningStatuses = new Set(["CREATED", "RUNNING", "WAITING_EXTERNAL"]);

const stageLabels = {
  CREATED: "等待处理",
  SUBMITTING_PARSE: "提交 MinerU",
  WAITING_PARSE: "等待 MinerU",
  NORMALIZING: "标准化",
  PROFILING_DOCUMENT: "文档画像",
  BUILDING_KNOWLEDGE_MAP: "知识映射",
  PLANNING_SOURCE_NOTE_OUTLINE: "大纲规划",
  PLANNING_NOTE_SET: "笔记规划",
  REVIEWING_NOTE_SET: "规划检查",
  REPAIRING_NOTE_SET: "规划修复",
  ANALYZING_VISUALS: "视觉理解",
  DRAFTING_NOTES: "笔记生成",
  EVALUATING_NOTES: "质量审校",
  VALIDATING_NOTES: "确定性校验",
  INDEXING: "建立索引",
  COMPLETED: "已完成",
};

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* response is not JSON */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, kind = "info") {
  const node = create("div", `toast ${kind === "error" ? "error" : ""}`, message);
  $("#toast-region").append(node);
  window.setTimeout(() => node.remove(), 3600);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "—";
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function parseApiTimestamp(value) {
  if (!value) return Number.NaN;
  const text = String(value).trim();
  const explicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  return Date.parse(explicitZone ? text : `${text}Z`);
}

function formatDate(value) {
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

function formatDuration(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return "—";
  const total = Math.max(0, Math.floor(Number(ms) / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ${String(seconds).padStart(2, "0")}s`;
}

function formatTokens(value) {
  const count = Number(value || 0);
  if (!count) return "—";
  if (count < 1000) return String(count);
  return `${(count / 1000).toFixed(count >= 10000 ? 0 : 1)}K`;
}

function jobElapsedMs(job, now = Date.now()) {
  const started = parseApiTimestamp(job?.created_at);
  if (!Number.isFinite(started)) return Number(job?.duration_ms || 0);
  const ended = terminalStatuses.has(job?.status) ? parseApiTimestamp(job?.updated_at) : now;
  return Math.max(0, (Number.isFinite(ended) ? ended : now) - started);
}

function createElapsedClock(job, className = "", tagName = "span") {
  const clock = create(tagName, `live-duration ${className}`.trim(), formatDuration(jobElapsedMs(job)));
  clock.dataset.startedAt = job?.created_at || "";
  clock.dataset.endedAt = terminalStatuses.has(job?.status) ? (job?.updated_at || "") : "";
  return clock;
}

function refreshElapsedClocks() {
  const now = Date.now();
  for (const clock of $$(".live-duration")) {
    const started = parseApiTimestamp(clock.dataset.startedAt);
    const ended = parseApiTimestamp(clock.dataset.endedAt);
    if (!Number.isFinite(started)) continue;
    clock.textContent = formatDuration(Math.max(0, (Number.isFinite(ended) ? ended : now) - started));
  }
}

function statusMeta(job) {
  if (!job) return { label: "等待处理", className: "" };
  if (job.status === "FAILED") return { label: "处理失败", className: "failed" };
  if (job.status === "CANCELLED") return { label: "已取消", className: "failed" };
  if (job.status === "COMPLETED_WITH_WARNINGS") return { label: "带警告完成", className: "completed" };
  if (job.status === "COMPLETED") return { label: "已完成", className: "completed" };
  return { label: stageLabels[job.stage] || "处理中", className: "running" };
}

function renderJobWarnings(job) {
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

function folderName(folderId) {
  if (!folderId) return "未分类";
  return state.folders.find((folder) => folder.id === folderId)?.name || "未知文件夹";
}

function folderOptions(selected, includeUnfiled = true) {
  const options = [];
  if (includeUnfiled) options.push(`<option value=""${!selected ? " selected" : ""}>未分类</option>`);
  for (const folder of state.folders) {
    const depth = folderDepth(folder);
    options.push(`<option value="${folder.id}"${folder.id === selected ? " selected" : ""}>${"　".repeat(depth)}${escapeHtml(folder.name)}</option>`);
  }
  return options.join("");
}

function folderDepth(folder) {
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

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]));
}

function filteredSources() {
  let items = [...state.sources];
  const folder = state.selectedFolder;
  if (folder === "running") items = items.filter((item) => runningStatuses.has(item.job?.status));
  else if (folder === "failed") items = items.filter((item) => item.job?.status === "FAILED");
  else if (folder === "unfiled") items = items.filter((item) => !item.folder_id);
  else if (folder !== "all") items = items.filter((item) => item.folder_id === folder);
  if (state.search) {
    const needle = state.search.toLocaleLowerCase();
    items = items.filter((item) => `${item.title} ${item.source_file}`.toLocaleLowerCase().includes(needle));
  }
  if (state.sort === "name") items.sort((a, b) => a.title.localeCompare(b.title, "zh-CN"));
  else if (state.sort === "manual" && !["all", "running", "failed", "unfiled"].includes(folder)) items.sort((a, b) => a.sort_order - b.sort_order);
  else items.sort((a, b) => parseApiTimestamp(b.job?.updated_at || b.updated_at) - parseApiTimestamp(a.job?.updated_at || a.updated_at));
  return items;
}

function renderFolderNavigation() {
  const running = state.sources.filter((item) => runningStatuses.has(item.job?.status)).length;
  const failed = state.sources.filter((item) => item.job?.status === "FAILED").length;
  $("#all-count").textContent = state.sources.length;
  $("#running-count").textContent = running;
  $("#failed-count").textContent = failed;
  $("#unfiled-count").textContent = state.sources.filter((item) => !item.folder_id).length;
  $$('[data-smart-folder]').forEach((button) => button.classList.toggle("active", button.dataset.smartFolder === state.selectedFolder));

  const tree = $("#folder-tree");
  tree.replaceChildren();
  const folders = [...state.folders].sort((a, b) => a.sort_order - b.sort_order);
  for (const folder of folders) {
    const button = create("button", "folder-row");
    button.type = "button";
    button.dataset.folderId = folder.id;
    button.dataset.depth = String(folderDepth(folder));
    button.classList.toggle("active", state.selectedFolder === folder.id);
    const name = create("span", "folder-name", folder.name);
    const count = create("b", "", String(state.sources.filter((source) => source.folder_id === folder.id).length));
    button.append(name, count);
    button.addEventListener("click", () => selectFolder(folder.id));
    button.addEventListener("dblclick", () => renameFolder(folder));
    tree.append(button);
  }

  $("#upload-folder").innerHTML = folderOptions(
    ["all", "running", "failed", "unfiled"].includes(state.selectedFolder) ? "" : state.selectedFolder,
  );
}

function selectedFolderTitle() {
  const smart = { all: "全部笔记", running: "正在处理", failed: "处理失败", unfiled: "未分类" };
  return smart[state.selectedFolder] || folderName(state.selectedFolder);
}

function renderSourceTable() {
  const items = filteredSources();
  $("#folder-title").textContent = selectedFolderTitle();
  $("#folder-summary").textContent = `${items.length} 个来源文件 · ${items.filter((item) => item.source_note_id).length} 份完整笔记`;
  $("#source-result-count").textContent = `${items.length} 个文件`;
  $("#empty-library").hidden = items.length !== 0;
  const body = $("#source-rows");
  body.replaceChildren();

  const canReorder = state.sort === "manual" && !state.search && !["all", "running", "failed", "unfiled"].includes(state.selectedFolder);
  for (const source of items) {
    const row = document.createElement("tr");
    row.dataset.sourceId = source.source_id;
    row.draggable = canReorder;
    if (canReorder) bindSourceDrag(row);

    const fileCell = document.createElement("td");
    const openButton = create("button", "source-name-button");
    openButton.type = "button";
    const mark = create("span", "file-mark", "PDF");
    const copy = create("span");
    copy.append(create("strong", "", source.title), create("small", "", source.source_file));
    openButton.append(mark, copy);
    openButton.addEventListener("click", () => openSource(source));
    fileCell.append(openButton);

    const meta = statusMeta(source.job);
    const statusCell = document.createElement("td");
    statusCell.append(create("span", `status-chip ${meta.className}`, meta.label));

    const sizeCell = create("td", "metric-mono", formatBytes(source.byte_size));
    const dateCell = create("td", "", formatDate(source.job?.updated_at || source.updated_at));
    dateCell.title = "杭州时间（UTC+8）";
    const durationCell = create("td", "metric-mono");
    durationCell.append(source.job ? createElapsedClock(source.job) : create("span", "", "—"));
    const tokenCell = create("td", "metric-mono", formatTokens(source.job?.total_tokens));
    const folderCell = document.createElement("td");
    const select = create("select", "folder-select");
    select.setAttribute("aria-label", `移动 ${source.title} 到文件夹`);
    select.innerHTML = folderOptions(source.folder_id);
    select.addEventListener("change", () => moveSource(source.source_id, select.value || null));
    folderCell.append(select);
    const actionCell = document.createElement("td");
    actionCell.append(renderSourceActions(source));
    row.append(fileCell, statusCell, sizeCell, dateCell, durationCell, tokenCell, folderCell, actionCell);
    body.append(row);
  }
}

function renderSourceActions(source) {
  const actions = create("div", "source-actions");
  if (source.job?.status === "FAILED") {
    const retry = create("button", "source-action retry", "断点重试");
    retry.type = "button";
    retry.title = "复用最近的安全断点；没有解析断点时从原始 PDF 重新开始";
    retry.addEventListener("click", () => retryFailedJob(source, retry));
    actions.append(retry);
  }
  const remove = create("button", "source-action danger", "删除来源");
  remove.type = "button";
  remove.title = "从 Noodle 删除来源条目、内部 PDF 副本及全部处理数据";
  remove.disabled = Boolean(source.job && runningStatuses.has(source.job.status));
  remove.addEventListener("click", () => deleteSource(source, remove));
  actions.append(remove);
  return actions;
}

function bindSourceDrag(row) {
  row.addEventListener("dragstart", (event) => {
    row.classList.add("dragging");
    event.dataTransfer.setData("text/plain", row.dataset.sourceId);
    event.dataTransfer.effectAllowed = "move";
  });
  row.addEventListener("dragend", () => row.classList.remove("dragging"));
  row.addEventListener("dragover", (event) => event.preventDefault());
  row.addEventListener("drop", async (event) => {
    event.preventDefault();
    const sourceId = event.dataTransfer.getData("text/plain");
    const dragged = $(`#source-rows tr[data-source-id="${CSS.escape(sourceId)}"]`);
    if (!dragged || dragged === row) return;
    const rect = row.getBoundingClientRect();
    row.parentElement.insertBefore(dragged, event.clientY < rect.top + rect.height / 2 ? row : row.nextSibling);
    const sourceIds = [...$("#source-rows").querySelectorAll("tr")].map((item) => item.dataset.sourceId);
    try {
      await request(`${API}/folders/${state.selectedFolder}/source-order`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_ids: sourceIds }),
      });
      toast("文件顺序已保存");
      await loadLibrary({ quiet: true });
    } catch (error) { toast(error.message, "error"); }
  });
}

async function moveSource(sourceId, folderId) {
  try {
    await request(`${API}/sources/${sourceId}/folder`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id: folderId }),
    });
    toast(`已移动到${folderId ? `“${folderName(folderId)}”` : "“未分类”"}`);
    await loadLibrary({ quiet: true });
  } catch (error) { toast(error.message, "error"); }
}

async function retryFailedJob(source, button) {
  if (!source.job || source.job.status !== "FAILED") return;
  button.disabled = true;
  button.textContent = "提交中…";
  try {
    const job = await request(`${API}/jobs/${source.job.id}/resume`, { method: "POST" });
    const message = job.retry_mode === "checkpoint"
      ? `已从“${stageLabels[job.resumed_from_stage] || job.resumed_from_stage}”断点继续`
      : "解析阶段没有安全断点，已从保留的原始 PDF 重新开始";
    toast(message);
    state.selectedJobId = job.id;
    localStorage.setItem("noodle.ui.job", job.id);
    await loadLibrary({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
    button.textContent = "断点重试";
  }
}

async function refreshKnowledgeSyncStatus() {
  try {
    const events = await request(`${API}/knowledge-sync`);
    let panel = document.querySelector("#knowledge-sync-status");
    if (!panel) {
      panel = create("details", "knowledge-sync-status");
      panel.id = "knowledge-sync-status";
      document.querySelector("#workbench-navigation")?.append(panel);
    }
    const labels = { QUEUED: "等待同步", RUNNING: "知识同步中", COMPLETED: "已同步", COMPLETED_WITH_REVIEW: "已同步 · 有待审查内容", COMPLETED_WITH_ERRORS: "部分失败", FAILED: "同步失败" };
    panel.replaceChildren(create("summary", "", `知识同步 · ${events.length} 条记录`));
    for (const event of events.slice(0, 8)) {
      const row = create("div", "knowledge-sync-row");
      const types = { SOURCE_PUBLISHED: "资料发布", NOTE_UPDATED: "笔记修改", NOTE_DELETED: "笔记删除", SOURCE_DELETED: "资料删除", SOURCE_RESTORED: "资料恢复" };
      row.append(create("span", "", `${event.source_title || types[event.event_type] || "资料"} · ${labels[event.status] || event.status} · 最近一轮更新 ${(event.result?.releases || []).length} 个模块`));
      if (["FAILED", "COMPLETED_WITH_ERRORS", "COMPLETED_WITH_REVIEW"].includes(event.status)) {
        const retry = create("button", "secondary-button", event.status === "COMPLETED_WITH_REVIEW" ? "重新审校" : "重试");
        retry.onclick = async () => { await request(`${API}/knowledge-sync/${event.id}/retry`, { method: "POST" }); await refreshKnowledgeSyncStatus(); };
        row.append(retry);
      }
      const result = event.result || {};
      if (event.status !== "RUNNING" && event.status !== "QUEUED") {
        const extra = create("small", "", `${result.needs_review || 0} 条待审查${result.index_mode === "LEXICAL_FALLBACK" ? " · 词法检索降级" : ""}`);
        row.append(extra);
      }
      const inspect = create("button", "secondary-button", "查看提议");
      inspect.onclick = async () => {
        const old = row.querySelector(".knowledge-sync-proposals");
        if (old) { old.remove(); return; }
        const items = await request(`${API}/knowledge-sync/${event.id}/proposals`);
        const list = create("div", "knowledge-sync-proposals");
        for (const item of items) {
          const names = {APPLIED:"已采纳输入", NEEDS_REVIEW:"待审查", SKIPPED:"无需更新", RETRY_ANALYSIS:"等待重新分析", REANALYZED:"已重新分析"};
          const paragraph = create("p", "", `${item.proposal.title || item.proposal.module_key || "笔记分析"} · ${names[item.status] || item.status}：${item.error_detail || item.proposal.reason || ""}`);
          if (item.status === "NEEDS_REVIEW" && item.proposal.module_id && ["COMPLETED_WITH_REVIEW", "COMPLETED_WITH_ERRORS"].includes(event.status)) {
            const rejudge = create("button", "secondary-button", "重审此模块");
            rejudge.onclick = async () => {
              await request(`${API}/knowledge-sync/${event.id}/retry`, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({module_ids:[item.proposal.module_id]})});
              await refreshKnowledgeSyncStatus();
            };
            paragraph.append(rejudge);
          }
          list.append(paragraph);
        }
        if (!items.length) list.append(create("p", "", "该事件为来源撤回或恢复，无新增内容提议。"));
        row.append(list);
      };
      row.append(inspect);
      row.title = event.error_detail || "";
      panel.append(row);
    }
  } catch (_) { /* Initial servers without migration can still display the workbench. */ }
}
setInterval(refreshKnowledgeSyncStatus, 5000);

async function deleteSource(source, button) {
  const confirmed = window.confirm(
    `确定从 Noodle 中删除“${source.title}”吗？\n\n` +
    "来源与笔记将退出当前检索，依赖它的知识点会按剩余来源自动更新。历史记录与内部文件保留用于恢复。\n" +
    "不会影响：你上传前存放在电脑原位置的 PDF。\n\n可通过来源恢复接口恢复，电脑原位置的 PDF 不受影响。",
  );
  if (!confirmed) return;
  button.disabled = true;
  button.textContent = "删除中…";
  try {
    const result = await request(`${API}/sources/${source.source_id}`, { method: "DELETE" });
    if (state.selectedJobId === source.job?.id) {
      state.selectedJobId = null;
      localStorage.removeItem("noodle.ui.job");
    }
    if (state.selectedSource?.source_id === source.source_id) {
      state.selectedSource = null;
      state.sourceNote = null;
      showWorkbench();
    }
    const suffix = result.filesystem_warnings?.length ? `，但有 ${result.filesystem_warnings.length} 个文件清理警告` : "";
    toast(`来源已退出资料库，知识点撤回同步已排队${suffix}`, result.filesystem_warnings?.length ? "error" : "info");
    await loadLibrary({ quiet: true });
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
    button.textContent = "删除来源";
  }
}

async function createFolder() {
  const name = window.prompt("新文件夹名称");
  if (!name?.trim()) return;
  try {
    const parentId = !["all", "running", "failed", "unfiled"].includes(state.selectedFolder) ? state.selectedFolder : null;
    const folder = await request(`${API}/folders`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), parent_id: parentId }),
    });
    await loadLibrary({ quiet: true });
    selectFolder(folder.id);
    toast(`已创建“${folder.name}”`);
  } catch (error) { toast(error.message, "error"); }
}

async function renameFolder(folder) {
  const name = window.prompt("重命名文件夹", folder.name);
  if (!name?.trim() || name.trim() === folder.name) return;
  try {
    await request(`${API}/folders/${folder.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadLibrary({ quiet: true });
    toast("文件夹已重命名");
  } catch (error) { toast(error.message, "error"); }
}

function selectFolder(folderId) {
  state.selectedFolder = folderId;
  localStorage.setItem("noodle.ui.folder", folderId);
  showWorkbench();
  renderFolderNavigation();
  renderSourceTable();
  history.replaceState(null, "", `#library/${encodeURIComponent(folderId)}`);
}

function sourceReleaseChanged(currentNote, freshSource) {
  return Boolean(
    currentNote
    && freshSource?.source_note_version_id
    && freshSource.source_note_version_id !== currentNote.version_id
  );
}

async function loadLibrary({ quiet = false } = {}) {
  if (state.polling) return;
  state.polling = true;
  try {
    const payload = await request(`${API}/library`);
    state.folders = payload.folders || [];
    state.sources = payload.sources || [];
    state.jobs = payload.jobs || [];
    $(".library-footer").classList.add("online");
    $("#domain-health").lastChild.textContent = "知识服务在线";
    renderFolderNavigation();
    renderSourceTable();
    renderJobs();
    if (state.drawerOpen && state.selectedJobId) await refreshSelectedJobDetail({ quiet: true });
    if (state.selectedSource) {
      const fresh = state.sources.find((item) => item.source_id === state.selectedSource.source_id);
      if (fresh) {
        const releaseChanged = sourceReleaseChanged(state.sourceNote, fresh);
        state.selectedSource = fresh;
        if (releaseChanged) await reloadOpenSource(fresh, { announce: true });
      }
    }
  } catch (error) {
    $(".library-footer").classList.remove("online");
    $("#domain-health").lastChild.textContent = "知识服务不可用";
    if (!quiet) toast(error.message, "error");
  } finally { state.polling = false; }
}

async function loadSyntheses({ quiet = false } = {}) {
  try {
    state.syntheses = await request(`${API}/syntheses`);
    $("#synthesis-count").textContent = String(state.syntheses.length);
    renderSynthesisCards();
  } catch (error) {
    if (!quiet) toast(error.message, "error");
  }
}

function synthesisSlug(value) {
  return String(value || "").normalize("NFKD").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 150);
}

function editorNode(raw) {
  return {
    _uid: crypto.randomUUID(),
    stable_key: raw.stable_key || synthesisSlug(raw.title) || `node-${crypto.randomUUID().slice(0, 8)}`,
    title: raw.title || "未命名",
    node_kind: raw.node_kind === "GROUP" ? "GROUP" : "CONCEPT",
    concept_id: raw.concept_id || "",
    children: (raw.children || []).map(editorNode),
  };
}

function editorPayload(nodes) {
  return nodes.map((node) => ({
    stable_key: node.stable_key,
    title: node.title,
    node_kind: node.node_kind,
    ...(node.concept_id ? { concept_id: node.concept_id } : {}),
    children: editorPayload(node.children || []),
  }));
}

function walkEditorNodes(nodes, callback, parent = null) {
  for (const node of nodes) {
    if (callback(node, nodes, parent)) return true;
    if (walkEditorNodes(node.children || [], callback, node)) return true;
  }
  return false;
}

function selectedEditorGroup() {
  let found = null;
  walkEditorNodes(state.synthesisEditor?.outline || [], (node) => {
    if (node._uid === state.synthesisEditor.selectedGroupUid && node.node_kind === "GROUP") {
      found = node;
      return true;
    }
    return false;
  });
  return found;
}

function uniqueEditorKey(base) {
  const used = new Set();
  walkEditorNodes(state.synthesisEditor?.outline || [], (node) => { used.add(node.stable_key); return false; });
  let value = synthesisSlug(base) || "concept";
  let suffix = 2;
  while (used.has(value)) value = `${synthesisSlug(base) || "concept"}-${suffix++}`;
  return value;
}

function moveEditorNode(uid, offset) {
  walkEditorNodes(state.synthesisEditor.outline, (node, siblings) => {
    if (node._uid !== uid) return false;
    const index = siblings.indexOf(node);
    const target = index + offset;
    if (target >= 0 && target < siblings.length) siblings.splice(target, 0, siblings.splice(index, 1)[0]);
    return true;
  });
  renderSynthesisDraft();
}

function removeEditorNode(uid) {
  walkEditorNodes(state.synthesisEditor.outline, (node, siblings) => {
    if (node._uid !== uid) return false;
    siblings.splice(siblings.indexOf(node), 1);
    return true;
  });
  if (state.synthesisEditor.selectedGroupUid === uid) {
    const firstGroup = state.synthesisEditor.outline.find((item) => item.node_kind === "GROUP");
    state.synthesisEditor.selectedGroupUid = firstGroup?._uid || null;
  }
  renderSynthesisDraft();
}

function renderSynthesisDraft() {
  const host = $("#synthesis-draft-tree");
  host.replaceChildren();
  const nodes = state.synthesisEditor?.outline || [];
  if (!nodes.length) {
    host.append(create("div", "synthesis-draft-empty", "先添加一个分组，再从右侧复用 Concept。"));
    return;
  }
  const append = (node, parent) => {
    const wrapper = create("section", node.node_kind === "GROUP" ? "synthesis-draft-group" : "");
    const row = create("div", `synthesis-draft-row ${node.node_kind.toLowerCase()}`);
    if (node._uid === state.synthesisEditor.selectedGroupUid) row.classList.add("selected");
    const copy = create("div");
    copy.append(
      create("strong", "", node.title),
      create("small", "", node.node_kind === "GROUP" ? "分组 · 点击后新增 Concept 将放入这里" : node.stable_key),
    );
    const actions = create("div", "synthesis-row-actions");
    for (const [label, title, handler] of [
      ["↑", "上移", () => moveEditorNode(node._uid, -1)],
      ["↓", "下移", () => moveEditorNode(node._uid, 1)],
      ["×", "移除", () => removeEditorNode(node._uid)],
    ]) {
      const button = create("button", "", label);
      button.type = "button";
      button.title = title;
      button.addEventListener("click", (event) => { event.stopPropagation(); handler(); });
      actions.append(button);
    }
    row.append(copy, actions);
    if (node.node_kind === "GROUP") row.addEventListener("click", () => {
      state.synthesisEditor.selectedGroupUid = node._uid;
      renderSynthesisDraft();
    });
    wrapper.append(row);
    for (const child of node.children || []) append(child, wrapper);
    parent.append(wrapper);
  };
  for (const node of nodes) append(node, host);
}

function renderConceptRegistry() {
  const host = $("#concept-registry-results");
  host.replaceChildren();
  const concepts = state.conceptRegistry;
  if (!concepts.length) {
    host.append(create("div", "synthesis-draft-empty", "没有匹配的 Concept。可以换一个名称或缩写。"));
    return;
  }
  for (const concept of concepts) {
    const row = create("article", "concept-registry-row");
    row.append(create("strong", "", concept.title), create("small", "", concept.stable_key));
    if (concept.summary) row.append(create("p", "", concept.summary));
    const footer = create("footer");
    footer.append(create("span", "", `${concept.synthesis_count || 0} 个体系复用`));
    const actions = create("div", "concept-registry-actions");
    const add = create("button", "", "加入目录");
    add.type = "button";
    add.addEventListener("click", () => {
      let exists = false;
      walkEditorNodes(state.synthesisEditor.outline, (node) => {
        if (node.concept_id === concept.concept_id) { exists = true; return true; }
        return false;
      });
      if (exists) return toast("这个 Concept 已在当前知识体系中", "error");
      const target = selectedEditorGroup();
      const collection = target ? target.children : state.synthesisEditor.outline;
      collection.push(editorNode({ stable_key: uniqueEditorKey(concept.stable_key.replace(/^[^:]+:/, "")), title: concept.title, node_kind: "CONCEPT", concept_id: concept.concept_id }));
      renderSynthesisDraft();
      $("#synthesis-editor-status").textContent = `已加入 ${concept.title}，等待发布`;
    });
    const mergeSource = create("button", "", "设为源");
    mergeSource.type = "button";
    mergeSource.addEventListener("click", () => setMergeConcept("source", concept));
    const mergeTarget = create("button", "", "设为目标");
    mergeTarget.type = "button";
    mergeTarget.addEventListener("click", () => setMergeConcept("target", concept));
    actions.append(add, mergeSource, mergeTarget);
    footer.append(actions);
    row.append(footer);
    host.append(row);
  }
}

function setMergeConcept(role, concept) {
  const input = $(`#merge-${role}-concept`);
  input.value = `${concept.title} · v${concept.version_no}`;
  input.dataset.conceptId = concept.concept_id;
  input.dataset.versionId = concept.version_id;
  $(".concept-merge-panel").open = true;
  $("#synthesis-editor-status").textContent = role === "source" ? "已选择待合并源" : "已选择保留目标";
}

async function loadConceptRegistry(query = "") {
  const host = $("#concept-registry-results");
  host.replaceChildren(create("div", "synthesis-draft-empty", "正在查询 Concept 注册表…"));
  try {
    state.conceptRegistry = await request(`${API}/concepts?q=${encodeURIComponent(query)}&limit=80`);
    renderConceptRegistry();
  } catch (error) {
    host.replaceChildren(create("div", "synthesis-draft-empty", error.message));
  }
}

async function openSynthesisEditor(synthesisId = null) {
  try {
    const current = synthesisId ? await request(`${API}/syntheses/${encodeURIComponent(synthesisId)}`) : null;
    const outline = current ? (current.outline || []).map(editorNode) : [editorNode({ stable_key: "foundations", title: "基础", node_kind: "GROUP", children: [] })];
    const firstGroup = outline.find((item) => item.node_kind === "GROUP");
    state.synthesisEditor = { mode: current ? "edit" : "create", synthesisId, versionId: current?.version_id || null, outline, selectedGroupUid: firstGroup?._uid || null };
    $("#synthesis-editor-title").textContent = current ? `编辑「${current.title}」` : "新建知识体系";
    $("#synthesis-title-input").value = current?.title || "";
    $("#synthesis-key-input").value = current?.stable_key || "";
    delete $("#synthesis-key-input").dataset.edited;
    $("#synthesis-key-input").disabled = Boolean(current);
    $("#synthesis-domain-input").value = current?.domain_key || "";
    $("#synthesis-domain-input").disabled = Boolean(current);
    $("#synthesis-description-input").value = current?.description || "";
    $("#synthesis-editor-status").textContent = current ? `当前版本 ${current.version_id.slice(0, 8)}，修改后发布新版本` : "尚未发布";
    for (const id of ["#merge-source-concept", "#merge-target-concept"]) {
      $(id).value = "";
      delete $(id).dataset.conceptId;
      delete $(id).dataset.versionId;
    }
    $("#merge-rationale").value = "";
    $("#concept-registry-search").value = "";
    renderSynthesisDraft();
    $("#synthesis-editor").showModal();
    await loadConceptRegistry();
  } catch (error) { toast(error.message, "error"); }
}

function closeSynthesisEditor() {
  $("#synthesis-editor").close();
  state.synthesisEditor = null;
}

async function publishSynthesisEditor(event) {
  event.preventDefault();
  const title = $("#synthesis-title-input").value.trim();
  const stableKey = $("#synthesis-key-input").value.trim();
  const domainKey = $("#synthesis-domain-input").value.trim();
  if (!title || !stableKey || !domainKey || !state.synthesisEditor.outline.length) return toast("请填写名称、标识并保留至少一个目录节点", "error");
  const payload = { title, description: $("#synthesis-description-input").value.trim(), outline: editorPayload(state.synthesisEditor.outline) };
  $("#publish-synthesis").disabled = true;
  $("#synthesis-editor-status").textContent = "正在发布不可变版本…";
  try {
    const result = state.synthesisEditor.mode === "create"
      ? await request(`${API}/syntheses`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...payload, stable_key: stableKey, domain_key: domainKey }) })
      : await request(`${API}/syntheses/${encodeURIComponent(state.synthesisEditor.synthesisId)}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...payload, expected_version_id: state.synthesisEditor.versionId }) });
    closeSynthesisEditor();
    await loadSyntheses();
    toast(`${title} 已发布为 v${result.version_no}`);
    await openSynthesis(result.synthesis_id);
  } catch (error) {
    $("#synthesis-editor-status").textContent = error.message;
    toast(error.message, "error");
  } finally { $("#publish-synthesis").disabled = false; }
}

async function mergeConceptsFromEditor() {
  const source = $("#merge-source-concept");
  const target = $("#merge-target-concept");
  const rationale = $("#merge-rationale").value.trim();
  if (!source.dataset.conceptId || !target.dataset.conceptId || source.dataset.conceptId === target.dataset.conceptId) return toast("请选择两个不同的 Concept", "error");
  if (!rationale) return toast("请填写判断为同一知识点的依据", "error");
  $("#merge-concepts").disabled = true;
  try {
    const payload = {
      source_concept_id: source.dataset.conceptId,
      target_concept_id: target.dataset.conceptId,
      expected_source_version_id: source.dataset.versionId,
      expected_target_version_id: target.dataset.versionId,
      rationale,
    };
    const preview = await request(`${API}/concepts/merge-preview`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
    });
    const lines = preview.modules.map(item => `${item.title}：${item.action === "COMBINE" ? "组合到同主题模块" : "保留独立模块"}，精确重复 ${item.identical_blocks} 块`);
    const message = [
      `将「${preview.source_title}」并入「${preview.target_title}」`,
      "", ...lines,
      "", `合并后范围：${preview.scope_after.includes.join("；") || "保留已有范围"}`,
      "仅去除完全相同的内容，并保留来源证据。",
      preview.relation === "REQUIRES_HUMAN_JUDGMENT" ? "尚无共同名称依据：请确认两者确为同一概念。" : "请确认两者确为同一概念。",
      "总览与子主题建议建立引用；来源分歧不会自动裁决。",
    ].join("\n");
    if (!window.confirm(message)) return;
    await request(`${API}/concepts/merge`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
    });
    toast("Concept 已合并，相关知识体系已发布新版本");
    const editingId = state.synthesisEditor?.synthesisId || null;
    closeSynthesisEditor();
    await loadSyntheses();
    if (editingId) await openSynthesisEditor(editingId);
  } catch (error) { toast(error.message, "error"); }
  finally { $("#merge-concepts").disabled = false; }
}

function renderSynthesisCards() {
  const host = $("#synthesis-cards");
  host.replaceChildren();
  if (!state.syntheses.length) {
    const empty = create("div", "synthesis-empty");
    empty.append(
      create("strong", "", "还没有知识体系"),
      create("p", "", "先通过 backbone-register 注册一份权威教材。"),
    );
    host.append(empty);
    return;
  }
  for (const item of state.syntheses) {
    const coverage = item.coverage || {};
    const card = create("button", "synthesis-card");
    card.type = "button";
    const label = create("span", "synthesis-card-label", "SYNTHESIS");
    const title = create("strong", "", item.title);
    const description = create("p", "", item.description);
    const metrics = create("div", "synthesis-card-metrics");
    metrics.append(
      create("span", "", `${coverage.concept_nodes || 0} Concepts`),
      create("span", "", `${Math.round(Number(coverage.coverage || 0) * 100)}% 已建立`),
      create("span", `synthesis-status ${String(item.status || "").toLowerCase()}`, item.status || "EMPTY"),
    );
    card.append(label, title, description, metrics);
    card.addEventListener("click", () => openSynthesis(item.synthesis_id));
    host.append(card);
  }
}

function setPrimaryView(view) {
  $("#workbench-view").hidden = view !== "workbench";
  $("#reader-view").hidden = view !== "source";
  $("#synthesis-view").hidden = view !== "syntheses";
  $("#synthesis-reader-view").hidden = view !== "synthesis-reader";
  $("#workbench-navigation").hidden = !["workbench", "syntheses"].includes(view);
  $("#reader-navigation").hidden = view !== "source";
  $("#synthesis-navigation").hidden = view !== "synthesis-reader";
  $("#job-drawer").hidden = !["workbench", "source"].includes(view);
  $("#open-syntheses").classList.toggle("active", ["syntheses", "synthesis-reader"].includes(view));
}

function showSynthesisLibrary() {
  releaseSourcePdfBlob();
  state.sourceNote = null;
  state.selectedSource = null;
  state.synthesis = null;
  state.concept = null;
  setPrimaryView("syntheses");
  renderSynthesisCards();
  history.replaceState(null, "", "#syntheses");
  updateAgentContext();
}

function flattenSynthesisNodes(nodes) {
  return nodes.flatMap((node) => [node, ...flattenSynthesisNodes(node.children || [])]);
}

function renderSynthesisOutline(nodes, host) {
  for (const node of nodes) {
    const wrapper = create("div", `synthesis-outline-node kind-${String(node.node_kind).toLowerCase()}`);
    const button = create("button", "synthesis-outline-link");
    button.type = "button";
    button.dataset.level = String(node.level);
    button.dataset.conceptId = node.concept_id || "";
    button.dataset.conceptVersionId = node.concept_version_id || "";
    button.append(
      create("span", "synthesis-outline-number", String(node.original_title || "").match(/^((?:[IVX]+|A|\d+(?:\.\d+)*))\s/)?.[1] || ""),
      create("span", "synthesis-outline-title", node.title),
    );
    if (node.concept_id) button.addEventListener("click", () => openConcept(
      node.concept_id,
      { versionId: node.concept_version_id || null },
    ));
    else button.setAttribute("aria-expanded", "true");
    wrapper.append(button);
    if (node.children?.length) {
      const children = create("div", "synthesis-outline-children");
      renderSynthesisOutline(node.children, children);
      wrapper.append(children);
    }
    host.append(wrapper);
  }
}

async function openSynthesis(synthesisId, preferredConceptId = null) {
  try {
    state.synthesis = await request(`${API}/syntheses/${synthesisId}`);
    state.concept = null;
    setPrimaryView("synthesis-reader");
    $("#synthesis-spine-title").textContent = state.synthesis.title;
    const coverage = state.synthesis.coverage || {};
    const coveredConcepts = coverage.published_concepts ?? coverage.seeded_concepts ?? 0;
    $("#synthesis-spine-coverage").textContent = `${coveredConcepts} / ${coverage.concept_nodes || 0} Concepts`;
    const outline = $("#synthesis-outline");
    outline.replaceChildren();
    renderSynthesisOutline(state.synthesis.outline || [], outline);
    history.replaceState(null, "", `#synthesis/${encodeURIComponent(synthesisId)}`);
    const firstNode = flattenSynthesisNodes(state.synthesis.outline || []).find(
      (item) => item.concept_id && (!preferredConceptId || item.concept_id === preferredConceptId),
    );
    if (firstNode) await openConcept(firstNode.concept_id, {
      updateHash: false,
      versionId: firstNode.concept_version_id || null,
    });
  } catch (error) {
    toast(error.message, "error");
    showSynthesisLibrary();
  }
}

function conceptBlockLabel(block) {
  const labels = {
    FORMULA: "公式", ALGORITHM: "算法", EXAMPLE: "例子", TABLE: "对照",
    FIGURE: "图示", LIMITATION: "边界与限制", PROSE: "", LIST: "要点",
    PROCEDURE: "步骤", CODE: "代码", CALLOUT: "补充", ASSET: "图示",
    CONCEPT_REF: "相关概念",
  };
  return block.title || labels[block.block_type] || "";
}

function appendConceptAsset(parent, asset, blockTitle, conceptTitle) {
  const figure = create("figure", "concept-source-figure concept-inline-figure");
  const image = create("img");
  image.src = asset.asset_url;
  image.alt = asset.alt_text || asset.caption || blockTitle || conceptTitle;
  image.loading = "lazy";
  image.decoding = "async";
  const caption = create("figcaption");
  const meta = create("div", "concept-source-figure-meta");
  meta.append(
    create("strong", "", blockTitle || "教材图示"),
    create("span", "", `教材 P.${asset.page_number}`),
  );
  const captionText = create("p");
  renderMarkdown(captionText, asset.caption || asset.alt_text || "");
  caption.append(meta, captionText);
  figure.append(image, caption);
  parent.append(figure);
}

function renderConceptContentBlock(item, assetsById, renderedAssets, context) {
  const type = String(item.type || "PROSE").toUpperCase();
  const wrap = create("div", `concept-content-block content-type-${type.toLowerCase()}`);
  wrap.dataset.blockKey = item.block_key || "";
  if (["PROSE", "EXAMPLE", "CALLOUT"].includes(type)) {
    const target = type === "CALLOUT" ? create("blockquote", "concept-callout") : wrap;
    renderMarkdown(target, item.markdown || "");
    if (target !== wrap) wrap.append(target);
  } else if (type === "LIST") {
    const list = document.createElement("ul");
    for (const value of item.items || []) {
      const row = document.createElement("li");
      renderMarkdown(row, value, { inline: true });
      list.append(row);
    }
    wrap.append(list);
  } else if (type === "TABLE") {
    const region = create("div", "markdown-scroll-region markdown-table-region");
    region.tabIndex = 0;
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const value of item.table_headers || []) {
      const cell = document.createElement("th");
      renderMarkdown(cell, String(value), { inline: true });
      headRow.append(cell);
    }
    head.append(headRow);
    const body = document.createElement("tbody");
    for (const values of item.table_rows || []) {
      const row = document.createElement("tr");
      for (const value of values) {
        const cell = document.createElement("td");
        renderMarkdown(cell, String(value), { inline: true });
        row.append(cell);
      }
      body.append(row);
    }
    table.append(head, body);
    region.append(table);
    wrap.append(region);
  } else if (type === "FORMULA") {
    const formula = create("div", "formula-block noodle-math-display");
    renderKatexNode(formula, stripMathDelimiters(item.latex || ""), true);
    wrap.append(formula);
    if (item.markdown) {
      const explanation = create("div", "concept-formula-explanation");
      renderMarkdown(explanation, item.markdown);
      wrap.append(explanation);
    }
    if (item.symbols?.length) {
      const symbols = create("dl", "concept-symbols");
      for (const symbol of item.symbols) {
        const term = document.createElement("dt");
        const definition = document.createElement("dd");
        // Symbol names are LaTeX expressions, including legacy values without delimiters.
        renderKatexNode(term, stripMathDelimiters(symbol.symbol || ""), false);
        renderMarkdown(definition, symbol.meaning || symbol.description || "", { inline: true });
        symbols.append(term, definition);
      }
      wrap.append(symbols);
    }
  } else if (type === "PROCEDURE") {
    const list = document.createElement("ol");
    for (const value of item.steps || []) {
      const row = document.createElement("li");
      renderMarkdown(row, value, { inline: true });
      list.append(row);
    }
    wrap.append(list);
  } else if (type === "IMAGE") {
    // The figure owns its caption; do not print IMAGE.text a second time.
    if (!(block.assets || []).length) renderMarkdown(wrap, block.text || block.caption || "");
  } else if (type === "CODE") {
    const pre = document.createElement("pre");
    const code = create("code", "", item.code || "");
    if (item.language) code.dataset.language = item.language;
    pre.append(code);
    wrap.append(pre);
  } else if (type === "CONCEPT_REF") {
    wrap.append(create("code", "concept-reference", item.concept_key || ""));
  }
  for (const assetId of item.asset_ids || []) {
    const asset = assetsById.get(assetId);
    if (!asset || renderedAssets.has(assetId)) continue;
    appendConceptAsset(wrap, asset, context.blockTitle, context.conceptTitle);
    renderedAssets.add(assetId);
  }
  if (item.conflict) {
    const conflict = create("aside", `concept-conflict ${String(item.conflict.status || "unresolved").toLowerCase()}`);
    conflict.append(create("strong", "", item.conflict.status === "RESOLVED" ? "来源分歧 · 已处理" : "来源分歧 · 待确认"));
    const list = document.createElement("ul");
    for (const position of item.conflict.positions || []) list.append(create("li", "", position.claim || ""));
    conflict.append(list);
    if (item.conflict.resolution) conflict.append(create("p", "", item.conflict.resolution));
    wrap.append(conflict);
  }
  if (item.evidence_details?.length) {
    const evidence = create("div", "concept-evidence concept-content-evidence");
    evidence.append(create("span", "", "精确证据"));
    for (const detail of item.evidence_details) {
      const label = detail.page_number ? `PDF P.${detail.page_number}` : detail.evidence_span_id;
      const chip = create("small", "", label);
      chip.title = detail.quote || detail.evidence_level || "";
      evidence.append(chip);
    }
    wrap.append(evidence);
  }
  enhanceScrollableMarkdown(wrap);
  return wrap;
}

function renderConceptDiff(diff, target) {
  target.replaceChildren();
  if (!diff.diff_available) {
    target.append(create("p", "concept-ops-empty", "候选版本尚未生成，Worker 完成后即可查看 Diff。"));
    return;
  }
  const changed = (diff.modules || []).filter((item) => item.status !== "UNCHANGED");
  if (!changed.length) {
    target.append(create("p", "concept-ops-empty", "候选版本与当前版本没有内容差异。"));
    return;
  }
  for (const item of changed) {
    const card = create("article", "concept-diff-card");
    card.append(create("h4", "", `${item.title || item.module_key} · ${item.status}`));
    const columns = create("div", "concept-diff-columns");
    for (const [label, value] of [["发布前", item.before], ["候选版", item.after]]) {
      const column = create("section", "concept-diff-column");
      column.append(create("strong", "", label), create("pre", "", value || "（空）"));
      columns.append(column);
    }
    card.append(columns);
    target.append(card);
  }
}

async function appendConceptOperations(body) {
  const concept = state.concept;
  if (!concept?.concept_id || !["concept-v2", "concept-v3"].includes(concept.schema_version)) return;
  const panel = create("details", "concept-ops");
  const heading = create("summary", "concept-ops-heading");
  heading.append(
    create("div", "", "来源与更新"),
    create("small", "", "检索只生成候选，不会自动写入"),
  );
  const controls = create("div", "concept-ops-controls");
  const select = create("select");
  select.setAttribute("aria-label", "选择 Concept Module");
  for (const block of concept.blocks || []) {
    const option = document.createElement("option");
    option.value = block.block_key;
    option.textContent = block.title || block.block_key;
    select.append(option);
  }
  const searchButton = create("button", "secondary-button", "查找 AtomicNote 候选");
  const candidates = create("div", "concept-candidate-list");
  const updates = create("div", "concept-update-list");
  const contributions = create("div", "concept-contribution-list");
  const diff = create("div", "concept-diff");
  controls.append(select, searchButton);
  panel.append(heading, controls, candidates, contributions, updates, diff);
  body.append(panel);

  searchButton.addEventListener("click", async () => {
    searchButton.disabled = true;
    candidates.replaceChildren(create("p", "concept-ops-empty", "正在检索…"));
    try {
      const rows = await request(
        `${API}/concepts/${encodeURIComponent(concept.concept_id)}/candidates?module_key=${encodeURIComponent(select.value)}&limit=8`,
      );
      candidates.replaceChildren();
      candidates.append(create("h3", "", `候选来源 · ${rows.length}`));
      if (!rows.length) candidates.append(create("p", "concept-ops-empty", "暂无未关联的候选 AtomicNote。"));
      for (const row of rows) {
        const card = create("article", "concept-candidate-card");
        const meta = create("div", "concept-candidate-meta");
        meta.append(
          create("span", "", `相关度 ${(Number(row.score || 0) * 100).toFixed(1)}%`),
          create("span", "", `${row.evidence_count} 条证据 · ${row.asset_count} 张图`),
        );
        card.append(create("strong", "", row.title), create("p", "", row.thesis), meta);
        card.title = `NoteVersion ${row.note_version_id}`;
        candidates.append(card);
      }
    } catch (error) {
      candidates.replaceChildren(create("p", "concept-ops-empty", error.message));
    } finally {
      searchButton.disabled = false;
    }
  });

  try {
    const rows = await request(`${API}/concepts/${encodeURIComponent(concept.concept_id)}/contributions`);
    contributions.append(create("h3", "", `来源生命周期 · ${rows.length}`));
    for (const row of rows) {
      const item = create("div", "concept-contribution-row");
      item.append(
        create("strong", "", row.note_title || row.contribution_id),
        create("span", "", row.module_key),
        create("b", `status-${String(row.decision_status || "").toLowerCase()}`, row.decision_status),
      );
      item.title = `${row.contribution_id}\n${row.rationale || ""}`;
      contributions.append(item);
    }
  } catch (error) {
    contributions.append(create("p", "concept-ops-empty", error.message));
  }

  try {
    const rows = await request(`${API}/concepts/${encodeURIComponent(concept.concept_id)}/updates?limit=8`);
    updates.append(create("h3", "", `最近更新 · ${rows.length}`));
    if (!rows.length) updates.append(create("p", "concept-ops-empty", "尚无 AtomicNote 更新记录。"));
    for (const row of rows) {
      const button = create("button", "concept-update-row");
      const judge = (row.judges || []).at(-1);
      button.append(
        create("strong", "", `${row.module_key} · ${row.status}`),
        create("span", "", judge ? `Judge ${judge.decision}` : "等待质量门禁"),
        create("small", "", formatDate(row.updated_at)),
      );
      button.addEventListener("click", async () => {
        diff.replaceChildren(create("p", "concept-ops-empty", "正在载入版本差异…"));
        try {
          renderConceptDiff(await request(`${API}/concept-updates/${encodeURIComponent(row.job_id)}/diff`), diff);
        } catch (error) {
          diff.replaceChildren(create("p", "concept-ops-empty", error.message));
        }
      });
      updates.append(button);
    }
  } catch (error) {
    updates.append(create("p", "concept-ops-empty", error.message));
  }
}

async function openConcept(conceptId, { updateHash = true, versionId = null } = {}) {
  try {
    const versionQuery = versionId ? `?version_id=${encodeURIComponent(versionId)}` : "";
    state.concept = await request(`${API}/concepts/${conceptId}${versionQuery}`);
    $$(".synthesis-outline-link").forEach((item) => item.classList.toggle("active", item.dataset.conceptId === conceptId));
    $("#concept-title").textContent = state.concept.title;
    $("#concept-toolbar-title").textContent = state.concept.title;
    $("#concept-original-title").textContent = state.concept.original_title;
    const scope = state.concept.scope;
    $("#concept-scope").textContent = typeof scope === "string"
      ? scope
      : [
          ...(scope?.includes || []),
          ...(scope?.excludes?.length ? [`不包含：${scope.excludes.join("；")}`] : []),
        ].join("；");
    $("#concept-version-chip").textContent = `Concept v${state.concept.version_no} · ${state.concept.status}`;
    const body = $("#concept-body");
    body.replaceChildren();
    const lead = create("div", "concept-summary");
    renderMarkdown(lead, state.concept.summary || "");
    body.append(lead);
    if (state.concept.sync_status === "HISTORICAL") {
      body.append(create("p", "concept-coverage-notice", "当前展示历史固定版本，来源引用记录当时状态，不代表仍可用于当前检索。"));
    }
    if (state.concept.sync_status && !["READY", "HISTORICAL"].includes(state.concept.sync_status)) {
      $("#concept-version-chip").textContent += state.concept.sync_status === "REBUILDING" ? " · 重建中" : " · 待处理";
    }
    if (state.concept.content_level === "OVERVIEW") {
      body.append(create("p", "concept-coverage-notice", "当前版本为概述；详细机制和操作请结合关联来源阅读。"));
    }
    if (!["concept-v2", "concept-v3"].includes(state.concept.schema_version) && state.concept.figures?.length) {
      const gallery = create("section", "concept-figure-gallery");
      const galleryHeading = create("div", "concept-figure-gallery-heading");
      galleryHeading.append(
        create("h2", "", "教材图示"),
        create("span", "", `${state.concept.figures.length} 幅 · 原书页码裁剪`),
      );
      gallery.append(galleryHeading);
      for (const figure of state.concept.figures) {
        const card = create("figure", "concept-source-figure");
        const image = create("img");
        image.src = figure.asset_url;
        image.alt = figure.caption || `Figure ${figure.figure_label}`;
        image.loading = "lazy";
        image.decoding = "async";
        const caption = create("figcaption");
        const meta = create("div", "concept-source-figure-meta");
        meta.append(
          create("strong", "", `Figure ${figure.figure_label}`),
          create("span", "", `教材 P.${figure.page_number}`),
        );
        caption.append(meta, create("p", "", figure.caption));
        card.append(image, caption);
        gallery.append(card);
      }
      body.append(gallery);
    }
    for (const block of state.concept.blocks || []) {
      const section = create("section", `concept-block type-${block.block_type.toLowerCase()}`);
      const label = conceptBlockLabel(block);
      if (label) section.append(create("h2", "concept-block-heading", label));
      const content = create("div", "markdown-body concept-block-content");
      const assetsById = new Map((block.assets || []).map((asset) => [asset.asset_id, asset]));
      const renderedAssets = new Set();
      if (block.content_blocks?.length) {
        for (const item of block.content_blocks) {
          content.append(renderConceptContentBlock(item, assetsById, renderedAssets, {
            blockTitle: block.title,
            conceptTitle: state.concept.title,
          }));
        }
      } else {
        renderMarkdown(content, block.content);
      }
      section.append(content);
      for (const asset of block.assets || []) {
        if (renderedAssets.has(asset.asset_id)) continue;
        appendConceptAsset(section, asset, block.title, state.concept.title);
        renderedAssets.add(asset.asset_id);
      }
      if (block.evidence?.length) {
        const evidence = create("div", "concept-evidence");
        evidence.append(create("span", "", "来源"));
        for (const item of block.evidence) {
          const pages = item.source_locator?.pages || [];
          evidence.append(create("small", "", pages.length ? `教材 P.${pages[0]}–${pages[1]}` : item.source_ref));
        }
        section.append(evidence);
      }
      body.append(section);
    }
    await appendConceptOperations(body);
    if (updateHash && state.synthesis) {
      history.replaceState(null, "", `#synthesis/${encodeURIComponent(state.synthesis.synthesis_id)}/concept/${encodeURIComponent(conceptId)}`);
    }
    $("#synthesis-reader-view").scrollTop = 0;
  } catch (error) {
    toast(error.message, "error");
  }
}

function openUpload() {
  $("#upload-zone").hidden = false;
  $("#pdf-file").focus();
}

function closeUpload() {
  $("#upload-zone").hidden = true;
  $("#upload-form").reset();
  $("#pdf-file-name").textContent = "或选择一个来源文件";
  $("#upload-status").textContent = "上传后由 Worker 在后台处理。";
}

async function uploadPdf(event) {
  event.preventDefault();
  const file = $("#pdf-file").files[0];
  if (!file) return toast("请先选择 PDF", "error");
  const button = $("#upload-button");
  button.disabled = true;
  button.textContent = "正在上传…";
  try {
    const form = new FormData();
    form.append("file", file, file.name);
    const title = $("#pdf-title").value.trim();
    const query = title ? `?title=${encodeURIComponent(title)}` : "";
    const result = await request(`${API}/sources/pdf${query}`, { method: "POST", body: form });
    const folderId = $("#upload-folder").value || null;
    if (folderId) await request(`${API}/sources/${result.source_id}/folder`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id: folderId }),
    });
    state.selectedJobId = result.job_id;
    localStorage.setItem("noodle.ui.job", result.job_id);
    state.drawerOpen = true;
    closeUpload();
    toast("PDF 已进入后台处理队列");
    await loadLibrary({ quiet: true });
    applyDrawerState();
    await selectJob(result.job_id);
  } catch (error) {
    $("#upload-status").textContent = error.message;
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "开始归纳";
  }
}

function sourceForJob(jobId) {
  return state.sources.find((source) => source.job?.id === jobId);
}

function renderJobs() {
  const running = state.jobs.filter((job) => runningStatuses.has(job.status));
  $("#job-pulse").classList.toggle("running", running.length > 0);
  $("#job-drawer-subtitle").textContent = running.length
    ? `${running.length} 个任务正在处理`
    : state.jobs.length ? `最近 ${state.jobs.length} 条记录` : "暂无运行任务";
  const list = $("#job-list");
  list.replaceChildren();
  for (const job of state.jobs) {
    const source = sourceForJob(job.id);
    const displayJob = source?.job || job;
    const button = create("button", `job-row ${job.id === state.selectedJobId ? "active" : ""}`);
    button.type = "button";
    button.append(
      create("strong", "", source?.title || `Job ${job.id.slice(0, 8)}`),
      create("small", "", `${stageLabels[displayJob.stage] || displayJob.stage} · ${Math.round((displayJob.progress || 0) * 100)}%`),
    );
    const progress = create("span", "mini-progress");
    const fill = create("span");
    fill.style.width = `${Math.max(2, Math.round((displayJob.progress || 0) * 100))}%`;
    fill.classList.toggle("running", runningStatuses.has(displayJob.status));
    progress.append(fill);
    const liveMeta = create("span", "job-live-meta");
    liveMeta.append(
      create("span", "", runningStatuses.has(displayJob.status) ? "已运行" : "用时"),
      createElapsedClock(displayJob),
    );
    button.append(progress, liveMeta);
    button.addEventListener("click", () => selectJob(job.id));
    list.append(button);
  }
}

async function selectJob(jobId) {
  state.selectedJobId = jobId;
  state.drawerOpen = true;
  localStorage.setItem("noodle.ui.job", jobId);
  renderJobs();
  applyDrawerState();
  $("#job-detail").innerHTML = '<p class="empty-detail">正在读取任务时间线…</p>';
  await refreshSelectedJobDetail();
}

async function refreshSelectedJobDetail({ quiet = false } = {}) {
  const jobId = state.selectedJobId;
  if (!jobId || state.timelinePolling) return;
  state.timelinePolling = true;
  try {
    const timeline = await request(`${API}/jobs/${jobId}/timeline`);
    if (state.selectedJobId !== jobId) return;
    renderJobDetail(timeline);
  } catch (error) {
    if (!quiet) $("#job-detail").innerHTML = `<div class="job-error">${escapeHtml(error.message)}</div>`;
  } finally {
    state.timelinePolling = false;
  }
}

function renderJobDetail(timeline) {
  const job = timeline.job;
  const source = sourceForJob(job.id);
  const detail = $("#job-detail");
  detail.replaceChildren();
  const metrics = create("div", "job-metrics");
  const values = [
    ["文件", source?.source_file || job.id.slice(0, 8)],
    ["阶段", stageLabels[job.stage] || job.stage],
    ["Token", formatTokens(timeline.summary.total_tokens)],
    ["LLM 调用", String(timeline.summary.llm_call_count)],
    ["视觉调用", String(timeline.summary.vision_call_count)],
  ];
  for (const [label, value] of values) {
    const item = create("div", "job-metric");
    item.append(create("span", "", label), create("b", "", value));
    metrics.append(item);
  }
  const elapsed = create("div", "job-metric");
  elapsed.title = "从文件上传创建任务开始，到当前时刻或任务结束的墙钟时间；包含排队、MinerU、模型调用和本地处理";
  elapsed.append(
    create("span", "", terminalStatuses.has(job.status) ? "总用时" : "已运行"),
    createElapsedClock(job, "job-detail-clock", "b"),
  );
  metrics.append(elapsed);
  const track = create("div", "stage-track");
  for (const stage of timeline.stages || []) {
    const statusClass = stage.status === "FAILED" ? "failed" : stage.status === "COMPLETED" ? "done" : "running";
    const item = create("div", `stage-pill ${statusClass}`);
    item.append(create("strong", "", stageLabels[stage.stage] || stage.stage), create("span", "", formatDuration(stage.duration_ms)));
    item.title = JSON.stringify(stage.detail || {}, null, 2);
    track.append(item);
  }
  detail.append(metrics, track);
  const warnings = renderJobWarnings(job);
  if (warnings) detail.append(warnings);
  if (job.error_code || job.error_detail) detail.append(create("div", "job-error", `${job.error_code || "ERROR"}\n${job.error_detail || ""}`));
}

function applyDrawerState() {
  $("#job-drawer").classList.toggle("collapsed", !state.drawerOpen);
  $("#job-drawer-toggle").setAttribute("aria-expanded", String(state.drawerOpen));
  $("#job-drawer-arrow").textContent = state.drawerOpen ? "⌄" : "⌃";
  localStorage.setItem("noodle.ui.drawer", String(state.drawerOpen));
}

function showWorkbench() {
  releaseSourcePdfBlob();
  state.sourceNote = null;
  state.navigationIndex = null;
  setReadingAnchor(null);
  setPrimaryView("workbench");
  applyContentMode("note");
  updateAgentContext();
}

async function openSource(source) {
  if (!source.source_note_id) {
    if (source.job?.id) {
      toast(terminalStatuses.has(source.job.status) ? "该任务还没有可读取的 SourceNote" : "文件仍在处理中，已打开任务进度");
      await selectJob(source.job.id);
    }
    return;
  }
  if (state.selectedSource?.source_id !== source.source_id) releaseSourcePdfBlob();
  state.selectedSource = source;
  state.pdfPage = 1;
  state.navigationIndex = null;
  setReadingAnchor(null);
  setPrimaryView("source");
  applyContentMode("note");
  $("#note-title").textContent = "正在读取…";
  $("#reader-toolbar-title").textContent = source.title || source.file_name || "正在读取…";
  history.replaceState(null, "", `#source/${encodeURIComponent(source.source_id)}`);
  try {
    await reloadOpenSource(source, { resetScroll: true });
  } catch (error) {
    toast(error.message, "error");
    showWorkbench();
  }
}

async function reloadOpenSource(source, { announce = false, resetScroll = false } = {}) {
  if (state.sourceNoteRefreshing) return false;
  state.sourceNoteRefreshing = true;
  const reader = $("#reader-view");
  const previousScrollTop = reader.scrollTop;
  const expectedSourceId = source.source_id;
  try {
    const note = await request(`${API}/source-notes/${source.source_note_id}`);
    if (state.selectedSource?.source_id !== expectedSourceId) return false;
    state.sourceNote = note;
    renderSourceNote();
    updateAgentContext();
    window.requestAnimationFrame(() => {
      const maximum = Math.max(0, reader.scrollHeight - reader.clientHeight);
      reader.scrollTop = resetScroll ? 0 : Math.min(previousScrollTop, maximum);
    });
    if (announce) toast(`已更新至 SourceNote v${note.version_no}`);
    return true;
  } finally {
    state.sourceNoteRefreshing = false;
  }
}

const codeTokenPattern = /```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^\n]*?`+/g;
const mathTokenPattern = /(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$|\\\[([\s\S]*?)\\\]|\\\(([\s\S]*?)\\\)|(?<![\\$])\$(?![\s$])((?:\\.|[^$\n])+?)(?<!\s)\$/g;

function restoreTextTokens(target, prefix, values) {
  const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  const tokenPattern = new RegExp(`${prefix}(\\d+)END`, "g");
  for (const textNode of nodes) {
    if (!textNode.nodeValue?.includes(prefix)) continue;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of textNode.nodeValue.matchAll(tokenPattern)) {
      fragment.append(document.createTextNode(textNode.nodeValue.slice(cursor, match.index)));
      fragment.append(document.createTextNode(values[Number(match[1])] || match[0]));
      cursor = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(textNode.nodeValue.slice(cursor)));
    textNode.replaceWith(fragment);
  }
}

function renderKatexNode(node, expression, displayMode) {
  if (!window.katex) {
    node.textContent = displayMode ? `$$${expression}$$` : `$${expression}$`;
    return;
  }
  try {
    window.katex.render(expression.trim(), node, {
      displayMode,
      throwOnError: true,
      strict: "ignore",
      trust: false,
      output: "htmlAndMathml",
    });
  } catch (error) {
    node.classList.add("math-render-warning");
    node.textContent = expression;
    node.title = `公式渲染失败：${error.message}`;
  }
}

function stripMathDelimiters(value) {
  const source = String(value || "").trim();
  if (source.startsWith("$$") && source.endsWith("$$")) return source.slice(2, -2);
  if (/^\$[^$][\s\S]*\$$/.test(source)) return source.slice(1, -1);
  if ((source.startsWith("\\[") && source.endsWith("\\]")) || (source.startsWith("\\(") && source.endsWith("\\)"))) return source.slice(2, -2);
  return source;
}

function enhanceScrollableMarkdown(target) {
  for (const table of target.querySelectorAll("table")) {
    if (table.parentElement?.classList.contains("markdown-table-region")) continue;
    const region = document.createElement("div");
    region.className = "markdown-scroll-region markdown-table-region";
    region.tabIndex = 0;
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", "可横向滚动的表格");
    table.before(region);
    region.append(table);
  }
  for (const node of target.querySelectorAll("pre, .noodle-math-display")) {
    node.classList.add("markdown-scroll-region");
    node.tabIndex = 0;
    node.setAttribute("role", "region");
    node.setAttribute("aria-label", node.matches("pre") ? "可横向滚动的代码" : "可横向滚动的公式");
  }
}

function renderMarkdown(target, source, { inline = false } = {}) {
  const value = String(source || "");
  if (!window.marked || !window.DOMPurify) { target.textContent = value; return; }
  const codeSegments = [];
  const codeProtected = value.replace(codeTokenPattern, (segment) => {
    const token = `NOODLECODETOKEN${codeSegments.length}END`;
    const fenced = segment.match(/^(```|~~~)([^\n]*)\n([\s\S]*?)(?:\n)?\1$/);
    if (fenced) {
      codeSegments.push(fenced[3]);
      return `${fenced[1]}${fenced[2]}\n${token}\n${fenced[1]}`;
    }
    const ticks = segment.match(/^`+/)?.[0] || "`";
    codeSegments.push(segment.slice(ticks.length, -ticks.length));
    return `${ticks}${token}${ticks}`;
  });
  const expressions = [];
  const protectedValue = codeProtected.replace(mathTokenPattern, (whole, blockDollar, blockBracket, inlineParen, inlineDollar) => {
    const expression = blockDollar ?? blockBracket ?? inlineParen ?? inlineDollar ?? whole;
    const display = blockDollar !== undefined || blockBracket !== undefined;
    const index = expressions.push({ expression, display }) - 1;
    return display
      ? `\n\n<div class="noodle-math-display" data-math-id="${index}"></div>\n\n`
      : `<span class="noodle-math-inline" data-math-id="${index}"></span>`;
  });
  const html = inline ? window.marked.parseInline(protectedValue, { gfm: true, breaks: true }) : window.marked.parse(protectedValue, { gfm: true, breaks: true });
  target.innerHTML = window.DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ["data-math-id"],
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed"],
  });
  restoreTextTokens(target, "NOODLECODETOKEN", codeSegments);
  for (const node of target.querySelectorAll("[data-math-id]")) {
    const item = expressions[Number(node.dataset.mathId)];
    if (item) renderKatexNode(node, item.expression, item.display);
    node.removeAttribute("data-math-id");
  }
  enhanceScrollableMarkdown(target);
}

function assetPages(asset) {
  const values = (asset.source_pages || [asset.page_label]).filter(Boolean).map((item) => String(item).replace(/^p\.?/i, ""));
  return [...new Set(values)].join("、") || "?";
}

function parsePdfPage(value) {
  const match = String(value ?? "").match(/\d+/);
  return match ? Math.max(1, Number(match[0])) : 1;
}

function physicalPage(anchor) {
  return Number.isFinite(Number(anchor?.page_index)) ? Number(anchor.page_index) + 1 : parsePdfPage(anchor?.page_label);
}

function navigationAnchorPriority(anchor) {
  return [
    anchor?.evidence_role === "PRIMARY" ? 0 : 1,
    anchor?.anchor_kind === "EVIDENCE" ? 0 : 1,
    anchor?.block_key ? 0 : 1,
    physicalPage(anchor),
  ];
}

function compareNavigationAnchors(left, right) {
  const a = navigationAnchorPriority(left);
  const b = navigationAnchorPriority(right);
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return 0;
}

function addNavigationIndexEntry(map, key, anchor) {
  if (!key) return;
  if (!map.has(key)) map.set(key, []);
  const values = map.get(key);
  if (!values.some((item) => item.outline_node_id === anchor.outline_node_id
    && item.note_id === anchor.note_id
    && item.block_key === anchor.block_key
    && physicalPage(item) === physicalPage(anchor)
    && item.anchor_kind === anchor.anchor_kind)) values.push(anchor);
}

function buildNavigationIndex(sourceNote) {
  const index = {
    anchors: [],
    byPage: new Map(),
    byBlock: new Map(),
    byNote: new Map(),
    byOutline: new Map(),
  };
  const apiAnchors = Array.isArray(sourceNote?.navigation_anchors) ? sourceNote.navigation_anchors : [];
  const anchors = apiAnchors.length ? apiAnchors : [];

  function collectFallback(node) {
    for (const note of node.notes || []) {
      for (const block of note.blocks || []) {
        for (const evidence of block.evidence || []) anchors.push({
          outline_node_id: node.outline_node_id,
          note_id: note.note_id,
          block_key: block.block_key,
          page_index: evidence.page_index,
          page_label: evidence.page_label,
          anchor_kind: "EVIDENCE",
          evidence_role: evidence.role || "PRIMARY",
          confidence: "EXACT",
        });
        for (const asset of block.assets || []) anchors.push({
          outline_node_id: node.outline_node_id,
          note_id: note.note_id,
          block_key: block.block_key,
          page_index: asset.page_index,
          page_label: asset.page_label,
          anchor_kind: "ASSET",
          evidence_role: "PRIMARY",
          confidence: "EXACT",
        });
      }
      if (!(note.blocks || []).some((block) => (block.evidence || []).length)) {
        for (const evidence of note.evidence || []) anchors.push({
          outline_node_id: node.outline_node_id,
          note_id: note.note_id,
          block_key: evidence.block_key || null,
          page_index: evidence.page_index,
          page_label: evidence.page_label,
          anchor_kind: "EVIDENCE",
          evidence_role: evidence.role || "PRIMARY",
          confidence: evidence.block_key ? "EXACT" : "NOTE",
        });
      }
    }
    for (const child of node.children || []) collectFallback(child);
  }
  if (!anchors.length) for (const node of sourceNote?.outline || []) collectFallback(node);

  index.anchors = anchors
    .filter((anchor) => Number.isFinite(Number(anchor.page_index)) && Number(anchor.page_index) >= 0)
    .map((anchor) => ({ ...anchor, page_index: Number(anchor.page_index) }))
    .sort((left, right) => physicalPage(left) - physicalPage(right) || compareNavigationAnchors(left, right));
  for (const anchor of index.anchors) {
    addNavigationIndexEntry(index.byPage, physicalPage(anchor), anchor);
    addNavigationIndexEntry(index.byBlock, anchor.block_key, anchor);
    addNavigationIndexEntry(index.byNote, anchor.note_id, anchor);
    addNavigationIndexEntry(index.byOutline, anchor.outline_node_id, anchor);
  }

  function aggregateOutline(node) {
    const values = [...(index.byOutline.get(node.outline_node_id) || [])];
    for (const child of node.children || []) values.push(...aggregateOutline(child));
    const unique = [];
    for (const anchor of values.sort(compareNavigationAnchors)) {
      if (!unique.some((item) => item.note_id === anchor.note_id
        && item.block_key === anchor.block_key
        && physicalPage(item) === physicalPage(anchor))) unique.push(anchor);
    }
    index.byOutline.set(node.outline_node_id, unique);
    return unique;
  }
  for (const node of sourceNote?.outline || []) aggregateOutline(node);
  for (const values of [...index.byPage.values(), ...index.byBlock.values(), ...index.byNote.values(), ...index.byOutline.values()]) {
    values.sort(compareNavigationAnchors);
  }
  state.navigationIndex = index;
  return index;
}

function chooseNavigationAnchor(candidates, context = state.readingAnchor) {
  if (!candidates?.length) return null;
  return [...candidates].sort((left, right) => {
    const contextScore = (anchor) => (anchor.note_id === context?.note_id ? -4 : 0)
      + (anchor.outline_node_id === context?.outline_node_id ? -2 : 0)
      + (anchor.block_key && anchor.block_key === context?.block_key ? -8 : 0);
    return contextScore(left) - contextScore(right) || compareNavigationAnchors(left, right);
  })[0];
}

function anchorForPdfPage(pageNumber) {
  const index = state.navigationIndex;
  if (!index?.anchors.length) return null;
  const exact = chooseNavigationAnchor(index.byPage.get(pageNumber));
  if (exact) return exact;
  const nearest = [...index.anchors].sort((left, right) => {
    const leftPage = physicalPage(left);
    const rightPage = physicalPage(right);
    const leftDistance = Math.abs(leftPage - pageNumber);
    const rightDistance = Math.abs(rightPage - pageNumber);
    if (leftDistance !== rightDistance) return leftDistance - rightDistance;
    if ((leftPage <= pageNumber) !== (rightPage <= pageNumber)) return leftPage <= pageNumber ? -1 : 1;
    return compareNavigationAnchors(left, right);
  })[0];
  return nearest ? { ...nearest, confidence: "FALLBACK" } : null;
}

function highlightNavigation(anchor) {
  $$(".outline-link").forEach((button) => {
    const exactNote = anchor?.note_id && button.dataset.noteId === anchor.note_id;
    const outline = anchor?.outline_node_id && button.dataset.outlineNodeId === anchor.outline_node_id;
    button.classList.toggle("navigation-active", Boolean(exactNote));
    button.classList.toggle("navigation-parent", Boolean(outline && !button.dataset.noteId));
  });
}

function setReadingAnchor(anchor, origin = "unknown") {
  state.readingAnchor = anchor ? { ...anchor, origin } : null;
  highlightNavigation(state.readingAnchor);
  return state.readingAnchor;
}

function beginNavigationGuard(surface, duration = 700) {
  state.navigationGuard = { surface, until: performance.now() + duration };
}

function navigationGuarded(surface) {
  if (!state.navigationGuard || performance.now() >= state.navigationGuard.until) {
    state.navigationGuard = null;
    return false;
  }
  return state.navigationGuard.surface === surface;
}

function findNoteAnchorAtReadingLine() {
  const reader = $("#reader-view");
  if (!reader || state.contentMode !== "note" || !state.navigationIndex) return null;
  const readerRect = reader.getBoundingClientRect();
  const toolbarHeight = $("#reader-view .reader-topbar")?.getBoundingClientRect().height || 0;
  const marker = readerRect.top + toolbarHeight + Math.min(160, Math.max(72, reader.clientHeight * 0.22));
  const candidates = [
    ...$$("#source-note-body [data-block-key]"),
    ...$$("#source-note-body .atomic-note"),
    ...$$("#source-note-body .source-section"),
  ];
  let target = candidates.find((element) => {
    const bounds = element.getBoundingClientRect();
    return bounds.top <= marker && bounds.bottom > marker;
  });
  if (!target) {
    target = candidates
      .filter((element) => element.getBoundingClientRect().top <= marker)
      .sort((left, right) => right.getBoundingClientRect().top - left.getBoundingClientRect().top)[0];
  }
  if (!target) return null;
  const blockKey = target.dataset.blockKey;
  const noteId = target.dataset.noteId;
  const outlineNodeId = target.dataset.outlineNodeId;
  return chooseNavigationAnchor(
    (blockKey && state.navigationIndex.byBlock.get(blockKey))
      || (noteId && state.navigationIndex.byNote.get(noteId))
      || (outlineNodeId && state.navigationIndex.byOutline.get(outlineNodeId)),
  );
}

function syncNoteAnchorFromScroll({ force = false } = {}) {
  if (!force && navigationGuarded("note")) return state.readingAnchor;
  const anchor = findNoteAnchorAtReadingLine();
  if (anchor) setReadingAnchor(anchor, "note-scroll");
  return anchor;
}

function scheduleNoteAnchorSync() {
  if (state.noteScrollFrame || state.contentMode !== "note") return;
  state.noteScrollFrame = requestAnimationFrame(() => {
    state.noteScrollFrame = 0;
    syncNoteAnchorFromScroll();
  });
}

function noteElementForAnchor(anchor) {
  if (!anchor) return null;
  if (anchor.block_key) {
    const block = $$("#source-note-body [data-block-key]").find((element) => element.dataset.blockKey === anchor.block_key);
    if (block) return block;
  }
  if (anchor.note_id) {
    const note = $$("#source-note-body .atomic-note").find((element) => element.dataset.noteId === anchor.note_id);
    if (note) return note;
  }
  return $$("#source-note-body .source-section").find((element) => element.dataset.outlineNodeId === anchor.outline_node_id) || null;
}

function scrollNoteToAnchor(anchor, { smooth = true, targetElement = null } = {}) {
  const target = targetElement || noteElementForAnchor(anchor);
  const reader = $("#reader-view");
  if (!target || !reader) return false;
  const sequence = ++state.navigationSequence;
  beginNavigationGuard("note", smooth ? 1400 : 1500);
  setReadingAnchor(anchor, "pdf-to-note");
  const align = () => {
    if (state.navigationSequence !== sequence || state.contentMode !== "note" || !target.isConnected) return;
    const toolbarHeight = $("#reader-view .reader-topbar")?.getBoundingClientRect().height || 0;
    const targetTop = reader.scrollTop
      + target.getBoundingClientRect().top
      - reader.getBoundingClientRect().top
      - toolbarHeight
      - 16;
    reader.scrollTo({ top: Math.max(0, targetTop), behavior: smooth ? "smooth" : "auto" });
  };
  align();
  if (!smooth) for (const delay of [120, 420, 850]) window.setTimeout(align, delay);
  target.classList.add("navigation-target");
  window.setTimeout(() => target.classList.remove("navigation-target"), 1200);
  return true;
}

function navigateFromDirectory(kind, id) {
  const candidates = kind === "note"
    ? state.navigationIndex?.byNote.get(id)
    : state.navigationIndex?.byOutline.get(id);
  const anchor = chooseNavigationAnchor(candidates);
  if (state.contentMode === "pdf") {
    if (!anchor) return toast("该目录节点没有可定位的原文页", "error");
    jumpToPdfPage(physicalPage(anchor), anchor);
    return;
  }
  const fallback = kind === "note"
    ? $$("#source-note-body .atomic-note").find((element) => element.dataset.noteId === id)
    : $$("#source-note-body .source-section").find((element) => element.dataset.outlineNodeId === id);
  if (anchor && scrollNoteToAnchor(anchor, { targetElement: fallback })) return;
  fallback?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function sourcePdfEndpoint(sourceId) {
  return `${API}/sources/${encodeURIComponent(sourceId)}/original-content`;
}

function setPdfLoadStatus(label, kind = "idle") {
  const status = $("#pdf-load-status");
  if (!status) return;
  status.textContent = label;
  status.className = `pdf-load-status ${kind}`.trim();
}

function clearPdfRendering({ preservePagesPromise = false } = {}) {
  state.pdfRenderObserver?.disconnect();
  state.pdfRenderObserver = null;
  for (const task of state.pdfRenderTasks.values()) task.cancel?.();
  state.pdfRenderTasks.clear();
  state.pdfPageElements.clear();
  if (!preservePagesPromise) state.pdfPagesPromise = null;
  state.pdfLoadingTask?.destroy?.();
  state.pdfLoadingTask = null;
  state.pdfDocument?.destroy?.();
  state.pdfDocument = null;
  state.pdfDocumentSourceId = null;
  const pages = $("#source-pdf-pages");
  if (pages) pages.replaceChildren();
}

function releaseSourcePdfBlob() {
  state.pdfAbortController?.abort();
  state.pdfAbortController = null;
  state.pdfLoadPromise = null;
  clearPdfRendering();
  if (state.pdfBlobUrl) URL.revokeObjectURL(state.pdfBlobUrl);
  state.pdfBlobUrl = null;
  state.pdfBlobSourceId = null;
  state.pdfBlobSize = 0;
  state.pdfData = null;
  const link = $("#open-source-pdf");
  if (link) {
    link.href = "#";
    link.hidden = true;
  }
  setPdfLoadStatus("按需加载");
}

async function ensureSourcePdfBlob() {
  const sourceId = state.selectedSource?.source_id;
  if (!sourceId) throw new Error("没有可读取的原始 PDF");
  if (state.pdfBlobUrl && state.pdfBlobSourceId === sourceId) return state.pdfBlobUrl;
  if (state.pdfLoadPromise?.sourceId === sourceId) return state.pdfLoadPromise.promise;

  state.pdfAbortController?.abort();
  const controller = new AbortController();
  state.pdfAbortController = controller;
  setPdfLoadStatus("正在载入原始 PDF…", "loading");
  const promise = (async () => {
    const response = await fetch(sourcePdfEndpoint(sourceId), {
      headers: { Accept: "application/pdf" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`原始 PDF 加载失败 (${response.status})`);
    const opaqueBlob = await response.blob();
    if (!opaqueBlob.size) throw new Error("原始 PDF 响应为空");
    const pdfData = new Uint8Array(await opaqueBlob.arrayBuffer());
    const blob = new Blob([pdfData], { type: "application/pdf" });
    const blobUrl = URL.createObjectURL(blob);
    if (controller.signal.aborted || state.selectedSource?.source_id !== sourceId) {
      URL.revokeObjectURL(blobUrl);
      throw new DOMException("PDF load was cancelled", "AbortError");
    }
    if (state.pdfBlobUrl) URL.revokeObjectURL(state.pdfBlobUrl);
    state.pdfBlobUrl = blobUrl;
    state.pdfBlobSourceId = sourceId;
    state.pdfBlobSize = blob.size;
    state.pdfData = pdfData;
    setPdfLoadStatus(`${formatBytes(blob.size)} · 已载入`, "ready");
    return blobUrl;
  })();
  state.pdfLoadPromise = { sourceId, promise };
  try {
    return await promise;
  } finally {
    if (state.pdfLoadPromise?.promise === promise) state.pdfLoadPromise = null;
    if (state.pdfAbortController === controller) state.pdfAbortController = null;
  }
}

async function loadPdfJs() {
  if (!state.pdfJsPromise) {
    state.pdfJsPromise = import("/app/vendor/pdfjs/pdf.min.mjs?v=4.10.38").then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = "/app/vendor/pdfjs/pdf.worker.min.mjs?v=4.10.38";
      return pdfjs;
    });
  }
  return state.pdfJsPromise;
}

async function ensurePdfDocument() {
  const sourceId = state.selectedSource?.source_id;
  if (!sourceId) throw new Error("没有可读取的原始 PDF");
  if (state.pdfDocument && state.pdfDocumentSourceId === sourceId) return state.pdfDocument;

  const [, pdfjs] = await Promise.all([ensureSourcePdfBlob(), loadPdfJs()]);
  if (state.selectedSource?.source_id !== sourceId) throw new DOMException("PDF load was cancelled", "AbortError");
  clearPdfRendering({ preservePagesPromise: true });
  if (!state.pdfData?.length) throw new Error("原始 PDF 数据不可用");
  const loadingTask = pdfjs.getDocument({ data: state.pdfData.slice() });
  state.pdfLoadingTask = loadingTask;
  const documentProxy = await loadingTask.promise;
  if (state.selectedSource?.source_id !== sourceId) {
    await documentProxy.destroy();
    throw new DOMException("PDF load was cancelled", "AbortError");
  }
  state.pdfLoadingTask = null;
  state.pdfDocument = documentProxy;
  state.pdfDocumentSourceId = sourceId;
  setPdfLoadStatus(`${formatBytes(state.pdfBlobSize)} · ${documentProxy.numPages} 页`, "ready");
  return documentProxy;
}

async function renderPdfPage(pageNumber) {
  const shell = state.pdfPageElements.get(pageNumber);
  const documentProxy = state.pdfDocument;
  const pages = $("#source-pdf-pages");
  if (!shell || !documentProxy || !pages || state.pdfRenderTasks.has(pageNumber)) return;
  const cssWidth = Math.max(280, Math.floor(pages.clientWidth));
  const previousWidth = Number(shell.dataset.renderedWidth || 0);
  if (shell.dataset.rendered === "true" && Math.abs(previousWidth - cssWidth) < 12) return;

  shell.classList.add("loading");
  shell.querySelector(".source-pdf-page-error")?.remove();
  try {
    const page = await documentProxy.getPage(pageNumber);
    const baseViewport = page.getViewport({ scale: 1 });
    const cssScale = cssWidth / baseViewport.width;
    const outputScale = Math.min(window.devicePixelRatio || 1, 2);
    const renderViewport = page.getViewport({ scale: cssScale * outputScale });
    const cssHeight = Math.round(baseViewport.height * cssScale);
    const canvas = shell.querySelector("canvas");
    canvas.width = Math.max(1, Math.floor(renderViewport.width));
    canvas.height = Math.max(1, Math.floor(renderViewport.height));
    canvas.style.height = `${cssHeight}px`;
    shell.style.minHeight = `${cssHeight}px`;
    const canvasContext = canvas.getContext("2d", { alpha: false });
    const task = page.render({ canvasContext, viewport: renderViewport, background: "rgb(255, 255, 255)" });
    state.pdfRenderTasks.set(pageNumber, task);
    await task.promise;
    shell.dataset.rendered = "true";
    shell.dataset.renderedWidth = String(cssWidth);
  } catch (error) {
    if (error?.name !== "RenderingCancelledException") {
      const message = create("span", "source-pdf-page-error", `第 ${pageNumber} 页渲染失败`);
      shell.append(message);
    }
  } finally {
    state.pdfRenderTasks.delete(pageNumber);
    shell.classList.remove("loading");
  }
}

async function ensurePdfPages() {
  if (state.pdfPagesPromise) return state.pdfPagesPromise;
  const promise = (async () => {
    const documentProxy = await ensurePdfDocument();
    const viewer = $("#source-pdf-viewer");
    const pages = $("#source-pdf-pages");
    if (!viewer || !pages) return;
    pages.replaceChildren();
    state.pdfPageElements.clear();

    const firstPage = await documentProxy.getPage(1);
    const firstViewport = firstPage.getViewport({ scale: 1 });
    const initialRatio = firstViewport.height / firstViewport.width;
    const initialWidth = Math.max(280, pages.clientWidth || viewer.clientWidth);
    for (let pageNumber = 1; pageNumber <= documentProxy.numPages; pageNumber += 1) {
      const shell = create("section", "source-pdf-page loading");
      shell.dataset.page = String(pageNumber);
      shell.setAttribute("aria-label", `PDF 第 ${pageNumber} 页`);
      shell.style.minHeight = `${Math.round(initialWidth * initialRatio)}px`;
      shell.append(document.createElement("canvas"));
      pages.append(shell);
      state.pdfPageElements.set(pageNumber, shell);
    }

    state.pdfRenderObserver?.disconnect();
    state.pdfRenderObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) void renderPdfPage(Number(entry.target.dataset.page));
      }
    }, { root: viewer, rootMargin: "1200px 0px", threshold: 0.01 });
    for (const shell of state.pdfPageElements.values()) state.pdfRenderObserver.observe(shell);
  })();
  state.pdfPagesPromise = promise;
  try {
    await promise;
  } catch (error) {
    state.pdfPagesPromise = null;
    throw error;
  }
}

function syncPdfPageFromScroll() {
  if (state.pdfScrollFrame) return;
  state.pdfScrollFrame = requestAnimationFrame(() => {
    state.pdfScrollFrame = 0;
    const viewer = $("#source-pdf-viewer");
    if (!viewer || state.contentMode !== "pdf") return;
    const marker = viewer.scrollTop + Math.min(180, viewer.clientHeight * 0.3);
    const viewerTop = viewer.getBoundingClientRect().top;
    let visiblePage = 1;
    for (const [pageNumber, shell] of state.pdfPageElements) {
      const pageTop = viewer.scrollTop + shell.getBoundingClientRect().top - viewerTop;
      if (pageTop <= marker) visiblePage = pageNumber;
      else break;
    }
    state.pdfPage = visiblePage;
    $("#pdf-page-label").textContent = `P.${visiblePage}`;
    if (!navigationGuarded("pdf")) setReadingAnchor(anchorForPdfPage(visiblePage), "pdf-scroll");
  });
}

function rerenderVisiblePdfPages() {
  if (state.contentMode !== "pdf" || !state.pdfDocument) return;
  const viewer = $("#source-pdf-viewer");
  if (!viewer) return;
  const viewport = viewer.getBoundingClientRect();
  for (const [pageNumber, shell] of state.pdfPageElements) {
    const bounds = shell.getBoundingClientRect();
    if (bounds.bottom >= viewport.top - 800 && bounds.top <= viewport.bottom + 800) void renderPdfPage(pageNumber);
  }
}

async function displayPdfPage() {
  const sourceId = state.selectedSource?.source_id;
  if (!sourceId) return;
  try {
    const blobUrl = await ensureSourcePdfBlob();
    if (state.selectedSource?.source_id !== sourceId || state.contentMode !== "pdf") return;
    await ensurePdfPages();
    if (state.selectedSource?.source_id !== sourceId || state.contentMode !== "pdf") return;
    const pageNumber = Math.min(parsePdfPage(state.pdfPage), state.pdfDocument?.numPages || 1);
    const shell = state.pdfPageElements.get(pageNumber);
    await renderPdfPage(pageNumber);
    const viewer = $("#source-pdf-viewer");
    if (shell && viewer) {
      const pageTop = viewer.scrollTop + shell.getBoundingClientRect().top - viewer.getBoundingClientRect().top;
      viewer.scrollTo({ top: pageTop, behavior: "auto" });
    }
    const viewUrl = `${blobUrl}#page=${pageNumber}&view=FitH`;
    const link = $("#open-source-pdf");
    link.href = viewUrl;
    link.hidden = false;
  } catch (error) {
    if (error?.name === "AbortError") return;
    setPdfLoadStatus("加载失败", "failed");
    toast(error.message, "error");
  }
}

function applyContentMode(mode) {
  state.contentMode = mode === "pdf" && sourceSupportsPdf() ? "pdf" : "note";
  $$('[data-content-mode]').forEach((button) => {
    const active = button.dataset.contentMode === state.contentMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  const notePane = $("#note-content-pane");
  const pdfPane = $("#pdf-content-pane");
  if (!notePane || !pdfPane) return;
  notePane.hidden = state.contentMode !== "note";
  pdfPane.hidden = state.contentMode !== "pdf";
  if (state.contentMode === "pdf" && state.selectedSource) {
    $("#pdf-page-label").textContent = `P.${state.pdfPage}`;
    requestAnimationFrame(() => {
      pdfPane.scrollIntoView({ block: "start", behavior: "auto" });
      void displayPdfPage();
    });
  }
}

function switchContentMode(mode) {
  const targetMode = mode === "pdf" ? "pdf" : "note";
  if (targetMode === state.contentMode) return;
  if (targetMode === "pdf") {
    const anchor = syncNoteAnchorFromScroll({ force: true }) || state.readingAnchor;
    if (anchor) {
      state.pdfPage = physicalPage(anchor);
      setReadingAnchor(anchor, "note-to-pdf");
    }
    beginNavigationGuard("pdf");
    applyContentMode("pdf");
    return;
  }

  const anchor = anchorForPdfPage(state.pdfPage) || state.readingAnchor;
  applyContentMode("note");
  if (anchor) requestAnimationFrame(() => requestAnimationFrame(() => scrollNoteToAnchor(anchor, { smooth: false })));
}

function jumpToPdfPage(value, anchor = null) {
  state.pdfPage = parsePdfPage(value);
  $("#pdf-page-label").textContent = `P.${state.pdfPage}`;
  if (anchor) setReadingAnchor(anchor, "evidence-click");
  else setReadingAnchor(anchorForPdfPage(state.pdfPage), "evidence-click");
  beginNavigationGuard("pdf");
  applyContentMode("pdf");
  $("#pdf-content-pane").scrollIntoView({ block: "start" });
}

function sourceSupportsPdf() {
  const source = state.sourceNote || state.selectedSource || {};
  if (typeof source.original_view_available === "boolean") return source.original_view_available;
  return /\.pdf$/i.test(source.source_file || state.selectedSource?.source_file || "");
}

function createPageButton(label, page, className, anchor = null) {
  if (!sourceSupportsPdf()) {
    const locator = create("span", `${className} source-locator`, label.replace(/^PDF P\.|^P\./, "原文 · "));
    locator.title = "来源为教材章节；此定位不是 PDF 页码";
    return locator;
  }
  const button = create("button", className, label);
  button.type = "button";
  button.title = `在原始 PDF 中打开第 ${parsePdfPage(page)} 页`;
  button.addEventListener("click", () => jumpToPdfPage(page, anchor));
  return button;
}

function assetUrl(asset) {
  if (asset.blob_url) return asset.blob_url;
  return `${API}/blobs/${String(asset.storage_key || "").split("/").map(encodeURIComponent).join("/")}`;
}

function renderAsset(asset, context = {}) {
  const figure = create("figure", "inline-asset");
  const image = document.createElement("img");
  image.src = assetUrl(asset);
  if (Number(asset.width) > 0 && Number(asset.height) > 0) {
    image.width = Number(asset.width);
    image.height = Number(asset.height);
    // Reserve the scaled aspect ratio before a lazy image is decoded.
    image.style.width = `${Math.min(image.width, 620 * image.width / image.height)}px`;
    image.style.aspectRatio = `${image.width} / ${image.height}`;
  }
  image.alt = asset.alt_text || asset.caption || "来源图示";
  image.loading = "lazy";
  const caption = create("figcaption");
  const pages = assetPages(asset);
  const page = Number.isFinite(Number(asset.page_index)) ? Number(asset.page_index) + 1 : parsePdfPage(asset.page_label || pages);
  const candidates = (context.blockKey && state.navigationIndex?.byBlock.get(context.blockKey))
    || (context.noteId && state.navigationIndex?.byNote.get(context.noteId))
    || state.navigationIndex?.byPage.get(page);
  const anchor = chooseNavigationAnchor((candidates || []).filter((item) => physicalPage(item) === page));
  caption.append(createPageButton(`P.${pages}`, page, "page-chip", anchor));
  const captionText = asset.caption || asset.purpose || asset.alt_text || "来源图示";
  const captionKey = `${pages}::${captionText}`;
  if (!context.renderedCaptions?.has(captionKey)) {
    const label = create("span", "asset-caption-text");
    renderMarkdown(label, captionText, { inline: true });
    caption.append(label);
    context.renderedCaptions?.add(captionKey);
  }
  figure.append(image, caption);
  return figure;
}

function renderBlock(block, renderedAssets, context) {
  const wrap = create("div", `content-block block-${String(block.type || "paragraph").toLowerCase()}`);
  wrap.dataset.blockKey = block.block_key || "";
  wrap.dataset.noteId = context.noteId;
  wrap.dataset.outlineNodeId = context.outlineNodeId;
  const type = String(block.type || "PARAGRAPH").toUpperCase();
  if (type === "BULLETED_LIST" || type === "NUMBERED_LIST") {
    const list = document.createElement(type === "BULLETED_LIST" ? "ul" : "ol");
    for (const item of block.items || []) {
      const li = document.createElement("li");
      renderMarkdown(li, item, { inline: true });
      list.append(li);
    }
    if ((block.items || []).length) wrap.append(list);
    else renderMarkdown(wrap, block.text || "");
  } else if (type === "TABLE") {
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const value of block.table_headers || []) { const th = document.createElement("th"); renderMarkdown(th, value, { inline: true }); headRow.append(th); }
    head.append(headRow);
    const body = document.createElement("tbody");
    for (const row of block.table_rows || []) { const tr = document.createElement("tr"); for (const value of row) { const td = document.createElement("td"); renderMarkdown(td, value, { inline: true }); tr.append(td); } body.append(tr); }
    table.append(head, body);
    if ((block.table_headers || []).length) wrap.append(table);
    else renderMarkdown(wrap, block.text || "");
  } else if (type === "CODE") {
    const pre = document.createElement("pre");
    pre.append(create("code", "", block.text || ""));
    wrap.append(pre);
  } else if (type === "FORMULA") {
    const formula = create("div", "formula-block noodle-math-display");
    const expression = String(block.text || "").trim();
    const fragments = [...expression.matchAll(mathTokenPattern)];
    const singleFormula = fragments.length === 1 && fragments[0][0] === expression;
    if ((fragments.length && !singleFormula) || /^#{1,6}\s|\n\n|\*\*/m.test(expression) && !singleFormula) {
      // Historical migrations stored prose and delimited math in FORMULA.text.
      renderMarkdown(wrap, expression);
    } else {
      renderKatexNode(formula, stripMathDelimiters(expression), true);
      wrap.append(formula);
    }
  } else if (type === "CALLOUT") {
    const callout = create("blockquote", "callout-block");
    renderMarkdown(callout, block.text || "");
    wrap.append(callout);
  } else if (type !== "IMAGE") {
    renderMarkdown(wrap, block.text || "");
  }
  for (const asset of block.assets || []) {
    const key = asset.storage_key || asset.visual_asset_id;
    if (key && renderedAssets.has(key)) continue;
    wrap.append(renderAsset(asset, { ...context, blockKey: block.block_key }));
    if (key) renderedAssets.add(key);
  }
  enhanceScrollableMarkdown(wrap);
  return wrap;
}

function renderAtomicNote(note, outlineNodeId, { hideTitle = false, titleLevel = 3 } = {}) {
  const article = create("section", "atomic-note");
  article.id = `note-${note.note_id}`;
  article.dataset.noteId = note.note_id;
  article.dataset.outlineNodeId = outlineNodeId;
  const header = create("header", "atomic-note-header");
  if (!hideTitle) {
    header.append(create(`h${Math.min(6, titleLevel)}`, "", note.title));
    article.append(header);
  }
  // thesis is retrieval metadata; repeating it here duplicates the note body.
  const renderedAssets = new Set();
  const context = { noteId: note.note_id, outlineNodeId, renderedCaptions: new Set() };
  for (const block of note.blocks || []) article.append(renderBlock(block, renderedAssets, context));
  for (const asset of note.assets || []) {
    const key = asset.storage_key || asset.visual_asset_id;
    if (!key || !renderedAssets.has(key)) article.append(renderAsset(asset, context));
  }
  const pages = [];
  for (const evidenceItem of note.evidence || []) {
    if (!Number.isFinite(Number(evidenceItem.page_index))) continue;
    const page = Number(evidenceItem.page_index) + 1;
    if (!pages.some((item) => item.page === page)) pages.push({
      page,
      label: evidenceItem.page_label ?? page,
      anchor: chooseNavigationAnchor(
        (state.navigationIndex?.byNote.get(note.note_id) || []).filter((item) => physicalPage(item) === page),
      ),
    });
  }
  if (pages.length) {
    const evidence = create("div", "evidence-list");
    for (const item of pages) {
      evidence.append(
        createPageButton(
          `PDF P.${String(item.label).replace(/^p\.?/i, "")}`,
          item.page,
          "evidence-chip",
          item.anchor,
        ),
      );
    }
    article.append(evidence);
  }
  return article;
}

function renderOutlineNode(node, body, nav, parentElided = false) {
  const id = `outline-${node.outline_node_id}`;
  const elide = node.level === 1 && !parentElided && state.sourceNote.outline.length === 1 && node.title.trim() === state.sourceNote.title.trim();
  const section = create("section", `source-section level-${node.level}`);
  section.id = id;
  section.dataset.outlineNodeId = node.outline_node_id;
  const singleNoteTitle = !node.children?.length && node.notes?.length === 1
    && node.notes[0].title.trim() === node.title.trim();
  if (!elide) section.append(create(`h${Math.min(6, node.level + 1)}`, "source-section-heading", node.title));
  const navButton = create("button", "outline-link", node.title);
  navButton.type = "button";
  navButton.dataset.level = String(node.level);
  navButton.dataset.outlineNodeId = node.outline_node_id;
  navButton.addEventListener("click", () => navigateFromDirectory("outline", node.outline_node_id));
  nav.append(navButton);
  for (const note of node.notes || []) {
    section.append(renderAtomicNote(note, node.outline_node_id, {
      hideTitle: singleNoteTitle && !elide, titleLevel: node.level + 2,
    }));
    if (singleNoteTitle && !elide) {
      navButton.dataset.noteId = note.note_id;
      continue;
    }
    const noteButton = create("button", "outline-link note-link", note.title);
    noteButton.type = "button";
    noteButton.dataset.level = String(Math.min(3, node.level + 1));
    noteButton.dataset.outlineNodeId = node.outline_node_id;
    noteButton.dataset.noteId = note.note_id;
    noteButton.addEventListener("click", () => navigateFromDirectory("note", note.note_id));
    nav.append(noteButton);
  }
  body.append(section);
  for (const child of node.children || []) renderOutlineNode(child, body, nav, elide || parentElided);
}

function renderReaderFiles() {
  const list = $("#reader-file-list");
  list.replaceChildren();
  const siblings = state.sources
    .filter((item) => item.folder_id === state.selectedSource?.folder_id && item.source_note_id)
    .sort((a, b) => a.sort_order - b.sort_order || a.title.localeCompare(b.title, "zh-CN"));
  for (const source of siblings) {
    const button = create("button", `reader-file-button ${source.source_id === state.selectedSource.source_id ? "active" : ""}`, source.title);
    button.type = "button";
    button.addEventListener("click", () => openSource(source));
    list.append(button);
  }
}

function renderSourceNote() {
  const note = state.sourceNote;
  buildNavigationIndex(note);
  $("#note-title").textContent = note.title;
  $("#reader-toolbar-title").textContent = note.title;
  $("#note-summary").textContent = note.summary || `${note.unit_count} 个知识单元`;
  $("#note-version-chip").textContent = `SourceNote v${note.version_no}`;
  const filename = state.selectedSource?.source_file || note.source_file;
  $("#note-source-file").textContent = /^[a-f0-9]{64}\.pdf$/i.test(filename || "")
    ? state.selectedSource?.title || note.title : filename;
  $("#note-source-file").title = filename || "";
  const pdfButton = $('[data-content-mode="pdf"]');
  pdfButton.disabled = !sourceSupportsPdf();
  pdfButton.hidden = !sourceSupportsPdf();
  $('[data-content-mode="note"]').textContent = sourceSupportsPdf() ? "笔记" : "教材原文";
  const body = $("#source-note-body");
  const nav = $("#outline-tree");
  body.replaceChildren();
  nav.replaceChildren();
  for (const node of note.outline || []) renderOutlineNode(node, body, nav);
  renderReaderFiles();
  applyContentMode(state.contentMode);
  requestAnimationFrame(() => syncNoteAnchorFromScroll({ force: true }));
}

function agentPrompt(note = null) {
  const release = state.sourceNote;
  if (!release) return "";
  const fields = [
    `source_note_id=${release.source_note_id}`,
    `source_version_id=${release.source_version_id}`,
    `source_note_version_id=${release.version_id}`,
    `source_title=${release.title}`,
  ];
  if (note) fields.push(
    `atomic_note_id=${note.note_id}`,
    `atomic_note_version_id=${note.version_id}`,
    `atomic_note_title=${note.title}`,
  );
  return `[noodle_selection]\n${fields.join("\n")}\n[/noodle_selection]\n当前来源问题：检索和问答工具必须传入以上 source_version_id。依据优先使用笔记的证据页码；assetPages 表示图示所在页。正文没有证据页码不代表该页没有图示。只说明已读取笔记中未找到，不能据此断言整份 PDF 没有相关内容。回答直接、简洁，用必要的页码支撑结论。`;
}

const AGENT_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const AGENT_IMAGE_LIMIT = 4;
const AGENT_IMAGE_MAX_BYTES = 15 * 1024 * 1024;

function attachmentName(file, index = 0) {
  const original = String(file?.name || "").trim();
  if (original && !/^image\.(png|jpe?g|webp)$/i.test(original)) return original;
  const extension = file?.type === "image/jpeg" ? "jpg" : file?.type === "image/webp" ? "webp" : "png";
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `clipboard-${stamp}-${index + 1}.${extension}`;
}

function renderAgentAttachments() {
  const shelf = $("#agent-attachment-shelf");
  const list = $("#agent-attachment-list");
  shelf.hidden = state.agentAttachments.length === 0;
  list.replaceChildren();
  for (const attachment of state.agentAttachments) {
    const card = create("figure", `agent-attachment-card ${attachment.status || "ready"}`);
    const image = document.createElement("img");
    image.src = attachment.previewUrl;
    image.alt = attachment.name;
    const caption = document.createElement("figcaption");
    caption.append(
      create("strong", "", attachment.name),
      create("small", "", attachment.status === "uploading"
        ? "正在上传并识图…"
        : attachment.status === "failed"
          ? attachment.error || "上传失败"
          : attachment.remote
            ? "已上传 · 等待发送"
            : `${formatBytes(attachment.file.size)} · 等待发送`),
    );
    const remove = create("button", "agent-attachment-remove", "×");
    remove.type = "button";
    remove.title = `移除 ${attachment.name}`;
    remove.setAttribute("aria-label", `移除图片 ${attachment.name}`);
    remove.disabled = state.agentSending;
    remove.addEventListener("click", () => removeAgentAttachment(attachment.localId));
    card.append(image, caption, remove);
    list.append(card);
  }
}

function addAgentImageFiles(files) {
  if (state.agentSending) {
    toast("当前消息正在发送，请在下一轮添加图片", "error");
    return;
  }
  const incoming = [...files];
  let added = 0;
  for (const file of incoming) {
    if (state.agentAttachments.length >= AGENT_IMAGE_LIMIT) {
      toast(`每轮最多添加 ${AGENT_IMAGE_LIMIT} 张图片`, "error");
      break;
    }
    if (!AGENT_IMAGE_TYPES.has(file.type)) {
      toast(`不支持 ${file.name || "该图片"}，请使用 PNG、JPEG 或 WebP`, "error");
      continue;
    }
    if (file.size > AGENT_IMAGE_MAX_BYTES) {
      toast(`${file.name || "图片"} 超过 15 MB`, "error");
      continue;
    }
    const name = attachmentName(file, state.agentAttachments.length);
    state.agentAttachments.push({
      localId: crypto.randomUUID(),
      file,
      name,
      previewUrl: URL.createObjectURL(file),
      status: "ready",
      remote: null,
      error: "",
    });
    added += 1;
  }
  renderAgentAttachments();
  if (added) {
    const input = $("#agent-input");
    input.placeholder = "说明这张图片要替换哪些页码或插入到哪篇笔记…";
    toast(`已添加 ${added} 张图片，发送时上传并交给 Agent`);
  }
}

function removeAgentAttachment(localId) {
  const index = state.agentAttachments.findIndex((item) => item.localId === localId);
  if (index < 0 || state.agentSending) return;
  const [removed] = state.agentAttachments.splice(index, 1);
  renderAgentAttachments();
  window.setTimeout(() => URL.revokeObjectURL(removed.previewUrl), 500);
}

function clearAgentAttachments() {
  const previewUrls = state.agentAttachments.map((attachment) => attachment.previewUrl);
  state.agentAttachments = [];
  renderAgentAttachments();
  window.setTimeout(() => previewUrls.forEach((url) => URL.revokeObjectURL(url)), 500);
}

function handleAgentPaste(event) {
  const items = [...(event.clipboardData?.items || [])];
  const images = items
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter(Boolean);
  if (!images.length) return;
  event.preventDefault();
  addAgentImageFiles(images);
}

async function uploadAgentAttachments() {
  for (const attachment of state.agentAttachments) {
    if (attachment.remote) continue;
    attachment.status = "uploading";
    attachment.error = "";
    renderAgentAttachments();
    try {
      const body = new FormData();
      body.append("file", attachment.file, attachment.name);
      attachment.remote = await request(`${API}/attachments/images?analyze=true`, {
        method: "POST",
        headers: state.agentSessionId ? { "X-Harness-Session-Id": state.agentSessionId } : {},
        body,
      });
      attachment.status = "uploaded";
    } catch (error) {
      attachment.status = "failed";
      attachment.error = error.message;
      renderAgentAttachments();
      throw error;
    }
    renderAgentAttachments();
  }
  return state.agentAttachments;
}

function escapedMarkdownLabel(value) {
  return String(value || "图片").replace(/[\\[\]()`*_]/g, "\\$&");
}

function attachmentMessageMarkdown(attachments) {
  if (!attachments.length) return "";
  return attachments.map((item) => {
    const name = escapedMarkdownLabel(item.remote.original_name || item.name);
    return `![已粘贴图片：${name}](${item.remote.blob_url})`;
  }).join("\n\n");
}

function attachmentAgentContext(attachments) {
  if (!attachments.length) return "";
  const details = attachments.map((item, index) => {
    const remote = item.remote;
    const rawVision = remote.vision || {};
    const compactVision = {};
    for (const key of ["asset_type", "caption", "teaching_role", "essential", "suggested_placement", "confidence"]) {
      const value = rawVision[key];
      if (value !== undefined && value !== null && value !== "") compactVision[key] = typeof value === "string" ? clipText(value, 320) : value;
    }
    for (const key of ["ocr_text", "factual_observations", "spatial_relations"]) {
      if (Array.isArray(rawVision[key]) && rawVision[key].length) compactVision[key] = rawVision[key].slice(0, 4).map((value) => clipText(value, 180));
    }
    const vision = JSON.stringify(compactVision);
    return [
      `${index + 1}. attachment_id：${remote.attachment_id}`,
      `   文件名：${remote.original_name || item.name}`,
      `   类型：${remote.media_type}；状态：${remote.status}`,
      vision && vision !== "{}" ? `   视觉摘要：${vision.slice(0, 1200)}` : "   视觉摘要：未启用或没有结果",
    ].join("\n");
  }).join("\n");
  return `[noodle_attachments]\n已保存为 AgentAttachment；替换图片时直接使用 attachment_id，不要重复上传。\n${details}\n[/noodle_attachments]`;
}

function focusAgent(note = null) {
  state.agentTargetNote = note;
  localStorage.setItem("noodle.ui.agentCollapsed", "false");
  applyPanelState();
  updateAgentContext();
  const input = $("#agent-input");
  input.placeholder = note
    ? `询问或修改「${note.title}」…`
    : state.sourceNote ? `询问或修改「${state.sourceNote.title}」…` : "询问知识库，或描述你希望完成的任务…";
  input.focus();
}

function updateAgentContext() {
  const value = state.agentTargetNote
    ? `${state.agentTargetNote.title} · AtomicNote`
    : state.sourceNote ? `${state.sourceNote.title} · v${state.sourceNote.version_no}` : selectedFolderTitle();
  $("#agent-context b").textContent = value;
}

function sessionTitle(session) {
  const value = session.projections?.values?.title;
  if (typeof value === "string" && value.trim()) return value.trim();
  if (value && typeof value.title === "string" && value.title.trim()) return value.title.trim();
  return session.blank ? "新对话" : `对话 ${session.sessionId.slice(-6)}`;
}

function setAgentStatus(label, kind = "idle") {
  const status = $("#agent-status");
  status.className = kind;
  status.innerHTML = `<i></i>${escapeHtml(label)}`;
}

function visibleUserText(text) {
  const marker = "用户请求：\n";
  const index = String(text || "").lastIndexOf(marker);
  return index >= 0 ? String(text).slice(index + marker.length).trim() : String(text || "");
}

function messageParts(message) {
  const content = Array.isArray(message?.content) ? message.content : message?.content ? [message.content] : [];
  return {
    text: content.filter((part) => part?.type === "text").map((part) => part.text || "").join("\n\n").trim(),
    reasoning: content.filter((part) => part?.type === "reasoning").map((part) => part.text || "").join("\n\n").trim(),
  };
}

const TOOL_PRESENTATION = {
  noodle_search_knowledge: ["检索知识库", "查找与问题相关的原子笔记和页码证据"],
  noodle_answer_knowledge: ["生成引用问答", "使用 Noodle 检索链生成带来源回答"],
  noodle_list_source_notes: ["列出完整笔记", "读取当前知识库中的 SourceNote"],
  noodle_get_source_note: ["读取完整笔记", "核对 SourceNote 的活动版本与大纲"],
  noodle_get_atomic_note: ["读取原子笔记", "读取 AtomicNote 正文和稳定 Block"],
  noodle_get_job_status: ["检查处理任务", "读取 PDF 后台任务的权威状态"],
  noodle_ingest_pdf: ["导入 PDF", "创建可恢复的后台处理任务"],
  noodle_upload_image_attachment: ["上传图片附件", "保存待用于笔记修改的图片"],
  noodle_restart_generation: ["局部重新生成", "复用解析结果并从指定阶段恢复"],
  noodle_edit_note_block: ["修改笔记 Block", "创建新的 AtomicNote 与 SourceNote 版本"],
  noodle_replace_source_images: ["替换来源图片", "按页码范围创建新的图片与笔记版本"],
  noodle_export_markdown: ["导出 Markdown", "从数据库活动版本生成本地投影"],
};

const WRITE_TOOL_NAMES = new Set([
  "noodle_ingest_pdf", "noodle_upload_image_attachment", "noodle_restart_generation",
  "noodle_edit_note_block", "noodle_replace_source_images", "noodle_export_markdown",
]);

function parseJson(value, fallback = {}) {
  if (value && typeof value === "object") return value;
  try { return JSON.parse(String(value || "")); }
  catch (_) { return fallback; }
}

function clipText(value, limit = 96) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function toolResultData(event) {
  const message = event.data?.message || {};
  const outer = Array.isArray(message.content) ? message.content : [];
  const result = outer.find((part) => part?.type === "tool-result") || {};
  const inner = Array.isArray(result.content) ? result.content : [];
  const text = inner.filter((part) => part?.type === "text").map((part) => part.text || "").join("\n");
  return {
    callId: message.source?.callId || result.toolCallId || "",
    isError: Boolean(result.isError),
    payload: parseJson(text, { detail: text }),
  };
}

function toolResultSummary(tool) {
  if (!tool.result) {
    if (["rejected", "cancelled", "unavailable"].includes(tool.approval?.outcome)) return "本次操作未获得授权";
    return TOOL_PRESENTATION[tool.name]?.[1] || "等待工具返回";
  }
  const value = tool.result.payload || {};
  if (tool.result.isError) return clipText(value.detail || value.error?.message || value.message || "工具执行失败", 120);
  if (tool.name === "noodle_search_knowledge") {
    const count = value.count ?? value.hits?.length;
    // Harness may spill long tool results, leaving a non-JSON preview.
    // Missing metadata does not mean the retrieval returned no results.
    return count === undefined ? "检索已完成，长结果摘要不可用" : `命中 ${count} 条知识结果`;
  }
  if (tool.name === "noodle_answer_knowledge" && Array.isArray(value.citations)) return `已生成回答 · ${value.citations.length} 条笔记引用`;
  if (tool.name === "noodle_list_source_notes") return `读取到 ${value.count ?? value.notes?.length ?? 0} 份 SourceNote`;
  if (tool.name === "noodle_get_source_note") return `已读取「${value.title || "SourceNote"}」v${value.versionNo ?? "?"}，共 ${value.unitCount ?? 0} 个知识单元`;
  if (tool.name === "noodle_get_atomic_note") return `已读取「${value.title || "AtomicNote"}」，共 ${value.blocks?.length ?? 0} 个 Block`;
  if (tool.name === "noodle_get_job_status") return `${value.stage || value.status || "任务状态已更新"}${Number.isFinite(value.progress) ? ` · ${Math.round(value.progress * (value.progress <= 1 ? 100 : 1))}%` : ""}`;
  if (tool.name === "noodle_ingest_pdf") return `已创建任务 ${clipText(value.jobId || value.job_id || "", 28)}`;
  if (tool.name === "noodle_upload_image_attachment") return `附件已保存：${value.originalName || value.original_name || value.attachmentId || "图片"}`;
  if (tool.name === "noodle_edit_note_block") return `已创建笔记版本 v${value.versionNo ?? "?"}`;
  if (tool.name === "noodle_replace_source_images") return `已替换 ${value.removedVisualCount ?? 0} 个图片碎片并创建新版本`;
  if (tool.name === "noodle_export_markdown") return `已生成 ${value.noteCount ?? value.length ?? 0} 份 Markdown 投影`;
  return "执行完成";
}

function toolStatus(tool) {
  if (tool.approval && !tool.approval.outcome) return "approval";
  if (["rejected", "cancelled", "unavailable"].includes(tool.approval?.outcome)) return "rejected";
  if (!tool.result) return "running";
  return tool.result.isError ? "failed" : "completed";
}

function durationLabel(start, end) {
  if (!start || !end || end < start) return "";
  const ms = end - start;
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

function turnIntent(turn) {
  const names = new Set(turn.tools.map((tool) => tool.name));
  if ([...names].some((name) => WRITE_TOOL_NAMES.has(name))) return "执行受控知识库修改";
  if (names.has("noodle_search_knowledge") || names.has("noodle_answer_knowledge")) return "检索并核对知识证据";
  if (names.has("noodle_get_source_note") || names.has("noodle_get_atomic_note")) return "读取活动笔记版本";
  if (names.has("noodle_get_job_status")) return "检查后台处理进度";
  return "调用 Noodle 知识能力";
}

function foldAgentHistory(entries) {
  const turns = [];
  const turnMap = new Map();
  const calls = new Map();
  const approvals = new Map();
  const messages = [];
  let toolCount = 0;
  let lastError = null;
  let activeTurn = 0;
  const ensureTurn = (id = activeTurn || turns.length + 1) => {
    const key = Number(id || 0);
    if (!turnMap.has(key)) {
      const turn = { id: key, users: [], assistants: [], tools: [], status: "running", startedAt: 0, endedAt: 0 };
      turnMap.set(key, turn);
      turns.push(turn);
    }
    return turnMap.get(key);
  };
  for (const entry of entries || []) {
    const event = entry.event || {};
    if (event.type === "turn/start") {
      activeTurn = Number(event.data?.turn || activeTurn + 1);
      const turn = ensureTurn(activeTurn);
      turn.startedAt = event.time || turn.startedAt;
    } else if (event.type === "user/message") {
      // Harness persists runtime snapshots, plugin context and skill catalogs as
      // user-role messages for the model protocol. Only human-authored messages
      // belong in the visible conversation; source.kind is the durable boundary.
      if (event.data?.source?.kind !== "user") continue;
      const parts = messageParts(event.data);
      if (parts.text) {
        const message = { role: "user", text: visibleUserText(parts.text), time: event.time, seq: event.seq };
        messages.push(message);
        ensureTurn().users.push(message);
      }
    } else if (event.type === "assistant/message") {
      const parts = messageParts(event.data?.message);
      if (parts.text) {
        const message = { role: "assistant", text: parts.text, time: event.time, seq: event.seq };
        messages.push(message);
        ensureTurn(event.data?.turn).assistants.push(message);
      }
    } else if (event.type === "tool/call") {
      toolCount += 1;
      const turn = ensureTurn(event.data?.turn);
      const tool = {
        callId: event.data?.callId || "",
        name: event.data?.name || "unknown_tool",
        arguments: parseJson(event.data?.arguments, {}),
        step: event.data?.step || 0,
        startedAt: event.time || 0,
        endedAt: 0,
        result: null,
        approval: null,
      };
      turn.tools.push(tool);
      if (tool.callId) calls.set(tool.callId, tool);
    } else if (event.type === "tool/result") {
      const result = toolResultData(event);
      const tool = calls.get(result.callId);
      if (tool) {
        tool.result = result;
        tool.endedAt = event.time || 0;
      }
    } else if (event.type === "approval/asked") {
      const approval = { id: event.data?.id || "", callId: event.data?.callId || "", reason: event.data?.reason || "", outcome: "" };
      approvals.set(approval.id, approval);
      const tool = calls.get(approval.callId);
      if (tool) tool.approval = approval;
    } else if (event.type === "approval/decided") {
      const approval = approvals.get(event.data?.id);
      if (approval) approval.outcome = event.data?.outcome || "unavailable";
      if (event.data?.id) state.agentApprovalBusy.delete(event.data.id);
    } else if (event.type === "turn/end" && event.data?.error) {
      lastError = event.data.error.message || String(event.data.error);
      const turn = ensureTurn(event.data?.turn);
      turn.status = "failed";
      turn.endedAt = event.time || 0;
    } else if (event.type === "turn/end") {
      const turn = ensureTurn(event.data?.turn);
      turn.status = "completed";
      turn.endedAt = event.time || 0;
    }
  }
  return { turns: turns.filter((turn) => turn.users.length || turn.assistants.length || turn.tools.length), messages, toolCount, lastError };
}

function renderAgentMessage(message) {
  const article = create("article", `agent-message ${message.role}`);
  const label = create("span", "agent-message-role", message.role === "user" ? "YOU" : "NOODLE");
  const body = create("div", "agent-message-body markdown-body");
  renderMarkdown(body, message.text || "");
  article.append(label, body);
  return article;
}

function renderAgentExecution(turn) {
  const section = create("section", "agent-execution");
  const header = create("header", "agent-execution-header");
  const title = create("div");
  title.append(create("span", "agent-execution-kicker", "执行轨迹"), create("strong", "", turnIntent(turn)));
  const completed = turn.tools.filter((tool) => toolStatus(tool) === "completed").length;
  const summary = create("span", "agent-execution-summary", `${completed}/${turn.tools.length} 步完成`);
  header.append(title, summary);
  section.append(header);

  const list = create("ol", "agent-tool-list");
  for (const tool of turn.tools) {
    const status = toolStatus(tool);
    const item = create("li", `agent-tool-step ${status}`);
    const marker = create("span", "agent-tool-marker", status === "completed" ? "✓" : status === "failed" || status === "rejected" ? "!" : status === "approval" ? "?" : "");
    const content = create("div", "agent-tool-content");
    const row = create("div", "agent-tool-row");
    const presentation = TOOL_PRESENTATION[tool.name] || [tool.name, "调用 Agent 工具"];
    const identity = create("div", "agent-tool-identity");
    identity.append(create("strong", "", presentation[0]), create("code", "", tool.name));
    const meta = create("span", "agent-tool-meta", status === "approval" ? "等待审批" : status === "running" ? "执行中" : status === "failed" ? "失败" : status === "rejected" ? "已拒绝" : durationLabel(tool.startedAt, tool.endedAt) || "完成");
    row.append(identity, meta);
    content.append(row, create("p", "agent-tool-description", toolResultSummary(tool)));

    if (Object.keys(tool.arguments || {}).length) {
      const details = create("details", "agent-tool-details");
      details.append(create("summary", "", "查看调用参数"));
      const pre = create("pre", "markdown-scroll-region");
      pre.tabIndex = 0;
      pre.append(create("code", "", JSON.stringify(tool.arguments, null, 2)));
      details.append(pre);
      content.append(details);
    }
    if (status === "approval") {
      const approval = create("div", "agent-approval-callout");
      approval.dataset.approvalId = tool.approval?.id || "";
      const copy = create("div");
      copy.append(create("strong", "", "需要你确认"), create("p", "", clipText(tool.approval?.reason || "该操作会修改知识库并创建新版本。", 150)));
      const actions = create("div", "agent-approval-actions");
      const reject = create("button", "agent-approval-reject", "拒绝");
      reject.type = "button";
      const allow = create("button", "agent-approval-allow", "仅允许本次");
      allow.type = "button";
      const busy = state.agentApprovalBusy.has(tool.approval?.id);
      reject.disabled = busy;
      allow.disabled = busy;
      reject.addEventListener("click", () => decideAgentApproval(tool.approval, "rejected"));
      allow.addEventListener("click", () => decideAgentApproval(tool.approval, "allowed-once"));
      const link = create("a", "", "高级面板 ↗");
      link.href = state.config.harness_web_url;
      link.target = "_blank";
      link.rel = "noopener";
      actions.append(reject, allow, link);
      if (busy) actions.prepend(create("span", "agent-approval-pending", "正在提交…"));
      approval.append(copy, actions);
      content.append(approval);
    }
    item.append(marker, content);
    list.append(item);
  }
  section.append(list);
  const footer = create("footer", "agent-execution-footer");
  footer.append(create("span", "", `共调用 ${turn.tools.length} 个工具`));
  const totalDuration = durationLabel(turn.startedAt, turn.endedAt);
  if (totalDuration) footer.append(create("span", "", `本轮 ${totalDuration}`));
  section.append(footer);
  return section;
}

async function decideAgentApproval(approval, outcome) {
  const approvalId = approval?.id;
  if (!state.agentSessionId || !approvalId || state.agentApprovalBusy.has(approvalId)) return;
  state.agentApprovalBusy.add(approvalId);
  state.agentHistorySignature = "";
  await loadAgentSessions({ quiet: true });
  try {
    await request(
      `${state.config.harness_chat_api}/sessions/${encodeURIComponent(state.agentSessionId)}/approvals/${encodeURIComponent(approvalId)}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ outcome }),
      },
    );
    toast(outcome === "allowed-once" ? "已允许本次操作" : "已拒绝本次操作");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.agentApprovalBusy.delete(approvalId);
    state.agentHistorySignature = "";
    await loadAgentSessions({ quiet: true });
  }
}

function renderAgentHistory(history) {
  const folded = foldAgentHistory(history.events);
  const signature = JSON.stringify({
    session: state.agentSessionId,
    seq: history.events?.at(-1)?.event?.seq ?? -1,
    running: state.agentRunning,
  });
  if (signature === state.agentHistorySignature) return;
  state.agentHistorySignature = signature;
  const panel = $("#agent-messages");
  panel.replaceChildren();
  if (!folded.messages.length) {
    const welcome = create("div", "agent-welcome");
    welcome.innerHTML = "<span>N</span><strong>与你的知识库一起工作</strong><p>询问笔记、核对引用，或让 Agent 使用 Noodle 工具修改当前文章。</p>";
    panel.append(welcome);
  }
  for (const turn of folded.turns) {
    for (const message of turn.users) panel.append(renderAgentMessage(message));
    if (turn.tools.length) panel.append(renderAgentExecution(turn));
    for (const message of turn.assistants) panel.append(renderAgentMessage(message));
  }
  if (folded.lastError) panel.append(create("div", "agent-error", folded.lastError));
  if (state.agentRunning) {
    const typing = create("div", "agent-typing");
    typing.innerHTML = "<i></i><i></i><i></i><span>Agent 正在执行</span>";
    panel.append(typing);
  }
  panel.scrollTop = panel.scrollHeight;
}

function renderAgentSessions() {
  const select = $("#agent-session-select");
  select.replaceChildren();
  for (const session of state.agentSessions) {
    if (session.origin === "subagent") continue;
    const option = document.createElement("option");
    option.value = session.sessionId;
    option.textContent = `${session.running ? "● " : ""}${sessionTitle(session)}`;
    option.selected = session.sessionId === state.agentSessionId;
    select.append(option);
  }
}

async function createAgentSession() {
  const suffix = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date());
  const created = await request(`${state.config.harness_chat_api}/sessions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ title: `Noodle 对话 · ${suffix}` }),
  });
  state.agentSessionId = created.sessionId;
  localStorage.setItem("noodle.agent.session", state.agentSessionId);
  state.agentHistorySignature = "";
  await loadAgentSessions();
  return created.sessionId;
}

async function renameAgentSession() {
  if (!state.agentSessionId) return;
  const current = state.agentSessions.find((item) => item.sessionId === state.agentSessionId);
  const previousTitle = current ? sessionTitle(current) : "新对话";
  const title = window.prompt("重命名当前对话", previousTitle)?.trim();
  if (!title || title === previousTitle) return;
  const button = $("#rename-agent-session");
  button.disabled = true;
  try {
    await request(
      `${state.config.harness_chat_api}/sessions/${encodeURIComponent(state.agentSessionId)}`,
      {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ title }),
      },
    );
    await loadAgentSessions({ quiet: true });
    toast(`对话已重命名为“${title}”`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function loadAgentSessions({ quiet = false } = {}) {
  if (state.agentBusy) return;
  state.agentBusy = true;
  try {
    const result = await request(`${state.config.harness_chat_api}/sessions`);
    state.agentSessions = (result.items || []).filter((item) => item.origin !== "subagent");
    if (!state.agentSessions.some((item) => item.sessionId === state.agentSessionId)) {
      state.agentSessionId = state.agentSessions[0]?.sessionId || null;
    }
    if (!state.agentSessionId) {
      state.agentBusy = false;
      await createAgentSession();
      return;
    }
    localStorage.setItem("noodle.agent.session", state.agentSessionId);
    const current = state.agentSessions.find((item) => item.sessionId === state.agentSessionId);
    state.agentRunning = Boolean(current?.running);
    renderAgentSessions();
    $("#rename-agent-session").disabled = !state.agentSessionId;
    $("#cancel-agent-run").hidden = !state.agentRunning;
    $("#send-agent-message").disabled = state.agentSending;
    setAgentStatus(state.agentRunning ? "Agent 正在执行" : "Harness 已连接", state.agentRunning ? "running" : "online");
    const history = await request(`${state.config.harness_chat_api}/sessions/${encodeURIComponent(state.agentSessionId)}/history?max_messages=100`);
    renderAgentHistory(history);
  } catch (error) {
    setAgentStatus("Harness 未连接", "error");
    $("#send-agent-message").disabled = true;
    if (!quiet) toast(error.message, "error");
  } finally {
    state.agentBusy = false;
  }
}

async function sendAgentMessage(event) {
  event.preventDefault();
  if (state.agentSending) return;
  const input = $("#agent-input");
  let message = input.value.trim();
  if (!message && !state.agentAttachments.length) return;
  if (!message) message = "请查看我粘贴的图片，并结合当前笔记上下文协助我处理。";
  state.agentSending = true;
  renderAgentAttachments();
  try {
    if (!state.agentSessionId) await createAgentSession();
    $("#send-agent-message").disabled = true;
    setAgentStatus(state.agentAttachments.length ? "正在上传并理解图片" : "正在提交任务", "running");
    const attachments = await uploadAgentAttachments();
    const visibleAttachments = attachmentMessageMarkdown(attachments);
    const context = `${agentPrompt(state.agentTargetNote)}${attachmentAgentContext(attachments)}`;
    setAgentStatus("正在提交任务", "running");
    await request(`${state.config.harness_chat_api}/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        session_id: state.agentSessionId,
        message: visibleAttachments ? `${message}\n\n${visibleAttachments}` : message,
        context,
        mode: state.agentRunning ? "steer" : "queue",
      }),
    });
    input.value = "";
    clearAgentAttachments();
    state.agentHistorySignature = "";
    await loadAgentSessions();
  } catch (error) {
    toast(error.message, "error");
    $("#send-agent-message").disabled = false;
  } finally {
    state.agentSending = false;
    renderAgentAttachments();
    if (!$("#agent-status").classList.contains("error")) $("#send-agent-message").disabled = false;
  }
}

async function cancelAgentRun() {
  if (!state.agentSessionId) return;
  try {
    await request(`${state.config.harness_chat_api}/sessions/${encodeURIComponent(state.agentSessionId)}/cancel`, { method: "POST" });
    await loadAgentSessions();
  } catch (error) { toast(error.message, "error"); }
}

function applyReaderMode(mode) {
  state.readerMode = mode;
  $$('[data-reader-mode]').forEach((button) => button.classList.toggle("active", button.dataset.readerMode === mode));
  $("#outline-tree").hidden = mode !== "outline";
  $("#reader-file-list").hidden = mode !== "files";
}

function applyPanelState() {
  const app = $("#app");
  app.classList.toggle("library-collapsed", localStorage.getItem("noodle.ui.libraryCollapsed") === "true");
  app.classList.toggle("agent-collapsed", localStorage.getItem("noodle.ui.agentCollapsed") === "true");
}

function applyMobileView(view) {
  const app = $("#app");
  app.classList.toggle("mobile-library", view === "library");
  app.classList.toggle("mobile-agent", view === "agent");
  if (view === "jobs") {
    app.classList.remove("mobile-library", "mobile-agent");
    state.drawerOpen = true;
    applyDrawerState();
  }
  if (view === "reader" && !state.sourceNote) toast("请先从知识库选择一份已完成的笔记");
  $$('[data-mobile-view]').forEach((button) => button.classList.toggle("active", button.dataset.mobileView === view));
}

function bindEvents() {
  $("#brand-home").addEventListener("click", () => selectFolder(state.selectedFolder));
  $("#open-syntheses").addEventListener("click", showSynthesisLibrary);
  $("#create-synthesis").addEventListener("click", () => openSynthesisEditor());
  $("#edit-synthesis").addEventListener("click", () => {
    if (state.synthesis?.synthesis_id) openSynthesisEditor(state.synthesis.synthesis_id);
  });
  $("#close-synthesis-editor").addEventListener("click", closeSynthesisEditor);
  $("#cancel-synthesis-editor").addEventListener("click", closeSynthesisEditor);
  $("#synthesis-editor").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeSynthesisEditor();
  });
  $("#synthesis-editor-form").addEventListener("submit", publishSynthesisEditor);
  $("#synthesis-title-input").addEventListener("input", (event) => {
    if (state.synthesisEditor?.mode !== "create") return;
    const key = $("#synthesis-key-input");
    if (!key.dataset.edited) key.value = synthesisSlug(event.target.value);
  });
  $("#synthesis-key-input").addEventListener("input", (event) => { event.target.dataset.edited = "true"; });
  $("#add-synthesis-group").addEventListener("click", () => {
    const title = window.prompt("分组名称，例如：基础、模型架构、训练与对齐");
    if (!title?.trim()) return;
    const group = editorNode({ stable_key: uniqueEditorKey(title), title: title.trim(), node_kind: "GROUP", children: [] });
    state.synthesisEditor.outline.push(group);
    state.synthesisEditor.selectedGroupUid = group._uid;
    renderSynthesisDraft();
    $("#synthesis-editor-status").textContent = `已添加分组「${group.title}」`;
  });
  $("#concept-registry-search").addEventListener("input", (event) => {
    window.clearTimeout(state.conceptRegistryTimer);
    state.conceptRegistryTimer = window.setTimeout(() => loadConceptRegistry(event.target.value.trim()), 220);
  });
  $("#merge-concepts").addEventListener("click", mergeConceptsFromEditor);
  $("#back-to-syntheses").addEventListener("click", showSynthesisLibrary);
  $("#synthesis-reader-back").addEventListener("click", showSynthesisLibrary);
  $("#refresh-library").addEventListener("click", () => loadLibrary());
  $("#open-upload").addEventListener("click", openUpload);
  $('[data-action="empty-upload"]').addEventListener("click", openUpload);
  $("#cancel-upload").addEventListener("click", closeUpload);
  $("#upload-form").addEventListener("submit", uploadPdf);
  $("#pdf-file").addEventListener("change", () => { $("#pdf-file-name").textContent = $("#pdf-file").files[0]?.name || "或选择一个来源文件"; });
  const drop = $("#drop-zone");
  for (const type of ["dragenter", "dragover"]) drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add("dragging"); });
  for (const type of ["dragleave", "drop"]) drop.addEventListener(type, () => drop.classList.remove("dragging"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    const file = [...event.dataTransfer.files].find((item) => item.type === "application/pdf" || item.name.toLowerCase().endsWith(".pdf"));
    if (!file) return toast("只支持 PDF 文件", "error");
    const transfer = new DataTransfer();
    transfer.items.add(file);
    $("#pdf-file").files = transfer.files;
    $("#pdf-file-name").textContent = file.name;
  });
  $("#library-search").addEventListener("input", (event) => { state.search = event.target.value.trim(); renderSourceTable(); });
  $("#source-sort").value = state.sort;
  $("#source-sort").addEventListener("change", (event) => { state.sort = event.target.value; localStorage.setItem("noodle.ui.sort", state.sort); renderSourceTable(); });
  $$('[data-smart-folder]').forEach((button) => button.addEventListener("click", () => selectFolder(button.dataset.smartFolder)));
  $("#new-folder").addEventListener("click", createFolder);
  $("#back-to-library").addEventListener("click", () => selectFolder(state.selectedFolder));
  $$('[data-content-mode]').forEach((button) => button.addEventListener("click", () => switchContentMode(button.dataset.contentMode)));
  $("#reader-view").addEventListener("scroll", scheduleNoteAnchorSync, { passive: true });
  $("#source-pdf-viewer").addEventListener("scroll", syncPdfPageFromScroll, { passive: true });
  let pdfResizeTimer = null;
  window.addEventListener("resize", () => {
    window.clearTimeout(pdfResizeTimer);
    pdfResizeTimer = window.setTimeout(rerenderVisiblePdfPages, 180);
  });
  $$('[data-reader-mode]').forEach((button) => button.addEventListener("click", () => applyReaderMode(button.dataset.readerMode)));
  $("#job-drawer-toggle").addEventListener("click", () => { state.drawerOpen = !state.drawerOpen; applyDrawerState(); if (state.drawerOpen && state.selectedJobId) selectJob(state.selectedJobId); });
  $("#collapse-library").addEventListener("click", () => { localStorage.setItem("noodle.ui.libraryCollapsed", "true"); applyPanelState(); window.setTimeout(rerenderVisiblePdfPages, 260); });
  $("#expand-library").addEventListener("click", () => { localStorage.setItem("noodle.ui.libraryCollapsed", "false"); applyPanelState(); window.setTimeout(rerenderVisiblePdfPages, 260); });
  $("#collapse-agent").addEventListener("click", () => { localStorage.setItem("noodle.ui.agentCollapsed", "true"); applyPanelState(); window.setTimeout(rerenderVisiblePdfPages, 260); });
  $("#expand-agent").addEventListener("click", () => { localStorage.setItem("noodle.ui.agentCollapsed", "false"); applyPanelState(); window.setTimeout(rerenderVisiblePdfPages, 260); });
  $("#agent-form").addEventListener("submit", sendAgentMessage);
  $("#agent-input").addEventListener("paste", handleAgentPaste);
  $("#agent-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#agent-form").requestSubmit();
    }
  });
  $("#add-agent-image").addEventListener("click", () => $("#agent-image-file").click());
  $("#agent-image-file").addEventListener("change", (event) => {
    addAgentImageFiles(event.target.files || []);
    event.target.value = "";
  });
  $("#agent-session-select").addEventListener("change", async (event) => {
    state.agentSessionId = event.target.value;
    localStorage.setItem("noodle.agent.session", state.agentSessionId);
    state.agentHistorySignature = "";
    await loadAgentSessions();
  });
  $("#new-agent-session").addEventListener("click", async () => {
    try { await createAgentSession(); $("#agent-input").focus(); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#rename-agent-session").addEventListener("click", renameAgentSession);
  $("#cancel-agent-run").addEventListener("click", cancelAgentRun);
  $$('[data-mobile-view]').forEach((button) => button.addEventListener("click", () => applyMobileView(button.dataset.mobileView)));
}

async function init() {
  bindEvents();
  applyPanelState();
  applyDrawerState();
  applyReaderMode("outline");
  try {
    state.config = { ...state.config, ...(await request(`${API}/ui-config`)) };
  } catch (_) { /* the default Harness URL remains usable */ }
  $("#open-harness").href = state.config.harness_web_url;
  await Promise.all([loadLibrary(), loadSyntheses(), loadAgentSessions()]);
  const hash = decodeURIComponent(location.hash || "");
  if (hash.startsWith("#synthesis/")) {
    const parts = hash.slice("#synthesis/".length).split("/concept/");
    await openSynthesis(parts[0], parts[1] || null);
  } else if (hash === "#syntheses") {
    showSynthesisLibrary();
  } else if (hash.startsWith("#source/")) {
    const sourceId = hash.slice("#source/".length);
    const source = state.sources.find((item) => item.source_id === sourceId);
    if (source) await openSource(source);
  } else if (hash.startsWith("#library/")) {
    const folder = hash.slice("#library/".length);
    if (["all", "running", "failed", "unfiled"].includes(folder) || state.folders.some((item) => item.id === folder)) selectFolder(folder);
  }
  if (state.selectedJobId && state.drawerOpen) selectJob(state.selectedJobId);
  window.setInterval(() => loadLibrary({ quiet: true }), Math.max(1200, Number(state.config.poll_interval_ms || 2500)));
  window.setInterval(() => loadSyntheses({ quiet: true }), 30000);
  window.setInterval(refreshElapsedClocks, 1000);
  window.setInterval(() => loadAgentSessions({ quiet: true }), 1400);
}

init();
