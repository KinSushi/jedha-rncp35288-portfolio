# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Caractéristiques du modèle, identiques à l'entraînement et au scoring — critères 2.1 et 2.2.

Le défaut classique d'un projet d'apprentissage en production est l'écart « entraînement /
service » : les caractéristiques calculées en lot pour entraîner ne sont pas celles calculées au
fil de l'eau pour noter. Ici, une seule définition mathématique est implémentée deux fois
(version vectorisée pour le lot, version incrémentale pour le flux) et un test vérifie qu'elles
donnent le même résultat.

Caractéristiques dérivées (en plus des champs masqués) :

* ``heure``, ``jour_semaine`` : composantes de l'horodatage UTC ;
* ``nb_tx_24h``       : nombre de transactions de la même carte dans les 24 h *strictement*
  précédentes (agrégation glissante, signal de rafale) ;
* ``montant_relatif`` : montant divisé par le montant moyen des transactions de la même carte sur
  les 30 jours précédents (1 si la carte est inconnue, plafonné à 100).

CHOIX
-----
- Retenu : deux implémentations d'une même définition, une incrémentale (``EtatCartes``,
  ``HistoriqueCarte``) pour le flux et une vectorisée (``ajouter_historique_cartes``) pour le lot,
  plutôt qu'une seule : le flux ne peut pas relire tout l'historique à chaque message, le lot ne
  peut pas rejouer un message à la fois.
- Retenu : des fenêtres strictement antérieures (ni le futur, ni la transaction elle-même, ni ses
  jumelles de la même seconde) : aucune fuite d'information.
- Retenu : un plafond de 100 sur le montant relatif et la valeur neutre 1 pour une carte inconnue.
- Retenu : un historique élagué au-delà de deux fenêtres de 30 jours pour borner la mémoire, et
  reconstruit à la demande depuis le bronze au redémarrage.
- Écarté : calculer les agrégats avec le futur de la série, qui avantagerait artificiellement le
  modèle à l'entraînement.

OÙ
---
- Place : entre le bronze et le modèle ; mêmes colonnes à l'entraînement et au scoring.
- Appelants : enrichir (detection_fraude/consommateur.py:167-196), retraiter
  (detection_fraude/retraitement.py:94-198) (version incrémentale), preparer_jeu_entrainement
  (detection_fraude/jeu_donnees.py:164-191) (version vectorisée), entrainer
  (detection_fraude/entrainement.py:351-526) (liste des colonnes).
- Dépendances : NumPy et pandas ; aucune dépendance interne.

COMMENT
-------
1. ``HistoriqueCarte`` garde les instants triés et les sommes cumulées ; ``statistiques(t)`` trouve
   par recherche dichotomique les bornes des fenêtres de 24 h et de 30 jours (bornes incluses à
   gauche, exclues à droite) et rend (nombre sur 24 h, somme sur 30 jours, nombre sur 30 jours).
2. Une arrivée tardive est insérée à sa place et les cumuls sont recalculés.
3. ``ajouter_historique_cartes`` fait le même calcul carte par carte avec ``searchsorted``.
Tests qui le prouvent : test_lot_et_flux_donnent_les_memes_caracteristiques
(tests/test_fonctionnalites.py:42-52),
test_une_transaction_ne_se_compte_pas_elle_meme_ni_ses_jumelles
(tests/test_fonctionnalites.py:55-66),
test_fenetre_de_24_heures_bornes_incluse_a_gauche_exclue_a_droite
(tests/test_fonctionnalites.py:74-81), test_arrivee_tardive_inseree_a_sa_place
(tests/test_fonctionnalites.py:99-105),
test_la_version_vectorisee_respecte_les_memes_bornes_exactes
(tests/test_fonctionnalites_bornes.py:325-341) et
test_l_historique_garde_les_transactions_et_les_cumuls_dans_l_ordre_du_temps
(tests/test_fonctionnalites_bornes.py:191-224).

POURQUOI
--------
- Feature engineering, deux versions de l'agrégation et test d'égalité :
  livrables/aia3/02_pipeline_et_qualite.md:38-107.
- Colonnes d'origine et lignage de chaque caractéristique :
  livrables/aia3/02_pipeline_et_qualite.md:289-330.
- Constantes de fenêtre et plafond : detection_fraude/fonctionnalites.py:85.
- Indicateurs 2.1 et 2.2 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Ce module ne journalise rien : ses fonctions sont des calculs purs, appelés dans le chemin chaud
du consommateur. Les compteurs de lot (``recus``, ``nouveaux``...) sont journalisés par le
consommateur dans l'événement ``lot_traite``.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import pandas as pd

FENETRE_COMPTE_S = 24 * 3600
FENETRE_MOYENNE_S = 30 * 24 * 3600
PLAFOND_MONTANT_RELATIF = 100.0
PREFIXE_COMMERCANT = "fraud_"  # artefact du jeu source : tous les noms de commerçants l'ont

COLONNES_NUMERIQUES = (
    "montant",
    "age",
    "distance_km",
    "population_ville",
    "heure",
    "jour_semaine",
    "nb_tx_24h",
    "montant_relatif",
)
COLONNES_CATEGORIELLES = ("categorie", "etat")
COLONNES_MODELE = COLONNES_NUMERIQUES + COLONNES_CATEGORIELLES


def normaliser_commercant(nom: str) -> str:
    """Nettoyage de la couche argent : retire le préfixe parasite et les espaces de bord."""
    nom = nom.strip()
    return nom.removeprefix(PREFIXE_COMMERCANT).strip()


def derivees_temporelles(horodatage_s: int) -> tuple[str, str, int, int]:
    """(date ISO, horodatage ISO, heure, jour de semaine) en UTC pour un horodatage en secondes."""
    instant = datetime.fromtimestamp(int(horodatage_s), UTC)
    return (
        instant.date().isoformat(),
        instant.isoformat(timespec="seconds"),
        instant.hour,
        instant.weekday(),
    )


def _montant_relatif(montant: float, somme: float, nombre: int) -> float:
    if nombre == 0 or somme <= 0:
        return 1.0
    return min(montant / (somme / nombre), PLAFOND_MONTANT_RELATIF)


# --------------------------------------------------------------------------------------------
# Version incrémentale (flux)
# --------------------------------------------------------------------------------------------
class HistoriqueCarte:
    """Transactions récentes d'une carte, triées par temps, avec sommes cumulées."""

    def __init__(self) -> None:
        self.temps: list[int] = []
        self.cumul: list[float] = []  # cumul[i] = somme des montants de 0 à i inclus

    def ajouter(self, t: int, montant: float) -> None:
        if not self.temps or t >= self.temps[-1]:
            self.temps.append(t)  # cas courant : arrivée dans l'ordre, coût constant
            self.cumul.append((self.cumul[-1] if self.cumul else 0.0) + montant)
        else:  # arrivée tardive : on insère à sa place et on recalcule les cumuls suivants
            position = bisect_right(self.temps, t)
            self.temps.insert(position, t)
            montants = self._montants()
            montants.insert(position, montant)
            self.cumul = list(np.cumsum(montants))
        self._elaguer(t)

    def _montants(self) -> list[float]:
        precedent = 0.0
        resultat = []
        for c in self.cumul:
            resultat.append(c - precedent)
            precedent = c
        return resultat

    def _elaguer(self, maintenant: int) -> None:
        """Oublie ce qui sort de la fenêtre de 30 jours (avec marge) pour borner la mémoire."""
        limite = maintenant - 2 * FENETRE_MOYENNE_S
        if self.temps and self.temps[0] < limite:
            k = bisect_left(self.temps, limite)
            base = self.cumul[k - 1]
            self.temps = self.temps[k:]
            self.cumul = [c - base for c in self.cumul[k:]]

    def statistiques(self, t: int) -> tuple[int, float, int]:
        """(nb sur 24 h, somme sur 30 j, nb sur 30 j) pour les transactions antérieures à ``t``."""
        fin = bisect_left(self.temps, t)
        debut_24h = bisect_left(self.temps, t - FENETRE_COMPTE_S, 0, fin)
        debut_30j = bisect_left(self.temps, t - FENETRE_MOYENNE_S, 0, fin)
        somme = 0.0
        if fin > debut_30j:
            somme = self.cumul[fin - 1] - (self.cumul[debut_30j - 1] if debut_30j > 0 else 0.0)
        return fin - debut_24h, somme, fin - debut_30j


class EtatCartes:
    """État du flux : l'historique récent de chaque carte, chargé à la première apparition.

    ``chargeur(carte_hash, avant_s, depuis_s)`` relit l'historique dans la couche bronze : c'est
    ce qui permet à un consommateur redémarré de retrouver exactement son état.
    """

    def __init__(
        self, chargeur: Callable[[str, int, int], list[tuple[int, float]]] | None = None
    ) -> None:
        self._chargeur = chargeur
        self._cartes: dict[str, HistoriqueCarte] = {}

    def _historique(self, carte_hash: str, t: int) -> HistoriqueCarte:
        historique = self._cartes.get(carte_hash)
        if historique is None:
            historique = HistoriqueCarte()
            if self._chargeur is not None:
                for temps, montant in self._chargeur(carte_hash, t, t - FENETRE_MOYENNE_S):
                    historique.ajouter(temps, montant)
            self._cartes[carte_hash] = historique
        return historique

    def caracteristiques(self, carte_hash: str, t: int, montant: float) -> tuple[int, float]:
        """(``nb_tx_24h``, ``montant_relatif``) de la transaction, sans la modifier."""
        nb_24h, somme, nombre = self._historique(carte_hash, t).statistiques(t)
        return nb_24h, _montant_relatif(montant, somme, nombre)

    def enregistrer(self, carte_hash: str, t: int, montant: float) -> None:
        self._historique(carte_hash, t).ajouter(t, montant)

    def __len__(self) -> int:
        return len(self._cartes)


# --------------------------------------------------------------------------------------------
# Version vectorisée (lot : entraînement, retraitement)
# --------------------------------------------------------------------------------------------
def ajouter_caracteristiques_temporelles(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute ``heure`` et ``jour_semaine`` (UTC) à un tableau de messages masqués."""
    instants = pd.to_datetime(df["horodatage_s"].astype("int64"), unit="s", utc=True)
    resultat = df.copy()
    resultat["heure"] = instants.dt.hour.astype("int64")
    resultat["jour_semaine"] = instants.dt.dayofweek.astype("int64")
    return resultat


def ajouter_historique_cartes(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute ``nb_tx_24h`` et ``montant_relatif`` à un tableau de messages masqués.

    Même arithmétique que ``HistoriqueCarte.statistiques`` : recherche dichotomique des bornes
    de fenêtre et différences de sommes cumulées, carte par carte.
    """
    resultat = df.copy()
    nb_24h = np.zeros(len(resultat), dtype="int64")
    relatif = np.ones(len(resultat), dtype="float64")
    temps_tous = resultat["horodatage_s"].to_numpy(dtype="int64")
    montants_tous = resultat["montant"].to_numpy(dtype="float64")
    for positions in resultat.groupby("carte_hash", sort=False).indices.values():
        # Tri stable par temps : l'ordre des égalités n'influe pas, car seules comptent les
        # transactions *strictement* antérieures.
        ordre = positions[np.argsort(temps_tous[positions], kind="stable")]
        t = temps_tous[ordre]
        m = montants_tous[ordre]
        cumul = np.concatenate(([0.0], np.cumsum(m)))
        fin = np.searchsorted(t, t, side="left")  # nombre de transactions strictement antérieures
        debut_24h = np.searchsorted(t, t - FENETRE_COMPTE_S, side="left")
        debut_30j = np.searchsorted(t, t - FENETRE_MOYENNE_S, side="left")
        nb_24h[ordre] = fin - debut_24h
        nombre = fin - debut_30j
        somme = cumul[fin] - cumul[debut_30j]
        moyenne = np.divide(somme, nombre, out=np.zeros_like(somme), where=nombre > 0)
        rapport = np.divide(m, moyenne, out=np.ones_like(m), where=moyenne > 0)
        relatif[ordre] = np.minimum(rapport, PLAFOND_MONTANT_RELATIF)
    resultat["nb_tx_24h"] = nb_24h
    resultat["montant_relatif"] = relatif
    return resultat


def construire_jeu_modele(messages_masques: pd.DataFrame) -> pd.DataFrame:
    """Messages masqués -> tableau complet des caractéristiques du modèle."""
    return ajouter_historique_cartes(ajouter_caracteristiques_temporelles(messages_masques))
