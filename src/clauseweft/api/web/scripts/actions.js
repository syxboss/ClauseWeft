const actions = {
  "copy-answer": copyAnswer,
  "download-answer": downloadAnswer,
  "new-workspace": () => workspaceForm(),
  "edit-workspace": (id) => workspaceForm(state.workspaces.find((item) => item.id === id)),
  "mobile-menu": () => setMobileNav(!$("#sidebar").classList.contains("open")),
  "close-sheet": closeSheet,
  refresh: async () => {
    await refreshCatalog();
    await render();
  },
  "new-skill": () => skillForm(),
  "edit-skill": (id) => skillForm(state.skills.find((item) => item.id === id)),
  "view-skill": (id) => {
    const item = state.skills.find((item) => item.id === id);
    sheet(
      item.name,
      `<div class="actions"><span class="pill">v${item.revision}</span>${badge(item.enabled ? "active" : "disabled")}<span class="muted">${item.data_scope === "local_only" ? "仅本地处理" : "允许发送到获准云端模型"}</span></div><p class="description">${esc(item.description)}</p><pre>${esc(item.instructions)}</pre><footer class="form-actions">${button("resource-history", "版本与审计", "skills/" + id)}</footer>`,
      true,
    );
  },
  "resource-history": async (reference) => {
    const items = await collection(reference + "/history");
    sheet(
      "资源版本历史",
      items.length
        ? items
            .map(
              (item) =>
                `<section class="artifact-section"><h3>版本 ${esc(item.revision)}</h3><p class="muted">${date(item.at)} · ${esc(item.source || "平台记录")}</p><dl><div class="key-value"><dt>内容摘要</dt><dd class="mono">${esc(item.sha256 || "—")}</dd></div><div class="key-value"><dt>审计序号</dt><dd>${esc(item.audit_seq ?? "—")}</dd></div></dl><details><summary>查看此版本的脱敏内容</summary><pre>${esc(pretty(item.payload))}</pre></details></section>`,
            )
            .join("")
        : empty("暂无版本记录", "资源保存后会保留不可覆盖的版本快照。"),
      true,
    );
  },
  "create-checkpoint": () => {
    const source = state.currentRun;
    reasonForm(
      "保存运行检查点",
      async (data) => {
        const run = await api("runs/" + source.run_id);
        await api(
          "runs/" + source.run_id + "/checkpoints",
          "POST",
          data,
          run.revision,
        );
        closeSheet();
        notify("检查点已保存");
        await render();
      },
      field("label", "检查点名称", "", "text", {
        required: true,
        placeholder: "例如：需求评审完成，等待负责人批准",
      }),
    );
  },
  "checkpoint-detail": (id) => {
    const item = state.checkpoints.find((item) => item.id === id);
    sheet(
      "检查点快照 · " + item.label,
      `<dl>${[
        ["来源运行", item.source_run_id],
        ["检查点 ID", item.id],
        ["运行版本", item.run_revision],
        ["保存时间", date(item.created_at)],
        ["保存人", item.created_by],
      ]
        .map(
          ([label, value]) =>
            `<div class="key-value"><dt>${label}</dt><dd class="mono">${esc(value ?? "—")}</dd></div>`,
        )
        .join("")}</dl><p class="description">${esc(item.reason)}</p>${table(
        ["节点", "状态", "世代", "产物摘要"],
        (item.node_states || []).map(
          (node) =>
            `<tr><td>${esc(node.node_id)}</td><td>${badge(node.state)}</td><td>${esc(node.generation)}</td><td class="mono audit-resource">${esc(node.artifact || "—")}</td></tr>`,
        ),
      )}`,
      true,
    );
  },
  "fork-checkpoint": (id) => {
    const checkpoint = state.checkpoints.find((item) => item.id === id);
    const source = state.currentRun;
    reasonForm(
      "从检查点派生任务",
      async (data) => {
        const run = await api("runs/" + source.run_id);
        const created = await api(
          "runs/" + source.run_id + "/forks",
          "POST",
          {
            checkpoint_id: id,
            title: data.title,
            reason: data.reason,
            budget: {
              currency: data.currency,
              limit_micros: decimalMicros(data.budget,"费用上限"),
            },
          },
          run.revision,
        );
        closeSheet();
        state.runTab = "pipeline";
        go("runs", created.run_id);
        notify("已创建具有独立预算的派生任务");
      },
      `<p class="description">从“${esc(checkpoint.label)}”继续。有效的已完成模型结果可复用，人工关卡与相关下游重新处理。</p>${field("title", "新任务名称", source.title + " · 派生", "text", { required: true })}<div class="field-grid">${field("budget", "新任务预算", Math.max(0, source.budget.limit_micros / 1000000), "number", { min: 0, step: ".001", required: true })}${field("currency", "币种", source.budget.currency, "text", { required: true })}</div>`,
    );
  },
  "audit-detail": (id) => {
    const item = state.auditItems.find(
      (item) => String(item.seq ?? item.id) === id,
    );
    sheet("审计详情 · #" + id, auditDetail(item), true);
  },
  "verify-audit": async () => {
    state.auditVerification = await api("audit/verify");
    await render();
    notify(
      state.auditVerification.valid
        ? "审计链校验通过"
        : "审计链存在不一致，请查看校验依据",
      !state.auditVerification.valid,
    );
  },
  "audit-verification-detail": () => {
    const verification = state.auditVerification;
    sheet(
      "审计链校验依据",
      `<p class="description">锚点标记本次校验的链尾。请将导出的锚点保存在独立位置，用于后续比对。</p><pre>${esc(pretty(verification))}</pre>`,
      true,
    );
  },
  "export-audit": async () => {
    const query = new URLSearchParams(state.auditFilters);
    if (state.project) query.set("project_id", state.project);
    const response = await fetch(
      "/api/v1/audit/exports" + (query.size ? "?" + query : ""),
      { headers: { Authorization: "Bearer " + state.token } },
    );
    if (!response.ok) {
      const body = await response.json();
      throw Error(
        `${response.status} · ${body.error?.details || body.error?.code || "导出失败"}`,
      );
    }
    // Keep the server's exact JSON bytes, including numeric representations in evidence.
    downloadBlob(
      await response.blob(),
      "clauseweft-audit-" + new Date().toISOString().slice(0, 10) + ".json",
    );
    notify("完整审计链、筛选范围与校验锚点已导出");
  },
  "clear-audit-filters": async () => {
    state.auditFilters = {};
    state.auditCursor = 0;
    await render();
  },
  "audit-previous": async () => {
    state.auditCursor = Math.max(0, state.auditCursor - 100);
    await render();
  },
  "audit-next": async () => {
    state.auditCursor = Number(state.auditNextCursor);
    await render();
  },
  "login-mode": (id) => {
    state.loginMode = id;
    $("#account-fields").hidden = id !== "account";
    $("#token-fields").hidden = id === "account";
    $("#username").required = $("#password").required = id === "account";
    $("#token").required = id !== "account";
    $("#login-error").textContent = "";
    $(".entry-intro").textContent = {pair: "首次使用，请输入启动时取得的本机访问令牌。", account: "使用已有成员账号登录。", token: "使用本机访问令牌管理实例。"}[id];
    $(".entry-help").textContent = {pair: "配对后可在此浏览器使用 12 小时；退出登录可提前结束会话。", account: "登录会话有效期为 12 小时。", token: "管理员入口用于实例配置和账号恢复。"}[id];
    $$("#login-tabs button").forEach(b => {
      b.classList.toggle("active", b.dataset.id === id);
      b.setAttribute("aria-pressed", String(b.dataset.id === id));
    });
  },
  "personal-preferences": editPersonalPreferences,
  "remember-point": rememberPoint,
  "runs-page": async (cursor) => {
    state.runsCursor = Number(cursor);
    await render();
  },
  "clear-preferences": () => {
    const input = $("#sheet-form [name=instructions]");
    input.value = "";
    input.focus();
  },
  account: () =>
    sheet(
      "当前账号",
      `<div class="key-value"><span>用户名</span><strong>${esc(state.user.username)}</strong></div><div class="key-value"><span>权限</span><strong>${esc(roleLabels[state.user.role])}</strong></div><div class="form-actions">${state.user.id === "bootstrap-admin" ? button("pair-owner", "配对个人工作台") : state.workspaceId ? button("personal-preferences", "我的偏好") : ""}${button("logout", "退出登录", "", "danger")}</div>`,
    ),
  "pair-owner": async () => {
    const session = await api("auth/pair", "POST", {});
    state.token = session.token;
    await connect(session.expires_at);
  },
  logout: async () => {
    try {
      await api("auth/logout", "POST", {});
    } finally {
      disconnect();
    }
  },
  "configure-models": () => { if ($("#sheet").open) closeSheet(); go("endpoints"); },
  "new-project": () => projectForm(),
  "edit-project": (id) => projectForm(state.projects.find((p) => p.id === id)),
  "open-project": openProject,
  "new-requirement": () => requirementForm(),
  "edit-requirement": requirementForm,
  "view-requirement": requirementForm,
  "requirement-run": async (id) =>
    newRun({ requirement: await api("requirements/" + id) }),
  "new-run": () => newRun(),
  "new-tool-run": (id) => {
    if (!viewingRun(id) || state.currentRun?.run_id !== id) return;
    return newRun({project_id:state.currentRun.project_id, template_id:"__tool__"});
  },
  "continue-answer": (action) => continueAnswer(action),
  "open-run": (id) => {
    if ($("#sheet").open) closeSheet();
    state.runTab = "pipeline";
    go("runs", id);
  },
  "back-runs": () => go("runs"),
  "run-audit": (id) => {
    state.auditFilters = { run_id: id };
    go("audit");
  },
  "run-tab": async (tab) => {
    state.runTab = tab;
    await render();
  },
  "pause-run": () => runAction("pause", {}),
  "resume-run": () => runAction("resume", {}),
  "cancel-run": () =>
    form(
      "取消任务",
      '<p class="description">取消会阻止新的模型派发。已发出请求的费用仍保留为可核对记录。</p>',
      () => runAction("cancel", {}),
      "确认取消",
    ),
  artifact: async (id) =>
    sheet(
      "执行产物",
      renderArtifact(
        (await api("runs/" + state.currentRun.run_id + "/artifacts/" + id))
          .content,
      ),
      true,
    ),
  "show-node": async (id) => {
    const run = state.currentRun;
    const n = run.nodes.find((node) => node.node_id === id);
    const upstream = n.dependencies.map((key) => run.nodes.find((node) => node.node_id === key)?.label || key);
    const details = [["世代",n.generation],["执行端点",n.endpoint_id],["依赖",upstream.join("、")],
      ["本次尝试",n.attempt_id],["节点说明",incompleteOutput[n.reason] || n.reason]];
    const result = n.artifact ? await api("runs/" + run.run_id + "/artifacts/" + n.artifact) : null;
    sheet(
      n.label || n.node_id,
      `<article class="node-inspection">${badge(n.state)}${n.brief ? `<p class="record-answer">${esc(n.brief)}</p>` : ""}
      <dl class="record-facts">${details.filter(([,v]) => v).map(([key,val]) => `<div><dt>${key}</dt><dd>${esc(val)}</dd></div>`).join("")}</dl>
      ${n.attempt_id && !n.inherited_from ? button("attempt-detail","检查路由与回执",n.attempt_id) : ""}
      ${result ? `<h3>节点产物</h3>${renderArtifact(result.content)}` : ""}
      ${n.kind === "gate" && n.state === "needs_input" ? `<footer class="form-actions">${button("run-action", "处理审批", id, "primary")}</footer>` : ""}</article>`,
      true,
    );
  },
  "run-action": async (id) => {
    const run = state.currentRun;
    if (!run || !viewingRun(run.run_id)) throw Error("当前任务已切换，请重新查看待办");
    if (run.mode === "tool") {
      if (id !== run.tool_proposal?.gate_id) throw Error("此步骤已更新，请刷新查看待确认的提议");
      return reviewToolCall(run);
    }
    if (id === "approve-spec" && run.mode === "workflow")
      return approveBaseline();
    if (id === "integrate" && run.mode === "workflow")
      return actions.integrate();
    if (id === "execute-tests" && run.mode === "workflow")
      return actions["execute-tests"]();
    if (id === "accept" && run.mode === "workflow")
      return actions["approve-delivery"]();
    const n = run.nodes.find((n) => n.node_id === id);
    if (n?.kind === "gate")
      reasonForm(
        "处理审批 · " + (n.label || id),
        (data) =>
          runAction("gates/" + id, {
            action: data.action,
            reason: data.reason,
          }),
        field("action", "处理决定", "approve", "select", {
          options: [
            ["approve", "批准并继续"],
            ["reject", "退回上游重新执行"],
          ],
        }),
      );
    else if (
      run.waiting_actions.find((w) => w.node_id === id)?.action.includes("修订")
    )
      reasonForm("重新执行评审", () =>
        runAction("retry-review", { node_id: id }),
      );
    else if (n?.state === "uncertain") {
      state.runTab = "attempts";
      await render();
    } else reasonForm("恢复节点", () => runAction("resume", {}));
  },
  "approve-baseline": approveBaseline,
  "revise-spec": reviseSpec,
  "add-clause": () => {
    const host = $("#clause-fields");
    const i = $$("section", host).length;
    host.insertAdjacentHTML(
      "beforeend",
      `<section class="artifact-section">${field("id_" + i, "条款 ID", "", "text", { required: true })}${field("text_" + i, "行为描述", "", "textarea", { required: true })}${field("acceptance_" + i, "验收条件", "", "textarea", { required: true })}</section>`,
    );
  },
  "resolve-finding": (id) => {
    const finding = state.findings.find(item => item.id === id);
    if (!finding || finding.target_status !== "open" || finding.resolution) throw Error("请刷新并选择当前待处置意见");
    const related = state.findings.filter(item => item.target === finding.target && item.id !== id);
    const choices = [["fix", "已修复"], ["accept", "接受风险"], ["reject", "驳回意见"]];
    if (related.length) choices.push(["duplicate", "合并重复意见"]);
    reasonForm(
      "记录评审处置",
      data => {
        const relatedIds = data.action === "duplicate" ? new FormData($("#sheet-form")).getAll("related_ids") : [];
        if (data.action === "duplicate" && !relatedIds.length) throw Error("请选择至少一条关联意见");
        return runAction("findings/" + id + "/resolutions", {
          action: data.action, reason: data.reason,
          ...(data.evidence.trim() ? {evidence: data.evidence.trim()} : {}),
          related_ids: relatedIds,
        });
      },
      field("action", "处置方式", "fix", "select", {options: choices}) +
        field("evidence", "复核证据", "", "textarea", {required: finding.severity === "blocker"}) +
        `<fieldset id="related-findings" class="grant-selection" hidden><legend>关联的原始意见</legend>${related.map(item =>
          `<label class="check"><input type="checkbox" name="related_ids" value="${esc(item.id)}"><span>${esc(item.location)} · ${esc(item.branch)} · ${esc(item.example || item.suggestion)} <code title="${esc(item.id)}">${esc(item.id.slice(0, 12))}</code></span></label>`).join("")}</fieldset>`,
    );
    $("#sheet-form [name=action]").addEventListener("change", event => {
      $("#related-findings").hidden = event.target.value !== "duplicate";
    });
  },
  integrate: () =>
    reasonForm(
      "整合工作包",
      (data) =>
        runAction("integrations", {
          selected: data.selected
            .split(",")
            .map((v) => v.trim())
            .filter(Boolean),
          reason: data.reason,
          ...(data.resolution_patch
            ? { resolution_patch: data.resolution_patch }
            : {}),
        }),
      field("selected", "选用工作包", "code,test-design", "select", {
        options: [
          ["code,test-design", "实现 + 测试设计"],
          ["code", "仅实现工作包"],
          ["test-design", "仅测试设计工作包"],
        ],
      }) +
        field("resolution_patch", "冲突解决补丁（可选）", "", "textarea", {
          rows: 8,
        }),
    ),
  rework: () =>
    reasonForm(
      "退回工作包",
      (data) =>
        runAction("rework", { node_id: data.node_id, reason: data.reason }),
      field("node_id", "退回对象", "code", "select", {
        options: [
          ["code", "代码实现"],
          ["test-design", "测试设计"],
        ],
      }),
    ),
  "execute-tests": () =>
    reasonForm(
      "执行冻结验收套件",
      (data) =>
        runAction("tests", {
          candidate_sha: state.currentRun.candidate_sha,
          ...(data.suite_id ? { suite_id: data.suite_id } : {}),
        }),
      field("suite_id", "验收套件", state.currentRun.suite_id || "", "select", {
        options: [
          ["", "使用当前套件"],
          ...state.workspace.suites.map((id) => [id, id]),
        ],
      }),
    ),
  "approve-delivery": () =>
    reasonForm("接纳交付", (data) =>
      runAction("approvals", {
        kind: "delivery",
        version: state.currentRun.spec_version_id,
        candidate_sha: state.currentRun.candidate_sha,
        reason: data.reason,
      }),
    ),
  "export-run": async (id) => {
    const run = await api("runs/" + id);
    const data = await api("runs/" + id + "/exports", "POST", {}, run.revision);
    const url = URL.createObjectURL(
      new Blob([pretty(data)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "clauseweft-" + id.slice(0, 10) + ".json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("交付清单已导出");
  },
  "attempt-detail": async (id) => {
    const run = state.currentRun;
    const attempts = await collection("runs/" + run.run_id + "/attempts");
    const a = attempts.find((attempt) => attempt.attempt_id === id);
    if (!a) throw Error("此尝试未出现在当前运行记录中。");
    const node = run.nodes.find((n) => n.node_id === a.node_id);
    const fields = [["节点",node?.label || a.node_id],["世代",a.generation],["调用标识",a.attempt_id],
      ["执行端点",a.endpoint_id],["模型身份",a.model_identity],["领取时间",date(a.started)],
      ["完成时间",a.finished ? date(a.finished) : null],["回执",a.receipt],
      ["网关实际输出上限",a.completion_metadata?.max_output_tokens],["终止原因",a.completion_metadata?.finish_reason],
      ["原因",incompleteOutput[a.error] || a.error]];
    sheet(
      "路由与调用回执",
      `<article class="node-inspection">${badge(a.state)}<dl class="record-facts">
        ${fields.filter(([,v]) => v).map(([key,val]) => `<div><dt>${key}</dt><dd>${esc(val)}</dd></div>`).join("")}</dl>
        ${a.error === "output_length_limit" && a.artifact ? button("artifact","查看截断的答复",a.artifact) : ""}
        ${a.decision ? `<details class="record-subdetails"><summary>实际派发依据</summary><pre>${esc(pretty(a.decision))}</pre></details>` : ""}
        <details class="record-subdetails"><summary>完整调用记录</summary><pre>${esc(pretty(a))}</pre></details></article>`,
      true,
    );
  },
  reconcile: (id) =>
    reasonForm(
      "登记实际账单",
      (data) =>
        runAction("reconciliations", {
          attempt_id: id,
          actual_micros: Math.round(Number(data.actual) * 1000000),
          evidence: data.reason,
        }),
      field("actual", "实际费用", 0, "number", {
        min: 0,
        step: ".000001",
        required: true,
      }),
    ),
  "template-history": async (id) => {
    const versions = await collection("templates/" + id + "/versions");
    state.templateVersions = versions;
    sheet(
      "模板发布历史",
      versions.length
        ? table(
            ["版本", "发布时间", "发布人", "节点", "查看"],
            versions.map(
              (v) =>
                `<tr><td>v${v.revision}</td><td>${date(v.published_at)}</td><td>${esc(v.actor)}</td><td>${v.node_count}</td><td>${button("version-detail", "冻结配置", String(v.revision), "text-button")}</td></tr>`,
            ),
          )
        : empty("尚未发布", "保存并发布后会保留每个版本的节点与角色配置。"),
      true,
    );
  },
  "version-detail": (rev) => {
    const version = state.templateVersions.find(
      (v) => String(v.revision) === rev,
    );
    sheet("冻结的模板版本 v" + rev, renderArtifact(version), true);
  },
  "new-template": templateForm,
  "open-template": (id) => {
    state.designer = null;
    state.node = null;
    go("templates", id);
  },
  "back-templates": () => {
    state.designer = null;
    go("templates");
  },
  "run-template": (id) => newRun({ template_id: id }),
  "select-node": async (id) => {
    state.node = id;
    await render(true);
  },
  "add-node": () =>
    form(
      "添加流程节点",
      field("node_id", "唯一节点 ID", "", "text", { required: true }) +
        field("label", "节点名称", "", "text", { required: true }) +
        field("kind", "节点类型", "model", "select", {
          options: [
            ["model", "Agent 节点"],
            ["gate", "人工审批"],
          ],
        }),
      async (data) => {
        if (
          !/^[A-Za-z0-9_-]{1,80}$/.test(data.node_id) ||
          state.designer.nodes.some((n) => n.node_id === data.node_id)
        )
          throw Error("节点 ID 必须唯一，使用字母、数字、短横线或下划线");
        state.designer.nodes.push({
          ...data,
          stage: "review",
          dependencies: [],
        });
        state.node = data.node_id;
        closeSheet();
        await render(true);
      },
    ),
  "remove-node": async (id) => {
    state.designer.nodes = state.designer.nodes
      .filter((n) => n.node_id !== id)
      .map((n) => ({
        ...n,
        dependencies: n.dependencies.filter((d) => d !== id),
      }));
    state.node = null;
    await render(true);
  },
  "save-template": async () => {
    const t = state.designer;
    state.designer = await api(
      "templates/" + t.id,
      "PATCH",
      {
        name: t.name,
        description: t.description,
        execution_mode: t.execution_mode,
        nodes: t.nodes,
      },
      t.revision,
    );
    await refreshCatalog();
    notify("模板草稿已保存");
    await render(true);
  },
  "publish-template": async () => {
    await actions["save-template"]();
    const t = state.designer;
    await api("templates/" + t.id + "/publish", "POST", {}, t.revision);
    state.designer = await api("templates/" + t.id);
    await refreshCatalog();
    notify("模板版本已发布，新任务将使用此配置");
    await render(true);
  },
  "edit-template-meta": () =>
    form(
      "模板设置",
      field("name", "模板名称", state.designer.name, "text", {
        required: true,
      }) +
        field(
          "description",
          "适用场景",
          state.designer.description,
          "textarea",
        ),
      async (data) => {
        Object.assign(state.designer, data);
        closeSheet();
        await render(true);
      },
    ),
  "new-role": () => roleForm(),
  "edit-role": (id) => roleForm(state.roles.find((r) => r.id === id)),
  "new-endpoint": () => endpointForm(),
  "edit-endpoint": (id) =>
    endpointForm(state.endpoints.find((m) => m.endpoint_id === id)),
  "toggle-endpoint": async (id) => {
    const m = state.endpoints.find((m) => m.endpoint_id === id);
    await api("endpoints/" + id, "PATCH", { enabled: !m.enabled }, m.revision);
    await refreshCatalog();
    notify(m.enabled ? "模型已停用" : "模型已启用");
    await render();
  },
  "probe-endpoint": async (id) => {
    const m = state.endpoints.find((m) => m.endpoint_id === id);
    const result = await api(
      "endpoints/" + id + "/probes",
      "POST",
      {},
      m.revision,
    );
    sheet(
      "连接探测结果",
      `<div class="actions">${badge(result.connected && result.etag_matches ? "completed" : "blocked")}</div><dl>${[
        ["端点", id],
        ["网关连接", result.connected ? "已连接" : "连接失败"],
        ["模型别名", result.alias_present ? "存在" : "缺失"],
        ["配置版本", result.etag_matches ? "一致" : "需要更新 ETag"],
      ]
        .map(
          ([k, v]) =>
            `<div class="key-value"><dt>${k}</dt><dd>${esc(v)}</dd></div>`,
        )
        .join(
          "",
        )}</dl><footer class="form-actions">${availableModels().some(model => model.endpoint_id === id)
          ? (state.projects.some(project => project.status === "active") ? button("capability-probe", "验证结构化输出", id)
            : '<span class="field-help">请先在当前工作空间创建一个进行中的项目。</span>')
          : '<span class="field-help">请先启用模型、允许调用，并授权给当前工作空间。</span>'}</footer>`,
    );
  },
  "capability-probe": (id) => {
    const projects = state.projects.filter(project => project.status === "active");
    const selected = projects.find(project => project.id === state.project) || (projects.length === 1 ? projects[0] : null);
    const workspaceId = state.workspaceId;
    const m = state.endpoints.find(model => model.endpoint_id === id);
    form(
      "验证结构化输出",
      field("project_id", "验证运行所属项目", selected?.id || "", "select", {
        required: true, options: [["", "选择项目"], ...projects.map(project => [project.id, project.name])],
      }) + field("budget", "本次探测费用上限 · " + m.currency, "", "number", {
        min: 0,
        step: ".001",
        required: true,
      }),
      async (data) => {
        const run = await api(
          "endpoints/" + id + "/probes",
          "POST",
          {
            kind: "structured_output",
            workspace_id: workspaceId,
            project_id: data.project_id,
            budget: {
              currency: m.currency,
              limit_micros: decimalMicros(data.budget,"费用上限"),
            },
          },
          m.revision,
        );
        closeSheet();
        go("runs", run.run_id);
      },
    );
  },
  "endpoint-history": async (id) => {
    const records = await collection("endpoints/" + id + "/probes");
    sheet(
      "模型探测历史",
      records.length
        ? table(
            ["时间", "状态", "验证", "目录版本"],
            records.map(
              (r) =>
                `<tr><td>${date(r.created, true)}</td><td>${badge(r.status)}</td><td>${r.verified ? "已验证" : "尚未验证"}</td><td>${r.current ? "当前版本" : "历史版本"}</td></tr>`,
            ),
          )
        : empty(
            "暂无探测记录",
            "运行结构化输出验证后，会保留与目录版本绑定的结果。",
          ),
      true,
    );
  },
  "preview-plan": async () => {
    try {
      const data = Object.fromEntries(new FormData($("#sheet-form")));
      data.skill_ids = selectedSkills();
      data.materials = collectMaterials($("#sheet-form"));
      const result = await api("routing/previews", "POST", makePlan(data));
      $("#preview-error").textContent = "";
      $("#preview-result").innerHTML =
        `<div class="block"><div class="block-heading"><h3>${result.selected_endpoint ? "选中 " + esc(result.selected_endpoint) : esc(states[result.status] || result.status)}</h3></div><div class="block-body">${result.note ? `<p class="field-help">${esc(result.note)}</p>` : ""}${result.budget ? `<p class="field-help">本次总费用上限 ${amount(result.budget.limit_micros,result.budget.currency)}，实际成本按模型回执统计。</p>` : ""}${(result.candidates || []).map((c) => `<div class="cost-row"><span>${esc(c.endpoint_id)}<small> · ${c.location === "local" ? "本地" : "云端"}</small></span><span>${c.reasons.length ? esc(c.reasons.join("、")) : amount(c.estimated_micros,result.budget?.currency || modelCurrency(result.selected_endpoint))}</span></div>`).join("")}</div></div>`;
    } catch (error) {
      $("#preview-error").textContent = error.message;
    }
  },
  "edit-member": (id) => {
    const member = state.members.find((m) => m.id === id);
    form(
      "管理团队成员",
      field("display_name", "显示名称", member.display_name, "text", {
        required: true,
      }) +
        field("role", "权限角色", member.role, "select", {
          options: Object.entries(roleLabels),
        }) +
        field("active", "启用成员账号", member.active, "checkbox") +
        field("password", "新密码（留空保留当前密码）", "", "password", {
          help: "设置时至少 12 个字符，变更后当前会话失效",
        }) + memberGrants(member),
      async (data) => {
        const body = {
          display_name: data.display_name,
          role: data.role,
          active: data.active === "on",
          ...selectedMemberGrants(),
        };
        if (data.password) body.password = data.password;
        await api("members/" + id, "PATCH", body, member.revision);
        closeSheet();
        notify("成员设置已保存");
        await render();
      },
    );
  },
  "new-member": () =>
    form(
      "添加团队成员",
      field("username", "用户名", "", "text", { required: true }) +
        field("display_name", "显示名称", "", "text", { required: true }) +
        field("role", "权限角色", "operator", "select", {
          options: [
            ["operator", "执行者 · 管理研发任务与资源"],
            ["viewer", "只读成员 · 查看项目与产物"],
            ["admin", "管理员 · 管理账号与平台"],
          ],
        }) +
        field("password", "初始密码", "", "password", {
          required: true,
          help: "至少 12 个字符，交付给成员后由管理员管理",
        }) + memberGrants(),
      async (data) => {
        const {workspace_ids, project_ids} = selectedMemberGrants();
        await api("members", "POST", {
          username: data.username, display_name: data.display_name,
          role: data.role, password: data.password, workspace_ids, project_ids,
        });
        closeSheet();
        notify("团队成员已创建");
        await render();
      },
    ),
};
