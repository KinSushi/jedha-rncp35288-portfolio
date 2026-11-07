# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Logique métier de l'analyse des retards Getaround, partagée par le notebook et le tableau de
bord (une seule définition des indicateurs : ils ne peuvent pas diverger d'un support à l'autre).

La fonctionnalité étudiée impose un **délai minimal entre deux locations d'une même voiture** :
une voiture n'apparaît plus dans les résultats de recherche si le début demandé est trop proche
de la fin d'une location déjà réservée.

Vocabulaire et définitions :
- ``time_delta_with_previous_rental_in_minutes`` (« écart prévu ») : minutes entre la fin prévue
  de la location précédente et le début prévu de la suivante. Getaround ne le renseigne que si
  la précédente s'achève moins de 12 h avant (sinon, aucun risque de conflit).
- **retard résiduel** = retard de la location précédente - écart prévu. S'il est positif, le
  conducteur suivant **attend** la voiture : c'est un **cas problématique**.
- avec un seuil S et un périmètre P, une location est **bloquée** si son écart prévu est
  inférieur à S et que la voiture est dans P (toutes, ou seulement « Connect ») ;
- un cas problématique est **résolu** si la location concernée aurait été bloquée ;
- la **part des locations terminées bloquées** est l'indicateur de substitution du revenu :
  les données ne contiennent aucun prix par location, on ne peut donc pas calculer un revenu.
  On ne retient que les locations terminées (une location annulée ne rapporte rien).

CHOIX
-----
- Retenu : un seul module de calcul, importé par le notebook et par le tableau de bord, qui ne
  dépend que de pandas. Les parts et les cas résolus sont des fonctions pures d'un tableau de
  locations : le même appel donne le même chiffre sur les deux supports.
- Écarté : recopier les formules dans le notebook et dans le tableau de bord. Deux copies
  finissent par diverger, et le jury verrait deux chiffres pour une même question.
- Écarté : un revenu estimé par location. Le classeur de locations n'a aucun prix
  (Getaround_Analyse_Retards.ipynb, section 5) ; l'indicateur est donc une part de
  locations, jamais une part de revenu.

OÙ
---
- Racine du dépôt, à côté du notebook. Appelants : le notebook Getaround_Analyse_Retards.ipynb et
  dashboard/app.py (copié dans l'image Docker du tableau de bord par dashboard/Dockerfile).
- Dépendance : pandas seulement (requirements-dashboard.txt).
- Données : l'onglet ``rentals_data`` de data/get_around_delay_analysis.xlsx.

COMMENT
-------
1. ``preparer`` joint le tableau à lui-même par l'identifiant de la location précédente, ajoute
   le retard de la précédente, le retard résiduel et le cas problématique (retard résiduel
   strictement positif).
2. ``bloquees`` compose trois conditions : écart prévu connu, strictement inférieur au seuil,
   voiture dans le périmètre (``dans_perimetre``, qui refuse un périmètre inconnu par une
   ``ValueError``).
3. ``locations_touchees`` et ``cas_resolus`` comptent ces masques ; ``tableau_seuils`` les
   rassemble pour chaque seuil et chaque périmètre, avec les parts en pourcentage.
4. ``tranche_de_retard`` classe un retard en minutes dans les six tranches communes au notebook
   et au tableau de bord.

POURQUOI
--------
- Comparaison stricte du seuil : un écart égal au seuil respecte le délai minimal.
- L'indicateur porte sur les locations terminées : une location annulée ne rapporte rien. Chiffres
  de référence : 358 sur 18 045, soit 1,98 %.
- L'énoncé du projet pose des questions sur les parts de revenu, de locations touchées et de cas
  résolus.

JOURNALISATION
--------------
Aucune : ce sont des fonctions pures de calcul, sans entrée-sortie. Un périmètre inconnu est
signalé par une exception ``ValueError`` dont le message cite le périmètre reçu et les périmètres
admis ; l'appelant (notebook, tableau de bord) décide de l'afficher.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pandas as pd

PERIMETRES = {"toutes": "toutes les voitures", "connect": "voitures Connect seulement"}

# Tranches de retard au check-out, communes au notebook et au tableau de bord : une seule
# définition, pour que les deux figures montrent les mêmes classes.
BORNES_TRANCHES_RETARD = [-math.inf, 0, 15, 60, 120, 1440, math.inf]
LIBELLES_TRANCHES_RETARD = [
    "à l'heure ou en avance",
    "0-15 min",
    "15-60 min",
    "1-2 h",
    "2-24 h",
    "plus de 24 h",
]


def preparer(locations: pd.DataFrame) -> pd.DataFrame:
    """Ajoute à chaque location le retard de la précédente, le retard résiduel et le cas
    problématique (jointure de la table sur elle-même par l'identifiant de location).

    Le cas est problématique si le retard résiduel est strictement positif : toute attente compte.
    """
    precedentes = locations[["rental_id", "delay_at_checkout_in_minutes"]].rename(
        columns={
            "rental_id": "previous_ended_rental_id",
            "delay_at_checkout_in_minutes": "retard_precedent",
        }
    )
    df = locations.merge(precedentes, on="previous_ended_rental_id", how="left")
    df["retard_residuel"] = (
        df["retard_precedent"] - df["time_delta_with_previous_rental_in_minutes"]
    )
    df["cas_problematique"] = df["retard_residuel"] > 0
    return df


def dans_perimetre(df: pd.DataFrame, perimetre: str) -> pd.Series[bool]:
    """Masque des locations concernées par le périmètre choisi."""
    if perimetre == "connect":
        return df["checkin_type"] == "connect"
    if perimetre == "toutes":
        return pd.Series(True, index=df.index)
    raise ValueError(f"périmètre inconnu : {perimetre} (attendu : {list(PERIMETRES)})")


def bloquees(df: pd.DataFrame, seuil: float, perimetre: str) -> pd.Series[bool]:
    """Masque des locations qui n'auraient pas pu être réservées avec ce seuil et ce périmètre.

    L'écart prévu doit être connu (location précédente à moins de 12 h) et STRICTEMENT inférieur
    au seuil : un écart égal au seuil respecte le délai minimal.
    """
    ecart = df["time_delta_with_previous_rental_in_minutes"]
    return ecart.notna() & (ecart < seuil) & dans_perimetre(df, perimetre)


def locations_touchees(df: pd.DataFrame, seuil: float, perimetre: str) -> int:
    """Nombre de locations bloquées par la fonctionnalité."""
    return int(bloquees(df, seuil, perimetre).sum())


def cas_resolus(df: pd.DataFrame, seuil: float, perimetre: str) -> int:
    """Nombre de cas problématiques (le conducteur suivant attend) évités par la fonctionnalité."""
    return int((bloquees(df, seuil, perimetre) & df["cas_problematique"]).sum())


def tranche_de_retard(retard_min: pd.Series[float]) -> pd.Series[str]:
    """Tranche de chaque retard au check-out, d'après ``LIBELLES_TRANCHES_RETARD``.

    Les bornes sont ouvertes à gauche et fermées à droite (``pd.cut``) : un retard de 0 minute est
    « à l'heure », 15 minutes pile reste dans « 0-15 min ». Un retard manquant reste manquant.
    """
    return pd.cut(retard_min, bins=BORNES_TRANCHES_RETARD, labels=LIBELLES_TRANCHES_RETARD)


def tableau_seuils(
    df: pd.DataFrame,
    seuils: Sequence[float],
    perimetres: Sequence[str] = ("toutes", "connect"),
) -> pd.DataFrame:
    """Pour chaque seuil et périmètre : locations bloquées et cas résolus, en nombre et en part.

    Les parts sont calculées sur toutes les locations (pour les bloquées) et sur tous les cas
    problématiques (pour les résolus) : c'est l'arbitrage que doit trancher le chef de produit.

    Deux colonnes supplémentaires donnent l'indicateur de substitution du revenu : le nombre de
    locations TERMINÉES bloquées et leur part parmi toutes les locations terminées. Une location
    annulée ne rapporte aucun revenu : la garder au dénominateur (et au numérateur) fausserait
    l'indicateur. Ce n'est pas un revenu : les données n'ont aucun prix par location.

    Un tableau sans location, sans cas problématique ou sans location terminée donne des parts
    nulles au lieu d'une division par zéro (``max(…, 1)`` au dénominateur).
    """
    total_locations = len(df)
    total_problemes = int(df["cas_problematique"].sum())
    terminees = df["state"] == "ended"
    total_terminees = int(terminees.sum())
    lignes = []
    for perimetre in perimetres:
        for seuil in seuils:
            touchees = locations_touchees(df, seuil, perimetre)
            resolus = cas_resolus(df, seuil, perimetre)
            terminees_touchees = int((bloquees(df, seuil, perimetre) & terminees).sum())
            lignes.append(
                {
                    "perimetre": perimetre,
                    "seuil_min": seuil,
                    "locations_bloquees": touchees,
                    "part_locations_pct": round(100 * touchees / max(total_locations, 1), 2),
                    "cas_resolus": resolus,
                    "part_cas_resolus_pct": round(100 * resolus / max(total_problemes, 1), 1),
                    "locations_terminees_bloquees": terminees_touchees,
                    "part_terminees_pct": round(
                        100 * terminees_touchees / max(total_terminees, 1), 2
                    ),
                }
            )
    return pd.DataFrame(lignes)
