"""Shared layout of the server's web pages."""

from __future__ import annotations

import pytest

from syno_workspace_mcp import __version__
from syno_workspace_mcp.web import create_environment

PAGE = """{% extends "layout.html" %}
{% from "_macros.html" import steps %}
{% block title %}Test{% endblock %}
{% block steps %}{{ steps(["One", "Two", "Three"], current) }}{% endblock %}
{% block content %}<p>body</p>{% endblock %}"""


def render(**values: object) -> str:
    return create_environment().from_string(PAGE).render({"error": None, "current": 2, **values})


def test_layout_inlines_theme_and_version() -> None:
    html = render()
    assert "<title>Test · syno-workspace-mcp</title>" in html
    assert "--primary:" in html
    assert __version__ in html
    assert "<p>body</p>" in html


def test_steps_mark_done_current_and_todo() -> None:
    html = render(current=2)
    assert 'class="step step-done"' in html
    assert 'class="step step-current" aria-current="step"' in html
    assert 'class="step step-todo"' in html


def test_error_is_escaped() -> None:
    html = render(error="<script>x</script>")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


@pytest.mark.parametrize(("theme", "expected"), [("dark", True), ("light", True), ("x", False)])
def test_theme_attribute_only_for_known_values(theme: str, expected: bool) -> None:
    assert (f'data-theme="{theme}"' in render(theme=theme)) is expected
