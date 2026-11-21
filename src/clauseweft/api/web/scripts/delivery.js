async function runAction(path, body) {
  const run = await api("runs/" + state.currentRun.run_id);
  await api("runs/" + run.run_id + "/" + path, "POST", body, run.revision);
  closeSheet();
  notify("操作已提交");
  await render();
}
async function approveBaseline() {
  const run = state.currentRun;
  reasonForm("批准需求基线", (data) =>
    runAction("approvals", {
      kind: "baseline",
      version: run.spec_version_id,
      reason: data.reason,
    }),
  );
}
async function reviseSpec() {
  const run = state.currentRun;
  const specs = await collection("runs/" + run.run_id + "/specs");
  const current = specs.find((s) => s.version === run.spec_version_id);
  state.editClauses = (
    current?.clauses || [{ id: "R1", text: "", acceptance: "" }]
  ).map((c) => ({
    ...c,
    acceptance: Array.isArray(c.acceptance)
      ? c.acceptance.join("\n")
      : c.acceptance,
  }));
  form(
    "修订需求条款",
    `<div id="clause-fields">${state.editClauses.map((c, i) => `<section class="artifact-section">${field("id_" + i, "条款 ID", c.id, "text", { required: true })}${field("text_" + i, "行为描述", c.text, "textarea", { required: true })}${field("acceptance_" + i, "验收条件", c.acceptance, "textarea", { required: true })}</section>`).join("")}</div>${button("add-clause", "＋ 添加条款")}${field("reason", "修订理由", "", "textarea", { required: true })}`,
    async (data) => {
      const clauses = Object.keys(data)
        .filter((k) => k.startsWith("id_"))
        .map((k) => {
          const i = k.slice(3);
          return {
            id: data[k],
            text: data["text_" + i],
            acceptance: data["acceptance_" + i],
          };
        });
      await runAction("specs", {
        parent: current?.version || null,
        clauses,
        reason: data.reason,
      });
    },
    "保存并重新评审",
    true,
  );
}
function renderArtifact(content) {
  let value = content;
  try {
    if (typeof value === "string") value = JSON.parse(value);
  } catch {}
  if (value && typeof value === "object" && typeof value.patch === "string")
    return /* HTML */ `<div class="artifact-section">
        <h3>补丁差异</h3>
        ${diff(value.patch)}
      </div>
      <details>
        <summary>完整产物</summary>
        <pre>${esc(pretty(value))}</pre>
      </details>`;
  if (value?.candidate?.patch)
    return /* HTML */ `<div class="artifact-section">
        <h3>候选补丁</h3>
        ${diff(value.candidate.patch)}
      </div>
      <details>
        <summary>完整证据</summary>
        <pre>${esc(pretty(value))}</pre>
      </details>`;
  if (value?.patch && typeof value.patch === "object")
    return /* HTML */ `<pre>${esc(pretty(value))}</pre>`;
  if (value?.cases)
    return /* HTML */ `<div class="actions">
        ${badge(value.status)}<span class="muted mono"
          >${esc(value.candidate_sha?.slice(0, 12) || "")}</span
        >
      </div>
      ${table(
        ["必测项", "条款", "结果", "退出码"],
        value.cases.map(
          (c) =>
            `<tr><td>${esc(c.id)}</td><td>${esc(c.clause_id || "—")}</td><td>${badge(c.status)}</td><td>${esc(c.exit_code ?? "—")}</td></tr>`,
        ),
      )}
      <details>
        <summary>完整执行记录</summary>
        <pre>${esc(pretty(value))}</pre>
      </details>`;
  return /* HTML */ `<div class="result-prose">${esc(pretty(value))}</div>`;
}
function diff(patch) {
  return /* HTML */ `<pre class="diff">
${patch
      .split("\n")
      .map(
        (line) =>
          `<span class="diff-line ${line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@") ? "header" : line.startsWith("+") ? "add" : line.startsWith("-") ? "remove" : ""}">${esc(line) || " "}</span>`,
      )
      .join("")}</pre
  >`;
}
