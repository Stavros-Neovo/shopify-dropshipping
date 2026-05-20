"""
inspect_feed.py
===============
Lädt eine Lieferanten-CSV von einer URL und zeigt:
  - Encoding-Erkennung
  - Trennzeichen-Erkennung
  - Spalten-Namen
  - Zeilen-Anzahl
  - Erste 2 Datenzeilen als Beispiel

Aufruf:
  python3 inspect_feed.py
  python3 inspect_feed.py --url <eigene-url>

Output kannst du komplett kopieren und an Claude schicken — daraus generiere ich
das passende Spalten-Mapping für config.yaml.
"""
from __future__ import annotations
import argparse
import csv
import io
import sys
from collections import Counter
import requests

DEFAULT_URL = "https://pricelist.bab-distribution.de/50429_a2Q2yGHHkSgMe4BV"


def detect_encoding(raw: bytes) -> str:
    """Schnelle Encoding-Heuristik für deutsche CSVs."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def detect_delimiter(text_sample: str) -> str:
    """Errät den Spalten-Trenner aus den ersten Zeilen."""
    candidates = [";", ",", "\t", "|"]
    counts = {c: text_sample.count(c) for c in candidates}
    # höchster Count gewinnt
    return max(counts, key=counts.get)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--max-bytes", type=int, default=2_000_000,
                        help="Maximal so viele Bytes runterladen (Schutz vor riesigen Feeds)")
    args = parser.parse_args()

    print(f"🔗 Lade {args.url} ...")
    auth = (args.user, args.password) if args.user else None
    try:
        r = requests.get(args.url, auth=auth, timeout=60, stream=True)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Download-Fehler: {e}")
        sys.exit(1)

    raw = b""
    for chunk in r.iter_content(chunk_size=64 * 1024):
        raw += chunk
        if len(raw) > args.max_bytes:
            print(f"   (… abgeschnitten bei {args.max_bytes:,} Bytes)")
            break

    print(f"✅ Geladen: {len(raw):,} Bytes")
    ct = r.headers.get("Content-Type", "(unbekannt)")
    cd = r.headers.get("Content-Disposition", "")
    print(f"   Content-Type: {ct}")
    if cd:
        print(f"   Content-Disposition: {cd}")

    # Encoding
    enc = detect_encoding(raw[:8192])
    print(f"   Erkanntes Encoding: {enc}")
    try:
        text = raw.decode(enc, errors="replace")
    except Exception as e:
        print(f"❌ Decode-Fehler: {e}")
        sys.exit(1)

    # Trennzeichen
    sample = "\n".join(text.splitlines()[:10])
    delim = detect_delimiter(sample)
    print(f"   Erkanntes Trennzeichen: '{delim}'")

    # CSV parsen
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = list(reader)
    if not rows:
        print("❌ Datei ist leer oder unparsbar")
        sys.exit(1)

    headers = rows[0]
    data_rows = rows[1:]
    print(f"\n📊 Daten:")
    print(f"   Zeilen total: {len(rows):,} (inkl. Header)")
    print(f"   Datenzeilen: {len(data_rows):,}")
    print(f"   Spalten: {len(headers)}")

    # Spalten-Liste
    print(f"\n📋 SPALTEN-NAMEN:")
    for i, h in enumerate(headers, 1):
        print(f"   {i:>3}. {h!r}")

    # Beispiel-Zeilen
    print(f"\n🧪 BEISPIEL-DATEN (erste 2 Zeilen, kompakt):")
    for idx, row in enumerate(data_rows[:2], 1):
        print(f"\n   --- Zeile {idx} ---")
        for h, v in zip(headers, row):
            v_short = (v[:80] + "…") if v and len(v) > 80 else v
            print(f"     {h}: {v_short}")


if __name__ == "__main__":
    main()
