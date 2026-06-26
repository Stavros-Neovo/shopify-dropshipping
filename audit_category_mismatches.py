"""
audit_category_mismatches.py
=============================
Findet SKUs, deren echte (live von repricer.py beobachtete) eBay-Kategorie
zu einem für diesen Katalog unplausiblen Department gehört (z.B. "Filme &
Serien" oder "Sammeln & Seltenes" bei einem Elektronik-Dropshipper - klassische
Taxonomy-API-Fehlgriffe beim Listing, siehe Session-Notiz 23.06.2026).

Nutzt nur lokal gecachte Daten (supplier_map.json::ebay_category_id +
ebay_category_ancestors.json) - keine Live-API-Aufrufe, kein Gebühren-Budget
verbraucht. Mehr SKUs bekommen automatisch eine echte Kategorie, je öfter
repricer.py läuft (siehe dort: schreibt categoryId aus dem Live-Offer zurück).

Reines Report-Tool, fixt nichts automatisch - die richtige Zielkategorie
braucht menschliches/KI-Urteil (siehe fix_categories.py-Vorgehen vom 23.06.).
"""
from __future__ import annotations
import json
from pathlib import Path

# Departments, die für diesen Elektronik/Haushalt/Haustier-Katalog praktisch
# nie korrekt sind - tauchen nur durch fehlgeschlagene Taxonomy-API-Vorschläge auf.
SUSPICIOUS_DEPARTMENTS = {
    "Filme & Serien", "Musik", "PC- & Videospiele", "Bücher & Zeitschriften",
    "Münzen", "Briefmarken", "Sammelkartenspiele/TCGs", "Trading Cards",
    "Kleidung & Accessoires", "Uhren & Schmuck", "Sammeln & Seltenes",
    "Modellbau", "Reisen",
}
# Diese brauchen Einzelfall-Blick statt Pauschal-Verdacht (unsere EcoFlow/NOCO/
# Solar-Artikel landen hier teils korrekt, teils falsch - siehe Session-Notiz):
REVIEW_DEPARTMENTS = {"Sport", "Auto & Motorrad: Teile", "Business & Industrie", "Möbel & Wohnen"}


def main():
    ancestors_path = Path(__file__).with_name("ebay_category_ancestors.json")
    tree_names_path = Path(__file__).with_name("ebay_category_names_cache.json")
    parent = json.loads(ancestors_path.read_text(encoding="utf-8"))
    names = json.loads(tree_names_path.read_text(encoding="utf-8")) if tree_names_path.exists() else {}

    sm = json.loads(Path("supplier_map.json").read_text(encoding="utf-8"))

    def top_department(cat_id: str) -> str:
        """Läuft hoch bis cur direktes Kind der Wurzel ist (cur hat einen
        Parent, aber dieser Parent selbst hat keinen -> cur ist die Department-Ebene)."""
        cur = str(cat_id)
        seen = set()
        while True:
            nxt = parent.get(cur)
            if nxt is None or nxt not in parent or cur in seen:
                return names.get(cur, cur)
            seen.add(cur)
            cur = nxt

    flagged, review = [], []
    for sku, v in sm.items():
        cat = v.get("ebay_category_id")
        if not cat:
            continue
        dept = top_department(cat)
        title = v.get("title", "?")
        if dept in SUSPICIOUS_DEPARTMENTS:
            flagged.append((sku, cat, dept, title))
        elif dept in REVIEW_DEPARTMENTS:
            review.append((sku, cat, dept, title))

    print(f"{sum(1 for v in sm.values() if v.get('ebay_category_id'))} SKUs mit bekannter echter Kategorie geprüft\n")
    print(f"=== {len(flagged)} klare Verdachtsfälle (Department passt nie zu diesem Katalog) ===")
    for sku, cat, dept, title in flagged:
        print(f"  {sku:10} [{dept:20}] cat={cat:>7}  {title[:55]}")
    print(f"\n=== {len(review)} Einzelfall-Kandidaten (Department kann legitim sein, bitte Titel prüfen) ===")
    for sku, cat, dept, title in review:
        print(f"  {sku:10} [{dept:20}] cat={cat:>7}  {title[:55]}")


if __name__ == "__main__":
    main()
