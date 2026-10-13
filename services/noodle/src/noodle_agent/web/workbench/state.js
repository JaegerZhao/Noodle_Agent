// state workbench responsibilities. State is shared explicitly through imports.

export const API = "/internal/v1";

export const state = {
  config: { harness_web_url: "http://127.0.0.1:3080", harness_chat_api: "/internal/v1/harness", poll_interval_ms: 2500 },
  folders: [],
  sources: [],
  jobs: [],
  syntheses: [],
  synthesis: null,
  concept: null,
  synthesisEditor: null,
  conceptRegistry: [],
  conceptRegistryTimer: null,
  selectedFolder: localStorage.getItem("noodle.ui.folder") || "all",
  selectedSource: null,
  sourceNote: null,
  sourceNoteRefreshing: false,
  contentMode: "note",
  pdfPage: 1,
  pdfBlobUrl: null,
  pdfBlobSourceId: null,
  pdfBlobSize: 0,
  pdfData: null,
  pdfLoadPromise: null,
  pdfAbortController: null,
  pdfJsPromise: null,
  pdfDocument: null,
  pdfDocumentSourceId: null,
  pdfLoadingTask: null,
  pdfPagesPromise: null,
  pdfRenderObserver: null,
  pdfRenderTasks: new Map(),
  pdfPageElements: new Map(),
  pdfScrollFrame: 0,
  navigationIndex: null,
  readingAnchor: null,
  noteScrollFrame: 0,
  navigationGuard: null,
  navigationSequence: 0,
  selectedJobId: localStorage.getItem("noodle.ui.job") || null,
  search: "",
  sort: localStorage.getItem("noodle.ui.sort") || "updated",
  readerMode: "outline",
  drawerOpen: localStorage.getItem("noodle.ui.drawer") === "true",
  polling: false,
  timelinePolling: false,
  agentSessions: [],
  agentSessionId: localStorage.getItem("noodle.agent.session") || null,
  agentHistorySignature: "",
  agentRunning: false,
  agentBusy: false,
  agentSending: false,
  agentTargetNote: null,
  agentAttachments: [],
  agentApprovalBusy: new Set(),
};

export const $ = (selector) => document.querySelector(selector);

export const $$ = (selector) => [...document.querySelectorAll(selector)];

export const terminalStatuses = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"]);

export const runningStatuses = new Set(["CREATED", "RUNNING", "WAITING_EXTERNAL"]);

export const stageLabels = {
  CREATED: "等待处理",
  SUBMITTING_PARSE: "提交 MinerU",
  WAITING_PARSE: "等待 MinerU",
  NORMALIZING: "标准化",
  PROFILING_DOCUMENT: "文档画像",
  BUILDING_KNOWLEDGE_MAP: "知识映射",
  PLANNING_SOURCE_NOTE_OUTLINE: "大纲规划",
  PLANNING_NOTE_SET: "笔记规划",
  REVIEWING_NOTE_SET: "规划检查",
  REPAIRING_NOTE_SET: "规划修复",
  ANALYZING_VISUALS: "视觉理解",
  DRAFTING_NOTES: "笔记生成",
  EVALUATING_NOTES: "质量审校",
  VALIDATING_NOTES: "确定性校验",
  INDEXING: "建立索引",
  COMPLETED: "已完成",
};

export const codeTokenPattern = /```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^\n]*?`+/g;

export const mathTokenPattern = /(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$|\\\[([\s\S]*?)\\\]|\\\(([\s\S]*?)\\\)|(?<![\\$])\$(?![\s$])((?:\\.|[^$\n])+?)(?<!\s)\$/g;

export const AGENT_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

export const AGENT_IMAGE_LIMIT = 4;

export const AGENT_IMAGE_MAX_BYTES = 15 * 1024 * 1024;

export const TOOL_PRESENTATION = {
  noodle_search_knowledge: ["检索知识库", "查找与问题相关的原子笔记和页码证据"],
  noodle_answer_knowledge: ["生成引用问答", "使用 Noodle 检索链生成带来源回答"],
  noodle_list_source_notes: ["列出完整笔记", "读取当前知识库中的 SourceNote"],
  noodle_get_source_note: ["读取完整笔记", "核对 SourceNote 的活动版本与大纲"],
  noodle_get_atomic_note: ["读取原子笔记", "读取 AtomicNote 正文和稳定 Block"],
  noodle_get_job_status: ["检查处理任务", "读取 PDF 后台任务的权威状态"],
  noodle_ingest_pdf: ["导入 PDF", "创建可恢复的后台处理任务"],
  noodle_upload_image_attachment: ["上传图片附件", "保存待用于笔记修改的图片"],
  noodle_restart_generation: ["局部重新生成", "复用解析结果并从指定阶段恢复"],
  noodle_edit_note_block: ["修改笔记 Block", "创建新的 AtomicNote 与 SourceNote 版本"],
  noodle_replace_source_images: ["替换来源图片", "按页码范围创建新的图片与笔记版本"],
  noodle_export_markdown: ["导出 Markdown", "从数据库活动版本生成本地投影"],
};

export const WRITE_TOOL_NAMES = new Set([
  "noodle_ingest_pdf", "noodle_upload_image_attachment", "noodle_restart_generation",
  "noodle_edit_note_block", "noodle_replace_source_images", "noodle_export_markdown",
]);
