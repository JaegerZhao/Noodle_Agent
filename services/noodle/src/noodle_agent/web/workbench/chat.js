// chat workbench responsibilities. State is shared explicitly through imports.
import { $, AGENT_IMAGE_LIMIT, AGENT_IMAGE_MAX_BYTES, AGENT_IMAGE_TYPES, API, TOOL_PRESENTATION, WRITE_TOOL_NAMES, state } from './state.js';
import { create, escapeHtml, formatBytes, request, toast } from './common.js';
import { applyPanelState } from './layout.js';
import { selectedFolderTitle } from './library.js';
import { renderMarkdown } from './markdown.js';

export function agentPrompt(note = null) {
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

export function attachmentName(file, index = 0) {
  const original = String(file?.name || "").trim();
  if (original && !/^image\.(png|jpe?g|webp)$/i.test(original)) return original;
  const extension = file?.type === "image/jpeg" ? "jpg" : file?.type === "image/webp" ? "webp" : "png";
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `clipboard-${stamp}-${index + 1}.${extension}`;
}

export function renderAgentAttachments() {
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

export function addAgentImageFiles(files) {
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

export function removeAgentAttachment(localId) {
  const index = state.agentAttachments.findIndex((item) => item.localId === localId);
  if (index < 0 || state.agentSending) return;
  const [removed] = state.agentAttachments.splice(index, 1);
  renderAgentAttachments();
  window.setTimeout(() => URL.revokeObjectURL(removed.previewUrl), 500);
}

export function clearAgentAttachments() {
  const previewUrls = state.agentAttachments.map((attachment) => attachment.previewUrl);
  state.agentAttachments = [];
  renderAgentAttachments();
  window.setTimeout(() => previewUrls.forEach((url) => URL.revokeObjectURL(url)), 500);
}

export function handleAgentPaste(event) {
  const items = [...(event.clipboardData?.items || [])];
  const images = items
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter(Boolean);
  if (!images.length) return;
  event.preventDefault();
  addAgentImageFiles(images);
}

export async function uploadAgentAttachments() {
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

export function escapedMarkdownLabel(value) {
  return String(value || "图片").replace(/[\\[\]()`*_]/g, "\\$&");
}

export function attachmentMessageMarkdown(attachments) {
  if (!attachments.length) return "";
  return attachments.map((item) => {
    const name = escapedMarkdownLabel(item.remote.original_name || item.name);
    return `![已粘贴图片：${name}](${item.remote.blob_url})`;
  }).join("\n\n");
}

export function attachmentAgentContext(attachments) {
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

export function focusAgent(note = null) {
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

export function updateAgentContext() {
  const value = state.agentTargetNote
    ? `${state.agentTargetNote.title} · AtomicNote`
    : state.sourceNote ? `${state.sourceNote.title} · v${state.sourceNote.version_no}` : selectedFolderTitle();
  $("#agent-context b").textContent = value;
}

export function sessionTitle(session) {
  const value = session.projections?.values?.title;
  if (typeof value === "string" && value.trim()) return value.trim();
  if (value && typeof value.title === "string" && value.title.trim()) return value.title.trim();
  return session.blank ? "新对话" : `对话 ${session.sessionId.slice(-6)}`;
}

export function setAgentStatus(label, kind = "idle") {
  const status = $("#agent-status");
  status.className = kind;
  status.innerHTML = `<i></i>${escapeHtml(label)}`;
}

export function visibleUserText(text) {
  const marker = "用户请求：\n";
  const index = String(text || "").lastIndexOf(marker);
  return index >= 0 ? String(text).slice(index + marker.length).trim() : String(text || "");
}

export function messageParts(message) {
  const content = Array.isArray(message?.content) ? message.content : message?.content ? [message.content] : [];
  return {
    text: content.filter((part) => part?.type === "text").map((part) => part.text || "").join("\n\n").trim(),
    reasoning: content.filter((part) => part?.type === "reasoning").map((part) => part.text || "").join("\n\n").trim(),
  };
}

export function parseJson(value, fallback = {}) {
  if (value && typeof value === "object") return value;
  try { return JSON.parse(String(value || "")); }
  catch (_) { return fallback; }
}

export function clipText(value, limit = 96) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

export function toolResultData(event) {
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

export function toolResultSummary(tool) {
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

export function toolStatus(tool) {
  if (tool.approval && !tool.approval.outcome) return "approval";
  if (["rejected", "cancelled", "unavailable"].includes(tool.approval?.outcome)) return "rejected";
  if (!tool.result) return "running";
  return tool.result.isError ? "failed" : "completed";
}

export function durationLabel(start, end) {
  if (!start || !end || end < start) return "";
  const ms = end - start;
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

export function turnIntent(turn) {
  const names = new Set(turn.tools.map((tool) => tool.name));
  if ([...names].some((name) => WRITE_TOOL_NAMES.has(name))) return "执行受控知识库修改";
  if (names.has("noodle_search_knowledge") || names.has("noodle_answer_knowledge")) return "检索并核对知识证据";
  if (names.has("noodle_get_source_note") || names.has("noodle_get_atomic_note")) return "读取活动笔记版本";
  if (names.has("noodle_get_job_status")) return "检查后台处理进度";
  return "调用 Noodle 知识能力";
}

export function foldAgentHistory(entries) {
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

export function renderAgentMessage(message) {
  const article = create("article", `agent-message ${message.role}`);
  const label = create("span", "agent-message-role", message.role === "user" ? "YOU" : "NOODLE");
  const body = create("div", "agent-message-body markdown-body");
  renderMarkdown(body, message.text || "");
  article.append(label, body);
  return article;
}

export function renderAgentExecution(turn) {
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

export async function decideAgentApproval(approval, outcome) {
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

export function renderAgentHistory(history) {
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

export function renderAgentSessions() {
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

export async function createAgentSession() {
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

export async function renameAgentSession() {
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

export async function loadAgentSessions({ quiet = false } = {}) {
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

export async function sendAgentMessage(event) {
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

export async function cancelAgentRun() {
  if (!state.agentSessionId) return;
  try {
    await request(`${state.config.harness_chat_api}/sessions/${encodeURIComponent(state.agentSessionId)}/cancel`, { method: "POST" });
    await loadAgentSessions();
  } catch (error) { toast(error.message, "error"); }
}
