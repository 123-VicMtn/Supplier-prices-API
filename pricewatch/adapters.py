"""Adaptateurs: un par mode d'acces au catalogue, pas un par fournisseur.

Chaque adaptateur expose la meme methode `iter_products()` qui produit des dicts
{sku, name, url, ean, price, currency}. Ajouter un fournisseur = une entree YAML,
pas du code -- sauf cas vraiment exotique, ou l'on ecrit une sous-classe.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from . import extract

log = logging.getLogger(__name__)

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL | re.IGNORECASE)

REGISTRY: dict[str, type] = {}


class SessionLost(RuntimeError):
    """La session authentifiee a saute en cours de collecte.

    A traiter comme une panne, jamais comme une page sans prix: certaines
    boutiques B2B servent une valeur sentinelle au visiteur anonyme (Krannich
    renvoie 9999.00) au lieu de masquer le prix. Sans cette garde, on
    enregistrerait un catalogue entier de prix faux, avec le bon nombre
    d'articles -- donc sans que le controle de sante ne detecte quoi que ce soit.
    """


def register(name):
    def deco(cls):
        REGISTRY[name] = cls
        return cls
    return deco


class Adapter:
    def __init__(self, cfg: dict, fetcher, conn):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        self.fetcher = fetcher
        self.conn = conn
        # URLs deja en base. Rempli par run.py: on n'envoie une requete
        # conditionnelle que pour une fiche qu'on saura rattacher a un produit
        # si le serveur repond 304.
        self.known_urls: set[str] = set()

        # Texte qui doit figurer sur CHAQUE fiche tant que la session tient.
        # Par defaut le meme marqueur que l'authentification: si un texte prouve
        # qu'on est connecte, il doit valoir aussi sur les pages produit.
        # `session_marker: false` desactive la garde.
        marker = cfg.get("session_marker")
        if marker is None and cfg.get("auth"):
            marker = cfg["auth"].get("check")
        self.session_marker = marker or None

    def iter_products(self):
        raise NotImplementedError


# --- niveau 1: le catalogue est deja expose en JSON --------------------------

@register("shopify")
class ShopifyAdapter(Adapter):
    """/products.json pagine. Chaque variante est un article distinct."""

    def iter_products(self):
        page = 1
        while page <= self.cfg.get("max_pages", 200):
            data = self.fetcher.get_json(f"{self.base}/products.json?limit=250&page={page}")
            products = (data or {}).get("products") or []
            if not products:
                return
            for p in products:
                handle = p.get("handle", "")
                for v in p.get("variants") or []:
                    price = extract.parse_price(v.get("price"))
                    if price is None:
                        continue
                    sku = v.get("sku") or str(v.get("id"))
                    title = p.get("title", "")
                    if v.get("title") and v["title"] != "Default Title":
                        title = f"{title} - {v['title']}"
                    yield {
                        "sku": sku,
                        "name": title,
                        "url": f"{self.base}/products/{handle}",
                        "ean": v.get("barcode"),
                        "category": p.get("product_type") or None,
                        "price": price,
                        "currency": self.cfg.get("currency"),
                    }
            page += 1


@register("woocommerce")
class WooCommerceAdapter(Adapter):
    """Store API publique. Attention: les prix sont en unites mineures."""

    def iter_products(self):
        page = 1
        while page <= self.cfg.get("max_pages", 500):
            url = f"{self.base}/wp-json/wc/store/v1/products?per_page=100&page={page}"
            items = self.fetcher.get_json(url)
            if not isinstance(items, list) or not items:
                return
            for it in items:
                prices = it.get("prices") or {}
                minor = int(prices.get("currency_minor_unit", 2))
                raw = prices.get("price")
                if raw in (None, ""):
                    continue
                cats = [c.get("name") for c in (it.get("categories") or []) if c.get("name")]
                yield {
                    "sku": it.get("sku") or str(it.get("id")),
                    "name": it.get("name"),
                    "url": it.get("permalink"),
                    "ean": None,
                    "category": " > ".join(cats) or None,
                    "price": int(raw) / (10 ** minor),
                    "currency": prices.get("currency_code") or self.cfg.get("currency"),
                }
            page += 1


# --- niveau 2: sitemap pour la liste, HTML pour le prix ----------------------

@register("sitemap")
class SitemapAdapter(Adapter):
    """Le cas general: on decouvre les URLs via sitemap, on extrait via JSON-LD.

    Parallelise en quelques threads (I/O pur, empreinte memoire negligeable),
    tout en respectant l'intervalle minimum par hote via le RateLimiter partage.
    """

    def _sitemap_roots(self):
        if self.cfg.get("sitemaps"):
            return self.cfg["sitemaps"]
        r = self.fetcher.get(urljoin(self.base + "/", "robots.txt"), conditional=False)
        found = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text) if r.ok else []
        return found or [urljoin(self.base + "/", "sitemap.xml")]

    def _walk_sitemap(self, url, depth=0, seen=None):
        seen = seen if seen is not None else set()
        if url in seen or depth > 3:
            return
        seen.add(url)
        r = self.fetcher.get(url, conditional=False)
        if not r.ok:
            return
        locs = LOC_RE.findall(r.text)
        if "<sitemapindex" in r.text[:2000].lower():
            for sub in locs:
                if self.cfg.get("sitemap_filter") and not re.search(
                        self.cfg["sitemap_filter"], sub, re.I):
                    continue
                yield from self._walk_sitemap(sub, depth + 1, seen)
        else:
            pattern = self.cfg.get("product_url_pattern")
            for loc in locs:
                if pattern is None or re.search(pattern, loc, re.I):
                    yield loc

    def product_urls(self):
        seen = set()
        for root in self._sitemap_roots():
            for url in self._walk_sitemap(root):
                if url not in seen:
                    seen.add(url)
                    yield url

    def scrape_one(self, url):
        r = self.fetcher.get(url, conditional=url in self.known_urls)
        if r.not_modified:
            # 304 = la fiche n'a pas bouge. L'article est bien present au
            # catalogue: il doit compter comme vu, sinon on le desactive a tort.
            return ("unchanged", url, {"unchanged": True, "url": url})
        if not r.ok:
            return ("error", url, None)
        if self.session_marker and self.session_marker not in r.text:
            raise SessionLost(
                f"marqueur de session absent de {url} -- collecte interrompue "
                f"avant d'enregistrer des prix de visiteur anonyme")
        item = extract.extract(r.text, self.cfg.get("selectors"), self.cfg.get("datalayer"))
        if item is None:
            return ("no_price", url, None)
        item["url"] = url
        item.setdefault("currency", None)
        item["currency"] = item["currency"] or self.cfg.get("currency")
        if not item.get("sku"):
            item["sku"] = url.rstrip("/").rsplit("/", 1)[-1]  # dernier recours: le slug
        return ("ok", url, item)

    def iter_products(self):
        urls = self.product_urls()
        workers = self.cfg.get("workers", 4)
        stats = {"unchanged": 0, "no_price": 0, "error": 0}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for status, url, item in pool.map(self.scrape_one, urls):
                if status in ("ok", "unchanged"):
                    yield item
                if status != "ok":
                    stats[status] += 1
                    if status == "no_price":
                        log.debug("aucun prix extrait: %s", url)
        log.info("sitemap: %d inchangees (304), %d sans prix, %d erreurs",
                 stats["unchanged"], stats["no_price"], stats["error"])


# --- niveau 3: il faut vraiment un navigateur --------------------------------

@register("browser")
class BrowserAdapter(SitemapAdapter):
    """Playwright sur le Chromium systeme, UN onglet a la fois.

    Sur 2 Go de RAM, la parallelisation d'un navigateur headless est le moyen le
    plus sur de partir en swap et d'user la carte SD. On redemarre aussi le
    navigateur tous les N pages: Chromium fuit lentement sur des sessions longues.
    """

    RESTART_EVERY = 150

    def iter_products(self):
        from playwright.sync_api import sync_playwright

        urls = list(self.product_urls())
        log.info("browser: %d fiches a rendre (sequentiel)", len(urls))
        wait_for = self.cfg.get("wait_for")  # selecteur CSS signalant que le prix est arrive

        with sync_playwright() as pw:
            browser = ctx = None
            try:
                for i, url in enumerate(urls):
                    if i % self.RESTART_EVERY == 0:
                        if browser:
                            ctx.close(); browser.close()
                        browser = pw.chromium.launch(
                            executable_path=self.cfg.get("chromium", "/usr/bin/chromium"),
                            args=["--disable-dev-shm-usage", "--disable-gpu",
                                  "--no-sandbox", "--disable-extensions",
                                  "--blink-settings=imagesEnabled=false"],
                        )
                        ctx = browser.new_context(
                            user_agent=self.fetcher.client.headers["User-Agent"],
                            viewport={"width": 1280, "height": 900},
                            storage_state=self.cfg.get("storage_state") or None,
                        )
                        # Les images/polices/videos ne servent a rien ici et
                        # representent l'essentiel du trafic et de la RAM.
                        ctx.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|mp4)"),
                                  lambda route: route.abort())

                    page = ctx.new_page()
                    try:
                        self.fetcher.limiter.wait()
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        if wait_for:
                            page.wait_for_selector(wait_for, timeout=10000)
                        content = page.content()
                        if self.session_marker and self.session_marker not in content:
                            raise SessionLost(f"marqueur de session absent de {url}")
                        item = extract.extract(content, self.cfg.get("selectors"),
                                               self.cfg.get("datalayer"))
                    except SessionLost:
                        raise          # une page qui echoue s'ignore, pas une session perdue
                    except Exception as exc:
                        log.debug("browser: echec %s (%s)", url, exc)
                        item = None
                    finally:
                        page.close()

                    if item:
                        item["url"] = url
                        item["currency"] = item.get("currency") or self.cfg.get("currency")
                        item["sku"] = item.get("sku") or url.rstrip("/").rsplit("/", 1)[-1]
                        yield item
            finally:
                if browser:
                    ctx.close(); browser.close()


def build(cfg: dict, fetcher, conn) -> Adapter:
    kind = cfg.get("type", "sitemap")
    if kind not in REGISTRY:
        raise ValueError(f"type d'adaptateur inconnu: {kind} (dispo: {', '.join(REGISTRY)})")
    return REGISTRY[kind](cfg, fetcher, conn)
