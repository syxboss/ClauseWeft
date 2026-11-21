"""Browser scripts in the order used by the standalone workbench."""

from .web import load_assets

SCRIPT = load_assets(
    "scripts/client.js",
    "scripts/components.js",
    "scripts/navigation.js",
    "scripts/overview.js",
    "scripts/run-records.js",
    "scripts/workflows.js",
    "scripts/administration.js",
    "scripts/router.js",
    "scripts/resource-forms.js",
    "scripts/task-form.js",
    "scripts/delivery.js",
    "scripts/actions.js",
    "scripts/agent-jobs.js",
    "scripts/events.js",
    "scripts/materials.js",
)
