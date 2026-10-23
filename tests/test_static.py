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


def test_no_em_dash_in_user_facing_files():
    for name in ("index.html", "app.js", "style.css"):
        assert "\\u2014".encode().decode("unicode_escape") not in (STATIC / name).read_text(encoding="utf-8"), name


def test_plate_options_wording_and_order():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "Masquer les plaques d'immatriculation" in html
    assert "Flouter les plaques" not in html
    assert "YOLOv8" not in html and "YOLOv8" not in (STATIC / "app.js").read_text(encoding="utf-8")
    # Intensité du flou, puis style des plaques, puis format
    assert html.index("Intensité du flou") < html.index('id="plate-style"') < html.index('id="out-format"')


def test_result_buttons_view_download_retouch_in_order():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert js.index('class="r-view"') < js.index('class="r-dl"') < js.index('class="r-rt"')
    assert "function openViewer" in js and "function closeViewer" in js


def test_app_js_is_syntactically_valid():
    """Une erreur de syntaxe casse toute l'interface sans qu'aucun test Python ne le voie."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node absent")
    proc = subprocess.run([node, "--check", str(STATIC / "app.js")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
