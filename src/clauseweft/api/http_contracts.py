"""Value, pagination and precondition contracts shared by HTTP resources."""

import uuid

from clauseweft.storage import canonical as encode

__all__ = ["Problem", "encode", "ident", "identifier_list", "match", "page", "require_object", "text"]


class Problem(Exception):
    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status = status
        self.code = code


def ident():
    return uuid.uuid4().hex


def require_object(data, allowed, required=()):
    if (
        not isinstance(data, dict)
        or data.keys() - set(allowed)
        or set(required) - data.keys()
    ):
        raise ValueError("缺少必要字段或存在不支持的字段")
    return data


def match(revision, expected):
    if expected is None:
        raise Problem(428, "IF_MATCH_REQUIRED", "请提交所查看资源的版本")
    if expected != revision:
        raise Problem(412, "REVISION_CHANGED", "资源已变化，请重新读取")


def page(items, query):
    limit = int(query.get("limit", 50))
    offset = int(query.get("cursor", 0))
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("分页参数无效")
    if query.get("status"):
        items = [item for item in items if item.get("status") == query["status"]]
    return {
        "items": items[offset : offset + limit],
        "next_cursor": offset + limit if len(items) > offset + limit else None,
    }


def text(value, field, *, empty=False, maximum=32000):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
    ):
        raise ValueError(f"{field} 须为有效文本")
    return value.strip()


def identifier_list(value, name):
    if (
        not isinstance(value, list)
        or len(value) > 10000
        or any(
            not isinstance(item, str) or not item or len(item) > 128
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{name} 须为不同资源 ID 的列表")
    return value
