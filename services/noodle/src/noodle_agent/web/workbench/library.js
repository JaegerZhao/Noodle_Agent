// library workbench responsibilities. State is shared explicitly through imports.
import { $, $$, API, runningStatuses, stageLabels, state } from './state.js';
import { create, createElapsedClock, folderDepth, folderName, folderOptions, formatBytes, formatDate, formatTokens, parseApiTimestamp, request, statusMeta, toast } from './common.js';
import { openSource, reloadOpenSource, showWorkbench } from './reader.js';
import { refreshSelectedJobDetail, renderJobs } from './jobs.js';

export function filteredSources() {
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

export function renderFolderNavigation() {
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

export function selectedFolderTitle() {
  const smart = { all: "全部笔记", running: "正在处理", failed: "处理失败", unfiled: "未分类" };
  return smart[state.selectedFolder] || folderName(state.selectedFolder);
}

export function renderSourceTable() {
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

export function renderSourceActions(source) {
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

export function bindSourceDrag(row) {
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

export async function moveSource(sourceId, folderId) {
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

export async function retryFailedJob(source, button) {
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

export async function refreshKnowledgeSyncStatus() {
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

export async function deleteSource(source, button) {
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

export async function createFolder() {
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

export async function renameFolder(folder) {
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

export function selectFolder(folderId) {
  state.selectedFolder = folderId;
  localStorage.setItem("noodle.ui.folder", folderId);
  showWorkbench();
  renderFolderNavigation();
  renderSourceTable();
  history.replaceState(null, "", `#library/${encodeURIComponent(folderId)}`);
}

export function sourceReleaseChanged(currentNote, freshSource) {
  return Boolean(
    currentNote
    && freshSource?.source_note_version_id
    && freshSource.source_note_version_id !== currentNote.version_id
  );
}

export async function loadLibrary({ quiet = false } = {}) {
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
