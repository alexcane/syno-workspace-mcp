"""Build the project's landing page (GitHub Pages) into a static folder.

The page reuses the theme of the server's own web pages (`_theme.css` from
`syno_workspace_mcp.web`), so both keep the same look without a copy.

    uv run python landing/build.py            # writes _site/
    uv run python landing/build.py --out DIR
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape

from syno_workspace_mcp import __version__

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing"
REPOSITORY = "https://github.com/alexcane/syno-workspace-mcp"
IMAGES = ("setup-summary.png",)


def create_environment() -> Environment:
    environment = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(LANDING / "templates"),
                PackageLoader("syno_workspace_mcp.web", "templates"),
            ]
        ),
        autoescape=select_autoescape(default=True),
    )
    environment.globals.update(version=__version__, repository=REPOSITORY)
    return environment


def render(theme: str | None = None) -> str:
    """The landing page; `theme` forces "light" or "dark" (previews only)."""
    return create_environment().get_template("index.html").render(theme=theme)


def build(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(), encoding="utf-8")
    images = out / "images"
    images.mkdir(exist_ok=True)
    for name in IMAGES:
        shutil.copyfile(ROOT / "docs" / "images" / name, images / name)
    # Serve the folder as is: no Jekyll processing on GitHub Pages.
    (out / ".nojekyll").touch()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    build(args.out)
    print(f"Landing page written to {args.out}")


if __name__ == "__main__":
    main()
