"""Text materials and image attachments supplied by the operator for a run."""

import base64
import binascii
import re


MAX_MATERIALS = 4
MAX_MATERIAL_BYTES = 64 * 1024
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
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


def validate_attachments(items):
    if not isinstance(items, list) or len(items) > MAX_ATTACHMENTS:
        raise ValueError(f"每次任务最多可附 {MAX_ATTACHMENTS} 张图片附件")
    total = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "media_type", "data_base64"}:
            raise ValueError("图片附件必须包含 name、media_type 和 data_base64")
        name = item["name"]
        media_type = item["media_type"]
        encoded = item["data_base64"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 160
                or re.search(r"[/\\\x00-\x1f\x7f]", name)):
            raise ValueError("附件名称须为 1 至 160 字符的文件名")
        if media_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError("附件仅支持 image/png、image/jpeg、image/webp 或 image/gif")
        if not isinstance(encoded, str) or not encoded or not encoded.isascii():
            raise ValueError("附件内容须为有效的 Base64 字符串")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("附件 Base64 解码失败") from None
        if not 1 <= len(raw) <= MAX_ATTACHMENT_BYTES:
            raise ValueError("单张图片附件大小须在 1 B 至 2 MiB 之间")
        total += len(raw)
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        raise ValueError("图片附件合计不能超过 4 MiB")
    return items


def material_messages(items):
    return [{"role": "user", "content": f"参考材料：{item['name']}\n{item['text']}"} for item in items]


def attachment_parts(items):
    return [
        {"type": "image_url", "image_url": {"url": f"data:{item['media_type']};base64,{item['data_base64']}"}}
        for item in items
    ]
