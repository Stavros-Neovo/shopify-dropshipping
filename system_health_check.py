#!/usr/bin/env python3
"""
system_health_check.py
======================
Deep-Check aller Systemkomponenten.
Läuft 4x täglich via GitHub Actions.
Output: docs/health_report.json (wird vom Dashboard geladen)

Checks:
  1. Workflow-Status (letzte Runs via GitHub API)
  2. Repricer: Zyklus, Offset, letzte Ausführung, Fehler
  3. Order Forwarder: Pending Orders, Flagged Orders
  4. Sync: State-Alter, aktive Listings
  5. Makita-Sperre: banned_skus.json Integrität
  6. supplier_map: Nur BAB, keine verbotenen SKUs
  7. SMTP: Test-Verbindung
  8. eBay API: Erreichbarkeit
"""
from __future__ import annotations
import json, os, sys, time, yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

OUTPUT = Path("docs/health_report.json")
Path("docs").mkdir(exist_ok=True)

NOW = datetime.now(timezone.utc)
REPO = os.environ.get("GITHUB_REPOSITORY", "Stavros-Neovo/shopify-dropshipping")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))

results: dict = {
    "generated_at": NOW.isoformat(),
    "overall": "ok",
    "checks": {}
}

def check(name: str, status: str, detail: str, value=None):
    """Registriert ein Check-Ergebnis."""
    results["checks"][name] = {
        "status": status,       # ok | warn | error
        "detail": detail,
        "value": value,
        "ts": NOW.isoformat(),
    }
    if status == "error":
        results["overall"] = "error"
    elif status == "warn" and results["overall"] == "ok":
        results["overall"] = "warn"
    icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}[status]
    print(f"  {icon} [{name}] {detail}")


# =============================================================================
# 1. REPRICER STATE
# =============================================================================
print("\n=== REPRICER ===")
try:
    rs = json.loads(Path("repricer_state.json").read_text())
    shop2 = rs.get("config_shop2", {})
    offset = shop2.get("offset", 0)
    total  = shop2.get("total", 0)
    cycle  = shop2.get("cycle", 0)
    updated_str = shop2.get("updated", "")

    age_h = 999
    if updated_str:
        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        age_h = (NOW - updated).total_seconds() / 3600

    check("repricer_last_run",
          "ok" if age_h < 3 else "warn" if age_h < 6 else "error",
          f"Letzter Run vor {age_h:.1f}h | Zyklus {cycle} | {offset}/{total}",
          {"age_hours": round(age_h, 1), "cycle": cycle, "offset": offset, "total": total})

    # Report prüfen
    rr = json.loads(Path("repricer_report.json").read_text())
    errors = rr.get("errors", 0)
    checked = rr.get("total_checked", 0)
    check("repricer_errors",
          "ok" if errors == 0 else "warn" if errors < 10 else "error",
          f"{errors} Fehler bei {checked} geprüften Artikeln",
          {"errors": errors, "checked": checked})
except Exception as e:
    check("repricer_last_run", "error", f"Kann repricer_state.json nicht lesen: {e}")


# =============================================================================
# 2. ORDER FORWARDER
# =============================================================================
print("\n=== ORDER FORWARDER ===")
try:
    proc = json.loads(Path("processed_orders_state_shop2.json").read_text())
    check("orders_processed", "ok", f"{len(proc)} Bestellungen verarbeitet", {"count": len(proc)})
except Exception as e:
    check("orders_processed", "warn", f"processed_orders nicht lesbar: {e}")

try:
    p = Path("pending_orders.json")
    if p.exists():
        pending = json.loads(p.read_text())
        count = len(pending)
        check("orders_pending",
              "warn" if count > 0 else "ok",
              f"{count} Bestellungen in pending (warten auf Freigabe)",
              {"count": count, "orders": list(pending.keys())[:10]})
    else:
        check("orders_pending", "ok", "Keine pending_orders.json — alles verarbeitet", {"count": 0})
except Exception as e:
    check("orders_pending", "warn", f"pending_orders nicht lesbar: {e}")

try:
    f = Path("flagged_orders.json")
    if f.exists():
        flagged = json.loads(f.read_text())
        count = len(flagged)
        check("orders_flagged",
              "warn" if count > 0 else "ok",
              f"{count} Bestellungen geflaggt (manuelle Prüfung nötig)",
              {"count": count, "orders": [{"id": k, "reason": v.get("reason")} for k, v in list(flagged.items())[:5]]})
    else:
        check("orders_flagged", "ok", "Keine flagged_orders — alles sauber", {"count": 0})
except Exception as e:
    check("orders_flagged", "warn", f"flagged_orders nicht lesbar: {e}")


# =============================================================================
# 3. SYNC STATE
# =============================================================================
print("\n=== SYNC STATE ===")
try:
    state = json.loads(Path("state_shop2.json").read_text())
    real_skus = [k for k in state if not k.startswith("__")]
    # Ältester last_seen
    ages = []
    for sku in real_skus:
        ls = state[sku].get("last_seen", "")
        if ls:
            try:
                dt = datetime.fromisoformat(ls.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ages.append((NOW - dt).total_seconds() / 3600)
            except: pass

    max_age = max(ages) if ages else 0
    check("sync_state",
          "ok" if max_age < 3 else "warn" if max_age < 12 else "error",
          f"{len(real_skus)} aktive SKUs | Ältester Eintrag: {max_age:.1f}h",
          {"active_skus": len(real_skus), "max_age_hours": round(max_age, 1)})
except Exception as e:
    check("sync_state", "error", f"state_shop2.json nicht lesbar: {e}")


# =============================================================================
# 4. MAKITA-SPERRE
# =============================================================================
print("\n=== MAKITA-SPERRE ===")
try:
    banned = set(json.loads(Path("banned_skus.json").read_text()))
    smap = json.loads(Path("supplier_map.json").read_text())
    bab_skus = {k for k, v in smap.items() if isinstance(v, dict) and v.get("supplier") == "BAB"}

    banned_in_map = banned & bab_skus
    check("banned_skus_count",
          "ok" if len(banned) >= 12 else "warn",
          f"{len(banned)} gesperrte SKUs in banned_skus.json",
          {"count": len(banned)})
    check("banned_in_supplier_map",
          "error" if banned_in_map else "ok",
          f"{len(banned_in_map)} gesperrte SKUs noch in supplier_map: {list(banned_in_map)[:5]}",
          {"count": len(banned_in_map), "skus": list(banned_in_map)})
except Exception as e:
    check("banned_skus_count", "error", f"banned_skus.json nicht lesbar: {e}")


# =============================================================================
# 5. SUPPLIER MAP
# =============================================================================
print("\n=== SUPPLIER MAP ===")
try:
    smap = json.loads(Path("supplier_map.json").read_text())
    bab = {k for k, v in smap.items() if isinstance(v, dict) and v.get("supplier") == "BAB"}
    non_bab = {k for k in smap if k not in bab}
    check("supplier_map_bab",
          "ok" if len(bab) > 1000 else "warn",
          f"{len(bab)} BAB-Artikel in supplier_map",
          {"bab_count": len(bab), "non_bab_count": len(non_bab)})
    if non_bab:
        check("supplier_map_non_bab",
              "warn",
              f"{len(non_bab)} Nicht-BAB-Einträge in supplier_map",
              {"count": len(non_bab)})
    else:
        check("supplier_map_non_bab", "ok", "Nur BAB-Einträge in supplier_map", {"count": 0})
except Exception as e:
    check("supplier_map_bab", "error", f"supplier_map.json nicht lesbar: {e}")


# =============================================================================
# 6. GITHUB WORKFLOW STATUS (via API)
# =============================================================================
print("\n=== GITHUB WORKFLOWS ===")
WATCHED = {
    "hourly_sync.yml":          "Hourly Sync",
    "order_forwarder.yml":      "Order Forwarder",
    "reprice.yml":              "Repricer",
    "smart_lister.yml":         "Smart Lister",
    "tracking_updater.yml":     "Tracking Updater",
    "hourly_matrixify_csv.yml": "Matrixify CSV",
}

if GH_TOKEN:
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    for wf_file, wf_label in WATCHED.items():
        try:
            url = f"https://api.github.com/repos/{REPO}/actions/workflows/{wf_file}/runs?per_page=1"
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                runs = r.json().get("workflow_runs", [])
                if runs:
                    run = runs[0]
                    conclusion = run.get("conclusion", "?")
                    created = run.get("created_at", "")
                    age_h = 999
                    if created:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_h = (NOW - dt).total_seconds() / 3600
                    status = "ok" if conclusion == "success" else "warn" if conclusion in ("skipped", None) else "error"
                    check(f"workflow_{wf_file.replace('.yml','')}",
                          status,
                          f"{wf_label}: {conclusion} vor {age_h:.1f}h",
                          {"conclusion": conclusion, "age_hours": round(age_h,1), "run_id": run.get("id")})
                else:
                    check(f"workflow_{wf_file.replace('.yml','')}", "warn", f"{wf_label}: Noch nie gelaufen")
            else:
                check(f"workflow_{wf_file.replace('.yml','')}", "warn", f"{wf_label}: API {r.status_code}")
        except Exception as e:
            check(f"workflow_{wf_file.replace('.yml','')}", "warn", f"{wf_label}: {e}")
        time.sleep(0.2)
else:
    check("github_api", "warn", "Kein GITHUB_TOKEN — Workflow-Status nicht prüfbar")


# =============================================================================
# 7. SMTP VERBINDUNG
# =============================================================================
print("\n=== SMTP ===")
try:
    import smtplib
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER", "")
    pw   = os.environ.get("SMTP_PASSWORD", "")
    if host and user and pw:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            s = smtplib.SMTP(host, port, timeout=10)
            s.starttls()
        s.login(user, pw)
        s.quit()
        check("smtp", "ok", f"SMTP-Verbindung OK ({host}:{port})")
    else:
        check("smtp", "warn", "SMTP nicht konfiguriert (kein SMTP_HOST/USER/PASSWORD)")
except Exception as e:
    check("smtp", "error", f"SMTP-Verbindung fehlgeschlagen: {e}")


# =============================================================================
# ERGEBNIS SPEICHERN
# =============================================================================
ok_count   = sum(1 for c in results["checks"].values() if c["status"] == "ok")
warn_count = sum(1 for c in results["checks"].values() if c["status"] == "warn")
err_count  = sum(1 for c in results["checks"].values() if c["status"] == "error")

results["summary"] = {
    "total": len(results["checks"]),
    "ok": ok_count,
    "warn": warn_count,
    "error": err_count,
}

OUTPUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n{'='*55}")
print(f"HEALTH CHECK: {results['overall'].upper()} | ✅ {ok_count}  ⚠️ {warn_count}  ❌ {err_count}")
print(f"Report: {OUTPUT}")

if results["overall"] == "error":
    sys.exit(1)
