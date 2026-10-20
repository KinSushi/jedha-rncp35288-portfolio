# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Réseaux construits (TextCNN, BiGRU), perte pondérée et boucle d'apprentissage à arrêt précoce.

CHOIX
-----
- Retenu : l'entropie croisée binaire pondérée (`BCEWithLogitsLoss(pos_weight=w)`) avec
  w = légitimes / spams (environ 6,9). `BCEWithLogitsLoss` part du score brut z, ce qui est
  numériquement stable ; le poids fait compter un spam raté autant que w légitimes mal classés.
- Retenu : deux familles de réseaux, le TextCNN (filtres de 3, 4 et 5 mots, max-pooling) et le
  BiGRU (lecture dans les deux sens, max-pooling sur le temps), pour comparer un détecteur
  d'expressions à un lecteur de contexte.
- Retenu : une boucle d'apprentissage écrite à la main (`entrainer`) : perte et F1 sur
  l'apprentissage et la validation à chaque époque, arrêt après `patience` époques sans progrès
  du F1 de validation, retour à l'état de la meilleure époque.
- Écarté : un entraînement sans pondération comparé au pondéré. Le notebook n'en mesure pas
  l'apport ; la pondération est un choix de conception, pas un gain démontré.
- Écarté : `state_dict()` sans copie. Il rend des références qui continuent d'évoluer avec
  l'entraînement ; l'état est donc copié (`copy.deepcopy`).
- Écarté : un `print` dans les modèles. Seule `entrainer` écrit, et seulement si `bavard` est vrai.

OÙ
---
- Place : sections 4.1 et 4.2 du notebook `ATT_Spam_Bloc4_CDSD.ipynb` (perte, TextCNN, BiGRU,
  entraînement) ; `entrainer` sert aussi à l'étude des cinq graines (section 6.3).
- Appelants : le notebook. `att_spam/gan.py` ne l'appelle pas ; `att_spam/evaluation.py` reçoit
  les probabilités calculées par `probabilites`.
- Dépendances : torch, numpy, scikit-learn (`f1_score`) et `att_spam/donnees.py` (`Augmenteur`).
  Aucun accès disque ni réseau.

COMMENT
-------
1. `poids_spam(y)` compte légitimes et spams ; `creer_perte(y)` en fait la perte pondérée.
2. `entrainer(modele, x, y, x_val, y_val, perte, augmenteur, graine=...)` fixe la graine de
   torch et de l'augmenteur, crée un Adam (taux 1e-3) et un `DataLoader` de lots de 64 mélangés
   par un générateur propre à la graine. À chaque époque : lots (avec augmentation des spams si
   demandée), puis mesures en mode évaluation, puis comparaison au meilleur F1 de validation.
3. `ligne_epoque` met en forme la ligne affichée par époque.
4. `probabilites` passe le modèle en mode évaluation (dropout coupé) et rend sigmoïde(score).
- Tests qui le prouvent : `tests/test_reseaux.py` (poids exact, structure, dropout coupé en
  évaluation, arrêt précoce et retour au meilleur état sur un modèle scripté, déterminisme par
  graine ; entrées refusées : zéro époque, zéro spam, indice hors du vocabulaire, cibles de
  longueur différente) ; `tests/test_notebook.py` (courbes du notebook).

POURQUOI
--------
- La perte pondérée et le choix de deux familles viennent de la compétence C4.2 (réseaux
  adaptés, convolutifs ou récursifs) du bloc 4. Source : référentiel de compétences
  RNCP35288, bloc 4.
- La copie du meilleur état a une raison de code vérifiable : `tests/test_reseaux.py` contient un
  test qui échoue si l'état n'est pas copié.
- La valeur du dropout (0,5), des largeurs de filtres (3, 4, 5) et du taux d'apprentissage
  (1e-3) sont des choix usuels non justifiés par une étude du projet : source : inconnue.

JOURNALISATION
--------------
- Aucune écriture dans `logging`. `entrainer` affiche sur la sortie standard, avec `print`, une
  ligne par époque (perte et F1 d'apprentissage et de validation) et une ligne d'arrêt précoce,
  seulement si `bavard` est vrai : c'est l'affichage voulu du notebook, testé par capture.
- Aucune exception n'est interceptée : zéro époque ou une base sans spam lève `ValueError`.
"""

import copy
import math
from typing import Any

import numpy as np
import numpy.typing as npt
import torch
from sklearn.metrics import f1_score  # type: ignore[import-untyped]
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from att_spam.donnees import PAD, Augmenteur

#: Courbes d'apprentissage : perte et F1, sur l'apprentissage et sur la validation.
type Historique = dict[str, list[float]]


def poids_spam(y: torch.Tensor) -> float:
    """Poids de la classe spam : nombre de légitimes divisé par nombre de spams."""
    nb_spams = (y == 1).sum()
    if nb_spams == 0:
        raise ValueError("aucun spam dans les cibles : le poids de la classe spam est indéfini")
    return float((y == 0).sum() / nb_spams)


def creer_perte(y: torch.Tensor) -> nn.BCEWithLogitsLoss:
    """Entropie croisée binaire dont le terme des spams est multiplié par `poids_spam(y)`."""
    return nn.BCEWithLogitsLoss(pos_weight=torch.tensor([poids_spam(y)]))


class TextCNN(nn.Module):
    """Plongement des mots, convolutions 1D de largeurs 3, 4, 5, max-pooling, couche linéaire."""

    def __init__(self, taille_vocab: int, dim: int = 100, filtres: int = 64) -> None:
        super().__init__()
        self.plongement = nn.Embedding(taille_vocab, dim, padding_idx=PAD)
        self.convolutions = nn.ModuleList(nn.Conv1d(dim, filtres, k) for k in (3, 4, 5))
        self.abandon = nn.Dropout(0.5)  # régularisation contre le sur-apprentissage
        self.sortie = nn.Linear(3 * filtres, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.plongement(x).transpose(1, 2)  # (lot, dim, longueur) : format de Conv1d
        h = torch.cat([torch.relu(c(h)).amax(dim=2) for c in self.convolutions], dim=1)
        return torch.squeeze(self.sortie(self.abandon(h)), dim=1)


class BiGRU(nn.Module):
    """Plongement, GRU bidirectionnel, max-pooling sur le temps, couche linéaire."""

    def __init__(self, taille_vocab: int, dim: int = 100, cache: int = 64) -> None:
        super().__init__()
        self.plongement = nn.Embedding(taille_vocab, dim, padding_idx=PAD)
        self.gru = nn.GRU(dim, cache, batch_first=True, bidirectional=True)
        self.abandon = nn.Dropout(0.5)
        self.sortie = nn.Linear(2 * cache, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h, _ = self.gru(self.plongement(x))
        return torch.squeeze(self.sortie(self.abandon(h.amax(dim=1))), dim=1)


def probabilites(modele: nn.Module, x: torch.Tensor) -> npt.NDArray[np.float32]:
    """Probabilités de spam prédites (mode évaluation : dropout désactivé)."""
    modele.eval()
    with torch.no_grad():
        return np.asarray(torch.sigmoid(modele(x)).numpy(), dtype=np.float32)


def ligne_epoque(nom: str, epoque: int, historique: Historique, largeur: int = 2) -> str:
    """Ligne affichée à la fin d'une époque, d'après la dernière valeur de chaque courbe."""
    return (
        f"{nom} époque {epoque:{largeur}d} | perte app {historique['perte_app'][-1]:.3f} "
        f"val {historique['perte_val'][-1]:.3f} | F1 app {historique['f1_app'][-1]:.3f} "
        f"val {historique['f1_val'][-1]:.3f}"
    )


def entrainer(
    modele: nn.Module,
    x: torch.Tensor,
    y: torch.Tensor,
    x_val: torch.Tensor,
    y_val: torch.Tensor,
    perte: nn.Module,
    augmenteur: Augmenteur,
    *,
    graine: int,
    epoques: int = 15,
    patience: int = 3,
    augmentation: bool = True,
    nom: str = "",
    bavard: bool = True,
) -> Historique:
    """Boucle d'apprentissage écrite à la main, avec arrêt précoce sur le F1 de validation.

    À chaque époque : perte et F1 sur l'apprentissage et la validation. Le meilleur état est
    COPIÉ (copy.deepcopy) : state_dict() renvoie des références qui continueraient d'évoluer avec
    l'entraînement. La graine fixe l'ordre des lots et les tirages de l'augmentation : deux appels
    avec la même graine donnent le même résultat.
    """
    if epoques < 1:
        raise ValueError("il faut au moins une époque pour retenir un meilleur état")
    torch.manual_seed(graine)
    augmenteur.reinitialiser(graine)
    optimiseur = torch.optim.Adam(modele.parameters(), lr=1e-3)
    chargeur = DataLoader(
        TensorDataset(x, y),
        batch_size=64,
        shuffle=True,
        generator=torch.Generator().manual_seed(graine),
    )
    historique: Historique = {"perte_app": [], "perte_val": [], "f1_app": [], "f1_val": []}
    meilleur_f1 = -math.inf
    attente: int  # remis à 0 dès la première époque, qui est toujours la meilleure
    meilleur_etat: dict[str, Any]  # affecté à la première époque, qui est toujours la meilleure
    for epoque in range(1, epoques + 1):
        modele.train()
        for lot_x, lot_y in chargeur:
            if augmentation:
                lot_x = augmenteur.augmenter_lot(lot_x, lot_y)
            optimiseur.zero_grad()
            perte(modele(lot_x), lot_y).backward()
            optimiseur.step()
        modele.eval()
        with torch.no_grad():
            z_app, z_val = modele(x), modele(x_val)
        historique["perte_app"].append(perte(z_app, y).item())
        historique["perte_val"].append(perte(z_val, y_val).item())
        historique["f1_app"].append(f1_score(y.numpy(), (z_app > 0).numpy()))
        historique["f1_val"].append(f1_score(y_val.numpy(), (z_val > 0).numpy()))
        if bavard:
            print(ligne_epoque(nom, epoque, historique))
        if historique["f1_val"][-1] > meilleur_f1:
            meilleur_f1, attente = historique["f1_val"][-1], 0
            meilleur_etat = copy.deepcopy(modele.state_dict())
        else:
            attente += 1
            if attente >= patience:
                if bavard:
                    print(f"{nom} arrêt précoce : pas de progrès depuis {patience} époques")
                break
    modele.load_state_dict(meilleur_etat)
    return historique
