async function connect(personalExpiry) {
  clearContext();
  state.user = await api("auth/me");
  if (sessionStorage.getItem("clauseweft-user") !== state.user.id)
    state.workspaceId = "default";
  sessionStorage.setItem("clauseweft-user", state.user.id);
  await refreshCatalog();
  const saved = savedPersonalSession();
  if (state.user.personal_owner && (personalExpiry || state.token === saved?.token)) {
    const expires = personalExpiry || saved.expires_at;
    window.localStorage.setItem(personalSessionKey, JSON.stringify({token: state.token, expires_at: expires}));
    sessionStorage.removeItem("clauseweft-token");
  } else {
    window.localStorage?.removeItem(personalSessionKey);
    if (state.user.id === "bootstrap-admin") sessionStorage.removeItem("clauseweft-token");
    else sessionStorage.setItem("clauseweft-token", state.token);
  }
  $("#access").hidden = true;
  $("#shell").hidden = false;
  await render();
}
document.addEventListener("click", async (event) => {
  if (event.target.closest(".skip")) {
    event.preventDefault();
    ($("#shell").hidden ? $("#login-tabs button.active") : $("#content")).focus();
    return;
  }
  const group = event.target.closest(".nav-disclosure");
  closeNavGroups(event.target.closest("#nav a") ? null : group);
  if (event.target.closest("#nav a") || !event.target.closest("#sidebar, .menu-control")) setMobileNav(false);
  const control = event.target.closest("[data-action]");
  if (!control) return;
  event.preventDefault();
  const handler = actions[control.dataset.action];
  if (!handler) return;
  const wasDisabled = control.disabled;
  const displayedRun = control.closest(".execution-record")?.dataset.run;
  control.disabled = true;
  try {
    if (!["mobile-menu", "close-sheet", "login-mode", "logout"].includes(control.dataset.action)) {
      if (await refreshIdentity()) {
        await render();
        notify("账号权限已更新，请在当前范围内重新操作。");
        return;
      }
    }
    if (!actionAllowed(control.dataset.action)) throw Error("当前账号没有此操作权限");
    if (displayedRun && (!viewingRun(displayedRun) || state.currentRun?.run_id !== displayedRun))
      throw Error("当前任务已切换，请重新操作");
    await handler(control.dataset.id);
  } catch (error) {
    notify(error.message, true);
  } finally {
    if (control.isConnected) control.disabled = wasDisabled;
  }
});
document.addEventListener("toggle", async (event) => {
  if (!event.target.matches(".nav-disclosure[open]")) return;
  const count = $("#approval-count", event.target);
  if (!count) return;
  try {
    const pending = (await api("overview" + projectQuery())).pending_approvals || 0;
    if (count.isConnected) {
      count.textContent = pending;
      count.hidden = !pending;
    }
  } catch (error) {
    if (count.isConnected && state.user) notify(error.message, true);
  }
}, true);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    const group = $("#nav details[open]");
    if (group) {
      group.open = false;
      group.querySelector("summary").focus();
    } else if ($("#sidebar").classList.contains("open")) {
      setMobileNav(false);
      $(".menu-control").focus();
    }
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k"
      && !$("#shell").hidden && !$(".page-search").hidden && !$("#sheet").open) {
    event.preventDefault();
    if (window.matchMedia("(max-width: 760px)").matches) setMobileNav(true);
    $("#search").focus();
  }
  if (
    event.key === "Enter" &&
    event.target.matches("[data-action].requirement-card")
  )
    event.target.click();
});
$("#login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("button[type=submit]", $("#login"));
  submit.disabled = true;
  $("#login-error").textContent = "";
  try {
    let personalExpiry;
    if (state.loginMode === "account") {
      const result = await api("auth/login", "POST", {
        username: $("#username").value,
        password: $("#password").value,
      });
      state.token = result.token;
    } else if (state.loginMode === "pair") {
      state.token = $("#token").value;
      const result = await api("auth/pair", "POST", {});
      state.token = result.token;
      personalExpiry = result.expires_at;
    } else state.token = $("#token").value;
    await connect(personalExpiry);
    $("#password").value = "";
    $("#token").value = "";
  } catch (error) {
    $("#login-error").textContent = error.message;
    state.token = "";
  } finally {
    submit.disabled = false;
  }
});
document.addEventListener("submit", async (event) => {
  if (event.target.id === "audit-filters") {
    event.preventDefault();
    const filters = Object.fromEntries(new FormData(event.target));
    state.auditFilters = Object.fromEntries(
      Object.entries(filters)
        .filter(([, value]) => value.trim())
        .map(([key, value]) => [key, value.trim()]),
    );
    state.auditCursor = 0;
    await render();
  }
  if (event.target.id === "sheet-form") {
    event.preventDefault();
    const formEl = event.target;
    const handler = state.formHandler;
    const submit = $("button[type=submit]", formEl);
    submit.disabled = true;
    $("#form-error").textContent = "";
    try {
      if (await refreshIdentity()) {
        await render();
        notify("账号权限已更新，请重新打开表单。");
        return;
      }
      if (!formEl.isConnected || handler !== state.formHandler) return;
      await handler(Object.fromEntries(new FormData(formEl)));
    } catch (error) {
      if ($("#form-error")) $("#form-error").textContent = error.message;
      else notify(error.message, true);
    } finally {
      if (submit.isConnected) submit.disabled = false;
    }
  }
  if (event.target.id === "quick-task") {
    event.preventDefault();
    const formEl = event.target;
    const submit = $("button[type=submit]", formEl);
    submit.disabled = true;
    try {
      if (await refreshIdentity()) {
        await render();
        notify("账号权限已更新，请重新操作。");
        return;
      }
      if (!can("write")) throw Error("当前账号没有启动任务的权限");
      const data = Object.fromEntries(new FormData(formEl));
      data.materials = collectMaterials(formEl);
      if (data.template_id) {
        await newRun(data);
        return;
      }
      const plan = makePlan({...data,stage:"general"});
      const run = await api("runs", "POST", plan);
      state.runTab = run.mode === "single" ? "result" : "pipeline";
      go("runs", run.run_id);
    } catch (error) {
      $("#quick-error").textContent = error.message;
    } finally {
      if (submit.isConnected) submit.disabled = false;
    }
  }
  if (event.target.id === "node-form") {
    event.preventDefault();
    if (!can("write")) return;
    const data = Object.fromEntries(new FormData(event.target));
    const node = state.designer.nodes.find((n) => n.node_id === state.node);
    Object.assign(node, {
      label: data.label,
      kind: data.kind,
      stage: data.stage || node.stage || "review",
      role_id: data.kind === "model" ? data.role_id || undefined : undefined,
      group: data.kind === "model" ? data.group || null : null,
      dependencies: new FormData(event.target).getAll("dependencies"),
    });
    await render(true);
    notify("节点配置已应用，保存草稿后发布");
  }
});
$("#project").addEventListener("change", async (event) => {
  if ($("#sheet").open) closeSheet();
  state.generation++;
  state.currentRun = null;
  state.designer = null;
  state.traceRunId = null;
  state.project = event.target.value;
  state.runsCursor = 0;
  if (route().page === "agents") go("agents");
  else if (route().page !== "runs" || route().id) go("overview");
  await render();
});
$("#workspace-select").addEventListener("change", async (event) => {
  state.workspaceId = event.target.value;
  clearContext();
  sessionStorage.setItem("clauseweft-workspace", state.workspaceId);
  try {
    await refreshIdentity();
    await refreshCatalog();
    go("overview");
    await render();
  } catch (error) {
    notify(error.message, true);
  }
});
$("#search").addEventListener("input", () => {
  state.query = $("#search").value;
  if (route().page === "runs" && !route().id) {
    state.runsCursor = 0;
    state.generation++;
    $$('[data-action="runs-page"]').forEach(button => { button.disabled = true; });
  }
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => render(true), 250);
});
document.addEventListener("change", (event) => {
  if (event.target.form?.id === "quick-task" && event.target.name === "template_id") {
    $("#quick-task .submit-label").textContent = quickModes.find((mode) => mode.id === event.target.value).submit;
    const preferences = $("#quick-task .quick-preferences");
    if (preferences) {
      preferences.hidden = Boolean(event.target.value);
      if (preferences.hidden) $("input", preferences).checked = false;
    }
  }
  if (event.target.id === "trace-run") {
    state.traceRunId = event.target.value;
    render(true);
  }

  if ($("#task-options") && event.target.closest("#sheet-form") &&
      ["endpoint_id", "template_id", "role_id", "project_id", "tool_id"].includes(event.target.name)) syncTaskFields();
  if (event.target.name === "role" && $("#member-grants"))
    $("#member-grants").hidden = event.target.value === "admin";
  if (event.target.name === "workspace_ids" && $("#member-grants")) {
    const selected = new FormData($("#sheet-form")).getAll("workspace_ids");
    $$("[name=project_ids]", $("#member-grants")).forEach((input) => {
      input.disabled = !selected.includes(input.dataset.workspace);
      if (input.disabled) input.checked = false;
    });
  }
  if (event.target.id === "run-status") {
    state.status = event.target.value;
    state.runsCursor = 0;
    render(true);
  }
});
window.addEventListener("hashchange", () => {
  if ($("#sheet").open) closeSheet();
  closeNavGroups();
  setMobileNav(false);
  state.query = "";
  state.runsCursor = 0;
  $("#search").value = "";
  render();
});
window.addEventListener("resize", () => {
  const { page, id } = route();
  if (page === "runs" && id && state.currentRun)
    drawGraph(state.currentRun.nodes);
  if (page === "templates" && id && state.designer)
    drawGraph(state.designer.nodes);
});
let lastPoll = 0, polling = false;
setInterval(async () => {
  if (polling || !state.user || document.hidden || $("#sheet").open) return;
  const {page, id} = route();
  const editing = ["INPUT","TEXTAREA","SELECT"].includes(document.activeElement.tagName);
  if (editing && !(page === "overview" && $("#quick-task")?.contains(document.activeElement))) return;
  const agentRunning = ["agents","overview"].includes(page) && Boolean($("[data-agent-live]"));
  const live = agentRunning || page === "overview" && $(".trace-status[data-phase=running], .trace-status[data-phase=queued]")
    || page === "runs" && id && ["running","queued"].includes(state.currentRun?.status);
  if (Date.now() - lastPoll < (live ? 2000 : 6000)) return;
  lastPoll = Date.now();
  polling = true;
  try {
    const updating = ["overview","runs","approvals","usage","audit"].includes(page) || page === "agents" && (!id || agentRunning);
    if (updating && !(page === "runs" && id && ["review","context","checkpoints"].includes(state.runTab)))
      await render(true);
    else if (await refreshIdentity()) await render(true);
  } catch (error) {
    if (state.user) notify(error.message, true);
  } finally {
    polling = false;
  }
}, 1000);
if (state.token)
  connect().catch((error) => {
    disconnect();
    $("#login-error").textContent = error.message;
  });
