function runKind(run) {
  return run.template_name || ({single: "直接任务", complex: "复杂任务", tool: "项目工具", workflow: "代码交付", sop: "自定义 SOP"})[run.mode] || run.mode;
}
function runProgress(run) {
  if (run.mode === "complex") return complexProgress(run);
  if (run.mode === "single") {
    const node = run.nodes[0];
    if (node?.artifact) return "结果已生成";
    return states[node?.state] || states[run.status] || "已提交";
  }
  return run.nodes.filter((node) => node.state === "succeeded").length + " / " + run.nodes.length;
}
function runTable(runs) {
  if (!runs.length) {
    if (state.query || state.status) return deskEmpty("没有符合筛选条件的任务", "调整状态或搜索词后再查看。");
    return deskEmpty("暂无任务", "", button("new-run", "新建任务", "", "desk-link"));
  }
  return table(
    ["任务名称", "状态", "所属项目", "执行进度", "创建时间", ...(can("usage") ? ["已结算"] : [])],
    runs.map((run) => `<tr>
      <td class="title-cell">${button("open-run", `<strong>${esc(run.title || "研发任务")}</strong><small>${esc(runKind(run))} · ${esc(run.run_id.slice(0, 12))}</small>`, run.run_id, "table-link")}</td>
      <td>${badge(run.budget_source === "no_configured_model" ? "not_configured" : run.status)}</td>
      <td>${esc(projectName(run.project_id))}</td><td>${esc(runProgress(run))}</td>
      <td class="desk-number">${date(run.created, true)}</td>
      ${can("usage") ? `<td class="desk-number">${run.budget_source === "no_configured_model" ? "—" : amount(run.budget?.settled_micros, run.budget?.currency)}</td>` : ""}
    </tr>`),
  );
}
function approvalTitle(item) {
  return (
    item.title ||
    item.run_title ||
    item.run_id?.slice(0, 12) ||
    item.node_id ||
    "人工审批"
  );
}
function approvalsHTML(items, compact = false) {
  if (!items.length)
    return empty("暂无待审批事项", "流程到达人工节点后，会在这里汇总。");
  return /* HTML */ `<div class="approvals-list">
    ${items
      .map(
        (item) =>
          `<div class="approval-item"><span class="approval-icon">✓</span><div><strong>${esc(approvalTitle(item))}</strong><p>${esc(item.label || item.node_label || states[item.node_id] || item.node_id || item.reason || "等待人工确认")}</p>${compact ? "" : `<p>${esc(projectName(item.project_id))} · ${esc(item.reason || "查看产物并记录审批依据")}</p>`}</div>${button("open-run", "查看 →", item.run_id, "text-button")}</div>`,
      )
      .join("")}
  </div>`;
}
function actionLabel(action) {
  const events = {
    "api.get": "读取平台记录",
    "api.post": "提交平台操作",
    "api.patch": "修改平台配置",
    "run.claimed": "领取执行节点",
    "run.dispatched": "派发模型请求",
    "run.attempt_finished": "记录模型回执",
    "run.gate_completed": "完成审批节点",
    "run.fork_created": "创建派生任务",
    "run.checkpoint_created": "保存运行检查点",
    "run.run_created": "登记新运行",
    "resource.saved": "保存资源版本",
    "resource.snapshot": "迁移历史资源",
  };
  if (events[action]) return events[action];
  const [method, resource] = String(action || "").split(".");
  const names = {
    runs: "任务",
    projects: "项目",
    requirements: "需求",
    roles: "Agent 角色",
    skills: "技能",
    templates: "SOP 模板",
    endpoints: "模型配置",
    members: "团队成员",
    evaluations: "路由评估",
    auth: "登录会话",
  };
  const verbs = { post: "创建或推进", patch: "更新", delete: "删除" };
  return names[resource]
    ? (verbs[method] || "更新") + names[resource]
    : action || "运行更新";
}
function settingValue(key, value) {
  if (key === "data_scope")
    return value === "local_only" ? "仅本地处理" : "允许按任务授权外发";
  if (key === "session_hours") return esc(value) + " 小时";
  if (key === "access_model") return "管理员、执行者、只读成员";
  if (key === "repositories")
    return value.length
      ? value
          .map(
            (repo) =>
              `<div><strong>${esc(repo.id)}</strong><p class="field-help">允许路径 · ${esc(repo.allowlist.join("、"))}</p></div>`,
          )
          .join("")
      : "未登记仓库";
  if (Array.isArray(value))
    return value.length
      ? value.map((item) => `<span class="pill">${esc(item)}</span>`).join(" ")
      : "尚未配置";
  return esc(value);
}
function activityHTML(items) {
  return items.length
    ? `<div class="activity-list">${items
        .slice(0, 6)
        .map(
          (a) =>
            `<div class="activity-item"><span class="activity-dot"></span><div><strong>${esc(a.summary || actionLabel(a.action || a.kind))}</strong><p>${esc(a.actor?.display_name || a.actor_name || a.actor || "系统")} · ${date(a.created || a.timestamp || a.at, true)}</p></div></div>`,
        )
        .join("")}</div>`
    : empty("暂无动态", "操作、执行与审批记录会在此更新。");
}
function deskEmpty(title, detail = "", action = "") {
  return `<div class="desk-empty"><p>${esc(title)}</p>${detail ? `<small>${esc(detail)}</small>` : ""}${action}</div>`;
}
function projectCards(projects) {
  return projects.map((project) => `<button type="button" class="desk-project" data-action="open-project" data-id="${esc(project.id)}">
    <span class="desk-folder">${icon("projects")}</span>
    <span class="desk-project-name"><strong>${esc(project.name)}</strong>${project.description ? `<small>${esc(project.description)}</small>` : ""}</span>
    <span class="desk-project-status">${project.status === "archived" ? "已归档" : "进行中"}</span>
    <span class="desk-arrow" aria-hidden="true">↗</span>
  </button>`).join("");
}
function availableModels() {
  return state.modelOptions.filter((model) => model.enabled !== false && model.allowed !== false);
}
function modelSetupHint() {
  return can("admin")
    ? `<p>登记模型、价目并授权给当前工作空间后，即可执行任务。</p>${button("configure-models", "配置模型", "", "primary")}`
    : '<p>当前工作空间还没有可用模型，请联系管理员配置并授权。</p>';
}
function deskMasthead(title, scope, actions = "") {
  return `<header class="desk-masthead"><div><p class="desk-scope">${esc(scope)}</p><h1>${esc(title)}</h1></div>
    ${actions ? `<div class="desk-head-actions">${actions}</div>` : ""}</header>`;
}
const quickModes = [
  {id: "", label: "直接执行", icon: "runs", submit: "开始执行"},
  {id: "__complex__", label: "并行分析", icon: "templates", submit: "设置总预算"},
  {id: "__delivery__", label: "代码交付", icon: "projects", submit: "配置交付流程"},
  {id: "__tool__", label: "项目工具", icon: "skills", submit: "选择工具"},
];
function composer() {
  if (!can("write")) {
    return `<section class="desk-composer"><header><h2>项目任务</h2></header><div class="desk-reader">
      <span>${icon("runs")}</span><h3>查看执行记录</h3><p>当前账号可读取所选项目的任务与成果。</p><a class="desk-link" href="#runs">查看任务列表 →</a></div></section>`;
  }
  const models = availableModels();
  const projects = state.projects.filter((project) => project.status !== "archived");
  const draftScope = JSON.stringify([state.user.id, state.workspaceId, state.project]);
  const oldForm = $("#quick-task");
  const draft = oldForm?.dataset?.scope === draftScope ? Object.fromEntries(new FormData(oldForm)) : {};
  const choice = draft.project_id ?? state.project;
  const project = projects.some((p) => p.id === choice) ? choice : projects.length === 1 ? projects[0].id : "";
  const mode = quickModes.find((item) => item.id === draft.template_id) || quickModes[0];
  const ready = projects.length > 0 && models.length > 0;
  const setup = [];
  if (!projects.length) setup.push(button("new-project", "创建项目", "", "desk-link"));
  if (!models.length) setup.push(can("admin") ? button("configure-models","配置模型","","desk-link") : "请联系管理员授权可用模型");
  return `<section class="desk-composer"><header><h2>发起任务</h2>${can("admin") ? '<a class="desk-link" href="#agents">使用外部 Agent →</a>' : ""}</header>
    <form id="quick-task" data-scope="${esc(draftScope)}">
      <fieldset class="desk-modes"><legend class="visually-hidden">执行方式</legend>${quickModes.map((item) => `<label>
        <input type="radio" name="template_id" value="${item.id}" ${mode.id === item.id ? "checked" : ""}><span>${icon(item.icon)}${item.label}</span></label>`).join("")}</fieldset>
      <label class="desk-input-label" for="desk-goal">任务目标</label>
      <textarea id="desk-goal" name="text" required rows="3" placeholder="描述目标、输入材料与期望结果…">${esc(draft.text || "")}</textarea>
      ${materialPicker(oldForm?.dataset?.scope === draftScope ? savedMaterials(oldForm) : [])}
      ${state.workspace?.has_personal_preferences ? `<div class="quick-preferences" ${mode.id ? "hidden" : ""}>${personalPreferenceChoice(draft.use_preferences === "on" && !mode.id)}</div>` : ""}
      <div class="desk-controls"><label>所属项目<select name="project_id" required>${options([["",projects.length ? "选择项目" : "还没有项目"], ...projects.map((p) => [p.id,p.name])],project)}</select></label>
        <label>执行模型<select name="endpoint_id">${options([["",models.length ? "自动选择" : "暂无可用模型"], ...models.map((m) => [m.endpoint_id,m.endpoint_id])],draft.endpoint_id || "")}</select></label></div>
      <footer>${ready ? "" : `<div class="desk-setup">${setup.join("")}</div>`}
        <button type="submit" class="primary desk-submit" ${ready ? "" : "disabled"}><span class="submit-label">${mode.submit}</span> <span aria-hidden="true">↗</span></button></footer>
      <p id="quick-error" role="alert" class="form-error"></p></form></section>`;
}
function traceName(node) {
  const labels = {task:"直接执行",plan:"拆分任务",summary:"结果汇总",draft:"需求起草","approve-spec":"基线批准",
    "review-product":"产品评审","review-test":"测试评审",code:"实现","test-design":"测试设计",integrate:"整合候选",
    "code-review":"代码评审","execute-tests":"执行验收",accept:"交付接纳"};
  const branch = /^branch-(\d+)$/.exec(node.node_id);
  return node.label || (branch ? "子任务 " + branch[1] : labels[node.node_id] || node.node_id);
}
function traceDiagram(nodes) {
  if (!nodes.length) return '<p class="trace-empty">等待节点登记</p>';
  const byId = new Map(nodes.map((node) => [node.node_id, node]));
  const ranks = new Map();
  function rank(id) {
    if (ranks.has(id)) return ranks.get(id);
    const deps = (byId.get(id).dependencies || []).filter((dep) => byId.has(dep));
    const result = deps.length ? 1 + Math.max(...deps.map(rank)) : 0;
    ranks.set(id, result);
    return result;
  }
  const lanes = [];
  for (const node of nodes) (lanes[rank(node.node_id)] ||= []).push(node);
  const height = Math.max(146, 24 + Math.max(...lanes.map((lane) => lane.length)) * 64);
  const width = Math.max(323, 112 * lanes.length);
  const positions = new Map();
  lanes.forEach((lane,col) => lane.forEach((n,row) => {
    positions.set(n.node_id, {x:(col+.5)*width/lanes.length, y:height/2 + (row-(lane.length-1)/2)*64 - 5});
  }));
  const paths = [], labels = [];
  for (const node of nodes) {
    const {x,y} = positions.get(node.node_id);
    for (const dep of node.dependencies || []) {
      if (!positions.has(dep)) continue;
      const from = positions.get(dep), mid = (x+from.x)/2;
      paths.push(`<path data-phase="${esc(node.state)}" d="M ${from.x+12} ${from.y} C ${mid} ${from.y} ${mid} ${y} ${x-12} ${y}"/>`);
    }
    const phase = states[node.state] || node.state || "就绪", name = traceName(node);
    const shortName = Array.from(name).length > 9 ? Array.from(name).slice(0,8).join("")+"…" : name;
    labels.push(`<g transform="translate(${x} ${y})" data-phase="${esc(node.state)}">
      <title>${esc(name)} · ${esc(phase)}${node.endpoint_id ? " · "+esc(node.endpoint_id) : ""}</title>
      <circle class="node-halo" r="13"/><circle class="node-pin" r="7"/>
      ${node.state === "succeeded" ? '<path class="node-check" d="m-3 0 2 2 4-5"/>' : '<circle class="node-center" r="2"/>'}
      <text y="29">${esc(shortName)}</text><text class="node-state" y="44">${esc(phase)}</text></g>`);
  }
  return `<div class="trace-map" tabindex="0" role="region" aria-label="节点依赖与执行状态">
    <svg viewBox="0 0 ${width} ${height+16}" width="${width}" height="${height+16}" aria-label="运行节点关系">
      <g class="trace-edges">${paths.join("")}</g>${labels.join("")}</svg></div>`;
}
function runTrace(runs) {
  const run = runs.find((item) => item.run_id === state.traceRunId)
    || runs.find((item) => item.status === "running")
    || runs.find((item) => item.status === "queued") || runs[0];
  const header = '<header><h2>运行轨迹</h2>';
  if (!run) return `<section class="desk-trace">${header}</header><div class="trace-blank">
    <span>${icon("templates")}</span><h3>暂无运行轨迹</h3><p>任务启动后，在这里查看实际节点及其依赖。</p></div></section>`;
  const nodes = run.nodes || [];
  const completed = nodes.filter((node) => node.state === "succeeded").length;
  const status = run.budget_source === "no_configured_model" ? "not_configured" : run.status;
  const active = nodes.filter((node) => ["leased","dispatched","needs_input","uncertain"].includes(node.state));
  const important = active[0] || nodes.find((node) => node.state === "ready");
  const focus = important ? `<p class="trace-focus"><b>${esc(states[important.state])}</b>
    ${esc(important.brief || traceName(important))}${active.length > 1 ? ` <small>等 ${active.length} 个节点</small>` : ""}</p>` : "";
  return `<section class="desk-trace" data-running="${["running","queued","needs_input"].includes(run.status)}">${header}<span class="trace-status" data-phase="${esc(status)}">${esc(states[status] || status)}</span></header>
    <div class="trace-selection"><select id="trace-run" aria-label="选择要查看轨迹的任务">
      ${options(runs.map((r) => [r.run_id,r.title || r.run_id]),run.run_id)}</select>
      <p>${esc(runKind(run))} · ${esc(projectName(run.project_id))}</p></div>
    ${traceDiagram(nodes)}${focus}
    <footer><span><strong>${completed}</strong> / ${nodes.length} 个节点已完成</span>
      ${button("open-run","查看详情 →",run.run_id,"desk-link")}</footer></section>`;
}
function recentTasks(runs) {
  const items = runs.filter(matches);
  if (!items.length) return deskEmpty(state.query ? "没有匹配的最近任务" : "暂无任务", "", state.query ? "" : button("new-run", "新建任务 →", "", "desk-link"));
  return `<ul class="desk-recent">${items.map((run) => `<li>
    <a href="#runs/${encodeURIComponent(run.run_id)}" data-action="open-run" data-id="${esc(run.run_id)}">
      <span class="desk-recent-name"><strong>${esc(run.title || "研发任务")}</strong>
        <small>${esc(projectName(run.project_id))} · ${esc(runKind(run))}</small></span>
      ${badge(run.budget_source === "no_configured_model" ? "not_configured" : run.status)}
      <time class="desk-number">${date(run.created, true)}</time>
    </a></li>`).join("")}</ul>`;
}
function homeAgentJobs(jobs) {
  const items = (jobs.recent || []).filter(matches);
  if (!items.length) return "";
  return `<section class="desk-section"><header><h2>外部 Agent</h2><a href="#agents">查看任务 →</a></header>
    <ul class="desk-recent">${items.map(job => `<li${agentBusy(job) ? " data-agent-live" : ""}>
      <a href="#agents/${encodeURIComponent(job.id)}"><span class="desk-recent-name"><strong>${esc(agentTitle(job))}</strong>
        <small>${esc(agentNames[job.kind] || job.runner_id)} · ${esc(projectName(job.project_id))}</small></span>
        ${agentStatus(job.status)}<time class="desk-number">${date(job.created, true)}</time></a></li>`).join("")}</ul></section>`;
}
async function overview() {
  const [data, usage] = await Promise.all([
    api("overview" + projectQuery()), can("usage") ? api("usage" + projectQuery()) : {totals: []},
  ]);
  const scope = state.project ? projectName(state.project) : "全部授权项目";
  const totals = (usage.totals || []).filter((total) => total.calls > 0);
  const external = data.agent_jobs || {};
  const metrics = [
    ["项目", data.projects], ["运行与排队", data.active_runs + (external.active || 0)], ["已完成", data.completed_runs + (external.completed || 0)],
  ];
  if (can("usage")) metrics.push(["模型网关费用", totals.length ? totals.map((t) => amount(t.settled_micros, t.currency)).join(" · ") : "—"]);
  const projects = (data.project_list || []).filter(matches);
  const approvals = (data.approvals || []).filter(matches);
  return `<div class="desk">
    ${deskMasthead(state.workspace?.name || state.workspaceId, "工作台 / " + scope, button("refresh", "刷新", "", "desk-refresh"))}
    <div class="desk-command">${composer()}${runTrace((data.recent_runs || []).filter(matches))}</div>
    <dl class="desk-totals">${metrics.map(([name,value]) => `<div><dt>${esc(name)}</dt><dd>${esc(value ?? "—")}</dd></div>`).join("")}</dl>
    <div class="desk-columns">
      <section class="desk-section"><header><h2>最近任务</h2><a href="#runs">全部任务 →</a></header>${recentTasks(data.recent_runs || [])}</section>
      <div class="desk-side">
        ${homeAgentJobs(external)}
        <section class="desk-section"><header><h2>项目</h2><a href="#projects">项目目录 →</a></header>
          ${projectCards(projects.slice(0, 4)) || deskEmpty(state.query ? "没有匹配的项目" : "暂无项目", "", state.query ? "" : button("new-project", "创建项目 →", "", "desk-link"))}</section>
        ${can("approve") ? `<section class="desk-section"><header><h2>待审批</h2><a href="#approvals">审批中心 →</a></header>
          ${approvals.length ? approvalsHTML(approvals.slice(0, 3), true) : deskEmpty(state.query ? "没有匹配的审批事项" : "当前没有待审批事项")}</section>` : ""}
      </div>
    </div></div>`;
}
async function projects() {
  const items = state.projects.filter(matches);
  const actions = button("new-project", "创建项目", "", "primary") +
    (state.project ? button("edit-project", "项目设置", state.project) : "");
  return `<div class="desk desk-directory">
    ${deskMasthead("项目目录", state.workspace?.name || state.workspaceId, actions)}
    <section class="desk-section"><header><h2>授权项目</h2><span class="desk-number">${items.length} 项</span></header>
      ${projectCards(items) || deskEmpty(state.query ? "没有匹配的项目" : "暂无项目", state.query ? "请调整搜索词。" : can("write") ? "创建项目后即可提交任务。" : "请联系管理员分配项目访问权限。")}
    </section></div>`;
}
async function requirements() {
  const items = (await collection("requirements" + projectQuery())).filter(
    matches,
  );
  const columns = ["backlog", "ready", "in_progress", "review", "done"];
  return (
    heading(
      "需求看板",
      "让研发目标、执行流程与交付结果保持关联。",
      button("new-requirement", "＋ 新建需求"),
    ) +
    `<div class="board">${columns
      .map(
        (status) =>
          `<section class="board-column"><header>${badge(status)}<span>${items.filter((i) => i.status === status).length}</span></header>${items
            .filter((i) => i.status === status)
            .map(
              (i) =>
                `<article class="requirement-card" tabindex="0" data-action="view-requirement" data-id="${esc(i.id)}"><small class="mono">${esc(i.id.slice(0, 10))}</small><h3>${esc(i.title)}</h3><footer><span class="priority ${esc(i.priority)}">${esc(priorityLabels[i.priority] || "中")}优先级</span><span>${esc(projectName(i.project_id))}</span><span class="mini-avatar">${esc((i.assignee || "未").slice(0, 1))}</span></footer></article>`,
            )
            .join("")}</section>`,
      )
      .join("")}</div>`
  );
}
