"""API JSON + UI web minimaliste.

Point d'architecture: l'UI n'a AUCUN acces privilegie a la base, elle consomme
exactement les memes endpoints /api/v1/* que consommera Reonic en V2. Ce qui
marche dans le navigateur marchera dans l'integration, sans code en double.

    uvicorn pricewatch.api:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

DB_PATH = os.environ.get("PRICEWATCH_DB", "var/prices.db")

app = FastAPI(title="Pricewatch", version="1.0")


def q(sql: str, params=()) -> list[dict]:
    """Connexion en lecture seule: l'UI ne peut structurellement rien casser."""
    path = Path(DB_PATH).resolve()
    if not path.exists():
        # Cas courant au premier demarrage: l'API tourne avant toute collecte.
        # Un message clair vaut mieux qu'une OperationalError sur 40 lignes.
        raise HTTPException(503, f"base absente ({path}). Lancez une premiere "
                                 f"collecte: python -m pricewatch.run --config suppliers.yaml")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


CURRENT = """
SELECT p.id, p.sku, p.name, p.url, p.ean, p.currency, p.active, p.last_seen,
       s.key AS supplier, s.name AS supplier_name,
       (SELECT price FROM price WHERE product_id = p.id ORDER BY observed_at DESC LIMIT 1) AS price,
       (SELECT observed_at FROM price WHERE product_id = p.id ORDER BY observed_at DESC LIMIT 1) AS priced_at
FROM product p JOIN supplier s ON s.id = p.supplier_id
"""


@app.get("/api/v1/suppliers")
def suppliers():
    return q("""
        SELECT s.key, s.name, s.base_url,
               COUNT(p.id) FILTER (WHERE p.active = 1) AS articles,
               (SELECT status FROM run WHERE supplier_id = s.id
                ORDER BY started_at DESC LIMIT 1) AS last_status,
               (SELECT started_at FROM run WHERE supplier_id = s.id
                ORDER BY started_at DESC LIMIT 1) AS last_run
        FROM supplier s LEFT JOIN product p ON p.supplier_id = s.id
        GROUP BY s.id ORDER BY s.name
    """)


@app.get("/api/v1/products")
def products(
    q_: str | None = Query(None, alias="q", description="recherche sur reference, nom ou EAN"),
    supplier: str | None = None,
    active: bool = True,
    limit: int = Query(100, le=1000),
    offset: int = 0,
):
    where, params = [], []
    if active:
        where.append("p.active = 1")
    if supplier:
        where.append("s.key = ?")
        params.append(supplier)
    if q_:
        where.append("(p.sku LIKE ? OR p.name LIKE ? OR p.ean LIKE ?)")
        params += [f"%{q_}%"] * 3
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = q(f"SELECT COUNT(*) AS n FROM product p JOIN supplier s ON s.id = p.supplier_id{clause}",
              params)[0]["n"]
    rows = q(f"{CURRENT}{clause} ORDER BY p.name LIMIT ? OFFSET ?", (*params, limit, offset))
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


@app.get("/api/v1/products/{product_id}")
def product(product_id: int):
    rows = q(f"{CURRENT} WHERE p.id = ?", (product_id,))
    if not rows:
        raise HTTPException(404, "article inconnu")
    item = rows[0]
    item["history"] = q(
        "SELECT price, observed_at FROM price WHERE product_id = ? ORDER BY observed_at",
        (product_id,),
    )
    return item


@app.get("/api/v1/changes")
def changes(days: int = 30, min_pct: float = 0.0, limit: int = Query(200, le=2000)):
    """Variations recentes: les deux derniers prix connus de chaque article."""
    rows = q("""
        WITH ranked AS (
            SELECT product_id, price, observed_at,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY observed_at DESC) AS rn
            FROM price
        )
        SELECT p.id, p.sku, p.name, p.currency, s.key AS supplier,
               cur.price AS price, prev.price AS previous, cur.observed_at AS changed_at
        FROM ranked cur
        JOIN ranked prev ON prev.product_id = cur.product_id AND prev.rn = 2
        JOIN product p ON p.id = cur.product_id
        JOIN supplier s ON s.id = p.supplier_id
        WHERE cur.rn = 1
          AND cur.observed_at >= datetime('now', ?)
        ORDER BY cur.observed_at DESC LIMIT ?
    """, (f"-{int(days)} days", limit))
    for r in rows:
        r["pct"] = round((r["price"] - r["previous"]) / r["previous"] * 100, 2) if r["previous"] else None
    return [r for r in rows if r["pct"] is not None and abs(r["pct"]) >= min_pct]


@app.get("/api/v1/runs")
def runs(limit: int = 50):
    return q("""
        SELECT r.*, s.key AS supplier FROM run r JOIN supplier s ON s.id = r.supplier_id
        ORDER BY r.started_at DESC LIMIT ?
    """, (limit,))


@app.get("/api/v1/export.csv")
def export_csv(supplier: str | None = None):
    clause, params = ("", ())
    if supplier:
        clause, params = " WHERE s.key = ? AND p.active = 1", (supplier,)
    rows = q(f"{CURRENT}{clause or ' WHERE p.active = 1'} ORDER BY s.key, p.sku", params)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["supplier", "sku", "ean", "name", "price",
                                        "currency", "priced_at", "url"],
                       extrasaction="ignore", delimiter=";")
    w.writeheader()
    w.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prix.csv"'},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """
<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prix fournisseurs</title>
<style>
 :root { --bg:#fff; --fg:#1a1a1a; --mut:#6b7280; --line:#e5e7eb; --up:#b91c1c; --down:#15803d; }
 @media (prefers-color-scheme: dark) {
   :root { --bg:#151719; --fg:#e8e8e8; --mut:#9ca3af; --line:#2c2f33; --up:#f87171; --down:#4ade80; }
 }
 * { box-sizing: border-box; }
 body { margin:0; padding:1.5rem; background:var(--bg); color:var(--fg);
        font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; }
 h1 { font-size:1.15rem; margin:0 0 1rem; font-weight:600; }
 .bar { display:flex; gap:.5rem; flex-wrap:wrap; margin-bottom:1rem; }
 input,select,button { padding:.5rem .7rem; border:1px solid var(--line); border-radius:6px;
        background:var(--bg); color:var(--fg); font:inherit; }
 input[type=search] { flex:1; min-width:200px; }
 button { cursor:pointer; }
 table { width:100%; border-collapse:collapse; font-size:.9rem; }
 th,td { text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line); }
 th { color:var(--mut); font-weight:500; font-size:.8rem; text-transform:uppercase;
      letter-spacing:.03em; position:sticky; top:0; background:var(--bg); }
 .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
 tr:hover { background:color-mix(in srgb, var(--fg) 4%, transparent); cursor:pointer; }
 .up { color:var(--up); } .down { color:var(--down); }
 .meta { color:var(--mut); font-size:.85rem; margin-top:.75rem; }
 .tabs { display:flex; gap:1rem; border-bottom:1px solid var(--line); margin-bottom:1rem; }
 .tabs a { padding:.4rem 0; color:var(--mut); text-decoration:none; border-bottom:2px solid transparent; }
 .tabs a.on { color:var(--fg); border-color:var(--fg); }
 dialog { border:1px solid var(--line); border-radius:10px; background:var(--bg); color:var(--fg);
          max-width:min(560px,92vw); padding:1.25rem; }
 dialog::backdrop { background:#0008; }
</style></head><body>
<h1>Prix fournisseurs</h1>
<div class="tabs">
  <a href="#" data-tab="products" class="on">Articles</a>
  <a href="#" data-tab="changes">Variations</a>
  <a href="#" data-tab="runs">Collectes</a>
</div>
<div class="bar" id="filters">
  <input type="search" id="q" placeholder="Reference, designation ou EAN...">
  <select id="supplier"><option value="">Tous les fournisseurs</option></select>
  <button id="csv">Export CSV</button>
</div>
<table><thead id="head"></thead><tbody id="rows"></tbody></table>
<div class="meta" id="meta"></div>
<dialog id="detail"></dialog>
<script>
const $ = s => document.querySelector(s);
let tab = 'products';

const fmt = (v, c) => v == null ? '—' : v.toFixed(2) + ' ' + (c || '');
const pct = v => `<span class="${v > 0 ? 'up' : 'down'}">${v > 0 ? '+' : ''}${v}%</span>`;
const esc = s => (s ?? '').toString().replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));

async function load() {
  const params = new URLSearchParams();
  if (tab === 'products') {
    if ($('#q').value) params.set('q', $('#q').value);
    if ($('#supplier').value) params.set('supplier', $('#supplier').value);
    params.set('limit', 200);
  }
  const path = { products:'products', changes:'changes?days=90', runs:'runs' }[tab];
  const data = await (await fetch('/api/v1/' + path + (path.includes('?') ? '&' : '?') + params)).json();
  const items = data.items || data;

  const cols = {
    products: ['Fournisseur','Reference','Designation','Prix'],
    changes:  ['Fournisseur','Reference','Designation','Avant','Apres','Variation'],
    runs:     ['Fournisseur','Debut','Statut','Articles','Prix modifies'],
  }[tab];
  $('#head').innerHTML = '<tr>' + cols.map((c, i) =>
    `<th${i >= 3 ? ' class="num"' : ''}>${c}</th>`).join('') + '</tr>';

  $('#rows').innerHTML = items.map(r => {
    if (tab === 'products') return `<tr data-id="${r.id}"><td>${esc(r.supplier)}</td>
      <td>${esc(r.sku)}</td><td>${esc(r.name)}</td>
      <td class="num">${fmt(r.price, r.currency)}</td></tr>`;
    if (tab === 'changes') return `<tr data-id="${r.id}"><td>${esc(r.supplier)}</td>
      <td>${esc(r.sku)}</td><td>${esc(r.name)}</td>
      <td class="num">${fmt(r.previous, r.currency)}</td>
      <td class="num">${fmt(r.price, r.currency)}</td>
      <td class="num">${pct(r.pct)}</td></tr>`;
    return `<tr><td>${esc(r.supplier)}</td><td>${esc(r.started_at)}</td>
      <td>${esc(r.status)}${r.error ? ' — ' + esc(r.error) : ''}</td>
      <td class="num">${r.products_seen}</td><td class="num">${r.prices_changed}</td></tr>`;
  }).join('') || '<tr><td colspan="6">Aucun resultat.</td></tr>';

  $('#meta').textContent = data.total != null
    ? `${items.length} affiches sur ${data.total}` : `${items.length} lignes`;
  $('#filters').style.display = tab === 'products' ? 'flex' : 'none';
}

document.querySelectorAll('.tabs a').forEach(a => a.onclick = e => {
  e.preventDefault();
  document.querySelectorAll('.tabs a').forEach(x => x.classList.remove('on'));
  a.classList.add('on'); tab = a.dataset.tab; load();
});

$('#rows').onclick = async e => {
  const id = e.target.closest('tr')?.dataset.id;
  if (!id) return;
  const p = await (await fetch('/api/v1/products/' + id)).json();
  const h = p.history.map(x => `<tr><td>${x.observed_at.slice(0,10)}</td>
      <td class="num">${fmt(x.price, p.currency)}</td></tr>`).reverse().join('');
  $('#detail').innerHTML = `<h3>${esc(p.name || p.sku)}</h3>
    <p class="meta">${esc(p.supplier_name)} — ref. ${esc(p.sku)}${p.ean ? ' — EAN ' + esc(p.ean) : ''}
    ${p.url ? ` — <a href="${esc(p.url)}" target="_blank" rel="noopener">fiche</a>` : ''}</p>
    <table><thead><tr><th>Date</th><th class="num">Prix</th></tr></thead><tbody>${h}</tbody></table>
    <p><button autofocus onclick="document.getElementById('detail').close()">Fermer</button></p>`;
  $('#detail').showModal();
};

let t; $('#q').oninput = () => { clearTimeout(t); t = setTimeout(load, 250); };
$('#supplier').onchange = load;
$('#csv').onclick = () => location = '/api/v1/export.csv'
  + ($('#supplier').value ? '?supplier=' + $('#supplier').value : '');

fetch('/api/v1/suppliers').then(r => r.json()).then(s =>
  $('#supplier').innerHTML += s.map(x =>
    `<option value="${esc(x.key)}">${esc(x.name)} (${x.articles})</option>`).join(''));
load();
</script></body></html>
"""
