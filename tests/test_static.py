import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"


def test_hidden_attribute_is_never_overridden_by_display():
    """Un display explicite (flex, inline-flex) écrase l'attribut hidden : la règle globale doit rester."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", css)


def test_html_references_current_assets_versions():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/style.css?v=" in html and "/app.js?v=" in html
