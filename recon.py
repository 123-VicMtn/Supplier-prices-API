#!/usr/bin/env python3
"""Sonde une boutique en ligne pour determiner la strategie d'extraction la moins couteuse.

Deux modes.

1) Anonyme, avant toute configuration:
       .venv/bin/python recon.py https://shop-fournisseur.ch
   Repond a: quelle plateforme ? y a-t-il un sitemap ? un catalogue JSON ?
   le prix est-il dans le HTML brut ou faut-il un navigateur ?

2) Authentifie, une fois le fournisseur declare dans suppliers.yaml:
       set -a && source .env && set +a
       .venv/bin/python recon.py --supplier ma_cle
   Repond a: le login passe-t-il ? la decouverte trouve-t-elle des fiches ?
   ou est le prix client ? et surtout: la boutique sert-elle un prix
   different au visiteur anonyme (le piege de la valeur sentinelle) ?

Dependances: celles de requirements.txt.
"""

import gzip
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from pricewatch import extract

UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}
TIMEOUT = 20

# Marqueurs dans le HTML -> plateforme. L'ordre compte, du plus specifique au plus vague.
PLATFORM_MARKERS = [
    ("Shopify", ("cdn.shopify.com", "Shopify.theme", "shopify-features")),
    ("WooCommerce", ("woocommerce", "wp-content/plugins/woocommerce")),
    ("PrestaShop", ("prestashop", "/modules/ps_")),
    ("Magento", ("Magento_", "mage/cookies", "static/version")),
    ("Shopware", ("shopware", "/widgets/")),
    ("BigCommerce", ("cdn11.bigcommerce.com", "bigcommerce.com/s-")),
    ("Odoo", ("odoo", "/web/static/")),
    ("Wix", ("wixstatic.com", "wix-code")),
    ("Squarespace", ("squarespace.com", "static1.squarespace")),
]

# Un prix "visible" dans du HTML: 12.90 / 12,90 accole a un symbole, ou un microformat.
PRICE_PATTERNS = [
    re.compile(r'itemprop=["\']price["\']'),
    re.compile(r'"price"\s*:\s*["\']?\d'),
    re.compile(r"(?:CHF|EUR|€|\$)\s?\d{1,6}[.,]\d{2}"),
    re.compile(r"\d{1,6}[.,]\d{2}\s?(?:CHF|EUR|€)"),
]

JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL | re.IGNORECASE)


def body(r):
    """Texte de la reponse, en degzippant les sitemaps .xml.gz servis en binaire."""
    if r.content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(r.content).decode("utf-8", "replace")
        except (OSError, EOFError):
            pass
    return r.text


def new_session():
    # follow_redirects n'est PAS le defaut chez httpx (contrairement a requests):
    # sans lui, une boutique qui redirige vers sa locale renvoie un 301 nu.
    return httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)


def get(session, url, **kw):
    """GET tolerant: renvoie la reponse ou None, sans jamais lever."""
    try:
        r = session.get(url, **kw)
        return r if r.status_code < 400 else None
    except httpx.RequestError:
        return None


def detect_platform(html):
    low = html.lower()
    for name, markers in PLATFORM_MARKERS:
        if any(m.lower() in low for m in markers):
            return name
    m = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', html, re.I)
    return f"? (generator: {m.group(1)})" if m else "inconnue"


def probe_json_endpoints(session, base):
    """Endpoints catalogue standards. Un hit ici = pas besoin de scraper du tout."""
    candidates = [
        ("Shopify products.json", "/products.json?limit=5"),
        ("WooCommerce Store API", "/wp-json/wc/store/v1/products?per_page=5"),
        ("WooCommerce Store API (v0)", "/wp-json/wc/store/products?per_page=5"),
    ]
    found = []
    for label, path in candidates:
        r = get(session, urljoin(base, path))
        if not r or "json" not in r.headers.get("content-type", ""):
            continue
        try:
            data = r.json()
        except ValueError:
            continue
        items = data.get("products", data) if isinstance(data, dict) else data
        if isinstance(items, list) and items:
            found.append((label, urljoin(base, path), len(items)))
    return found


def find_sitemaps(session, base):
    """Sitemaps declares dans robots.txt, sinon /sitemap.xml."""
    urls = []
    r = get(session, urljoin(base, "/robots.txt"))
    if r:
        urls += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", body(r))
    if not urls:
        r = get(session, urljoin(base, "/sitemap.xml"))
        if r and "<" in body(r)[:200]:
            urls.append(urljoin(base, "/sitemap.xml"))
    return urls


def sample_product_urls(session, sitemap_url, want=3, depth=0):
    """Descend dans l'index de sitemaps et remonte quelques URLs qui sentent le produit."""
    r = get(session, sitemap_url)
    if not r:
        return []
    text = body(r)
    locs = LOC_RE.findall(text)
    if "<sitemapindex" in text[:2000].lower() and depth < 2:
        # Priorise les sous-sitemaps dont le nom evoque des produits.
        ranked = sorted(locs, key=lambda u: 0 if re.search(r"produ|article|item", u, re.I) else 1)
        out = []
        for sub in ranked[:3]:
            out += sample_product_urls(session, sub, want, depth + 1)
            if len(out) >= want:
                break
        return out[:want]
    product_like = [u for u in locs if re.search(r"/produ|/article|/item|/p/|/shop/", u, re.I)]
    return (product_like or locs)[:want]


def extract_jsonld_products(html):
    """Retourne les objets schema.org Product trouves, avec leur prix si present."""
    out = []

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            types = node.get("@type", "")
            types = types if isinstance(types, list) else [types]
            if any(str(t).lower() == "product" for t in types):
                offers = node.get("offers", {})
                offers = offers[0] if isinstance(offers, list) and offers else offers
                price = offers.get("price") if isinstance(offers, dict) else None
                cur = offers.get("priceCurrency") if isinstance(offers, dict) else None
                out.append({"name": node.get("name"), "sku": node.get("sku"), "price": price, "currency": cur})
            for v in node.values():
                walk(v)

    for block in JSONLD_RE.findall(html):
        try:
            walk(json.loads(block.strip()))
        except ValueError:
            continue
    return out


def analyse_product_page(session, url):
    r = get(session, url)
    if not r:
        return None
    html = body(r)
    return {
        "url": url,
        "jsonld": extract_jsonld_products(html),
        "price_in_html": [p.pattern for p in PRICE_PATTERNS if p.search(html)],
        "size_kb": len(html) // 1024,
        "needs_login": bool(re.search(r"connectez-vous|se connecter|login.*pour voir|prix.*sur demande", html, re.I)),
    }


def report(url):
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    session = new_session()
    print(f"\n{'=' * 70}\n  {base}\n{'=' * 70}")

    home = get(session, base)
    if not home:
        print("  [X] injoignable")
        return
    print(f"  Plateforme        : {detect_platform(body(home))}")

    endpoints = probe_json_endpoints(session, base)
    if endpoints:
        print("  [!! JACKPOT] catalogue expose en JSON, aucun scraping necessaire :")
        for label, ep, n in endpoints:
            print(f"      - {label}: {ep}  ({n} items sur la 1ere page)")
    else:
        print("  API catalogue     : aucune trouvee")

    sitemaps = find_sitemaps(session, base)
    print(f"  Sitemaps          : {len(sitemaps)} declare(s)" + (f" -> {sitemaps[0]}" if sitemaps else ""))

    samples = sample_product_urls(session, sitemaps[0]) if sitemaps else []
    if not samples:
        print("  Pages produit     : pas d'echantillon (sitemap absent ou opaque)")
        return

    print(f"  Echantillon       : {len(samples)} page(s) produit testee(s)")
    for s in samples:
        info = analyse_product_page(session, s)
        if not info:
            continue
        print(f"\n    {info['url']}  ({info['size_kb']} Ko)")
        if info["jsonld"]:
            for p in info["jsonld"]:
                print(f"      JSON-LD  : {p['name']!r} sku={p['sku']} prix={p['price']} {p['currency'] or ''}")
            verdict = "HTTP simple + JSON-LD  (leger, ~50 Mo RAM)"
        elif info["price_in_html"]:
            verdict = "HTTP simple + selecteur CSS a ecrire  (leger)"
        else:
            verdict = "prix absent du HTML brut -> navigateur headless requis (lourd)"
        if info["needs_login"]:
            verdict += "  [+ authentification necessaire]"
        print(f"      Strategie: {verdict}")


# =============================================================================
# Mode authentifie: --supplier <cle>
#
# Reconnaissance anonyme et reconnaissance connectee ne repondent pas aux memes
# questions. Une fois la session ouverte, ce qui compte est: ou se trouve le
# prix client, et est-ce que la page ment quand la session saute ?
# =============================================================================

def inspect_login_form(fetcher, login_url):
    """Champs du formulaire de connexion, pour remplir le bloc `auth:`."""
    r = fetcher.get(login_url, conditional=False)
    if not r.ok:
        return None
    forms = HTMLParser(r.text).css("form")
    target = next((f for f in forms
                   if "login" in (f.attributes.get("action") or "").lower()), None) or \
        (forms[0] if forms else None)
    if target is None:
        return None
    fields = []
    for inp in target.css("input"):
        name, typ = inp.attributes.get("name"), inp.attributes.get("type", "text")
        if name:
            fields.append((name, typ))
    return {"action": target.attributes.get("action"), "fields": fields}


def guess_datalayer_keys(html):
    """Cles du dataLayer qui ressemblent a un prix, une reference, un nom.

    C'est par la qu'on trouve le prix sur les boutiques B2B qui l'injectent en
    AJAX: le HTML rendu n'a qu'un spinner, mais le dataLayer, lui, est servi
    dans la page.
    """
    guessed = {}
    for obj in extract._json_objects_after(html, "dataLayer.push("):
        if not isinstance(obj, dict):
            continue
        for key, value in obj.items():
            if not isinstance(value, (str, int, float)):
                continue
            low = key.lower()
            if "price" in low or "prix" in low:
                if extract.parse_price(value):
                    guessed.setdefault("price", (key, value))
            elif low.endswith("sku") or "artikel" in low or "reference" in low:
                guessed.setdefault("sku", (key, value))
            elif low.endswith("name") and "page" not in low:
                guessed.setdefault("name", (key, value))
            elif "currency" in low or "devise" in low:
                guessed.setdefault("currency", (key, value))
    return guessed


def price_of(html, datalayer_map=None):
    """Prix trouve par chaque strategie, pour savoir laquelle configurer."""
    found = {}
    for label, item in (
        ("json-ld", extract.from_jsonld(html)),
        ("microdata", extract.from_microdata(HTMLParser(html))),
        ("datalayer", extract.from_datalayer(html, datalayer_map) if datalayer_map else None),
    ):
        if item and item.get("price") is not None:
            found[label] = item
    return found


def report_supplier(key, config_path):
    import shutil
    import tempfile

    import yaml
    from pricewatch import adapters, db as dbmod
    from pricewatch.fetch import Fetcher

    cfg_all = yaml.safe_load(Path(config_path).read_text())
    entry = next((s for s in cfg_all["suppliers"] if s["key"] == key), None)
    if entry is None:
        sys.exit(f"fournisseur {key!r} absent de {config_path}")
    cfg = {**cfg_all.get("defaults", {}), **entry}

    print(f"\n{'=' * 72}\n  {cfg.get('name', key)}  [{key}]\n{'=' * 72}")
    conn = dbmod.open_db(cfg_all.get("database", "var/prices.db"))
    fetcher = Fetcher(conn, key, delay=cfg.get("delay", 1.5),
                      session_dir=cfg.get("session_dir", "var/sessions"))
    # Session jetable, sans cookies: sert a voir le site comme un visiteur
    # anonyme. Indispensable des la premiere etape -- interroger la page de
    # login avec une session ouverte ne montre que la page de compte.
    tmpdir = tempfile.mkdtemp()
    anon = Fetcher(conn, "recon_anon", delay=cfg.get("delay", 1.5), session_dir=tmpdir)

    # --- 1. authentification ---------------------------------------------
    auth = cfg.get("auth")
    if auth:
        form = inspect_login_form(anon, auth["url"])
        if form and form["fields"]:
            print(f"  Formulaire        : action={form['action']}")
            print("    champs          : " + ", ".join(
                f"{n} ({t})" for n, t in form["fields"]))
        else:
            print("  Formulaire        : introuvable — page de login en JavaScript ?")
        if not fetcher.login(auth):
            print("  [X] LOGIN REFUSE — verifiez ${...} dans .env et le champ `check`")
            return
        print("  Authentification  : OK")
    else:
        print("  Authentification  : aucune declaree (bloc `auth:` absent)")

    # --- 2. decouverte ----------------------------------------------------
    adapter = adapters.build(cfg, fetcher, conn)
    adapter.session_marker = None          # on veut diagnostiquer, pas interrompre
    urls = []
    for u in adapter.product_urls():
        urls.append(u)
        if len(urls) >= 3:
            break
    if not urls:
        print("  [X] aucune URL produit. Verifiez `sitemaps:` et `product_url_pattern:`.")
        print("      Piege frequent: /robots.txt redirige vers une autre langue/canal.")
        return
    print(f"  Decouverte        : OK, exemple -> {urls[0]}")

    # --- 3. ou est le prix ? ---------------------------------------------
    r = fetcher.get(urls[0], conditional=False)
    dl = guess_datalayer_keys(r.text)
    if dl:
        print("  dataLayer detecte :")
        for role, (k, v) in dl.items():
            print(f"      {role:<9} <- {k} = {v!r}")
    dl_map = {role: k for role, (k, _) in dl.items()}
    found = price_of(r.text, dl_map)
    print("  Strategies qui donnent un prix : " +
          (", ".join(found) if found else "AUCUNE"))
    if not found:
        print("      -> ecrivez un bloc `selectors:`, ou passez en `type: browser`.")
        return

    # --- 4. le test qui evite la catastrophe ------------------------------
    # Une boutique qui sert une valeur sentinelle (et non un prix masque) au
    # visiteur anonyme corrompt toute la base si la session saute en silence.
    ra = anon.get(urls[0], conditional=False)
    best = next(iter(found))
    prix_connecte = found[best]["price"]
    anon_found = price_of(ra.text, dl_map)
    prix_anonyme = anon_found[best]["price"] if best in anon_found else None

    print(f"  Prix connecte     : {prix_connecte}")
    print(f"  Prix anonyme      : {prix_anonyme if prix_anonyme is not None else 'aucun (bien)'}")
    if prix_anonyme is not None and prix_anonyme != prix_connecte:
        print("  [!] DANGER: la page sert un prix different hors session.")
        print("      Si la session saute, ce prix serait enregistre comme le votre.")
        print("      `session_marker:` est INDISPENSABLE pour ce fournisseur.")
    elif prix_anonyme is not None:
        print("  [i] Meme prix connecte et anonyme: ce tarif n'est peut-etre pas negocie.")

    # --- 5. marqueur de session suggere -----------------------------------
    for candidate in ('"visitorLoginState":"Logged In"', "account/logout",
                      "/logout", "Deconnexion", "Se deconnecter"):
        if candidate in r.text and candidate not in ra.text:
            print(f"  session_marker    : {candidate!r}  (present connecte, absent anonyme)")
            break
    else:
        print("  session_marker    : aucun candidat evident, cherchez-en un a la main")

    fetcher.close()
    anon.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--supplier":
        if len(sys.argv) < 3:
            sys.exit("usage: recon.py --supplier <cle> [suppliers.yaml]")
        report_supplier(sys.argv[2],
                        sys.argv[3] if len(sys.argv) > 3 else "suppliers.yaml")
        sys.exit(0)
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for arg in sys.argv[1:]:
        report(arg if "://" in arg else "https://" + arg)
