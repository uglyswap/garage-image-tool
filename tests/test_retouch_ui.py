import pathlib

JS = (pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")


def test_manual_retouch_has_its_own_blur_strength_slider():
    assert 'id="rt-strength"' in JS and 'min="1" max="5"' in JS
    # la valeur envoyée vient du curseur de la retouche, plus de celui de la page
    apply_block = JS[JS.index("async function applyRetouch"):]
    assert '$("rt-strength").value' in apply_block
    assert '$("blur-strength").value' not in apply_block


def test_retouch_slider_starts_from_the_page_value_and_is_disabled_for_white():
    open_block = JS[JS.index("function openRetouch"):JS.index("function layoutRt")]
    assert '$("blur-strength").value' in open_block
    assert 'classList.toggle("disabled", this.value === "white")' in open_block
