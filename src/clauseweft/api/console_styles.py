"""Workbench stylesheets in cascade order."""

from .web import load_assets

STYLE = load_assets(
    "styles/base.css",
    "styles/forms.css",
    "styles/workflow.css",
    "styles/responsive.css",
    "styles/evidence.css",
    "styles/tasks.css",
    "styles/shell.css",
    "styles/desk.css",
    "styles/resources.css",
    "styles/run-record.css",
    "styles/materials.css",
)
