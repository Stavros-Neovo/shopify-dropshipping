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


def test_derive_produktart_jabra_govee_verbatim():
    f = EbayClient._derive_produktart
    assert f("JABRA Evolve2 75 - Headset", "80183") == "Ohraufliegende Kopfhörer"
    assert f("JABRA Evolve 65 TE Stereo - Headset - On-Ear", "112529") == "Ohraufliegende Kopfhörer"
    assert f("Govee - String Lights 2S - 20M", "116022") == "Lichterkette"
    assert f("Govee - LED Strip Lights Matter ready 5 Meter", "116022") == "Lichtstreifen"
    assert f("Govee Smart Wireless Meat Thermometer (2-Probe)", "43421") == "Bratenthermometer"
    assert f("VERBATIM CHARGE 'N' TRAVEL 100W UNIVERSAL TRAVEL ADAPTER", "79846") == "Reiseadapter / Konverter"
    assert f("Verbatim Mobile DVD ReWriter USB 2.0", "131542") == "Externes Laufwerk"
    # Kategorien ohne Produktart-Aspekt (44996/162/171949) - bewusst NICHT
    # geraten, Funktion faellt korrekt auf Sonstiges zurueck
    assert f("Govee - LED Christmas Light 20 Meter", "162") == "Sonstiges"


def test_derive_produktart_unchanged_existing():
    f = EbayClient._derive_produktart
    assert f("Irgendein USB-C Hub", "44932") == "USB-Hub"
    assert f("Intel CPU Box", "164") == "Prozessor (CPU) – Boxed"


if __name__ == "__main__":
    test_guess_category_noco()
    test_derive_produktart_noco()
    test_derive_produktart_jabra_govee_verbatim()
    test_derive_produktart_unchanged_existing()
    print("ok")
