"""Self-contained ClauseWeft workbench, shared by the source and packaged CLI."""

from base64 import b64encode
from hashlib import sha256
from string import Template

from .console_app import SCRIPT
from .console_styles import STYLE
from .web import load_assets

WORDMARK = '<span class="signal-mark" aria-hidden="true"><i></i><i></i></span>ClauseWeft'
BROWSER_SCRIPT = SCRIPT + "\n"
SCRIPT_HASH = "sha256-" + b64encode(sha256(BROWSER_SCRIPT.encode("utf-8")).digest()).decode("ascii")
PAGE = Template(load_assets("page.html")).substitute(
    wordmark=WORDMARK,
    style=STYLE,
    browser_script=BROWSER_SCRIPT,
)
