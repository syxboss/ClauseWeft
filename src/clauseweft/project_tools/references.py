"""Load a project's registered document source and search its records."""

import hashlib
import json
from pathlib import Path

from .decisions import checked_text


def load_references(config):
    """Read only the files explicitly mapped to project IDs by the instance administrator."""
    references = {}
    for project_id, spec in config.items():
        if not {"path", "name"} <= spec.keys() <= {"path", "name", "data_scope"}:
            raise ValueError("项目资料源需要 path、name 和可选 data_scope")
        name = checked_text(spec["name"], 160, "资料源名称")
        path = Path(checked_text(spec["path"], 4096, "资料文件路径"))
        scope = spec.get("data_scope", "local_only")
        if scope not in ("public", "local_only") or not path.is_absolute() or not path.is_file():
            raise ValueError("资料源须指定已登记的本机普通文件和有效数据范围")
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("单个资料源最大为 1 MiB")
        rows = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(rows, list) or len(rows) > 2000:
            raise ValueError("资料源须为最多 2000 篇记录的 JSON 数组")
        docs = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "title", "text"}:
                raise ValueError("资料条目须含 id、title、text")
            identifier = checked_text(row["id"], 128, "文档 ID")
            if identifier in docs:
                raise ValueError("资料源内文档 ID 重复")
            docs[identifier] = {
                "id": identifier,
                "title": checked_text(row["title"], 160, "文档标题"),
                "text": checked_text(row["text"], 32000, "文档正文"),
            }
        references[project_id] = dict(
            name=name, data_scope=scope, digest=hashlib.sha256(raw).hexdigest(), docs=docs
        )
    return references


def search(records, query):
    hits = []
    keyword = query.casefold()
    for record in records:
        title, content = record["title"], record["text"]
        offset = content.casefold().find(keyword)
        if keyword in title.casefold() or offset >= 0:
            start = max(0, offset - 70)
            hits.append({
                "id": record["id"], "title": title, "excerpt": content[start:start + 360],
                **({"revision": record["revision"]} if "revision" in record else {}),
            })
            if len(hits) == 8:
                break
    return hits
