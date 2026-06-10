"""
title_optimizer.py
==================
Generiert SEO-optimierte deutsche eBay-Titel (max. 80 Zeichen).

Ablauf:
  1. Liest enrichment_index.csv (title_full, short_summary, brand)
  2. Generiert für jedes Produkt einen optimierten Titel
  3. Exportiert title_review.csv  → du prüfst Vorher/Nachher
  4. Nach deiner Freigabe: apply_seo_titles.py pusht die Änderungen

eBay-SEO-Regeln:
  - Marke + Produkttyp (Deutsch) + Hauptspecs + NEU
  - 70-80 Zeichen anstreben
  - Keine Sonderzeichen (!, *, @)
  - Keine Füllwörter ("Top", "Super", "Günstig")
  - Wichtigste Keywords zuerst

Aufruf:
  python title_optimizer.py           # alle Produkte
  python title_optimizer.py --limit 50
  python title_optimizer.py --dry-run  # nur anzeigen
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

ENRICHMENT_FILE = "enrichment_index.csv"
REVIEW_FILE     = "title_review.csv"
TARGET_MIN      = 30   # Mindestlänge damit wir den neuen Titel akzeptieren
TARGET_MAX      = 80

# ─── Bekannte Marken (für Brand-Extraktion aus Titel) ────────────────────────
# Enthält Marken mit Leerzeichen oder Sonderzeichen zuerst
KNOWN_BRANDS = [
    # Multi-Word / Sonderzeichen zuerst
    "G.Skill", "TP-Link", "D-Link", "Micro-Star", "Be Quiet", "be quiet!",
    "Club 3D", "Club3D", "Western Digital", "Kingston Technology",
    "Crucial Technology",
    # Einwort
    "AVM", "APC", "Asus", "ASUS", "MSI", "Gigabyte", "ASRock", "Corsair",
    "Kingston", "Crucial", "Samsung", "Seagate", "WD", "Toshiba", "Hitachi",
    "Intel", "AMD", "NVIDIA", "Qualcomm",
    "Logitech", "Razer", "Corsair", "SteelSeries", "Roccat",
    "Thrustmaster", "NZXT", "Cooler Master", "CoolerMaster",
    "Fractal", "Lian Li",
    "SanDisk", "Transcend", "Verbatim", "Lexar", "PNY",
    "HP", "Hewlett-Packard", "Dell", "Lenovo", "Acer", "Asus",
    "Microsoft", "Apple", "Google",
    "Ubiquiti", "Netgear", "Zyxel", "Fritz", "FRITZ", "Devolo", "Linksys",
    "Belkin", "Synology", "QNAP", "TerraMaster",
    "Philips", "Sony", "Panasonic", "JBL", "Jabra", "Plantronics", "Poly",
    "Sennheiser", "Bose", "Harman", "Creative",
    "Canon", "Epson", "Brother", "Xerox", "Lexmark",
    "Anker", "EcoFlow", "NOCO", "Ecoflow",
    "Hama", "Delock", "Club3D",
    "Wacom", "Huion",
    "LG", "BenQ", "AOC", "Iiyama", "ViewSonic",
    "Elgato", "Streamdeck",
    "Sharkoon", "Caseking",
    "Startech", "StarTech",
]


def extract_brand_from_title(title: str, brand_stored: str) -> str:
    """
    Gibt die Marke zurück.
    Priorität: 1) aus enrichment_index, 2) aus KNOWN_BRANDS, 3) erstes Wort des Titels
    """
    if brand_stored and brand_stored.strip():
        return brand_stored.strip()

    t = title.strip()

    # Bekannte Marken prüfen (längste zuerst für Multi-Word-Marken)
    for brand in sorted(KNOWN_BRANDS, key=len, reverse=True):
        if t.lower().startswith(brand.lower()):
            return brand

    # Fallback: erstes Wort wenn Großbuchstabe
    first_word = t.split()[0] if t else ""
    if first_word and (first_word[0].isupper() or first_word.isupper()):
        # Zahlen, reine Sonderzeichen ausschließen
        if re.match(r"[A-Za-z]", first_word):
            return first_word.rstrip("!,.;:")

    return ""


# ─── Produkttyp-Erkennung ──────────────────────────────────────────────────────

PRODUCT_TYPES = [
    # ── Speicher ──────────────────────────────────────────────────────────────
    (r"memory module|arbeitsspeicher|dimm|sodimm|ram\b",   "RAM Arbeitsspeicher"),
    (r"nvme|m\.2.*ssd|ssd.*m\.2",                          "M.2 NVMe SSD"),
    (r"internal.*ssd|ssd.*internal|2\.5.*ssd|ssd.*2\.5",   "Interne SSD"),
    (r"\bssd\b",                                            "SSD"),
    (r"hdd|hard.*disk|festplatte",                         "Festplatte HDD"),
    (r"usb.*stick|flash.*drive|pen.*drive|datatraveler|cruzer|jetflash",
                                                            "USB-Stick"),
    (r"sd.*card|microsd|micro.*sd|speicherkarte|memory.*card",
                                                            "Speicherkarte"),
    (r"nas.*drive|network.*storage",                       "NAS Festplatte"),

    # ── Netzwerk ──────────────────────────────────────────────────────────────
    (r"wifi.*adapter|wlan.*adapter|wireless.*adapter|usb.*wifi|usb.*wlan",
                                                            "WLAN USB Adapter"),
    (r"wifi.*card|wlan.*card|pcie.*wifi",                  "WLAN PCIe Karte"),
    (r"network.*switch|ethernet.*switch|\bswitch\b(?!.*power)",
                                                            "Netzwerk Switch"),
    (r"wifi.*router|wlan.*router|\brouter\b",              "WLAN Router"),
    (r"access.*point|wifi.*ap\b",                          "WLAN Access Point"),
    (r"powerline|devolo|fritz.*powerline",                 "Powerline Adapter"),
    (r"network.*card|lan.*card|ethernet.*adapter",         "Netzwerkkarte"),
    (r"dsl.*modem|\bmodem\b",                              "DSL Modem"),

    # ── Peripherie ────────────────────────────────────────────────────────────
    (r"gaming.*mouse|gaming.*maus",                        "Gaming Maus"),
    (r"\bmouse\b|\bmaus\b",                                "Maus"),
    (r"gaming.*keyboard|gaming.*tastatur|mechanical.*keyboard",
                                                            "Gaming Tastatur"),
    (r"\bkeyboard\b|\btastatur\b",                         "Tastatur"),
    (r"gaming.*headset",                                   "Gaming Headset"),
    (r"\bheadset\b",                                       "Headset"),
    (r"wireless.*headphone|bluetooth.*headphone|kopfhörer.*bluetooth",
                                                            "Bluetooth Kopfhörer"),
    (r"\bheadphone\b|\bkopfhörer\b",                       "Kopfhörer"),
    (r"gaming.*controller|gamepad",                        "Gaming Controller"),
    (r"\bwebcam\b|\bweb.*camera\b",                        "Webcam"),
    (r"numeric.*pad|numpad|num.*pad",                      "Nummernblock Numpad"),
    (r"trackpad|touchpad",                                 "Touchpad"),
    (r"scanner\b(?!.*antivirus)",                          "Scanner"),
    (r"\bprinter\b|\bdrucker\b|\bmfp\b|\ball.*in.*one.*print",
                                                            "Drucker"),
    (r"toner|tonerkartusche",                              "Toner Kartusche"),
    (r"ink.*cartridge|tintenpatrone|druckpatrone",         "Tintenpatrone"),

    # ── Kabel & Adapter ───────────────────────────────────────────────────────
    (r"hdmi.*cable|hdmi.*kabel",                           "HDMI Kabel"),
    (r"displayport.*cable|dp.*cable|displayport.*kabel",   "DisplayPort Kabel"),
    (r"usb-c.*cable|type-c.*cable|usb.*c.*kabel",          "USB-C Kabel"),
    (r"usb.*cable|usb.*kabel",                             "USB Kabel"),
    (r"ethernet.*cable|lan.*kabel|cat.*\d.*kabel|patch.*kabel",
                                                            "LAN Netzwerkkabel"),
    (r"audio.*cable|klinken.*kabel|aux.*kabel|3\.5.*kabel",
                                                            "Audio Klinken Kabel"),
    (r"vga.*cable|vga.*kabel",                             "VGA Kabel"),
    (r"thunderbolt.*cable|thunderbolt.*kabel",             "Thunderbolt Kabel"),
    (r"usb.*hub|hub.*usb",                                 "USB Hub"),
    (r"kvm.*switch",                                       "KVM Switch"),
    (r"docking.*station|dock.*station|\bdock\b",           "Docking Station"),
    (r"usb-c.*adapter|type-c.*adapter|usb.*c.*adapter",    "USB-C Adapter"),
    (r"hdmi.*adapter|adapter.*hdmi",                       "HDMI Adapter"),
    (r"\badapter\b",                                       "Adapter"),

    # ── Audio / Video ─────────────────────────────────────────────────────────
    (r"bluetooth.*speaker|bluetooth.*lautsprecher|wireless.*speaker",
                                                            "Bluetooth Lautsprecher"),
    (r"\bspeaker\b|\blautsprecher\b",                      "Lautsprecher"),
    (r"soundbar",                                          "Soundbar"),
    (r"audio.*receiver|bluetooth.*receiver|audio.*empfänger",
                                                            "Audio Empfänger"),
    (r"sound.*card|soundkarte|audio.*card",                "Soundkarte"),
    (r"microphone|\bmikrofon\b|\bmic\b",                   "Mikrofon"),
    (r"streaming.*stick|fire.*tv|chromecast|hdmi.*stick",  "Streaming Stick"),
    (r"\bprojector\b|\bbeamer\b",                          "Beamer Projektor"),
    (r"\bmonitor\b|\bdisplay\b(?!.*port)|\bbildschirm\b",  "Monitor"),

    # ── Smart Home ────────────────────────────────────────────────────────────
    (r"smart.*plug|steckdose.*smart|fritz.*dect|zwischenstecker.*smart|dect.*\d+.*smart|smart.*\d+.*steckdose",
                                                            "Smarte Steckdose"),
    (r"smart.*bulb|smart.*light|smart.*lampe",             "Smart LED Lampe"),
    (r"smart.*strip|led.*strip.*smart",                    "Smart LED Streifen"),
    (r"smart.*thermostat|heizkörper.*thermostat",          "Smart Thermostat"),
    (r"zigbee.*hub|smart.*hub|smart.*bridge",              "Smart Home Hub"),

    # ── Power ─────────────────────────────────────────────────────────────────
    (r"power.*bank|powerbank",                             "Powerbank"),
    (r"wireless.*charger|qi.*charger|induktiv.*laden",     "Wireless Ladegerät"),
    (r"usb.*charger|usb.*ladegerät|multi.*charger",        "USB Ladegerät"),
    (r"laptop.*charger|notebook.*charger|laptop.*netzteil",
                                                            "Laptop Netzteil"),
    (r"ups\b|usv\b|uninterruptible",                       "USV Stromversorgung"),
    (r"pdu\b|power.*distribution",                         "Steckdosenleiste PDU"),
    (r"extension.*lead|steckdosenleiste|verlängerungskabel.*steckdose",
                                                            "Steckdosenleiste"),

    # ── Computer-Komponenten ──────────────────────────────────────────────────
    (r"graphics.*card|gpu\b|geforce|radeon.*rx|grafikkarte",
                                                            "Grafikkarte"),
    (r"cpu\b|processor\b|prozessor\b|core i[3579]|ryzen [3579]",
                                                            "Prozessor CPU"),
    (r"motherboard|mainboard|hauptplatine",                "Mainboard"),
    (r"power.*supply|netzteil|atx.*psu|\bpsu\b",           "ATX Netzteil"),
    (r"cpu.*cooler|cpu.*kühler|cpu.*lüfter|tower.*cooler", "CPU Kühler"),
    (r"pc.*case|computer.*case|tower.*case|gehäuse",       "PC Gehäuse"),
    (r"case.*fan|gehäuse.*lüfter|120mm.*fan|140mm.*fan",   "Gehäuse Lüfter"),

    # ── Zubehör ───────────────────────────────────────────────────────────────
    (r"laptop.*stand|notebook.*stand|laptop.*halterung",   "Laptop Ständer"),
    (r"laptop.*bag|notebook.*bag|laptop.*tasche",          "Laptop Tasche"),
    (r"screen.*protector|displayschutzfolie",              "Displayschutzfolie"),
    (r"tablet.*case|tablet.*hülle",                        "Tablet Hülle"),
    (r"phone.*case|smartphone.*hülle|handy.*hülle",        "Handy Hülle"),
    (r"kensington.*lock|cable.*lock|notebook.*lock",       "Notebook Schloss"),
    (r"label.*maker|label.*printer|etikettendrucker",      "Etikettendrucker"),
    (r"barcode.*scanner|qr.*scanner",                      "Barcode Scanner"),
]


def detect_product_type(title: str, summary: str) -> str:
    combined = (title + " " + summary).lower()
    for pattern, product_type in PRODUCT_TYPES:
        if re.search(pattern, combined):
            return product_type
    return ""


# ─── Spec-Extraktion ──────────────────────────────────────────────────────────

def extract_specs(title: str, summary: str, product_type: str = "") -> list[str]:
    """Extrahiert die wichtigsten Specs für den eBay-Titel."""
    combined = (title + " " + summary).lower()
    pt = product_type.lower()
    specs: list[str] = []

    # Kapazität (Speicher/Datenträger)
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(tb|gb|mb)\b", combined)
    if m:
        val = m.group(1)
        if val.endswith(".0"):
            val = val[:-2]
        unit = m.group(2).upper()
        specs.append(f"{val}{unit}")

    # RAM-Geschwindigkeit MHz
    m = re.search(r"\b(\d{3,4})\s*mhz\b", combined)
    if m:
        specs.append(f"{m.group(1)}MHz")

    # DDR-Generation
    m = re.search(r"\b(ddr[45]?x?)\b", combined)
    if m:
        specs.append(m.group(1).upper())

    # USB-Version
    m = re.search(r"\busb\s*(\d[\.\d]*)\b", combined)
    if m:
        specs.append(f"USB {m.group(1)}")

    # WiFi-Standard
    m = re.search(r"\b(wi-fi\s*\d+|wifi\s*\d+|802\.11\s*\w+)\b", combined)
    if m:
        val = m.group(1).replace("wifi ", "Wi-Fi ").replace("wi-fi ", "Wi-Fi ")
        specs.append(val.strip())
    elif re.search(r"\bax\b", combined) and re.search(r"wifi|wlan|wireless", combined):
        specs.append("Wi-Fi 6")
    elif re.search(r"\bac\b", combined) and re.search(r"wifi|wlan|wireless", combined):
        specs.append("Wi-Fi 5")

    # Bluetooth-Version
    m = re.search(r"\bbluetooth\s*(\d+(?:\.\d+)?)\b", combined)
    if m:
        specs.append(f"Bluetooth {m.group(1)}")
    elif "bluetooth" in combined and "bluetooth" not in pt.lower():
        specs.append("Bluetooth")

    # Auflösung / DPI
    m = re.search(r"\b(\d{3,4})\s*dpi\b", combined)
    if m:
        specs.append(f"{m.group(1)} DPI")

    # 4K / Full HD
    if re.search(r"\b4k\b|\b2160p\b|\buhd\b", combined):
        specs.append("4K UHD")
    elif re.search(r"\bfull\s*hd\b|\b1080p\b|\bfhd\b", combined):
        specs.append("Full HD")

    # Kabellänge
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*m\b(?!\w)", combined)
    if m and float(m.group(1)) <= 30:
        specs.append(f"{m.group(1)}m")

    # IP-Schutz
    m = re.search(r"\bip\s*(\d{2})\b", combined)
    if m:
        specs.append(f"IP{m.group(1)}")

    # PoE
    if re.search(r"\bpoe\b", combined):
        specs.append("PoE")

    # Ports-Anzahl
    m = re.search(r"\b(\d+)\s*-?\s*port\b", combined)
    if m and int(m.group(1)) > 1:
        specs.append(f"{m.group(1)}-Port")

    # Watt
    m = re.search(r"\b(\d{2,4})\s*w\b(?!\w)", combined)
    if m and 10 <= int(m.group(1)) <= 2000:
        specs.append(f"{m.group(1)}W")

    # Kabellos / Wireless (nur wenn nicht schon im Produkttyp)
    if re.search(r"\bwireless\b|\bkabellos\b", combined):
        specs_lower = " ".join(specs).lower()
        if "kabellos" not in specs_lower and "bluetooth" not in specs_lower:
            specs.append("kabellos")

    # NVMe
    if re.search(r"\bnvme\b", combined) and "NVMe" not in specs:
        specs.append("NVMe")

    # PCIe Generation
    m2 = re.search(r"\bpcie\s*(\d+)\b|\bpci-e\s*(\d+)\b", combined)
    if m2:
        gen = m2.group(1) or m2.group(2)
        specs.append(f"PCIe {gen}")

    # Leise / Silent
    if re.search(r"\bsilent\b|\bleise\b", combined):
        specs.append("leise")

    # Kit 2x (RAM)
    if re.search(r"\b2\s*x\s*\d+\s*gb\b", combined):
        specs.append("2x Kit")

    # Deduplizieren
    seen: set[str] = set()
    unique: list[str] = []
    for s in specs:
        key = s.lower().replace(" ", "")
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return unique


# ─── Modellnummer-Extraktion ──────────────────────────────────────────────────

MODEL_NOISE_WORDS = {
    "usb", "hdmi", "vga", "lan", "wan", "poe", "rgb", "ssd", "hdd",
    "ram", "ddr", "nvme", "wireless", "bluetooth", "wifi", "wlan",
    "cable", "kabel", "hub", "adapter", "switch", "dock", "charger",
    "mouse", "keyboard", "headset", "speaker", "webcam", "monitor",
    "printer", "scanner", "stick", "drive", "blade", "mini", "plus",
    "silent", "pro", "max", "lite", "slim", "nano", "new",
}


def extract_model(title: str, brand: str) -> str:
    """Extrahiert eine Modellbezeichnung aus dem Titel."""
    t = title.strip()

    # Marke aus Anfang entfernen
    if brand:
        if t.lower().startswith(brand.lower()):
            t = t[len(brand):].strip(" -–_")

    # Produkttyp-Suffixe am Ende entfernen
    # Längere Suffixe ZUERST, damit "usb flash drive" vor "flash drive" trifft
    stop_suffixes = [
        "usb flash drive", "solid state drive", "hard disk drive",
        "flash drive", "memory module", "access point",
        "mouse", "keyboard", "headset", "webcam",
        "printer", "scanner", "monitor", "speaker", "receiver",
        "smart plug", "steckdose", "cable", "kabel",
        "hub", "adapter", "switch", "router", "charger",
        "dock", "headphone", "kopfhörer", "microphone", "mikrofon",
    ]
    t_lower = t.lower()
    for sw in stop_suffixes:
        if t_lower.endswith(sw):
            t = t[:-(len(sw))].strip(" -–_,")
            t_lower = t.lower()
            break

    # Bekannte Modell-Muster
    # Priorität: "F4-3200C16D-32GVK", "UAP-AC-PRO", "M330", "MX Keys"
    m = re.search(r"([A-Z][A-Z0-9]{1,}\s*[A-Z0-9]+[A-Z0-9\-]*|[A-Z]+[-]?[A-Z]*\d+[A-Z0-9\-]*)", t)
    if m:
        model = m.group(1).strip()
        # Reine Noise-Words ablehnen
        if model.lower() not in MODEL_NOISE_WORDS and 2 <= len(model) <= 25:
            return model

    # Fallback: ganzer Rest, wenn kurz und kein Noise-Word
    t = t.strip()
    words = t.split()
    # Noise-Words vom Ende entfernen
    while words and words[-1].lower() in MODEL_NOISE_WORDS:
        words.pop()
    t = " ".join(words)

    if t and 2 <= len(t) <= 20 and t[0].isupper():
        # Nur akzeptieren wenn alphanumerisch und kein reines Noise
        if not all(w.lower() in MODEL_NOISE_WORDS for w in t.split()):
            return t

    return ""


# ─── Titel-Generator ─────────────────────────────────────────────────────────

def generate_seo_title(title_full: str, short_summary: str, brand_stored: str) -> str:
    """
    Generiert einen SEO-optimierten deutschen eBay-Titel (max 80 Zeichen).
    Format: Brand + [Modell] + Produkttyp + Specs + NEU
    """
    t_full  = (title_full or "").strip()
    summary = (short_summary or "").strip()

    brand        = extract_brand_from_title(t_full, brand_stored)
    product_type = detect_product_type(t_full, summary)
    specs        = extract_specs(t_full, summary, product_type)
    model        = extract_model(t_full, brand)

    # Vermeide Modell wenn es identisch mit Marke ist
    if model and brand and model.lower() == brand.lower():
        model = ""

    # Teile zusammenbauen
    parts: list[str] = []
    if brand:
        parts.append(brand)
    if model and model.lower() not in brand.lower():
        parts.append(model)
    if product_type:
        parts.append(product_type)

    # Specs hinzufügen bis Limit
    budget = TARGET_MAX - len(" ".join(parts)) - len(" NEU") - 1
    for spec in specs:
        token = " " + spec if parts else spec
        if len(token) <= budget:
            parts.append(spec)
            budget -= len(token)
        if budget <= 0:
            break

    parts.append("NEU")

    # Duplikate auf Wort-Ebene entfernen (case-insensitive, Reihenfolge erhalten)
    # Damit "Bluetooth Audio" + "Audio Empfänger" → "Bluetooth Audio Empfänger"
    seen_words: set[str] = set()
    deduped_words: list[str] = []
    for part in parts:
        for word in part.split():
            wkey = word.lower().strip(".,")
            if wkey not in seen_words:
                seen_words.add(wkey)
                deduped_words.append(word)

    title = " ".join(deduped_words)

    # Sonderzeichen bereinigen (eBay erlaubt keine ! @ # etc.)
    title = re.sub(r"[!@#$%^&*()+={}\[\];:'\"\\|<>?]", "", title)
    title = re.sub(r"\s+", " ", title).strip()

    # Zu lang → am letzten Leerzeichen kürzen
    if len(title) > TARGET_MAX:
        title = title[:TARGET_MAX].rsplit(" ", 1)[0].rstrip(" ,")

    return title


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur anzeigen, keine Dateien schreiben")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # Enrichment-Datei lesen
    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    # title_seo Spalte hinzufügen wenn noch nicht vorhanden
    if "title_seo" not in fieldnames:
        fieldnames.append("title_seo")

    targets = rows if not args.limit else rows[:args.limit]

    print(f"Produkte: {len(targets)}")
    print(f"Generiere SEO-Titel …\n")

    review_rows = []
    improved = 0
    kept_original = 0

    for row in targets:
        title_full    = (row.get("title_full") or "").strip()
        short_summary = (row.get("short_summary") or "").strip()
        brand_stored  = (row.get("brand") or "").strip()
        ean           = (row.get("ean") or "").strip()
        existing_seo  = (row.get("title_seo") or "").strip()

        new_title = generate_seo_title(title_full, short_summary, brand_stored)

        # Neuer Titel wird akzeptiert wenn:
        # - mindestens TARGET_MIN Zeichen lang
        # - enthält "NEU"
        # - unterscheidet sich vom alten Titel
        use_new = (
            bool(new_title)
            and len(new_title) >= TARGET_MIN
            and "NEU" in new_title
            and new_title.lower() != title_full.lower()
        )

        final_title = new_title if use_new else (existing_seo or title_full)
        row["title_seo"] = final_title

        review_rows.append({
            "ean":        ean,
            "brand":      extract_brand_from_title(title_full, brand_stored),
            "title_alt":  title_full,
            "title_neu":  new_title,
            "final":      final_title,
            "len_alt":    len(title_full),
            "len_neu":    len(new_title),
            "ok":         "JA" if use_new else "NEIN",
        })

        if use_new:
            improved += 1
        else:
            kept_original += 1

    print(f"Verbessert:   {improved}")
    print(f"Unverändert:  {kept_original}")
    print(f"Gesamt:       {len(targets)}")

    if args.dry_run:
        print("\n[DRY-RUN] Keine Dateien geschrieben.")
        print("\n=== BEISPIELE (erste 15) ===")
        for r in review_rows[:15]:
            status = "✓" if r["ok"] == "JA" else "✗"
            print(f"\n{status} ALT ({r['len_alt']:2}): {r['title_alt']}")
            print(f"  NEU ({r['len_neu']:2}): {r['title_neu']}")
        return

    # Review-CSV
    review_fields = ["ean", "brand", "title_alt", "title_neu",
                     "final", "len_alt", "len_neu", "ok"]
    with open(REVIEW_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=review_fields)
        writer.writeheader()
        writer.writerows(review_rows)
    print(f"\n✓ Review-CSV: {REVIEW_FILE}")

    # enrichment_index.csv aktualisieren
    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"✓ Enrichment-Index: {ENRICHMENT_FILE}")
    print(f"\nNächster Schritt: Prüfe {REVIEW_FILE} → dann apply_seo_titles.py")


if __name__ == "__main__":
    main()
