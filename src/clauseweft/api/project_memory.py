"""Project-scoped structured long-term memory with revision history and data-scope controls."""

import time

from clauseweft import audit
from .http_contracts import Problem, ident, match, require_object

CATEGORIES = ("convention", "glossary", "decision", "lesson")
MAX_MEMORIES_PER_PROJECT = 128
MAX_FROZEN_MEMORIES = 16
MAX_MEMORY_BYTES = 16 * 1024


def init_schema(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS project_memories (
               id TEXT PRIMARY KEY,
               project_id TEXT NOT NULL,
               workspace_id TEXT NOT NULL,
               category TEXT NOT NULL,
               title TEXT NOT NULL,
               content TEXT NOT NULL,
               data_scope TEXT NOT NULL,
               enabled INTEGER NOT NULL,
               revision INTEGER NOT NULL,
               created REAL NOT NULL,
               updated REAL NOT NULL
           )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS project_memories_project_idx ON project_memories(project_id, updated)"
    )


def _format(row):
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workspace_id": row["workspace_id"],
        "category": row["category"],
        "title": row["title"],
        "content": row["content"],
        "data_scope": row["data_scope"],
        "enabled": bool(row["enabled"]),
        "revision": row["revision"],
        "created": row["created"],
        "updated": row["updated"],
    }


def _validate_fields(data, *, partial=False):
    allowed = ("category", "title", "content", "data_scope", "enabled")
    required = () if partial else ("category", "title", "content")
    require_object(data, allowed, required)
    cleaned = {}
    if "category" in data:
        if data["category"] not in CATEGORIES:
            raise ValueError("记忆分类须为 convention、glossary、decision 或 lesson")
        cleaned["category"] = data["category"]
    if "title" in data:
        title = data["title"]
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
            raise ValueError("记忆标题须为 1 至 160 字符")
        cleaned["title"] = title.strip()
    if "content" in data:
        content = data["content"]
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 4096:
            raise ValueError("记忆内容须为非空文本且不超过 4 KiB")
        cleaned["content"] = content.strip()
    if "data_scope" in data:
        if data["data_scope"] not in ("local_only", "public"):
            raise ValueError("data_scope 须为 local_only 或 public")
        cleaned["data_scope"] = data["data_scope"]
    if "enabled" in data:
        if type(data["enabled"]) is not bool:
            raise ValueError("enabled 须为布尔值")
        cleaned["enabled"] = data["enabled"]
    if partial and not cleaned:
        raise ValueError("至少需要提供一个可更新字段")
    return cleaned


def list_memories(db, project_id, query=None):
    query = query or {}
    clauses = ["project_id=?"]
    params = [project_id]
    if category := query.get("category"):
        if category not in CATEGORIES:
            raise ValueError("记忆分类无效")
        clauses.append("category=?")
        params.append(category)
    if "enabled" in query:
        flag = query["enabled"]
        if flag not in ("true", "false", True, False):
            raise ValueError("enabled 筛选须为 true 或 false")
        clauses.append("enabled=?")
        params.append(1 if flag in ("true", True) else 0)
    sql = f"SELECT * FROM project_memories WHERE {' AND '.join(clauses)} ORDER BY updated DESC, id ASC"
    return [_format(row) for row in db.execute(sql, params)]


def get(db, project_id, memory_id):
    row = db.execute(
        "SELECT * FROM project_memories WHERE id=? AND project_id=?",
        (memory_id, project_id),
    ).fetchone()
    if row is None:
        raise Problem(404, "MEMORY_NOT_FOUND", "项目记忆条目不存在")
    return _format(row)


def create(db, project, data, actor):
    if project["status"] != "active":
        raise Problem(409, "PROJECT_ARCHIVED", "归档项目不能新增记忆条目")
    cleaned = _validate_fields(data, partial=False)
    count = db.execute(
        "SELECT count(*) FROM project_memories WHERE project_id=?", (project["id"],)
    ).fetchone()[0]
    if count >= MAX_MEMORIES_PER_PROJECT:
        raise Problem(409, "MEMORY_LIMIT", f"单个项目最多保存 {MAX_MEMORIES_PER_PROJECT} 条记忆")
    mid = "mem_" + ident()[:12]
    now = time.time()
    item = {
        "id": mid,
        "project_id": project["id"],
        "workspace_id": project["workspace_id"],
        "category": cleaned["category"],
        "title": cleaned["title"],
        "content": cleaned["content"],
        "data_scope": cleaned.get("data_scope", "local_only"),
        "enabled": cleaned.get("enabled", True),
        "revision": 1,
        "created": now,
        "updated": now,
    }
    db.execute(
        """INSERT INTO project_memories
           (id, project_id, workspace_id, category, title, content, data_scope, enabled, revision, created, updated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item["id"], item["project_id"], item["workspace_id"], item["category"],
            item["title"], item["content"], item["data_scope"], int(item["enabled"]),
            item["revision"], item["created"], item["updated"],
        ),
    )
    audit.save_revision(db, "project_memories", mid, 1, item, actor=actor)
    return item


def update(db, project, memory_id, data, expected, actor):
    if project["status"] != "active":
        raise Problem(409, "PROJECT_ARCHIVED", "归档项目不能修改记忆条目")
    before = get(db, project["id"], memory_id)
    match(before["revision"], expected)
    cleaned = _validate_fields(data, partial=True)
    updated = dict(before, **cleaned, revision=before["revision"] + 1, updated=time.time())
    db.execute(
        """UPDATE project_memories
           SET category=?, title=?, content=?, data_scope=?, enabled=?, revision=?, updated=?
           WHERE id=? AND project_id=?""",
        (
            updated["category"], updated["title"], updated["content"], updated["data_scope"],
            int(updated["enabled"]), updated["revision"], updated["updated"],
            memory_id, project["id"],
        ),
    )
    audit.save_revision(db, "project_memories", memory_id, updated["revision"], updated, actor=actor, before=before)
    return updated


def freeze(db, project_id, *, memory_ids=None):
    """Freeze project-scoped enabled memories for a run snapshot."""
    if memory_ids is not None:
        if (not isinstance(memory_ids, list) or not 1 <= len(memory_ids) <= MAX_FROZEN_MEMORIES
                or any(not isinstance(mid, str) or not mid.strip() for mid in memory_ids)
                or len(set(memory_ids)) != len(memory_ids)):
            raise ValueError(f"memory_ids 须为 1 至 {MAX_FROZEN_MEMORIES} 个不重复的记忆 ID")
        rows = []
        for mid in memory_ids:
            item = get(db, project_id, mid)
            if not item["enabled"]:
                raise Problem(409, "MEMORY_DISABLED", f"项目记忆 {mid} 已停用")
            rows.append(item)
    else:
        rows = list_memories(db, project_id, {"enabled": True})[:MAX_FROZEN_MEMORIES]
    snapshots = []
    total_bytes = 0
    for item in rows:
        size = len(item["title"].encode("utf-8")) + len(item["content"].encode("utf-8"))
        if total_bytes + size > MAX_MEMORY_BYTES:
            if memory_ids is not None:
                raise ValueError("所选项目记忆合计超过 16 KiB")
            break
        total_bytes += size
        snapshots.append({
            "id": item["id"],
            "category": item["category"],
            "title": item["title"],
            "content": item["content"],
            "data_scope": item["data_scope"],
            "revision": item["revision"],
        })
    return snapshots
