"""
modules/mod_documentation.py — In-app documentation (plan §16.4).

Loads Markdown files from `docs/userdoc/` and renders them in a navigable
sidebar + content layout. Admin sections (06-11) are conditionally shown
to admins only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import markdown
from shiny import module, reactive, render, ui

from services import authz


# Resolved at import time : the path containing the user docs
_USERDOC_DIR = Path(__file__).parent.parent / "docs" / "userdoc"

# Files prefixed 06-11 require admin role to be shown
_ADMIN_FILE_PREFIXES = ("06_", "07_", "08_", "09_", "10_")


def _list_userdoc_files() -> list[Path]:
    """Return sorted list of .md files in docs/userdoc/."""
    if not _USERDOC_DIR.exists():
        return []
    return sorted(_USERDOC_DIR.glob("*.md"))


def _is_admin_only(path: Path) -> bool:
    """True if this file requires admin role."""
    return path.name.startswith(_ADMIN_FILE_PREFIXES)


def _file_title(path: Path) -> str:
    """Extract the first '# ...' heading from the file, or fall back to filename."""
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
        if first_line.startswith("# "):
            return first_line[2:].strip()
    except Exception:
        pass
    return path.stem.replace("_", " ").title()


def _render_md(path: Path) -> str:
    """Convert a Markdown file to HTML. Uses extensions for tables + code highlighting."""
    md = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br"])
    text = path.read_text(encoding="utf-8")
    return md.convert(text)


# ─────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────

@module.ui
def documentation_ui() -> ui.Tag:
    return ui.div(
        ui.h4(
            ui.tags.i(class_="bi bi-book me-2"),
            "Documentation",
        ),
        ui.layout_sidebar(
            ui.sidebar(ui.output_ui("toc"), width=320),
            ui.div(ui.output_ui("content")),
        ),
    )


@module.server
def documentation_server(input, output, session, current_user: reactive.Value):
    selected_file = reactive.value(None)

    @reactive.calc
    def visible_files() -> list[Path]:
        """Files filtered by user role."""
        u = current_user()
        files = _list_userdoc_files()
        if u is None or not authz.is_admin(u):
            # Non-admins : hide files 06-11 (admin docs)
            files = [f for f in files if not _is_admin_only(f)]
        return files

    @output
    @render.ui
    def toc():
        files = visible_files()
        if not files:
            return ui.div("Aucune documentation trouvée.", class_="text-muted small")
        u = current_user()
        is_admin = u is not None and authz.is_admin(u)

        items_user = []
        items_admin = []
        for f in files:
            title = _file_title(f)
            link = ui.input_action_link(
                f"doc_link_{f.stem}",
                title,
                class_="text-decoration-none d-block py-1 px-2 small",
            )
            if _is_admin_only(f):
                items_admin.append(link)
            else:
                items_user.append(link)

        toc_children = [
            ui.h6(ui.tags.i(class_="bi bi-people me-2"), "Guide utilisateur",
                   class_="text-muted mb-2"),
            *items_user,
        ]
        if is_admin and items_admin:
            toc_children += [
                ui.hr(class_="my-2"),
                ui.h6(ui.tags.i(class_="bi bi-shield-check me-2"),
                       "Documentation Admin",
                       class_="text-muted mb-2"),
                *items_admin,
            ]
        return ui.div(*toc_children, class_="border-end pe-2")

    # Bind one observer per file link
    def _make_link_handler(file_path: Path):
        @reactive.effect
        @reactive.event(getattr(input, f"doc_link_{file_path.stem}"))
        def _handler():
            selected_file.set(file_path)
        return _handler

    # Bind all visible files (re-bind on each render — simple approach)
    @reactive.effect
    def _bind_links():
        files = visible_files()
        for f in files:
            _make_link_handler(f)

    @output
    @render.ui
    def content():
        sel = selected_file()
        if sel is None:
            files = visible_files()
            if files:
                # Default : first file
                sel = files[0]
            else:
                return ui.div("Documentation indisponible.", class_="alert alert-warning")
        if not sel.exists():
            return ui.div(f"Fichier introuvable : {sel.name}", class_="alert alert-danger")
        try:
            html = _render_md(sel)
        except Exception as e:
            return ui.div(f"Erreur de rendu Markdown : {e}", class_="alert alert-danger")
        return ui.div(
            ui.HTML(html),
            class_="markdown-body p-3",
            style="line-height: 1.6;",
        )
