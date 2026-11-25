"""Load a project's registered document source and search its records using SQLite FTS5."""

import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .decisions import checked_text

_WORD_OR_CJK = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_CJK_CHAR = re.compile(r"([\u4e00-\u9fff])")


def _read_reference_file(path, name, scope):
    if scope not in ("public", "local_only") or not path.is_absolute() or not path.is_file():
        raise ValueError("资料源须指定已登记的本机普通文件和有效数据范围")
    stat = path.stat()
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
    return {
        "name": name,
        "data_scope": scope,
        "digest": hashlib.sha256(raw).hexdigest(),
        "docs": docs,
        "path": str(path),
        "mtime_ns": stat.st_mtime_ns,
    }


def load_references(config):
    """Read only the files explicitly mapped to project IDs by the instance administrator."""
    references = {}
    for project_id, spec in config.items():
        if not {"path", "name"} <= spec.keys() <= {"path", "name", "data_scope"}:
            raise ValueError("项目资料源需要 path、name 和可选 data_scope")
        name = checked_text(spec["name"], 160, "资料源名称")
        path = Path(checked_text(spec["path"], 4096, "资料文件路径"))
        scope = spec.get("data_scope", "local_only")
        references[project_id] = _read_reference_file(path, name, scope)
    return references


def reload_if_changed(entry):
    """Hot-reload a registered reference file for new runs when its mtime changes."""
    path_text = entry.get("path")
    if not path_text:
        return False
    path = Path(path_text)
    if not path.is_file():
        return False
    mtime_ns = path.stat().st_mtime_ns
    if mtime_ns == entry.get("mtime_ns"):
        return False
    fresh = _read_reference_file(path, entry["name"], entry["data_scope"])
    entry.update(fresh)
    return True


def _fts_segment(text):
    return _CJK_CHAR.sub(r" \1 ", text)


def _fts_phrase(token):
    if _CJK_CHAR.search(token):
        chars = " ".join(ch for ch in token if ch.strip())
        return '"' + chars.replace('"', '""') + '"'
    return '"' + token.replace('"', '""') + '"'


def _excerpt(content, query, tokens):
    lower = content.casefold()
    offset = lower.find(query.casefold())
    if offset < 0:
        for tok in tokens:
            pos = lower.find(tok.casefold())
            if pos >= 0:
                offset = pos
                break
    start = max(0, offset - 70) if offset >= 0 else 0
    return content[start:start + 360]


def _format_hit(record, query, tokens):
    return {
        "id": record["id"],
        "title": record["title"],
        "excerpt": _excerpt(record["text"], query, tokens),
        **({"revision": record["revision"]} if "revision" in record else {}),
    }


def search(records, query):
    """Rank records with SQLite FTS5 BM25 (supporting both CJK and Latin) plus substring fallback."""
    records = list(records)
    if not records or not isinstance(query, str) or not query.strip():
        return []
    tokens = _WORD_OR_CJK.findall(query)
    hits = []
    seen = set()
    by_id = {record["id"]: record for record in records}

    # 1. SQLite FTS5 BM25 search (title weighted 5x over body text)
    if tokens:
        match_expr = " ".join(_fts_phrase(tok) for tok in tokens[:12])
        try:
            conn = sqlite3.connect(":memory:")
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE docs USING fts5(doc_id UNINDEXED, title, text, tokenize='unicode61')"
                )
                conn.executemany(
                    "INSERT INTO docs(doc_id, title, text) VALUES (?, ?, ?)",
                    [(r["id"], _fts_segment(r["title"]), _fts_segment(r["text"])) for r in records],
                )
                rows = conn.execute(
                    "SELECT doc_id FROM docs WHERE docs MATCH ? ORDER BY bm25(docs, 5.0, 1.0) ASC, rowid ASC LIMIT 8",
                    (match_expr,),
                ).fetchall()
                for (doc_id,) in rows:
                    if doc_id not in seen and doc_id in by_id:
                        seen.add(doc_id)
                        hits.append(_format_hit(by_id[doc_id], query, tokens))
            finally:
                conn.close()
        except sqlite3.OperationalError:
            pass

    # 2. Substring fallback (title matches ranked ahead of body-only matches)
    if len(hits) < 8:
        keyword = query.casefold()
        title_matches = []
        body_matches = []
        for record in records:
            if record["id"] in seen:
                continue
            if keyword in record["title"].casefold():
                title_matches.append(record)
            elif keyword in record["text"].casefold():
                body_matches.append(record)
        for record in [*title_matches, *body_matches]:
            seen.add(record["id"])
            hits.append(_format_hit(record, query, tokens))
            if len(hits) == 8:
                break
    return hits
