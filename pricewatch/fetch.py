"""Client HTTP: politesse, cache conditionnel, sessions authentifiees.

Le cache conditionnel est ce qui rend le volume "quelques milliers" tenable sur
un Pi: un article dont la fiche n'a pas bouge repond 304 en ~200 octets, sans
parsing. En regime etabli, la majorite d'un passage nocturne coute presque rien.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from . import db

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

ENV_REF = re.compile(r"\$\{(\w+)\}")


def body(response: httpx.Response) -> str:
    """Texte de la reponse, en degzippant si besoin.

    httpx decompresse le Content-Encoding, mais pas un fichier .xml.gz servi
    tel quel en application/octet-stream -- ce que font Shopware, PrestaShop et
    beaucoup d'autres pour leurs sitemaps. Sans ca, on parse du binaire.
    """
    raw = response.content
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw).decode("utf-8", "replace")
        except (OSError, EOFError):
            pass
    return response.text


def resolve_env(value):
    """Remplace ${VAR} par la variable d'environnement.

    Les identifiants fournisseurs ne vivent JAMAIS dans le YAML de config: on y
    met ${SONEPAR_PASS}, et la valeur arrive par systemd LoadCredential ou un
    fichier .env en chmod 600.
    """
    if isinstance(value, dict):
        return {k: resolve_env(v) for k, v in value.items()}
    if not isinstance(value, str):
        return value

    def sub(m):
        val = os.environ.get(m.group(1))
        if val is None:
            raise RuntimeError(f"variable d'environnement manquante: {m.group(1)}")
        return val

    return ENV_REF.sub(sub, value)


class RateLimiter:
    """Un intervalle minimum entre deux requetes vers le meme hote, thread-safe."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.min_interval
        if sleep_for:
            time.sleep(sleep_for)


@dataclass
class Response:
    url: str
    status: int
    text: str
    not_modified: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Fetcher:
    """Client HTTP par fournisseur: throttle, retries, cache 304, session persistante."""

    def __init__(self, conn, key: str, *, delay: float = 1.5, timeout: float = 25.0,
                 session_dir: str | Path = "var/sessions", headers: dict | None = None,
                 use_cache: bool = True):
        self.conn = conn
        self.key = key
        # Last-Modified n'a qu'une granularite d'une seconde et tous les sites
        # n'envoient pas d'ETag: un passage --no-cache periodique (hebdomadaire,
        # cf. README) rattrape les rares angles morts du cache conditionnel.
        self.use_cache = use_cache
        self.limiter = RateLimiter(delay)
        self._db_lock = threading.Lock()  # les adaptateurs paralleles partagent la connexion
        self.session_path = Path(session_dir) / f"{key}.cookies"
        self.client = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9", **(headers or {})},
            timeout=timeout,
            # Les redirections des GET sont suivies a la main (cf. _get_redirecting)
            # pour pouvoir refuser un passage https -> http.
            follow_redirects=False,
            http2=True,
        )
        self._load_cookies()

    # -- cookies persistants: evite de se reconnecter a chaque passage --------

    def _load_cookies(self):
        if not self.session_path.exists():
            return
        for line in self.session_path.read_text().splitlines():
            name, _, rest = line.partition("\t")
            value, _, domain = rest.partition("\t")
            if name and domain:
                self.client.cookies.set(name, value, domain=domain)
        log.debug("[%s] cookies restaures", self.key)

    def _save_cookies(self):
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{c.name}\t{c.value}\t{c.domain}" for c in self.client.cookies.jar]
        self.session_path.write_text("\n".join(lines))
        self.session_path.chmod(0o600)  # contient une session authentifiee

    # -- requetes ------------------------------------------------------------

    def _get_redirecting(self, url: str, headers: dict, max_hops: int = 6):
        """GET suivant les redirections, en refusant tout retour en clair.

        Certaines boutiques redirigent vers http:// au milieu d'une chaine (vu
        chez Shopware: /robots.txt -> http://host/de-de/robots.txt). Suivre ce
        saut enverrait le cookie de session B2B en clair sur le reseau. On force
        le https et on continue.
        """
        current = url
        for _ in range(max_hops):
            r = self.client.get(current, headers=headers)
            location = r.headers.get("location")
            if not (300 <= r.status_code < 400) or not location:
                return r
            target = urljoin(current, location)
            parts = urlsplit(target)
            if urlsplit(current).scheme == "https" and parts.scheme == "http":
                target = urlunsplit(("https", *parts[1:]))
                log.warning("[%s] redirection https->http refusee, forcee en https: %s",
                            self.key, target)
            current = target
        log.warning("[%s] trop de redirections: %s", self.key, url)
        return r

    def get(self, url: str, *, conditional: bool = True, retries: int = 2) -> Response:
        headers = {}
        conditional = conditional and self.use_cache
        if conditional:
            with self._db_lock:
                etag, last_mod = db.cache_get(self.conn, url)
            if etag:
                headers["If-None-Match"] = etag
            if last_mod:
                headers["If-Modified-Since"] = last_mod

        for attempt in range(retries + 1):
            self.limiter.wait()
            try:
                r = self._get_redirecting(url, headers)
            except httpx.RequestError as exc:
                if attempt == retries:
                    log.warning("[%s] echec reseau %s: %s", self.key, url, exc)
                    return Response(url, 0, "")
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 304:
                return Response(url, 304, "", not_modified=True)
            # 429/503: le serveur demande de lever le pied, on obeit.
            if r.status_code in (429, 503) and attempt < retries:
                wait = float(r.headers.get("Retry-After", 5 * (attempt + 1)))
                log.info("[%s] %s -> pause %.0fs", self.key, r.status_code, wait)
                time.sleep(min(wait, 60))
                continue

            if r.is_success and conditional:
                with self._db_lock:
                    db.cache_put(self.conn, url, r.headers.get("ETag"),
                                 r.headers.get("Last-Modified"))
            return Response(str(r.url), r.status_code, body(r))

        return Response(url, 0, "")

    def get_json(self, url: str, **kw):
        r = self.get(url, conditional=False, **kw)
        if not r.ok:
            return None
        try:
            return httpx.Response(200, text=r.text).json()
        except ValueError:
            return None

    # -- authentification ----------------------------------------------------

    def _hidden_fields(self, login_url: str) -> dict:
        """Recupere les champs caches du formulaire de connexion.

        Evite d'avoir a declarer un jeton CSRF par fournisseur: on renvoie tel
        quel ce que la page nous a donne (_csrf_token chez Shopware 6.4 et
        PrestaShop, form_key chez Magento, redirectTo chez Shopware 6.5+).
        """
        r = self.get(login_url, conditional=False)
        if not r.ok:
            return {}
        forms = HTMLParser(r.text).css("form")
        target = next((f for f in forms
                       if "login" in (f.attributes.get("action") or "").lower()), None)
        if target is None:
            target = forms[0] if forms else None
        if target is None:
            return {}
        fields = {}
        for inp in target.css('input[type="hidden"]'):
            name, value = inp.attributes.get("name"), inp.attributes.get("value")
            if name:
                fields[name] = value or ""
        if fields:
            log.debug("[%s] champs caches repris: %s", self.key, ", ".join(fields))
        return fields

    def login(self, spec: dict) -> bool:
        """spec: {url, data: {champ: valeur}, check: "texte attendu apres login"}.

        Verifie d'abord si la session restauree est encore valide, pour ne pas
        re-authentifier inutilement a chaque passage.
        """
        check = spec.get("check")
        probe_url = spec.get("probe") or spec["url"]
        if self.client.cookies.jar and check:
            probe = self.get(probe_url, conditional=False)
            if probe.ok and check in probe.text:
                log.info("[%s] session existante valide", self.key)
                return True

        # Les champs du YAML priment sur ceux repris de la page.
        data = {**self._hidden_fields(spec["url"]), **resolve_env(spec["data"])}
        self.limiter.wait()
        try:
            # Beaucoup de formulaires exigent un Referer coherent.
            r = self.client.post(spec["url"], data=data, headers={"Referer": spec["url"]},
                                 follow_redirects=True)
        except httpx.RequestError as exc:
            log.error("[%s] login injoignable: %s", self.key, exc)
            return False

        ok = r.is_success and (check is None or check in r.text)
        if not ok and check:
            # Certains sites redirigent vers une page d'accueil qui, elle, porte le marqueur.
            ok = check in self.get(probe_url, conditional=False).text
        if ok:
            self._save_cookies()
            log.info("[%s] authentifie", self.key)
        else:
            log.error("[%s] LOGIN REFUSE (verifiez les identifiants)", self.key)
        return ok

    def close(self):
        try:
            self._save_cookies()
        finally:
            self.client.close()
