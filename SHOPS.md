# Multi-Shop Setup

Jeder neue eBay-Shop braucht nur 3 Dinge: eine Config-Datei, GitHub Secrets, und Workflows.
Ein neuer Shop ist in ~30 Minuten live.

---

## Aktuelle Shops

| Shop | Config | eBay Account | Marge | Status |
|------|--------|-------------|-------|--------|
| Shop 2 | `config_shop2.yaml` | Best_Neodeals | 25% fix | ✅ Live |

---

## Neuen Shop aufsetzen

### 1. Config-Datei erstellen

Kopiere `config_shop2.yaml` → `config_shop3.yaml` und passe an:

```yaml
ebay:
  fulfillment_policy_id: "XXXXXX"   # Neue Business Policy
  payment_policy_id:     "XXXXXX"
  return_policy_id:      "XXXXXX"
  merchant_location_key: "hauptlager_shop3"   # eindeutiger Name!
  refresh_token_env_var: "EBAY_REFRESH_TOKEN_3"

ebay_pricing:
  margin_tiers:
    - ek_max: 999999.0
      margin: 0.25   # Marge anpassen

runtime:
  state_file: "state_shop3.json"   # eigene State-Datei!
```

**Wichtig:** `merchant_location_key` und `state_file` müssen eindeutig sein — sonst überschreiben sich Shops gegenseitig.

---

### 2. GitHub Secrets anlegen

Gehe zu: GitHub Repo → Settings → Secrets and variables → Actions

| Secret Name | Inhalt |
|-------------|--------|
| `EBAY_REFRESH_TOKEN_3` | Refresh Token vom neuen eBay-Account |
| `EBAY_CLIENT_ID` | Bereits vorhanden (gleiche App) |
| `EBAY_CLIENT_SECRET` | Bereits vorhanden (gleiche App) |

**Refresh Token generieren:**
1. developer.ebay.com → Production → User Tokens
2. "Sign in to Production for OAuth" → mit dem neuen eBay-Seller-Account einloggen
3. Token kopieren → Secret anlegen

---

### 3. GitHub Actions Workflows duplizieren

Kopiere die bestehenden Workflows und ersetze überall `config_shop2.yaml` durch `config_shop3.yaml` und `EBAY_REFRESH_TOKEN_2` durch `EBAY_REFRESH_TOKEN_3`:

```
.github/workflows/
  reprice_shop2.yml  →  reprice_shop3.yml
  audit.yml          →  (kann mehrere Shops mit Parameter)
  dashboard.yml      →  dashboard_shop3.yml  (optional)
```

Beispiel `reprice_shop3.yml`:
```yaml
name: Repricing Shop 3
on:
  schedule:
    - cron: "30 * * * *"   # Versetzt zu Shop 2 (läuft zur vollen Stunde)
  workflow_dispatch:

concurrency:
  group: shop3-main     # EIGENE Concurrency Group!
  cancel-in-progress: false

jobs:
  reprice:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"
      - run: pip install -r requirements.txt
      - name: Run repricing
        env:
          EBAY_CLIENT_ID:       ${{ secrets.EBAY_CLIENT_ID }}
          EBAY_CLIENT_SECRET:   ${{ secrets.EBAY_CLIENT_SECRET }}
          EBAY_REFRESH_TOKEN_3: ${{ secrets.EBAY_REFRESH_TOKEN_3 }}
          CSV_HTTP_USER:        ${{ secrets.CSV_HTTP_USER }}
          CSV_HTTP_PASSWORD:    ${{ secrets.CSV_HTTP_PASSWORD }}
        run: python repricer.py --config config_shop3.yaml
```

**Wichtig:** Jeder Shop bekommt eine eigene Concurrency Group (`shop3-main`), damit sich die Shops nicht gegenseitig blockieren.

---

## Zeitleiste für neuen Shop

| Schritt | Zeit |
|---------|------|
| Config-Datei erstellen | 5 Min |
| eBay Business Policies anlegen | 10 Min |
| Refresh Token generieren | 5 Min |
| GitHub Secrets anlegen | 2 Min |
| Workflows duplizieren & anpassen | 5 Min |
| Erster Sync-Run | ~30 Min |
| **Gesamt** | **~1 Stunde** |

---

## Troubleshooting

**"merchant_location_key already exists"**
→ `merchant_location_key` in der Config ist nicht eindeutig. Anderen Namen wählen.

**"EBAY_REFRESH_TOKEN_3 fehlt"**
→ Secret in GitHub nicht angelegt oder falscher Name in der Config (`refresh_token_env_var`).

**"Offer nicht gefunden"**
→ Erster Sync-Run noch nicht ausgeführt. `sync.py --config config_shop3.yaml --live` manuell triggern.

**Shops blockieren sich gegenseitig**
→ Verschiedene Concurrency Groups pro Shop nutzen (siehe oben).

---

## Scripts die mehrere Shops unterstützen

Alle Scripts akzeptieren `--config`:

```bash
python repricer.py --config config_shop3.yaml
python audit.py    --config config_shop3.yaml
python apply_seo_titles.py  # nutzt immer config_shop2.yaml (noch nicht parametrisiert)
```
