const state = {
  notes: [],
  sourceNotes: [],
  filtered: [],
  selectedId: null,
  conversationId: localStorage.getItem("noodle.conversationId"),
  currentJobId: localStorage.getItem("noodle.currentJobId"),
  openedJobResultId: null,
  pendingAttachment: null,
  libraryCollapsed: localStorage.getItem("noodle.libraryCollapsed") === "true",
  chatCollapsed: localStorage.getItem("noodle.chatCollapsed") === "true",
};

const $ = (selector) => document.querySelector(selector);
const noteList = $("#note-list");
const chatMessages = $("#chat-messages");
let jobPollTimer = null;

function updatePanelToggle(button, { collapsed, panelName, expandedIcon, collapsedIcon }) {
  const action = collapsed ? "展开" : "收起";
  const label = panelName === "library" ? "左侧笔记库" : "右侧问答";
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", `${action}${label}`);
  button.title = `${action}${label}`;
  button.classList.toggle("is-collapsed", collapsed);
  button.querySelector(".panel-toggle-icon").textContent = collapsed ? collapsedIcon : expandedIcon;
}

function applyPanelVisibility() {
  const shell = $(".app-shell");
  shell.classList.toggle("library-collapsed", state.libraryCollapsed);
  shell.classList.toggle("chat-collapsed", state.chatCollapsed);
  updatePanelToggle($("#toggle-library"), {
    collapsed: state.libraryCollapsed,
    panelName: "library",
    expandedIcon: "‹",
    collapsedIcon: "›",
  });
  updatePanelToggle($("#toggle-chat"), {
    collapsed: state.chatCollapsed,
    panelName: "chat",
    expandedIcon: "›",
    collapsedIcon: "‹",
  });
}

function toggleWorkspacePanel(panelName) {
  if (panelName === "library") {
    state.libraryCollapsed = !state.libraryCollapsed;
    localStorage.setItem("noodle.libraryCollapsed", String(state.libraryCollapsed));
  } else {
    state.chatCollapsed = !state.chatCollapsed;
    localStorage.setItem("noodle.chatCollapsed", String(state.chatCollapsed));
  }
  applyPanelVisibility();
}

function createText(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text ?? "";
  if (className) node.className = className;
  return node;
}

const mathTokenPattern = /\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$(?!\$)(?:\\.|[^$\n])+\$/g;

function protectMath(source) {
  const expressions = [];
  const text = String(source ?? "").replace(mathTokenPattern, (expression) => {
    const token = `NOODLEMATHTOKEN${expressions.length}END`;
    expressions.push(expression);
    return token;
  });
  return { text, expressions };
}

function restoreMathTokens(root, expressions) {
  if (!expressions.length) return;
  const tokenPattern = /NOODLEMATHTOKEN(\d+)END/g;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  textNodes.forEach((textNode) => {
    const value = textNode.nodeValue || "";
    if (!value.includes("NOODLEMATHTOKEN")) return;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    let match;
    tokenPattern.lastIndex = 0;
    while ((match = tokenPattern.exec(value)) !== null) {
      fragment.append(document.createTextNode(value.slice(cursor, match.index)));
      fragment.append(document.createTextNode(expressions[Number(match[1])] || match[0]));
      cursor = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(value.slice(cursor)));
    textNode.replaceWith(fragment);
  });
}

function typesetMath(root) {
  if (typeof window.renderMathInElement !== "function") return;
  window.renderMathInElement(root, {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
      { left: "$", right: "$", display: false },
    ],
    ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    throwOnError: false,
    strict: "ignore",
    trust: false,
    errorCallback: (message) => {
      root.classList.add("math-render-warning");
      console.warn("KaTeX rendering warning:", message);
    },
  });
}

function renderMarkdown(target, source, { inline = false } = {}) {
  const value = String(source ?? "");
  if (!window.marked || !window.DOMPurify) {
    target.textContent = value;
    return target;
  }
  const protectedMath = protectMath(value);
  const rendered = inline
    ? window.marked.parseInline(protectedMath.text, { gfm: true, breaks: true })
    : window.marked.parse(protectedMath.text, { gfm: true, breaks: true });
  target.innerHTML = window.DOMPurify.sanitize(rendered, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed"],
  });
  restoreMathTokens(target, protectedMath.expressions);
  typesetMath(target);
  return target;
}

function formulaSource(source) {
  const value = String(source ?? "").trim();
  if (value.startsWith("$$") && value.endsWith("$$")) return value.slice(2, -2).trim();
  if (value.startsWith("\\[") && value.endsWith("\\]")) return value.slice(2, -2).trim();
  if (value.startsWith("\\(") && value.endsWith("\\)")) return value.slice(2, -2).trim();
  if (value.startsWith("$") && value.endsWith("$")) return value.slice(1, -1).trim();
  return value;
}

function createFormula(source) {
  const node = document.createElement("div");
  node.className = "formula-block";
  const formula = formulaSource(source);
  if (!formula) return node;
  if (!window.katex) {
    node.textContent = formula;
    node.classList.add("formula-fallback");
    return node;
  }
  window.katex.render(formula, node, {
    displayMode: true,
    throwOnError: false,
    strict: "ignore",
    trust: false,
    output: "htmlAndMathml",
  });
  return node;
}

function displayCaption(asset) {
  const caption = (asset.caption || "").trim();
  if (!caption || /<\/?(?:table|tr|td|th)\b/i.test(caption)) {
    return asset.purpose || asset.alt_text || "来源图示";
  }
  return caption.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function displayAssetPages(asset) {
  const pages = (asset.source_pages || [asset.page_label])
    .map((page) => String(page || "").replace(/^p\.?/i, ""))
    .filter(Boolean);
  if (!pages.length) return "?";
  const numeric = pages.map(Number);
  if (numeric.every(Number.isFinite) && numeric.length > 1) {
    const sorted = [...new Set(numeric)].sort((left, right) => left - right);
    const continuous = sorted.every((page, index) => index === 0 || page === sorted[index - 1] + 1);
    if (continuous) return `${sorted[0]}–${sorted.at(-1)}`;
  }
  return [...new Set(pages)].join("、");
}

function createAssetFigure(asset, fallbackTitle = "来源图片") {
  const figure = document.createElement("figure");
  figure.className = "asset-card inline-asset";
  const img = document.createElement("img");
  img.src = `/blobs/${asset.storage_key.split("/").map(encodeURIComponent).join("/")}`;
  img.alt = asset.alt_text || asset.caption || fallbackTitle;
  img.loading = "lazy";
  const caption = document.createElement("figcaption");
  caption.append(createText("span", `P.${displayAssetPages(asset)}`, "page-chip"));
  caption.append(document.createTextNode(displayCaption(asset)));
  figure.append(img, caption);
  return figure;
}

function renderStructuredBlock(block, container, { showLabel = false, renderedAssets = null } = {}) {
  const wrap = document.createElement("div");
  wrap.className = `content-block block-${String(block.type || "paragraph").toLowerCase()}`;
  if (showLabel) wrap.append(createText("span", `${block.type || "PARAGRAPH"} · ${block.origin}`, "block-label"));
  const type = String(block.type || "PARAGRAPH").toUpperCase();
  if (type === "BULLETED_LIST" || type === "NUMBERED_LIST") {
    const list = document.createElement(type === "BULLETED_LIST" ? "ul" : "ol");
    (block.items || []).forEach((item) => {
      const li = document.createElement("li");
      renderMarkdown(li, item, { inline: true });
      list.append(li);
    });
    wrap.append(list);
  } else if (type === "TABLE") {
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    (block.table_headers || []).forEach((cell) => {
      const th = document.createElement("th");
      renderMarkdown(th, cell, { inline: true });
      headRow.append(th);
    });
    head.append(headRow);
    const body = document.createElement("tbody");
    (block.table_rows || []).forEach((row) => {
      const tr = document.createElement("tr");
      row.forEach((cell) => {
        const td = document.createElement("td");
        renderMarkdown(td, cell, { inline: true });
        tr.append(td);
      });
      body.append(tr);
    });
    table.append(head, body);
    wrap.append(table);
  } else if (type === "FORMULA") {
    wrap.append(createFormula(block.text));
  } else if (type === "CODE") {
    const pre = document.createElement("pre");
    pre.className = "code-block";
    const code = createText("code", block.text);
    if (block.language) code.className = `language-${String(block.language).toLowerCase().replace(/[^a-z0-9_-]/g, "")}`;
    pre.append(code);
    wrap.append(pre);
  } else if (type === "CALLOUT") {
    const callout = document.createElement("blockquote");
    callout.className = "callout-block markdown-body";
    renderMarkdown(callout, block.text);
    wrap.append(callout);
  } else if (type !== "IMAGE" && block.text) {
    const body = document.createElement("div");
    body.className = "markdown-body";
    renderMarkdown(body, block.text);
    wrap.append(body);
  }
  (block.assets || []).forEach((asset) => {
    const key = asset.storage_key || asset.visual_asset_id;
    if (renderedAssets && key && renderedAssets.has(key)) return;
    wrap.append(createAssetFigure(asset));
    if (renderedAssets && key) renderedAssets.add(key);
  });
  container.append(wrap);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) { /* no JSON body */ }
    throw new Error(message);
  }
  return response.json();
}

function setConnection(online, message) {
  $(".pulse").classList.toggle("online", online);
  $("#system-status").textContent = message;
}

function renderNotes() {
  noteList.replaceChildren();
  $("#note-count").textContent = state.filtered.length;
  if (!state.filtered.length) {
    noteList.append(createText("p", state.notes.length ? "没有匹配的笔记。" : "知识库中还没有已激活的笔记。", "empty-list"));
    return;
  }
  const template = $("#note-item-template");
  state.filtered.forEach((note, index) => {
    const item = template.content.firstElementChild.cloneNode(true);
    const entryId = `${note.kind}:${note.note_id || note.source_note_id}`;
    item.dataset.noteId = entryId;
    item.classList.toggle("active", entryId === state.selectedId);
    item.querySelector(".note-item-index").textContent = String(index + 1).padStart(2, "0");
    item.querySelector("strong").textContent = note.title;
    item.querySelector("small").textContent = note.kind === "source" ? `完整笔记 · ${note.summary}` : note.thesis;
    item.addEventListener("click", () => note.kind === "source" ? loadSourceNote(note.source_note_id) : loadNote(note.note_id));
    noteList.append(item);
  });
}

function showMobilePanel(name) {
  document.querySelectorAll("[data-panel-name]").forEach((panel) => panel.classList.toggle("panel-active", panel.dataset.panelName === name));
  document.querySelectorAll(".mobile-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.panel === name));
}

async function loadNote(noteId) {
  state.selectedId = `note:${noteId}`;
  renderNotes();
  $("#empty-note").hidden = true;
  $("#note-view").hidden = false;
  $("#note-blocks").replaceChildren(createText("div", "正在读取数据库…", "loading-line"));
  if (window.innerWidth <= 720) showMobilePanel("reader");
  try {
    const note = await request(`/notes/${encodeURIComponent(noteId)}`);
    $("#note-title").textContent = note.title;
    $("#note-thesis").textContent = "";
    $("#note-thesis").hidden = true;
    $("#note-status").textContent = note.review === "APPROVED" ? "人工审核完成" : "AI 已验证";
    $("#note-origin").textContent = note.review === "APPROVED" ? "含人工编辑块" : "源自 PDF · 块级溯源";

    const tags = $("#note-tags");
    tags.replaceChildren(...(note.tags || []).map((tag) => createText("span", tag, "tag")));

    const blocks = $("#note-blocks");
    blocks.replaceChildren();
    const renderedAssets = new Set();
    (note.blocks || []).forEach((block) => renderStructuredBlock(
      block,
      blocks,
      { renderedAssets },
    ));

    const assetsSection = $("#note-assets");
    const assetList = $("#asset-list");
    assetList.replaceChildren();
    assetsSection.hidden = !(note.assets || []).length;
    (note.assets || []).forEach((asset) => {
      const key = asset.storage_key || asset.visual_asset_id;
      if (key && renderedAssets.has(key)) return;
      assetList.append(createAssetFigure(asset));
      if (key) renderedAssets.add(key);
    });
    assetsSection.hidden = assetList.childElementCount === 0;

    const evidenceList = $("#evidence-list");
    evidenceList.replaceChildren();
    (note.evidence || []).forEach((evidence) => {
      const card = document.createElement("div");
      card.className = "evidence-card";
      card.append(createText("span", `PDF · P.${evidence.page_label}`, "page-chip"));
      card.append(createText("p", evidence.quote || "该页提供结构或视觉证据。"));
      evidenceList.append(card);
    });
    if (!(note.evidence || []).length) evidenceList.append(createText("p", "当前笔记没有可展示的证据片段。", "empty-list"));
  } catch (error) {
    $("#note-blocks").replaceChildren(createText("p", `读取失败：${error.message}`, "empty-list"));
  }
}

async function loadSourceNote(sourceNoteId) {
  state.selectedId = `source:${sourceNoteId}`;
  renderNotes();
  $("#empty-note").hidden = true;
  $("#note-view").hidden = false;
  $("#note-blocks").replaceChildren(createText("div", "正在组装完整笔记…", "loading-line"));
  if (window.innerWidth <= 720) showMobilePanel("reader");
  try {
    const sourceNote = await request(`/source-notes/${encodeURIComponent(sourceNoteId)}`);
    $("#note-title").textContent = sourceNote.title;
    $("#note-thesis").textContent = sourceNote.summary;
    $("#note-thesis").hidden = !sourceNote.summary;
    $("#note-status").textContent = "完整来源笔记";
    $("#note-origin").textContent = `${sourceNote.unit_count} 个原子知识单元 · ${sourceNote.source_file}`;
    $("#note-tags").replaceChildren(createText("span", "Source Note", "tag"));

    const blocks = $("#note-blocks");
    const assetList = $("#asset-list");
    const evidenceList = $("#evidence-list");
    blocks.replaceChildren();
    assetList.replaceChildren();
    evidenceList.replaceChildren();
    let assetCount = 0;
    let evidenceCount = 0;

    const renderedAssets = new Set();
    const normalizeTitle = (value) => String(value || "").replace(/[：:·\s]/g, "").toLocaleLowerCase("zh-CN");
    const renderOutline = (nodes, levelOffset = 0) => {
      nodes.forEach((node) => {
        const heading = createText(`h${Math.min(6, node.level + 2 - levelOffset)}`, node.title, "source-outline-heading");
        blocks.append(heading);
        const nodeNotes = node.notes || [];
        nodeNotes.forEach((note) => {
          const wrap = document.createElement("section");
          wrap.className = "content-block source-unit";
          if (nodeNotes.length > 1 && normalizeTitle(note.title) !== normalizeTitle(node.title)) {
            wrap.append(createText("h4", note.title));
          }
          (note.blocks || []).forEach((block) => renderStructuredBlock(
            block,
            wrap,
            { renderedAssets },
          ));
          blocks.append(wrap);
          (note.assets || []).forEach((asset) => {
            const key = asset.storage_key || asset.visual_asset_id;
            if (key && renderedAssets.has(key)) return;
            assetList.append(createAssetFigure(asset, note.title));
            if (key) renderedAssets.add(key);
            assetCount += 1;
          });
          (note.evidence || []).forEach((evidence) => {
            const card = document.createElement("div");
            card.className = "evidence-card";
            card.append(createText("span", `PDF · P.${evidence.page_label}`, "page-chip"));
            card.append(createText("p", evidence.quote || "该页提供结构或视觉证据。"));
            evidenceList.append(card);
            evidenceCount += 1;
          });
        });
        renderOutline(node.children || [], levelOffset);
      });
    };
    let renderedOutline = sourceNote.outline || [];
    let levelOffset = 0;
    if (
      renderedOutline.length === 1
      && renderedOutline[0].title.trim().toLocaleLowerCase("zh-CN") === sourceNote.title.trim().toLocaleLowerCase("zh-CN")
      && !(renderedOutline[0].notes || []).length
    ) {
      renderedOutline = renderedOutline[0].children || [];
      levelOffset = 1;
    }
    renderOutline(renderedOutline, levelOffset);
    $("#note-assets").hidden = assetCount === 0;
    if (!evidenceCount) evidenceList.append(createText("p", "当前完整笔记没有可展示的证据片段。", "empty-list"));
  } catch (error) {
    $("#note-blocks").replaceChildren(createText("p", `读取失败：${error.message}`, "empty-list"));
  }
}

function appendMessage(
  text,
  role,
  hits = [],
  warning = null,
  toolCalls = [],
  skill = null,
  runId = null,
) {
  const message = document.createElement("div");
  message.className = `message ${role === "user" ? "user-message" : "assistant-message"}`;
  if (role === "user") {
    message.append(createText("p", text));
  } else {
    const body = document.createElement("div");
    body.className = "message-body markdown-body";
    renderMarkdown(body, text);
    message.append(body);
  }
  if (warning) message.append(createText("p", `覆盖提示：${warning}`, "coverage-warning"));
  if (role !== "user" && (skill || toolCalls.length)) {
    const run = document.createElement("div");
    run.className = "agent-run-line";
    if (skill) run.append(createText("span", `SKILL · ${skill}`, "agent-skill-chip"));
    toolCalls.forEach((call) => {
      run.append(createText("span", `${call.tool_name} · ${call.risk_level} · ${call.status}`, "agent-tool-chip"));
    });
    message.append(run);
  }
  if (role !== "user" && runId) {
    const trace = document.createElement("details");
    trace.className = "agent-trace";
    trace.append(createText("summary", `Agent Run · ${runId.slice(0, 8)}`));
    const traceBody = createText("pre", "展开后读取持久化运行轨迹…");
    trace.append(traceBody);
    trace.addEventListener("toggle", async () => {
      if (!trace.open || trace.dataset.loaded) return;
      try {
        const run = await request(`/agent/runs/${encodeURIComponent(runId)}`);
        traceBody.textContent = JSON.stringify({
          status: run.status,
          skill: run.skill,
          step_count: run.step_count,
          context: run.context,
          tool_calls: run.tool_calls,
        }, null, 2);
        trace.dataset.loaded = "true";
      } catch (error) {
        traceBody.textContent = `运行轨迹读取失败：${error.message}`;
      }
    });
    message.append(trace);
  }
  if (hits.length) {
    const sources = document.createElement("div");
    sources.className = "answer-sources";
    hits.forEach((hit) => {
      const pages = (hit.pages || [...new Set((hit.evidence || []).map((item) => item.page_label))]).join("、");
      const button = createText("button", `${hit.title}${pages ? ` · P.${pages}` : ""}`, "answer-source");
      button.type = "button";
      button.addEventListener("click", () => {
        if (hit.source_note_id) loadSourceNote(hit.source_note_id);
        else if (hit.note_id) loadNote(hit.note_id);
      });
      sources.append(button);
    });
    message.append(sources);
  }
  chatMessages.append(message);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function loadNotes({ autoSelect = true } = {}) {
  try {
    await request("/health");
    state.notes = await request("/notes");
    state.sourceNotes = await request("/source-notes");
    state.filtered = [
      ...state.sourceNotes.map((note) => ({ ...note, kind: "source" })),
      ...state.notes.map((note) => ({ ...note, kind: "note" })),
    ];
    renderNotes();
    setConnection(true, `本地知识库在线 · ${state.sourceNotes.length} 篇完整笔记 · ${state.notes.length} 个知识单元`);
    if (autoSelect && !state.selectedId) {
      if (state.sourceNotes.length) loadSourceNote(state.sourceNotes[0].source_note_id);
      else if (state.notes.length) loadNote(state.notes[0].note_id);
    }
  } catch (error) {
    setConnection(false, "本地服务未连接");
    noteList.replaceChildren(createText("p", `无法连接：${error.message}`, "empty-list"));
  }
}

const terminalJobStatuses = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"]);

async function pollJob(jobId) {
  try {
    const monitor = $("#job-monitor-status");
    const timelineNode = $("#job-timeline");
    const [job, timeline] = await Promise.all([
      request(`/jobs/${encodeURIComponent(jobId)}`),
      request(`/jobs/${encodeURIComponent(jobId)}/timeline`),
    ]);
    monitor.textContent = `${Math.round(job.progress * 100)}% · ${job.stage}`;
    $("#job-progress-bar").style.width = `${Math.round(job.progress * 100)}%`;
    $("#job-monitor").open = !terminalJobStatuses.has(job.status);
    $("#resume-job").hidden = job.status !== "FAILED";
    timelineNode.replaceChildren();
    (timeline.stages || []).forEach((stage) => {
      const row = document.createElement("div");
      row.className = `job-stage ${String(stage.status).toLowerCase()}`;
      row.append(createText("code", stage.stage));
      row.append(createText("span", stage.duration_ms == null ? "进行中" : `${(stage.duration_ms / 1000).toFixed(1)}s`));
      timelineNode.append(row);
    });
    if (!(timeline.stages || []).length) timelineNode.append(createText("p", "任务尚未产生阶段事件。", "empty-list"));
    const message = $("#job-message");
    if (job.status === "COMPLETED_WITH_WARNINGS") {
      message.textContent = `任务已完成，但存在 ${job.warnings?.length || 1} 条覆盖或视觉警告。`;
      message.className = "job-message warning";
    } else if (job.status === "FAILED") {
      message.textContent = `${job.error_code || "任务失败"}：${job.error_detail || "请查看时间线与日志。"}`;
      message.className = "job-message error";
    } else if (job.status === "COMPLETED") {
      message.textContent = "归纳完成，已建立活动索引并生成完整笔记。";
      message.className = "job-message success";
    } else {
      message.textContent = job.status === "WAITING_EXTERNAL" ? "正在等待 MinerU 外部解析结果…" : "后台 Worker 正在处理…";
      message.className = "job-message";
    }
    const terminal = terminalJobStatuses.has(job.status);
    clearTimeout(jobPollTimer);
    if (!terminal) {
      jobPollTimer = setTimeout(() => pollJob(jobId), 3000);
      return;
    }
    if (["COMPLETED", "COMPLETED_WITH_WARNINGS"].includes(job.status)) {
      const result = await request(`/jobs/${encodeURIComponent(jobId)}/result`);
      if (result.source_note_id && state.openedJobResultId !== jobId) {
        state.openedJobResultId = jobId;
        await loadNotes({ autoSelect: false });
        await loadSourceNote(result.source_note_id);
      }
    }
  } catch (error) {
    $("#job-monitor-status").textContent = "读取失败";
    $("#job-message").textContent = error.message;
  }
}

async function loadCurrentJob() {
  if (state.currentJobId) {
    await pollJob(state.currentJobId);
    return;
  }
  try {
    const jobs = await request("/jobs?limit=1");
    if (!jobs.length) {
      $("#job-monitor-status").textContent = "暂无任务";
      $("#job-timeline").replaceChildren(createText("p", "还没有 PDF 生成任务。", "empty-list"));
      return;
    }
    state.currentJobId = jobs[0].id;
    localStorage.setItem("noodle.currentJobId", state.currentJobId);
    await pollJob(state.currentJobId);
  } catch (error) {
    $("#job-monitor-status").textContent = "读取失败";
  }
}

$("#pdf-file").addEventListener("change", (event) => {
  $("#pdf-file-name").textContent = event.target.files?.[0]?.name || "尚未选择文件";
});

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("#pdf-file").files?.[0];
  if (!file) return;
  const button = $("#upload-button");
  const uploadStatus = $("#upload-status");
  button.disabled = true;
  uploadStatus.textContent = "正在上传并创建持久化任务…";
  try {
    const form = new FormData();
    form.append("file", file);
    const title = $("#pdf-title").value.trim();
    const query = title ? `?title=${encodeURIComponent(title)}` : "";
    const result = await request(`/sources/pdf${query}`, { method: "POST", body: form });
    state.currentJobId = result.job_id;
    state.openedJobResultId = null;
    localStorage.setItem("noodle.currentJobId", result.job_id);
    uploadStatus.textContent = result.deduplicated === "true"
      ? "检测到相同PDF，正在打开已有任务。"
      : "上传完成，后台Worker将自动处理。";
    $("#job-monitor").open = true;
    clearTimeout(jobPollTimer);
    await pollJob(result.job_id);
  } catch (error) {
    uploadStatus.textContent = `上传失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});

$("#resume-job").addEventListener("click", async () => {
  if (!state.currentJobId) return;
  const button = $("#resume-job");
  button.disabled = true;
  try {
    await request(`/jobs/${encodeURIComponent(state.currentJobId)}/resume`, { method: "POST" });
    state.openedJobResultId = null;
    await pollJob(state.currentJobId);
  } catch (error) {
    $("#job-message").textContent = `恢复失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});

$("#note-search").addEventListener("input", (event) => {
  const query = event.target.value.trim().toLocaleLowerCase("zh-CN");
  const entries = [
    ...state.sourceNotes.map((note) => ({ ...note, kind: "source" })),
    ...state.notes.map((note) => ({ ...note, kind: "note" })),
  ];
  state.filtered = entries.filter((note) => `${note.title} ${note.thesis || note.summary || ""}`.toLocaleLowerCase("zh-CN").includes(query));
  renderNotes();
});

function clearChatAttachment() {
  if (state.pendingAttachment?.previewUrl) URL.revokeObjectURL(state.pendingAttachment.previewUrl);
  state.pendingAttachment = null;
  $("#chat-attachment").value = "";
  $("#chat-attachment-preview").hidden = true;
  $("#chat-attachment-image").removeAttribute("src");
}

$("#chat-attachment").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  if (!file) {
    clearChatAttachment();
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    appendMessage("图片超过 15 MB，无法作为 Agent 附件上传。", "assistant");
    clearChatAttachment();
    return;
  }
  clearChatAttachment();
  const previewUrl = URL.createObjectURL(file);
  state.pendingAttachment = { file, previewUrl };
  $("#chat-attachment-image").src = previewUrl;
  $("#chat-attachment-name").textContent = file.name;
  $("#chat-attachment-status").textContent = "等待发送";
  $("#chat-attachment-preview").hidden = false;
});

$("#remove-chat-attachment").addEventListener("click", clearChatAttachment);

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#chat-input");
  const question = input.value.trim();
  if (!question) return;
  const pendingAttachment = state.pendingAttachment;
  appendMessage(pendingAttachment ? `${question}\n\n[附图：${pendingAttachment.file.name}]` : question, "user");
  input.value = "";
  $("#send-button").disabled = true;
  try {
    const attachmentIds = [];
    if (pendingAttachment) {
      $("#chat-attachment-status").textContent = "正在上传并识图…";
      const form = new FormData();
      form.append("file", pendingAttachment.file);
      const conversationQuery = state.conversationId
        ? `?conversation_id=${encodeURIComponent(state.conversationId)}`
        : "";
      const attachment = await request(`/agent/attachments${conversationQuery}`, {
        method: "POST",
        body: form,
      });
      attachmentIds.push(attachment.attachment_id);
      $("#chat-attachment-status").textContent = attachment.status === "READY_WITH_WARNING"
        ? "上传完成 · 识图降级"
        : "上传与识图完成";
    }
    const activeSourceNoteId = state.selectedId?.startsWith("source:")
      ? state.selectedId.slice("source:".length)
      : null;
    const result = await request("/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: question,
        conversation_id: state.conversationId,
        attachment_ids: attachmentIds,
        active_source_note_id: activeSourceNoteId,
      }),
    });
    state.conversationId = result.conversation_id;
    localStorage.setItem("noodle.conversationId", state.conversationId);
    appendMessage(
      result.answer,
      "assistant",
      result.citations || [],
      result.coverage_warning,
      result.tool_calls || [],
      result.skill,
      result.run_id,
    );
    const editCall = (result.tool_calls || []).find(
      (call) => call.tool_name === "note.replace_source_images" && call.status === "SUCCEEDED",
    );
    const editedSourceNoteId = editCall?.result?.data?.source_note_id;
    if (editedSourceNoteId) {
      await loadNotes({ autoSelect: false });
      await loadSourceNote(editedSourceNoteId);
    }
    clearChatAttachment();
  } catch (error) {
    appendMessage(`问答失败：${error.message}`, "assistant");
  } finally {
    $("#send-button").disabled = false;
    input.focus();
  }
});

$("#chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$("#clear-chat").addEventListener("click", () => {
  state.conversationId = null;
  localStorage.removeItem("noodle.conversationId");
  chatMessages.replaceChildren();
  appendMessage("对话已清空。你可以继续从当前知识库提问。", "assistant");
});

$("#toggle-library").addEventListener("click", () => toggleWorkspacePanel("library"));
$("#toggle-chat").addEventListener("click", () => toggleWorkspacePanel("chat"));

document.querySelectorAll(".mobile-tab").forEach((tab) => tab.addEventListener("click", () => showMobilePanel(tab.dataset.panel)));

async function initialize() {
  applyPanelVisibility();
  await loadNotes();
  await loadCurrentJob();
}

initialize();
