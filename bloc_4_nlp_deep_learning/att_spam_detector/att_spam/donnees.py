# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Données du détecteur de spam : réparation, normalisation, quasi-doublons, découpage, tenseurs.

CHOIX
-----
- Retenu : réparer `spam.csv` en recollant les trois colonnes « Unnamed » à la colonne du texte
  (ce sont des morceaux de SMS coupés aux virgules), puis retirer les doublons EXACTS avant tout
  découpage (`charger_sms`).
- Retenu : remplacer URL, montants, numéros (au moins 5 chiffres) et nombres par des jetons
  génériques, dans cet ordre (`normaliser`). L'ordre compte : le montant doit passer avant le
  nombre, sinon `£100` deviendrait `£ <nombre>`.
- Retenu : regrouper les quasi-doublons par similarité de Jaccard >= 0,8 sur les ensembles de mots
  normalisés, de proche en proche (composantes connexes), puis découper en 7 plis groupés et
  stratifiés (`StratifiedGroupKFold`) : un pli pour le test, un pour la validation, cinq pour
  l'apprentissage.
- Écarté : un découpage au hasard (`train_test_split`). Une même campagne de spam, envoyée en
  variantes, tomberait des deux côtés et la mesure récompenserait la mémoire.
- Écarté : un test d'égalité stricte entre SMS pour détecter les doublons approchés. Il ne voit pas
  les variantes qui ne diffèrent que par un numéro ou un mot.
- Écarté : la double boucle sur les paires de SMS. La matrice creuse de présence des mots,
  multipliée par sa transposée, donne tous les mots communs d'un seul produit.
- Retenu : un `Encodeur` dont le vocabulaire vient de l'apprentissage seul (mots vus au moins
  deux fois), avec deux indices réservés (0 remplissage, 1 mot inconnu), et une longueur fixe
  au 95e centile des longueurs.
- Retenu : une augmentation à la volée (suppression de mots, permutation de deux voisins) dont le
  hasard est un `random.Random` propre à l'objet `Augmenteur`, donc réinitialisable par graine.

OÙ
---
- Place : premier maillon de la chaîne du notebook `ATT_Spam_Bloc4_CDSD.ipynb`, sections 1 et 2
  (cellules de chargement, de normalisation, de découpage, de vocabulaire et d'augmentation).
- Appelants : le notebook ; `att_spam/reseaux.py` (entraînement) reçoit un `Augmenteur` ;
  `att_spam/evaluation.py` reçoit les plis ; `att_spam/gan.py` reçoit les listes de mots.
- Dépendances : numpy, pandas, scipy (matrices creuses et graphes), scikit-learn
  (`StratifiedGroupKFold`), torch (tenseurs). Pas de réseau, pas d'écriture sur disque.

COMMENT
-------
1. `charger_sms` lit le CSV en latin-1, recolle les colonnes de texte avec une virgule, retire les
   doublons de texte et rend aussi les comptes (lus, coupés, doublons) pour l'affichage.
2. `normaliser` met en minuscules puis applique quatre motifs ; `decouper_mots` garde les jetons
   `<...>` et les mots faits de lettres, de chiffres et d'apostrophes.
3. `matrice_presence` construit la matrice creuse mots x SMS et les tailles des ensembles de
   mots ; `grouper_quasi_doublons` en tire une arête par paire distincte de similarité >= seuil et
   rend les composantes connexes ; `similarites` donne la similarité de Jaccard entre deux
   ensembles de SMS (contrôle du test contre l'apprentissage).
4. `decouper_par_groupes` fait les 7 plis et rend un `Decoupage`.
5. `Encodeur.apprendre` fige vocabulaire et longueur ; `encoder` complète ou tronque ;
   `tenseurs` rend la matrice d'entiers et le vecteur des cibles.
6. `Augmenteur.augmenter` supprime chaque mot avec la probabilité donnée puis échange deux mots
   voisins ; `augmenter_lot` l'applique aux seuls spams du lot.
- Tests qui le prouvent : `tests/test_donnees.py` (valeurs exactes, borne du seuil 0,8, groupes
  transitifs, déterminisme par graine ; entrées refusées : colonne absente, fichier vide, corpus
  vide, moins de trois plis, texte absent ; entrées laissées inchangées) ;
  `tests/test_chiffres_reels.py` (chiffres du vrai `spam.csv` : 5 169 SMS, 4 969 groupes,
  3 691 / 739 / 739) ; `tests/test_notebook.py` (le notebook donne les mêmes sorties).

POURQUOI
--------
- Le notebook reprend l'énoncé : « énonce clairement la performance » sur des SMS jamais vus ; un
  découpage sans quasi-doublon est la condition pour que ce chiffre ne mesure pas la mémoire.
  Source : `att_spam/donnees.py` (fonction `decouper_par_groupes`) et
  `tests/test_donnees.py` (test des groupes partagés).
- Le seuil de Jaccard 0,8 est un choix de conception, non un résultat : le notebook affiche ce
  qui reste en dessous (34 spams de test sur 93 gardent une similarité >= 0,5). Source :
  inconnue pour la valeur 0,8 elle-même ; aucune étude citée.
- Compétence visée : C4.1, transformer des données non structurées en matrices et en tenseurs.
  Source : référentiel de compétences RNCP35288, bloc 4, compétence C4.1.
- Le vocabulaire appris sur l'apprentissage seul évite que des mots du test influencent le modèle ;
  source : inconnue en tant que référence publiée, le principe est vérifié par le test
  `tests/test_donnees.py` sur un mot présent seulement dans le test.

JOURNALISATION
--------------
- Aucune écriture dans le journal `logging` : ce module ne fait que calculer. Aucun `except` : une
  colonne absente ou un jeu vide lève une exception explicite (`ValueError`) que l'appelant voit.
- Aucun `print` ici : les comptes (SMS lus, coupés, doublons, groupes) sont rendus à l'appelant,
  qui les affiche. Le notebook en fait ses sorties visibles.
"""

import collections
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import numpy as np
import numpy.typing as npt
import pandas as pd
import torch
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from sklearn.model_selection import StratifiedGroupKFold  # type: ignore[import-untyped]

#: Indices réservés du vocabulaire des réseaux construits.
PAD = 0
INCONNU = 1

#: Similarité de Jaccard à partir de laquelle deux SMS sont des quasi-doublons.
SEUIL_QUASI_DOUBLON = 0.8

#: Codage de `spam.csv` (le fichier d'origine n'est pas en UTF-8).
ENCODAGE_CSV = "latin-1"

#: Colonnes du texte dans `spam.csv` : la colonne `v2` et les trois morceaux « Unnamed ».
COLONNES_TEXTE = ["v2", "Unnamed: 2", "Unnamed: 3", "Unnamed: 4"]

MOTIF_URL = re.compile(r"https?://\S+|www\.\S+|\b\S+\.(?:com|co\.uk|net|org)\S*", re.IGNORECASE)
MOTIF_ARGENT = re.compile(
    r"[£$€]\s?\d+(?:[.,]\d+)?|\b\d+(?:[.,]\d+)?\s?(?:£|\$|€|pounds?|pence|p\b)", re.IGNORECASE
)
MOTIF_TEL = re.compile(r"\b\d{5,}\b")
MOTIF_NOMBRE = re.compile(r"\b\d+\b")
MOTIF_MOT = re.compile(r"<\w+>|[a-z0-9']+")


@dataclass(frozen=True)
class Chargement:
    """SMS réparés et dédoublonnés, avec les comptes de l'opération."""

    donnees: pd.DataFrame
    nb_lus: int
    nb_coupes: int
    nb_doublons: int


def charger_sms(chemin: Path | str) -> Chargement:
    """Lit `spam.csv`, recolle les SMS coupés aux virgules, retire les doublons exacts."""
    brut = pd.read_csv(chemin, encoding=ENCODAGE_CSV)
    manquantes = [c for c in ["v1", *COLONNES_TEXTE] if c not in brut.columns]
    if manquantes:
        raise ValueError(f"colonne(s) absente(s) de {chemin} : {', '.join(manquantes)}")
    nb_coupes = int(brut[COLONNES_TEXTE[1:]].notna().any(axis=1).sum())
    brut["texte"] = brut[COLONNES_TEXTE].apply(
        lambda ligne: ",".join(str(x) for x in ligne if pd.notna(x)), axis=1
    )
    donnees = brut[["v1", "texte"]].rename(columns={"v1": "etiquette"})
    donnees["cible"] = (donnees["etiquette"] == "spam").astype(int)
    nb_lus = len(donnees)
    nb_doublons = int(donnees.duplicated(subset="texte").sum())
    donnees = donnees.drop_duplicates(subset="texte").reset_index(drop=True)
    return Chargement(donnees, nb_lus, nb_coupes, nb_doublons)


def normaliser(texte: str) -> str:
    """Minuscules, puis jetons génériques (l'ordre compte : le montant avant le nombre)."""
    texte = texte.lower()
    texte = MOTIF_URL.sub(" <url> ", texte)
    texte = MOTIF_ARGENT.sub(" <argent> ", texte)
    texte = MOTIF_TEL.sub(" <tel> ", texte)
    return MOTIF_NOMBRE.sub(" <nombre> ", texte)


def decouper_mots(texte: str) -> list[str]:
    """Tokenisation par mots : les jetons <...> et les mots en lettres et chiffres."""
    return MOTIF_MOT.findall(texte)


@dataclass(frozen=True)
class Presence:
    """Matrice creuse binaire (un SMS par ligne, un mot par colonne) et taille de chaque SMS."""

    matrice: sparse.csr_matrix
    tailles: npt.NDArray[np.float64]


def matrice_presence(mots_par_sms: Sequence[Sequence[str]]) -> Presence:
    """Ensemble de mots de chaque SMS sous forme de matrice creuse binaire."""
    index_mots: dict[str, int] = {}
    lignes_x: list[int] = []
    colonnes_x: list[int] = []
    for position, mots in enumerate(mots_par_sms):
        for mot in set(mots):
            lignes_x.append(position)
            colonnes_x.append(index_mots.setdefault(mot, len(index_mots)))
    matrice = sparse.csr_matrix(
        (np.ones(len(lignes_x)), (lignes_x, colonnes_x)),
        shape=(len(mots_par_sms), len(index_mots)),
    )
    tailles = np.asarray(matrice.sum(axis=1)).ravel()
    return Presence(matrice, tailles)


def grouper_quasi_doublons(
    presence: Presence, seuil: float = SEUIL_QUASI_DOUBLON
) -> tuple[int, npt.NDArray[np.int32]]:
    """Groupes de SMS quasi identiques : composantes connexes du graphe de Jaccard >= seuil.

    Le produit X·Xᵀ donne le nombre de mots communs à chaque paire ; la similarité de Jaccard
    vaut communs / (|A| + |B| - communs), sans double boucle. Si A ressemble à B et B à C, les
    trois vont ensemble.
    """
    communs = (presence.matrice @ presence.matrice.T).tocoo()
    tailles = presence.tailles
    jaccard = communs.data / (tailles[communs.row] + tailles[communs.col] - communs.data)
    aretes = (jaccard >= seuil) & (communs.row != communs.col)
    nb_sms = presence.matrice.shape[0]
    graphe = sparse.csr_matrix(
        (np.ones(aretes.sum()), (communs.row[aretes], communs.col[aretes])),
        shape=(nb_sms, nb_sms),
    )
    nb_groupes, etiquettes = connected_components(graphe)  # graphe symétrique : sens sans objet
    return int(nb_groupes), etiquettes


def similarites(
    presence: Presence, lignes_a: npt.NDArray[np.int64], lignes_b: npt.NDArray[np.int64]
) -> npt.NDArray[np.float64]:
    """Similarité de Jaccard de chaque SMS de `lignes_a` avec chaque SMS de `lignes_b`."""
    communs = (presence.matrice[lignes_a] @ presence.matrice[lignes_b].T).toarray()
    tailles = presence.tailles
    return np.asarray(communs / (tailles[lignes_a][:, None] + tailles[lignes_b][None, :] - communs))


@dataclass(frozen=True)
class Decoupage:
    """Les trois jeux et les sept plis dont ils sont tirés."""

    apprentissage: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    plis: list[npt.NDArray[np.int64]]

    def groupes_partages(self) -> set[int]:
        """Groupes présents à la fois dans l'apprentissage et dans la validation ou le test."""
        hors_apprentissage = set(self.test["groupe"]) | set(self.validation["groupe"])
        return set(self.apprentissage["groupe"]) & hors_apprentissage


def decouper_par_groupes(donnees: pd.DataFrame, graine: int, nb_plis: int = 7) -> Decoupage:
    """Découpage stratifié par groupes : pli 0 = test, pli 1 = validation, le reste = apprentissage.

    `donnees` doit porter les colonnes `cible` et `groupe`. Chaque groupe va tout entier dans un
    seul jeu ; la part de spams est la même dans chaque pli.
    """
    if nb_plis < 3:
        raise ValueError("il faut au moins 3 plis : test, validation et apprentissage")
    decoupeur = StratifiedGroupKFold(n_splits=nb_plis, shuffle=True, random_state=graine)
    plis = [
        indices for _, indices in decoupeur.split(donnees, donnees["cible"], donnees["groupe"])
    ]
    test = donnees.iloc[plis[0]].copy()
    validation = donnees.iloc[plis[1]].copy()
    apprentissage = donnees.drop(index=test.index.union(validation.index)).copy()
    return Decoupage(apprentissage, validation, test, plis)


@dataclass(frozen=True)
class Encodeur:
    """Vocabulaire (appris sur l'apprentissage) et longueur fixe des séquences."""

    vocabulaire: dict[str, int]
    longueur: int

    @classmethod
    def apprendre(
        cls,
        mots_par_sms: Sequence[Sequence[str]],
        frequence_min: int = 2,
        centile: float = 95,
    ) -> Self:
        """Mots vus au moins `frequence_min` fois ; longueur = `centile` des longueurs."""
        if len(mots_par_sms) == 0:
            raise ValueError("impossible d'apprendre un vocabulaire sans aucun SMS")
        compteur = collections.Counter(mot for mots in mots_par_sms for mot in mots)
        vocabulaire = {"<pad>": PAD, "<unk>": INCONNU}
        for mot, frequence in compteur.most_common():
            if frequence >= frequence_min:
                vocabulaire[mot] = len(vocabulaire)
        longueur = int(np.percentile([len(mots) for mots in mots_par_sms], centile))
        return cls(vocabulaire, longueur)

    @property
    def inverse(self) -> dict[int, str]:
        """Indice -> mot."""
        return {indice: mot for mot, indice in self.vocabulaire.items()}

    def encoder(self, mots: Sequence[str]) -> list[int]:
        """Mots -> indices, tronqués ou complétés par <pad> à la longueur fixe."""
        indices = [self.vocabulaire.get(mot, INCONNU) for mot in mots][: self.longueur]
        return indices + [PAD] * (self.longueur - len(indices))

    def tenseurs(self, jeu: pd.DataFrame) -> tuple[torch.Tensor, torch.Tensor]:
        """Matrice des séquences (entiers) et vecteur des cibles (réels 0/1)."""
        sequences = np.stack(jeu["mots"].map(self.encoder).tolist()).astype(np.int64)
        x = torch.from_numpy(sequences)
        y = torch.tensor(jeu["cible"].to_numpy(), dtype=torch.float32)
        return x, y

    def lire(self, sequence: Sequence[int] | torch.Tensor) -> str:
        """Indices -> texte lisible (sans le remplissage)."""
        inverse = self.inverse
        return " ".join(inverse[int(i)] for i in sequence if int(i) != PAD)


class Augmenteur:
    """Augmentation à la volée des spams d'un lot ; le hasard est propre à l'objet."""

    def __init__(self, longueur: int, graine: int) -> None:
        self.longueur = longueur
        self.hasard = random.Random(graine)

    def reinitialiser(self, graine: int) -> None:
        """Remet le hasard dans l'état déterminé par la graine."""
        self.hasard.seed(graine)

    def augmenter(self, sequence: list[int], p_suppression: float = 0.1) -> list[int]:
        """Supprime des mots au hasard puis échange deux mots voisins ; recomplète par <pad>."""
        mots = [i for i in sequence if i != PAD and self.hasard.random() > p_suppression]
        if len(mots) >= 2:
            k = self.hasard.randrange(len(mots) - 1)
            mots[k], mots[k + 1] = mots[k + 1], mots[k]
        return (mots + [PAD] * self.longueur)[: self.longueur]

    def augmenter_lot(
        self, x: torch.Tensor, y: torch.Tensor, probabilite: float = 0.5
    ) -> torch.Tensor:
        """Applique `augmenter` à chaque spam du lot avec la probabilité donnée."""
        x = x.clone()
        for i in torch.nonzero(y == 1).flatten().tolist():
            if self.hasard.random() < probabilite:
                x[i] = torch.tensor(self.augmenter(x[i].tolist()))
        return x
