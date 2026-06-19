# Dropshipping Automation — Best_Neodeals

eBay-Dropshipping via BAB Distribution GmbH. Einziger Lieferant.

## System auf einen Blick

| Was | Wie |
|-----|-----|
| Preise/Bestand | `sync.py` stündlich → eBay Inventory API |
| Neue Bestellungen | `ebay_order_forwarder.py` → E-Mail an BAB |
| Tracking | `ebay_tracking_updater.py` (Gmail OAuth fehlt noch) |
| Bilder | Nur Icecat per EAN (`image_audit.py`) — kein DDG! |
| Dashboard | GitHub Pages: `docs/` → `https://stavros-neovo.github.io/shopify-dropshipping/` |

## Preisformel

```
VK_brutto = (EK_netto × 1.25 + 5.00) × 1.19 ÷ 0.79
```
- EK aus `Price_B2B` in BAB CSV (netto, ohne MwSt, ohne Versand)
- +€5 Versand netto, ×1.19 MwSt, ÷0.79 = 21% eBay-Gebühren (13% + 8% Promoted — noch nicht aktiv)

## Schlüsseldateien

| Datei | Inhalt |
|-------|--------|
| `supplier_map.json` | Herzstück: alle 1106 SKUs mit EAN, EK, VK, Bild-Status |
| `enrichment_index.csv` | Icecat-Daten: Titel, Beschreibung, Bilder (1411 Einträge) |
| `bab_preisliste.csv` | BAB-Preisliste (stündlich gecacht) |
| `config_shop2.yaml` | eBay Shop 2 (aktiver Shop) |
| `docs/orders.json` | eBay Bestellungen (stündlich sync) |

## eBay API

- Shop 2 (aktiv): `EBAY_REFRESH_TOKEN_2`
- Kein neues Listing erstellen! `ebay_client.py` ist gesperrt (nur Updates)
- Einstellgebühr: €0,06/Listing → nie `--list` in smart_lister

## Bild-Regeln (KRITISCH)

- `image_verified: true` = sicher (Icecat/ipcstore per EAN)
- `image_verified: false` + kein `image_url` = kein Bild → Listing inaktiv
- **DDG-Bilder = VERBOTEN** (Ursache aller Retouren)
- Icecat API nur von GitHub Actions erreichbar, nicht lokal

## Aktueller Stand (19.06.2026)

- 823/1106 Listings mit verifiziertem Bild
- 283 ohne Bild (nicht in Icecat Open Plan — Icecat Premium evaluieren)
- 11 Bestellungen (90 Tage), €973,80 Umsatz
- SEO Titel: 890/1106 angewendet

## Wichtige Gotchas

1. **Repo:** `Stavros-Neovo/shopify-dropshipping` (Großschreibung bei curl!)
2. **git pull:** immer `--no-rebase` (Merge statt Rebase — Lock-Files vermeiden)
3. **Zwei Shops:** `EBAY_REFRESH_TOKEN` (Shop 1) vs `EBAY_REFRESH_TOKEN_2` (Shop 2 = aktiv)
4. **PPSE0017** ist manuell in supplier_map (EcoFlow Smart Plug, nicht mehr in BAB CSV)
5. **GMAIL_REFRESH_TOKEN** fehlt → Tracking-Workflow schlägt fehl (pre-existing)

## GitHub Actions Workflows (wichtigste)

| Workflow | Wann | Was |
|----------|------|-----|
| `hourly_sync.yml` | :05 stündlich | Preise+Bestand+Bilder → eBay |
| `image_audit.yml` | 03:30 täglich | Icecat-Bilder verifizieren |
| `enrich_descriptions.yml` | 01:00 täglich | Icecat Texte/Bilder holen |
| `seo_titles.yml` | manuell | SEO Titel → eBay pushen (mode=apply) |
| `publish_offers.yml` | manuell | UNPUBLISHED Offers aktivieren |
| `emergency_deactivate.yml` | manuell | Notfall: alle unsicheren Listings deaktivieren |

## GitHub PAT (für curl Workflow-Trigger)

Liegt in GitHub Settings → Developer Settings → Personal Access Tokens (nicht im Repo).

## Kontakt BAB

SSchulze@bab-distribution.de — für Retouren, Tracking, Rücknahmen.
