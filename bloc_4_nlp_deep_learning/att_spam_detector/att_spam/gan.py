# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""GAN textuel : générateur LSTM, discriminateur convolutif, entraînement adverse, qualité.

CHOIX
-----
- Retenu : un générateur récurrent (LSTM) dont l'état initial vient d'un bruit z, et un
  discriminateur convolutif (comme le TextCNN) qui lit des suites de vecteurs one-hot.
- Retenu : Gumbel-softmax en version « straight-through » (`hard=True`) : en avant le générateur
  émet un vrai mot (one-hot), en arrière le gradient passe par la distribution adoucie, dont la
  température descend de 1 à 0,3. Des mots « durs » empêchent le discriminateur de reconnaître un
  faux à son format (distribution étalée contre vecteur exact).
- Écarté : le gradient de politique (SeqGAN). Le gradient est estimé par échantillonnage, très
  bruité et lent à converger sur processeur.
- Retenu : un pré-entraînement du générateur par maximum de vraisemblance (40 époques) avant la
  phase adverse, et un terme de vraisemblance (poids 1) gardé pendant la phase adverse. Les GAN
  textuels purs sont très instables ; ce terme empêche le générateur de dégénérer.
- Retenu : masquer par du remplissage tout ce qui suit le premier `<fin>` d'une phrase fabriquée
  (`masquer_apres_fin`). Sans cela le discriminateur reconnaîtrait un faux à sa queue de séquence,
  indice de forme sans rapport avec le contenu.
- Retenu : mesurer la qualité contre trois références (vrais spams de validation, les mêmes à mots
  mélangés, mots tirés indépendamment) plutôt que dans l'absolu : une mesure n'a de sens que
  comparée.

OÙ
---
- Place : section 6 du notebook `ATT_Spam_Bloc4_CDSD.ipynb` (C4.4) : vocabulaire du GAN,
  pré-entraînement, entraînement adverse, génération, mesures de qualité.
- Appelants : le notebook. Les SMS générés servent ensuite à l'étude d'utilité sur cinq graines
  (section 6.3) avec `att_spam/reseaux.py`. Dépendances : torch, numpy. Aucun accès disque.

COMMENT
-------
1. `VocabulaireGAN.apprendre` retient les mots vus au moins deux fois dans les spams
   d'apprentissage, plus `<pad>`, `<debut>`, `<fin>` ; `encoder` termine chaque spam par `<fin>`.
2. `Generateur.vraisemblance` calcule la perte « mot suivant » avec teacher forcing ;
   `echantillonner` produit des phrases (n, longueur, vocabulaire) en one-hot dérivables.
3. `pre_entrainer` fait les époques de vraisemblance, avec coupure du gradient à la norme 5.
4. `entrainer_adverse` alterne un pas du discriminateur (vrais contre faux masqués) et un pas du
   générateur (faire classer ses faux comme vrais, plus la vraisemblance), et journalise les
   pertes et l'exactitude du discriminateur.
5. `forme_phrases` mesure, avant masquage, la part de phrases avec `<fin>` et la part des positions
   après `<fin>` occupées par un mot. `generer` rend les phrases en mots.
6. `distinct`, `quadrigrammes`, `mesurer` calculent diversité, structure locale, copies exactes et
   part classée spam.
- Tests qui le prouvent : `tests/test_gan.py` (masquage exact sur des phrases écrites à la main,
  formes, one-hot, vocabulaire, température de 1 à 0,3, déterminisme par graine, valeurs exactes
  des mesures) ; `tests/test_notebook.py` (chiffres de la section 6 du notebook).

POURQUOI
--------
- La compétence C4.4 demande de créer des données non structurées avec des réseaux adverses et
  d'en juger la qualité. Source : référentiel de compétences RNCP35288, bloc 4.
- Le résultat est honnête : ni qualité grammaticale, ni gain pour le détecteur. Le notebook le
  mesure (cellules de qualité et des cinq graines) ; le code ne le promet pas.
- Les tailles (bruit 32, plongement 64, état 128), le taux 1e-4 de la phase adverse et les 300
  itérations sont des choix de conception sans étude de sensibilité : source : inconnue.

JOURNALISATION
--------------
- Aucune écriture dans `logging`. `pre_entrainer` affiche avec `print` la perte toutes les 10
  époques et `entrainer_adverse` une ligne toutes les 50 itérations, si `bavard` est vrai : c'est
  l'affichage voulu du notebook, testé par capture de la sortie.
- Les courbes détaillées (une valeur par itération) sont rendues à l'appelant dans un dictionnaire
  et non journalisées. Aucune exception n'est interceptée ; une température sans recuit possible
  (moins de 2 itérations) lève `ValueError`.
"""

import collections
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Self

import numpy as np
import torch
from torch import nn

#: Indices réservés du vocabulaire du GAN.
PAD = 0
DEBUT = 1
FIN = 2

#: Journal de l'entraînement adverse : une valeur par itération et par grandeur suivie.
type JournalGAN = dict[str, list[float]]


def compter_mots(spams: Sequence[Sequence[str]]) -> collections.Counter[str]:
    """Fréquence de chaque mot dans une liste de SMS (ordre d'apparition conservé)."""
    return collections.Counter(mot for mots in spams for mot in mots)


@dataclass(frozen=True)
class VocabulaireGAN:
    """Vocabulaire des spams d'apprentissage et longueur fixe des phrases du GAN."""

    mot_vers_indice: dict[str, int]
    longueur: int

    @classmethod
    def apprendre(
        cls, spams: Sequence[Sequence[str]], longueur: int = 25, frequence_min: int = 2
    ) -> Self:
        """Mots vus au moins `frequence_min` fois, plus <pad>, <debut> et <fin>."""
        vocabulaire = {"<pad>": PAD, "<debut>": DEBUT, "<fin>": FIN}
        for mot, frequence in compter_mots(spams).most_common():
            if frequence >= frequence_min:
                vocabulaire[mot] = len(vocabulaire)
        return cls(vocabulaire, longueur)

    @property
    def taille(self) -> int:
        """Nombre d'entrées du vocabulaire."""
        return len(self.mot_vers_indice)

    @property
    def inverse(self) -> dict[int, str]:
        """Indice -> mot."""
        return {i: m for m, i in self.mot_vers_indice.items()}

    def encoder(self, mots: Sequence[str]) -> list[int]:
        """Spam -> indices (mots rares ignorés), terminé par <fin>, complété par <pad>."""
        indices = [self.mot_vers_indice[m] for m in mots if m in self.mot_vers_indice]
        indices = indices[: self.longueur - 1] + [FIN]
        return indices + [PAD] * (self.longueur - len(indices))

    def tenseur(self, spams: Sequence[Sequence[str]]) -> torch.Tensor:
        """Matrice d'entiers (nombre de spams, longueur)."""
        return torch.tensor([self.encoder(m) for m in spams]).long()


class Generateur(nn.Module):
    """LSTM conditionné par un bruit z, qui produit une phrase mot après mot."""

    def __init__(
        self,
        taille_vocab: int,
        longueur: int,
        dim_bruit: int = 32,
        dim: int = 64,
        cache: int = 128,
    ) -> None:
        super().__init__()
        self.taille_vocab = taille_vocab
        self.longueur = longueur
        self.dim_bruit = dim_bruit
        self.depuis_bruit = nn.Linear(dim_bruit, cache)  # bruit -> état initial du LSTM
        self.plongement = nn.Embedding(taille_vocab, dim)
        self.cellule = nn.LSTMCell(dim, cache)
        self.sortie = nn.Linear(cache, taille_vocab)

    def etat_initial(self, n: int) -> tuple[torch.Tensor, torch.Tensor]:
        h = torch.tanh(self.depuis_bruit(torch.randn(n, self.dim_bruit)))
        return h, torch.zeros_like(h)

    def vraisemblance(self, cibles: torch.Tensor) -> torch.Tensor:
        """Perte d'entropie croisée « mot suivant » sur de vrais spams (teacher forcing)."""
        n = cibles.shape[0]
        entrees = torch.cat([torch.ones(n, 1, dtype=torch.long), cibles[:, :-1]], dim=1)
        h, c = self.etat_initial(n)
        scores = []
        for t in range(self.longueur):
            h, c = self.cellule(self.plongement(entrees[:, t]), (h, c))
            scores.append(self.sortie(h))
        logits = torch.stack(scores, dim=1)
        return nn.functional.cross_entropy(
            logits.reshape(-1, self.taille_vocab), cibles.reshape(-1), ignore_index=PAD
        )

    def echantillonner(self, n: int, temperature: float) -> torch.Tensor:
        """Phrases (n, longueur, vocabulaire) : mots one-hot en avant, gradient adouci en arrière.

        gumbel_softmax(hard=True) renvoie le vecteur one-hot du mot tiré, mais laisse passer le
        gradient de la distribution adoucie (estimateur « straight-through »).
        """
        h, c = self.etat_initial(n)
        entree = self.plongement(torch.ones(n, dtype=torch.long))
        mots = []
        for _ in range(self.longueur):
            h, c = self.cellule(entree, (h, c))
            mot = nn.functional.gumbel_softmax(self.sortie(h), tau=temperature, hard=True)
            mots.append(mot)
            entree = mot @ self.plongement.weight  # plongement du mot tiré, toujours dérivable
        return torch.stack(mots, dim=1)


class Discriminateur(nn.Module):
    """Convolutions sur une suite de mots (vecteurs one-hot) -> score réel/fabriqué."""

    def __init__(self, taille_vocab: int, dim: int = 64, filtres: int = 64) -> None:
        super().__init__()
        self.projection = nn.Linear(taille_vocab, dim, bias=False)  # équivaut à un plongement
        self.convolutions = nn.ModuleList(nn.Conv1d(dim, filtres, k) for k in (3, 4, 5))
        self.abandon = nn.Dropout(0.3)
        self.sortie = nn.Linear(3 * filtres, 1)

    def forward(self, distributions: torch.Tensor) -> torch.Tensor:
        h = self.projection(distributions).transpose(1, 2)
        h = torch.cat([torch.relu(c(h)).amax(dim=2) for c in self.convolutions], dim=1)
        return torch.squeeze(self.sortie(self.abandon(h)), dim=1)


def masquer_apres_fin(phrases: torch.Tensor) -> torch.Tensor:
    """Remplace par <pad> tout ce qui suit le premier <fin> d'une phrase fabriquée.

    Les vrais spams s'arrêtent à <fin> puis ne contiennent que du remplissage (<pad>) ; le
    générateur, lui, continue d'émettre des mots jusqu'à la dernière position. Sans ce masquage, le
    discriminateur pourrait reconnaître un faux à sa « queue » de séquence, un indice de forme,
    sans rien juger du contenu. Le masque est constant (calculé sur les mots tirés) : le gradient
    passe par les positions conservées.
    """
    indices = phrases.argmax(dim=-1)
    est_fin = indices == FIN
    apres_fin = (est_fin.long().cumsum(dim=1) - est_fin.long()) > 0  # strictement après
    garde = (~apres_fin).unsqueeze(-1).float()
    remplissage = torch.zeros_like(phrases)
    remplissage[..., PAD] = 1.0
    return phrases * garde + remplissage * (1 - garde)


def pre_entrainer(
    generateur: Generateur, reels: torch.Tensor, epoques: int = 40, bavard: bool = True
) -> list[float]:
    """Pré-entraînement par maximum de vraisemblance ; rend la perte moyenne de chaque époque."""
    optim_g = torch.optim.Adam(generateur.parameters(), lr=1e-3)
    pertes: list[float] = []
    for epoque in range(1, epoques + 1):
        permutation = torch.randperm(len(reels))
        total = 0.0
        for debut_lot in range(0, len(reels), 32):
            lot = reels[permutation[debut_lot : debut_lot + 32]]
            optim_g.zero_grad()
            valeur = generateur.vraisemblance(lot)
            torch.autograd.backward(valeur)
            nn.utils.clip_grad_norm_(generateur.parameters(), 5.0)
            optim_g.step()
            total += valeur.item() * len(lot)
        pertes.append(total / len(reels))
        if bavard and epoque % 10 == 0:
            print(f"Pré-entraînement, époque {epoque} : perte de vraisemblance {pertes[-1]:.3f}")
    return pertes


def entrainer_adverse(
    generateur: Generateur,
    discriminateur: Discriminateur,
    reels: torch.Tensor,
    iterations: int = 300,
    bavard: bool = True,
) -> JournalGAN:
    """Entraînement adverse : D sépare vrais et faux, G cherche à le tromper.

    La température du Gumbel-softmax descend linéairement de 1,0 à 0,3. Le discriminateur ne
    reçoit que des faux masqués après <fin>. Le journal rendu a une valeur par itération.
    """
    if iterations < 2:
        raise ValueError("le recuit de la température demande au moins 2 itérations")
    taille_vocab = generateur.taille_vocab
    optim_g = torch.optim.Adam(generateur.parameters(), lr=1e-4)
    optim_d = torch.optim.Adam(discriminateur.parameters(), lr=1e-4)
    bce = nn.BCEWithLogitsLoss()
    journal: JournalGAN = {
        "perte_d": [],
        "perte_g_adverse": [],
        "perte_g_mv": [],
        "precision_d": [],
    }
    for iteration in range(iterations):
        temperature = 1.0 - 0.7 * iteration / (iterations - 1)  # recuit de 1,0 à 0,3
        lot_reel = reels[torch.randint(len(reels), (32,))]
        vrais = nn.functional.one_hot(lot_reel, taille_vocab).float()
        faux = masquer_apres_fin(generateur.echantillonner(32, temperature))
        # Discriminateur : vrais -> 1, faux -> 0 (faux détachés : on ne met à jour que D ici).
        optim_d.zero_grad()
        score_vrais, score_faux = discriminateur(vrais), discriminateur(faux.detach())
        perte_d = bce(score_vrais, torch.ones(32)) + bce(score_faux, torch.zeros(32))
        perte_d.backward()
        optim_d.step()
        # Générateur : faire classer ses faux comme vrais, plus le terme de vraisemblance.
        optim_g.zero_grad()
        perte_adverse = bce(discriminateur(faux), torch.ones(32))
        perte_mv = generateur.vraisemblance(lot_reel)
        torch.autograd.backward(perte_adverse + perte_mv)
        nn.utils.clip_grad_norm_(generateur.parameters(), 5.0)
        optim_g.step()
        precision_d = ((score_vrais > 0).float().mean() + (score_faux < 0).float().mean()) / 2
        journal["perte_d"].append(perte_d.item())
        journal["perte_g_adverse"].append(perte_adverse.item())
        journal["perte_g_mv"].append(perte_mv.item())
        journal["precision_d"].append(precision_d.item())
        if bavard and (iteration + 1) % 50 == 0:
            print(
                f"itération {iteration + 1} | température {temperature:.2f} | perte D "
                f"{perte_d.item():.3f} | perte G adverse {perte_adverse.item():.3f} | "
                f"vraisemblance {perte_mv.item():.3f} | exactitude de D {precision_d.item():.2f}"
            )
    return journal


def forme_phrases(
    generateur: Generateur, n: int = 500, temperature: float = 0.3
) -> tuple[float, float]:
    """Indices de FORME restants, mesurés avant masquage sur `n` phrases fabriquées.

    Rend (part des phrases qui contiennent <fin>, part des positions après <fin> occupées par un
    mot). Les vrais spams valent (1, 0) : tous ont un <fin> et du remplissage après.
    """
    generateur.eval()
    with torch.no_grad():
        echantillon = generateur.echantillonner(n, temperature).argmax(dim=-1)
    generateur.train()
    avec_fin = (echantillon == FIN).any(dim=1)
    apres_fin = ((echantillon == FIN).long().cumsum(dim=1) - (echantillon == FIN).long()) > 0
    occupees = ((echantillon != PAD) & apres_fin)[avec_fin].float().sum()
    non_vides_apres_fin = occupees / apres_fin[avec_fin].float().sum().clamp(min=1)
    return float(avec_fin.float().mean()), float(non_vides_apres_fin)


def generer(
    generateur: Generateur, vocabulaire: VocabulaireGAN, n: int, temperature: float = 0.3
) -> list[list[str]]:
    """n SMS générés (température finale 0,3), de <debut> jusqu'à <fin>."""
    inverse = vocabulaire.inverse
    generateur.eval()
    with torch.no_grad():
        indices = generateur.echantillonner(n, temperature).argmax(dim=-1).tolist()
    phrases = []
    for sequence in indices:
        mots = []
        for i in sequence:
            if i == FIN:
                break
            if i not in (PAD, DEBUT):
                mots.append(inverse[i])
        phrases.append(mots)
    return phrases


def suites(phrases: Sequence[Sequence[str]], n: int) -> list[tuple[str, ...]]:
    """Toutes les suites de n mots consécutifs des phrases (doublons conservés)."""
    return [tuple(p[i : i + n]) for p in phrases for i in range(len(p) - n + 1)]


def distinct(phrases: Sequence[Sequence[str]], n: int) -> float:
    """Part de n-grammes distincts parmi tous les n-grammes produits."""
    ngrammes = suites(phrases, n)
    return len(set(ngrammes)) / max(len(ngrammes), 1)


def quadrigrammes(phrases: Sequence[Sequence[str]]) -> set[tuple[str, ...]]:
    """Ensemble des suites de quatre mots présentes dans les phrases."""
    return set(suites(phrases, 4))


def tirer_independamment(
    tirage: np.random.Generator, compteur: collections.Counter[str], longueurs: Sequence[int]
) -> list[list[str]]:
    """Phrases de mots tirés indépendamment selon leur fréquence : le plus simple générateur."""
    mots = list(compteur)
    total = sum(compteur.values())
    probabilites = [compteur[m] / total for m in mots]
    return [list(tirage.choice(mots, size=taille, p=probabilites)) for taille in longueurs]


def melanger(tirage: np.random.Generator, phrases: Sequence[Sequence[str]]) -> list[list[str]]:
    """Mêmes mots, ordre au hasard : bon vocabulaire, aucune structure."""
    return [list(tirage.permutation(m)) for m in phrases]


def mesurer(
    phrases: Sequence[Sequence[str]],
    *,
    nb_comparables: int,
    quadri_app: set[tuple[str, ...]],
    textes_app: set[str],
    part_spam: Callable[[Sequence[Sequence[str]]], float],
) -> dict[str, float]:
    """Mesures de qualité d'un corpus ; la diversité est calculée à effectif égal."""
    comparables = phrases[:nb_comparables]  # distinct-n baisse quand le corpus grandit
    quadri = suites(phrases, 4)
    return {
        "messages": len(phrases),
        "distinct-1": distinct(comparables, 1),
        "distinct-2": distinct(comparables, 2),
        "4-grammes déjà vus dans les spams d'app.": float(
            np.mean([q in quadri_app for q in quadri])
        ),
        "copies exactes d'un spam d'app.": float(
            np.mean([" ".join(p) in textes_app for p in phrases])
        ),
        "classés spam par le TextCNN": part_spam(phrases),
    }
