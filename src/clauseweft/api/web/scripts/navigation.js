async function refreshCatalog() {
  const epoch = state.accessEpoch;
  const workspaces = await collection("workspaces");
  if (epoch !== state.accessEpoch) return;
  state.workspaces = workspaces;
  if (!state.workspaces.some((item) => item.id === state.workspaceId)) {
    state.workspaceId = state.workspaces[0]?.id || "";
    state.traceRunId = null;
    state.project = "";
  }
  sessionStorage.setItem("clauseweft-workspace", state.workspaceId);
  $("#workspace-select").innerHTML = options(
    state.workspaces.map((item) => [item.id, item.name]), state.workspaceId,
  );
  if (!state.workspaceId) {
    $("#project").innerHTML = options([["", "暂无项目访问权限"]], "");
    return;
  }
  const query = workspaceQuery();
  [
    state.projects,
    state.roles,
    state.skills,
    state.templates,
    state.workspace,
  ] = await Promise.all([
    collection("projects" + query),
    can("write") ? collection("roles" + query) : [],
    can("write") ? collection("skills" + query) : [],
    can("write") ? collection("templates" + query) : [],
    api("workspace" + query),
  ]);
  state.endpoints = can("admin")
    ? await collection("endpoints")
    : state.workspace.model_options || [];
  state.modelOptions = state.workspace.model_options || [];
  state.workspace.repositories ||= [];
  state.workspace.suites ||= [];
  if (state.project && !state.projects.some((p) => p.id === state.project)) {
    state.traceRunId = null;
    state.project = "";
  }
  $("#project").innerHTML = options(
    [["", "空间内所有项目"], ...state.projects.map((p) => [p.id, p.name])],
    state.project,
  );
}
function setMobileNav(open) {
  $("#sidebar").classList.toggle("open", open);
  $('.menu-control').setAttribute("aria-expanded", String(open));
}
function closeNavGroups(except) {
  $$("#nav details[open]").forEach(group => {
    if (group !== except) group.open = false;
  });
}
function nav() {
  const {page, id} = route();
  const link = key => `<a href="#${key}" class="nav-link${key === page ? " active" : ""}"${key === page ? ' aria-current="page"' : ""}>${icon(key)}<span>${labels[key]}</span>${key === "approvals" ? '<small class="nav-count" id="approval-count" aria-label="待审批项" hidden></small>' : ""}</a>`;
  const allowed = pages => pages.filter(key => can(pageCapabilities[key]));
  const group = (name, pages) => {
    const items = allowed(pages);
    if (!items.length) return "";
    const current = items.includes(page);
    return `<details class="nav-disclosure"><summary class="${current ? "active" : ""}" aria-label="${name}">${icon(name === "协作" ? "templates" : "settings")}<span>${name}</span></summary><div class="nav-popover">${items.map(link).join("")}</div></details>`;
  };
  const personal = state.user?.personal_owner;
  const markup = allowed(["overview", "projects", "runs", "agents"]).map(link).join("")
    + group(personal ? "工作流" : "协作", ["requirements", "templates", "roles", "skills", "approvals"])
    + group(personal ? "设置" : "资源与管理", personal ? ["endpoints", "usage", "audit"] : ["endpoints", "usage", "audit", "settings"]);
  const container = $("#nav");
  if (container.navMarkup !== markup) {
    container.innerHTML = markup;
    container.navMarkup = markup;
  }
  document.title = (labels[page] || "工作台") + " · ClauseWeft";
  $(".page-search").hidden = Boolean(id) || ["settings"].includes(page);
  const searchLabel = page === "runs" && !id ? "搜索任务标题或目标" : "筛选当前页";
  $("#search").placeholder = searchLabel;
  $("#search").setAttribute("aria-label", searchLabel);
  $(".top-actions [data-action=new-run]").hidden = !can("write") || !state.workspaceId;
  const example = state.workspace?.is_example || state.workspaceId === "example";
  $("#workspace-banner").hidden = !example;
  $("#workspace-banner").textContent = example
    ? "示例工作空间 · 这里的数据用于了解流程，与你的业务项目分开展示。"
    : "";
  $("#user-name").textContent = state.user.display_name || state.user.username;
  $("#user-role").textContent = personal ? "个人工作台" : roleLabels[state.user.role] || state.user.role;
  $("#avatar").textContent = (
    state.user.display_name ||
    state.user.username ||
    "A"
  )
    .slice(0, 1)
    .toUpperCase();
}
