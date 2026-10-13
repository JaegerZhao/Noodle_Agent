// knowledge workbench responsibilities. State is shared explicitly through imports.
import { $, $$, API, state } from './state.js';
import { create, formatDate, request, toast } from './common.js';
import { releaseSourcePdfBlob } from './pdf.js';
import { updateAgentContext } from './chat.js';
import { enhanceScrollableMarkdown, renderKatexNode, renderMarkdown, stripMathDelimiters } from './markdown.js';

export async function loadSyntheses({ quiet = false } = {}) {
  try {
    state.syntheses = await request(`${API}/syntheses`);
    $("#synthesis-count").textContent = String(state.syntheses.length);
    renderSynthesisCards();
  } catch (error) {
    if (!quiet) toast(error.message, "error");
  }
}

export function synthesisSlug(value) {
  return String(value || "").normalize("NFKD").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 150);
}

export function editorNode(raw) {
  return {
    _uid: crypto.randomUUID(),
    stable_key: raw.stable_key || synthesisSlug(raw.title) || `node-${crypto.randomUUID().slice(0, 8)}`,
    title: raw.title || "未命名",
    node_kind: raw.node_kind === "GROUP" ? "GROUP" : "CONCEPT",
    concept_id: raw.concept_id || "",
    children: (raw.children || []).map(editorNode),
  };
}

export function editorPayload(nodes) {
  return nodes.map((node) => ({
    stable_key: node.stable_key,
    title: node.title,
    node_kind: node.node_kind,
    ...(node.concept_id ? { concept_id: node.concept_id } : {}),
    children: editorPayload(node.children || []),
  }));
}

export function walkEditorNodes(nodes, callback, parent = null) {
  for (const node of nodes) {
    if (callback(node, nodes, parent)) return true;
    if (walkEditorNodes(node.children || [], callback, node)) return true;
  }
  return false;
}

export function selectedEditorGroup() {
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

export function uniqueEditorKey(base) {
  const used = new Set();
  walkEditorNodes(state.synthesisEditor?.outline || [], (node) => { used.add(node.stable_key); return false; });
  let value = synthesisSlug(base) || "concept";
  let suffix = 2;
  while (used.has(value)) value = `${synthesisSlug(base) || "concept"}-${suffix++}`;
  return value;
}

export function moveEditorNode(uid, offset) {
  walkEditorNodes(state.synthesisEditor.outline, (node, siblings) => {
    if (node._uid !== uid) return false;
    const index = siblings.indexOf(node);
    const target = index + offset;
    if (target >= 0 && target < siblings.length) siblings.splice(target, 0, siblings.splice(index, 1)[0]);
    return true;
  });
  renderSynthesisDraft();
}

export function removeEditorNode(uid) {
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

export function renderSynthesisDraft() {
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

export function renderConceptRegistry() {
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

export function setMergeConcept(role, concept) {
  const input = $(`#merge-${role}-concept`);
  input.value = `${concept.title} · v${concept.version_no}`;
  input.dataset.conceptId = concept.concept_id;
  input.dataset.versionId = concept.version_id;
  $(".concept-merge-panel").open = true;
  $("#synthesis-editor-status").textContent = role === "source" ? "已选择待合并源" : "已选择保留目标";
}

export async function loadConceptRegistry(query = "") {
  const host = $("#concept-registry-results");
  host.replaceChildren(create("div", "synthesis-draft-empty", "正在查询 Concept 注册表…"));
  try {
    state.conceptRegistry = await request(`${API}/concepts?q=${encodeURIComponent(query)}&limit=80`);
    renderConceptRegistry();
  } catch (error) {
    host.replaceChildren(create("div", "synthesis-draft-empty", error.message));
  }
}

export async function openSynthesisEditor(synthesisId = null) {
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

export function closeSynthesisEditor() {
  $("#synthesis-editor").close();
  state.synthesisEditor = null;
}

export async function publishSynthesisEditor(event) {
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

export async function mergeConceptsFromEditor() {
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

export function renderSynthesisCards() {
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

export function setPrimaryView(view) {
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

export function showSynthesisLibrary() {
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

export function flattenSynthesisNodes(nodes) {
  return nodes.flatMap((node) => [node, ...flattenSynthesisNodes(node.children || [])]);
}

export function renderSynthesisOutline(nodes, host) {
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

export async function openSynthesis(synthesisId, preferredConceptId = null) {
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

export function conceptBlockLabel(block) {
  const labels = {
    FORMULA: "公式", ALGORITHM: "算法", EXAMPLE: "例子", TABLE: "对照",
    FIGURE: "图示", LIMITATION: "边界与限制", PROSE: "", LIST: "要点",
    PROCEDURE: "步骤", CODE: "代码", CALLOUT: "补充", ASSET: "图示",
    CONCEPT_REF: "相关概念",
  };
  return block.title || labels[block.block_type] || "";
}

export function appendConceptAsset(parent, asset, blockTitle, conceptTitle) {
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

export function renderConceptContentBlock(item, assetsById, renderedAssets, context) {
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

export function renderConceptDiff(diff, target) {
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

export async function appendConceptOperations(body) {
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

export async function openConcept(conceptId, { updateHash = true, versionId = null } = {}) {
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
