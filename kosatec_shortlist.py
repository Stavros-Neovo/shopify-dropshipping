#!/usr/bin/env python3
"""Kosatec-Gewinner (would_win & profit>0) -> Import-Shortlist CSV.
Re-runnbar: liest den laufenden/fertigen Scan, dedupt gegen supplier_map (BAB).
    python3 kosatec_shortlist.py
"""
import json, csv

results = json.load(open("kosatec_scan_results.json"))
have = {str(v.get("ean")).strip() for v in json.load(open("supplier_map.json")).values() if v.get("ean")}

MAX_EK = 300  # kein Wiederverkaufsmarkt + Vorkasse zu teuer ueber 300€ EK (Entscheidung 01.07.2026)
win = [x for x in results if x.get("would_win") and x.get("real_profit", 0) > 0 and (x.get("hek") or 0) <= MAX_EK]
win.sort(key=lambda x: -x["real_profit"])

OUT = "kosatec_shortlist.csv"
cols = ["artnr", "ean", "name", "hersteller", "kat1", "hek", "menge",
        "lowest_competitor", "competitor_count", "undercut_price",
        "real_profit", "marge_pct", "already_bab"]
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for x in win:
        hek = x.get("hek") or 0
        x["marge_pct"] = round(x["real_profit"] / x["undercut_price"] * 100, 1) if x.get("undercut_price") else 0
        x["already_bab"] = "ja" if str(x.get("ean")).strip() in have else ""
        w.writerow(x)

overlap = sum(1 for x in win if str(x.get("ean")).strip() in have)
print(f"{len(win)} Gewinner -> {OUT}")
print(f"  davon {overlap} bereits als BAB-SKU vorhanden (billigere Quelle), {len(win)-overlap} echte Neuprodukte")
print(f"  Profit gesamt (undercut): {sum(x['real_profit'] for x in win):.0f}€/Verkauf | Median-Marge {sorted(x['marge_pct'] for x in win)[len(win)//2]:.1f}%")

# ponytail: sanity-check, faellt wenn Sortierung/Filter bricht
assert win[0]["real_profit"] >= win[-1]["real_profit"], "nicht sortiert"
assert all(x["real_profit"] > 0 for x in win), "Verlustbringer in Gewinnerliste"
