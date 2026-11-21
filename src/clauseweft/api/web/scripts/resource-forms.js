async function saveResource(collectionName, item, body) {
  if (!item && ["projects", "requirements", "roles", "skills", "templates"].includes(collectionName))
    body.workspace_id = state.workspaceId;
  return api(
    collectionName + (item ? "/" + item.id : ""),
    item ? "PATCH" : "POST",
    body,
    item?.revision,
  );
}
async function openProject(id) {
  state.traceRunId = null;
  state.project = id;
  $("#project").value = id;
  go("overview");
  await render();
}
function projectForm(item) {
  const workspaceId = state.workspaceId;
  form(
    item ? "项目设置" : "创建项目",
    field("name","项目名称",item?.name || "","text",{required:true,placeholder:"为相关内容和任务命名"})+
      field("description","项目说明（可选）",item?.description || "","textarea")+
      `<details class="optional-panel"><summary>高级设置 · 仓库与归档</summary>${field("repository_id","关联受信仓库",item?.repository_id || "","select",{options:[["","暂不关联"],...(state.workspace.repositories || []).map((id) => [id,id])]})}${field("status","项目状态",item?.status || "active","select",{options:[["active","进行中"],["archived","已归档"]]})}${item ? `<div class="actions">${button("resource-history","项目版本历史","projects/"+item.id)}</div>` : ""}</details>`,
    async (data) => {
      const saved = await saveResource("projects",item,data);
      closeSheet();
      // Creating a project also changes the actor's project grants. Refresh them
      // before selecting the new project, since a changed grant clears the scope.
      if (!(await refreshIdentity())) await refreshCatalog();
      if (!item && state.workspaceId === workspaceId && state.projects.some(project => project.id === saved.id)) {
        state.project = saved.id;
        $("#project").value = saved.id;
        go("overview");
      }
      notify("项目已保存"); await render();
    },
  );
}
async function requirementForm(id) {
  const item = id ? await api("requirements/" + id) : null;
  if (!item && !state.projects.length) {
    notify("请先在当前工作空间创建项目，再记录需求。");
    go("projects");
    await render();
    return;
  }
  if (!can("write")) {
    if (!item) throw Error("当前账号没有创建需求的权限");
    sheet(item.title, `<p class="description">${esc(item.description)}</p><dl><div class="key-value"><dt>所属项目</dt><dd>${esc(projectName(item.project_id))}</dd></div><div class="key-value"><dt>状态</dt><dd>${badge(item.status)}</dd></div><div class="key-value"><dt>负责人</dt><dd>${esc(item.assignee || "未分配")}</dd></div></dl>${item.latest_run_id ? button("open-run", "查看关联任务", item.latest_run_id) : ""}`);
    return;
  }
  form(
    item ? "需求详情" : "新建需求",
    field("title", "需求标题", item?.title || "", "text", { required: true }) +
      field(
        "description",
        "需求说明与验收条件",
        item?.description || "",
        "textarea",
        { rows: 7 },
      ) +
      `<div class="field-grid">${item ? `<label>所属项目<input value="${esc(projectName(item.project_id))}" readonly><input type="hidden" name="project_id" value="${esc(item.project_id)}"><span class="field-help">项目归属创建后固定。如需在其他项目管理，请新建独立需求。</span></label>` : field("project_id", "项目", state.project || "", "select", { options: [["", "请选择项目"], ...state.projects.map((p) => [p.id, p.name])], required: true })}${field("status", "状态", item?.status || "backlog", "select", { options: ["backlog", "ready", "in_progress", "review", "done"].map((s) => [s, states[s]]) })}${field("priority", "优先级", item?.priority || "medium", "select", { options: Object.entries(priorityLabels) })}${field("assignee", "负责人", item?.assignee || "")}</div>${item ? `<div class="actions">${button("requirement-run", "基于此需求启动任务", id)}${item.latest_run_id ? button("open-run", "查看关联任务", item.latest_run_id) : ""}${button("resource-history", "需求版本历史", "requirements/" + id)}</div>` : ""}`,
    async (data) => {
      await saveResource("requirements", item, data);
      closeSheet();
      notify("需求已保存");
      await render();
    },
  );
}
function roleForm(item) {
  form(
    item ? "编辑 Agent 角色" : "创建 Agent 角色",
    field("name", "角色名称", item?.name || "", "text", { required: true }) +
      field("description", "职责说明", item?.description || "") +
      `<div class="field-grid">${field("stage", "阶段", item?.stage || "review", "select", { options: Object.entries(stageLabels).filter(([k]) => k !== "auto") })}${field("endpoint_id", "执行模型", item?.endpoint_id || "", "select", { options: [["", "自动路由"], ...state.modelOptions.map((m) => [m.endpoint_id, m.endpoint_id])] })}</div>` +
      field("prompt", "角色提示与输出契约", item?.prompt || "", "textarea", {
        required: true,
        rows: 8,
        help: "角色提示会随模板发布版本冻结，并进入实际模型请求。",
      }) +
      skillChoices(item?.skill_ids || []),
    async (data) => {
      data.skill_ids = selectedSkills();
      await saveResource("roles", item, data);
      closeSheet();
      await refreshCatalog();
      notify("角色已保存");
      await render();
    },
  );
}
function skillForm(item) {
  form(
    item ? "编辑技能" : "创建技能",
    field("name", "技能名称", item?.name || "", "text", { required: true }) +
      field("description", "适用场景", item?.description || "", "textarea", {
        rows: 3,
      }) +
      field("instructions", "技能指令", item?.instructions || "", "textarea", {
        required: true,
        rows: 12,
        help: "填写可直接用于任务的操作规范和验收要求。保存后生成新版本，历史运行继续使用冻结内容。",
      }) +
      field(
        "data_scope",
        "数据范围",
        item?.data_scope || "local_only",
        "select",
        {
          options: [
            ["local_only", "仅本地"],
            ["public", "允许发送到获准的云端模型"],
          ],
        },
      ) +
      field(
        "enabled",
        "允许新运行使用此技能",
        item?.enabled ?? true,
        "checkbox",
      ),
    async (data) => {
      data.enabled = data.enabled === "on";
      await saveResource("skills", item, data);
      closeSheet();
      await refreshCatalog();
      notify("技能版本已保存");
      await render();
    },
  );
}
function templateForm() {
  form(
    "创建 SOP 模板",
    field("name", "模板名称", "", "text", { required: true }) +
      field("description", "适用场景", "", "textarea") +
      field("execution_mode", "流程类型", "sop", "select", {
        options: [
          ["sop", "自定义 Agent 与人工审批流程"],
          ["workflow", "研发交付流程"],
        ],
      }),
    async (data) => {
      data.nodes =
        data.execution_mode === "workflow"
          ? state.templates.find((t) => t.execution_mode === "workflow")
              ?.nodes
          : [
              {
                node_id: "analyze",
                label: "需求分析",
                kind: "model",
                stage: "requirements",
                dependencies: [],
                role_id:
                  state.roles.find((r) => r.stage === "requirements")?.id || "",
              },
              {
                node_id: "approve",
                label: "负责人审批",
                kind: "gate",
                stage: "review",
                dependencies: ["analyze"],
              },
            ];
      const created = await api("templates", "POST", {...data, workspace_id: state.workspaceId});
      closeSheet();
      await refreshCatalog();
      state.designer = null;
      go("templates", created.id);
    },
  );
}
function decimalMicros(value, label = "金额") {
  const normalized = String(value).trim();
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(normalized))
    throw Error(`${label}请填写非负金额，最多六位小数`);
  const [whole, fraction = ""] = normalized.split(".");
  const result = BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0"));
  if (result > BigInt(Number.MAX_SAFE_INTEGER)) throw Error(`${label}超出可接受范围`);
  return Number(result);
}
function decimalPrice(value) {
  if (value === undefined || value === null) return "";
  const integer = BigInt(value);
  return `${integer / 1000000n}.${String(integer % 1000000n).padStart(6, "0")}`.replace(/\.?0+$/, "");
}
function modelSelect(selected = "") {
  return field("endpoint_id", "执行模型", selected, "select", {options: [["", "自动选择已配置模型"], ...availableModels().map((model) => [model.endpoint_id, model.endpoint_id])]});
}
function endpointForm(item) {
  const model = item || {};
  const currency = model.currency || "";
  form(
    item ? "配置模型" : "添加自己的模型",
    `<p class="field-help">使用你已接入网关的模型，填写供应商价格。模型密钥由网关保管。</p><div class="field-grid">${field("endpoint_id", "模型名称 / ID", model.endpoint_id || "", "text", {required:true, help:"用于任务选择，字母、数字、短横线或下划线"})}${field("model_identity", "底层模型名称", model.model_identity || "", "text", {required:true})}${field("gateway_id", "已配置网关 ID", model.gateway_id || "", "text", {required:true, help:"填写服务端配置 gateways 中的 ID"})}${field("endpoint_alias", "网关内模型名称", model.endpoint_alias || "", "text", {help:"留空时与模型 ID 相同"})}</div>${field("gateway_etag", "网关配置版本 ETag", model.gateway_etag || "", "text", {required:true, help:'网关 GET /v1/models 响应头中的 ETag，保留双引号'})}<h3 class="form-section">模型计价</h3><div class="field-grid">${field("currency", "计价币种", currency, "text", {required:true, placeholder:"如 CNY 或 USD"})}${field("location", "模型位置", model.location || "local", "select", {options:[["local","本地"],["cloud","云端服务"]]})}${field("input_price", "输入价格 / 百万 token", decimalPrice(model.input_micros_per_million), "text", {required:true, placeholder:"填写实际价格，免费请明确填 0"})}${field("output_price", "输出价格 / 百万 token", decimalPrice(model.output_micros_per_million), "text", {required:true, placeholder:"填写实际价格，免费请明确填 0"})}</div><p class="field-help">成本 = 实际输入 token × 输入单价 + 实际输出 token × 输出单价。没有用量回执时标为待核对。</p>${field("assign_workspace", "用于当前工作空间 · " + (state.workspace?.name || state.workspaceId), !item || (state.workspaces.find((workspace) => workspace.id === state.workspaceId)?.endpoint_ids || []).includes(model.endpoint_id), "checkbox")}<details class="optional-panel"><summary>高级模型设置</summary><div class="field-grid">${field("context_tokens", "上下文 token 上限", model.context_tokens || 32000, "number", {required:true,min:1})} ${field("endpoint_token_limit", "端点输出 token 上限", model.max_output_tokens ?? "", "number", {min:1,max:1048576,help:"可填写网关模型列表中的 max_output_tokens，用于准确估算预算。"})}${field("priority", "路由优先级", model.priority ?? 1, "number", {min:0,required:true})}</div>${field("capabilities", "能力标识", (model.capabilities || []).join(", "), "text", {help:"可选，以逗号分隔"})}${field("identity_verified", "已核实底层模型身份", model.identity_verified || false, "checkbox")}${field("enabled", "启用模型", model.enabled !== false, "checkbox")}${field("allowed", "允许派发", model.allowed !== false, "checkbox")}<h3 class="form-section">任务适配等级</h3><div class="field-grid">${Object.entries(stageLabels).filter(([key]) => key !== "auto").map(([key,label]) => field("fit_"+key,label,model.fit?.[key] || 3,"number",{min:1,max:5,required:true})).join("")}</div></details>`,
    async (data) => {
      const body = {
        endpoint_id: data.endpoint_id, model_identity: data.model_identity,
        gateway_id: data.gateway_id, endpoint_alias: data.endpoint_alias || data.endpoint_id,
        gateway_etag: data.gateway_etag, location: data.location,
        max_output_tokens: data.endpoint_token_limit ? Number(data.endpoint_token_limit) : null,
        context_tokens: Number(data.context_tokens), identity_verified: data.identity_verified === "on",
        enabled: data.enabled === "on", allowed: data.allowed === "on",
        capabilities: data.capabilities.split(",").map((value) => value.trim()).filter(Boolean),
        currency: data.currency.trim().toUpperCase(), priority: Number(data.priority),
        input_micros_per_million: decimalMicros(data.input_price, "输入价格"),
        output_micros_per_million: decimalMicros(data.output_price, "输出价格"),
        fit: Object.fromEntries(Object.keys(stageLabels).filter((key) => key !== "auto").map((key) => [key,Number(data["fit_"+key])])),
      };
      const saved = await api("endpoints"+(item ? "/"+item.endpoint_id : ""),item ? "PATCH" : "POST",body,item?.revision);
      let assignmentError = "";
      try {
        const workspace = await api("workspaces/"+state.workspaceId);
        const assigned = workspace.endpoint_ids.includes(saved.endpoint_id);
        const requested = data.assign_workspace === "on";
        if (assigned !== requested) {
          const endpoint_ids = requested ? [...workspace.endpoint_ids,saved.endpoint_id] : workspace.endpoint_ids.filter((identifier) => identifier !== saved.endpoint_id);
          await api("workspaces/"+workspace.id,"PATCH",{endpoint_ids},workspace.revision);
        }
      } catch (error) { assignmentError = error.message; }
      closeSheet();
      await refreshCatalog();
      notify(assignmentError ? "模型已保存，但当前工作空间分配未完成："+assignmentError : "模型配置已保存",Boolean(assignmentError));
      await render();
    },
    "保存模型", true,
  );
  if (item) $("[name=endpoint_id]",$("#sheet")).readOnly = true;
}
