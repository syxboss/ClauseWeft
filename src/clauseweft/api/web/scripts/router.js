async function render(silent = false) {
  if (!state.user) return;
  const generation = ++state.generation;
  const { page, id } = route();
  if (!silent)
    $("#content").innerHTML =
      '<div class="skeleton skeleton-head"></div><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div>';
  try {
    if (await refreshIdentity()) return render(silent);
    if (generation !== state.generation || !state.user) return;
    nav();
    if (!state.workspaceId) {
      $("#content").innerHTML = empty("尚未分配工作空间", "请联系管理员分配工作空间和项目。账号不会自动获得全部数据权限。");
      return;
    }
    if (!can(pageCapabilities[page])) {
      state.currentRun = null;
      state.designer = null;
      $("#content").innerHTML = empty("没有访问权限", "当前角色不能访问此页面。请通过主导航查看已获授权的内容。");
      return;
    }
    const views = {
      overview,
      agents: () => externalAgentPage(id ? decodeURIComponent(id) : ""),
      projects,
      requirements,
      runs,
      templates,
      roles,
      skills,
      endpoints,
      approvals,
      usage,
      audit,
      settings,
    };
    const html =
      page === "runs" && id
        ? await runDetail(decodeURIComponent(id), generation)
        : page === "templates" && id
          ? await templateDetail(decodeURIComponent(id))
          : await views[page]();
    if (generation !== state.generation) return;
    const content = $("#content");
    if (silent && page === "overview" && ($("#quick-task")?.contains(document.activeElement)
        || $("#quick-task .task-materials[open]"))) {
      // Keep the live form mounted while the operator types, including its caret
      // and IME composition. Only the read-only regions need the new snapshot.
      const next = document.createElement("template");
      next.innerHTML = html;
      for (const selector of [".desk-trace", ".desk-totals", ".desk-columns"]) {
        const replacement = next.content.querySelector(selector);
        if (replacement) content.querySelector(selector)?.replaceWith(replacement);
      }
    } else {
      content.innerHTML = html;
    }
    if (page === "runs" && id) drawGraph(state.currentRun.nodes);
    if (page === "templates" && id) drawGraph(state.designer.nodes);

  } catch (error) {
    if (generation === state.generation) {
      if (!state.user) return;
      state.currentRun = null;
      state.designer = null;
      $("#content").innerHTML = empty(
        [403, 404].includes(error.status) ? "内容不可访问" : "页面暂时无法加载",
        error.message,
        "refresh",
        "重新加载",
      );
      notify(error.message, true);
    }
  }
}
