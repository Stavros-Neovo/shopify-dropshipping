"""
pricing.py
==========
Pricing-Logik für das Dropshipping-Automation-System.

Wendet die in config.yaml definierte Hybrid-Pricing-Staffel an:
1. Bestimme Markup-Faktor anhand des EK-Preis-Tiers
2. Addiere Versand-Puffer
3. Rechne MwSt. drauf
4. Erzwinge Mindest-Marge
5. Runde auf psychologischen Endpreis (X,99)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import math


@dataclass
class PricingResult:
    purchase_price_net: float  # EK netto
    vk_gross: float            # Verkaufspreis brutto (Endkunde sieht)
    margin_eur: float          # absolute Marge in € (nach Retouren-Rücklage, = echter erwarteter Gewinn)
    margin_pct: float          # Marge in % des EK
    tier_markup: float         # angewendeter Aufschlag
    shipping_buffer: float     # Versand-Puffer der draufgerechnet wurde
    return_reserve_eur: float = 0.0  # zurückgehaltener Anteil für Retouren (BAB nimmt nichts zurück)


def _select_tier(ek: float, tiers: List[Dict[str, float]]) -> float:
    """Findet den passenden Markup-Faktor für einen EK-Preis."""
    for tier in tiers:
        if ek < tier["ek_max"]:
            return float(tier["markup"])
    # Fallback: letzter Tier
    return float(tiers[-1]["markup"])


def _select_shipping_buffer(ek: float, buffers: List[Dict[str, float]]) -> float:
    """Findet den passenden Versand-Puffer für einen EK-Preis."""
    for b in buffers:
        if ek < b["ek_max"]:
            return float(b["buffer_eur"])
    return 0.0


def _round_psychological(price: float, strategy: str) -> float:
    """
    Rundet auf psychologischen Endpreis.
    - 'psychological_99': 12.43 -> 12.99, 12.50 -> 12.99
    - 'psychological_95': 12.43 -> 12.95
    - 'none': keine Rundung
    """
    if strategy == "psychological_99":
        # Auf vollen Euro aufrunden, dann -0,01
        return math.ceil(price) - 0.01
    elif strategy == "psychological_95":
        # Auf vollen Euro aufrunden, dann -0,05
        return math.ceil(price) - 0.05
    else:
        return round(price, 2)


def calculate_vk(ek_net: float, pricing_cfg: Dict[str, Any]) -> PricingResult:
    """
    Hauptfunktion: berechnet den Verkaufspreis aus einem EK-Preis.

    Args:
        ek_net: Einkaufspreis netto in EUR
        pricing_cfg: das 'pricing' Sub-Dict aus config.yaml

    Returns:
        PricingResult mit allen relevanten Werten für Logging & Diagnose
    """
    if ek_net <= 0:
        raise ValueError(f"Ungültiger EK: {ek_net}")

    # 1. Markup aus Staffel
    markup = _select_tier(ek_net, pricing_cfg["tiers"])

    # 2. Versand-Puffer
    buffer = _select_shipping_buffer(ek_net, pricing_cfg["shipping_buffer"])

    # 3. Netto-VK = (EK + Puffer) * (1 + Markup)
    vk_net = (ek_net + buffer) * (1 + markup)

    # 4. Mindest-Marge erzwingen (auf Netto-Basis, einfacher)
    min_margin = float(pricing_cfg["min_absolute_margin_eur"])
    if (vk_net - ek_net) < min_margin:
        vk_net = ek_net + min_margin

    # 5. MwSt drauf
    vat = float(pricing_cfg["vat_rate"])
    vk_gross = vk_net * (1 + vat)

    # 6. Psychologische Rundung
    rounding = pricing_cfg.get("rounding_strategy", "psychological_99")
    vk_gross = _round_psychological(vk_gross, rounding)

    # Korrigiere falls Rundung Mindestmarge unterschreitet
    while (vk_gross / (1 + vat)) - ek_net < min_margin:
        # Eine Stufe höher runden (nächste ,99)
        vk_gross = _round_psychological(vk_gross + 1.0, rounding)
        if vk_gross > 9999:  # Sicherheits-Stop
            break

    margin_eur = (vk_gross / (1 + vat)) - ek_net
    margin_pct = (margin_eur / ek_net) * 100

    return PricingResult(
        purchase_price_net=round(ek_net, 2),
        vk_gross=round(vk_gross, 2),
        margin_eur=round(margin_eur, 2),
        margin_pct=round(margin_pct, 1),
        tier_markup=markup,
        shipping_buffer=buffer,
    )


def calculate_ebay_vk(ek_net: float, ebay_pricing_cfg: Dict[str, Any],
                      shopify_vk_gross: Optional[float] = None) -> PricingResult:
    """
    Berechnet den eBay-Verkaufspreis nach der exakten Formel:

        VK_brutto = (EK + Versand + EK × Marge%) × 1.19 ÷ (1 − eBay_fee)

    Die eBay-Gebühr wird herausgerechnet (nicht draufaddiert!), weil eBay
    seinen Anteil vom VK nimmt — der Preis muss also so gesetzt werden,
    dass nach eBay-Abzug noch die gewünschte Marge übrig bleibt.

    Args:
        ek_net: Einkaufspreis netto in EUR
        ebay_pricing_cfg: das 'ebay_pricing' Sub-Dict aus config.yaml
        shopify_vk_gross: nicht mehr genutzt, nur für Rückwärtskompatibilität
    """
    if ek_net <= 0:
        raise ValueError(f"Ungültiger EK: {ek_net}")

    shipping = float(ebay_pricing_cfg.get("shipping_cost_eur", 5.00))
    ebay_fee = float(ebay_pricing_cfg.get("ebay_fee_rate", 0.13))
    vat = float(ebay_pricing_cfg.get("vat_rate", 0.19))
    min_margin = float(ebay_pricing_cfg.get("min_margin_eur", 5.00))
    rounding = ebay_pricing_cfg.get("rounding_strategy", "psychological_99")
    return_reserve = float(ebay_pricing_cfg.get("return_reserve_rate", 0.0))

    # Marge-Staffel
    margin_pct = 0.15  # Fallback
    for tier in ebay_pricing_cfg.get("margin_tiers", []):
        if ek_net < float(tier["ek_max"]):
            margin_pct = float(tier["margin"])
            break

    # Kostenbasis: EK + Versand + Marge auf EK + Retouren-Rücklage auf EK
    # (BAB nimmt nichts zurück - jede Kundenretoure ist ein voller EK-Verlust,
    # die Rücklage verteilt dieses Risiko auf alle verkauften Einheiten)
    cost_base = ek_net + shipping + ek_net * margin_pct + ek_net * return_reserve

    # MwSt drauf, dann eBay-Gebühr herausrechnen
    # VK_brutto × (1 − eBay_fee) = cost_base × (1 + vat)
    # → VK_brutto = cost_base × (1 + vat) / (1 − eBay_fee)
    vk_gross_raw = cost_base * (1 + vat) / (1 - ebay_fee)

    # Psychologische Rundung
    vk_gross = _round_psychological(vk_gross_raw, rounding)

    return_reserve_eur = ek_net * return_reserve

    # Mindestmarge sicherstellen — Rücklage zählt NICHT als Marge, sonst
    # würde sie effektiv mitverkauft statt zurückgehalten zu werden
    def actual_margin(vk: float) -> float:
        net_after_ebay = vk * (1 - ebay_fee)
        net_without_vat = net_after_ebay / (1 + vat)
        return net_without_vat - ek_net - shipping - return_reserve_eur

    while actual_margin(vk_gross) < min_margin:
        vk_gross = _round_psychological(vk_gross + 1.0, rounding)
        if vk_gross > 99999:
            break

    margin_eur = actual_margin(vk_gross)
    margin_pct_real = (margin_eur / ek_net) * 100

    return PricingResult(
        purchase_price_net=round(ek_net, 2),
        vk_gross=round(vk_gross, 2),
        margin_eur=round(margin_eur, 2),
        return_reserve_eur=round(return_reserve_eur, 2),
        margin_pct=round(margin_pct_real, 1),
        tier_markup=margin_pct,
        shipping_buffer=shipping,
    )


# Kleiner Selbsttest – führt eine Reihe von Beispiel-Berechnungen durch
if __name__ == "__main__":
    demo_cfg = {
        "vat_rate": 0.19,
        "tiers": [
            {"ek_max": 5.0, "markup": 2.50},
            {"ek_max": 15.0, "markup": 1.80},
            {"ek_max": 30.0, "markup": 1.20},
            {"ek_max": 50.0, "markup": 0.80},
            {"ek_max": 100.0, "markup": 0.60},
            {"ek_max": 200.0, "markup": 0.40},
            {"ek_max": 999999.0, "markup": 0.25},
        ],
        "min_absolute_margin_eur": 5.00,
        "shipping_buffer": [
            {"ek_max": 10.0, "buffer_eur": 1.50},
            {"ek_max": 25.0, "buffer_eur": 0.80},
            {"ek_max": 999999.0, "buffer_eur": 0.00},
        ],
        "rounding_strategy": "psychological_99",
    }

    print(f"{'EK':>8} | {'VK brutto':>10} | {'Marge €':>10} | {'Marge %':>8}")
    print("-" * 50)
    for ek in [1.50, 4.20, 8.90, 14.99, 24.00, 39.50, 75.00, 149.00, 299.00]:
        r = calculate_vk(ek, demo_cfg)
        print(f"{r.purchase_price_net:>8.2f} | {r.vk_gross:>10.2f} | "
              f"{r.margin_eur:>10.2f} | {r.margin_pct:>8.1f}")
