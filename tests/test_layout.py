import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, selector
    return match.group(1)


def test_result_card_can_shrink_so_the_last_button_never_overflows():
    """Un nom de fichier long ne doit pas pousser le dernier bouton hors de la carte."""
    assert "min-width: 0" in _rule(".result")
    assert "min-width: 0" in _rule(".r-info")


def test_result_buttons_share_the_row_equally():
    rule = _rule(".r-view, .r-dl, .r-rt, .r-adj, .r-save")
    assert "flex: 1 1 0" in rule and "min-width: 0" in rule and "height: 34px" in rule


def test_every_overlay_created_by_the_script_is_styled():
    for overlay in ("rt-overlay", "view-overlay", "adj-overlay", "save-overlay"):
        assert '"' + overlay + '"' in JS, overlay
        assert "#" + overlay in CSS, overlay


def test_results_are_not_rebuilt_on_every_poll():
    """La carte porte une signature : sans changement, le DOM n'est pas touché."""
    card = JS[JS.index("function renderCard"):JS.index("function updateResult")]
    assert "state.cards[r.index] === signature" in card
    assert "thumbUrl(r)" in card and "fullUrl(r)" not in card  # la grille charge des vignettes, pas les images entières


def test_job_survives_a_page_reload():
    assert "store(JOB_KEY, state.jobId)" in JS and "async function restoreJob" in JS
    assert "store(JOB_KEY, null)" in JS[JS.index("function reset"):JS.index("function wire")]


def test_upload_reports_progress():
    assert "xhr.upload.onprogress" in JS and '"Envoi "' in JS


def test_bulk_tools_and_adjust_sliders_exist():
    for element in ("select-all", "btn-bulk-auto", "btn-bulk-adjust", "btn-bulk-save", "results-bar"):
        assert 'id="' + element + '"' in HTML, element
    for key in ("brightness", "contrast", "saturation", "vibrance"):
        assert '"' + key + '"' in JS
    assert "/auto-adjust" in JS and "/preview?" in JS and "/zip" in JS
    assert "showSaveFilePicker" in JS  # « enregistrer sous » avec choix du dossier quand le navigateur le permet


def test_retouch_has_touch_friendly_tools():
    block = JS[JS.index("function openRetouch"):JS.index("function layoutRt")]
    for element in ("rt-undo-pt", "rt-zoom-in", "rt-zoom-out", "rt-undo"):
        assert 'id="' + element + '"' in block, element


def test_no_inline_scripts_or_handlers_so_the_csp_holds():
    assert not re.search(r"<script(?![^>]*\bsrc=)", HTML)
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", HTML)
    assert not re.search(r"['\"] on(click|load|error|change|input)=", JS)
