"""
ebay_fees.py
============
Kategorie-abhängige eBay-Verkaufsgebühr, ersetzt die bisherige Pauschal-
Annahme von 13% (siehe ebay_category_fees.yaml für Sätze, ebay_category_ancestors.json
für den EBAY_DE-Taxonomy-Baum, Stand 23.06.2026, category_tree/77).
"""
from __future__ import annotations
from datetime import date
from pathlib import Path
import json
import yaml

from ebay_client import get_ebay_category_id, _guess_category_from_title

FEE_CHANGE_DATE = date(2026, 7, 1)

_data = yaml.safe_load(Path(__file__).with_name("ebay_category_fees.yaml").read_text(encoding="utf-8"))
_FEE_GROUPS = _data["fee_groups"]
_GROUP_ROOTS = {group: set(roots) for group, roots in _data["group_roots"].items()}
_DEFAULT_GROUP = _data.get("default_group", "standard")
_HYGIENE_ROOTS = set(_data.get("hygiene_category_roots", []))
_PARENT = json.loads(Path(__file__).with_name("ebay_category_ancestors.json").read_text(encoding="utf-8"))


def iter_ancestors(category_id: str):
    """Yields category_id, dann jede Eltern-ID aufwärts bis zur Wurzel
    (Zyklen-Schutz via seen-Set). Gemeinsame Traversierung für alles, was die
    Kategorie-Ancestor-Kette laufen muss (siehe auch audit_category_mismatches.py)."""
    cur = str(category_id)
    seen = set()
    while cur and cur not in seen:
        yield cur
        seen.add(cur)
        cur = _PARENT.get(cur, "")


def _fee_group_for_category(category_id: str) -> str:
    """Nimmt die tiefste (spezifischste) Übereinstimmung mit einer Gruppen-Wurzel
    entlang der Ancestor-Kette."""
    for cur in iter_ancestors(category_id):
        for group, roots in _GROUP_ROOTS.items():
            if cur in roots:
                return group
    return _DEFAULT_GROUP


def is_hygiene_category(category_id: str) -> bool:
    """Versiegelter Hygieneartikel (elektr. Zahn-/Mundpflege, Rasur, Haarstyling)
    - Widerrufsrecht entfällt nach Öffnen der Versiegelung (§312g Abs.2 Nr.3 BGB).
    Nutzt für Mindestgewinn-statt-Retouren-Rücklage-Logik, siehe pricing.py/repricer.py."""
    if not category_id:
        return False
    return any(cur in _HYGIENE_ROOTS for cur in iter_ancestors(category_id))


def resolve_fee_category(bab_category: str, title: str) -> str:
    """Beste Schätzung der eBay-Kategorie-ID ohne Live-API-Aufruf - spiegelt
    die ersten zwei Schritte von EbayClient.get_category_for_title() (Mapping,
    dann Titel-Keywords). Für Artikel, die beim Listing über die Taxonomy API
    gelandet sind (gemischte BAB-Gruppen CE/ZUBEHÖR/HW/GA/PPS) oder deren Titel
    keine Marken-/Gattungsbegriffe enthält (z.B. "Oral-B iO Series 10 Lunar"
    ohne das Wort "Zahnbürste"), ist das nur eine Näherung -> fällt auf die
    Default-Kategorie zurück. ponytail: für SKUs, die repricer.py bereits per
    Live-Offer gesehen hat, liefert die echte categoryId ein genaueres Ergebnis."""
    cat_id = get_ebay_category_id(bab_category)
    guessed = _guess_category_from_title(title)
    return guessed or cat_id


def get_ebay_fee_rate(category_id: str, today: date | None = None) -> float:
    """eBay-Verkaufsgebühr (Anteil, z.B. 0.065) für eine eBay-Kategorie-ID."""
    today = today or date.today()
    group_key = _fee_group_for_category(category_id) if category_id else _DEFAULT_GROUP
    group = _FEE_GROUPS[group_key]
    rate_key = "rate_from" if today >= FEE_CHANGE_DATE else "rate_until"
    return float(group[rate_key])


def resolve_fee_rate(category_id: str, fallback: float) -> float:
    """Wie get_ebay_fee_rate(), aber mit explizitem Fallback-Satz statt der
    Default-Gruppe, wenn keine category_id vorliegt - für Aufrufer, die ohne
    Kategorie auf die alte Pauschale aus config_shop2.yaml zurückfallen wollen
    statt auf die Default-Gruppe dieser Datei."""
    return get_ebay_fee_rate(category_id) if category_id else fallback


if __name__ == "__main__":
    assert get_ebay_fee_rate("175669", today=date(2026, 6, 23)) == 0.065   # SSD, vor 1.7.
    assert get_ebay_fee_rate("175669", today=date(2026, 7, 1)) == 0.07     # SSD, ab 1.7.
    assert get_ebay_fee_rate("44932", today=date(2026, 6, 23)) == 0.11     # Kabel (Default), vor 1.7.
    assert get_ebay_fee_rate("9999999999", today=date(2026, 6, 23)) == 0.12  # unbekannt -> standard
    assert resolve_fee_category("ROUTER", "irgendein Titel") == "51268"
    assert resolve_fee_category("CE", "Sony Powerbank 20000mAh") == "20357"
    assert resolve_fee_category("CE", "Sony Kopfhörer") == "44932"        # kein Treffer -> Default
    # Echte Live-Fälle (23.06.2026) - via Ancestor-Walk statt hartkodierter Liste:
    assert get_ebay_fee_rate("31770") == get_ebay_fee_rate("99654")  # Zahnbürste + Ersatzbürsten -> gleiche Gruppe (geraete)
    assert _fee_group_for_category("31770") == "geraete"             # Braun Oral-B Zahnbürste
    assert _fee_group_for_category("99654") == "geraete"             # Oral-B Ersatzbürsten-Zubehör
    assert is_hygiene_category("31770") and is_hygiene_category("99654")  # Oral-B - versiegelter Hygieneartikel
    assert is_hygiene_category("11860")   # Frisierprodukte (ghd Sprays/Öle) - unter 26395
    assert is_hygiene_category("21205")   # Tagespflege (Desinfektionsgel) - unter 26395
    assert not is_hygiene_category("51268")  # Netzwerk-Switch - kein Hygieneartikel
    assert not is_hygiene_category("")
    print("ebay_fees.py: alle Selbsttests OK")
