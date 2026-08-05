# Pricewatch

Collecte nocturne de prix depuis des boutiques en ligne, sans catalogue exportable.
Conçu pour tourner sur un Raspberry Pi.

## Démarrage rapide

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp suppliers.example.yaml suppliers.yaml
# créer .env avec les identifiants (${VAR} référencées dans suppliers.yaml)
chmod 600 .env suppliers.yaml
```

Variables d'environnement (`.env`) : identifiants fournisseurs référencés par `${VAR}` dans `suppliers.yaml`.

## Commandes utiles

```bash
# Diagnostiquer un nouveau site
.venv/bin/python recon.py https://example-shop.com
.venv/bin/python recon.py --supplier ma_cle

# Lancer une collecte
set -a && source .env && set +a
.venv/bin/python -m pricewatch.run --supplier ma_cle --limit 20 --dry-run
.venv/bin/python -m pricewatch.run                              # tous les fournisseurs

# UI + API
.venv/bin/uvicorn pricewatch.api:app --host 0.0.0.0 --port 8080
```

## Structure

```
pricewatch/          modules Python (collecte, extraction, API)
recon.py             diagnostic d'un fournisseur
suppliers.yaml       config réelle (gitignored)
suppliers.example.yaml
var/                 base SQLite, cookies de session (gitignored)
systemd/             units pour le Pi
doc.md               documentation complète
```

## Déploiement Pi

Copier les units `systemd/` dans `/etc/systemd/system/`, adapter les chemins si l'utilisateur n'est pas `pi`, puis :

```bash
sudo systemctl enable --now pricewatch-scrape.timer
sudo systemctl enable --now pricewatch-web.service
```

## Documentation

Tout le reste (installation Pi, ajout d'un fournisseur, pièges courants, API) est dans [`doc.md`](doc.md).

## Licence

Voir [`LICENSE`](LICENSE) — consultation autorisée, partage interdit sans accord écrit.
