import json
import yaml
from repricer import get_margin_tier, calc_vk


def test_pinned_prices_are_profitable():
    cfg = yaml.safe_load(open("config_shop2.yaml"))
    sm = json.load(open("supplier_map.json"))
    pinned = json.load(open("pinned_skus.json"))
    min_margin = cfg["ebay_pricing"].get("min_margin_eur", 5.0)

    for sku, p in pinned.items():
        ek = sm[sku]["ek"]
        floor_m, _ = get_margin_tier(ek, cfg)
        floor_price = calc_vk(ek, floor_m, cfg)
        assert p["price"] >= floor_price - 5, (
            f"{sku}: gepinnter Preis {p['price']} liegt deutlich unter Floor {floor_price:.2f} - Tippfehler?"
        )


if __name__ == "__main__":
    test_pinned_prices_are_profitable()
    print("ok")
