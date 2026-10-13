// navigation workbench responsibilities. State is shared explicitly through imports.
import { $, $$, API, state } from './state.js';
import { toast } from './common.js';
import { jumpToPdfPage } from './pdf.js';

export function assetPages(asset) {
  const values = (asset.source_pages || [asset.page_label]).filter(Boolean).map((item) => String(item).replace(/^p\.?/i, ""));
  return [...new Set(values)].join("、") || "?";
}

export function parsePdfPage(value) {
  const match = String(value ?? "").match(/\d+/);
  return match ? Math.max(1, Number(match[0])) : 1;
}

export function physicalPage(anchor) {
  return Number.isFinite(Number(anchor?.page_index)) ? Number(anchor.page_index) + 1 : parsePdfPage(anchor?.page_label);
}

export function navigationAnchorPriority(anchor) {
  return [
    anchor?.evidence_role === "PRIMARY" ? 0 : 1,
    anchor?.anchor_kind === "EVIDENCE" ? 0 : 1,
    anchor?.block_key ? 0 : 1,
    physicalPage(anchor),
  ];
}

export function compareNavigationAnchors(left, right) {
  const a = navigationAnchorPriority(left);
  const b = navigationAnchorPriority(right);
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return 0;
}

export function addNavigationIndexEntry(map, key, anchor) {
  if (!key) return;
  if (!map.has(key)) map.set(key, []);
  const values = map.get(key);
  if (!values.some((item) => item.outline_node_id === anchor.outline_node_id
    && item.note_id === anchor.note_id
    && item.block_key === anchor.block_key
    && physicalPage(item) === physicalPage(anchor)
    && item.anchor_kind === anchor.anchor_kind)) values.push(anchor);
}

export function buildNavigationIndex(sourceNote) {
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

export function chooseNavigationAnchor(candidates, context = state.readingAnchor) {
  if (!candidates?.length) return null;
  return [...candidates].sort((left, right) => {
    const contextScore = (anchor) => (anchor.note_id === context?.note_id ? -4 : 0)
      + (anchor.outline_node_id === context?.outline_node_id ? -2 : 0)
      + (anchor.block_key && anchor.block_key === context?.block_key ? -8 : 0);
    return contextScore(left) - contextScore(right) || compareNavigationAnchors(left, right);
  })[0];
}

export function anchorForPdfPage(pageNumber) {
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

export function highlightNavigation(anchor) {
  $$(".outline-link").forEach((button) => {
    const exactNote = anchor?.note_id && button.dataset.noteId === anchor.note_id;
    const outline = anchor?.outline_node_id && button.dataset.outlineNodeId === anchor.outline_node_id;
    button.classList.toggle("navigation-active", Boolean(exactNote));
    button.classList.toggle("navigation-parent", Boolean(outline && !button.dataset.noteId));
  });
}

export function setReadingAnchor(anchor, origin = "unknown") {
  state.readingAnchor = anchor ? { ...anchor, origin } : null;
  highlightNavigation(state.readingAnchor);
  return state.readingAnchor;
}

export function beginNavigationGuard(surface, duration = 700) {
  state.navigationGuard = { surface, until: performance.now() + duration };
}

export function navigationGuarded(surface) {
  if (!state.navigationGuard || performance.now() >= state.navigationGuard.until) {
    state.navigationGuard = null;
    return false;
  }
  return state.navigationGuard.surface === surface;
}

export function findNoteAnchorAtReadingLine() {
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

export function syncNoteAnchorFromScroll({ force = false } = {}) {
  if (!force && navigationGuarded("note")) return state.readingAnchor;
  const anchor = findNoteAnchorAtReadingLine();
  if (anchor) setReadingAnchor(anchor, "note-scroll");
  return anchor;
}

export function scheduleNoteAnchorSync() {
  if (state.noteScrollFrame || state.contentMode !== "note") return;
  state.noteScrollFrame = requestAnimationFrame(() => {
    state.noteScrollFrame = 0;
    syncNoteAnchorFromScroll();
  });
}

export function noteElementForAnchor(anchor) {
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

export function scrollNoteToAnchor(anchor, { smooth = true, targetElement = null } = {}) {
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

export function navigateFromDirectory(kind, id) {
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

export function sourcePdfEndpoint(sourceId) {
  return `${API}/sources/${encodeURIComponent(sourceId)}/original-content`;
}

export function setPdfLoadStatus(label, kind = "idle") {
  const status = $("#pdf-load-status");
  if (!status) return;
  status.textContent = label;
  status.className = `pdf-load-status ${kind}`.trim();
}
