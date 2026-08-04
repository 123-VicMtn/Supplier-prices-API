"""Persistance SQLite.

Deux partis pris qui comptent sur un Pi:

- WAL: l'UI web peut lire pendant qu'un scraping ecrit, sans se bloquer.
- Historique delta: on n'insere une ligne de prix que lorsque le prix CHANGE.
  Sur quelques milliers d'articles releves chaque nuit, ca fait la difference
  entre une base de quelques Mo et une base d'un Go au bout d'un an.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supplier (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,      -- identifiant court, ex: "sonepar"
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product (
    id          INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    sku         TEXT NOT NULL,             -- reference fournisseur
    name        TEXT,
    url         TEXT,
    ean         TEXT,
    currency    TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE (supplier_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_product_sku  ON product(sku);
CREATE INDEX IF NOT EXISTS idx_product_ean  ON product(ean);
CREATE INDEX IF NOT EXISTS idx_product_name ON product(name);

-- Une ligne uniquement quand le prix change. Le prix courant = MAX(observed_at).
CREATE TABLE IF NOT EXISTS price (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES product(id),
    price       REAL NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_product ON price(product_id, observed_at DESC);

-- ETag / Last-Modified: permet de renvoyer If-None-Match et de se prendre un
-- 304 (quelques centaines d'octets) au lieu de re-telecharger et re-parser.
CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id            INTEGER PRIMARY KEY,
    supplier_id   INTEGER NOT NULL REFERENCES supplier(id),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,           -- running | ok | failed | suspect
    products_seen INTEGER DEFAULT 0,
    prices_changed INTEGER DEFAULT 0,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_supplier ON run(supplier_id, started_at DESC);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --- fournisseurs & produits -------------------------------------------------

def upsert_supplier(conn, key: str, name: str, base_url: str) -> int:
    conn.execute(
        "INSERT INTO supplier (key, name, base_url) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET name = excluded.name, base_url = excluded.base_url",
        (key, name, base_url),
    )
    return conn.execute("SELECT id FROM supplier WHERE key = ?", (key,)).fetchone()["id"]


def upsert_product(conn, supplier_id: int, sku: str, **fields) -> int:
    ts = now()
    row = conn.execute(
        "SELECT id FROM product WHERE supplier_id = ? AND sku = ?", (supplier_id, sku)
    ).fetchone()
    if row:
        sets = ", ".join(f"{k} = ?" for k in fields if fields[k] is not None)
        values = [v for v in fields.values() if v is not None]
        conn.execute(
            f"UPDATE product SET {sets + ', ' if sets else ''}last_seen = ?, active = 1 WHERE id = ?",
            (*values, ts, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO product (supplier_id, sku, name, url, ean, currency, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (supplier_id, sku, fields.get("name"), fields.get("url"), fields.get("ean"),
         fields.get("currency"), ts, ts),
    )
    return cur.lastrowid


def urls_by_product(conn, supplier_id: int) -> dict[str, int]:
    """Index url -> product_id, pour rattacher une reponse 304 a son article."""
    rows = conn.execute(
        "SELECT id, url FROM product WHERE supplier_id = ? AND url IS NOT NULL",
        (supplier_id,),
    ).fetchall()
    return {r["url"]: r["id"] for r in rows}


def touch_product(conn, product_id: int) -> None:
    """Article confirme present sans nouveau prix (fiche inchangee, 304)."""
    conn.execute("UPDATE product SET last_seen = ?, active = 1 WHERE id = ?", (now(), product_id))


def record_price(conn, product_id: int, price: float) -> bool:
    """Insere seulement si le prix differe du dernier connu. True si changement."""
    last = conn.execute(
        "SELECT price FROM price WHERE product_id = ? ORDER BY observed_at DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    if last and abs(last["price"] - price) < 0.0001:
        return False
    conn.execute(
        "INSERT INTO price (product_id, price, observed_at) VALUES (?, ?, ?)",
        (product_id, price, now()),
    )
    return True


def deactivate_unseen(conn, supplier_id: int, seen_ids: set[int]) -> int:
    """Marque inactifs les produits absents du passage. Ne supprime jamais:
    un article disparu peut revenir, et son historique reste consultable."""
    rows = conn.execute(
        "SELECT id FROM product WHERE supplier_id = ? AND active = 1", (supplier_id,)
    ).fetchall()
    stale = [r["id"] for r in rows if r["id"] not in seen_ids]
    conn.executemany("UPDATE product SET active = 0 WHERE id = ?", [(i,) for i in stale])
    return len(stale)


# --- cache HTTP --------------------------------------------------------------

def cache_get(conn, url: str) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)).fetchone()
    return (row["etag"], row["last_modified"]) if row else (None, None)


def cache_put(conn, url: str, etag: str | None, last_modified: str | None) -> None:
    if not etag and not last_modified:
        return
    conn.execute(
        "INSERT INTO http_cache (url, etag, last_modified, fetched_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET etag = excluded.etag, "
        "last_modified = excluded.last_modified, fetched_at = excluded.fetched_at",
        (url, etag, last_modified, now()),
    )


# --- passages ----------------------------------------------------------------

def start_run(conn, supplier_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO run (supplier_id, started_at, status) VALUES (?, ?, 'running')",
        (supplier_id, now()),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str, seen: int = 0, changed: int = 0, error: str = None):
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ?, products_seen = ?, "
        "prices_changed = ?, error = ? WHERE id = ?",
        (now(), status, seen, changed, error, run_id),
    )
    conn.commit()


def previous_run_count(conn, supplier_id: int) -> int | None:
    """Nombre d'articles du dernier passage reussi, pour detecter un adaptateur casse."""
    row = conn.execute(
        "SELECT products_seen FROM run WHERE supplier_id = ? AND status = 'ok' "
        "ORDER BY started_at DESC LIMIT 1",
        (supplier_id,),
    ).fetchone()
    return row["products_seen"] if row else None
