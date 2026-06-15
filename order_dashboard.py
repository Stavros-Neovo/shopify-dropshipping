#!/usr/bin/env python3
"""
order_dashboard.py — Bestellungs-Dashboard
===========================================
Lokaler Server auf http://localhost:8765
Auto-Refresh alle 30 Minuten.

Start:
  cd ~/Documents/Dropshipping
  python3 order_dashboard.py
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import sys
import webbrowser
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

PENDING_FILE  = "pending_orders.json"
FLAGGED_FILE  = "flagged_orders.json"
PROCESSED_PREFIX = "processed_orders_"

# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: str, data: dict | list):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_processed(order_id: str, config_path: str):
    """Trägt Order-ID in processed_orders_stateXXX.json ein."""
    state_base = Path(config_path).stem  # z.B. "config_shop2"
    proc_file = f"{PROCESSED_PREFIX}{state_base}.json"
    p = Path(proc_file)
    processed = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    if order_id not in processed:
        processed.append(order_id)
        save_json(proc_file, sorted(processed))


def build_and_send_email(order_data: dict) -> bool:
    """Baut Email aus gespeicherten Order-Daten und versendet sie."""
    cfg_path = order_data.get("shop_config", "config_shop2.yaml")
    try:
        cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    except Exception as e:
        log.error(f"Config nicht ladbar: {e}"); return False

    raw_order = order_data.get("_raw", {})
    order_id  = order_data.get("orderId", "?")
    addr      = order_data.get("address", {})
    items     = order_data.get("lineItems", [])

    # Email bauen
    order_date = order_data.get("creationDate", "")[:10]
    subject = f"Neue eBay-Bestellung #{order_id} – bitte versenden"

    lines = [
        "Hallo Sebastian,",
        "",
        f"bitte folgende eBay-Bestellung direkt an den Endkunden versenden:",
        "",
        "─" * 50,
        "LIEFERADRESSE",
        "─" * 50,
    ]

    # Vollständige Adresse aus _raw holen
    fulfillments = raw_order.get("fulfillmentStartInstructions", [])
    if fulfillments:
        ship_to = fulfillments[0].get("shippingStep", {}).get("shipTo", {})
        contact = ship_to.get("contactAddress", {})
        lines += [
            ship_to.get("fullName", addr.get("name", "")),
            contact.get("addressLine1", ""),
        ]
        if contact.get("addressLine2"):
            lines.append(contact["addressLine2"])
        lines += [
            f"{contact.get('postalCode', '')} {contact.get('city', '')}".strip(),
            contact.get("countryCode", addr.get("country", "")),
        ]
        phone = ship_to.get("primaryPhone", {}).get("phoneNumber", "")
        if phone:
            lines.append(f"Tel: {phone}")
    else:
        lines += [addr.get("name", ""), addr.get("zip", "") + " " + addr.get("city", ""), addr.get("country", "")]

    lines += ["", "─" * 50, "ARTIKEL", "─" * 50]
    for item in items:
        lines.append(f"  {item.get('quantity', 1)}x  {item.get('sku', '?')}  –  {item.get('title', '')}")

    lines += [
        "", "─" * 50,
        f"eBay Bestellnummer:  {order_id}",
        f"Bestelldatum:        {order_date}",
        "",
        "Bitte Sendungsverfolgungsnummer per Antwort-Mail.",
        "",
        "Vielen Dank!",
    ]

    body = "\n".join(str(l) for l in lines)

    # Senden
    supplier_cfg = cfg.get("supplier_email", {})
    sender = os.environ.get("SMTP_USER", "")
    to     = supplier_cfg.get("to", "")
    from_name = supplier_cfg.get("from_name", "eBay Shop")

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        host = os.environ["SMTP_HOST"]
        port = int(os.environ["SMTP_PORT"])
        user = os.environ["SMTP_USER"]
        pw   = os.environ["SMTP_PASSWORD"]
        if port == 465:
            with smtplib.SMTP_SSL(host, port) as s:
                s.login(user, pw); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as s:
                s.starttls(); s.login(user, pw); s.send_message(msg)
        log.info(f"✅ Mail gesendet: {subject}")
        return True
    except Exception as e:
        log.error(f"❌ Mail-Fehler: {e}"); return False


# ─────────────────────────────────────────────────────────────────────────────
# HTML Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def render_dashboard() -> str:
    pending = load_json(PENDING_FILE)
    flagged = load_json(FLAGGED_FILE)
    now_utc = datetime.now(timezone.utc)

    def age_str(iso: str) -> str:
        if not iso: return ""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            mins = int((now_utc - dt).total_seconds() / 60)
            if mins < 60: return f"vor {mins} Min"
            return f"vor {mins//60}h {mins%60}min"
        except Exception: return ""

    # Pending Order Cards
    pending_cards = ""
    if not pending:
        pending_cards = '<div class="empty">Keine offenen Bestellungen ✅</div>'
    else:
        for oid, o in sorted(pending.items(), key=lambda x: x[1].get("pending_since",""), reverse=True):
            items_html = "".join(
                f'<div class="item"><span class="sku">{i.get("sku","")}</span> '
                f'<span class="qty">×{i.get("quantity",1)}</span> '
                f'<span class="title">{i.get("title","")}</span></div>'
                for i in o.get("lineItems", [])
            )
            addr = o.get("address", {})
            addr_str = f'{addr.get("name","")} · {addr.get("zip","")} {addr.get("city","")} · {addr.get("country","")}'
            pending_since = age_str(o.get("pending_since",""))
            pending_cards += f'''
            <div class="card pending-card">
              <div class="card-header">
                <div>
                  <span class="order-id">#{oid}</span>
                  <span class="badge badge-pending">Wartend</span>
                </div>
                <div class="card-meta">
                  <span>📅 {o.get("creationDate","")[:10]}</span>
                  <span class="age">⏱ {pending_since}</span>
                </div>
              </div>
              <div class="address">📦 {addr_str}</div>
              <div class="items">{items_html}</div>
              <div class="actions">
                <button class="btn btn-approve" onclick="approveOrder('{oid}', this)">
                  ✅ Freigeben → BAB
                </button>
                <button class="btn btn-reject" onclick="rejectOrder('{oid}', this)">
                  ❌ Ablehnen
                </button>
              </div>
            </div>'''

    # Flagged Order Cards
    flagged_cards = ""
    if flagged:
        for oid, f in flagged.items():
            items_html = "".join(
                f'<div class="item"><span class="sku">{i.get("sku","")}</span> '
                f'<span class="title">{i.get("title","")}</span></div>'
                for i in f.get("items", [])
            )
            flagged_cards += f'''
            <div class="card flagged-card">
              <div class="card-header">
                <div>
                  <span class="order-id">#{oid}</span>
                  <span class="badge badge-flagged">⚠️ Gefahrgut</span>
                </div>
                <div class="card-meta"><span>Grund: {f.get("reason","")}</span></div>
              </div>
              <div class="items">{items_html}</div>
            </div>'''

    pending_count = len(pending)
    flagged_count = len(flagged)

    return f'''<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <title>Bestellungs-Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #0f1117; color: #e2e8f0; min-height: 100vh; }}
    .header {{ background: #1a1d2e; border-bottom: 1px solid #2d3748; padding: 20px 32px;
               display: flex; align-items: center; justify-content: space-between; }}
    .header h1 {{ font-size: 20px; font-weight: 700; color: #fff; }}
    .header-right {{ display: flex; align-items: center; gap: 16px; }}
    .countdown {{ font-size: 13px; color: #718096; }}
    .refresh-btn {{ background: #2d3748; border: 1px solid #4a5568; color: #e2e8f0;
                    padding: 7px 16px; border-radius: 8px; cursor: pointer; font-size: 13px; }}
    .refresh-btn:hover {{ background: #4a5568; }}
    .stats {{ display: flex; gap: 16px; padding: 24px 32px 0; }}
    .stat {{ background: #1a1d2e; border: 1px solid #2d3748; border-radius: 10px;
             padding: 16px 24px; min-width: 160px; }}
    .stat-num {{ font-size: 32px; font-weight: 700; }}
    .stat-label {{ font-size: 12px; color: #718096; margin-top: 4px; }}
    .stat-pending .stat-num {{ color: #f6ad55; }}
    .stat-flagged .stat-num {{ color: #fc8181; }}
    .stat-ok .stat-num {{ color: #68d391; }}
    .section {{ padding: 24px 32px; }}
    .section h2 {{ font-size: 15px; font-weight: 600; color: #a0aec0;
                   text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }}
    .card {{ background: #1a1d2e; border: 1px solid #2d3748; border-radius: 12px;
             padding: 20px; margin-bottom: 16px; transition: border-color 0.2s; }}
    .card:hover {{ border-color: #4a5568; }}
    .pending-card {{ border-left: 3px solid #f6ad55; }}
    .flagged-card {{ border-left: 3px solid #fc8181; }}
    .card-header {{ display: flex; justify-content: space-between; align-items: flex-start;
                    margin-bottom: 12px; }}
    .order-id {{ font-size: 16px; font-weight: 700; color: #fff; margin-right: 10px; }}
    .badge {{ font-size: 11px; padding: 3px 10px; border-radius: 20px; font-weight: 600; }}
    .badge-pending {{ background: #744210; color: #f6ad55; }}
    .badge-flagged  {{ background: #742a2a; color: #fc8181; }}
    .card-meta {{ display: flex; gap: 12px; font-size: 12px; color: #718096; }}
    .age {{ color: #f6ad55; }}
    .address {{ font-size: 13px; color: #a0aec0; margin-bottom: 12px; }}
    .items {{ margin-bottom: 16px; }}
    .item {{ display: flex; gap: 8px; align-items: baseline; padding: 5px 0;
             border-bottom: 1px solid #2d3748; font-size: 13px; }}
    .item:last-child {{ border-bottom: none; }}
    .sku {{ font-family: monospace; background: #2d3748; padding: 2px 8px;
             border-radius: 4px; font-size: 12px; color: #90cdf4; white-space: nowrap; }}
    .qty {{ color: #a0aec0; white-space: nowrap; }}
    .title {{ color: #e2e8f0; }}
    .actions {{ display: flex; gap: 12px; }}
    .btn {{ padding: 10px 20px; border-radius: 8px; border: none; cursor: pointer;
             font-size: 14px; font-weight: 600; transition: all 0.2s; }}
    .btn-approve {{ background: #276749; color: #9ae6b4; }}
    .btn-approve:hover {{ background: #2f855a; }}
    .btn-reject {{ background: #742a2a; color: #fc8181; }}
    .btn-reject:hover {{ background: #9b2c2c; }}
    .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .empty {{ color: #718096; font-size: 14px; padding: 32px; text-align: center;
              background: #1a1d2e; border-radius: 12px; border: 1px dashed #2d3748; }}
    .toast {{ position: fixed; top: 20px; right: 20px; padding: 12px 20px;
              border-radius: 10px; font-size: 14px; font-weight: 600;
              z-index: 1000; display: none; animation: slidein 0.3s ease; }}
    .toast-ok  {{ background: #276749; color: #9ae6b4; }}
    .toast-err {{ background: #742a2a; color: #fc8181; }}
    @keyframes slidein {{ from {{ transform: translateX(100px); opacity:0 }}
                           to   {{ transform: translateX(0);    opacity:1 }} }}
    .progress-bar {{ height: 3px; background: #2d3748; position: fixed; bottom: 0;
                     left: 0; right: 0; }}
    .progress-fill {{ height: 100%; background: #f6ad55; transition: width 1s linear; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>📦 Bestellungs-Dashboard</h1>
    <div class="header-right">
      <span class="countdown" id="countdown">Nächste Aktualisierung: 30:00</span>
      <button class="refresh-btn" onclick="refreshNow()">↻ Jetzt aktualisieren</button>
    </div>
  </div>

  <div class="stats">
    <div class="stat stat-pending">
      <div class="stat-num">{pending_count}</div>
      <div class="stat-label">Wartende Bestellungen</div>
    </div>
    <div class="stat stat-flagged">
      <div class="stat-num">{flagged_count}</div>
      <div class="stat-label">Gefahrgut / Manuell</div>
    </div>
    <div class="stat stat-ok">
      <div class="stat-num" id="sent-count">–</div>
      <div class="stat-label">Heute freigegeben</div>
    </div>
  </div>

  <div class="section">
    <h2>⏳ Warten auf Freigabe</h2>
    <div id="pending-container">
      {pending_cards}
    </div>
  </div>

  {"<div class='section'><h2>⚠️ Gefahrgut — manuelle Prüfung</h2>" + flagged_cards + "</div>" if flagged_cards else ""}

  <div class="toast" id="toast"></div>
  <div class="progress-bar"><div class="progress-fill" id="progress" style="width:100%"></div></div>

  <script>
    let sentCount = 0;
    let refreshInterval;
    let countdownSeconds = 1800; // 30 Minuten

    function showToast(msg, ok=true) {{
      const t = document.getElementById("toast");
      t.textContent = msg;
      t.className = "toast " + (ok ? "toast-ok" : "toast-err");
      t.style.display = "block";
      setTimeout(() => t.style.display = "none", 4000);
    }}

    async function approveOrder(orderId, btn) {{
      btn.disabled = true;
      btn.textContent = "⏳ Sende...";
      try {{
        const r = await fetch("/api/approve/" + orderId, {{method: "POST"}});
        const data = await r.json();
        if (data.ok) {{
          showToast("✅ Bestellung " + orderId + " → Mail an BAB gesendet");
          sentCount++;
          document.getElementById("sent-count").textContent = sentCount;
          btn.closest(".card").style.opacity = "0.3";
          setTimeout(() => btn.closest(".card").remove(), 1000);
        }} else {{
          showToast("❌ Fehler: " + data.error, false);
          btn.disabled = false;
          btn.textContent = "✅ Freigeben → BAB";
        }}
      }} catch(e) {{
        showToast("❌ Verbindungsfehler", false);
        btn.disabled = false;
        btn.textContent = "✅ Freigeben → BAB";
      }}
    }}

    async function rejectOrder(orderId, btn) {{
      if (!confirm("Bestellung " + orderId + " ablehnen? Keine Mail wird gesendet.")) return;
      btn.disabled = true;
      try {{
        const r = await fetch("/api/reject/" + orderId, {{method: "POST"}});
        const data = await r.json();
        if (data.ok) {{
          showToast("🗑 Bestellung " + orderId + " abgelehnt");
          btn.closest(".card").style.opacity = "0.3";
          setTimeout(() => btn.closest(".card").remove(), 1000);
        }}
      }} catch(e) {{
        showToast("❌ Verbindungsfehler", false);
        btn.disabled = false;
      }}
    }}

    function refreshNow() {{
      countdownSeconds = 1800;
      window.location.reload();
    }}

    // Countdown
    function updateCountdown() {{
      countdownSeconds--;
      if (countdownSeconds <= 0) {{ window.location.reload(); return; }}
      const m = Math.floor(countdownSeconds / 60).toString().padStart(2,"0");
      const s = (countdownSeconds % 60).toString().padStart(2,"0");
      document.getElementById("countdown").textContent = "Nächste Aktualisierung: " + m + ":" + s;
      const pct = (countdownSeconds / 1800 * 100).toFixed(1);
      document.getElementById("progress").style.width = pct + "%";
    }}

    setInterval(updateCountdown, 1000);
  </script>
</body>
</html>'''


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Request Handler
# ─────────────────────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(f"HTTP {args[0]} {args[1]}")

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            html = render_dashboard().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        parts  = parsed.path.strip("/").split("/")

        if len(parts) == 3 and parts[0] == "api" and parts[1] in ("approve", "reject"):
            action   = parts[1]
            order_id = parts[2]
            pending  = load_json(PENDING_FILE)

            if order_id not in pending:
                self.send_json({"ok": False, "error": "Order nicht in pending_orders.json"}, 404)
                return

            order_data  = pending[order_id]
            config_path = order_data.get("shop_config", "config_shop2.yaml")

            if action == "approve":
                ok = build_and_send_email(order_data)
                if ok:
                    del pending[order_id]
                    save_json(PENDING_FILE, pending)
                    mark_processed(order_id, config_path)
                    log.info(f"✅ Freigegeben & gesendet: {order_id}")
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": False, "error": "E-Mail konnte nicht gesendet werden"}, 500)

            elif action == "reject":
                del pending[order_id]
                save_json(PENDING_FILE, pending)
                mark_processed(order_id, config_path)
                log.info(f"🗑 Abgelehnt: {order_id}")
                self.send_json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()


# ─────────────────────────────────────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = 8765
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    log.info(f"📦 Dashboard startet auf http://localhost:{port}")
    log.info(f"Arbeitsverzeichnis: {script_dir}")
    webbrowser.open(f"http://localhost:{port}")
    server = HTTPServer(("localhost", port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Dashboard beendet.")
