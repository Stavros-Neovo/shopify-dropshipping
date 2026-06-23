"""Self-check fuer sync.py::apply_verified_images — Regressionstest fuer den Bug,
der die manuell/image_audit.py gefixten Petkit/OutIn-Mino/Bose-Bilder beim
naechsten Sync-Lauf wieder durch das ungeprueft Icecat/CSV-Bild ersetzt hat."""
from sync import apply_verified_images


def test_verified_smap_image_overrides_enrichment():
    # enrichment hat das Produkt schon mit einem (ungeprueften) Bild befuellt
    product = {"image_url": "https://media.ipcstore.net/img/l/bad.jpg", "image_urls": ["https://media.ipcstore.net/img/l/bad.jpg"]}
    smap = {"PETK0018": {"image_verified": True, "image_url": "https://good.example/a.jpg", "images": ["https://good.example/a.jpg", "https://good.example/b.jpg"]}}
    out = apply_verified_images(product, "PETK0018", smap, {"PETK0018"})
    assert out["image_url"] == "https://good.example/a.jpg"
    assert out["image_urls"] == ["https://good.example/a.jpg", "https://good.example/b.jpg"]


def test_unverified_sku_keeps_enrichment_image():
    product = {"image_url": "https://media.ipcstore.net/img/l/bad.jpg"}
    smap = {"SKU2": {"image_verified": False, "image_url": "https://irrelevant.example/x.jpg"}}
    out = apply_verified_images(product, "SKU2", smap, set())
    assert out["image_url"] == "https://media.ipcstore.net/img/l/bad.jpg"


def test_verified_but_no_smap_entry_keeps_existing():
    product = {"image_url": "https://existing.example/x.jpg"}
    out = apply_verified_images(product, "SKU3", {}, {"SKU3"})
    assert out["image_url"] == "https://existing.example/x.jpg"


if __name__ == "__main__":
    test_verified_smap_image_overrides_enrichment()
    test_unverified_sku_keeps_enrichment_image()
    test_verified_but_no_smap_entry_keeps_existing()
    print("OK")
