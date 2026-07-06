"""
csv_loader.py
=============
Lädt den Lieferanten-Feed (CSV) von einer HTTP(S)-URL und normalisiert
die Spalten gemäß config.yaml -> csv.columns.
"""
from __future__ import annotations
import csv
import io
import logging
from typing import Dict, Any, Iterator, Optional
import os
import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger(__name__)


def download_csv(url: str, encoding: str = "utf-8",
                 user: Optional[str] = None,
                 password: Optional[str] = None,
                 timeout: int = 60) -> str:
    """Lädt CSV von URL und gibt den Text-Inhalt zurück."""
    auth = HTTPBasicAuth(user, password) if user else None
    log.info(f"Lade CSV von {url} ...")
    r = requests.get(url, auth=auth, timeout=timeout)
    r.raise_for_status()
    # Encoding manuell setzen (requests rät oft falsch)
    r.encoding = encoding
    text = r.text
    log.info(f"CSV geladen: {len(text):,} Zeichen")
    return text


def parse_csv(text: str, delimiter: str = ";") -> list[dict]:
    """Parst CSV-Text in eine Liste von Zeilen-Dicts."""
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader]
    log.info(f"CSV geparst: {len(rows):,} Zeilen")
    return rows


def normalize_row(row: dict, column_map: Dict[str, str]) -> dict:
    """
    Wandelt eine CSV-Zeile in das einheitliche interne Format um.

    column_map ist {"sku": "Artikelnummer", "title": "Produktname", ...}
    Liefert {"sku": "...", "title": "...", ...} mit getypten Werten.
    """
    result = {}
    for internal_name, csv_column in column_map.items():
        raw = row.get(csv_column, "")
        result[internal_name] = raw.strip() if isinstance(raw, str) else raw

    # Numerische Konvertierungen (deutsche Komma-Schreibweise unterstützen)
    for num_field in ("purchase_price", "weight_kg"):
        v = result.get(num_field, "")
        if isinstance(v, str):
            v = v.replace(",", ".").replace(" ", "")
            try:
                result[num_field] = float(v) if v else 0.0
            except ValueError:
                result[num_field] = 0.0

    # Stock als int
    s = result.get("stock", "")
    if isinstance(s, str):
        s = s.replace(",", "").replace(".", "")
        try:
            result["stock"] = int(s) if s else 0
        except ValueError:
            result["stock"] = 0

    return result


_TITLE_OVERRIDES = None


def _title_overrides() -> Dict[str, str]:
    """SKU -> korrigierter Titel aus title_overrides.yaml. Fixt falsche
    Lieferanten-Titel an EINER Stelle für Shop + eBay + SEO; wird bei jedem
    Feed-Load neu angewandt, ist also BAB-resistent."""
    global _TITLE_OVERRIDES
    if _TITLE_OVERRIDES is None:
        try:
            import yaml
            p = os.path.join(os.path.dirname(__file__), "title_overrides.yaml")
            with open(p, encoding="utf-8") as fh:
                _TITLE_OVERRIDES = yaml.safe_load(fh) or {}
        except (FileNotFoundError, ImportError):
            _TITLE_OVERRIDES = {}
    return _TITLE_OVERRIDES


def load_supplier_feed(cfg: Dict[str, Any]) -> Iterator[dict]:
    """
    Komfort-Funktion: Lädt, parst, normalisiert in einem Rutsch.
    Gibt einen Iterator über normalisierte Produkt-Dicts zurück.
    """
    csv_cfg = cfg["csv"]
    text = download_csv(
        url=csv_cfg["url"],
        encoding=csv_cfg.get("encoding", "utf-8"),
        user=os.getenv("CSV_HTTP_USER") or None,
        password=os.getenv("CSV_HTTP_PASSWORD") or None,
    )
    rows = parse_csv(text, delimiter=csv_cfg.get("delimiter", ";"))
    column_map = csv_cfg["columns"]
    overrides = _title_overrides()
    for raw in rows:
        item = normalize_row(raw, column_map)
        ov = overrides.get(item.get("sku"))
        if ov:
            item["title"] = ov
        yield item
