
function savedMaterials(form) {
  const field = form?.querySelector("[data-material-json]");
  return field ? JSON.parse(field.value) : [];
}
function collectMaterials(form) {
  if (form.querySelector(".task-materials[aria-busy=true]")) throw Error("正在读取材料，请稍候");
  return savedMaterials(form);
}
function materialList(items, editable = false) {
  return items.map((item, index) => `<div class="material-item"><details>
    <summary>${esc(item.name)} <small>${new Blob([item.text]).size} B</small></summary>
    <pre>${esc(item.text)}</pre></details>${editable ? `<button type="button" class="text-button" data-material-remove="${index}" aria-label="移除 ${esc(item.name)}">移除</button>` : ""}</div>`).join("");
}
function materialPicker(items = []) {
  const target = state.workspace?.data_scope === "local_only" ? "仅限本地模型" : "可能使用获授权的云端模型";
  return `<details class="task-materials"${items.length ? " open" : ""}>
    <summary>引用文本材料 <span data-material-count>${items.length ? "（" + items.length + "）" : ""}</span></summary>
    <p class="field-help">选择 UTF-8 文本，最多 4 份、合计 64 KiB。预览路由或提交时，材料全文随任务发送到工作空间；执行时每个模型步骤都能读取，${target}。</p>
    <input type="file" data-material-files aria-label="选择 UTF-8 文本文件" multiple accept="text/*,.md,.csv,.json,.jsonl,.yaml,.yml,.toml,.log,.xml,.py,.js,.ts,.sql,.diff,.patch">
    <input type="hidden" data-material-json value="${esc(JSON.stringify(items))}">
    <div data-material-list>${materialList(items, true)}</div><p role="status" class="field-help" data-material-status></p>
  </details>`;
}
function updateMaterialList(group, items) {
  group.querySelector("[data-material-json]").value = JSON.stringify(items);
  group.querySelector("[data-material-list]").innerHTML = materialList(items, true);
  group.querySelector("[data-material-count]").textContent = items.length ? "（" + items.length + "）" : "";
}
async function readMaterialFiles(files, existing) {
  if (files.length + existing.length > 4) throw Error("最多可附 4 份材料");
  const size = existing.reduce((sum, item) => sum + new Blob([item.text]).size, 0)
             + files.reduce((sum, file) => sum + file.size, 0);
  if (size > 65536) throw Error("材料合计超过 64 KiB，请缩小内容后重选");
  const result = [...existing];
  for (const file of files) {
    if (!file.name.trim() || [...file.name].length > 160 || /[/\\\x00-\x1f\x7f]/.test(file.name))
      throw Error("材料名称须为 1 至 160 字符的文件名");
    const bytes = await file.arrayBuffer();
    let text;
    try { text = new TextDecoder("utf-8", {fatal:true}).decode(bytes); }
    catch { throw Error(file.name + " 的编码不是 UTF-8"); }
    if (!text.trim() || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text))
      throw Error(file.name + " 须为非空纯文本");
    result.push({name:file.name, text});
  }
  return result;
}
document.addEventListener("change", async event => {
  const input = event.target;
  if (!input.matches("[data-material-files]")) return;
  const group = input.closest(".task-materials"), status = group.querySelector("[data-material-status]");
  const files = Array.from(input.files);
  if (!files.length) return;
  group.setAttribute("aria-busy", "true");
  input.disabled = true;
  status.textContent = "正在读取材料…";
  try {
    const items = await readMaterialFiles(files, savedMaterials(input.form));
    updateMaterialList(group, items);
    status.textContent = "已添加 " + files.length + " 份，可展开文件名核对全文。";
  } catch (error) {
    status.textContent = "本次选择未添加。" + error.message;
  } finally {
    input.value = "";
    input.disabled = false;
    group.removeAttribute("aria-busy");
  }
});
document.addEventListener("click", event => {
  const control = event.target.closest("[data-material-remove]");
  if (!control) return;
  const group = control.closest(".task-materials");
  if (group.hasAttribute("aria-busy")) return;
  const items = savedMaterials(control.form);
  items.splice(Number(control.dataset.materialRemove), 1);
  updateMaterialList(group, items);
  group.querySelector("[data-material-status]").textContent = "已移除材料";
  group.querySelector("[data-material-files]").focus();
});
function presentAnswer(value, run, artifact) {
  const ref = run.run_id + "/artifacts/" + artifact;
  const remember = ["single", "complex"].includes(run.mode) && run.status === "completed"
    && state.user?.id && state.user.id !== "bootstrap-admin" && state.workspaceId === run.workspace_id;
  return `<div class="record-answer">${esc(pretty(value))}</div><div class="answer-actions">
    ${button("copy-answer", "复制结果", ref, "text-button")}${button("download-answer", "下载文本", ref, "text-button")}${remember ? button("remember-point", "记住要点", "", "text-button") : ""}</div>`;
}
async function savedAnswer(ref) {
  const result = await api("runs/" + ref);
  return pretty(result.content);
}
async function copyAnswer(ref) {
  const text = await savedAnswer(ref);
  try {
    await navigator.clipboard.writeText(text);
    notify("结果已复制");
  } catch {
    const answer = document.querySelector(".record-answer");
    if (answer && answer.textContent === text.replace(/\r\n?/g, "\n")) {
      const selection = window.getSelection();
      selection.selectAllChildren(answer);
      notify("已选中结果，请使用系统复制操作。");
    } else throw Error("浏览器未授予剪贴板权限，可以下载文本。");
  }
}
async function downloadAnswer(ref) {
  const text = await savedAnswer(ref);
  downloadBlob(new Blob([text], {type:"text/plain;charset=utf-8"}), "clauseweft-" + ref.split("/")[0] + ".txt");
}
