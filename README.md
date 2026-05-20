# Shopify Dropshipping Automation

Vollautomatisches CSV-zu-Shopify-Sync-System für Elektronik/Gadgets-Dropshipping.
Läuft stündlich, berechnet Verkaufspreise nach Hybrid-Staffel, filtert Produkte
nach Gewicht/Stock/Marken und leitet eingehende Bestellungen automatisch per
E-Mail an deinen Lieferanten weiter.

---

## Was das System macht

1. Stündlich (per GitHub Actions oder Windows-Aufgabenplanung) den Lieferanten-CSV-Feed laden
2. Produkte filtern: > 2 kg raus, Markenrechtsverstöße raus, EK > 500 € raus
3. Verkaufspreis berechnen via Hybrid-Staffel + ,99-Rundung + Versand-Puffer
4. Produkte in Shopify anlegen / updaten + Lagerbestand synchronisieren
5. Eingehende Shopify-Bestellungen automatisch per E-Mail an Lieferanten

## Dateien-Übersicht

| Datei | Zweck |
|---|---|
| `config.yaml` | Alle Einstellungen (Pricing, Filter, Shop-Domain, …) |
| `.env.example` | Vorlage für Secrets — kopiere zu `.env` und fülle aus |
| `pricing.py` | Pricing-Engine (Staffel, MwSt, Rundung) |
| `csv_loader.py` | Lädt + parsed Lieferanten-CSV |
| `shopify_client.py` | Shopify Admin API Wrapper |
| `sync.py` | **Hauptskript** für den stündlichen Sync |
| `order_forwarder.py` | Webhook-Empfänger + Mail-Sender für Bestellungen |
| `requirements.txt` | Python-Abhängigkeiten |
| `.github/workflows/hourly_sync.yml` | GitHub-Actions-Cron |
| `run_local_hourly.bat` / `.sh` | Lokale Cron-Wrapper |
| `Shopify_Setup_Guide.docx` | Schritt-für-Schritt Shop-Aufbau |

## Quickstart (lokaler Test in 5 Minuten)

```bash
# 1. Python-Abhängigkeiten installieren
pip install -r requirements.txt

# 2. Konfiguration anpassen
#    Mindestens csv.url + shopify.shop_domain in config.yaml setzen
nano config.yaml

# 3. Secrets eintragen
cp .env.example .env
nano .env  # SHOPIFY_ADMIN_TOKEN und SMTP-Daten

# 4. ERSTEN LAUF im DRY-RUN starten (sendet nichts!)
python sync.py --dry-run
```

Im Dry-Run siehst du im Log, welche Produkte importiert würden und mit welchen
Preisen. Wenn alles gut aussieht:

```bash
# 5. Live-Lauf (sendet wirklich an Shopify)
python sync.py --live
```

## Pricing-Logik im Detail

Beispiel aus dem Selbsttest (`python pricing.py`):

```
      EK |  VK brutto |    Marge € |  Marge %
--------------------------------------------------
    1.50 |      12.99 |       9.42 |    627.7
    8.90 |      34.99 |      20.50 |    230.4
   24.00 |      64.99 |      30.61 |    127.6
   75.00 |     142.99 |      45.16 |     60.2
  299.00 |     444.99 |      74.94 |     25.1
```

Die Staffel ist auf den Elektronik/Gadgets-Markt zugeschnitten: günstige
Impulskäufe brauchen hohe % aber liefern absolut wenig, teure Produkte
müssen preislich näher an Amazon sein.

Anpassen in `config.yaml` unter `pricing.tiers`. Nach Änderung erneut testen:

```bash
python pricing.py
```

## Stündliche Ausführung

### Option A: GitHub Actions (empfohlen, kostenlos)

1. Repo auf GitHub anlegen
2. Code pushen
3. Im Repo unter Settings → Secrets → Actions die Variablen aus `.env` eintragen
4. Workflow läuft automatisch jede Stunde (Cron `5 * * * *`)
5. Manueller Trigger: Actions → Hourly Shopify Sync → Run workflow

### Option B: Windows-Aufgabenplanung (lokal)

1. Aufgabenplanung öffnen → "Aufgabe erstellen"
2. Trigger: Täglich, alle 1 Stunde wiederholen
3. Aktion: Programm starten → `run_local_hourly.bat`

### Option C: Cron (Linux/Mac)

```bash
crontab -e
# Folgende Zeile einfügen:
5 * * * * /pfad/zu/run_local_hourly.sh
```

## Auto-Bestellung beim Lieferanten

```bash
# Webhook-Server starten (Port 8080)
python order_forwarder.py serve --port 8080
```

In Shopify einen Webhook auf `orders/paid` einrichten, der auf
`https://<deine-domain>/webhook` zeigt. Im Setup-Guide steht, wie.

**Wichtig:** `supplier_email.auto_send: false` lassen, bis du eine Test-Bestellung
geprüft hast! Mails landen sonst als `.eml` in `outbox/` und du kannst sie
manuell weiterleiten.

## Sicherheits-Checkliste

- [ ] `.env` ist in `.gitignore` (oder du verwendest GitHub Secrets)
- [ ] `excluded_keywords` enthält geschützte Marken (Apple, Samsung Galaxy, …)
- [ ] `max_purchase_price_eur` ist gesetzt (verhindert Riesen-Bestellungen)
- [ ] Erst `dry_run: true` → testen → dann auf `false`
- [ ] Erste Wochen täglich Logs prüfen (Filter-Gründe, Preisfehler)

## Beispiel-Dateien

Im Ordner liegen außerdem als Demo-Material:
- `test_feed.csv` – Beispiel-CSV mit 8 Test-Produkten
- `outbox/order_*.eml` – Beispiel einer generierten Bestellmail
- `state.json` – Cache des letzten Sync-Laufs

Diese kannst du löschen, sobald du dein Produktiv-Setup hast.

## Häufige Fragen

**Was wenn die CSV ein anderes Format hat?**
→ In `config.yaml` unter `csv.columns` die Spaltennamen anpassen.

**Was wenn mein Lieferant kein UTF-8 verwendet?**
→ `csv.encoding: "latin-1"` oder `"cp1252"` setzen.

**Was wenn ich den Markup-Faktor ändern will?**
→ `pricing.tiers` in der `config.yaml` anpassen, dann `python pricing.py` zum Testen.

**Was wenn der Sync abbricht?**
→ Logs unter `logs/` prüfen. Jeder Lauf erzeugt eine eigene Datei mit Zeitstempel.
