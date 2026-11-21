"""Read the workbench's offline assets from the installed Python package."""

from importlib.resources import files


def load_assets(*paths):
    root = files(__name__)
    return "".join(root.joinpath(path).read_text(encoding="utf-8") for path in paths)
