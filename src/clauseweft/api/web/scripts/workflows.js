async function templates() {
  const items = state.templates.filter(matches);
  const rows = items.map((template) => {
    const gates = template.nodes.filter((node) => node.kind === "gate").length;
    return `<article class="resource-row resource-record"><div><h3>${button("open-template", esc(template.name), template.id, "table-link")}</h3>
      ${template.description ? `<p>${esc(template.description)}</p>` : ""}</div>
      <div><span class="resource-label">${template.execution_mode === "workflow" ? "研发交付" : "自定义 SOP"}</span>
        ${template.nodes.length} 个节点${gates ? ` · ${gates} 项审批` : ""}</div>
      <div><span class="resource-label">发布状态</span>${template.published_revision ? `已发布 v${esc(template.published_revision)}` : "草稿"}
        <small>当前草稿 v${esc(template.revision)}</small></div><div class="resource-controls">
        ${button("open-template", "编辑流程", template.id)}${template.published_revision ? button("run-template", "运行", template.id, "text-button") : ""}</div></article>`;
  });
  return heading("SOP 模板", "", button("new-template", "创建模板", "", "primary")) +
    resourceSection("工作空间模板", rows.join("") || resourceEmpty("尚未创建模板", "编排角色、依赖与审批节点，发布后即可用于新任务。"), `${items.length} 个模板`);
}
function inspector(template, node) {
  if (!node) return empty("选择一个节点", "在画布中选择节点，编辑角色和依赖。");
  return /* HTML */ `<h3>节点配置</h3>
    <form id="node-form">
      ${field("label", "显示名称", node.label || node.node_id, "text", {
        required: true,
      })}${field("kind", "节点类型", node.kind, "select", {
        options: [
          ["model", "Agent 节点"],
          ["gate", "人工审批"],
        ],
      })}${node.kind === "model"
        ? field("stage", "任务阶段", node.stage || "review", "select", {
            options: Object.entries(stageLabels).filter(([k]) => k !== "auto"),
          }) +
          field("role_id", "执行角色", node.role_id || "", "select", {
            options: [
              ["", "自动选择"],
              ...state.roles.map((r) => [r.id, r.name]),
            ],
          }) +
          field("group", "独立模型并行组", node.group || "", "text", {
            help: "同组 Agent 需使用不同且已核实的模型身份",
          })
        : '<p class="field-help" style="margin:15px 0">流程在此暂停，由负责人核对上游产物并记录决定。</p>'}<label>依赖节点</label>${template.nodes
        .filter((n) => n.node_id !== node.node_id)
        .map(
          (n) =>
            `<label class="dependency-choice"><input name="dependencies" type="checkbox" value="${esc(n.node_id)}" ${(node.dependencies || []).includes(n.node_id) ? "checked" : ""}>${esc(n.label || n.node_id)}</label>`,
        )
        .join("")}
      <p class="form-error" id="node-error"></p>
      <div class="actions">
        <button class="primary" type="submit">应用配置</button>${button(
          "remove-node",
          "删除节点",
          node.node_id,
          "danger",
        )}
      </div>
    </form>`;
}
async function templateDetail(id) {
  if (!state.designer || state.designer.id !== id)
    state.designer = await api("templates/" + id);
  const t = state.designer;
  state.node =
    state.node && t.nodes.some((n) => n.node_id === state.node)
      ? state.node
      : t.nodes[0]?.node_id;
  return (
    heading(
      t.name,
      t.description,
      button("back-templates", "返回模板") +
        button("save-template", "保存草稿") +
        button("publish-template", "发布版本", "", "primary"),
    ) +
    `<div class="inline-hint">${t.execution_mode === "workflow" ? "研发交付模板保留服务端的整合、测试与审批契约。可调整节点角色与模型，节点结构由交付引擎校验。" : "Agent 节点消费前序产物，人工审批节点等待负责人决定。发布后新任务使用冻结的模板和角色配置。"}</div><div class="designer resource-designer"><div class="designer-canvas">${graph(t.nodes, state.node, true)}</div><aside class="designer-inspector">${inspector(
      t,
      t.nodes.find((n) => n.node_id === state.node),
    )}</aside></div><div class="designer-toolbar">${button("add-node", "＋ 添加节点")}${button("edit-template-meta", "模板设置")}${(t.published_revision ? button("run-template", "运行此流程", t.id) : "") + button("template-history", "发布历史", t.id) + button("resource-history", "草稿历史", "templates/" + t.id)}<small>草稿 v${t.revision} · 已发布 ${t.published_revision ? "v" + t.published_revision : "未发布"}</small></div>`
  );
}
async function roles() {
  const items = state.roles.filter(matches);
  const rows = items.map((role) => {
    const skills = (role.skill_ids || []).map((id) => state.skills.find((item) => item.id === id)?.name || id);
    return `<article class="resource-row resource-record">
      <div><h3>${esc(role.name)}</h3>${role.description || role.prompt ? `<p>${esc(role.description || role.prompt.slice(0, 120))}</p>` : ""}</div>
      <div><span class="resource-label">任务阶段</span>${esc(stageLabels[role.stage] || role.stage)}</div>
      <div><span class="resource-label">执行模型</span>${esc(role.endpoint_id || "自动路由")}${skills.length ? `<small>技能 · ${esc(skills.join("、"))}</small>` : ""}</div>
      <div class="resource-controls">${button("edit-role", "编辑", role.id)}${button("resource-history", "版本历史", "roles/" + role.id, "text-button")}</div></article>`;
  });
  return heading("Agent 角色", "", button("new-role", "创建角色", "", "primary")) +
    resourceSection("执行角色", rows.join("") || resourceEmpty("尚未创建角色", "创建角色可复用提示、模型和技能，供单任务和 SOP 节点调用。"), `${items.length} 个角色`);
}
async function skills() {
  const items = state.skills.filter(matches);
  const rows = items.map((skill) => `<article class="resource-row resource-record">
    <div><h3>${button("view-skill", esc(skill.name), skill.id, "table-link")}</h3>${skill.description ? `<p>${esc(skill.description)}</p>` : ""}</div>
    <div><span class="resource-label">数据范围</span>${skill.data_scope === "local_only" ? "仅限本地" : "允许外发"}</div>
    <div>${badge(skill.enabled ? "active" : "disabled")}<small class="mono">v${esc(skill.revision)}</small></div>
    <div class="resource-controls">${button("edit-skill", "编辑", skill.id)}${button("resource-history", "版本历史", "skills/" + skill.id, "text-button")}</div></article>`);
  return heading("技能库", "", button("new-skill", "创建技能", "", "primary")) +
    resourceSection("工作空间技能", rows.join("") || resourceEmpty("尚未登记技能", "将团队规范保存为技能，可在任务或角色中选择。"), `${items.length} 项技能`);
}
