function skillChoices(selected = [], name = "skill_ids") {
  const skills = state.skills.filter(
    (item) => item.enabled || selected.includes(item.id),
  );
  return `<fieldset class="skill-selection"><legend>关联技能</legend>${
    skills.length
      ? skills
          .map(
            (item) =>
              `<label class="check"><input type="checkbox" name="${name}" value="${esc(item.id)}" ${selected.includes(item.id) ? "checked" : ""}><span>${esc(item.name)} <small>v${item.revision} · ${item.data_scope === "local_only" ? "仅本地" : "允许外发"}${item.enabled ? "" : " · 已停用"}</small></span></label>`,
          )
          .join("")
      : '<p class="field-help">技能库中尚无技能，可以先创建并保存团队操作规范。</p>'
  }<p class="field-help">运行会冻结技能内容与版本，实际派发遵守技能的数据范围。</p></fieldset>`;
}
function selectedSkills(form = $("#sheet-form")) {
  return new FormData(form).getAll("skill_ids");
}
function personalPreferenceChoice(checked = false) {
  if (!state.workspace?.has_personal_preferences) return "";
  return field("use_preferences", "本次使用我的偏好", checked, "checkbox", {
    help: "偏好会发送给模型，并写入项目成员可查看的任务上下文。"
  });
}
async function editPersonalPreferences() {
  const path = "me/preferences/" + encodeURIComponent(state.workspaceId);
  const saved = await api(path);
  form("我的偏好",
    field("instructions", "在「" + state.workspace.name + "」中的长期偏好", saved.instructions, "textarea", {
      rows: 6, placeholder: "例如：默认用简体中文，先给结论，再列出依据。",
      help: "最多 4,000 字。保存后仅自己可见。在提交任务或追问时勾选，才会带入该次请求。"
    }) + (saved.instructions ? button("clear-preferences", "清空输入") : ""),
    async (data) => {
      await api(path, "PATCH", {instructions: data.instructions}, saved.revision);
      state.workspace.has_personal_preferences = Boolean(data.instructions.trim());
      closeSheet();
      notify(state.workspace.has_personal_preferences ? "个人偏好已保存" : "个人偏好已清空");
      await render();
    }, "保存偏好");
  $("#sheet-form [name=instructions]").maxLength = 4000;
}
async function rememberPoint() {
  const run = state.currentRun;
  if (!run || run.workspace_id !== state.workspaceId) return;
  const path = "me/preferences/" + encodeURIComponent(run.workspace_id);
  const workspace = state.workspace.name;
  function review(saved, draft = "") {
    const remaining = 4000 - Array.from(saved.instructions).length - (saved.instructions ? 1 : 0);
    form("记住要点",
      `<p class="description">保存到「${esc(workspace)}」的个人偏好。</p>
       <details class="record-subdetails" open><summary>已有偏好</summary><pre>${esc(saved.instructions || "尚未保存偏好")}</pre></details>`
      + field("point", "要追加的要点", draft, "textarea", {
        required: true, rows: 3, placeholder: "用自己的话写下一条以后要沿用的做法",
        help: `还可追加 ${Math.max(0, remaining)} 字。只保存这里填写的文字。`
      }), async data => {
        const point = data.point.trim();
        if (!point || Array.from(point).length > remaining) throw Error("请填写要点，并确保追加后的偏好在 4,000 字以内。");
        const instructions = saved.instructions ? saved.instructions + "\n" + point : point;
        try {
          await api(path, "PATCH", {instructions}, saved.revision);
        } catch (error) {
          if (error.status !== 412) throw error;
          review(await api(path), point);
          $("#form-error").textContent = "已有偏好刚刚更新，请核对后再追加。";
          return;
        }
        state.workspace.has_personal_preferences = true;
        closeSheet();
        notify("要点已追加到个人偏好");
        await render();
      }, "追加到我的偏好");
  }
  review(await api(path));
}
function matches(value) {
  return (
    !state.query ||
    pretty(value).toLowerCase().includes(state.query.toLowerCase())
  );
}
function route() {
  const [page = "overview", id] = location.hash.slice(1).split("/");
  return { page: Object.hasOwn(labels, page) ? page : "overview", id };
}
function go(page, id) {
  location.hash = page + (id ? "/" + encodeURIComponent(id) : "");
}
function empty(title, description = "", action = "", label = "创建") {
  if (!actionAllowed(action)) action = "";
  return /* HTML */ `<div class="empty">
    <div class="empty-icon">◇</div>
    <h3>${esc(title)}</h3>
    <p>${esc(description)}</p>
    ${action ? `<button data-action="${action}">${esc(label)}</button>` : ""}
  </div>`;
}
function heading(title, description = "", actions = "") {
  return /* HTML */ `<div class="page-heading">
    <div>
      <h1>${esc(title)}</h1>
      ${description ? `<p>${esc(description)}</p>` : ""}
    </div>
    <div class="actions">${actions}</div>
  </div>`;
}
function table(headers, rows) {
  return /* HTML */ `<div class="table-wrap">
    <table>
      <thead>
        <tr>
          ${headers.map((h) => `<th>${esc(h)}</th>`).join("")}
        </tr>
      </thead>
      <tbody>
        ${rows.join("")}
      </tbody>
    </table>
  </div>`;
}
function button(action, label, id = "", cls = "") {
  if (!actionAllowed(action)) return "";
  return /* HTML */ `<button
    type="button"
    class="${cls}"
    data-action="${action}"
    data-id="${esc(id)}"
  >
    ${label}
  </button>`;
}
function options(items, value) {
  return items
    .map(
      ([v, l]) =>
        `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(l)}</option>`,
    )
    .join("");
}
function field(name, label, value = "", type = "text", extra = {}) {
  const help = extra.help
    ? `<span class="field-help">${esc(extra.help)}</span>`
    : "";
  if (type === "checkbox")
    return /* HTML */ `<label class="check ${extra.span ? "span" : ""}"
      ><input type="checkbox" name="${name}" ${value ? "checked" : ""} />${esc(
        label,
      )}${help}</label
    >`;
  const required = extra.required ? "required" : "";
  let control =
    type === "select"
      ? `<select name="${name}" ${required}>${options(extra.options || [], value)}</select>`
      : type === "textarea"
        ? `<textarea name="${name}" ${required} ${extra.rows ? `rows="${extra.rows}"` : ""} placeholder="${esc(extra.placeholder || "")}">${esc(value)}</textarea>`
        : `<input name="${name}" type="${type}" value="${esc(value)}" ${required} ${extra.min !== undefined ? `min="${extra.min}"` : ""} ${extra.max !== undefined ? `max="${extra.max}"` : ""} ${extra.step ? `step="${extra.step}"` : ""} placeholder="${esc(extra.placeholder || "")}">`;
  return /* HTML */ `<label class="${extra.span ? "span" : ""}"
    >${esc(label)}${control}${help}</label
  >`;
}
function sheet(title, body, wide = false) {
  $("#sheet-title").textContent = title;
  $("#sheet-body").innerHTML = body;
  $("#sheet").classList.toggle("wide", wide);
  if (!$("#sheet").open) $("#sheet").showModal();
}
function closeSheet() {
  $("#sheet").close();
  $("#sheet-body").replaceChildren();
  state.formHandler = null;
}
function form(title, body, handler, saveLabel = "保存", wide = false) {
  state.formHandler = handler;
  sheet(
    title,
    `<form id="sheet-form">${body}<p class="form-error" role="alert" id="form-error"></p><footer class="form-actions">${button("close-sheet", "取消")}<button class="primary" type="submit">${esc(saveLabel)}</button></footer></form>`,
    wide,
  );
}
function reasonForm(title, handler, fields = "") {
  form(
    title,
    fields +
      field("reason", "处理依据", "", "textarea", {
        required: true,
        placeholder: "说明本次决定的依据，记录会保留在运行历史中",
      }),
    async (data) => handler(data),
    "确认",
  );
}
