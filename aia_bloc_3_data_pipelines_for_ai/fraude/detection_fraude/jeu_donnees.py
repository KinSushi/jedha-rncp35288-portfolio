# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Lecture du jeu ``fraudTest.csv`` et mise au format de l'API temps réel.

Le jeu sert à deux choses : **entraîner** le modèle et **rejouer** un flux quand l'API n'est pas
joignable (ou pour une démonstration reproductible). Dans les deux cas, ses lignes doivent suivre
exactement le même chemin que celles de l'API (validation puis masquage) pour que le modèle
apprenne sur ce qu'il verra en production. Ce module convertit donc une ligne du fichier en
« ligne au format API » (mêmes noms de champs, horodatage ``current_time`` en millisecondes).

Particularité du fichier : sa colonne ``unix_time`` est décalée de sept ans par rapport à
``trans_date_trans_time`` (``unix_time`` de la première ligne = 2013, date = 2020). La date fait
foi : elle est lue comme UTC.

CHOIX
-----
- Retenu : passer le jeu d'entraînement par le même code que le flux (``valider_brut`` puis
  ``masquer``) : le modèle apprend sur des données déjà masquées, comme en production.
- Retenu : la date ``trans_date_trans_time`` (lue comme UTC) plutôt que ``unix_time``, décalé de
  sept ans dans ce fichier.
- Retenu : une lecture économe pour un jour donné (deux lectures : la seule colonne des dates,
  puis uniquement les lignes du jour si elles sont contiguës ; sinon lecture par tranches de
  100 000 lignes), parce que le conteneur du producteur n'a que 256 Mo.
- Retenu : une conversion en dictionnaires par tranches de 20 000 lignes, jamais en une fois.
- Écarté : tout charger en mémoire puis filtrer, et convertir les 555 719 lignes en dictionnaires
  d'un coup (mémoire bornée des conteneurs).

OÙ
---
- Place : adaptateur entre le fichier CSV et le pipeline.
- Appelants : lignes_du_jeu (detection_fraude/producteur.py:295-344) (rejeu),
  entrainer (detection_fraude/entrainement.py:351-526) (entraînement) et les tests.
- Dépendances : pandas et NumPy ; ``fonctionnalites.construire_jeu_modele``,
  ``masquage`` et ``schema``.

COMMENT
-------
1. ``lire_jeu_brut`` refuse un fichier absent (``FileNotFoundError``, message avec la commande de
   téléchargement), lit le jour demandé et applique la limite.
2. ``lignes_depuis_jeu`` ajoute ``current_time`` (millisecondes depuis 1970, UTC) et produit les
   lignes dans l'ordre du fichier.
3. ``preparer_jeu_entrainement`` valide et masque chaque ligne, décompte les rejets par
   ``champ:code``, assemble par tranches, puis calcule les caractéristiques ; si aucune ligne n'est
   valide, il lève ``ValueError``.
Tests qui le prouvent : test_rejouer_un_jour_sans_transaction_donne_une_liste_vide
(tests/test_producteur.py:397-399), test_jour_rejoue_meme_quand_le_fichier_n_est_pas_trie
(tests/test_producteur.py:437-449), test_un_jeu_sans_ligne_valide_est_refuse_clairement
(tests/test_entrainement.py:316-323),
test_un_jeu_absent_est_refuse_avec_la_commande_de_telechargement
(tests/test_jeu_donnees_garanties.py:123-133) et
test_les_lignes_valides_sont_regroupees_par_tranches_de_la_taille_prevue_sans_tranche_vide
(tests/test_jeu_donnees_garanties.py:401-428).

POURQUOI
--------
- Le jeu d'entraînement passe par le même code que le flux :
  livrables/aia3/02_pipeline_et_qualite.md:7-36.
- Taille du jeu et volumes (555 719 transactions sur 194 jours) :
  livrables/aia3/01_architecture.md:152-159.
- Contrainte de mémoire des conteneurs : commentaires de lire_jeu_brut
  (detection_fraude/jeu_donnees.py:113-140) et lignes_depuis_jeu
  (detection_fraude/jeu_donnees.py:150-161).
- Décalage de sept ans de ``unix_time`` : constat consigné dans ce module (mesure de ce projet,
  fichier brut non publié).
- Indicateurs 2.1 et 4.1 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Ce module ne journalise rien. Les compteurs de la préparation (lignes lues, retenues, rejets) sont
journalisés par l'appelant dans l'événement ``jeu_prepare`` (INFO) du module d'entraînement ; les
rejets d'un rejeu le sont par le producteur (``ligne_rejetee``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from detection_fraude.fonctionnalites import construire_jeu_modele
from detection_fraude.masquage import HacheurCarte, masquer
from detection_fraude.schema import valider_brut

TAILLE_TRANCHE = 20_000

COLONNES_API = (
    "cc_num",
    "merchant",
    "category",
    "amt",
    "first",
    "last",
    "gender",
    "street",
    "city",
    "state",
    "zip",
    "lat",
    "long",
    "city_pop",
    "job",
    "dob",
    "trans_num",
    "merch_lat",
    "merch_long",
    "is_fraud",
)


def lire_jeu_brut(
    chemin: Path, *, jour: str | None = None, limite: int | None = None
) -> pd.DataFrame:
    """Lit le fichier ; ``jour`` (AAAA-MM-JJ) ne garde que les transactions de ce jour."""
    if not chemin.exists():
        raise FileNotFoundError(
            f"Jeu de données introuvable : {chemin}. "
            "Le télécharger avec : python -m detection_fraude.telechargement"
        )
    if not jour:
        return pd.read_csv(chemin, nrows=limite)
    # Deux lectures économes (le conteneur du producteur n'a que 256 Mo) : d'abord la seule colonne
    # des dates, pour repérer les lignes du jour ; puis, si elles sont contiguës (le fichier est
    # trié par date), uniquement ces lignes. Sinon, repli sur une lecture par tranches.
    dates = pd.read_csv(chemin, usecols=["trans_date_trans_time"])["trans_date_trans_time"]
    positions = np.flatnonzero(dates.str.startswith(jour).to_numpy())
    del dates
    if not len(positions):
        df = pd.read_csv(chemin, nrows=0)
    elif positions[-1] - positions[0] + 1 == len(positions):
        df = pd.read_csv(chemin, skiprows=range(1, int(positions[0]) + 1), nrows=len(positions))
    else:
        tranches = (
            t[t["trans_date_trans_time"].str.startswith(jour)]
            for t in pd.read_csv(chemin, chunksize=100_000)
        )
        df = pd.concat([t for t in tranches if len(t)])
    return (df.head(limite) if limite else df).reset_index(drop=True)


def _millisecondes_utc(dates: pd.Series) -> pd.Series:
    """Convertit des dates « AAAA-MM-JJ HH:MM:SS » (lues comme UTC) en millisecondes d'époque."""
    instants = pd.to_datetime(dates, utc=True)
    ecart = instants - pd.Timestamp("1970-01-01", tz="UTC")
    return (ecart // pd.Timedelta(milliseconds=1)).astype("int64")


def lignes_depuis_jeu(df: pd.DataFrame) -> Iterator[dict[str, Any]]:
    """Génère des lignes au format de l'API, dans l'ordre chronologique du fichier."""
    df = df.copy()
    df["current_time"] = _millisecondes_utc(df["trans_date_trans_time"])
    colonnes = [*COLONNES_API, "current_time"]
    # Par tranches : convertir 555 719 lignes en dictionnaires d'un coup coûterait plus de mémoire
    # que le tableau lui-même (le DAG d'entraînement tourne dans un conteneur à mémoire bornée).
    for debut in range(0, len(df), TAILLE_TRANCHE):
        # pandas type les clés de ``to_dict`` en « Hashable » ; ici ce sont des noms de colonnes.
        yield from (  # type: ignore[misc]
            df[colonnes].iloc[debut : debut + TAILLE_TRANCHE].to_dict("records")
        )


def preparer_jeu_entrainement(
    df_brut: pd.DataFrame, hacheur: HacheurCarte
) -> tuple[pd.DataFrame, Counter[str]]:
    """Valide, masque et enrichit tout le jeu avec le code même du flux de production.

    Renvoie le tableau des caractéristiques (avec ``etiquette_source`` comme cible) et le
    décompte des lignes rejetées par code d'erreur.
    """
    rejets: Counter[str] = Counter()
    tranches: list[
        pd.DataFrame
    ] = []  # un tableau compact par tranche, jamais 555 719 dictionnaires
    masques: list[dict[str, Any]] = []
    for ligne in lignes_depuis_jeu(df_brut):
        erreurs = valider_brut(ligne)
        if erreurs:
            for erreur in erreurs:
                rejets[f"{erreur.champ}:{erreur.code}"] += 1
            continue
        masques.append(masquer(ligne, hacheur, source="jeu"))
        if len(masques) >= TAILLE_TRANCHE:
            tranches.append(pd.DataFrame(masques))
            masques = []
    if masques:
        tranches.append(pd.DataFrame(masques))
    if not tranches:
        raise ValueError(f"Aucune ligne valide dans le jeu ({sum(rejets.values())} rejet(s)).")
    return construire_jeu_modele(pd.concat(tranches, ignore_index=True)), rejets
