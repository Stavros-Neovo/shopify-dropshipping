# Dropshipping Automation — Best_Neodeals

eBay-Dropshipping via BAB Distribution GmbH. Einziger Lieferant.

## System auf einen Blick

| Was | Wie |
|-----|-----|
| Preise/Bestand | `sync.py` stündlich → eBay Inventory API |
| Neue Bestellungen | `ebay_order_forwarder.py` → E-Mail an BAB |
| Tracking | `ebay_tracking_updater.py` (Gmail OAuth fehlt noch) |
| Bilder | Nur Icecat per EAN (`image_audit.py`) — kein DDG! |
| Dashboard | `dashboard.html` (Repo-Root) ist die QUELLE, `dashboard.yml` kopiert stündlich → `docs/index.html` (GitHub Pages) → `https://stavros-neovo.github.io/shopify-dropshipping/`. **Nie nur docs/index.html editieren — wird stündlich überschrieben!** |

## Preisformel (siehe `pricing.py::calculate_ebay_vk`)

```
Kostenbasis = EK_netto + 5.00 (Versand) + EK×25% (Marge) + EK×5% (Retouren-Rücklage)
VK_brutto   = Kostenbasis × 1.19 ÷ (1 − ebay_fee_rate − campaign_fee_rate)
```
- EK aus `Price_B2B` in BAB CSV (netto, ohne MwSt, ohne Versand)
- **Promoted Listings seit 20.06.2026 deaktiviert** (`campaign_fee_rate: 0.0` in `config_shop2.yaml`). War vorher aktiv (8%, vom Nutzer am 19.06. bestätigt). Effektive eBay-Gebühr aktuell nur 13%.
- **Bug gefunden+gefixt (20.06.):** `pricing.py` hatte `campaign_fee_rate` nie in die Formel eingerechnet (nur `repricer.py` tat das) → die beiden Preis-Skripte widersprachen sich seit Aktivierung der Kampagne. Jetzt konsistent.
- **Retouren-Rücklage 5% auf EK** (`config_shop2.yaml: ebay_pricing.return_reserve_rate`): BAB nimmt nichts zurück, eBay-Kunden haben aber 14 Tage Widerrufsrecht — jede Retoure ist ein voller EK-Verlust. Noch nicht durch echte Retourenquote validiert, nachjustieren wenn Daten da sind.
- Gleiche Rücklage fließt in `dashboard_generator.py::calc_profit_item` (Reporting) und den JS-Wasserfall im Gewinn-Tab ein

## Schlüsseldateien

| Datei | Inhalt |
|-------|--------|
| `supplier_map.json` | Herzstück: alle 1097 SKUs mit EAN, EK, VK, Bild-Status |
| `enrichment_index.csv` | Icecat-Daten: Titel, Beschreibung, Bilder (1411 Einträge) |
| `bab_preisliste.csv` | BAB-Preisliste (stündlich gecacht) |
| `config_shop2.yaml` | eBay Shop 2 (aktiver Shop) |
| `docs/orders.json` | eBay Bestellungen (stündlich sync) |

## eBay API

- Shop 2 (aktiv): `EBAY_REFRESH_TOKEN_2`
- Kein neues Listing erstellen! `ebay_client.py` ist gesperrt (nur Updates)
- Einstellgebühr: €0,06/Listing → nie `--list` in smart_lister

## Bild-Regeln (KRITISCH)

- `image_verified: true` = Icecat-Bild gefunden **UND** ≥500×500px geprüft (`image_audit.py::meets_min_resolution`, Pillow)
- **ipcstore.net (`media.ipcstore.net/img/l/...`) ist NICHT sicher** — liefert systematisch nur 200×200px, eBay lehnt mit Fehler 25002 ab. Trotz des Namens "Icecat/ipcstore" in alten Notizen: ipcstore-Bilder ohne Auflösungs-Check NIE als verified markieren.
- `image_too_small: true` = Marker für genau dieses Problem (19.06. an 373 SKUs gesetzt)
- `image_verified: false` + kein `image_url` = kein Bild → Listing inaktiv
- **DDG-Bilder = VERBOTEN** (Ursache der Makita-Abmahnung, siehe unten)
- Icecat API nur von GitHub Actions erreichbar, nicht lokal
- `image_fix_needed.json` (eBay 25002-Fehler-Cache) wird nur befüllt, nie automatisch bereinigt → kann veraltete Einträge enthalten, im Zweifel zurücksetzen

## Gesperrte SKUs (`banned_skus.json`)

12 Makita-SKUs dauerhaft gesperrt (Bildrechte-Abmahnung 16.06.2026 — Bilder durften nicht verwendet werden). **Niemals wieder listen ohne Rücksprache, auch nicht mit neuen Bildern.** `sync.py` deaktiviert gebannte SKUs automatisch über `ebay.get_offer_for_sku()` (Singular — nicht `get_offers_for_sku`, die Methode existiert nicht).

## Aktueller Stand (19.06.2026, Stand nach Session 3)

- 1097 SKUs in supplier_map.json. Nur ~443 mit echtem Bild verifiziert (Pixel-Check) — vorher fälschlich 816, siehe Bild-Regeln oben
- 11 Bestellungen (90 Tage), €973,80 Umsatz — bei aktuellem Volumen kaum profitabel nach Fixkosten (Top-Shop €59,95/Mon.)
- SEO Titel: ~1378/1411 in enrichment_index angewendet
- Dashboard (`dashboard.html`/`docs/index.html`) zeigt jetzt echte Live-Daten aus `dashboard_data.js` (Dashboard-Startseite, Bestellungen, Gewinn & Steuern, Listings) — Retouren-Tab und Fehlerbehebung-Tabelle noch mit alten Beispieldaten

## Wichtige Gotchas

1. **Repo:** `Stavros-Neovo/shopify-dropshipping` (Großschreibung bei curl!)
2. **git pull:** immer `--no-rebase` (Merge statt Rebase — Lock-Files vermeiden)
3. **Zwei Shops:** `EBAY_REFRESH_TOKEN` (Shop 1) vs `EBAY_REFRESH_TOKEN_2` (Shop 2 = aktiv, `ebay.enabled: true` nur in `config_shop2.yaml`)
4. **PPSE0017** ist manuell in supplier_map (EcoFlow Smart Plug = identisches Produkt wie BAB-SKU PPSE0102, EAN 4895251644723). Manuell nachgetragene SKUs brauchen eine ECHTE, im aktuellen BAB-Katalog vorhandene EAN, sonst löscht `deactivate_zero_stock` sie wieder.
5. **Tracking funktioniert tatsächlich** über `tracking_updater.yml` (IMAP, App-Passwort) — die alte Notiz "GMAIL_REFRESH_TOKEN fehlt → Tracking schlägt fehl" bezog sich auf den separaten, redundanten `upload_tracking.yml` (Gmail-OAuth), der nie fertig eingerichtet wurde und nichts Einzigartiges leistet.
6. **`dashboard.html` vs `docs/index.html`:** siehe Dashboard-Zeile oben — leicht zu verwechseln, Fixes immer in `dashboard.html` machen.

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
