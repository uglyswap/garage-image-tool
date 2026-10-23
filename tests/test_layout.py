import pathlib
import re

CSS = (pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, selector
    return match.group(1)


def test_result_card_can_shrink_so_the_last_button_never_overflows():
    """Un nom de fichier long ne doit pas pousser le dernier bouton hors de la carte."""
    assert "min-width: 0" in _rule(".result")
    assert "min-width: 0" in _rule(".r-info")


def test_result_buttons_share_one_fixed_size():
    rule = _rule(".r-view, .r-dl, .r-rt")
    assert "width: 36px" in rule and "height: 32px" in rule
