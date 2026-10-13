// reader workbench responsibilities. State is shared explicitly through imports.
import { applyContentMode, jumpToPdfPage, releaseSourcePdfBlob } from './pdf.js';
import { $, API, mathTokenPattern, state, terminalStatuses } from './state.js';
import { assetPages, buildNavigationIndex, chooseNavigationAnchor, navigateFromDirectory, parsePdfPage, physicalPage, setReadingAnchor, syncNoteAnchorFromScroll } from './navigation.js';
import { setPrimaryView } from './knowledge.js';
import { updateAgentContext } from './chat.js';
import { create, request, toast } from './common.js';
import { selectJob } from './jobs.js';
import { enhanceScrollableMarkdown, renderKatexNode, renderMarkdown, stripMathDelimiters } from './markdown.js';

export function showWorkbench() {
  releaseSourcePdfBlob();
  state.sourceNote = null;
  state.navigationIndex = null;
  setReadingAnchor(null);
  setPrimaryView("workbench");
  applyContentMode("note");
  updateAgentContext();
}

export async function openSource(source) {
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

export async function reloadOpenSource(source, { announce = false, resetScroll = false } = {}) {
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

export function sourceSupportsPdf() {
  const source = state.sourceNote || state.selectedSource || {};
  if (typeof source.original_view_available === "boolean") return source.original_view_available;
  return /\.pdf$/i.test(source.source_file || state.selectedSource?.source_file || "");
}

export function createPageButton(label, page, className, anchor = null) {
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

export function assetUrl(asset) {
  if (asset.blob_url) return asset.blob_url;
  return `${API}/blobs/${String(asset.storage_key || "").split("/").map(encodeURIComponent).join("/")}`;
}

export function renderAsset(asset, context = {}) {
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

export function renderBlock(block, renderedAssets, context) {
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

export function renderAtomicNote(note, outlineNodeId, { hideTitle = false, titleLevel = 3 } = {}) {
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

export function renderOutlineNode(node, body, nav, parentElided = false) {
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

export function renderReaderFiles() {
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

export function renderSourceNote() {
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
