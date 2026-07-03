#!/usr/bin/env python3
"""Generiert für alle Kosatec-Produkte (KOS-) Titel + Beschreibung + SEO + Bild-URL
→ kosatec_content.json. Beschreibung: Icecat (EAN/MPN, läuft lokal) wo vorhanden,
sonst kategorie-basiertes Template. Schreibt NICHTS nach Shopify (das macht die
Action fix_kosatec.yml mit dem Secret-Token).

    python3 gen_kosatec_content.py
"""
import json, re, time, html
import requests

ICECAT_API = "https://live.icecat.biz/api"
ICECAT_USER = "neovogen"
ICECAT_TOKEN = "a923fe60-04bd-4f83-ae2e-a1e1a8427c98"

# Kategorie-Templates (type_override → beschreibender Satz) für Produkte ohne Icecat
TEMPLATES = {
    "Netzwerk-Switches": "ist ein leistungsfähiger Netzwerk-Switch für stabile, schnelle und zuverlässige Verbindungen in anspruchsvollen Umgebungen.",
    "Überwachungskameras": "ist eine hochwertige Überwachungskamera für zuverlässige Sicherheit – scharfe Bilder bei Tag und Nacht.",
    "Netzwerk-Zubehör": "ist hochwertiges Netzwerk-Zubehör für den professionellen Aufbau und Betrieb deiner Infrastruktur.",
    "Access Points": "ist ein leistungsstarker WLAN-Access-Point für schnelles, stabiles WLAN auf großer Fläche.",
    "Router": "ist ein zuverlässiger Router für stabile Konnektivität und professionelles Netzwerk-Management.",
    "Tablets": "ist ein vielseitiges Tablet für den professionellen und privaten Einsatz.",
    "Antennen": "ist eine hochwertige Antenne für optimale Signalqualität und Reichweite.",
    "VoIP-Telefone": "ist ein professionelles VoIP-Telefon für kristallklare Sprachqualität im Business-Einsatz.",
    "Netzteile": "ist ein zuverlässiges Netzteil für sichere und stabile Stromversorgung.",
    "Kabel & Adapter": "ist ein hochwertiges Kabel/Adapter für zuverlässige Verbindungen.",
}
DEFAULT_TPL = "ist ein hochwertiges Markenprodukt – Originalware, sofort lieferbar."


def norm(s):
    return " ".join((s or "").split())


def icecat_lookup(ean, brand, mpn):
    """(long_desc, specs_html) oder (None, None). GTIN zuerst, dann Brand+MPN."""
    for params in ([{"GTIN": ean}] if ean else []) + ([{"Brand": brand, "ProductCode": mpn}] if brand and mpn else []):
        p = {"UserName": ICECAT_USER, "Language": "de"}; p.update(params)
        try:
            r = requests.get(ICECAT_API, params=p, headers={"Authorization": f"Bearer {ICECAT_TOKEN}"}, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        d = r.json().get("data", {}) or {}
        gi = d.get("GeneralInfo", {}) or {}
        summ = gi.get("SummaryDescription", {}) or {}
        long_d = norm(summ.get("LongSummaryDescription") or summ.get("ShortSummaryDescription") or "")
        # Specs
        specs = ""
        for grp in (d.get("FeaturesGroups") or []):
            for f in (grp.get("Features") or []):
                nm = ((f.get("Feature") or {}).get("Name") or {}).get("Value") or f.get("LocalValue")
                val = f.get("PresentationValue") or f.get("LocalValue")
                if nm and val:
                    specs += f"<tr><td><strong>{html.escape(str(nm))}</strong></td><td>{html.escape(str(val))}</td></tr>"
        specs_html = f'<table>{specs}</table>' if specs else ""
        if long_d or specs_html:
            return long_d, specs_html
    return None, None


def build_description(p, ice_long, ice_specs):
    title = norm(p.get("title", "")); brand = norm(p.get("brand", "")); typ = p.get("type_override", "")
    if ice_long or ice_specs:
        body = f"<p>{html.escape(ice_long)}</p>" if ice_long else f"<p><strong>{html.escape(title)}</strong></p>"
        if ice_specs:
            body += f'<div class="product-specs">{ice_specs}</div>'
        return body, "icecat"
    tpl = TEMPLATES.get(typ, DEFAULT_TPL)
    body = (f"<p><strong>{html.escape(title)}</strong> {tpl}</p>"
            f"<p>Marke: {html.escape(brand)}. Originalware, versandkostenfrei geliefert, mit 14 Tagen Rückgaberecht.</p>")
    return body, "template"


def seo_title(title):
    t = norm(title)
    return t if len(t) <= 65 else t[:65].rsplit(" ", 1)[0]


def seo_desc(title, brand):
    base = f"{norm(title)} – {norm(brand)}. Versandkostenfrei & sofort lieferbar bei Neovodeals."
    return base[:158]


def main():
    prods = json.load(open("kosatec_products.json", encoding="utf-8"))
    out = {}
    stats = {"icecat": 0, "template": 0, "no_image": 0}
    for i, p in enumerate(prods, 1):
        sku = p["sku"]
        ean = str(p.get("ean", "")).strip().lstrip("0")
        ice_long, ice_specs = icecat_lookup(ean, p.get("brand", ""), p.get("mpn_code", ""))
        desc, src = build_description(p, ice_long, ice_specs)
        stats[src] += 1
        img = p.get("kosatec_image", "")
        if not img:
            stats["no_image"] += 1
        out[sku] = {
            "title": norm(p.get("title", "")),
            "description_html": desc,
            "seo_title": seo_title(p.get("title", "")),
            "seo_description": seo_desc(p.get("title", ""), p.get("brand", "")),
            "image": img,
            "desc_source": src,
        }
        time.sleep(0.25)
        if i % 50 == 0:
            print(f"  {i}/{len(prods)} … Icecat {stats['icecat']} | Template {stats['template']}", flush=True)
    json.dump(out, open("kosatec_content.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n{len(out)} → kosatec_content.json | Icecat: {stats['icecat']} | Template: {stats['template']} | ohne Bild: {stats['no_image']}")


if __name__ == "__main__":
    main()
