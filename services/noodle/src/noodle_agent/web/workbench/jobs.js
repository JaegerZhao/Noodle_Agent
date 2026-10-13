// jobs workbench responsibilities. State is shared explicitly through imports.
import { $, API, runningStatuses, stageLabels, state, terminalStatuses } from './state.js';
import { create, createElapsedClock, escapeHtml, formatDuration, formatTokens, renderJobWarnings, request, toast } from './common.js';
import { loadLibrary } from './library.js';

export function openUpload() {
  $("#upload-zone").hidden = false;
  $("#pdf-file").focus();
}

export function closeUpload() {
  $("#upload-zone").hidden = true;
  $("#upload-form").reset();
  $("#pdf-file-name").textContent = "或选择一个来源文件";
  $("#upload-status").textContent = "上传后由 Worker 在后台处理。";
}

export async function uploadPdf(event) {
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

export function sourceForJob(jobId) {
  return state.sources.find((source) => source.job?.id === jobId);
}

export function renderJobs() {
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

export async function selectJob(jobId) {
  state.selectedJobId = jobId;
  state.drawerOpen = true;
  localStorage.setItem("noodle.ui.job", jobId);
  renderJobs();
  applyDrawerState();
  $("#job-detail").innerHTML = '<p class="empty-detail">正在读取任务时间线…</p>';
  await refreshSelectedJobDetail();
}

export async function refreshSelectedJobDetail({ quiet = false } = {}) {
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

export function renderJobDetail(timeline) {
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

export function applyDrawerState() {
  $("#job-drawer").classList.toggle("collapsed", !state.drawerOpen);
  $("#job-drawer-toggle").setAttribute("aria-expanded", String(state.drawerOpen));
  $("#job-drawer-arrow").textContent = state.drawerOpen ? "⌄" : "⌃";
  localStorage.setItem("noodle.ui.drawer", String(state.drawerOpen));
}
