from ebay_client import EbayClient, _guess_category_from_title


def test_guess_category_noco():
    assert _guess_category_from_title("NOCO GBX75 | Boost X 12V 2500A Jump Starter") == "179472"
    assert _guess_category_from_title("NOCO GENIUS5EU | 5A Battery Charger") == "179471"
    # Generische Ladegeraete anderer Marken duerfen NICHT als KFZ-Batterieladegeraet matchen
    assert _guess_category_from_title("Samsung Schnellladegerät USB-C 25W") == ""
    assert _guess_category_from_title("ECOFLOW 140W Charger (No cable)") == ""


def test_derive_produktart_noco():
    f = EbayClient._derive_produktart
    assert f("NOCO GBX75 | Boost X 12V 2500A Jump Starter", "179472") == "Energiestation"
    assert f("NOCO GENIUS5EU | 5A Battery Charger", "179471") == "Batterieladegerät"
    # Taxonomy-Fallback landet manchmal in falscher Kategorie (z.B. 44932) -
    # Titel-Erkennung muss trotzdem greifen
    assert f("NOCO GB150 | Boost 12V 3000A Jump Starter", "44932") == "Energiestation"


def test_derive_produktart_unchanged_existing():
    f = EbayClient._derive_produktart
    assert f("Irgendein USB-C Hub", "44932") == "USB-Hub"
    assert f("Intel CPU Box", "164") == "Prozessor (CPU) – Boxed"


if __name__ == "__main__":
    test_guess_category_noco()
    test_derive_produktart_noco()
    test_derive_produktart_unchanged_existing()
    print("ok")
