"""Text supplied by the operator for this run. Names never identify server files."""

import re


MAX_MATERIALS = 4
MAX_MATERIAL_BYTES = 64 * 1024
_BINARY_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_materials(items):
    if not isinstance(items, list) or len(items) > MAX_MATERIALS:
        raise ValueError("每次任务最多可附 4 份文本材料")
    size = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "text"}:
            raise ValueError("文本材料必须包含 name 和 text")
        name, text = item["name"], item["text"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 160
                or re.search(r"[/\\\x00-\x1f\x7f]", name)):
            raise ValueError("材料名称须为 1 至 160 字符的文件名")
        if not isinstance(text, str) or not text.strip() or _BINARY_CONTROLS.search(text):
            raise ValueError("材料正文须为非空 UTF-8 纯文本")
        try:
            name.encode("utf-8")
            size += len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("材料名称和正文须为有效 UTF-8 文本") from None
    if size > MAX_MATERIAL_BYTES:
        raise ValueError("文本材料合计超过 64 KiB")
    return items


def material_messages(items):
    return [{"role": "user", "content": f"参考材料：{item['name']}\n{item['text']}"} for item in items]
