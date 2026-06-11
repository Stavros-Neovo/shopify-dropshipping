"""
review_images.py — Visuelles Bild-Review Tool
===============================================
Generiert image_review.html aus enrichment_index.csv.
Öffne die HTML-Datei im Browser, klicke falsche Bilder an,
exportiere wrong_images.json, dann fix_wrong_images.py ausführen.

Aufruf:
  python review_images.py
  python review_images.py --only-sourced ddg,amazon   # nur bestimmte Quellen
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ENRICHMENT_FILE = "enrichment_index.csv"
OUTPUT_FILE     = "image_review.html"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-sourced", default="",
                        help="Komma-getrennte Quellen filtern (z.B. ddg,amazon)")
    args = parser.parse_args()

    filter_sources = [s.strip().lower() for s in args.only_sourced.split(",") if s.strip()]

    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    # Nur Produkte mit Bild
    products = []
    for row in rows:
        img = (row.get("image_main") or "").strip()
        if not img:
            continue
        source = (row.get("image_source") or row.get("source") or "").strip().lower()
        if filter_sources and not any(s in source for s in filter_sources):
            continue
        products.append({
            "sku":    (row.get("sku") or row.get("ItemNo") or "").strip(),
            "ean":    (row.get("ean") or "").strip(),
            "title":  (row.get("title_seo") or row.get("title") or row.get("Description") or "").strip(),
            "image":  img,
            "source": source or "unbekannt",
        })

    print(f"{len(products)} Produkte mit Bild → {OUTPUT_FILE}")

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bild Review — {len(products)} Produkte</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, sans-serif; background: #0f0f13; color: #e0e0e0; }}

  .header {{
    position: sticky; top: 0; z-index: 100;
    background: #1a1a2e; border-bottom: 1px solid #333;
    padding: 12px 20px; display: flex; align-items: center; gap: 16px;
    flex-wrap: wrap;
  }}
  .header h1 {{ font-size: 16px; font-weight: 600; color: #fff; }}
  .counter {{
    background: #e74c3c; color: #fff; padding: 4px 12px;
    border-radius: 20px; font-size: 14px; font-weight: 600;
    min-width: 80px; text-align: center;
  }}
  .counter.zero {{ background: #27ae60; }}
  .btn {{
    padding: 8px 18px; border-radius: 8px; border: none;
    cursor: pointer; font-size: 14px; font-weight: 600;
  }}
  .btn-export {{ background: #e74c3c; color: #fff; }}
  .btn-export:hover {{ background: #c0392b; }}
  .btn-export:disabled {{ background: #555; cursor: not-allowed; }}
  .btn-clear {{ background: #333; color: #aaa; }}
  .btn-clear:hover {{ background: #444; }}
  .search {{
    padding: 7px 14px; border-radius: 8px; border: 1px solid #444;
    background: #252535; color: #e0e0e0; font-size: 14px; width: 220px;
  }}
  .stats {{ color: #888; font-size: 13px; margin-left: auto; }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px; padding: 16px;
  }}

  .card {{
    background: #1e1e2e; border-radius: 10px; overflow: hidden;
    border: 2px solid transparent; cursor: pointer;
    transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
    position: relative;
  }}
  .card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 20px rgba(0,0,0,0.4); }}
  .card.wrong {{ border-color: #e74c3c; background: #2a1a1e; }}
  .card.wrong .img-wrap {{ opacity: 0.6; }}

  .wrong-badge {{
    display: none; position: absolute; top: 8px; right: 8px;
    background: #e74c3c; color: #fff; border-radius: 50%;
    width: 28px; height: 28px; font-size: 16px;
    align-items: center; justify-content: center; font-weight: bold;
  }}
  .card.wrong .wrong-badge {{ display: flex; }}

  .img-wrap {{
    width: 100%; aspect-ratio: 1;
    background: #252535; overflow: hidden;
    display: flex; align-items: center; justify-content: center;
  }}
  .img-wrap img {{
    width: 100%; height: 100%; object-fit: contain;
    transition: opacity 0.2s;
  }}
  .img-wrap img.error {{ opacity: 0.2; }}

  .info {{
    padding: 8px 10px;
  }}
  .info .title {{
    font-size: 11px; color: #ccc; line-height: 1.3;
    display: -webkit-box; -webkit-line-clamp: 2;
    -webkit-box-orient: vertical; overflow: hidden;
    margin-bottom: 4px;
  }}
  .info .meta {{
    font-size: 10px; color: #666;
    display: flex; justify-content: space-between;
  }}
  .source-badge {{
    font-size: 9px; padding: 1px 5px; border-radius: 4px;
    background: #2a3a2a; color: #5a9a5a;
  }}
  .source-badge.ddg {{ background: #3a2a2a; color: #9a5a5a; }}
  .source-badge.amazon {{ background: #3a3020; color: #9a8a40; }}
  .source-badge.ipcstore {{ background: #3a2a3a; color: #9a5a9a; }}

  .ean-link {{
    display: block; margin-top: 5px;
    font-size: 10px; color: #5a9adf; text-decoration: none;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .ean-link:hover {{ color: #7ab8ff; text-decoration: underline; }}

  .no-results {{ text-align: center; color: #555; padding: 60px; font-size: 18px; }}

  .export-info {{
    display: none; position: fixed; bottom: 20px; right: 20px;
    background: #27ae60; color: #fff; padding: 12px 20px;
    border-radius: 10px; font-size: 14px; font-weight: 600;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    animation: fadeIn 0.3s ease;
  }}
  @keyframes fadeIn {{ from {{ opacity:0; transform: translateY(10px); }} to {{ opacity:1; transform: translateY(0); }} }}
</style>
</head>
<body>

<div class="header">
  <h1>🖼️ Bild Review</h1>
  <div class="counter zero" id="counter">0 markiert</div>
  <button class="btn btn-export" id="btnExport" onclick="exportWrong()" disabled>
    ❌ Falsche exportieren
  </button>
  <button class="btn btn-clear" onclick="clearAll()">Alles zurücksetzen</button>
  <input class="search" type="text" id="search" placeholder="SKU oder Titel suchen…" oninput="filterCards()">
  <span class="stats" id="stats">{len(products)} Produkte</span>
</div>

<div class="grid" id="grid"></div>
<div class="no-results" id="noResults" style="display:none">Keine Treffer</div>
<div class="export-info" id="exportInfo">✓ wrong_images.json gespeichert!</div>

<script>
const PRODUCTS = {json.dumps(products, ensure_ascii=False)};
const wrong = new Set();

function renderGrid(items) {{
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  items.forEach((p, idx) => {{
    const uid = p.sku || p.ean || ('idx_' + idx);  // eindeutige ID
    const isWrong = wrong.has(uid);
    const srcClass = p.source.includes('ddg') ? 'ddg'
                   : p.source.includes('amazon') ? 'amazon'
                   : p.source.includes('ipcstore') ? 'ipcstore' : '';
    const div = document.createElement('div');
    div.className = 'card' + (isWrong ? ' wrong' : '');
    div.dataset.uid = uid;
    div.onclick = () => toggleWrong(uid, div);
    div.innerHTML = `
      <div class="wrong-badge">✕</div>
      <div class="img-wrap">
        <img src="${{p.image}}" alt="${{p.title}}"
             onerror="this.classList.add('error'); this.src='data:image/svg+xml,<svg xmlns=\\'http://www.w3.org/2000/svg\\' viewBox=\\'0 0 100 100\\'><text y=\\'.9em\\'font-size=\\'80\\'> 🚫</text></svg>'"
             loading="lazy">
      </div>
      <div class="info">
        <div class="title">${{p.title || p.sku}}</div>
        <div class="meta">
          <span>${{p.sku}}</span>
          <span class="source-badge ${{srcClass}}">${{p.source || '?'}}</span>
        </div>
        <a class="ean-link" href="https://www.google.com/search?tbm=isch&q=${{encodeURIComponent(p.ean || p.sku)}}" target="_blank" onclick="event.stopPropagation()">🔍 EAN: ${{p.ean || '—'}}</a>
      </div>`;
    grid.appendChild(div);
  }});
  document.getElementById('noResults').style.display = items.length ? 'none' : 'block';
}}

function toggleWrong(uid, el) {{
  if (wrong.has(uid)) {{ wrong.delete(uid); el.classList.remove('wrong'); }}
  else {{ wrong.add(uid); el.classList.add('wrong'); }}
  updateCounter();
}}

function updateCounter() {{
  const n = wrong.size;
  const el = document.getElementById('counter');
  el.textContent = n + ' markiert';
  el.className = 'counter' + (n === 0 ? ' zero' : '');
  document.getElementById('btnExport').disabled = n === 0;
}}

function clearAll() {{
  wrong.clear();
  document.querySelectorAll('.card.wrong').forEach(c => c.classList.remove('wrong'));
  updateCounter();
}}

function filterCards() {{
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = q ? PRODUCTS.filter(p =>
    p.sku.toLowerCase().includes(q) || p.title.toLowerCase().includes(q)
  ) : PRODUCTS;
  renderGrid(filtered);
  document.getElementById('stats').textContent = filtered.length + ' Produkte';
}}

function exportWrong() {{
  const data = Array.from(wrong).map(uid => {{
    const p = PRODUCTS.find(x => (x.sku || x.ean) === uid || ('idx_' + PRODUCTS.indexOf(x)) === uid);
    return {{ sku: p?.sku || uid, ean: p?.ean || '', title: p?.title || '', image: p?.image || '' }};
  }});

  const blob = new Blob([JSON.stringify(data, null, 2)], {{type: 'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'wrong_images.json';
  a.click();

  const info = document.getElementById('exportInfo');
  info.style.display = 'block';
  setTimeout(() => info.style.display = 'none', 3000);
}}

// Init
renderGrid(PRODUCTS);
</script>
</body>
</html>"""

    Path(OUTPUT_FILE).write_text(html, encoding="utf-8")
    print(f"✓ {OUTPUT_FILE} erstellt — im Browser öffnen")
    print(f"  Falsche Bilder anklicken → 'Falsche exportieren' → wrong_images.json")
    print(f"  Dann: python fix_wrong_images.py")


if __name__ == "__main__":
    main()
