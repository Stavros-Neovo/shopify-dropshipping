# =============================================================================
# Dropshipping Automation – Makefile
# =============================================================================
# Aufruf:
#   make sync          → Produkt-Sync (beide Shops)
#   make orders        → Neue eBay-Bestellungen → Email an BAB
#   make tracking      → BAB-Antwortmails → Tracking auf eBay eintragen
#   make all           → sync + orders + tracking (für GitHub Actions)
#   make dry           → alles im Dry-Run (kein echtes Senden)
# =============================================================================

.PHONY: all sync orders tracking kosatec_stock dry

# Stündlicher Komplett-Durchlauf (GitHub Actions)
all: sync orders tracking kosatec_stock

# Produkt-Sync: Preise + Bestand auf eBay aktualisieren
sync:
	python sync.py --config config.yaml --live
	python sync.py --config config_shop2.yaml --live

# Neue eBay-Bestellungen per Email an BAB weiterleiten
orders:
	python ebay_order_forwarder.py --config config.yaml
	python ebay_order_forwarder.py --config config_shop2.yaml

# BAB-Antwortmails lesen → Tracking auf eBay eintragen
tracking:
	python ebay_tracking_updater.py --config config.yaml
	python ebay_tracking_updater.py --config config_shop2.yaml

# Kosatec Bestandscheck: out-of-stock Artikel sofort deaktivieren
kosatec_stock:
	curl -fL "$$KOSATEC_URL" -o kosatec_preisliste.csv 2>/dev/null || true
	python kosatec_stock_check.py

# Alles im Dry-Run (nichts wird wirklich gesendet)
dry:
	python sync.py --config config.yaml
	python ebay_order_forwarder.py --config config.yaml --dry-run
	python ebay_tracking_updater.py --config config.yaml --dry-run
