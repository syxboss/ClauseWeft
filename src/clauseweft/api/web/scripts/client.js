
"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const pretty = (value) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);
const paths = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  projects: "M3 7h7l2-3h9v16H3z",
  requirements: "M7 3h10v18H7z M10 8h4 M10 12h4 M10 16h4",
  runs: "M5 4h14v16H5z M9 8l6 4-6 4z",
  templates: "M4 3h6v6H4z M14 15h6v6h-6z M17 15V6h-7 M7 9v9h7",
  roles:
    "M8 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M2 21v-3a6 6 0 0 1 12 0v3 M16 4a4 4 0 0 1 0 7 M17 15a5 5 0 0 1 5 5",
  skills: "M4 3h7v18H4z M14 3h6v18h-6z M7 7h1 M7 11h1 M17 7h1 M17 11h1",
  endpoints: "M8 4v6 M16 4v6 M5 10h14v3a7 7 0 0 1-14 0z M12 20v3",
  approvals: "M9 3h6v4H9z M9 5H5v16h14V5h-4 M8 13l3 3 5-6",
  usage: "M4 20V10 M10 20V4 M16 20v-7 M22 20H2",
  audit: "M4 3h16v18H4z M8 7h8 M8 12h8 M8 17h5",
  settings: "M4 5h16 M4 12h16 M4 19h16 M8 2v6 M16 9v6 M10 16v6",
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.runs}" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const labels = {
  overview: "工作台",
  projects: "项目",
  requirements: "需求看板",
  runs: "任务",
  templates: "SOP 模板",
  roles: "Agent 角色",
  skills: "技能库",
  endpoints: "模型配置",
  approvals: "审批中心",
  usage: "模型用量",
  audit: "审计日志",
  agents: "外部 Agent",
  settings: "团队与设置",
};
const defaultTaskOutputTokens = 1024;
const incompleteOutput = {
  output_length_limit: "模型达到输出上限，答复已截断。可在调用回执中查看已返回的文本。",
  output_content_filtered: "模型服务因内容过滤停止生成。请检查任务输入后再提交。",
  empty_model_output: "模型返回了空答复。请检查模型配置与调用回执。",
};
const states = {
  queued: "排队中",
  ready: "就绪",
  running: "运行中",
  leased: "准备派发",
  dispatched: "执行中",
  needs_input: "待处理",
  not_configured: "待配置模型",
  uncertain: "待核对",
  completed: "已完成",
  succeeded: "已完成",
  waiting: "等待依赖",
  cancelled: "已取消",
  failed: "失败",
  blocked: "已阻塞",
  active: "已启用",
  paused: "已暂停",
  disabled: "已停用",
  backlog: "待规划",
  in_progress: "进行中",
  review: "待验收",
  done: "已交付",
  passed: "通过",
  model: "Agent",
  gate: "人工审批",
};
const roleLabels = { admin: "管理员", operator: "执行者", viewer: "只读成员" };
const stageLabels = {
  auto: "自动识别",
  general: "通用任务",
  requirements: "需求分析",
  review: "独立评审",
  code: "代码实现",
  test: "测试设计",
};
const priorityLabels = { low: "低", medium: "中", high: "高" };
const badge = (state) =>
  `<span class="badge ${esc(state)}">${esc(states[state] || state || "未开始")}</span>`;
const personalSessionKey = "clauseweft-personal-session";
function savedPersonalSession() {
  const value = window.localStorage?.getItem(personalSessionKey);
  if (!value) return null;
  try {
    const session = JSON.parse(value);
    if (typeof session.token === "string" && Number.isFinite(session.expires_at)
        && session.expires_at * 1000 > Date.now()) return session;
  } catch { /* Discard an unreadable record. */ }
  window.localStorage.removeItem(personalSessionKey);
  return null;
}
const state = {
  token: sessionStorage.getItem("clauseweft-token") || savedPersonalSession()?.token || "",
  user: null,
  workspaceId: sessionStorage.getItem("clauseweft-workspace") || "default",
  workspaces: [],
  project: "",
  projects: [],
  endpoints: [],
  modelOptions: [],
  templates: [],
  roles: [],
  skills: [],
  auditFilters: {},
  auditCursor: 0,
  runsCursor: 0,
  auditQueryKey: "",
  workspace: null,
  runTab: "result",
  query: "",
  status: "",
  loginMode: "pair",
  generation: 0,
  accessEpoch: 0,
  designer: null,
  node: null,
  formHandler: null,
};
function can(capability) {
  if (["write", "approve"].includes(capability) && state.workspace?.read_only)
    return false;
  return (state.user?.capabilities || []).includes(capability);
}
const pageCapabilities = {
  overview: "read", projects: "read", requirements: "read", runs: "read",
  approvals: "approve", templates: "write", roles: "write", skills: "write",
  agents: "admin", endpoints: "admin", settings: "admin", audit: "admin", usage: "usage",
};
const actionCapabilities = {
  "agent-create": "admin", "agent-review": "admin", "agent-cancel": "admin", "agent-repositories": "admin",
  "new-project": "write", "edit-project": "write", "new-requirement": "write",
  "edit-requirement": "write", "requirement-run": "write", "new-run": "write",
  "new-role": "write", "edit-role": "write", "new-skill": "write", "edit-skill": "write",
  "new-template": "write", "save-template": "write", "publish-template": "write",
  "edit-template-meta": "write", "run-template": "write", "add-node": "write", "remove-node": "write",
  "pause-run": "write", "resume-run": "write", "cancel-run": "write", "attempt-detail": "usage",
  "run-action": "approve", "approve-baseline": "approve", "approve-delivery": "approve",
  "revise-spec": "write", "add-clause": "write", "resolve-finding": "approve",
  integrate: "write", rework: "write", "execute-tests": "write", reconcile: "write",
  "export-run": "write", "create-checkpoint": "write", "fork-checkpoint": "write", "preview-plan": "write",
  "continue-answer": "write", "new-tool-run": "write",
  "configure-models": "admin",
  "new-member": "admin", "edit-member": "admin", "new-endpoint": "admin", "edit-endpoint": "admin",
  "new-workspace": "admin", "edit-workspace": "admin",
  "toggle-endpoint": "admin", "probe-endpoint": "admin", "capability-probe": "admin", "endpoint-history": "admin",
  "run-audit": "admin", "audit-detail": "admin", "verify-audit": "admin", "export-audit": "admin",
  "audit-verification-detail": "admin", "clear-audit-filters": "admin", "audit-previous": "admin", "audit-next": "admin",
};
function actionAllowed(action) {
  return !actionCapabilities[action] || can(actionCapabilities[action]);
}
function clearContext() {
  state.accessEpoch++;
  state.generation++;
  state.projects = [];
  state.workspaces = [];
  state.endpoints = [];
  state.modelOptions = [];
  state.roles = [];
  state.skills = [];
  state.templates = [];
  state.workspace = null;
  state.currentRun = null;
  state.designer = null;
  state.checkpoints = [];
  state.findings = [];
  state.spec = null;
  state.editClauses = [];
  state.templateVersions = [];
  state.auditItems = [];
  state.auditVerification = null;
  state.auditFilters = {};
  state.auditCursor = 0;
  state.runsCursor = 0;
  state.auditNextCursor = null;
  state.auditQueryKey = "";
  state.members = [];
  state.adminConfig = null;
  state.grantProjects = [];
  state.node = null;
  state.traceRunId = null;
  state.project = "";
  state.query = "";
  state.status = "";
  state.runTab = "pipeline";
  keys.clear();
  if ($("#sheet").open) closeSheet();
  $("#sheet-body").replaceChildren();
  $("#content").innerHTML = "";
  $("#search").value = "";
  $("#project").innerHTML = options([["", "正在加载项目范围"]], "");
}
function disconnect() {
  if (savedPersonalSession()?.token === state.token)
    window.localStorage.removeItem(personalSessionKey);
  clearContext();
  state.token = "";
  state.user = null;
  state.workspaces = [];
  sessionStorage.removeItem("clauseweft-token");
  sessionStorage.removeItem("clauseweft-user");
  $("#shell").hidden = true;
  $("#access").hidden = false;
  setMobileNav(false);
  closeNavGroups();
  document.title = "ClauseWeft · 研发工作台";
}
function identity(user) {
  return JSON.stringify([user.id, user.role, user.capabilities, user.workspace_ids, user.project_ids]);
}
async function refreshIdentity() {
  const user = await api("auth/me");
  if (identity(user) !== identity(state.user || {})) {
    clearContext();
    state.user = user;
    await refreshCatalog();
    return true;
  }
  state.user = user;
  return false;
}
const keys = new Map();
let toastTimer;
function notify(message, error = false) {
  const box = $("#notice");
  box.textContent = message;
  box.classList.toggle("error", error);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), error ? 9000 : 4000);
}
async function api(path, method = "GET", data, revision) {
  const epoch = state.accessEpoch;
  const headers = { Authorization: "Bearer " + state.token };
  const fingerprint = JSON.stringify([path, method, data, revision]);
  if (data !== undefined) {
    headers["Content-Type"] = "application/json";
    if (!keys.has(fingerprint)) keys.set(fingerprint, crypto.randomUUID());
    headers["Idempotency-Key"] = keys.get(fingerprint);
  }
  if (revision !== undefined) headers["If-Match"] = '"' + revision + '"';
  const response = await fetch("/api/v1/" + path, {
    method,
    headers,
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const body = await response.json();
  if (epoch !== state.accessEpoch) throw Error("工作空间或权限已变化，请重新操作");
  if (response.status < 500) keys.delete(fingerprint);
  if (!response.ok) {
    if (response.status === 401 && path !== "auth/login") disconnect();
    if (response.status === 403) {
      state.currentRun = null;
      state.designer = null;
      if ($("#sheet").open) closeSheet();
    }
    const detail =
      body.error?.details ||
      body.error?.message ||
      body.error?.code ||
      "请求失败";
    const error = Error(`${response.status} · ${detail}`);
    error.status = response.status;
    throw error;
  }
  return body;
}
async function collection(path) {
  const items = [];
  let cursor = 0;
  while (cursor !== null) {
    const page = await api(
      path + (path.includes("?") ? "&" : "?") + "limit=500&cursor=" + cursor,
    );
    items.push(...page.items);
    cursor = page.next_cursor ?? null;
  }
  return items;
}
function projectQuery() {
  const query = new URLSearchParams({workspace_id: state.workspaceId});
  if (state.project) query.set("project_id", state.project);
  return "?" + query;
}
function workspaceQuery() {
  return "?workspace_id=" + encodeURIComponent(state.workspaceId);
}
function projectName(id) {
  return state.projects.find((p) => p.id === id)?.name || "所属项目";
}
function endpointName(id) {
  return (
    state.endpoints.find((e) => e.endpoint_id === id)?.endpoint_id || "自动路由"
  );
}
function date(value, short = false) {
  if (!value) return "—";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleString(
        "zh-CN",
        short
          ? {
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            }
          : {
              year: "numeric",
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            },
      );
}
function amount(value, currency) {
  if (!currency) return "—";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency,
    minimumFractionDigits: 4,
    maximumFractionDigits: 6,
  }).format((value || 0) / 1000000);
}
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
