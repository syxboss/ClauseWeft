const runsPageSize = 30;
async function runs() {
  const cursor = state.runsCursor;
  const query = new URLSearchParams(projectQuery());
  query.set("limit", String(runsPageSize));
  query.set("cursor", String(cursor));
  if (state.status) query.set("status", state.status);
  if (state.query.trim()) query.set("q", state.query.trim());
  const {items, next_cursor} = await api("runs?" + query);
  const statuses = ["queued", "running", "paused", "needs_input", "completed", "cancelled"];
  const scope = [state.workspace?.name || state.workspaceId, state.project ? projectName(state.project) : "全部授权项目"].join(" / ");
  return `<div class="desk desk-directory">
    ${deskMasthead("任务记录", scope, button("new-run", "新建任务", "", "primary"))}
    <section class="desk-section"><header><h2>任务列表</h2>
      <label class="desk-filter">运行状态<select id="run-status">${options([["", "全部状态"], ...statuses.map((s) => [s, states[s]])], state.status)}</select></label></header>
      ${runTable(items)}${cursor || next_cursor != null ? `<footer class="actions audit-pagination">
        ${cursor ? button("runs-page", "上一页", String(Math.max(0, cursor - runsPageSize))) : ""}
        <span class="muted">第 ${Math.floor(cursor / runsPageSize) + 1} 页 · ${items.length} 条</span>
        ${next_cursor != null ? button("runs-page", "下一页", String(next_cursor)) : ""}</footer>` : ""}</section></div>`;
}
function graph(nodes, selected, editable = false) {
  const byId = new Map(nodes.map((node) => [node.node_id, node]));
  const levels = new Map(), visiting = new Set();
  function depth(id) {
    if (levels.has(id)) return levels.get(id);
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const dependencies = (byId.get(id).dependencies || []).filter((key) => byId.has(key));
    const level = dependencies.length ? Math.max(...dependencies.map(depth)) + 1 : 0;
    visiting.delete(id);
    levels.set(id, level);
    return level;
  }
  const columns = [];
  for (const node of nodes) (columns[depth(node.node_id)] ||= []).push(node);
  return `<div class="flow record-flow" id="flow" aria-label="节点依赖关系">
    <svg class="flow-lines" aria-hidden="true"></svg>
    ${columns.map((column) => `<div class="flow-column">${column.map((node) => {
      const type = node.kind === "gate" ? "人工关卡" : (node.role_name || state.roles.find((r) => r.id === node.role_id)?.name || "模型任务");
      const detail = node.state ? (states[node.state] || node.state) : stageLabels[node.stage];
      return `<button type="button" class="flow-node ${node.node_id === selected ? "selected" : ""}"
        data-action="${editable ? "select-node" : "show-node"}" data-id="${esc(node.node_id)}"
        data-node="${esc(node.node_id)}" data-phase="${esc(node.state || node.kind)}">
        <span class="flow-type">${esc(type)}</span><strong>${esc(node.label || node.node_id)}</strong>
        <small class="flow-state">${esc(detail || "待配置")}</small>${node.group ? `<small>独立模型组 · ${esc(node.group)}</small>` : ""}
        ${node.dependencies?.length ? `<small class="flow-mobile-deps">上游 · ${esc(node.dependencies.map((key) => byId.get(key)?.label || key).join("、"))}</small>` : ""}
      </button>`;
    }).join("")}</div>`).join("")}</div>`;
}
function drawGraph(nodes) {
  requestAnimationFrame(() => {
    const host = $("#flow");
    if (!host) return;
    const svg = $(".flow-lines", host);
    const base = host.getBoundingClientRect();
    const bounds = new Map($$("[data-node]", host).map((el) => [el.dataset.node, el.getBoundingClientRect()]));
    svg.setAttribute("viewBox", `0 0 ${host.scrollWidth} ${host.scrollHeight}`);
    svg.innerHTML = nodes.flatMap((node) => (node.dependencies || []).map((dep) => {
      const from = bounds.get(dep), to = bounds.get(node.node_id);
      if (!from || !to) return "";
      const x1 = from.right - base.left, y1 = from.top + from.height / 2 - base.top;
      const x2 = to.left - base.left, y2 = to.top + to.height / 2 - base.top;
      return `<path data-to-phase="${esc(node.state || node.kind)}" d="M${x1},${y1} H${(x1 + x2) / 2} V${y2} H${x2}"/>`;
    })).join("");
  });
}
function runNodeLedger(run) {
  return `<section class="record-section"><header><h2>阶段记录</h2><span>${run.nodes.length} 个节点</span></header>
    <ol class="record-stages">${run.nodes.map((node) => `<li>
      <span class="record-stage-marker" data-phase="${esc(node.state)}" aria-hidden="true"></span>
      <div class="record-stage-body">${button("show-node", esc(node.label || node.node_id), node.node_id, "record-node-link")}
        ${node.brief ? `<p>${esc(node.brief)}</p>` : ""}
        <small>${esc(node.endpoint_id || (node.kind === "gate" ? "人工关卡" : stageLabels[node.stage] || "模型任务"))}
          ${node.dependencies.length ? " · 上游 " + esc(node.dependencies.join("、")) : ""}</small>
      </div><div class="record-stage-end">${badge(node.state)}
        ${node.attempt_id && !node.inherited_from ? button("attempt-detail","调用记录",node.attempt_id,"text-button") : ""}
        ${node.artifact ? button("artifact", "查看产物", node.artifact, "text-button node-artifact") : ""}
      </div></li>`).join("")}</ol></section>`;
}
function runArtifactLedger(run) {
  const produced = run.nodes.filter((node) => node.artifact);
  const jobs = (run.jobs || []).filter((job) => ["succeeded", "failed", "stale"].includes(job.state));
  if (!produced.length && !jobs.length) return empty("尚无产物", "完成阶段后可在这里检查答复、补丁和测试报告。");
  return `<section class="record-section"><header><h2>产物清单</h2><span>${produced.length + jobs.length} 份</span></header>
    ${produced.map((n) => `<article class="record-line"><div><h3>${esc(n.label || n.node_id)}</h3><code>${esc(n.artifact)}</code></div>
      ${button("artifact", "展开产物", n.artifact)}</article>`).join("")}
    ${jobs.map((j) => `<article class="record-line"><div><h3>${j.kind === "integrate" ? "补丁整合" : "验收执行报告"} ${badge(j.state)}</h3>
      ${j.reason ? `<p>${esc(j.reason)}</p>` : ""}</div>${button("artifact", "查看证据", j.id)}</article>`).join("")}</section>`;
}
function viewingRun(id) {
  const current = route();
  return current.page === "runs" && current.id === encodeURIComponent(id);
}
async function runDetail(id, generation) {
  const run = await api("runs/" + id);
  if (generation !== state.generation || !viewingRun(id)) return "";
  const observed = new Date();
  if (!state.projects.some((project) => project.id === run.project_id))
    throw Error("此任务不属于当前工作空间，请切换至已授权的工作空间");
  const single = run.mode === "single", complex = run.mode === "complex";
  if (complex) {
    for (const node of run.nodes) {
      const index = /^branch-(\d+)$/.exec(node.node_id)?.[1];
      node.label = {plan:"拆分任务", summary:"汇总结果"}[node.node_id] || (index ? "子任务 " + index : node.node_id);
    }
  }
  state.currentRun = run;
  const controls = [];
  if (!["completed","cancelled"].includes(run.status) && run.budget_source !== "no_configured_model") {
    if (["queued","running"].includes(run.status)) controls.push(button("pause-run", "暂停", id));
    const retryable = run.nodes.some(n => {
      if ((n.state === "uncertain" || incompleteOutput[n.reason]) && run.budget?.uncertain_attempts?.includes(n.attempt_id)) return false;
      return n.kind === "model" && ["needs_input","uncertain","failed","blocked"].includes(n.state);
    });
    if (run.status === "paused" || (retryable && ["needs_input","failed","blocked"].includes(run.status)))
      controls.push(button("resume-run", run.status === "paused" ? "继续执行" : "重试", id, "primary"));
    controls.push(button("cancel-run", "取消任务", id, "danger"));
  }
  if (run.mode === "workflow" && run.status === "completed")
    controls.push(button("export-run", "导出交付", id, "primary"));
  controls.push(button("run-audit", "审计证据", id), button("refresh", "刷新"));
  const tabs = single ? [["result","内容与结果"]] : [["pipeline",complex ? "分工与汇总" : "执行流程"]];
  if (!single && !complex) tabs.push(["artifacts",run.mode === "tool" ? "产物与回执" : "产物与差异"]);
  if (!single && !complex && run.mode !== "tool") tabs.push(["review","评审与决议"]);
  if (can("usage")) tabs.push(["attempts","调用与费用"]);
  tabs.push(["context","请求上下文"], ["events","事件记录"], ["checkpoints","检查点"]);
  if (!tabs.some(([key]) => key === state.runTab)) state.runTab = tabs[0][0];
  let content;
  switch (state.runTab) {
    case "result": content = await simpleTaskContent(run); break;
    case "pipeline": {
      const diagram = `<section class="record-section record-topology"><header><h2>执行拓扑</h2><span>${complex ? esc(complexProgress(run)) : esc(run.template_name || "")}</span></header><div class="flow-stage">${graph(run.nodes)}</div></section>`;
      const result = complex ? await complexTaskContent(run) : run.mode === "tool" ? await toolAnswer(run) : "";
      const stages = waitingActions(run) + diagram;
      content = (run.status === "completed" ? result + stages : stages + result) + runNodeLedger(run);
      break;
    }
    case "artifacts": content = runArtifactLedger(run); break;
    case "review": content = await reviewsHTML(run, generation); break;
    case "context": content = await contextHTML(run); break;
    case "checkpoints": content = await checkpointsHTML(run, generation); break;
    case "attempts": {
      const attempts = await collection("runs/" + id + "/attempts");
      content = `<dl class="record-budget">${[["总预算",run.budget.limit_micros],["已结算",run.budget.settled_micros],["预留",run.budget.held_micros],["可用",run.budget.available_micros]]
        .map(([label,val]) => `<div><dt>${label}</dt><dd>${amount(val,run.budget.currency)}</dd></div>`).join("")}</dl>
        <section class="record-section"><header><h2>调用记录</h2><span>${attempts.length} 次尝试</span></header>${attempts.length ? table(
          ["节点 / 尝试","模型","状态","账务","结算","凭证"],
          attempts.map((a) => `<tr><td class="title-cell"><strong>${esc(a.node_id)}</strong><small>${esc(a.attempt_id.slice(0,12))}</small></td><td>${esc(a.endpoint_id || "未派发")}</td><td>${badge(a.state)}</td><td>${esc(({held:"预留",settled:"已结算",uncertain:"待核对"})[a.accounting] || a.accounting || "—")}</td><td>${a.accounting === "settled" ? amount(a.settled,run.budget.currency) : "—"}</td><td>${button("attempt-detail","路由与回执",a.attempt_id,"text-button")}${run.budget.uncertain_attempts.includes(a.attempt_id) ? button("reconcile","对账",a.attempt_id) : ""}</td></tr>`)
        ) : empty("尚无调用记录","节点就绪并获得调度后会登记尝试。")}</section>`;
      break;
    }
    default: {
      const events = (await api("runs/" + id + "/events?limit=500")).items;
      const record = $(".execution-record");
      const openEvents = new Set(record?.dataset.run === id
        ? $$(".record-event[open]", record).map((el) => el.dataset.seq) : []);
      content = `<section class="record-section"><header><h2>事件记录</h2><span>按发生顺序</span></header>${events.map((e) =>
        `<details class="record-event" data-seq="${esc(e.seq)}"${openEvents.has(String(e.seq)) ? " open" : ""}><summary><time>${date(e.created || e.at,true)}</time><strong>${esc(actionLabel("run." + e.kind))}</strong><code>${esc(e.kind)}</code></summary><pre>${esc(pretty(e.payload || e.data || {}))}</pre></details>`
      ).join("") || empty("尚无事件","运行状态变更时会生成事件记录。")}</section>`;
    }
  }
  const finished = run.nodes.filter((n) => n.state === "succeeded").length;
  const executing = run.nodes.filter((n) => ["dispatched","leased"].includes(n.state));
  const waiting = run.nodes.filter((n) => ["ready","waiting"].includes(n.state)).length;
  const blocked = run.nodes.filter((n) => ["needs_input","uncertain","failed","blocked"].includes(n.state)).length;
  const cancelled = run.nodes.filter((n) => n.state === "cancelled").length;
  const modes = {single:"普通任务",complex:"复杂任务",tool:"项目工具",workflow:"研发交付",sop:"SOP 流程"};
  return `<article class="execution-record" data-run="${esc(id)}">
    <header class="record-head"><div class="record-tools">${button("back-runs","← 任务运行","","record-back")}<div class="actions">${controls.join("")}</div></div>
      <div class="record-mast"><div><p class="record-type">${esc(modes[run.mode] || run.mode)} <span>／</span> ${esc(projectName(run.project_id))}</p>
        <h1>${esc(run.title || "任务详情")}</h1><div class="record-meta"><code title="${esc(id)}">${esc(id.slice(0,16))}</code>
        <time>创建于 ${date(run.created)}</time>${run.role_name ? `<span>${esc(run.role_name)} · v${esc(run.role_revision)}</span>` : ""}
        ${run.candidate_sha ? `<code>候选 ${esc(run.candidate_sha.slice(0,10))}</code>` : ""}</div></div>
        <div class="record-meter"><div class="record-sample">${badge(run.status)}<small>读取于 <time datetime="${observed.toISOString()}">${observed.toLocaleTimeString("zh-CN",{hour12:false})}</time></small></div><div class="record-counts">
          <span class="${executing.length ? "has-active" : ""}"><b>${executing.length}</b> 执行中</span><span><b>${waiting}</b> 等待</span><span><b>${finished}</b> 完成</span>${blocked ? `<span class="has-blocked"><b>${blocked}</b> 待处理</span>` : ""}${cancelled ? `<span><b>${cancelled}</b> 已取消</span>` : ""}</div>
          ${executing.length ? `<div class="record-live-nodes">${executing.slice(0,3).map((n) => button("show-node",esc(n.label || n.node_id),n.node_id,"text-button")).join("")}${executing.length > 3 ? `<small>等 ${executing.length} 个节点</small>` : ""}</div>` : ""}
        </div>
      </div></header>
    <nav class="record-nav" aria-label="任务详情">${tabs.map(([key,label]) => `<button type="button" data-action="run-tab" data-id="${key}" ${state.runTab === key ? 'aria-current="page"' : ""}>${label}</button>`).join("")}</nav>
    <div class="record-view">${content}</div></article>`;
}
async function toolAnswer(run) {
  const gates = run.nodes.filter(node => node.kind === "gate" && node.state === "succeeded" && node.artifact);
  const receipts = await Promise.all(gates.map(async gate => {
    const artifact = await api("runs/" + run.run_id + "/artifacts/" + gate.artifact);
    const {tool_id, result:output} = JSON.parse(artifact.content);
    let records;
    if (tool_id === "create_requirement") {
      const disposition = output.disposition === "existing" ? "已有关联需求" : "已创建需求";
      records = `<div class="record-line"><div><small>${disposition}</small>
        <p>${button("view-requirement", esc(output.title) + " →", output.id, "text-button")}</p>
        <code>${esc(output.id)}</code></div></div>`;
    } else {
      records = `<p class="muted">检索词 · ${esc(output.query)}</p>` + (output.items.map(item =>
        `<div class="record-line"><div><strong>${tool_id === "search_requirements"
          ? button("view-requirement",esc(item.title) + " →",item.id,"text-button") : esc(item.title)}</strong>
          <p>${esc(item.excerpt)}</p><code>${esc(item.id)}</code></div></div>`).join("") || "<p>没有匹配条目。</p>");
    }
    return `<section class="record-section"><header><h2>${esc(gate.tool_step ? gate.label : "工具回执")}</h2>
      <span>${esc(output.source || run.project_name)}</span></header>${records}</section>`;
  }));
  const receiptView = receipts.join("");
  const answer = run.nodes.find(node => node.state === "succeeded" && node.artifact &&
    (node.is_agent_answer || node.node_id === "answer"));
  if (!answer) return receiptView;
  const result = await api("runs/" + run.run_id + "/artifacts/" + answer.artifact);
  return `<div class="record-opening"><section class="record-response"><header><h2>工具执行结果</h2></header>
    ${presentAnswer(result.content, run, answer.artifact)}${conversationControls(run)}</section>${runGoal(run)}</div>` + receiptView;
}
function reviewToolCall(run) {
  const proposal = run.tool_proposal;
  if (!proposal?.proposal_ref) throw Error("尚未收到工具提议");
  const labels = {query:"检索关键词", title:"需求标题", description:"需求说明", priority:"优先级"};
  const parameters = proposal.error ? `<p class="form-error">${esc(proposal.error)}</p>` :
    Object.entries(proposal.arguments).map(([key,value]) => {
      const display = key === "priority" ? ({low:"低",medium:"中",high:"高"})[value] : value;
      return `<dl class="key-value"><dt>${esc(labels[key] || key)}</dt><dd>${esc(display)}</dd></dl>`;
    }).join("");
  const decisions = [["","选择处理决定"],["revise","退回模型重新拟定"],["reject","拒绝并结束任务"]];
  if (!proposal.error) decisions.splice(1,0,["approve","批准并执行"]);
  form("确认工具调用", `<h3>${esc(proposal.name)}</h3><p class="description">所属项目 · ${esc(run.project_name)}</p>
    ${parameters}${field("action","处理决定","","select",{options:decisions,required:true})}
    ${field("reason","处理说明","","textarea",{required:true,rows:2})}`,
    async data => {
      if (!viewingRun(run.run_id) || state.currentRun?.run_id !== run.run_id)
        throw Error("当前任务已切换，请重新打开工具提议");
      await api("runs/" + run.run_id + "/tool-decisions","POST",{
        action:data.action, reason:data.reason, proposal_ref:proposal.proposal_ref,
      },run.revision);
      closeSheet(); notify("已记录工具决定"); await render();
    }, "提交决定");
}
function waitingActions(run) {
  if (!run.waiting_actions?.length) return "";
  const rows = run.waiting_actions.map((item) => {
    const node = run.nodes.find((entry) => entry.node_id === item.node_id);
    let action = button("run-action", "处理 →", item.node_id);
    let reason = item.reason || "等待负责人确认";
    if (run.mode === "tool" && item.node_id === run.tool_proposal?.gate_id) {
      reason = run.tool_proposal?.name || "查看工具提议";
      action = button("run-action", "查看提议", item.node_id);
    }
    if (run.mode === "complex" || run.mode === "tool" && node?.kind === "model" || incompleteOutput[reason]) {
      const unsettled = (node?.state === "uncertain" || incompleteOutput[reason]) && run.budget?.uncertain_attempts?.includes(node?.attempt_id);
      reason = incompleteOutput[reason] || reason;
      if (unsettled) reason += " 请先核对本次费用。";
      action = unsettled ? (can("usage") ? button("run-tab", "查看待核对调用", "attempts") : "")
        : button("resume-run", run.status === "paused" ? "继续执行" : "重试待处理节点", run.run_id);
      if (reason === "invalid_complex_plan") reason = "模型返回的分工未通过校验，请检查规划节点的回执后重试。";
      if (reason === "invalid_agent_decision") reason = "模型给出的工具或参数未通过校验，可重试本轮决策。";
      if (reason === "tool_step_limit") reason = "已达到工具执行次数上限，可重试本轮决策，让模型根据已有回执回答。";
      if (reason === "additional_tool_requested") {
        reason = "本次仅授权一次调用，模型又提出了工具请求。可重试答复，或另建多步任务选择允许使用的工具。";
        action += button("new-tool-run", "新建多步任务", run.run_id);
      }
    }
    return `<div class="cost-row"><div><strong>${esc(node?.label || item.node_id)}</strong><p class="muted">${esc(reason)}</p></div>${action}</div>`;
  });
  return `<section class="block"><div class="block-heading"><h2>待处理事项</h2></div><div class="block-body">${rows.join("")}</div></section>`;
}
function complexProgress(run) {
  const branches = run.nodes.filter((node) => node.node_id.startsWith("branch-"));
  if (!branches.length) return "任务拆分";
  const finished = branches.filter((node) => node.state === "succeeded").length;
  if (finished < branches.length) return `子任务已完成 ${finished} / ${branches.length}`;
  return run.nodes.find((node) => node.node_id === "summary")?.artifact ? "汇总已完成" : "结果汇总";
}
function conversationControls(run) {
  const controls = (run.continuation_actions || []).map(action =>
    button("continue-answer", action === "followup" ? "继续追问" : (run.mode === "complex" ? "单次重新回答" : "重新生成"), action)
  ).join("");
  return controls ? `<div class="actions conversation-controls">${controls}</div>` : "";
}
function conversationHistory(run) {
  const context = run.conversation;
  if (!context) return "";
  const turns = context.turns || [];
  const expanded = new Set($$("details[data-conversation-turn][open]").map(el => el.dataset.conversationTurn));
  return `<section class="record-section conversation-context"><header><h2>对话上下文</h2>
    <a href="#runs/${esc(context.source_id)}">查看来源任务 →</a></header>
    <p class="muted">${context.action === "regenerate" ? "沿用原问题重新回答。" : "承接上一条答复。"}
    ${context.omitted_turns ? `更早的 ${esc(context.omitted_turns)} 轮保存在来源记录中。` : ""}</p>
    ${turns.map((t,i) => `<details class="record-subdetails" data-conversation-turn="${esc(t.run_id)}" ${expanded.has(t.run_id) ? "open" : ""}><summary>第 ${(context.omitted_turns || 0)+i+1} 轮 · ${esc(t.messages[0].content.slice(0,60))}${t.excerpted ? "（节选）" : ""}</summary>
      ${t.messages.map((m,index)=>{
        const receipt = index >= t.messages.length - 1 - (t.receipt_refs || []).length;
        const label = m.role === "assistant" ? "模型答复" : receipt ? "项目工具回执" : "用户输入";
        return `<div class="record-message"><small>${label}</small><pre>${esc(m.content)}</pre></div>`;
      }).join("")}
      <a href="#runs/${esc(t.run_id)}">查看完整任务 →</a></details>`).join("")}</section>`;
}
function continueAnswer(action) {
  const source = state.currentRun;
  const followup = action === "followup";
  const label = followup ? "继续追问" : "重新生成";
  const help = followup
    ? (source.mode === "tool" ? "新任务将引用工具回执和最终答复，进行一次文本回答。" : "新任务会携带最近的问答，最多 8 轮、24 KiB。")
    : "新建一次直接回答，沿用原问题、引用材料和之前的对话。";
  form(label, `<p class="description">${help}本次调用单独计费。</p>
    ${followup ? field("text","追问内容","","textarea",{required:true}) + materialPicker() : ""}
    ${personalPreferenceChoice()}
    <details class="optional-panel" ${source.nodes?.some(n => n.reason === "output_length_limit") ? "open" : ""}><summary>本次执行设置</summary><div class="field-grid">
      ${followup ? "" : field("max_output_tokens","每次输出 token 上限",source.task?.max_output_tokens ?? 256,"number",{min:1,max:1000000,required:true,help:"实际输出同时受模型端点上限约束。"})}
      ${field("budget","费用上限","","number",{min:0,step:"0.000001",placeholder:"按本次输入估算"})}
      ${field("currency","币种",source.budget.currency)}</div></details>`,
    async data => {
      const payload = {action};
      if (!followup && data.max_output_tokens) payload.max_output_tokens = Number(data.max_output_tokens);
      if (followup) {
        payload.text = data.text;
        const materials = collectMaterials($("#sheet-form"));
        if (materials.length) payload.materials = materials;
      }
      if (data.use_preferences) payload.use_preferences = true;
      if (data.budget !== "") payload.budget = {currency:data.currency,limit_micros:decimalMicros(data.budget,"费用上限")};
      const created = await api("runs/" + source.run_id + "/continuations","POST",payload,source.revision);
      closeSheet();
      state.runTab = "result";
      go("runs", created.run_id);
    }, label);
}
function runGoal(run) {
  return `<aside class="record-goal"><h2>原始目标</h2><p>${esc(run.task?.text || "历史记录未保存原始输入。")}</p>
    ${run.task?.materials?.length ? `<h3>引用材料</h3>${materialList(run.task.materials)}` : ""}
    ${run.task?.pinned_endpoint || run.role_name ? `<dl>${run.role_name ? `<div><dt>执行角色</dt><dd>${esc(run.role_name)}</dd></div>` : ""}
      ${run.task?.pinned_endpoint ? `<div><dt>指定模型</dt><dd>${esc(run.task.pinned_endpoint)}</dd></div>` : ""}</dl>` : ""}</aside>`;
}
async function complexTaskContent(run) {
  const summary = run.nodes.find((node) => node.node_id === "summary");
  const result = summary?.artifact ? await api("runs/" + run.run_id + "/artifacts/" + summary.artifact) : null;
  const pending = {cancelled:"任务已取消。",paused:"任务已暂停。",needs_input:"处理下方待办后可继续执行。"};
  return `<div class="record-opening"><section class="record-response">
    <header><h2>汇总结果</h2><span>${complexProgress(run)}</span></header>
    ${result ? presentAnswer(result.content, run, summary.artifact) :
      `<p class="record-pending">${pending[run.status] || "所有子任务完成后，汇总模型会提交最终答复。"}</p>`}
    ${conversationControls(run)}</section>${runGoal(run)}</div>`;
}
async function simpleTaskContent(run) {
  const node = run.nodes[0];
  const result = node?.artifact ? await api("runs/" + run.run_id + "/artifacts/" + node.artifact) : null;
  const reasons = {workspace_policy_changed:"工作空间授权或数据策略已变化。",insufficient_budget:"本次调用超过费用上限。",budget_limit:"本次调用超过费用上限。",run_paused:"任务已暂停。"};
  const needsSetup = node?.reason === "model_configuration_required" || run.budget_source === "no_configured_model";
  const fallback = {cancelled:"任务已取消。",paused:"任务已暂停。",queued:"正在等待工作线程领取任务。",running:"模型正在执行。",needs_input:"请检查节点记录并处理阻塞原因。"};
  const reason = node?.reason?.split(",").map((code) => incompleteOutput[code] || reasons[code] || code).join(" · ");
  return `<div class="record-opening"><section class="record-response"><header><h2>执行结果</h2>
    ${node?.endpoint_id ? `<span>${esc(node.endpoint_id)}</span>` : ""}</header>
    ${result ? presentAnswer(result.content, run, node.artifact) :
      needsSetup && !["cancelled","paused"].includes(run.status) ? `<div class="record-pending"><h3>请先配置模型</h3>${modelSetupHint()}</div>` :
      `<p class="record-pending">${esc(reason || fallback[run.status] || "当前结果不可用。")}</p>`}
    ${can("usage") && run.budget && !needsSetup ? `<footer class="record-result-cost"><span>已结算 <b>${amount(run.budget.settled_micros,run.budget.currency)}</b></span>
      ${run.budget.held_micros ? `<span>预留 ${amount(run.budget.held_micros,run.budget.currency)}</span>` : ""}
      ${button("run-tab","查看调用","attempts","text-button")}</footer>` : ""}${conversationControls(run)}</section>${runGoal(run)}</div>
    ${conversationHistory(run)}${!needsSetup ? waitingActions(run) : ""}${runNodeLedger(run)}`;
}
async function contextHTML(run) {
  const data = await api("runs/" + run.run_id + "/context");
  const items = data.items || [];
  if (!items.length) return empty("尚无请求上下文","实际派发后可检查发送给模型的消息、技能版本和请求摘要。");
  return `<section class="record-section"><header><h2>派发上下文</h2><span>${items.length} 次派发</span></header>
    ${items.map((item) => {
      const node = run.nodes.find((n) => n.node_id === item.node_id);
      const fields = [["尝试",item.attempt_id],["模型身份",item.model_identity],["原始请求摘要",item.context_sha256],
        ["展示内容摘要",item.view_sha256],["来源运行",item.inherited_from?.run_id || run.run_id]];
      return `<details class="record-evidence"${items.length === 1 ? " open" : ""}>
        <summary><div><strong>${esc(node?.label || item.node_id)}</strong><small>第 ${esc(item.generation)} 世代 · ${esc(item.endpoint_id || "未记录端点")}</small></div>
          <span>${item.data_scope === "local_only" ? "仅本地" : item.data_scope === "public" ? "允许外发" : "未记录范围"}</span></summary>
        <div class="record-evidence-body">${item.availability === "legacy_missing" ? "<p>此历史尝试未保存原始消息与技能快照。</p>" :
          `<div class="record-skills">${(item.skills || []).map((skill) => `<span>${esc(skill.name)} · v${esc(skill.revision)}</span>`).join("")}</div>
          <details class="record-subdetails"><summary>发送的消息 · ${item.messages?.length || 0} 条</summary>
            ${(item.messages || []).map((message) => `<div class="record-message"><small>${esc(message.role)}</small><pre>${esc(message.content)}</pre></div>`).join("")}</details>`}
          <details class="record-subdetails"><summary>核对来源与摘要</summary><dl class="record-facts">
          ${fields.filter(([,val]) => val).map(([key,val]) => `<div><dt>${key}</dt><dd>${esc(val)}</dd></div>`).join("")}</dl>
          ${item.redacted ? "<p>当前消息已脱敏。原始请求与展示内容各自保留摘要。</p>" : ""}</details>
        </div></details>`;
    }).join("")}</section>`;
}
async function checkpointsHTML(run, generation) {
  const items = await collection("runs/" + run.run_id + "/checkpoints");
  if (generation !== state.generation || !viewingRun(run.run_id)) return "";
  state.checkpoints = items;
  const canFork = ["single","sop"].includes(run.mode);
  const busy = run.nodes.some((n) => ["leased","dispatched","uncertain"].includes(n.state)) ||
    (run.jobs || []).some((j) => ["queued","running"].includes(j.state));
  return `<section class="record-section"><header><h2>运行检查点</h2>
    ${can("write") && !busy ? button("create-checkpoint","保存检查点",run.run_id,"primary") : ""}</header>
    ${busy && can("write") ? '<p class="record-notice">待调用、对账或交付作业处理后可保存检查点。</p>' : ""}
    ${!items.length ? empty("尚未保存检查点","保存后可核对该时刻的节点世代和产物。") : items.map((item) =>
      `<article class="record-checkpoint"><div class="record-line"><div><h3>${esc(item.label)}</h3>
        <small>${date(item.created_at)} · ${esc(item.created_by)} · 运行 v${esc(item.run_revision)}</small>
        <p>${esc(item.reason)}</p></div><div class="actions">${button("checkpoint-detail","查看快照",item.id)}
        ${can("write") && canFork ? button("fork-checkpoint","从此处派生任务",item.id) : ""}</div></div>
        <details class="record-subdetails"><summary>节点状态 · ${item.node_states?.length || 0} 项</summary>
        <ul class="record-checklist">${(item.node_states || []).map((n) => `<li><code>${esc(n.node_id)}</code><span>第 ${esc(n.generation)} 世代</span>${badge(n.state)}</li>`).join("")}</ul></details></article>`
    ).join("")}</section>`;
}
async function reviewsHTML(run, generation) {
  if (run.mode !== "workflow")
    return empty(
      "当前任务采用 SOP 人工节点",
      "请在执行流程中查看节点产物，并批准或退回。",
    );
  const [specs, findings] = await Promise.all([
    collection("runs/" + run.run_id + "/specs"),
    collection("runs/" + run.run_id + "/findings"),
  ]);
  if (generation !== state.generation || !viewingRun(run.run_id)) return "";
  const spec = specs.find((s) => s.version === run.spec_version_id);
  state.spec = spec;
  state.findings = findings;
  return /* HTML */ `<div class="review-layout">
      <section class="block">
        <div class="block-heading">
          <h2>需求基线</h2>
          ${button("revise-spec", "修订条款", "", "text-button")}
        </div>
        <div class="block-body">
          ${spec
            ? `<div class="actions">${badge(spec.approved ? "completed" : "review")}<code class="muted mono">${esc(spec.version.slice(0, 12))}</code></div>${spec.clauses.map((c) => `<div class="clause"><code>${esc(c.id)}</code><div><strong>${esc(c.text)}</strong><p>${esc(Array.isArray(c.acceptance) ? c.acceptance.join("；") : c.acceptance)}</p></div></div>`).join("")}${!spec.approved ? `<div class="actions" style="margin-top:20px">${button("approve-baseline", "批准需求基线", "", "primary")}</div>` : ""}`
            : empty(
                "等待需求草稿",
                "需求节点完成后，条款与验收条件会显示在这里。",
              )}
        </div>
      </section>
      <section class="block">
        <div class="block-heading">
          <h2>独立评审意见</h2>
          <span class="pill">${findings.length} 条意见</span>
        </div>
        <div class="block-body">
          ${findings.length
            ? findings
                .map(f => {
                  const target = {historical:"历史版本", signed:"目标已签核", open:"当前评审"}[f.target_status];
                  const resolution = f.resolution ? "已处置 · " + f.resolution.action
                    : f.target_status === "open" ? "待处置" : "无处置记录";
                  const edit = !f.resolution && f.target_status === "open" && can("approve");
                  return `<article class="finding"><div class="actions">${badge(f.severity === "blocker" ? "blocked" : "review")}<code class="mono muted">${esc(f.location)}</code></div><p>${esc(f.example || f.suggestion)}</p><small>${esc(f.evidence || "")}</small><footer><span>${esc([target, resolution].filter(Boolean).join(" · "))}</span>${edit ? button("resolve-finding", "记录处置", f.id, "text-button") : ""}</footer></article>`;
                })
                .join("")
            : empty(
                "暂无评审意见",
                "独立评审完成后，阻断问题与处理记录会显示在这里。",
              )}
        </div>
      </section>
    </div>
    <section class="block">
      <div class="block-heading"><h2>交付操作</h2></div>
      <div class="block-body">
        <div class="actions">
          ${button("integrate", "整合工作包")}${run.baseline_approved
            ? button("rework", "退回工作包")
            : ""}${run.candidate_sha
            ? button("execute-tests", "执行必测项") +
              button("approve-delivery", "接纳交付", "", "primary")
            : ""}${run.status === "completed"
            ? button("export-run", "导出交付清单", run.run_id)
            : ""}
        </div>
        <p class="bottom-note">审批与实测始终绑定当前需求版本和候选提交。</p>
      </div>
    </section>`;
}
