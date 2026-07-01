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

from ebay_fees import resolve_fee_rate, is_hygiene_category


@dataclass
class PricingResult:
    purchase_price_net: float  # EK netto
    vk_gross: float            # Verkaufspreis brutto (Endkunde sieht)
    margin_eur: float          # absolute Marge in € (nach Retouren-Rücklage, = echter erwarteter Gewinn)
    margin_pct: float          # Marge in % des EK
    tier_markup: float         # angewendeter Aufschlag
    shipping_buffer: float     # Versand-Puffer der draufgerechnet wurde
    return_reserve_eur: float = 0.0  # zurückgehaltener Anteil für Retouren (BAB nimmt nichts zurück)
    fixed_order_fee_eur: float = 0.0  # fixe eBay-Gebühr pro Bestellung
    cash_margin_eur: float = 0.0      # Marge ohne Retouren-Rücklage


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


def get_fee_cost_multiplier(ebay_pricing_cfg: Dict[str, Any]) -> float:
    """Konservativer Faktor fuer eBay-Gebuehren.

    eBay weist gewerbliche Gebuehren netto aus. Solange nicht sicher ist, ob die
    Vorsteuer gezogen wird, kann die Gebuehren-USt als echter Kostenblock
    kalkuliert werden.
    """
    if ebay_pricing_cfg.get("treat_ebay_fee_vat_as_cost", False):
        return 1 + float(ebay_pricing_cfg.get("ebay_fee_vat_rate", 0.19))
    return 1.0


def get_order_fixed_fee_eur(total_gross: float, ebay_pricing_cfg: Dict[str, Any]) -> float:
    """Fixe eBay-Bestellgebuehr nach Bestellwert.

    Stand eBay DE gewerblich: 0,35 EUR bis 10 EUR, 0,45 EUR ueber 10 EUR.
    Die Werte sind netto; optional wird die Gebuehren-USt konservativ addiert.
    """
    threshold = float(ebay_pricing_cfg.get("order_fixed_fee_threshold_eur", 10.00))
    low = float(ebay_pricing_cfg.get("order_fixed_fee_low_eur", 0.35))
    high = float(ebay_pricing_cfg.get("order_fixed_fee_high_eur", 0.45))
    fee = high if total_gross > threshold else low
    return fee * get_fee_cost_multiplier(ebay_pricing_cfg)


def get_min_margin_eur(ek_net: float, ebay_pricing_cfg: Dict[str, Any],
                       category_id: str = "") -> float:
    """Mindestgewinn nach allen bekannten Kosten fuer einen Artikel."""
    hygiene_profit = float(ebay_pricing_cfg.get("hygiene_min_profit_eur", 0.0))
    if hygiene_profit and is_hygiene_category(category_id or ""):
        return hygiene_profit

    for tier in ebay_pricing_cfg.get("min_margin_tiers", []):
        if ek_net < float(tier["ek_max"]):
            return float(tier["min_margin_eur"])
    return float(ebay_pricing_cfg.get("min_margin_eur", 5.00))


def _select_ebay_margin_pct(ek_net: float, ebay_pricing_cfg: Dict[str, Any],
                            margin_key: str = "margin") -> float:
    """Ziel- oder Floor-Marge aus der eBay-Staffel."""
    fallback = float(ebay_pricing_cfg.get("fallback_margin", 0.10))
    for tier in ebay_pricing_cfg.get("margin_tiers", []):
        if ek_net < float(tier["ek_max"]):
            return float(tier.get(margin_key, tier.get("margin", fallback)))
    return fallback


def calculate_ebay_margin_eur(ek_net: float, vk_gross: float,
                              ebay_pricing_cfg: Dict[str, Any],
                              category_id: Optional[str] = None,
                              include_return_reserve: bool = True) -> float:
    """Echter Gewinn eines eBay-Preises nach bekannten Kosten.

    Gibt erwarteten Gewinn zurueck, wenn include_return_reserve=True, sonst
    Cash-Gewinn ohne Ruecklagenabzug.
    """
    shipping = float(ebay_pricing_cfg.get("shipping_cost_eur", 5.00))
    buyer_shipping = float(ebay_pricing_cfg.get("buyer_shipping_eur", 0.00))
    fee_mult = get_fee_cost_multiplier(ebay_pricing_cfg)
    base_fee = resolve_fee_rate(category_id or "", float(ebay_pricing_cfg.get("ebay_fee_rate", 0.13)))
    ebay_fee = (base_fee + float(ebay_pricing_cfg.get("campaign_fee_rate", 0.0))) * fee_mult
    vat = float(ebay_pricing_cfg.get("vat_rate", 0.19))

    return_reserve = float(ebay_pricing_cfg.get("return_reserve_rate", 0.0))
    if is_hygiene_category(category_id or ""):
        return_reserve = 0.0
    return_reserve_eur = ek_net * return_reserve if include_return_reserve else 0.0

    total_gross = vk_gross + buyer_shipping
    fixed_fee = get_order_fixed_fee_eur(total_gross, ebay_pricing_cfg)
    net_after_ebay = total_gross * (1 - ebay_fee) - fixed_fee
    net_without_vat = net_after_ebay / (1 + vat)
    return net_without_vat - ek_net - shipping - return_reserve_eur


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

    # 5b. Zahlungsgebühr (Shopify Payments) herausrechnen, damit nach Abzug der
    # Gebühr die Zielmarge übrig bleibt: VK/(1−%) + Fixgebühr. So bleibt der Preis
    # so günstig wie möglich, deckt aber die Gebühr (sonst verstecktes Minus,
    # v.a. bei den dünnen Hygiene-/Volumen-Preisen).
    pf_pct = float(pricing_cfg.get("payment_fee_pct", 0.0))
    pf_fixed = float(pricing_cfg.get("payment_fee_fixed_eur", 0.0))
    if pf_pct or pf_fixed:
        vk_gross = (vk_gross + pf_fixed) / (1 - pf_pct)

    # 6. Psychologische Rundung
    rounding = pricing_cfg.get("rounding_strategy", "psychological_99")
    vk_gross = _round_psychological(vk_gross, rounding)

    def _net_margin(vkg: float) -> float:
        """Marge nach Zahlungsgebühr UND MwSt (echter erwarteter Gewinn)."""
        net_after_fee = vkg - (pf_pct * vkg + pf_fixed)
        return (net_after_fee / (1 + vat)) - ek_net

    # Korrigiere falls Rundung Mindestmarge (nach Gebühr) unterschreitet
    while _net_margin(vk_gross) < min_margin:
        vk_gross = _round_psychological(vk_gross + 1.0, rounding)
        if vk_gross > 9999:  # Sicherheits-Stop
            break

    margin_eur = _net_margin(vk_gross)
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
                      shopify_vk_gross: Optional[float] = None,
                      category_id: Optional[str] = None,
                      margin_override: Optional[float] = None,
                      margin_key: str = "margin") -> PricingResult:
    """
    Berechnet den eBay-Verkaufspreis nach der exakten Formel:

        VK_brutto = (EK + Versand + EK × Marge%) × 1.19 ÷ (1 − eBay_fee) − Käufer-Versand

    Die eBay-Gebühr wird herausgerechnet (nicht draufaddiert!), weil eBay
    seinen Anteil vom VK nimmt — der Preis muss also so gesetzt werden,
    dass nach eBay-Abzug noch die gewünschte Marge übrig bleibt. eBay
    berechnet die Provision auf VK + vom Käufer gezahlte Versandkosten
    (siehe eBay-Gebührenseite: "Gesamtbetrag des Verkaufs umfasst den
    Artikelpreis... die Kosten für den vom Käufer gewählten Versanddienst"),
    nicht nur auf den Artikelpreis - konsistent mit repricer.py::calc_vk.

    Args:
        ek_net: Einkaufspreis netto in EUR
        ebay_pricing_cfg: das 'ebay_pricing' Sub-Dict aus config.yaml
        shopify_vk_gross: nicht mehr genutzt, nur für Rückwärtskompatibilität
        category_id: eBay-Kategorie-ID für die kategorie-spezifische Gebühr
            (siehe ebay_fees.py). Fehlt sie, greift ebay_pricing_cfg's
            ebay_fee_rate als Fallback (alte Pauschal-Logik).
    """
    if ek_net <= 0:
        raise ValueError(f"Ungültiger EK: {ek_net}")

    shipping = float(ebay_pricing_cfg.get("shipping_cost_eur", 5.00))
    buyer_shipping = float(ebay_pricing_cfg.get("buyer_shipping_eur", 0.00))
    # Gesamtgebühr = Grundgebühr (kategorie-abhängig, Fallback alte Pauschale) +
    # Promoted Listings (falls Kampagne aktiv, sonst campaign_fee_rate=0 in
    # config_shop2.yaml) - konsistent mit repricer.py::calc_vk
    fee_mult = get_fee_cost_multiplier(ebay_pricing_cfg)
    base_fee = resolve_fee_rate(category_id or "", float(ebay_pricing_cfg.get("ebay_fee_rate", 0.13)))
    ebay_fee = (base_fee + float(ebay_pricing_cfg.get("campaign_fee_rate", 0.0))) * fee_mult
    vat = float(ebay_pricing_cfg.get("vat_rate", 0.19))
    min_margin = get_min_margin_eur(ek_net, ebay_pricing_cfg, category_id or "")
    rounding = ebay_pricing_cfg.get("rounding_strategy", "psychological_99")
    return_reserve = float(ebay_pricing_cfg.get("return_reserve_rate", 0.0))

    # Versiegelte Hygieneartikel (Widerruf entfällt nach Öffnen, §312g BGB):
    # keine Retouren-Rücklage, fester Mindestgewinn statt %-Marge-Staffel,
    # um "auf Masse" nah an den Mitbewerber zu kommen.
    hygiene_profit = float(ebay_pricing_cfg.get("hygiene_min_profit_eur", 0.0))
    if hygiene_profit and is_hygiene_category(category_id or ""):
        return_reserve = 0.0
        min_margin = hygiene_profit
        margin_pct = hygiene_profit / ek_net
    else:
        margin_pct = margin_override if margin_override is not None else _select_ebay_margin_pct(
            ek_net, ebay_pricing_cfg, margin_key=margin_key
        )

    # Kostenbasis: EK + Versand + Marge auf EK + Retouren-Rücklage auf EK
    # (BAB nimmt nichts zurück - jede Kundenretoure ist ein voller EK-Verlust,
    # die Rücklage verteilt dieses Risiko auf alle verkauften Einheiten).
    # Die fixe eBay-Bestellgebühr wird separat addiert, weil sie kein Teil der
    # Verkaufs-MwSt-Basis ist. Sie hängt vom finalen Bestellwert ab, deshalb
    # wird sie kurz iterativ stabilisiert.
    fixed_order_fee = float(ebay_pricing_cfg.get("order_fixed_fee_high_eur", 0.45)) * fee_mult
    vk_gross_raw = 0.0
    for _ in range(4):
        cost_base = ek_net + shipping + ek_net * margin_pct + ek_net * return_reserve

        # MwSt drauf, dann eBay-Gebühr herausrechnen - eBay rechnet seine Provision
        # auf (VK + Käufer-Versand), nicht nur auf VK:
        # (VK_brutto + buyer_shipping) × (1 − eBay_fee)
        #     = cost_base × (1 + vat) + fixed_fee
        # → VK_brutto = (cost_base × (1 + vat) + fixed_fee)
        #                / (1 − eBay_fee) − buyer_shipping
        vk_gross_raw = (cost_base * (1 + vat) + fixed_order_fee) / (1 - ebay_fee) - buyer_shipping
        next_fixed = get_order_fixed_fee_eur(vk_gross_raw + buyer_shipping, ebay_pricing_cfg)
        if abs(next_fixed - fixed_order_fee) < 0.001:
            break
        fixed_order_fee = next_fixed

    # Psychologische Rundung
    vk_gross = _round_psychological(vk_gross_raw, rounding)

    return_reserve_eur = ek_net * return_reserve

    # Mindestmarge sicherstellen — Rücklage zählt NICHT als Marge, sonst
    # würde sie effektiv mitverkauft statt zurückgehalten zu werden
    def actual_margin(vk: float) -> float:
        return calculate_ebay_margin_eur(
            ek_net, vk, ebay_pricing_cfg, category_id=category_id,
            include_return_reserve=True,
        )

    while actual_margin(vk_gross) < min_margin:
        vk_gross = _round_psychological(vk_gross + 1.0, rounding)
        if vk_gross > 99999:
            break

    margin_eur = actual_margin(vk_gross)
    cash_margin_eur = calculate_ebay_margin_eur(
        ek_net, vk_gross, ebay_pricing_cfg, category_id=category_id,
        include_return_reserve=False,
    )
    fixed_order_fee = get_order_fixed_fee_eur(vk_gross + buyer_shipping, ebay_pricing_cfg)
    margin_pct_real = (margin_eur / ek_net) * 100

    return PricingResult(
        purchase_price_net=round(ek_net, 2),
        vk_gross=round(vk_gross, 2),
        margin_eur=round(margin_eur, 2),
        return_reserve_eur=round(return_reserve_eur, 2),
        fixed_order_fee_eur=round(fixed_order_fee, 2),
        cash_margin_eur=round(cash_margin_eur, 2),
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
