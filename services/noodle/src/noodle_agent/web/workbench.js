// main workbench responsibilities. State is shared explicitly through imports.
import { loadLibrary, refreshKnowledgeSyncStatus, selectFolder } from './workbench/library.js';
import { applyPanelState, applyReaderMode, bindEvents } from './workbench/layout.js';
import { applyDrawerState, selectJob } from './workbench/jobs.js';
import { $, API, state } from './workbench/state.js';
import { refreshElapsedClocks, request } from './workbench/common.js';
import { loadSyntheses, openSynthesis, showSynthesisLibrary } from './workbench/knowledge.js';
import { loadAgentSessions } from './workbench/chat.js';
import { openSource } from './workbench/reader.js';
import * as stateModule from './workbench/state.js';
import * as commonModule from './workbench/common.js';
import * as libraryModule from './workbench/library.js';
import * as knowledgeModule from './workbench/knowledge.js';
import * as jobsModule from './workbench/jobs.js';
import * as readerModule from './workbench/reader.js';
import * as markdownModule from './workbench/markdown.js';
import * as navigationModule from './workbench/navigation.js';
import * as pdfModule from './workbench/pdf.js';
import * as chatModule from './workbench/chat.js';
import * as layoutModule from './workbench/layout.js';

globalThis.NoodleWorkbench = Object.freeze({ ...stateModule, ...commonModule, ...libraryModule, ...knowledgeModule, ...jobsModule, ...readerModule, ...markdownModule, ...navigationModule, ...pdfModule, ...chatModule, ...layoutModule });

setInterval(refreshKnowledgeSyncStatus, 5000);

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
