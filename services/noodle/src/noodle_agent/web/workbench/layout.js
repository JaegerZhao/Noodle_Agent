// layout workbench responsibilities. State is shared explicitly through imports.
import { $, $$, state } from './state.js';
import { applyDrawerState, closeUpload, openUpload, selectJob, uploadPdf } from './jobs.js';
import { toast } from './common.js';
import { createFolder, loadLibrary, renderSourceTable, selectFolder } from './library.js';
import { closeSynthesisEditor, editorNode, loadConceptRegistry, mergeConceptsFromEditor, openSynthesisEditor, publishSynthesisEditor, renderSynthesisDraft, showSynthesisLibrary, synthesisSlug, uniqueEditorKey } from './knowledge.js';
import { rerenderVisiblePdfPages, switchContentMode, syncPdfPageFromScroll } from './pdf.js';
import { scheduleNoteAnchorSync } from './navigation.js';
import { addAgentImageFiles, cancelAgentRun, createAgentSession, handleAgentPaste, loadAgentSessions, renameAgentSession, sendAgentMessage } from './chat.js';

export function applyReaderMode(mode) {
  state.readerMode = mode;
  $$('[data-reader-mode]').forEach((button) => button.classList.toggle("active", button.dataset.readerMode === mode));
  $("#outline-tree").hidden = mode !== "outline";
  $("#reader-file-list").hidden = mode !== "files";
}

export function applyPanelState() {
  const app = $("#app");
  app.classList.toggle("library-collapsed", localStorage.getItem("noodle.ui.libraryCollapsed") === "true");
  app.classList.toggle("agent-collapsed", localStorage.getItem("noodle.ui.agentCollapsed") === "true");
}

export function applyMobileView(view) {
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

export function bindEvents() {
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
