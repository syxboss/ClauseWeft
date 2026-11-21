const agentNames = {claude: "Claude Code", deepseek: "DeepSeek Harness"};
const agentStates = {queued:"排队中", running:"运行中", succeeded:"已完成", failed:"失败", cancelled:"已取消", interrupted:"执行中断"};

function agentProject() {
  return state.projects.find(project => project.id === state.project)
    || state.projects.find(project => project.status === "active")
    || state.projects[0];
}
function agentStatus(status) {
  return `<span class="badge ${esc(status)}">${esc(agentStates[status] || status)}</span>`;
}
function agentBusy(job) {
  return ["queued", "running"].includes(job.status);
}
function agentTitle(job) {
  return (job.title || job.prompt || "").split("\n")[0].slice(0, 90) || job.id;
}
function agentPath(id) {
  return "agent-jobs/" + encodeURIComponent(id);
}
function agentConfigNote(options) {
  if (!options.runners.length) return "在工作台启动配置的 agent_runners 中登记执行器，然后重启。";
  if (!options.repositories.length) return "请登记允许外发的 Git 仓库和文件范围，并在当前工作空间授权。";
  return "";
}
async function externalAgentPage(id) {
  if (id) {
    const job = await api(agentPath(id));
    if (!state.projects.some(p => p.id === job.project_id) || (state.project && job.project_id !== state.project)) {
      return empty("此任务属于其他项目", "切换到任务所属的项目即可查看。");
    }
    return agentJobDetail(job);
  }
  const project = agentProject();
  if (!project) return heading("外部 Agent") + empty("先创建一个项目", "Agent 任务、运行结果和补丁都保存在所属项目中。", "new-project", "创建项目");
  const scope = "?project_id=" + encodeURIComponent(project.id);
  const key = JSON.stringify([state.user?.id, state.workspaceId, project.id]);
  if (state.agentScope !== key) {
    state.agentScope = key;
    state.agentCursor = 0;
  }
  const cursor = state.agentCursor || 0;
  const [options, page] = await Promise.all([
    api("agent-jobs/options" + scope),
    api("agent-jobs" + scope + "&limit=30&cursor=" + cursor),
  ]);
  const jobs = page.items;
  const hint = agentConfigNote(options);
  const controls = !hint && project.status === "active" ? button("agent-create", "发起 Agent 任务", project.id, "primary") : "";
  const header = heading("外部 Agent", project.name,
    button("agent-repositories", "仓库授权", state.workspaceId) + controls);
  const intro = hint ? `<section class="record-section"><h2>配置执行环境</h2><p>${esc(hint)}</p><p class="muted">外部 Agent 会将任务和授权文件发送给所选服务。仅本地处理的仓库可继续使用本地模型。</p></section>` : "";
  const rows = jobs.filter(job => matches([agentTitle(job), job.repo_id, agentNames[job.kind], job.id]));
  const history = rows.length ? table(["任务", "执行器", "仓库", "状态", "创建时间"], rows.map(job =>
    `<tr${agentBusy(job) ? " data-agent-live" : ""}><td>${button("agent-open", esc(agentTitle(job)), job.id, "table-link")}</td><td>${esc(agentNames[job.kind] || job.runner_id)}</td><td>${esc(job.repo_id)}</td><td>${agentStatus(job.status)}</td><td>${date(job.created, true)}</td></tr>`))
    : deskEmpty(state.query ? "没有匹配的任务" : "这个项目还没有外部 Agent 任务");
  const pager = cursor || page.next_cursor != null ? `<footer class="actions audit-pagination">
    ${cursor ? button("agent-page", "上一页", String(Math.max(0, cursor - 30))) : ""}
    <span>第 ${Math.floor(cursor / 30) + 1} 页</span>
    ${page.next_cursor != null ? button("agent-page", "下一页", String(page.next_cursor)) : ""}</footer>` : "";
  return header + intro + history + pager;
}
function agentJobDetail(job) {
  const events = job.events || [];
  const running = agentBusy(job);
  const back = '<a class="text-button" href="#agents">返回外部 Agent</a>';
  const buttons = running ? button("agent-cancel", "取消任务", job.id, "danger") : "";
  const canFollowup = job.status === "succeeded" && job.kind === "claude";
  const review = canFollowup && job.patch ? button("agent-review", "用 DeepSeek 评审这份改动", job.id) : "";
  const cont = canFollowup ? button("agent-continue", "基于此候选继续修改", job.id) : "";
  const runTest = canFollowup ? button("agent-test", "运行容器验收测试", job.id) : "";
  const patch = job.patch ? `<section class="record-section"><h2>候选补丁</h2>
    <p>${(job.paths || []).map(p => `<code>${esc(p)}</code>`).join(" · ")}</p>
    <div class="actions">${button("agent-download", "下载补丁", job.id)}${cont}${runTest}${review}</div>
    <pre class="agent-output" dir="auto">${esc(job.patch)}</pre></section>`
    : canFollowup ? `<section class="record-section"><h2>后续操作</h2><div class="actions">${cont}${runTest}</div></section>` : "";
  const reports = job.test_reports || [];
  const testsSection = reports.length ? `<section class="record-section"><h2>容器验收测试</h2>${reports.map(item =>
    `<div class="record-meta">${agentStatus(item.verified ? "succeeded" : "failed")}<span>套件 <code>${esc(item.suite_id)}</code></span><span>${item.verified ? "校验通过" : "未通过"}</span><time>${date(item.created, true)}</time></div>`).join("")}</section>` : "";
  const reference = [
    job.review_of ? `<p>评审来源 · ${button("agent-open", esc(job.review_of), job.review_of, "text-button")}</p>` : "",
    job.continue_from ? `<p>续接来源 · ${button("agent-open", esc(job.continue_from), job.continue_from, "text-button")}</p>` : "",
  ].filter(Boolean).join("");
  const usageLabels = {input_tokens:"输入", output_tokens:"输出", cache_read_tokens:"缓存读取", cache_write_tokens:"缓存写入"};
  const tokenUsage = Object.entries(job.usage || {}).filter(([name, count]) => Object.hasOwn(usageLabels, name) && Number.isSafeInteger(count))
    .map(([name, count]) => `<span>${usageLabels[name]} ${esc(count)} token</span>`).join(" ");
  const receipt = job.session_id || tokenUsage ? `<details class="record-subdetails"><summary>执行回执</summary>
    ${job.session_id ? `<p>会话 ID · <code>${esc(job.session_id)}</code></p>` : ""}<p>${tokenUsage}</p></details>` : "";
  const result = job.error || job.text ? `<section class="record-section"><h2>${job.error ? "执行结果与错误" : "执行结果"}</h2><pre class="agent-output" dir="auto">${esc([job.error_detail || job.error, job.text].filter(Boolean).join("\n"))}</pre></section>` : "";
  const eventLabels = {progress:"处理中", tool_use:"使用工具", testing:"运行验收测试", test_passed:"验收测试通过", test_failed:"验收测试未通过", auto_fix:"自动修复反思", ...agentStates};
  return `<article class="agent-record"${running ? " data-agent-live" : ""}>${heading(agentTitle(job), agentNames[job.kind] || job.runner_id, back + buttons)}
    <div class="record-meta">${agentStatus(job.status)}<span>${esc(projectName(job.project_id))} · ${esc(job.repo_id)}</span><code title="${esc(job.base_sha)}">${esc((job.base_sha || "").slice(0, 12))}</code><time>${date(job.created)}</time></div>
    ${reference}<section class="record-section"><h2>任务目标</h2><p class="agent-prompt">${esc(job.prompt)}</p></section>
    <section class="record-section"><h2>运行记录</h2>${events.length ? `<ol class="agent-events">${events.map(event =>
      `<li><time>${date(event.at, true)}</time> <span>${esc(eventLabels[event.type] || event.type)}</span> ${event.tool ? `<code>${esc(event.tool)}</code>` : ""}</li>`).join("")}</ol>` : "<p>等待第一条执行事件。</p>"}</section>${result}${testsSection}${receipt}${patch}</article>`;
}
async function agentJobForm(projectId, source = null, mode = source ? "review" : "create") {
  const project = state.projects.find(item => item.id === projectId && item.status === "active");
  if (!project) throw Error("请选择一个活跃项目");
  const selection = await api("agent-jobs/options?project_id=" + encodeURIComponent(projectId));
  const isReview = mode === "review";
  const isContinue = mode === "continue";
  const runners = selection.runners.filter(r => isReview ? r.kind === "deepseek" : isContinue ? r.kind === "claude" : true);
  const repos = selection.repositories.filter(r => !source || r.id === source.repo_id);
  if (!runners.length || !repos.length) throw Error(isReview ? "该项目需要可用的 DeepSeek 执行器和来源仓库授权。" : isContinue ? "该项目需要可用的 Claude Code 执行器和来源仓库授权。" : agentConfigNote(selection));
  const choices = runners.map(r => [r.id, (agentNames[r.kind] || r.kind) + " · " + r.id]);
  const suites = [...new Set(repos.flatMap(r => r.suites || []))];
  const testControls = !isReview && suites.length
    ? field("suite_id", "完成后自动运行验收套件（可选）", "", "select", {options:[["","不自动运行"], ...suites.map(s => [s, s])]})
      + field("max_fix_rounds", "测试失败自动修复轮次（0–3）", 0, "number", {min:0, max:3, step:1})
    : "";
  const body = field("runner_id", "执行器", runners[0].id, "select", {options:choices, required:true})
    + (isReview ? `<p>评审仓库 <strong>${esc(source.repo_id)}</strong> 的已保存候选补丁。</p>`
      : isContinue ? `<p>基于仓库 <strong>${esc(source.repo_id)}</strong> 的前次候选文件继续修改，并累计生成相对于原基准的补丁。</p>`
      : field("repo_id", "仓库", repos[0].id, "select", {options:repos.map(r => [r.id, r.id]), required:true})
      + field("base_sha", "基准提交", "HEAD", "text", {required:true, help:"使用 HEAD 或完整提交哈希。运行会固定实际提交。"}))
    + testControls
    + field("prompt", isReview ? "评审关注点" : "任务目标", isReview ? "请审阅此候选补丁，说明问题及其依据。" : "", "textarea", {rows:4, required:true})
    + '<label class="check"><input type="checkbox" name="allow_external" required>同意将任务和该仓库授权范围内的文件交给所选外部服务。</label>';
  const title = isReview ? "评审候选补丁" : isContinue ? "基于候选继续修改" : "发起 Agent 任务";
  form(title, body, async data => {
    if (!data.allow_external) throw Error("请确认外部处理范围");
    const request = {project_id:projectId, runner_id:data.runner_id, prompt:data.prompt.trim(), repo_id:source?.repo_id || data.repo_id};
    if (isReview) request.review_of = source.id;
    else if (isContinue) request.continue_from = source.id;
    else request.base_sha = data.base_sha.trim();
    if (data.suite_id) {
      request.suite_id = data.suite_id;
      const rounds = Number(data.max_fix_rounds ?? 0);
      if (rounds > 0) request.max_fix_rounds = rounds;
    }
    const job = await api("agent-jobs", "POST", request);
    closeSheet();
    go("agents", job.id);
  }, isReview ? "开始评审" : "开始执行");
}
Object.assign(actions, {
  "agent-repositories": async id => {
    if (id !== state.workspaceId) throw Error("请重新选择工作空间");
    const [config, workspace] = await Promise.all([api("settings"), api("workspaces/" + encodeURIComponent(id))]);
    state.adminConfig = config;
    workspaceForm(workspace);
  },
  "agent-page": cursor => {
    state.agentCursor = Number(cursor);
    return render();
  },
  "agent-open": id => go("agents", id),
  "agent-create": id => agentJobForm(id, null, "create"),
  "agent-review": async id => {
    const source = await api(agentPath(id));
    return agentJobForm(source.project_id, source, "review");
  },
  "agent-continue": async id => {
    const source = await api(agentPath(id));
    return agentJobForm(source.project_id, source, "continue");
  },
  "agent-test": async id => {
    const job = await api(agentPath(id));
    const options = await api("agent-jobs/options?project_id=" + encodeURIComponent(job.project_id));
    const repo = options.repositories.find(r => r.id === job.repo_id);
    const suites = repo?.suites || [];
    if (!suites.length) throw Error("当前仓库未配置可执行的验收测试套件");
    await api(agentPath(id) + "/tests", "POST", {suite_id: suites[0]});
    notify("容器验收测试已完成");
    await render();
  },
  "agent-cancel": async id => {
    const job = await api(agentPath(id));
    if (agentBusy(job)) await api(agentPath(id) + "/cancel", "POST", {}, job.revision);
    await render();
  },
  "agent-download": async id => {
    const job = await api(agentPath(id));
    if (!job.patch) throw Error("此任务没有候选补丁");
    downloadBlob(new Blob([job.patch], {type:"text/x-diff;charset=utf-8"}), "clauseweft-" + job.id + ".patch");
  },
});
