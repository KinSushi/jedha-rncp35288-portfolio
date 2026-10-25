# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Lit les fichiers de la liste de prix publique d'AWS (Price List, offre « OnDemand »).

Pourquoi : un chiffrage FinOps doit citer des prix **datés** et **sourcés**. Ce script lit le
fichier officiel d'une offre et d'une région (``offers/v1.0/aws/<offre>/current/<région>/
index.json``), filtre les produits par leurs attributs et imprime prix, unité, description
officielle et date de publication. Aucun prix n'est saisi à la main.

Usage :
    python lire_prix_aws.py <index.json> <clé=valeur> [<clé=valeur> ...]
Exemple :
    python lire_prix_aws.py msk.json productFamily=Serverless
"""

from __future__ import annotations

import json
import sys


def lire(chemin: str, filtres: dict[str, str]) -> list[dict]:
    with open(chemin, encoding="utf-8") as flux:
        offre = json.load(flux)
    resultats = []
    for sku, produit in offre["products"].items():
        attributs = {"productFamily": produit.get("productFamily", ""),
                     **produit.get("attributes", {})}
        if not all(valeur.lower() in str(attributs.get(cle, "")).lower()
                   for cle, valeur in filtres.items()):
            continue
        for terme in offre.get("terms", {}).get("OnDemand", {}).get(sku, {}).values():
            for dimension in terme["priceDimensions"].values():
                resultats.append({
                    "sku": sku,
                    "prix_usd": float(dimension["pricePerUnit"].get("USD", "nan")),
                    "unite": dimension["unit"],
                    "description": dimension["description"],
                    "publication": offre.get("publicationDate"),
                })
    return resultats


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    criteres = dict(argument.split("=", 1) for argument in sys.argv[2:])
    for ligne in lire(sys.argv[1], criteres):
        print(f"{ligne['prix_usd']:>12.6f} USD / {ligne['unite']:<12} {ligne['description']}"
              f"  [{ligne['sku']}, publié {ligne['publication']}]")
