"""Mini-Self-Checks für die SEO-Logik in build_matrixify_csv.py."""
from build_matrixify_csv import build_meta_description, image_identity_ok


def test_strips_html_and_unescapes():
    out = build_meta_description("<p>Schnelle <b>SSD</b> &amp; leise</p>", "fallback")
    assert "<" not in out and ">" not in out
    assert out == "Schnelle SSD & leise"


def test_truncates_at_word_boundary():
    long = "<p>" + "wort " * 60 + "</p>"
    out = build_meta_description(long, "fallback")
    assert len(out) <= 156  # 155 + Ellipse
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")  # kein abgeschnittenes Teilwort/Leerzeichen


def test_falls_back_to_title_when_empty():
    assert build_meta_description("<p></p>", "Mein Titel") == "Mein Titel"


def test_image_identity_gate():
    # Modellnummer in beiden Titeln → Bild vertrauenswürdig
    assert image_identity_ok(
        "Seagate IronWolf ST8000VN004 / 8TB",
        {"title_full": "Seagate IronWolf ST8000VN004 Interne Festplatte 8 TB"},
        "HDIS0198")
    # Icecat-Titel zeigt ANDERES Modell → Falschbild → blockiert
    assert not image_identity_ok(
        "Seagate IronWolf ST8000VN004 / 8TB",
        {"title_full": "Seagate IronWolf ST4000VN006 Interne Festplatte 4 TB"},
        "HDIS0198")
    # Refurbished → EAN unzuverlässig → blockiert, egal was Icecat sagt
    assert not image_identity_ok(
        "Lenovo ThinkPad T14 (14\") - Refurbished",
        {"title_full": "Lenovo ThinkPad T14 (14\") - Refurbished"},
        "REFL0003")
    # Kein Enrichment / keine Modellnummer → blockiert (strikt)
    assert not image_identity_ok("Oral-B Junior Zahnbürste", {"title_full": "Oral-B Junior"}, "HHWB1234")
    assert not image_identity_ok("irgendwas", None, "ABC0001")


if __name__ == "__main__":
    test_strips_html_and_unescapes()
    test_truncates_at_word_boundary()
    test_falls_back_to_title_when_empty()
    test_image_identity_gate()
    print("OK")
