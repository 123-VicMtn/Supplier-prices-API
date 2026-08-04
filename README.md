# Pricewatch

Collecte des prix d'articles chez des fournisseurs qui n'ont **pas** de catalogue
téléchargeable, uniquement une boutique en ligne. Conçu pour tourner sur un
Raspberry Pi 4B 2 Go.

## Le principe

Une boutique en ligne *est* un catalogue, simplement mal emballé. Trois niveaux
d'accès, du moins coûteux au plus coûteux :

| Niveau | Accès | Coût | Robustesse |
|---|---|---|---|
| 1 | Catalogue JSON exposé (`/products.json`, Store API WooCommerce) | ~10 requêtes | très haute |
| 2 | Sitemap pour la liste + JSON-LD, microdata ou `dataLayer` pour le prix | 1 requête/article, ~30 Mo RAM | haute |
| 3 | Rendu JavaScript via Chromium headless | 1 page/2-3 s, ~400 Mo RAM | moyenne |

**Toujours commencer par `recon.py`** : il classe chaque fournisseur dans un de
ces niveaux. Beaucoup de boutiques qu'on croit « à scraper » tombent en niveau 1.

```bash
.venv/bin/python recon.py shop-fournisseur-a.com pro.fournisseur-d.com
```

## Installation du Raspberry Pi, depuis zéro

Compter une heure, dont l'essentiel en téléchargements. Tout se fait en SSH,
le Pi n'a besoin ni d'écran ni de clavier.

### 1. Matériel

- Raspberry Pi 4B 2 Go, son alimentation officielle (une alim sous-dimensionnée
  provoque des corruptions de carte SD difficiles à diagnostiquer)
- carte micro-SD de 16 Go minimum, en classe A1 ou A2
- **de préférence un petit SSD USB** plutôt que la carte SD : la base est
  réécrite chaque nuit, et c'est ce qui use les cartes SD. Un SSD de 120 Go
  d'entrée de gamme suffit largement et change la durée de vie du montage.

### 2. Graver le système

Avec [Raspberry Pi Imager](https://www.raspberrypi.com/software/), choisir
**Raspberry Pi OS Lite (64-bit)**. Les deux mots comptent :

- **64-bit** : les paquets Python compilés (`selectolax`, `httpx[http2]`) ne
  sont distribués en roue précompilée que pour `aarch64`. En 32 bits, `pip`
  tente de compiler depuis les sources et échoue ou prend une heure.
- **Lite** : pas d'environnement de bureau. Sur 2 Go de RAM, cela libère
  environ 400 Mo — soit la marge qui rend le tout confortable.

Avant de graver, ouvrir les réglages avancés de l'Imager (l'engrenage, ou
`Ctrl+Shift+X`) et renseigner :

| Réglage | Valeur |
|---|---|
| Nom d'hôte | `pricewatch` → joignable en `pricewatch.local` |
| Activer SSH | avec **authentification par clé publique**, pas par mot de passe |
| Utilisateur | `pi` (voir la note plus bas si vous choisissez autre chose) |
| Wi-Fi | vos identifiants, ou rien si vous branchez en Ethernet |
| Fuseau horaire | `Europe/Zurich` — il détermine l'heure de la collecte nocturne |

> **Le nom d'utilisateur n'est plus `pi` par défaut** depuis 2022, et l'Imager
> vous en fait choisir un. Les unités systemd fournies contiennent
> `/home/pi` en dur. Le plus simple est de garder `pi` ; sinon, l'étape 6
> donne la commande pour les adapter.

### 3. Premier démarrage

```bash
ssh pi@pricewatch.local
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

Si `pricewatch.local` ne répond pas, votre réseau ne relaie pas le mDNS :
récupérez l'adresse IP sur l'interface de votre box et utilisez-la.

### 4. Réglages système qui comptent sur 2 Go

Ces trois réglages ne sont pas cosmétiques : ils décident si le Pi tient des
mois ou s'il devient illisible et use sa carte.

```bash
# a) zram plutôt qu'un fichier d'échange sur la carte SD.
#    Compresser la RAM coûte quelques % de CPU ; swapper sur SD la détruit.
sudo apt install -y zram-tools
printf 'ALGO=zstd\nPERCENT=50\n' | sudo tee -a /etc/default/zramswap
sudo systemctl restart zramswap
sudo dphys-swapfile swapoff && sudo systemctl disable dphys-swapfile

# b) Borner le journal système, qui grossit sans limite par défaut.
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/taille.conf
sudo systemctl restart systemd-journald

# c) Correctifs de sécurité automatiques : la machine est en service permanent.
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Vérifier au passage que l'horloge est juste — les horodatages de l'historique
des prix et le cache `Last-Modified` en dépendent :

```bash
timedatectl   # doit indiquer "System clock synchronized: yes"
```

### 5. Installer Pricewatch

```bash
sudo apt install -y git python3-venv
git clone <votre-dépôt> ~/pricewatch && cd ~/pricewatch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp suppliers.example.yaml suppliers.yaml
```

Les identifiants des comptes pro vont dans `.env`, **jamais** dans le YAML.
`umask 077` crée le fichier directement en `600`, et `read -s` évite de laisser
le mot de passe dans l'historique du shell :

```bash
cd ~/pricewatch && umask 077 && \
  read -rp 'Identifiant fournisseur : ' user && \
  read -rsp 'Mot de passe : ' pass && echo && \
  printf 'KRANNICH_USER=%s\nKRANNICH_PASS=%s\n' "$user" "$pass" > .env && \
  unset user pass && echo '.env créé'
```

Chromium n'est nécessaire **que** si un fournisseur est en niveau 3 :

```bash
sudo apt install -y chromium && .venv/bin/pip install playwright
```

N'utilisez pas `playwright install` : il n'y a pas de build Chromium arm64
fiable côté Playwright. Le paquet Debian fonctionne, d'où `chromium:` dans le YAML.

Premier essai, qui n'écrit rien :

```bash
cd ~/pricewatch && set -a && source .env && set +a
.venv/bin/python -m pricewatch.run --supplier krannich --limit 20 --dry-run
```

### 6. Mettre en service

Si votre utilisateur n'est pas `pi`, adapter les unités avant de les copier :

```bash
sed -i "s|/home/pi|$HOME|g; s|^User=pi\$|User=$USER|" ~/pricewatch/systemd/*.service
```

```bash
sudo cp ~/pricewatch/systemd/*.service ~/pricewatch/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pricewatch-scrape.timer pricewatch-web.service
```

Contrôles :

```bash
systemctl list-timers pricewatch-scrape.timer   # prochaine exécution
systemctl status pricewatch-web.service         # l'UI doit être "active (running)"
sudo systemctl start pricewatch-scrape.service  # forcer une collecte tout de suite
journalctl -u pricewatch-scrape.service -f      # la suivre en direct
```

### 7. Accéder à l'interface, sans l'exposer

L'UI écoute sur `http://pricewatch.local:8080`. **Elle n'a aucune
authentification** : elle n'est pas faite pour être exposée sur Internet. À
réserver au réseau local, et si le Pi est joignable de l'extérieur, la fermer :

```bash
sudo apt install -y ufw
sudo ufw allow ssh
sudo ufw allow from 192.168.0.0/16 to any port 8080 proto tcp
sudo ufw enable
```

Pour y accéder à distance, passez par un tunnel SSH plutôt que par une
redirection de port :

```bash
ssh -L 8080:localhost:8080 pi@<ip-du-pi>   # puis http://localhost:8080
```

### 8. Sauvegarder

Tout l'état tient dans un seul fichier. Une copie hebdomadaire suffit, et
`.backup` est sûr même pendant une collecte, contrairement à un `cp` — qui
copierait une base incohérente si une écriture est en cours :

```bash
sudo apt install -y sqlite3   # absent de l'image Lite
sqlite3 ~/pricewatch/var/prices.db ".backup /chemin/de/sauvegarde/prices-$(date +%F).db"
```

`suppliers.yaml` mérite d'être versionné dans votre dépôt ; `.env`, `var/` et
`.venv/` ne doivent jamais l'être — ils sont déjà dans `.gitignore`. Et ne
recopiez jamais un `.venv` d'une machine à l'autre : il contient des chemins
absolus. Recréez-le sur place.

## Utilisation

```bash
# Mise au point d'un fournisseur : affiche 20 articles, n'écrit rien
.venv/bin/python -m pricewatch.run --supplier fournisseur_public --limit 20 --dry-run

# Passage complet
.venv/bin/python -m pricewatch.run --config suppliers.yaml

# Relecture intégrale, cache conditionnel ignoré (à programmer une fois par semaine)
.venv/bin/python -m pricewatch.run --config suppliers.yaml --no-cache

# UI web + API sur http://<ip-du-pi>:8080
.venv/bin/uvicorn pricewatch.api:app --host 0.0.0.0 --port 8080
```

Automatisation :

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pricewatch-scrape.timer pricewatch-web.service
```

## Ajouter un fournisseur

Une entrée YAML, pas de code. `type:` vaut `shopify`, `woocommerce`, `sitemap`
ou `browser` — c'est `recon.py` qui vous dit lequel. Les `selectors:` ne servent
que si ni JSON-LD ni microdata ne portent le prix ; commencez sans, ajoutez-les
seulement si le `--dry-run` sort vide.

## Ajouter un fournisseur derrière un login

Six étapes, dont deux commandes qui font le gros du travail. Krannich sert de
modèle complet dans `suppliers.yaml`.

### 1. Reconnaissance anonyme

```bash
.venv/bin/python recon.py https://shop.nouveau-fournisseur.ch
```

Vous obtenez la plateforme, l'existence d'un sitemap, et si un catalogue JSON
est exposé. Si c'est du Shopify ou du WooCommerce, regardez d'abord si l'API
catalogue ne suffit pas — ce serait beaucoup plus simple.

### 2. Trouver la page de connexion et écrire une entrée minimale

Cherchez l'URL du formulaire (typiquement `/account/login`, `/connexion`,
`/customer/account/login`). Une entrée de départ suffit — le reste viendra de
l'étape 4 :

```yaml
  - key: nouveau
    name: Nouveau Fournisseur
    type: sitemap
    base_url: https://shop.nouveau-fournisseur.ch
    delay: 2.5              # compte identifié : restez poli
    workers: 2
    currency: CHF
    auth:
      url: https://shop.nouveau-fournisseur.ch/account/login
      data:
        username: ${NOUVEAU_USER}     # noms de champs corrigés à l'étape 4
        password: ${NOUVEAU_PASS}
      check: "à remplir"
```

### 3. Les identifiants dans `.env`, jamais dans le YAML

```bash
cd ~/pricewatch && umask 077 && \
  read -rp 'Identifiant : ' user && read -rsp 'Mot de passe : ' pass && echo && \
  printf 'NOUVEAU_USER=%s\nNOUVEAU_PASS=%s\n' "$user" "$pass" >> .env && \
  unset user pass
```

### 4. La commande qui fait le diagnostic

```bash
set -a && source .env && set +a
.venv/bin/python recon.py --supplier nouveau
```

Elle répond, dans l'ordre, aux cinq questions qui posent problème :

| Ce qu'elle affiche | Ce que vous en faites |
|---|---|
| Les champs réels du formulaire, vus en anonyme | corriger les clés de `data:` |
| Si le login passe | si non : `.env`, ou `check:` trop strict |
| Une URL produit d'exemple | si vide : `sitemaps:` et `product_url_pattern:` |
| Quelle stratégie donne un prix, et les clés `dataLayer` détectées | remplir `datalayer:` ou `selectors:` |
| **Le prix servi au visiteur anonyme** | décider si `session_marker:` est vital |

Le jeton CSRF, `redirectTo` et les autres champs cachés sont repris
automatiquement du formulaire : ne les déclarez pas.

### 5. Compléter le YAML

Recopiez ce que la commande vous a donné. Pour Krannich, cela donne :

```yaml
    sitemaps:                        # évite /robots.txt — voir les pièges
      - https://shop.krannich-solar.com/fr-ch/sitemap.xml
    product_url_pattern: "/\\d{7}$"
    datalayer:
      price: productPrice
      sku: productSku
      name: productName
      currency: productCurrency
    session_marker: '"visitorLoginState":"Logged In"'
```

### 6. Essayer, puis lancer

```bash
.venv/bin/python -m pricewatch.run --supplier nouveau --limit 20 --dry-run
.venv/bin/python -m pricewatch.run --supplier nouveau     # sans --dry-run : écrit
```

`--dry-run` **n'écrit rien** : il affiche et annule. Le passage apparaît dans
l'onglet « Collectes » avec le statut `dry-run`, pas `ok`.

### Les quatre pièges, dans l'ordre où ils vous tomberont dessus

**Le prix vaut `null` alors que vous êtes connecté.** Il est injecté en AJAX ;
le HTML n'a qu'un spinner. Cherchez le `dataLayer` avant de sortir le
navigateur headless — `recon.py --supplier` le fait pour vous.

**La boutique sert un prix différent en anonyme.** Le cas le plus coûteux :
certaines renvoient une valeur sentinelle (Krannich : `9999.00`) au lieu de
masquer le prix. Une session qui saute enregistre alors un catalogue entier de
prix faux, avec le bon nombre d'articles, donc invisible pour le contrôle de
santé. `session_marker:` est la seule protection.

**`/robots.txt` vous emmène ailleurs.** Sur les boutiques multilingues, il
redirige vers le canal par défaut — et chez Shopware, ce détour **efface les
cookies de session**. Épinglez `sitemaps:` sur le canal voulu.

**Les sitemaps sont souvent gzippés** (`.xml.gz` servi en
`application/octet-stream`). C'est géré, mais si une découverte revient vide
alors que l'URL répond 200, c'est la première chose à vérifier.

## Points de conception à connaître

**Historique delta.** Une ligne dans `price` uniquement quand le prix change.
Sur 5 000 articles relevés chaque nuit, c'est la différence entre ~15 Mo et
~2 Go de base au bout d'un an — décisif sur carte SD.

**Cache conditionnel.** ETag/Last-Modified sont stockés et renvoyés. En régime
établi, la plupart des fiches répondent `304` en quelques centaines d'octets,
sans parsing. C'est ce qui rend « quelques milliers d'articles » tenable.
Un `304` compte comme *article vu* : sans cela, le contrôle de santé prendrait
un catalogue stable pour un catalogue disparu. Comme `Last-Modified` n'a qu'une
granularité d'une seconde et que tous les sites n'envoient pas d'ETag, prévoyez
un passage `--no-cache` hebdomadaire (une copie du timer avec un
`OnCalendar=Sun *-*-* 04:00:00` et `--no-cache` dans l'`ExecStart`).

**Contrôle de santé.** Le vrai mode de panne du scraping n'est pas l'erreur,
c'est le silence : le site change de structure et l'extraction renvoie zéro prix.
Si un passage remonte moins de 50 % des articles du dernier passage réussi, il
est marqué `suspect` : les données précédentes sont conservées, rien n'est
désactivé, et le code retour non nul déclenche `pricewatch-alert.service`.

**Suppression douce.** Un article absent passe `active = 0`, il n'est jamais
supprimé. Il revient tel quel s'il réapparaît, avec son historique.

**Prix injectés en AJAX.** Sur les boutiques B2B, le prix client est souvent
chargé après coup en JavaScript : le JSON-LD reste à `price: null` et le
conteneur HTML ne contient qu'un spinner. Avant de sortir l'artillerie du
navigateur headless, cherchez le `dataLayer` de Google Tag Manager — il porte
très souvent le prix négocié en clair dans le HTML brut. C'est le cas chez
Krannich, et c'est ce que déclare le bloc `datalayer:` de la config.

**Garde de session — le piège le plus coûteux.** Certaines boutiques ne
masquent pas le prix au visiteur anonyme : elles servent une **valeur
sentinelle**. Krannich renvoie `9999.00`. Si la session saute en cours de
collecte, on enregistrerait donc un catalogue entier de prix faux, avec le bon
nombre d'articles — invisible pour le contrôle de santé. D'où `session_marker` :
un texte qui doit figurer sur chaque fiche tant que la session tient. S'il
disparaît, la collecte s'arrête net (`failed`, code retour 1, alerte systemd)
plutôt que d'écrire. Par défaut il reprend le `check` de l'authentification ;
`session_marker: false` le désactive.

**Budget mémoire.** `MemoryMax=1200M` sur la collecte et `MemorySwapMax=0` :
mieux vaut un service tué qu'un Pi qui swappe sur SD pendant six heures.
L'adaptateur `browser` ne rend jamais deux pages en parallèle et redémarre
Chromium toutes les 150 fiches.

## V2 — injection dans Reonic

L'UI web ne dispose d'aucun accès privilégié : elle consomme les mêmes endpoints
`/api/v1/*` que consommera l'intégration. Ce qui est visible dans le navigateur
est donc déjà disponible côté machine :

| Endpoint | Usage |
|---|---|
| `GET /api/v1/products?supplier=&q=&limit=&offset=` | catalogue courant paginé |
| `GET /api/v1/products/{id}` | fiche + historique complet |
| `GET /api/v1/changes?days=30&min_pct=2` | variations, pour ne pousser que le delta |
| `GET /api/v1/export.csv` | export tabulaire |

Pour la V2, il restera à ajouter côté API : une authentification (clé d'API ou
mTLS), une correspondance `sku` fournisseur → référence Reonic, et un
déclencheur après chaque passage réussi. Le point d'accroche est le code retour
de `pricewatch.run` : un `ExecStartPost=` dans l'unité systemd suffit à pousser
le delta.

## Cadre d'usage

Les délais entre requêtes, le respect des `429/503` et le `User-Agent` explicite
sont là pour rester dans un usage raisonnable de comptes fournisseurs légitimes.
Vérifiez les CGU de vos fournisseurs : certains encadrent l'extraction
automatisée, et beaucoup fournissent un accès EDI ou un export sur simple
demande commerciale — ce qui reste toujours préférable à la collecte web.
