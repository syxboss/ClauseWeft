"""Recognize common credentials at file, log and export boundaries."""
import re

FIELD = re.compile(r"(?:password|passphrase|(?:client[_-]?)?secret|secret[_-]?key|api[_-]?key|"
                   r"(?:access[_-]?|refresh[_-]?|auth[_-]?)?token|authorization)", re.I)
_PEM = re.compile(r"-----BEGIN [^\n-]*PRIVATE KEY-----.*?(?:-----END [^\n-]*PRIVATE KEY-----|\Z)", re.S)
_TOKEN = re.compile(r"(?:sk-(?:proj-)?|gh[pousr]_|github_pat_|glpat-)[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}")
_AUTH = re.compile(r"(\b(?:Bearer|Basic)[ \t]+)[A-Za-z0-9_~+/=.:-]+", re.I)
_ASSIGN = re.compile(r"((?:" + FIELD.pattern + r")[\x22']?[ \t]*[:=][ \t]*[\x22']?)"
                     r"([^\s,;\x22'}]+)", re.I)
_URL = re.compile(r"(\b[a-z][a-z0-9+.-]{0,16}://)[^\s/@:]+:[^\s/@]+@", re.I)


def redact(text):
    text = _PEM.sub("[redacted]", text)
    text = _TOKEN.sub("[redacted]", text)
    text = _AUTH.sub(r"\1[redacted]", text)
    text = _URL.sub(r"\1[redacted]@", text)
    return _ASSIGN.sub(r"\1[redacted]", text)


def contains(raw):
    text = raw.decode("utf-8", errors="replace")
    return redact(text) != text
