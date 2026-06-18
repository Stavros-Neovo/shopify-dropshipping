#!/usr/bin/env python3
"""
bab_preisliste_export.py
========================
Lädt die BAB B2B-Preisliste (CSV) herunter und konvertiert sie
zu docs/bab_preisliste.json (wird vom Dashboard gelesen).

Aufruf: python bab_preisliste_export.py --config config_shop2.yaml
"""
from __future__ import annotations
import argparse, csv, io, json, sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config_shop2.yaml")
    parser.add_argument("--output", default="docs/bab_preisliste.json")
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    csv_url   = cfg["csv"]["url"]
    delimiter = cfg["csv"].get("delimiter", ";")
    encoding  = cfg["csv"].get("encoding", "utf-8-sig")

    print(f"Lade BAB-Preisliste von: {csv_url}")

    # Redirect folgen (die URL liefert eine HTML-Seite mit Meta-Refresh)
    resp = requests.get(csv_url, timeout=30, allow_redirects=True)
    content_type = resp.headers.get("Content-Type", "")

    if "text/html" in content_type:
        # Meta-Refresh URL extrahieren
        import re
        m = re.search(r'url=([^\s"\']+\.csv[^\s"\']*)', resp.text)
        if not m:
            print("FEHLER: Keine CSV-URL in HTML gefunden", file=sys.stderr)
            sys.exit(1)
        direct_url = m.group(1)
        print(f"Folge Redirect zu: {direct_url}")
        resp = requests.get(direct_url, timeout=60, allow_redirects=True)

    resp.encoding = encoding
    raw = resp.text

    # CSV parsen
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
    articles = []
    for row in reader:
        sku   = (row.get("ItemNo") or "").strip()
        title = (row.get("Description") or "").strip()
        ean   = (row.get("GTIN") or "").strip()
        mfr   = (row.get("ManufacturerName") or "").strip()
        mfr_code = (row.get("ManufacturerCode") or "").strip()
        ref_no = (row.get("ReferenceNumber") or "").strip()
        stock_raw = (row.get("Stock") or "0").strip()
        ek_raw = (row.get("Price_B2B") or "0").strip().replace(",", ".")

        if not sku:
            continue

        try:
            ek = round(float(ek_raw), 2)
        except ValueError:
            ek = 0.0

        try:
            stock = int(stock_raw)
        except ValueError:
            stock = 0

        # Korrekte Formel (EK netto + 5€ Versand netto, 25% Marge, 21% eBay+Kampagne, 19% MwSt):
        # VK_brutto = (EK_netto × 1.25 + 5.00) × 1.19 ÷ 0.79
        MARGIN    = 0.25
        SHIP_NET  = 5.00
        VAT       = 1.19
        TOTAL_FEE = 0.79   # 1 − 0.21 (13% eBay + 8% Kampagne)

        if ek > 0:
            vk_raw = (ek * (1 + MARGIN) + SHIP_NET) * VAT / TOTAL_FEE
            import math as _math
            floored = _math.floor(vk_raw)
            vk = floored + 0.99 if (floored + 0.99) >= vk_raw else floored + 1.99
        else:
            vk = 0

        # Netto-Gewinn: (VK × 0.79 / 1.19) − EK_netto − Versand_netto
        gewinn = round((vk * TOTAL_FEE / VAT) - ek - SHIP_NET, 2) if vk > 0 else 0

        articles.append({
            "sku":      sku,
            "title":    title,
            "ean":      ean,
            "mfr":      mfr,
            "mfr_code": mfr_code,
            "ref_no":   ref_no,
            "stock":    stock,
            "ek":       ek,
            "vk":       round(vk, 2),
            "gewinn":   gewinn,
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(articles),
        "articles": articles,
    }

    Path(args.output).parent.mkdir(exist_ok=True)
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"✅ {len(articles)} Artikel → {args.output}")


if __name__ == "__main__":
    main()
