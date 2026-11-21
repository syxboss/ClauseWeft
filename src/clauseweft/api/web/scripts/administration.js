function resourceSection(title, body, metadata = "") {
  return `<section class="resource-section"><header class="resource-heading"><h2>${esc(title)}</h2>${metadata ? `<span>${metadata}</span>` : ""}</header>${body}</section>`;
}
function resourceEmpty(title, message) {
  return `<div class="resource-empty"><h3>${state.query ? "没有匹配的记录" : esc(title)}</h3><p>${state.query ? "调整页面顶部的搜索内容后重试。" : esc(message)}</p></div>`;
}
function endpointRecord(model, assigned) {
  let status = "已授权派发";
  let style = "enabled";
  if (!model.enabled || !model.allowed || !assigned) {
    status = !model.enabled ? "已停用" : !model.allowed ? "禁止派发" : "未授权给当前空间";
    style = model.enabled && !model.allowed ? "restricted" : "";
  }
  return `<article class="resource-row endpoint-record">
    <div class="endpoint-identity"><h3>${esc(model.endpoint_id)}</h3><small>${esc(model.model_identity)}${model.identity_verified ? " · 身份已核实" : ""}</small>
      <span class="resource-status ${style}">${status}</span></div>
    <div><span class="resource-label">网关 · ${model.location === "local" ? "本地" : "云端"}</span>${esc(model.gateway_id)}
      <small>${esc(model.endpoint_alias || model.endpoint_id)} · v${esc(model.revision)}</small></div>
    <div class="model-rates"><span class="resource-label">${esc(model.currency)} / 百万 token</span>
      <dl><dt>输入</dt><dd>${esc(decimalPrice(model.input_micros_per_million))}</dd><dt>输出</dt><dd>${esc(decimalPrice(model.output_micros_per_million))}</dd></dl></div>
    <div class="resource-controls">${button("edit-endpoint", "配置", model.endpoint_id)}<details class="resource-menu" name="model-maintenance"><summary>维护</summary><div>
      ${button("probe-endpoint", "探测连接", model.endpoint_id, "text-button")}
      ${button("endpoint-history", "探测记录", model.endpoint_id, "text-button")}
      ${button("toggle-endpoint", model.enabled ? "停用模型" : "启用模型", model.endpoint_id, "text-button")}</div></details></div></article>`;
}
async function endpoints() {
  const workspace = state.workspaces.find((item) => item.id === state.workspaceId);
  const assigned = new Set(workspace?.endpoint_ids || []);
  const items = state.endpoints.filter(matches);
  const groups = [
    [`已分配给${workspace?.name || "当前工作空间"}`, items.filter((model) => assigned.has(model.endpoint_id))],
    ["尚未分配给当前空间", items.filter((model) => !assigned.has(model.endpoint_id))],
  ];
  const sections = groups.filter(([, models]) => models.length).map(([title, models]) =>
    resourceSection(title, models.map((model) => endpointRecord(model, assigned.has(model.endpoint_id))).join(""), `${models.length} 个模型`));
  return heading("模型配置", "", button("new-endpoint", "添加模型", "", "primary")) +
    (sections.join("") || resourceSection("模型目录", resourceEmpty("尚未登记模型", "添加网关中的模型别名和输入、输出价格，勾选「用于当前工作空间」后即可派发。")));
}
function resourceRefreshTime() {
  const now = new Date();
  const time = now.toLocaleTimeString("zh-CN", {hour12:false, hour:"2-digit", minute:"2-digit", second:"2-digit"});
  return `<div class="resource-sync">最近更新 <time datetime="${now.toISOString()}">${esc(time)}</time><a href="#runs">查看任务进度 →</a></div>`;
}
async function approvals() {
  const data = await api("overview" + projectQuery());
  const items = (data.approvals || []).filter(matches);
  const rows = items.map((item) => `<article class="resource-row approval-record"><div><h3>${esc(approvalTitle(item))}</h3>
    <small>${esc(projectName(item.project_id))}</small></div><div><span class="resource-label">等待确认</span>${esc(item.label || item.node_label || item.node_id || "")}
    ${item.reason ? `<small>${esc(item.reason)}</small>` : ""}</div>${button("open-run", "查看任务", item.run_id)}</article>`);
  return heading("审批中心", "", button("refresh", "刷新")) + resourceRefreshTime() +
    resourceSection("待处理事项", rows.join("") || resourceEmpty("当前没有待审批事项", "任务到达人工审批节点后会出现在这里。"), `${items.length} 项`);
}
function usageQuantity(row, name) {
  const partial = row[name] === null;
  const count = partial ? row["known_" + name] : row[name];
  return `<span class="mono">${Number(count || 0).toLocaleString("zh-CN")}</span>${partial ? "（已知）" : ""}`;
}
function usageRow(model) {
  const prices = model.pricing_snapshot;
  const rate = (field) => prices?.[field] == null ? "未记录" : decimalPrice(prices[field]);
  return `<article class="resource-row usage-record">
    <div><h3>${esc(model.endpoint_id)}</h3><small>${esc(model.model_identity || "")}</small>
      <small>${model.endpoint_revision == null ? "版本未记录" : "v" + esc(model.endpoint_revision)} · ${model.configured ? "当前配置" : "历史配置"}</small></div>
    <div><span class="resource-label">模型调用</span><b class="mono">${esc(model.calls)}</b>
      ${model.failed_calls ? `<small class="resource-warning">${esc(model.failed_calls)} 次失败</small>` : ""}</div>
    <div class="usage-tokens"><span class="resource-label">token 数 / 百万 token 单价</span><dl>
      <dt>输入</dt><dd>${usageQuantity(model,"input_tokens")} / ${rate("input_micros_per_million")}</dd>
      <dt>输出</dt><dd>${usageQuantity(model,"output_tokens")} / ${rate("output_micros_per_million")}</dd></dl>
      ${model.missing_usage_calls ? `<small class="resource-warning">${esc(model.missing_usage_calls)} 次缺少用量回执</small>` : ""}</div>
    <div class="usage-spend"><span class="resource-label">已结算</span><b class="mono">${amount(model.settled_micros,model.currency)}</b>
      ${model.held_micros || model.pending_calls ? `<small>预留 ${amount(model.held_micros,model.currency)} · ${esc(model.pending_calls)} 次尚未结算</small>` : ""}</div></article>`;
}
async function usage() {
  const data = await api("usage" + projectQuery());
  const models = (data.models || []).filter(matches);
  const totals = data.totals || [];
  const ledger = totals.map((total) => `<article class="usage-balance">
    <div><span class="resource-label">已结算 · ${esc(total.currency)}</span><strong>${amount(total.settled_micros, total.currency)}</strong></div>
    <dl><dt>模型调用</dt><dd>${esc(total.calls)} 次</dd><dt>预留费用</dt><dd>${amount(total.held_micros, total.currency)}</dd>
    <dt>尚未结算</dt><dd class="${total.pending_calls ? "resource-warning" : ""}">${esc(total.pending_calls)} 次</dd></dl></article>`);
  const active = models.filter((model) => model.calls > 0);
  const idle = models.filter((model) => model.calls === 0);
  const sections = [];
  for (const currency of new Set(active.map((model) => model.currency))) {
    const rows = active.filter((model) => model.currency === currency);
    sections.push(resourceSection(currency + " 调用明细", rows.map(usageRow).join(""), `${rows.length} 个模型版本`));
  }
  if (idle.length) {
    const rows = idle.map((model) => `<div class="idle-model"><b>${esc(model.endpoint_id)}</b><span class="mono">v${esc(model.endpoint_revision)}</span>
      <span>${esc(model.currency)} / 百万 token · 输入 ${esc(decimalPrice(model.pricing_snapshot?.input_micros_per_million))} · 输出 ${esc(decimalPrice(model.pricing_snapshot?.output_micros_per_million))}</span></div>`);
    sections.push(resourceSection("已配置，暂无调用", rows.join(""), `${rows.length} 个模型版本`));
  }
  return heading("模型用量", "", button("refresh", "刷新")) + resourceRefreshTime() +
    (ledger.length ? `<div class="usage-ledger" aria-label="分币种账本">${ledger.join("")}</div>` : "") +
    (sections.join("") || resourceSection("调用明细", resourceEmpty("还没有模型调用", "为工作空间分配模型并启动任务后，可在这里核对实际用量和费用。"),
      can("admin") && !state.query ? button("configure-models", "配置模型", "", "text-button") : "")) +
    (active.length ? '<p class="resource-footnote">单价按派发时的模型版本记录，以该币种每百万 token 计。缺少回执时，仅展示已知 token 数。</p>' : "");
}
async function audit() {
  const query = new URLSearchParams(state.auditFilters);
  if (state.project) query.set("project_id", state.project);
  const queryKey = query.toString();
  if (state.auditQueryKey !== queryKey) {
    state.auditQueryKey = queryKey;
    state.auditCursor = 0;
  }
  query.set("limit", "100");
  query.set("cursor", String(state.auditCursor));
  const page = await api("audit?" + query);
  state.auditNextCursor = page.next_cursor;
  const items = page.items.filter(matches);
  state.auditItems = items;
  const verification = state.auditVerification;
  return (
    heading(
      "审计日志",
      "",
      button("verify-audit", "校验审计链") +
        button("export-audit", "导出完整审计") +
        button("refresh", "↻ 刷新"),
    ) +
    `<details class="resource-filters" id="audit-query" ${Object.keys(state.auditFilters).length || $("#audit-query")?.open ? "open" : ""}>
      <summary>筛选审计记录${Object.keys(state.auditFilters).length ? " · " + Object.keys(state.auditFilters).length + " 项条件" : ""}</summary><form id="audit-filters" class="audit-filters"><div class="field-grid">${
      field("actor", "操作者 ID", state.auditFilters.actor || "", "text", {
        placeholder: "如 bootstrap-admin",
      }) +
      field("action", "动作", state.auditFilters.action || "", "text", {
        placeholder: "如 run.dispatched",
      }) +
      field(
        "resource",
        "完整资源路径",
        state.auditFilters.resource || "",
        "text",
        {
          placeholder: "如 roles/资源ID",
        },
      ) +
      field("outcome", "结果", state.auditFilters.outcome || "", "select", {
        options: [
          ["", "全部结果"],
          ["succeeded", "成功"],
          ["read", "读取"],
          ["denied", "已拒绝"],
          ["failed", "失败"],
          ["imported", "历史迁移"],
        ],
      }) +
      field("run_id", "运行 ID", state.auditFilters.run_id || "", "text", {
        placeholder: "指定运行的完整记录",
      })
    }</div><div class="actions"><button class="primary" type="submit">应用筛选</button>${button("clear-audit-filters", "清除筛选")}<span class="muted">${items.length} 条匹配记录</span></div></form></details>${
      verification
        ? `<div class="audit-verification ${verification.valid ? "verified" : "invalid"}"><div><strong>${verification.valid ? "审计链校验通过" : "审计链存在不一致"}</strong><p>已核验 ${esc(verification.checked_entries)} 条记录，截至序号 ${esc(verification.anchor?.seq)}。</p></div>${button("audit-verification-detail", "查看校验依据", "", "text-button")}</div>`
        : ""
    }` +
    resourceSection("审计记录",
      items.length
        ? table(
            ["序号 / 时间", "操作者", "操作", "结果", "资源", "证据"],
            items.map(
              (a) =>
                `<tr><td class="title-cell"><strong class="mono">#${esc(a.seq ?? a.id)}</strong><small>${date(a.created || a.timestamp || a.at)}</small></td><td>${esc(a.actor?.display_name || a.actor_name || a.actor || "系统")}<small class="muted audit-actor">${esc(a.actor_id || "")}</small></td><td>${esc(actionLabel(a.action || a.kind))}</td><td>${auditOutcome(a.outcome)}</td><td class="audit-resource mono">${esc(a.resource_id || a.target || a.resource || "—")}</td><td>${button("audit-detail", "查看详情", a.seq ?? a.id, "text-button")}</td></tr>`,
            ),
          )
        : resourceEmpty("没有符合条件的审计记录", "调整筛选条件，或清除筛选查看其他记录。"),
      `${items.length} 条 / 本页`) + `<div class="actions audit-pagination">${state.auditCursor ? button("audit-previous", "上一页") : ""}${state.auditNextCursor !== null && state.auditNextCursor !== undefined ? button("audit-next", "下一页") : ""}<span class="muted">每页 100 条，完整链可通过导出保存。</span></div>`
  );
}
function auditOutcome(value) {
  const labels = {
    succeeded: "成功",
    denied: "已拒绝",
    failed: "失败",
    read: "读取",
    migrated: "历史迁移",
    imported: "历史迁移",
  };
  return `<span class="badge audit-${esc(value || "legacy")}">${esc(labels[value] || value || "历史记录")}</span>`;
}
function auditDetail(item) {
  const evidence = [
    ["审计序号", item.seq ?? item.id],
    ["记录时间", date(item.at)],
    ["操作者", item.actor?.display_name || item.actor || item.actor_id],
    ["操作者 ID", item.actor_id],
    ["动作", item.action],
    ["资源", item.resource],
    ["项目", item.project_id],
    ["运行", item.run_id],
    ["关联请求", item.correlation_id],
    ["前一条哈希", item.prev_hash],
    ["本条哈希", item.hash],
    ["变更前摘要", item.before_sha256],
    ["变更后摘要", item.after_sha256],
  ];
  return `<div class="actions">${auditOutcome(item.outcome)}</div><dl>${evidence.map(([label, value]) => `<div class="key-value"><dt>${label}</dt><dd class="mono">${esc(value ?? "—")}</dd></div>`).join("")}</dl><section class="artifact-section"><h3>操作依据与证据</h3><pre>${esc(pretty(item.details || {}))}</pre></section><div class="audit-snapshots"><section class="artifact-section"><h3>变更前的脱敏快照</h3><pre>${esc(pretty(item.before ?? "未保存快照"))}</pre></section><section class="artifact-section"><h3>变更后的脱敏快照</h3><pre>${esc(pretty(item.after ?? "未保存快照"))}</pre></section></div>`;
}
function memberGrants(member = {}) {
  return `<p class="field-help">管理员可以访问实例全部数据。执行者和只读成员仅能访问下面明确分配的工作空间和项目，新成员默认不分配。</p><div id="member-grants" ${member.role === "admin" ? "hidden" : ""}><fieldset class="grant-selection"><legend>工作空间访问权限</legend>${state.workspaces.map((workspace) => `<label class="check"><input type="checkbox" name="workspace_ids" value="${esc(workspace.id)}" ${(member.workspace_ids || []).includes(workspace.id) ? "checked" : ""}><span>${esc(workspace.name)}${workspace.is_example || workspace.id === "example" ? " · 示例" : ""}</span></label>`).join("") || '<p class="field-help">暂无可分配的工作空间。</p>'}</fieldset><fieldset class="grant-selection"><legend>项目数据访问权限</legend>${(state.grantProjects || []).map((project) => `<label class="check"><input type="checkbox" name="project_ids" value="${esc(project.id)}" data-workspace="${esc(project.workspace_id)}" ${(member.project_ids || []).includes(project.id) ? "checked" : ""} ${!(member.workspace_ids || []).includes(project.workspace_id) ? "disabled" : ""}><span>${esc(project.name)}<small>${esc(state.workspaces.find((workspace) => workspace.id === project.workspace_id)?.name || project.workspace_id)}</small></span></label>`).join("") || '<p class="field-help">暂无项目，成员获得工作空间权限后，执行者可创建自己负责的项目。</p>'}<p class="field-help">请先分配工作空间，再选择项目。进入工作空间不会自动获得其中所有项目的数据。</p></fieldset></div>`;
}
function selectedMemberGrants() {
  const data = new FormData($("#sheet-form"));
  return {
    workspace_ids: data.get("role") === "admin" ? [] : data.getAll("workspace_ids"),
    project_ids: data.get("role") === "admin" ? [] : data.getAll("project_ids"),
  };
}
function workspaceForm(item) {
  const repositories = state.adminConfig?.repositories || [];
  form(item ? "工作空间设置" : "创建工作空间",
    field("name", "工作空间名称", item?.name || "", "text", {required: true}) +
    field("description", "说明", item?.description || "", "textarea") +
    `<fieldset class="grant-selection"><legend>允许使用的模型</legend>${state.endpoints.map((endpoint) => `<label class="check"><input type="checkbox" name="endpoint_ids" value="${esc(endpoint.endpoint_id)}" ${(item?.endpoint_ids || []).includes(endpoint.endpoint_id) ? "checked" : ""}>${esc(endpoint.endpoint_id)}</label>`).join("") || '<p class="field-help">先在模型连接中登记端点，再为工作空间分配。</p>'}</fieldset><fieldset class="grant-selection"><legend>允许使用的仓库</legend>${repositories.map((entry) => { const id = typeof entry === "string" ? entry : entry.id || entry.repository_id; return `<label class="check"><input type="checkbox" name="repository_ids" value="${esc(id)}" ${(item?.repository_ids || []).includes(id) ? "checked" : ""}>${esc(id)}</label>`; }).join("") || '<p class="field-help">暂无已登记的受信仓库。</p>'}</fieldset><p class="field-help">授权范围在服务端核验，未分配的模型和仓库不能被此工作空间的运行使用。</p>`,
    async (data) => {
      const values = new FormData($("#sheet-form"));
      await api("workspaces" + (item ? "/" + item.id : ""), item ? "PATCH" : "POST", {
        name: data.name, description: data.description,
        endpoint_ids: values.getAll("endpoint_ids"), repository_ids: values.getAll("repository_ids"),
      }, item?.revision);
      closeSheet();
      await refreshCatalog();
      await render();
      notify("工作空间设置已保存");
    });
}
async function settings() {
  const [members, config] = await Promise.all([
    collection("members"),
    api("settings"),
  ]);
  state.members = members;
  state.adminConfig = config;
  state.grantProjects = (await Promise.all(state.workspaces.map((workspace) =>
    collection("projects?workspace_id=" + encodeURIComponent(workspace.id)),
  ))).flat();
  const memberRows = members.filter(matches).map((member) => `<article class="resource-row resource-record">
    <div><h3>${esc(member.display_name || member.username)}</h3><small class="mono">${esc(member.username)}</small></div>
    <div><span class="resource-label">权限角色</span>${esc(roleLabels[member.role] || member.role)}</div>
    <div>${badge(member.active ? "active" : "disabled")}<small>${member.role === "admin" ? "实例内全部数据" :
      `${(member.workspace_ids || []).length} 个工作空间 · ${(member.project_ids || []).length} 个项目`}</small></div>
    <div class="resource-controls">${button("edit-member", "管理成员", member.id)}</div></article>`);
  const workspaces = state.workspaces.filter(matches).map((workspace) => `<article class="resource-row resource-record">
    <div><h3>${esc(workspace.name)}</h3><small>${esc(workspace.description || workspace.id)}</small></div>
    <div><span class="resource-label">工作空间用途</span>${workspace.is_example || workspace.id === "example" ? "示例" : "团队业务"}</div>
    <div><span class="resource-label">授权资源</span>${(workspace.endpoint_ids || []).length} 个模型 · ${(workspace.repository_ids || []).length} 个仓库</div>
    <div class="resource-controls">${button("edit-workspace", "管理授权", workspace.id)}</div></article>`);
  const configNames = {workspace_id:"实例标识", data_scope:"数据策略", repositories:"受信仓库", suites:"验收套件",
    gateways:"已配置网关", session_hours:"会话有效期", access_model:"访问控制", deployment:"部署模式"};
  const entries = Object.entries(config).map(([key,value]) =>
    `<div class="key-value"><dt>${esc(configNames[key] || key)}</dt><dd>${settingValue(key,value)}</dd></div>`);
  return heading("团队与设置", "", button("new-member", "添加成员", "", "primary")) +
    resourceSection("团队成员", memberRows.join("") || resourceEmpty("尚无具名成员", "添加成员并分配工作空间、项目访问权限。"), `${memberRows.length} 位成员`) +
    resourceSection("工作空间授权", workspaces.join("") || resourceEmpty("尚无工作空间", "创建工作空间后可为其分配模型与仓库。"),
      button("new-workspace", "创建工作空间", "", "text-button")) +
    resourceSection("实例配置", `<dl>${entries.join("")}</dl>`) +
    `<div class="actions">${button("logout", "退出登录", "", "danger")}</div>`;
}
