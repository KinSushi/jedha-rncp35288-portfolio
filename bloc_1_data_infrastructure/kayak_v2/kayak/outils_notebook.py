# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Fonctions d'aide du notebook Kayak, sorties du notebook pour être testées.

Le notebook ``Kayak_Bloc1_CDSD.ipynb`` calcule des volumes, un bilan des réponses Overpass et une
image allégée. Ces fonctions ne dépendent que du data lake et de fichiers locaux : elles sont
ici, dans le paquet, pour avoir des tests directs et inverses.

CHOIX
-----
- **Retenu : extraire du notebook les fonctions qui calculent** (``milliers``, ``octets``,
  ``bilan_overpass``, ``hotels_de``, ``image_allegee``) et leur passer le lake en paramètre plutôt
  qu'une variable globale du notebook. *Écarté* : les laisser définies dans les cellules, où aucun
  test ne peut les atteindre.
- **Retenu : ``afficher``** enveloppe ``IPython.display.display``, non typé, pour que les cellules
  restent vérifiables par ``mypy --strict`` sans ``type: ignore`` dans le notebook.
- **Retenu : ``ImagePIL.Resampling.LANCZOS``** (même filtre qu'avant, même valeur numérique)
  plutôt que la constante ``LANCZOS`` du module, que le typage de Pillow 12 ne déclare plus.

OÙ
---
- **Place** : module d'aide de présentation, hors chaîne de données. Appelé uniquement par
  ``Kayak_Bloc1_CDSD.ipynb`` (et sa source ``Kayak_Bloc1_CDSD_source.py``).
- **Dépendances** : ``kayak.stockage.data_lake`` (lecture seule), pandas, Pillow, IPython.

COMMENT
-------
- ``octets`` additionne la taille réelle des fichiers du lake ; ``bilan_overpass`` lit chaque
  archive ``ville_NN.json`` d'un jour et compte éléments, points, contours et éléments nommés ;
  ``hotels_de`` relit ``hotels.jsonl`` ; ``image_allegee`` réduit un PNG à 256 couleurs.
- **Preuves** : les comptes d'une archive (tests/test_outils_notebook.py:250), la somme des
  tailles (tests/test_outils_notebook.py:188), l'image (tests/test_outils_notebook.py:564).
- **Refus** (tests inverses) : un fichier absent est refusé sans être créé
  (tests/test_outils_notebook.py:226), une archive qui n'est pas du JSON aussi
  (tests/test_outils_notebook.py:445), et aucune fonction ne modifie le lake
  (tests/test_outils_notebook.py:401).
- Exécution réelle dans le notebook : tests/test_notebook.py.

POURQUOI
--------
- Les fonctions reprennent à l'identique le code des cellules du notebook livré, sauf le passage
  du lake en paramètre : la comparaison des sorties avant et après est dans le rapport de mise en
  conformité. Code appelant : Kayak_Bloc1_CDSD_source.py:108.
- Les unités du volume sont décimales (1 Ko = 1 000 octets) : voir la cellule « Volume du data
  lake » du notebook, qui les annonce ; source : inconnue pour le choix des unités, décision de
  présentation.

JOURNALISATION
--------------
- Aucune : les fonctions rendent des valeurs, et le notebook les affiche avec ``print``. Aucun
  ``logging``.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
from IPython.display import Image, display
from PIL import Image as ImagePIL

from kayak.stockage.data_lake import DataLake


def afficher(objet: object) -> None:
    """Affiche un objet dans le notebook (rendu riche d'IPython : tableau, image...)."""
    display(objet)  # type: ignore[no-untyped-call]


def milliers(nombre: int) -> str:
    """Entier avec une espace entre les milliers (usage français)."""
    return f"{nombre:,}".replace(",", " ")


def octets(lake: DataLake, chemins: Iterable[str]) -> int:
    """Taille cumulée, en octets, des fichiers du data lake donnés."""
    return sum(lake.chemin_local(c).stat().st_size for c in chemins)


def bilan_overpass(lake: DataLake, jour: str, coordonnees: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par ville : éléments, points, contours, remarque d'Overpass, marque de requête."""
    lignes: list[dict[str, Any]] = []
    for chemin in sorted(lake.lister(f"raw/osm/date={jour}/")):
        if chemin.endswith(".json"):
            # Le numéro de ville est le nombre qui suit « ville_ » (un ou plusieurs chiffres).
            numero = re.search(r"ville_(\d+)\.json$", chemin)
            if numero is None:
                raise ValueError(f"nom d'archive Overpass inattendu : {chemin}")
            reponse = json.loads(lake.lire(chemin).decode("utf-8"))
            elements = reponse["elements"]
            lignes.append(
                {
                    "id_ville": int(numero.group(1)),
                    "elements": len(elements),
                    "nommes": sum(bool(e.get("tags", {}).get("name")) for e in elements),
                    "points": sum(e["type"] == "node" for e in elements),
                    "contours": sum(e["type"] == "way" for e in elements),
                    "remarque": reponse.get("remark"),
                    "requete_actuelle": "kayak_requete" in reponse,
                }
            )
    return pd.DataFrame(lignes).merge(coordonnees[["id_ville", "ville"]], on="id_ville")


def hotels_de(lake: DataLake, jour: str) -> pd.DataFrame:
    """Hôtels convertis (hotels.jsonl) d'une collecte."""
    return pd.read_json(lake.chemin_local(f"raw/osm/date={jour}/hotels.jsonl"), lines=True)


def image_allegee(chemin: Path, largeur: int = 1600) -> Image:
    """Copie réduite (largeur en pixels, 256 couleurs) d'un PNG : le notebook reste léger.

    Les PNG de `sorties/` gardent leur pleine résolution (2200 à 3200 pixels de large, plusieurs
    Mo) ; le notebook n'en embarque qu'une version réduite.
    """
    with ImagePIL.open(chemin) as source:
        image = source.convert("RGB")
    image = image.resize(
        (largeur, round(image.height * largeur / image.width)), ImagePIL.Resampling.LANCZOS
    )
    tampon = io.BytesIO()
    image.quantize(colors=256, method=ImagePIL.Quantize.MEDIANCUT).save(
        tampon, format="PNG", optimize=True
    )
    return Image(data=tampon.getvalue(), format="png", width=1000)  # type: ignore[no-untyped-call]
