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
    # Marke stimmt überein → Icecat-GTIN-Bild vertrauenswürdig (Icecat-Titel ohne MPN)
    assert image_identity_ok(
        "Seagate Exos X24 ST24000NM002H / 24TB",
        {"brand": "Seagate", "title_full": "Seagate Exos X24 Interne Festplatte 24 TB"},
        "HDIS0273")
    # Modellnummer matcht (ohne Marken-Feld) → vertrauenswürdig
    assert image_identity_ok(
        "Seagate IronWolf ST8000VN004 / 8TB",
        {"title_full": "Seagate IronWolf ST8000VN004 Interne Festplatte 8 TB"},
        "HDIS0198")
    # Neuware ohne Enrichment → GTIN-Bild wird vertraut
    assert image_identity_ok("Crucial CT16G memory", None, "RAM0001")
    # Refurbished → EAN unzuverlässig → IMMER blockiert (Hauptursache Falschbild-Retouren)
    assert not image_identity_ok(
        "Lenovo ThinkPad T14 (14\") - Refurbished",
        {"brand": "Lenovo", "title_full": "Lenovo ThinkPad T14"},
        "REFL0003")
    # Enrichment da, aber WEDER Marke NOCH Modell matcht → verdächtig → blockiert
    assert not image_identity_ok(
        "Fremdmarke XYZ Gerät",
        {"brand": "Sony", "title_full": "Sony Kopfhörer WH-1000"},
        "SUP0001")


if __name__ == "__main__":
    test_strips_html_and_unescapes()
    test_truncates_at_word_boundary()
    test_falls_back_to_title_when_empty()
    test_image_identity_gate()
    print("OK")
