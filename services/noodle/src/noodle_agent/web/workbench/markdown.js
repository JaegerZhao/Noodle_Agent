// markdown workbench responsibilities. State is shared explicitly through imports.
import { codeTokenPattern, mathTokenPattern } from './state.js';

export function restoreTextTokens(target, prefix, values) {
  const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  const tokenPattern = new RegExp(`${prefix}(\\d+)END`, "g");
  for (const textNode of nodes) {
    if (!textNode.nodeValue?.includes(prefix)) continue;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of textNode.nodeValue.matchAll(tokenPattern)) {
      fragment.append(document.createTextNode(textNode.nodeValue.slice(cursor, match.index)));
      fragment.append(document.createTextNode(values[Number(match[1])] || match[0]));
      cursor = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(textNode.nodeValue.slice(cursor)));
    textNode.replaceWith(fragment);
  }
}

export function renderKatexNode(node, expression, displayMode) {
  if (!window.katex) {
    node.textContent = displayMode ? `$$${expression}$$` : `$${expression}$`;
    return;
  }
  try {
    window.katex.render(expression.trim(), node, {
      displayMode,
      throwOnError: true,
      strict: "ignore",
      trust: false,
      output: "htmlAndMathml",
    });
  } catch (error) {
    node.classList.add("math-render-warning");
    node.textContent = expression;
    node.title = `公式渲染失败：${error.message}`;
  }
}

export function stripMathDelimiters(value) {
  const source = String(value || "").trim();
  if (source.startsWith("$$") && source.endsWith("$$")) return source.slice(2, -2);
  if (/^\$[^$][\s\S]*\$$/.test(source)) return source.slice(1, -1);
  if ((source.startsWith("\\[") && source.endsWith("\\]")) || (source.startsWith("\\(") && source.endsWith("\\)"))) return source.slice(2, -2);
  return source;
}

export function enhanceScrollableMarkdown(target) {
  for (const table of target.querySelectorAll("table")) {
    if (table.parentElement?.classList.contains("markdown-table-region")) continue;
    const region = document.createElement("div");
    region.className = "markdown-scroll-region markdown-table-region";
    region.tabIndex = 0;
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", "可横向滚动的表格");
    table.before(region);
    region.append(table);
  }
  for (const node of target.querySelectorAll("pre, .noodle-math-display")) {
    node.classList.add("markdown-scroll-region");
    node.tabIndex = 0;
    node.setAttribute("role", "region");
    node.setAttribute("aria-label", node.matches("pre") ? "可横向滚动的代码" : "可横向滚动的公式");
  }
}

export function renderMarkdown(target, source, { inline = false } = {}) {
  const value = String(source || "");
  if (!window.marked || !window.DOMPurify) { target.textContent = value; return; }
  const codeSegments = [];
  const codeProtected = value.replace(codeTokenPattern, (segment) => {
    const token = `NOODLECODETOKEN${codeSegments.length}END`;
    const fenced = segment.match(/^(```|~~~)([^\n]*)\n([\s\S]*?)(?:\n)?\1$/);
    if (fenced) {
      codeSegments.push(fenced[3]);
      return `${fenced[1]}${fenced[2]}\n${token}\n${fenced[1]}`;
    }
    const ticks = segment.match(/^`+/)?.[0] || "`";
    codeSegments.push(segment.slice(ticks.length, -ticks.length));
    return `${ticks}${token}${ticks}`;
  });
  const expressions = [];
  const protectedValue = codeProtected.replace(mathTokenPattern, (whole, blockDollar, blockBracket, inlineParen, inlineDollar) => {
    const expression = blockDollar ?? blockBracket ?? inlineParen ?? inlineDollar ?? whole;
    const display = blockDollar !== undefined || blockBracket !== undefined;
    const index = expressions.push({ expression, display }) - 1;
    return display
      ? `\n\n<div class="noodle-math-display" data-math-id="${index}"></div>\n\n`
      : `<span class="noodle-math-inline" data-math-id="${index}"></span>`;
  });
  const html = inline ? window.marked.parseInline(protectedValue, { gfm: true, breaks: true }) : window.marked.parse(protectedValue, { gfm: true, breaks: true });
  target.innerHTML = window.DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ["data-math-id"],
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed"],
  });
  restoreTextTokens(target, "NOODLECODETOKEN", codeSegments);
  for (const node of target.querySelectorAll("[data-math-id]")) {
    const item = expressions[Number(node.dataset.mathId)];
    if (item) renderKatexNode(node, item.expression, item.display);
    node.removeAttribute("data-math-id");
  }
  enhanceScrollableMarkdown(target);
}
