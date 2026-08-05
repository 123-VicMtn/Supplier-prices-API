"""Orchestration d'un passage de collecte.

    python3 -m pricewatch.run --config suppliers.yaml
    python3 -m pricewatch.run --config suppliers.yaml --supplier sonepar --limit 20 --dry-run

Le garde-fou important est le controle de sante en fin de passage: un site qui
change de structure ne renvoie pas d'erreur, il renvoie zero prix. Sans ce
controle, on desactive tout le catalogue en silence et on ne s'en apercoit que
la semaine suivante.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

from . import adapters, db
from .fetch import Fetcher

log = logging.getLogger("pricewatch")

# En dessous de ce ratio d'articles par rapport au dernier passage reussi,
# on considere l'adaptateur casse plutot que le catalogue reduit.
HEALTH_RATIO = 0.5


def run_supplier(conn, cfg: dict, *, limit: int | None = None, dry_run: bool = False,
                 no_cache: bool = False) -> dict:
    key = cfg["key"]
    supplier_id = db.upsert_supplier(conn, key, cfg.get("name", key), cfg["base_url"])
    run_id = db.start_run(conn, supplier_id)
    t0 = time.monotonic()

    fetcher = Fetcher(
        conn, key,
        delay=cfg.get("delay", 1.5),
        session_dir=cfg.get("session_dir", "var/sessions"),
        headers=cfg.get("headers"),
        use_cache=not no_cache,
    )
    seen_ids: set[int] = set()
    changed = 0

    try:
        if cfg.get("auth") and not fetcher.login(cfg["auth"]):
            raise RuntimeError("authentification refusee")

        adapter = adapters.build(cfg, fetcher, conn)
        url_index = db.urls_by_product(conn, supplier_id)
        adapter.known_urls = set(url_index)

        for n, item in enumerate(adapter.iter_products(), 1):
            if limit and n > limit:
                log.info("[%s] limite --limit atteinte", key)
                break

            # Fiche inchangee (304): pas de prix a enregistrer, mais l'article
            # est confirme au catalogue.
            if item.get("unchanged"):
                pid = url_index.get(item["url"])
                if pid:
                    seen_ids.add(pid)
                    if not dry_run:
                        db.touch_product(conn, pid)
                continue

            if dry_run:
                print(f"  {item['sku']:<24} {item['price']:>10.2f} {item.get('currency') or '':<4} "
                      f"{(item.get('name') or '')[:60]}")
                seen_ids.add(n)
                continue

            pid = db.upsert_product(
                conn, supplier_id, str(item["sku"]),
                name=item.get("name"), url=item.get("url"),
                ean=item.get("ean"), category=item.get("category"),
                currency=item.get("currency"),
            )
            seen_ids.add(pid)
            if db.record_price(conn, pid, item["price"]):
                changed += 1
            if n % 200 == 0:
                conn.commit()
                log.info("[%s] %d articles...", key, n)

        seen = len(seen_ids)
        previous = db.previous_run_count(conn, supplier_id)
        suspect = seen == 0 or (previous and seen < previous * HEALTH_RATIO)

        if dry_run:
            conn.rollback()
            log.info("[%s] dry-run: %d articles, RIEN ECRIT EN BASE "
                     "(relancez sans --dry-run pour enregistrer)", key, seen)
            # Statut explicite: un dry-run affiche "ok" dans l'historique laissait
            # croire a une collecte reussie alors que rien n'avait ete enregistre.
            db.finish_run(conn, run_id, "dry-run", seen, 0)
            return {"key": key, "seen": seen, "changed": 0, "status": "dry-run"}

        if suspect:
            # On garde les prix collectes, mais on ne desactive rien: le
            # catalogue precedent reste la verite tant qu'un humain n'a pas vu.
            msg = f"{seen} articles contre {previous} au dernier passage"
            log.error("[%s] PASSAGE SUSPECT: %s -- adaptateur probablement casse", key, msg)
            db.finish_run(conn, run_id, "suspect", seen, changed, msg)
        else:
            gone = db.deactivate_unseen(conn, supplier_id, seen_ids)
            db.finish_run(conn, run_id, "ok", seen, changed)
            log.info("[%s] OK: %d articles, %d prix modifies, %d disparus, %.0fs",
                     key, seen, changed, gone, time.monotonic() - t0)
        conn.commit()
        return {"key": key, "seen": seen, "changed": changed,
                "status": "suspect" if suspect else "ok"}

    except KeyboardInterrupt:
        # KeyboardInterrupt derive de BaseException: sans ce cas, un Ctrl-C
        # laissait la ligne de passage bloquee en "running" pour toujours.
        conn.commit()
        db.finish_run(conn, run_id, "interrompu", len(seen_ids), changed, "Ctrl-C")
        log.warning("[%s] interrompu apres %d articles", key, len(seen_ids))
        raise
    except Exception as exc:
        conn.commit()  # on conserve ce qui a ete collecte avant l'incident
        db.finish_run(conn, run_id, "failed", len(seen_ids), changed, str(exc))
        log.exception("[%s] ECHEC: %s", key, exc)
        return {"key": key, "seen": len(seen_ids), "changed": changed, "status": "failed"}
    finally:
        fetcher.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Collecte des prix fournisseurs")
    ap.add_argument("--config", default="suppliers.yaml")
    ap.add_argument("--db", default=None, help="chemin SQLite (defaut: celui du YAML)")
    ap.add_argument("--supplier", action="append", help="ne traiter que ce(s) fournisseur(s)")
    ap.add_argument("--limit", type=int, help="s'arreter apres N articles (mise au point)")
    ap.add_argument("--dry-run", action="store_true", help="afficher sans rien ecrire")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignorer ETag/Last-Modified et tout relire (passage complet)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="detail de nos modules (pas des couches HTTP)")
    ap.add_argument("--debug-http", action="store_true",
                    help="tres bavard: trames httpx/httpcore/h2, pour un diagnostic reseau")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # -v ne concerne QUE nos propres modules. Mettre la racine en DEBUG fait
    # remonter les trames HTTP/2 de httpcore/hpack/h2: des milliers de lignes
    # illisibles ou l'information utile se noie.
    logging.getLogger("pricewatch").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    for noisy in ("httpx", "httpcore", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if args.debug_http else logging.WARNING)

    cfg = yaml.safe_load(Path(args.config).read_text())
    conn = db.open_db(args.db or cfg.get("database", "var/prices.db"))

    suppliers = cfg["suppliers"]
    if args.supplier:
        suppliers = [s for s in suppliers if s["key"] in args.supplier]
        if not suppliers:
            sys.exit(f"aucun fournisseur nomme {args.supplier}")

    results = []
    for supplier in suppliers:
        if supplier.get("enabled") is False:
            continue
        merged = {**cfg.get("defaults", {}), **supplier}
        results.append(run_supplier(conn, merged, limit=args.limit,
                                    dry_run=args.dry_run, no_cache=args.no_cache))

    print("\n--- resume ---")
    for r in results:
        print(f"  {r['status']:<8} {r['key']:<20} {r['seen']:>6} articles, "
              f"{r['changed']:>5} prix modifies")

    # Code de sortie non nul -> systemd le signale, et OnFailure peut alerter.
    return 1 if any(r["status"] in ("failed", "suspect") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
