"""Mini-Self-Checks für die SEO-Logik in build_matrixify_csv.py."""
from build_matrixify_csv import build_meta_description


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


if __name__ == "__main__":
    test_strips_html_and_unescapes()
    test_truncates_at_word_boundary()
    test_falls_back_to_title_when_empty()
    print("OK")
