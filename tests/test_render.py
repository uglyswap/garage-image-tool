"""Rendu non destructif : réglages limités au premier plan, retouches rejouées, exports."""
import io

import numpy as np
from PIL import Image

from app import processing
from app.processing import Layers


def _layers(color=(120, 90, 60), size=(60, 40), mask=None, alpha=None):
    rgb = np.full((size[1], size[0], 3), color, np.uint8)
    return Layers(rgb, alpha, mask if mask is not None else alpha, None, 1.0)


def _half_mask(size=(60, 40)):
    mask = np.zeros((size[1], size[0]), np.uint8)
    mask[:, size[0] // 2:] = 255  # moitié droite = véhicule
    return mask


def test_adjust_touches_only_the_foreground():
    layers = _layers(mask=_half_mask())
    out = np.asarray(processing.render_layers(layers, [], {"brightness": 60, "saturation": 40}))
    assert np.array_equal(out[:, :30], layers.rgb[:, :30])  # arrière-plan strictement identique
    assert out[:, 30:].mean() > layers.rgb[:, 30:].mean()   # véhicule éclairci


def test_adjust_without_mask_applies_to_whole_image():
    layers = _layers()
    out = np.asarray(processing.render_layers(layers, [], {"brightness": -50}))
    assert out.mean() < layers.rgb.mean()


def test_neutral_adjust_is_identity_and_master_is_never_modified():
    layers = _layers(mask=_half_mask())
    before = layers.rgb.copy()
    out = np.asarray(processing.render_layers(layers, [], {}))
    assert np.array_equal(out, before)
    processing.render_layers(layers, [{"polygon": [[5, 5], [50, 5], [50, 30]], "style": "white"}], {"contrast": 80})
    assert np.array_equal(layers.rgb, before)


def test_each_setting_has_the_expected_direction():
    def render(**adj):
        return np.asarray(processing.render_layers(_layers((150, 100, 60)), [], adj)).astype(float)[0, 0]

    base = render()
    spread = lambda px: px.max() - px.min()  # noqa: E731
    assert render(saturation=60).std() > base.std() > render(saturation=-60).std()
    assert spread(render(vibrance=60)) > spread(base)
    assert abs(render(contrast=60)[0] - 127.5) > abs(base[0] - 127.5)
    assert render(saturation=-100).std() < 1.5  # désaturation totale = gris


def test_vibrance_spares_already_saturated_colors():
    def gain(color):
        base = np.array(color, float)
        out = np.asarray(processing.render_layers(_layers(color), [], {"vibrance": 80})).astype(float)[0, 0]
        return (out.max() - out.min()) / (base.max() - base.min())

    assert gain((140, 120, 110)) > gain((250, 20, 20))


def test_clean_adjust_clamps_and_ignores_garbage():
    assert processing.clean_adjust({"brightness": 500, "contrast": "x", "evil": 3}) == {
        "brightness": 100, "contrast": 0, "saturation": 0, "vibrance": 0}


def test_edits_are_scaled_for_previews():
    layers = _layers((10, 10, 10), size=(100, 100))
    layers.scale = 0.5  # aperçu à demi-taille : le polygone est en pixels de l'image réelle
    out = np.asarray(processing.render_layers(
        layers, [{"polygon": [[40, 40], [160, 40], [160, 120], [40, 120]], "style": "white"}], {}))
    assert out[40, 50].min() > 200 and out[90, 90].max() < 40


def test_rgba_master_keeps_alpha_and_adjusts_opaque_pixels_only():
    alpha = _half_mask()
    img = processing.render_layers(_layers(alpha=alpha), [], {"brightness": 50})
    arr = np.asarray(img)
    assert img.mode == "RGBA" and np.array_equal(arr[:, :, 3], alpha)
    assert arr[0, 0, 0] == 120 and arr[0, 59, 0] > 120


def test_load_layers_reads_mask_and_preview_scale():
    buf, mbuf = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (400, 200), (50, 60, 70)).save(buf, "JPEG")
    Image.fromarray(_half_mask((400, 200))).save(mbuf, "PNG")
    full = processing.load_layers(buf.getvalue(), mbuf.getvalue())
    assert full.rgb.shape == (200, 400, 3) and full.scale == 1.0 and full.fg_mask[0, 399] == 255
    small = processing.load_layers(buf.getvalue(), mbuf.getvalue(), max_side=100)
    assert max(small.rgb.shape[:2]) <= 100 and small.fg_mask.shape == small.rgb.shape[:2]
    assert abs(small.scale - small.rgb.shape[1] / 400) < 1e-6


def test_encode_formats_and_icc_roundtrip():
    icc = b"profil-factice"
    img = Image.new("RGBA", (20, 10), (200, 10, 10, 0))
    for fmt, expected in (("jpeg", "JPEG"), ("png", "PNG"), ("webp", "WEBP")):
        out = Image.open(io.BytesIO(processing.encode_image(img, fmt, 90, icc)))
        assert out.format == expected
        assert out.info.get("icc_profile") == icc
    flat = Image.open(io.BytesIO(processing.encode_image(img, "jpeg", 90))).convert("RGB")
    assert min(flat.getpixel((5, 5))) > 240  # transparence aplatie sur blanc, pas sur noir


def test_process_keeps_icc_profile_and_returns_mask_in_replace_mode(monkeypatch):
    src = io.BytesIO()
    Image.new("RGB", (200, 150), (90, 120, 150)).save(src, "JPEG", icc_profile=b"profil-source")
    bg = io.BytesIO()
    Image.new("RGB", (50, 50), (0, 200, 0)).save(bg, "PNG")

    def fake_remove(image, model=None):
        rgba = Image.new("RGBA", image.size, (0, 0, 0, 0))
        rgba.paste(Image.new("RGBA", (120, 60), (200, 50, 50, 255)), (10, 10))
        return rgba

    monkeypatch.setattr(processing, "_remove_background", fake_remove)
    res = processing.process_image(src.getvalue(), processing.ProcessOptions(
        bg_mode="replace", background=bg.getvalue(), blur_enabled=False))
    assert Image.open(io.BytesIO(res.data)).info.get("icc_profile") == b"profil-source"
    mask = np.asarray(Image.open(io.BytesIO(res.mask)))
    assert mask.shape == (150, 200) and mask.max() == 255 and mask.min() == 0
    plain = processing.process_image(src.getvalue(), processing.ProcessOptions(blur_enabled=False))
    assert plain.mask is None and plain.master_ext == "jpg"


def test_white_retouch_has_soft_edges():
    arr = np.full((60, 80, 3), 10, np.uint8)
    processing._white_polygon(arr, [[20, 20], [60, 20], [60, 40], [20, 40]])
    assert arr[30, 40].min() >= 245
    assert 10 < arr[30, 20, 0] < 250  # bord fondu, pas une marche d'escalier


def test_auto_adjust_pulls_a_batch_together():
    dark = {"lum": 0.25, "spread": 0.6, "sat": 0.2}
    normal = {"lum": 0.45, "spread": 0.6, "sat": 0.2}
    bright = {"lum": 0.7, "spread": 0.6, "sat": 0.2}
    a, b, c, missing = processing.auto_adjust([dark, normal, bright, None])
    assert a["brightness"] > 0 > c["brightness"]
    assert abs(b["brightness"]) <= 5
    assert missing == processing.clean_adjust(None)
    assert all(abs(v) <= 35 for adj in (a, b, c) for v in adj.values())  # correction plafonnée


def test_auto_adjust_leaves_grey_cars_colourless_and_single_image_stable():
    grey = {"lum": 0.5, "spread": 0.7, "sat": 0.02}
    vivid = {"lum": 0.5, "spread": 0.7, "sat": 0.4}
    out = processing.auto_adjust([grey, vivid, vivid])
    assert out[0]["vibrance"] == 0 and out[0]["saturation"] == 0
    (alone,) = processing.auto_adjust([{"lum": 0.48, "spread": 0.7, "sat": 0.3}])
    assert all(abs(v) <= 3 for v in alone.values())


def test_foreground_stats_ignores_the_background():
    rgb = np.zeros((40, 60, 3), np.uint8)
    rgb[:, 30:] = (200, 200, 200)
    stats = processing.foreground_stats(Layers(rgb, None, _half_mask(), None, 1.0))
    assert abs(stats["lum"] - 200 / 255) < 0.01 and stats["sat"] < 0.01
    assert processing.foreground_stats(Layers(rgb, None, np.zeros((40, 60), np.uint8), None, 1.0)) is None
