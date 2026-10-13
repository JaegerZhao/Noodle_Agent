// pdf workbench responsibilities. State is shared explicitly through imports.
import { $, $$, state } from './state.js';
import { anchorForPdfPage, beginNavigationGuard, navigationGuarded, parsePdfPage, physicalPage, scrollNoteToAnchor, setPdfLoadStatus, setReadingAnchor, sourcePdfEndpoint, syncNoteAnchorFromScroll } from './navigation.js';
import { create, formatBytes, toast } from './common.js';
import { sourceSupportsPdf } from './reader.js';

export function clearPdfRendering({ preservePagesPromise = false } = {}) {
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

export function releaseSourcePdfBlob() {
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

export async function ensureSourcePdfBlob() {
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

export async function loadPdfJs() {
  if (!state.pdfJsPromise) {
    state.pdfJsPromise = import("/app/vendor/pdfjs/pdf.min.mjs?v=4.10.38").then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = "/app/vendor/pdfjs/pdf.worker.min.mjs?v=4.10.38";
      return pdfjs;
    });
  }
  return state.pdfJsPromise;
}

export async function ensurePdfDocument() {
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

export async function renderPdfPage(pageNumber) {
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

export async function ensurePdfPages() {
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

export function syncPdfPageFromScroll() {
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

export function rerenderVisiblePdfPages() {
  if (state.contentMode !== "pdf" || !state.pdfDocument) return;
  const viewer = $("#source-pdf-viewer");
  if (!viewer) return;
  const viewport = viewer.getBoundingClientRect();
  for (const [pageNumber, shell] of state.pdfPageElements) {
    const bounds = shell.getBoundingClientRect();
    if (bounds.bottom >= viewport.top - 800 && bounds.top <= viewport.bottom + 800) void renderPdfPage(pageNumber);
  }
}

export async function displayPdfPage() {
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

export function applyContentMode(mode) {
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

export function switchContentMode(mode) {
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

export function jumpToPdfPage(value, anchor = null) {
  state.pdfPage = parsePdfPage(value);
  $("#pdf-page-label").textContent = `P.${state.pdfPage}`;
  if (anchor) setReadingAnchor(anchor, "evidence-click");
  else setReadingAnchor(anchorForPdfPage(state.pdfPage), "evidence-click");
  beginNavigationGuard("pdf");
  applyContentMode("pdf");
  $("#pdf-content-pane").scrollIntoView({ block: "start" });
}
