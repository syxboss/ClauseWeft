"""Exercise the shipped console JavaScript without installing browser packages."""
import shutil
import subprocess
import unittest

from clauseweft.api.console_app import SCRIPT


@unittest.skipUnless(shutil.which("node"), "console JavaScript tests need Node.js")
class TaskConsoleTest(unittest.TestCase):
    def check_script(self, assertions):
        browser = """
const assert = require("node:assert/strict");
const sessionStorage = {getItem: () => null};
const element = {addEventListener() {}};
const handlers = {};
const document = {addEventListener(name, fn) { handlers[name] = fn; }, querySelector: () => element};
const window = element;
const location = {hash:"#overview"};
const setInterval = () => {};
"""
        fixture = """
Object.assign(state, {
  workspaceId: "team", workspace: {data_scope:"local_only"}, projects: [{id:"p"}],
  roles: [{id:"editor", prompt:"Role instructions", stage:"review",
           endpoint_id:"bound", skill_ids:["from-role"]}],
  modelOptions: [{endpoint_id:"bound", currency:"CNY"}, {endpoint_id:"other", currency:"USD"}],
});
const input = {project_id:"p", text:"Original input", role_id:"editor",
               endpoint_id:"other", skill_ids:["manual"]};
"""
        result = subprocess.run(
            ["node"], input=browser + SCRIPT + fixture + assertions,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_created_project_selected_after_grant_refresh_only_if_still_authorized(self):
        self.check_script("""
(async () => {
  let submit, workspaceAfter = "team", visible = true;
  form = (title, fields, handler) => { submit = handler; };
  closeSheet = notify = render = go = () => {};
  saveResource = async () => ({id:"created"});
  refreshIdentity = async () => {
    state.project = "";
    state.workspaceId = workspaceAfter;
    state.projects = visible ? [{id:"created"}] : [];
    return true;
  };
  projectForm(); await submit({name:"First"});
  assert.equal(state.project, "created");
  visible = false;
  projectForm(); await submit({name:"Revoked"});
  assert.equal(state.project, "");
  visible = true; workspaceAfter = "other";
  projectForm(); await submit({name:"Switched"});
  assert.equal(state.project, "");
})().catch(e => { console.error(e); process.exitCode = 1; });
""")

    def test_personal_pairing_remembers_only_the_revocable_owner_session(self):
        self.check_script("""
(async () => {
  const saved = new Map(), tab = new Map();
  window.localStorage = {
    getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key),
  };
  Object.assign(sessionStorage, {getItem: key => tab.get(key),
    setItem: (key, value) => tab.set(key, value), removeItem: key => tab.delete(key)});
  clearContext = refreshCatalog = render = setMobileNav = closeNavGroups = () => {};
  let actor = {id:"owner-id", personal_owner:true, role:"admin"};
  api = async () => actor;
  state.token = "owner-session";
  await connect(Date.now() / 1000 + 43200);
  assert.equal(savedPersonalSession().token, "owner-session");
  assert.equal(tab.get("clauseweft-token"), undefined);
  disconnect();
  assert.equal(saved.size, 0);
  saved.set(personalSessionKey, JSON.stringify({token:"expired", expires_at:1}));
  assert.equal(savedPersonalSession(), null);
  assert.equal(saved.size, 0);
  actor = {id:"bootstrap-admin", role:"admin"};
  state.token = "master-token"; await connect();
  assert(![...tab.values(), ...saved.values()].includes("master-token"));
  actor = {id:"reader", role:"viewer"};
  state.token = "team-session"; await connect();
  assert.equal(tab.get("clauseweft-token"), "team-session");
  assert.equal(saved.size, 0);
})().catch(e => { console.error(e); process.exitCode = 1; });
""")

    def test_resolve_finding_only_offers_its_peers_and_omits_optional_blank_evidence(self):
        self.check_script("""
let submit, fields, related = [], saved;
reasonForm = (title, handler, html) => { submit = handler; fields = html; };
runAction = (path, payload) => { saved = payload; };
globalThis.FormData = class { getAll() { return related; } };
const finding = {id:"subject", target:"v2", target_status:"open", severity:"minor"};
state.findings = [finding, {...finding, id:"peer"},
  {...finding, id:"old", target:"v1", target_status:"historical"}];
actions["resolve-finding"]("subject");
assert(fields.includes('value="peer"'));
assert(!fields.includes('value="subject"') && !fields.includes('value="old"'));
const decision = {action:"fix", reason:"checked", evidence:"  "};
submit(decision);
assert(!Object.hasOwn(saved, "evidence"));
assert.throws(() => submit({...decision, action:"duplicate"}), /关联意见/);
related = ["peer"];
submit({...decision, action:"duplicate", evidence:"audit note"});
assert.deepEqual(saved.related_ids, ["peer"]);
assert.equal(saved.evidence, "audit note");
assert.throws(() => actions["resolve-finding"]("old"), /当前待处置/);
""")

    def test_quick_submit_and_task_form_share_a_gateway_compatible_output_limit(self):
        self.check_script("""
(async () => {
  state.user = {capabilities:["write"]};
  Object.assign(state.workspace, {repositories:[], suites:[]});
  const posted = [];
  api = async (path, method, body) => { posted.push(body); return {mode:"single", run_id:"r"}; };
  collectMaterials = selectedSkills = () => [];
  go = closeSheet = notify = syncTaskFields = () => {};
  refreshIdentity = async () => false;
  const data = {project_id:"p", text:"Review"};
  globalThis.FormData = class { [Symbol.iterator]() { return Object.entries(data)[Symbol.iterator](); } };
  const quick = {id:"quick-task", querySelector: () => ({})};
  await handlers.submit({target:quick, preventDefault() {}});
  let modal, submit;
  form = (title, fields, handler) => { modal = fields; submit = handler; };
  await newRun({project_id:"p"});
  assert.match(modal, /name="max_output_tokens" type="number" value="1024"/);
  await submit({...data, max_output_tokens:"1024"});
  await submit({...data, max_output_tokens:"40"});
  assert.deepEqual(posted.map(p => p.task.max_output_tokens), [1024,1024,40]);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_direct_role_and_manual_skills_keep_separate_sources(self):
        self.check_script("""
const direct = makePlan({...input, budget:"0.5"});
assert.equal(direct.task.text, input.text);
assert.equal(direct.role_id, "editor");
assert.deepEqual(direct.skill_ids, ["manual"]);
assert.equal(direct.task.stage, undefined);
assert.equal(direct.task.pinned_endpoint, undefined);
assert.deepEqual(direct.budget, {currency:"CNY", limit_micros:500000});
""")

    def test_structured_probe_chooses_active_project_and_charges_selected_scope(self):
        self.check_script("""
(async () => {
  state.user = {capabilities:["admin", "write", "read"]};
  state.endpoints = [{endpoint_id:"bound",currency:"CNY",revision:7}];
  state.projects = [{id:"active",name:"活跃项目",status:"active"}, {id:"archived",name:"归档项目",status:"archived"}];
  state.project = "archived";
  let fields, submit, request, detail, html;
  sheet = (title, content) => {html = content;};
  form = (title, content, callback) => {fields = content; submit = callback;};
  api = async (...args) => {
    request = args;
    return args[2].kind === "structured_output" ? {run_id:"checked"} : {connected:true,etag_matches:true};
  };
  closeSheet = () => {};
  go = (...args) => {detail = args;};
  await actions["probe-endpoint"]("bound");
  assert.match(html, /data-action="capability-probe"/);
  actions["capability-probe"]("bound");
  assert.match(fields, /value="active" selected/);
  assert.doesNotMatch(fields, /归档项目/);
  await submit({project_id:"active",budget:"0.005"});
  assert.deepEqual(request, ["endpoints/bound/probes", "POST", {kind:"structured_output",workspace_id:"team",
     project_id:"active",budget:{currency:"CNY",limit_micros:5000}}, 7]);
  assert.deepEqual(detail, ["runs","checked"]);
  state.modelOptions = [];
  await actions["probe-endpoint"]("bound");
  assert.doesNotMatch(html, /data-action="capability-probe"/);
})().catch(err => {console.error(err); process.exitCode=1;});
""")

    def test_completed_answer_can_append_edited_note_after_revision_conflict(self):
        self.check_script("""
(async () => {
  const run = {run_id:"r", mode:"single", status:"completed", workspace_id:"team"};
  state.user = {id:"bootstrap-admin"};
  assert.doesNotMatch(presentAnswer("private result", run, "sha"), /remember-point/);
  state.user = {id:"member"};
  state.currentRun = run;
  assert.match(presentAnswer("private result", run, "sha"), /remember-point/);
  let handler, html, writes=[];
  let saved = {instructions:"old", revision:3};
  form = (title, fields, submit) => { handler=submit; html=fields; };
  notify = closeSheet = render = () => {};
  api = async (path, method, value, revision) => {
    assert.equal(path, "me/preferences/team");
    if (!method) return {...saved};
    if (revision !== saved.revision) throw Object.assign(Error("conflict"), {status:412});
    writes.push(value);
  };
  await rememberPoint();
  assert.match(html, /old/);
  assert.doesNotMatch(html, /private result/);
  assert.deepEqual(writes, []);
  saved = {instructions:"changed in another tab", revision:4};
  await handler({point:"my note"});
  assert.match(html, /changed in another tab/);
  assert.match(html, /my note/);
  assert.deepEqual(writes, []);
  await assert.rejects(handler({point:"a".repeat(4000)}), /4,000/);
  await handler({point:"my note"});
  assert.deepEqual(writes, [{instructions:"changed in another tab\\nmy note"}]);
})().catch(error => { console.error(error); process.exitCode=1; });
""")

    def test_material_picker_reads_only_valid_utf8_and_escapes_preview(self):
        self.check_script("""
(async () => {
  const file = (name, data) => Object.assign(new Blob([data]), {name});
  const list = await readMaterialFiles([file("接口.md", "<amount>&")], []);
  assert.deepEqual(makePlan({...input, materials:list}).task.materials, [{name:"接口.md", text:"<amount>&"}]);
  assert.match(materialPicker(list), /&lt;amount&gt;&amp;/);
  assert.doesNotMatch(materialList(list), /<amount>/);
  await assert.rejects(readMaterialFiles([file("bad.txt", new Uint8Array([255]))], list), /UTF-8/);
  await assert.rejects(readMaterialFiles([file("bin", new Uint8Array([0, 65]))], list), /纯文本/);
  await assert.rejects(readMaterialFiles([file("big", new Uint8Array(65536))], list), /64 KiB/);
  assert.equal(list.length, 1);
  assert.match(presentAnswer("<output>", {run_id:"r"}, "sha"), /&lt;output&gt;/);
  assert.match(presentAnswer("ok", {run_id:"r"}, "sha"), /data-action="download-answer"/);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_complex_form_carries_shared_budget_pin_and_skills(self):
        self.check_script("""
const data = {...input, template_id:"__complex__"};
assert.throws(() => makePlan(data), /总费用上限/);
const plan = makePlan({...data, budget:"0"});
assert.equal(plan.mode, "complex");
assert.equal(plan.role_id, undefined);
assert.equal(plan.template_id, undefined);
assert.deepEqual(plan.skill_ids, ["manual"]);
assert.equal(plan.task.pinned_endpoint, "other");
assert.deepEqual(plan.budget, {currency:"USD", limit_micros:0});
const auto = makePlan({...data, endpoint_id:"", budget:"1", currency:"CNY"});
assert.equal(auto.task.pinned_endpoint, undefined);
assert.deepEqual(auto.budget, {currency:"CNY", limit_micros:1000000});
""")

    def test_tool_selection_sends_only_supported_tools_and_valid_limits(self):
        self.check_script("""
const data = {...input, template_id:"__tool__", budget:"1"};
const plan = makePlan(data);
assert.equal(plan.mode, "tool");
assert.deepEqual(plan.allowed_tools, ["search_requirements","create_requirement"]);
assert.equal(plan.max_tool_steps, 4);
assert.equal(plan.tool_id, undefined);
assert.throws(() => makePlan({...data,include_reference:"on"}), /资料源/);
state.workspace.project_references = {p:"资料库"};
assert.deepEqual(makePlan({...data,include_reference:"on"}).allowed_tools, [...plan.allowed_tools,"search_reference"]);
const ensure = {...data,tool_id:"__ensure__",requirement_query:"  节点  ",max_tool_steps:"2",include_reference:"on"};
assert.equal(makePlan(ensure).requirement_query, "节点");
assert.deepEqual(makePlan(ensure).allowed_tools, plan.allowed_tools);
for (const limit of ["0","1","2.5","9"]) assert.throws(() => makePlan({...ensure,max_tool_steps:limit}), /次数/);
for (const query of ["  ","事".repeat(161)]) assert.throws(() => makePlan({...ensure,requirement_query:query}), /查重关键词/);
for (const tool_id of ["search_reference","create_requirement","search_requirements"]) {
  const legacy = makePlan({...ensure,tool_id});
  assert.equal(legacy.tool_id, tool_id);
  assert.equal(legacy.allowed_tools, undefined);
  assert.equal(legacy.requirement_query, undefined);
}
""")

    def test_adaptive_gate_approval_receipts_final_answer_and_retry(self):
        self.check_script("""
(async () => {
  state.user = {capabilities:["read","write","approve","usage"]};
  const proposal = {gate_id:"confirm-2",name:"新建项目需求",proposal_ref:"proposal",arguments:{title:"<new>"}};
  location.hash = "#runs/r";
  const run = {run_id:"r",mode:"tool",revision:5,status:"needs_input",project_name:"项目",tool_proposal:proposal,
    nodes:[{node_id:"confirm-2",kind:"gate",state:"needs_input"}],waiting_actions:[{node_id:"confirm-2"}]};
  state.currentRun = run;
  let handler, content, request;
  form = (title, html, submit) => {content=html;handler=submit;};
  api = async (...args) => {request=args;};
  closeSheet = notify = render = () => {};
  assert.match(waitingActions(run), /查看提议/);
  await actions["run-action"]("confirm-2");
  assert.match(content, /&lt;new&gt;/);
  await handler({action:"approve",reason:"核对"});
  assert.deepEqual(request,["runs/r/tool-decisions","POST",{action:"approve",reason:"核对",proposal_ref:"proposal"},5]);
  location.hash = "#runs/other";
  request = null;
  await assert.rejects(handler({action:"approve",reason:"核对"}), /任务已切换/);
  await assert.rejects(actions["run-action"]("confirm-2"), /任务已切换/);
  assert.equal(request, null);
  location.hash = "#runs/r";
  await assert.rejects(actions["run-action"]("confirm-1"), /步骤已更新/);
  run.nodes = [
    {node_id:"confirm-1",kind:"gate",state:"succeeded",artifact:"a",label:"第 1 步 · 检索项目需求",tool_step:1},
    {node_id:"confirm-2",kind:"gate",state:"succeeded",artifact:"b",label:"第 2 步 · 新建项目需求",tool_step:2},
    {node_id:"decide-3",kind:"model",state:"needs_input",reason:"invalid_agent_decision"},
  ];
  for (const reason of ["invalid_agent_decision","tool_step_limit"]) {
    run.waiting_actions=[{node_id:"decide-3",reason}];
    assert.match(waitingActions(run), /重试本轮决策/);
  }
  api = async path => ({content:({
    a:JSON.stringify({tool_id:"search_requirements",result:{items:[],query:"<query>"}}),
    b:JSON.stringify({tool_id:"create_requirement",result:{id:"created",title:"<created>"}}), c:"<final>",
  })[path.split("/").pop()]});
  const interim = await toolAnswer(run);
  assert.match(interim, /没有匹配条目/);
  assert.match(interim, /&lt;created&gt;/);
  assert.doesNotMatch(interim, /工具执行结果/);
  Object.assign(run.nodes[2],{is_agent_answer:true,state:"succeeded",artifact:"c"});
  Object.assign(run,{status:"completed",continuation_actions:["followup"]});
  const finished = await toolAnswer(run);
  assert.match(finished, /&lt;final&gt;/);
  assert.match(finished, /继续追问/);
  assert.match(finished, /第 2 步/);
  assert.doesNotMatch(finished, /重新生成/);
})().catch(error => { console.error(error); process.exitCode=1; });
""")

    def test_live_trace_follows_running_work_unless_a_run_was_chosen(self):
        self.check_script("""
const queued = {run_id:"queued", title:"Queued", status:"queued", nodes:[]};
const active = {run_id:"active", title:"Active", status:"running", nodes:[
  {node_id:"plan", state:"succeeded", dependencies:[]},
  {node_id:"branch-1", state:"dispatched", dependencies:["plan"], brief:"Read <input>"},
  {node_id:"branch-2", state:"uncertain", dependencies:["plan"]},
  {node_id:"summary", state:"waiting", dependencies:["branch-1","branch-2"]},
]};
const html = runTrace([queued, active]);
assert.match(html, /value="active" selected/);
assert.match(html, /Read &lt;input&gt;/);
assert.equal((html.match(/<path data-phase=/g) || []).length, 4);
assert.match(html, /data-phase="uncertain"/);
state.traceRunId = "queued";
assert.match(runTrace([queued, active]), /value="queued" selected/);
assert.match(runTrace([]), /暂无运行轨迹/);
""")

    def test_catalog_refresh_preserves_allowed_project_scope(self):
        self.check_script("""
(async () => {
  state.project = "p";
  state.traceRunId = "selected-run";
  sessionStorage.setItem = () => {};
  collection = async path => path === "workspaces" ? [{id:"team", name:"Team"}] :
                             path.startsWith("projects") ? [{id:"p", name:"Project"}] : [];
  api = async () => ({model_options:[]});
  await refreshCatalog();
  assert.equal(state.project, "p");
  assert.equal(state.traceRunId, "selected-run");
  state.project = "revoked";
  await refreshCatalog();
  assert.equal(state.project, "");
  assert.equal(state.traceRunId, null);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_late_run_detail_and_checkpoint_response_cannot_replace_visible_task(self):
        self.check_script("""
(async () => {
  let pendingRun, pendingCheckpoints;
  const run = id => ({run_id:id,project_id:"p",workspace_id:"team",mode:"single",status:"completed",nodes:[]});
  api = path => path === "runs/old" ? new Promise(resolve => {pendingRun = resolve;}) : Promise.resolve(run("new"));
  simpleTaskContent = async () => "";
  collection = () => new Promise(resolve => {pendingCheckpoints = resolve;});
  location.hash = "#runs/old";
  const oldRequest = runDetail("old", ++state.generation);
  const oldTab = checkpointsHTML(run("old"), state.generation);
  location.hash = "#runs/new";
  const visible = await runDetail("new", ++state.generation);
  assert.match(visible, /data-run="new"/);
  state.checkpoints = [{id:"new-checkpoint"}];
  pendingRun(run("old"));
  pendingCheckpoints([{id:"old-checkpoint"}]);
  assert.equal(await oldRequest, "");
  assert.equal(await oldTab, "");
  assert.equal(state.currentRun.run_id, "new");
  assert.deepEqual(state.checkpoints, [{id:"new-checkpoint"}]);
})().catch(error => {console.error(error);process.exitCode=1;});
""")

    def test_progress_distinguishes_planning_branches_and_summary(self):
        self.check_script("""
const run = {nodes:[{node_id:"plan",state:"dispatched"}]};
assert.equal(complexProgress(run), "任务拆分");
run.nodes.push({node_id:"branch-1", state:"succeeded"}, {node_id:"branch-2", state:"ready"},
               {node_id:"summary", state:"waiting"});
assert.equal(complexProgress(run), "子任务已完成 1 / 2");
run.nodes[2].state = "succeeded";
assert.equal(complexProgress(run), "结果汇总");
run.nodes[3].artifact = "result";
assert.equal(complexProgress(run), "汇总已完成");
""")

    def test_waiting_branch_reconciles_before_retry(self):
        self.check_script("""
state.user = {capabilities:["read","write","usage"]};
const run = {
  run_id:"r", mode:"complex", status:"needs_input", budget:{uncertain_attempts:["a"]},
  waiting_actions:[{node_id:"branch-1",action:"reconcile"}],
  nodes:[{node_id:"branch-1",attempt_id:"a",state:"uncertain"}],
};
assert.match(waitingActions(run), /查看待核对调用/);
assert.doesNotMatch(waitingActions(run), /重试待处理节点/);
run.budget.uncertain_attempts = [];
assert.match(waitingActions(run), /重试待处理节点/);
state.user = {capabilities:["read"]};
assert.doesNotMatch(waitingActions(run), /data-action=/);
""")

    def test_external_agent_forms_consent_permissions_and_escaped_evidence(self):
        self.check_script("""
(async () => {
  state.user = {id:"owner", capabilities:["admin","read","write"]};
  state.projects = [{id:"p",name:"个人项目",status:"active"}];
  const source = {id:"source", title:"改问候语", project_id:"p", repo_id:"repo",
    status:"succeeded", patch:"<review>", prompt:"<input>", text:"<result>", kind:"claude",
    paths:["<path>"], events:[{type:"tool_use", tool:"<tool>"}], usage:{output_tokens:9}};
  const detail = agentJobDetail(source);
  for (const field of ["review", "input", "result", "path", "tool"]) {
    assert.ok(detail.includes("&lt;" + field + "&gt;"));
    assert.ok(!detail.includes("<" + field + ">"));
  }
  assert.match(detail, /agent-review/);
  assert.match(detail, /输出 9 token/);
  let body, submit, sent;
  const configs = {repositories:[{id:"repo"}], runners:[{id:"a",kind:"claude"},{id:"b",kind:"deepseek"}]};
  form = (_, fields, handler) => { body=fields; submit=handler; };
  api = async (path, method, data) => {
    if (method === "POST") {sent = data; return {id:"j"};}
    return configs;
  };
  closeSheet = go = () => {};
  await agentJobForm("p", source);
  assert.ok(body.includes("DeepSeek Harness") && !body.includes("Claude Code"));
  const request = {runner_id:"b",prompt:" review ",allow_external:"on"};
  await assert.rejects(submit({...request,allow_external:""}), /确认/);
  assert.equal(sent, undefined);
  await submit(request);
  assert.deepEqual(sent, {project_id:"p", runner_id:"b", repo_id:"repo", prompt:"review", review_of:"source"});
  state.user = {capabilities:["read"]};
  assert.equal(can(pageCapabilities.agents), false);
  for (const action of ["agent-create","agent-review","agent-cancel","agent-repositories"]) {
    assert.equal(actionAllowed(action), false);
  }
  location.hash="#constructor";
  assert.equal(route().page, "overview");
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
