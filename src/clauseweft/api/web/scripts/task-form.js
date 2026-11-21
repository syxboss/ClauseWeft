async function newRun(options = {}) {
  if (!state.projects.length) {
    notify("请先创建一个项目，再提交任务。"); go("projects"); await render(); return;
  }
  const requirement = options.requirement;
  const projectId = requirement?.project_id || options.project_id || state.project || (state.projects.length === 1 ? state.projects[0].id : "");
  const project = state.projects.find((item) => item.id === projectId);
  const models = availableModels();
  const selectedModel = options.endpoint_id || "";
  const currencies = [...new Set(models.map((model) => model.currency))];
  const executionModes = [
    ["", "直接执行一个任务"],
    ["__complex__", "复杂任务 · 自动拆分、并行执行与汇总"],
    ["__delivery__", "代码变更与验证（内置研发流程）"],
    ["__tool__", "项目工具 · 确认后调用"],
    ...state.templates.filter((item) => item.published_revision).map((item) => [item.id, item.name]),
  ];
  form(
    "提交任务",
    `${!models.length ? `<div class="model-setup-hint">${modelSetupHint()}</div>` : ""}
    ${field("text", "研发目标与输入", options.text || requirement?.description || requirement?.title || "", "textarea", {required:true, rows:6, placeholder:"说明要实现的目标、已有内容和验收条件…"})}
    ${materialPicker(options.materials || [])}
    <div class="field-grid">
      ${requirement ? `<label>所属项目<input value="${esc(projectName(requirement.project_id))}" readonly><input type="hidden" name="project_id" value="${esc(requirement.project_id)}"><input type="hidden" name="requirement_id" value="${esc(requirement.id)}"></label>` :
        field("project_id", "项目", projectId, "select", {required:true, options:[["","选择项目"],...state.projects.map((item) => [item.id,item.name])]})}
      ${field("template_id", "执行方式", options.template_id || "", "select", {options:executionModes})}
    </div>
    <p class="field-help" id="complex-hint" hidden>模型先规划 2 至 8 个子任务。各分支并行处理，全部完成后汇总。选择固定模型时，各阶段都使用该模型。</p>
    <div id="project-tool-options" hidden>${field("tool_id","工具使用方式","__auto__","select",{
      options:[["__auto__","按目标选择 · 检索与新建需求"],["__ensure__","查重后新建"],
        ["search_requirements","仅检索项目需求"],["create_requirement","仅新建项目需求"],["search_reference","仅检索项目知识库"]]
    })}
      <div id="tool-query">${field("requirement_query","查重关键词","","text",{help:"用 1 至 160 字指定要核对的需求。命中时引用已有需求，未命中时提议新建。"})}</div>
      <div id="tool-reference">${field("include_reference","允许检索本项目知识库",false,"checkbox")}</div>
      <div id="tool-steps">${field("max_tool_steps","最多执行几次工具",4,"number",{min:1,max:8,step:1})}</div>
      <p class="field-help" id="project-tool-help"></p></div>
    ${modelSelect(selectedModel)}<p class="field-help" id="selected-model-price"></p>
    <details class="optional-panel" id="task-options" ${options.template_id ? "open" : ""}><summary>执行设置与费用上限</summary>
      ${field("title", "任务名称（可选）", requirement?.title || "")}
      <div id="direct-fields" class="field-grid">
        ${field("role_id", "执行角色", "", "select", {options:[["","使用任务内容"],...state.roles.map((item) => [item.id,item.name])]})}
        ${field("stage", "任务类型", "general", "select", {options:Object.entries(stageLabels)})}
      </div>
      <div id="task-preferences">${personalPreferenceChoice()}</div>
      <div class="field-grid">
        ${field("preference", "路由偏好", "cost", "select", {options:[["cost","成本优先"],["quality","质量优先"],["latency","速度优先"]]})}
        ${field("max_output_tokens", "每次输出 token 上限", defaultTaskOutputTokens, "number", {required:true,min:1,max:1000000})}
        ${field("budget", "总费用上限", "", "text")}
        ${field("currency", "上限币种", modelCurrency(selectedModel), "select", {options:[["","按模型确定"],...currencies.map((c) => [c,c])]})}
      </div>
      <p class="field-help" id="budget-help"></p>
      <div id="task-skills">${skillChoices()}</div>
      <div id="repository-fields" hidden>
        <h3 class="form-section">代码变更与验证配置</h3><p class="field-help">各阶段从已授权模型中选择，上方模型用于确定预算币种。负责人审批基线并核验候选和测试证据。</p>
        ${field("repo_id", "受信仓库", project?.repository_id || "", "select", {options:[["","请选择"],...state.workspace.repositories.map((id) => [id,id])]})}
        ${field("base_sha", "基准提交 SHA", "")}
        ${field("suite_id", "验收套件", "", "select", {options:[["","使用仓库默认"],...state.workspace.suites.map((id) => [id,id])]})}
      </div>
      <div class="actions">${button("preview-plan","预览路由与预算")}</div><p class="form-error" id="preview-error"></p><div id="preview-result" class="form-preview"></div>
    </details>`,
    async (data) => {
      data.skill_ids = selectedSkills();
      data.materials = collectMaterials($("#sheet-form"));
      const run = await api("runs","POST",makePlan(data));
      closeSheet(); state.runTab = run.mode === "single" ? "result" : "pipeline";
      go("runs",run.run_id); notify("任务已提交");
    },
    "提交任务", true,
  );
  syncTaskFields();
}
function syncTaskFields() {
  const form = $("#sheet-form");
  const fields = form.elements;
  const selection = fields.template_id.value;
  const template = state.templates.find((item) => item.id === selection);
  const complex = selection === "__complex__";
  const tool = selection === "__tool__";
  $("#project-tool-options").hidden = !tool;
  const reference = state.workspace?.project_references?.[fields.project_id.value];
  fields.tool_id.querySelector('[value="search_reference"]').disabled = !reference;
  if (!reference && fields.tool_id.value === "search_reference") fields.tool_id.value = "__auto__";
  const deduplicate = tool && fields.tool_id.value === "__ensure__";
  const adaptive = tool && (deduplicate || fields.tool_id.value === "__auto__");
  $("#tool-query").hidden = fields.requirement_query.disabled = !deduplicate;
  fields.requirement_query.required = deduplicate;
  $("#tool-steps").hidden = fields.max_tool_steps.disabled = !adaptive;
  fields.max_tool_steps.required = adaptive;
  fields.max_tool_steps.min = deduplicate ? 2 : 1;
  if (deduplicate && fields.max_tool_steps.value === "1") fields.max_tool_steps.value = "2";
  $("#tool-reference").hidden = fields.include_reference.disabled = !reference || !adaptive || deduplicate;
  if (fields.include_reference.disabled) fields.include_reference.checked = false;
  $("#project-tool-help").textContent = (adaptive ? "每一步都会列出工具与参数，批准后执行，模型根据回执决定下一步。" : "批准参数后执行一次，并根据回执整理结果。")
    + (reference && (fields.tool_id.value === "search_reference" || adaptive && !deduplicate) ? " 可用资料源：" + reference : "");
  const delivery = selection === "__delivery__" || template?.execution_mode === "workflow";
  const role = selection ? null : state.roles.find((item) => item.id === fields.role_id.value);
  $("#direct-fields").hidden = Boolean(selection);
  $("#task-preferences").hidden = Boolean(selection);
  $("#task-skills").hidden = Boolean(selection) && !complex;
  $("#repository-fields").hidden = !delivery;
  $("#complex-hint").hidden = !complex;
  // Restore the operator's model and stage when a role binding is cleared.
  for (const [control, fixed] of [[fields.endpoint_id, role?.endpoint_id], [fields.stage, role?.stage]]) {
    if (fixed) {
      if (!control.disabled) control.dataset.manualValue = control.value;
      control.value = fixed;
    } else if (control.disabled) control.value = control.dataset.manualValue || "";
    control.disabled = Boolean(fixed);
  }
  const endpointId = role?.endpoint_id || fields.endpoint_id.value;
  $("#selected-model-price").textContent = (role?.endpoint_id ? `角色指定模型 ${endpointId}。` : "") + modelPriceHint(endpointId);
  const currency = modelCurrency(endpointId);
  if (currency) fields.currency.value = currency;
  fields.currency.disabled = Boolean(currency);
  const required = complex || tool || delivery || Boolean(template?.nodes?.some((node) => node.kind !== "gate"));
  fields.budget.required = required;
  $("#budget-help").textContent = required ? "请填写覆盖全部模型调用的总费用上限。" : "单任务可留空，由服务端按模型价格与请求上限估算。";
  if (required) $("#task-options").open = true;
  $("#preview-error").textContent = "";
  $("#preview-result").textContent = "";
}
function modelCurrency(identifier) {
  const selected = state.modelOptions.find((model) => model.endpoint_id === identifier);
  const currencies = [...new Set(availableModels().map((model) => model.currency))];
  return selected?.currency || (currencies.length === 1 ? currencies[0] : "");
}
function modelPriceHint(identifier) {
  const model = state.modelOptions.find((item) => item.endpoint_id === identifier);
  return model ? `${model.currency} / 百万 token：输入 ${decimalPrice(model.input_micros_per_million)} · 输出 ${decimalPrice(model.output_micros_per_million)}。${model.max_output_tokens ? "每次输出最多 " + model.max_output_tokens + " token。" : ""}按模型返回的实际用量统计。` : availableModels().length ? `从当前空间 ${availableModels().length} 个已配置模型中按任务选择，实际派发前核对价格与授权。` : "当前空间尚无可用模型。";
}
function makePlan(data) {
  const projectId = data.project_id ?? state.project;
  if (!projectId || !state.projects.some((project) => project.id === projectId))
    throw Error("请选择当前工作空间中已获授权的项目");
  const template = state.templates.find((item) => item.id === data.template_id);
  const builtinDelivery = data.template_id === "__delivery__";
  const delivery = builtinDelivery || template?.execution_mode === "workflow";
  const complex = data.template_id === "__complex__";
  const single = !data.template_id;
  const role = single ? state.roles.find((item) => item.id === data.role_id) : null;
  const endpointId = role?.endpoint_id || data.endpoint_id;
  const model = state.modelOptions.find((item) => item.endpoint_id === endpointId && item.enabled !== false && item.allowed !== false);
  if (!template && !availableModels().length) throw Error("当前工作空间尚无可用模型，请先配置并授权");
  if (endpointId && !model) throw Error("所选模型不在当前工作空间授权范围内");
  const task = {
    text: data.text, preference: data.preference || "cost",
    max_output_tokens: Number(data.max_output_tokens || defaultTaskOutputTokens),
    data_scope: state.workspace.data_scope,
  };
  if (data.materials?.length) task.materials = data.materials;
  if (!role) task.stage = single ? data.stage || "general" : "general";
  if (endpointId && !delivery && !role?.endpoint_id) task.pinned_endpoint = endpointId;
  const plan = {task,project_id:projectId,workspace_id:state.workspaceId};
  if (single && data.use_preferences === "on") plan.use_preferences = true;
  if (String(data.budget ?? "").trim()) {
    const currency = modelCurrency(endpointId) || data.currency;
    if (!currency) throw Error("请选择本次总费用上限的币种");
    plan.budget = {currency,limit_micros:decimalMicros(data.budget,"费用上限")};
  }
  if (data.title) plan.title = data.title;
  if (data.requirement_id) plan.requirement_id = data.requirement_id;
  if (role) plan.role_id = role.id;
  if ((single || complex) && Array.isArray(data.skill_ids) && data.skill_ids.length) plan.skill_ids = data.skill_ids;
  if (complex) {
    if (!plan.budget) throw Error("复杂任务需要覆盖拆分、子任务和汇总的总费用上限");
    plan.mode = "complex";
  }
  if (data.template_id === "__tool__") {
    if (!plan.budget) throw Error("请填写覆盖工具决策和回答的总费用上限");
    plan.mode = "tool";
    const choice = data.tool_id || "__auto__";
    if (choice === "__auto__" || choice === "__ensure__") {
      const minimum = choice === "__ensure__" ? 2 : 1;
      const steps = Number(data.max_tool_steps ?? 4);
      if (!Number.isInteger(steps) || steps < minimum || steps > 8) throw Error(`工具执行次数应为 ${minimum} 至 8 次`);
      plan.max_tool_steps = steps;
      plan.allowed_tools = ["search_requirements", "create_requirement"];
      if (choice === "__ensure__") {
        const query = (data.requirement_query || "").trim();
        if (!query || Array.from(query).length > 160) throw Error("查重关键词应为 1 至 160 字");
        plan.requirement_query = query;
      } else if (data.include_reference === "on") {
        if (!state.workspace.project_references?.[projectId]) throw Error("所选项目尚未登记资料源");
        plan.allowed_tools.push("search_reference");
      }
    } else {
      plan.tool_id = choice;
    }
  }
  if (template) {
    if (template.nodes?.some((node) => node.kind !== "gate") && !plan.budget)
      throw Error("协作流程包含多次调用，请在高级选项中填写总费用上限");
    plan.template_id = template.id;
  }
  if (delivery) {
    if (!plan.budget) throw Error("代码变更与验证包含多次模型调用，请填写本次总费用上限");
    if (!data.repo_id || !data.base_sha) throw Error("请选择受信仓库并填写基准提交 SHA");
    plan.mode="workflow";plan.repo_id=data.repo_id;plan.base_sha=data.base_sha;
    if (data.suite_id) plan.suite_id=data.suite_id;
  }
  return plan;
}
