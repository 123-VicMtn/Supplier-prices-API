"""Extraction du couple (reference, prix) depuis une fiche produit HTML.

Ordre de preference, du plus stable au plus fragile:
  1. dataLayer (Google Tag Manager) -> si la config le declare explicitement
  2. JSON-LD schema.org/Product     -> donnees structurees, survit aux refontes
  3. microdata itemprop             -> idem, plus ancien
  4. selecteur CSS de la config     -> a n'ecrire que si rien d'autre ne marche

Le dataLayer merite d'etre en tete quand il est configure: sur les boutiques
B2B dont le prix est injecte en AJAX (le JSON-LD reste alors a null), c'est
souvent le seul endroit du HTML brut ou le prix client figure reellement.
"""

from __future__ import annotations

import json
import re

from selectolax.parser import HTMLParser

JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Nombre avec separateurs: 1'234.50 (CH), 1 234,50 (FR), 1,234.50 (EN), 1234.5
NUM_RE = re.compile(r"-?\d[\d\s' .,]*\d|\d")


def parse_price(raw) -> float | None:
    """Normalise un prix ecrit dans n'importe quelle convention europeenne.

    Le piege classique: "1,234.50" et "1.234,50" valent la meme chose mais se
    lisent a l'envers. On tranche sur la position du DERNIER separateur.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    m = NUM_RE.search(str(raw))
    if not m:
        return None
    s = m.group(0).replace(" ", "").replace(" ", "").replace("'", "")

    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot > last_comma:
        s = s.replace(",", "")                       # virgules = milliers
    elif last_comma > last_dot:
        s = s.replace(".", "").replace(",", ".")     # virgule = decimale
    # Un separateur suivi de 3 chiffres pile est un separateur de milliers,
    # pas une decimale: "1.234" vaut 1234, pas 1.234.
    if "." in s and len(s.split(".")[-1]) == 3 and s.count(".") == 1 and last_comma == -1:
        s = s.replace(".", "")

    try:
        value = float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def _json_objects_after(html: str, marker: str):
    """Objets JSON qui suivent `marker` dans le HTML.

    Un compteur d'accolades conscient des chaines, plutot qu'une regex: les
    payloads dataLayer contiennent des accolades et des guillemets echappes
    dans leurs valeurs, ce qui met en defaut un `\\{.*?\\}`.
    """
    for m in re.finditer(re.escape(marker), html):
        start = html.find("{", m.end())
        if start == -1:
            continue
        depth, in_str, escaped = 0, False, False
        for i in range(start, min(len(html), start + 200_000)):
            ch = html[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(html[start:i + 1])
                    except ValueError:
                        pass
                    break


def from_datalayer(html: str, mapping: dict) -> dict | None:
    """mapping: {price: "productPrice", sku: "productSku", ...} -> cles du dataLayer."""
    price_key = mapping.get("price")
    if not price_key:
        return None
    for obj in _json_objects_after(html, "dataLayer.push("):
        if not isinstance(obj, dict) or obj.get(price_key) in (None, ""):
            continue
        price = parse_price(obj[price_key])
        if price is None:
            continue
        return {
            "name": obj.get(mapping.get("name", "")),
            "sku": obj.get(mapping.get("sku", "")),
            "ean": obj.get(mapping.get("ean", "")),
            "category": obj.get(mapping.get("category", "")),
            "price": price,
            "currency": obj.get(mapping.get("currency", "")),
        }
    return None


def _walk_jsonld(node, out):
    if isinstance(node, list):
        for x in node:
            _walk_jsonld(x, out)
    elif isinstance(node, dict):
        types = node.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "product" for t in types):
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict) and offers.get("@type") == "AggregateOffer":
                price = offers.get("lowPrice") or offers.get("price")
            else:
                price = offers.get("price") if isinstance(offers, dict) else None
            category = node.get("category")
            if isinstance(category, dict):          # parfois un objet Thing
                category = category.get("name")
            elif isinstance(category, list):
                category = " > ".join(str(c) for c in category if c)
            out.append({
                "name": node.get("name"),
                "sku": node.get("sku") or node.get("mpn") or node.get("productID"),
                "ean": node.get("gtin13") or node.get("gtin") or node.get("gtin14"),
                "category": category,
                "price": parse_price(price),
                "currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
            })
        for v in node.values():
            _walk_jsonld(v, out)


def from_jsonld(html: str) -> dict | None:
    found = []
    for block in JSONLD_RE.findall(html):
        try:
            _walk_jsonld(json.loads(block.strip()), found)
        except ValueError:
            continue
    # Sur une fiche produit, plusieurs Product peuvent coexister (produits lies).
    # Le bon est le premier qui porte un prix.
    for item in found:
        if item["price"] is not None:
            return item
    return found[0] if found else None


def from_microdata(tree: HTMLParser) -> dict | None:
    def prop(name):
        node = tree.css_first(f'[itemprop="{name}"]')
        if node is None:
            return None
        return node.attributes.get("content") or node.text(strip=True)

    price = parse_price(prop("price"))
    if price is None:
        return None
    return {
        "name": prop("name"),
        "sku": prop("sku") or prop("mpn"),
        "ean": prop("gtin13"),
        "category": prop("category"),
        "price": price,
        "currency": prop("priceCurrency"),
    }


def from_selectors(tree: HTMLParser, selectors: dict) -> dict | None:
    """selectors: {price: "...", sku: "...", name: "..."} en CSS.

    Suffixe "@attr" pour lire un attribut plutot que le texte:
        price: "meta[property='product:price:amount']@content"
    """
    def pick(spec):
        if not spec:
            return None
        sel, _, attr = spec.partition("@")
        node = tree.css_first(sel.strip())
        if node is None:
            return None
        return node.attributes.get(attr) if attr else node.text(strip=True)

    price = parse_price(pick(selectors.get("price")))
    if price is None:
        return None
    return {
        "name": pick(selectors.get("name")),
        "sku": pick(selectors.get("sku")),
        "ean": pick(selectors.get("ean")),
        "category": pick(selectors.get("category")),
        "price": price,
        "currency": pick(selectors.get("currency")),
    }


def extract(html: str, selectors: dict | None = None,
            datalayer: dict | None = None) -> dict | None:
    """Chaine complete. Renvoie None si aucune strategie n'aboutit."""
    if not html:
        return None
    tree = HTMLParser(html)

    best = None
    for candidate in (from_datalayer(html, datalayer) if datalayer else None,
                      from_jsonld(html), from_microdata(tree),
                      from_selectors(tree, selectors) if selectors else None):
        if candidate is None:
            continue
        if best is None:
            best = candidate
        else:
            # Complete les trous d'une strategie avec les suivantes.
            for k, v in candidate.items():
                if best.get(k) is None:
                    best[k] = v
        if best.get("price") is not None and best.get("sku"):
            break

    return best if best and best.get("price") is not None else None
