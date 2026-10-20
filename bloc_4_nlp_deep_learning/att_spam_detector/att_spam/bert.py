# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Transfer learning : chargement, encodage WordPiece et ajustement de DistilBERT.

CHOIX
-----
- Retenu : DistilBERT (`distilbert/distilbert-base-uncased`), version allégée de BERT, avec une
  tête de classification à UNE sortie (`num_labels=1`) qui remplace la tête d'origine. Les poids
  « manquants » signalés au chargement sont ceux de cette tête neuve : c'est voulu.
- Retenu : réentraîner TOUT le réseau (pas seulement la tête) deux époques, avec AdamW et un petit
  taux (5e-5), pour ne pas effacer ce que le modèle sait de l'anglais.
- Retenu : la même perte pondérée que les réseaux construits (`att_spam/reseaux.py`), pour une
  comparaison équitable, et le texte normalisé de `att_spam/donnees.py` (presque aucune donnée de
  contact transmise au réseau).
- Écarté : une troisième époque. La meilleure époque est la dernière, donc elle aurait pu aider ou
  amorcer un sur-apprentissage ; elle n'est pas testée, pour tenir le temps de calcul (environ
  9 minutes sur processeur pour deux époques).
- Écarté : garder les barres de progression et les avertissements de téléchargement. Ils
  n'apportent rien au lecteur du notebook ; `charger_distilbert` les masque.

OÙ
---
- Place : section 5 du notebook `ATT_Spam_Bloc4_CDSD.ipynb` (C4.3). Le modèle ajusté alimente
  l'évaluation finale (section 7) et l'ablation (section 8).
- Appelants : le notebook. Dépendances : torch, transformers, numpy, scikit-learn,
  `att_spam/reseaux.py` (type `Historique`, `ligne_epoque`). Le modèle de départ est lu dans le
  cache local de Hugging Face ou téléchargé au premier lancement (réseau, une seule fois).

COMMENT
-------
1. `charger_distilbert(nom, graine)` masque les journaux de transformers et de huggingface_hub,
   charge le tokeniseur, fixe la graine de torch, puis charge le modèle : l'initialisation de la
   tête neuve dépend de cette graine.
2. `encoder_bert` rend un `TensorDataset` (identifiants, masque d'attention, cibles) de 64 jetons
   au plus, complété par du remplissage.
3. `probabilites_bert` rend sigmoïde(logit) par lots de 64, en mode évaluation et sans gradient.
4. `ajuster_distilbert` boucle sur les époques : lots de 32 mélangés par un générateur propre à la
   graine, perte pondérée, AdamW ; en fin d'époque, probabilités sur l'apprentissage et la
   validation, courbes de perte et de F1 (seuil 0,5) ; l'état de la meilleure époque est copié puis
   rechargé à la fin.
- Tests qui le prouvent : `tests/test_bert.py` (mini-DistilBERT sans réseau : formes, masque,
  probabilités dans ]0 ; 1[, retour au meilleur état, refus de zéro époque) ;
  `tests/test_notebook.py` (chiffres de la section 5 du notebook).

POURQUOI
--------
- Le conseil de l'énoncé est d'essayer le transfer learning, et la compétence C4.3 demande de
  configurer un réseau pré-entraîné profond. Source : référentiel de compétences
  RNCP35288, bloc 4, compétence C4.3.
- Le nombre d'époques (2), le taux (5e-5) et la longueur maximale (64 jetons) sont des valeurs
  usuelles de fine-tuning, non établies par une étude de ce projet : source : inconnue.
- Le temps d'ajustement et le nombre de paramètres sont mesurés dans le notebook, section 5, pas
  fixés ici : voir `tests/test_notebook.py`.

JOURNALISATION
--------------
- `charger_distilbert` règle le niveau de `transformers` et de `huggingface_hub` sur ERROR, coupe
  leurs barres de progression et masque l'avertissement « requêtes non authentifiées » : seules les
  erreurs de ces bibliothèques restent visibles.
- `ajuster_distilbert` affiche avec `print` une ligne par époque (perte, F1) : c'est l'affichage
  voulu du notebook. Aucune exception n'est interceptée.
"""

import copy
import math
import warnings
from typing import Any

import numpy as np
import numpy.typing as npt
import torch
from huggingface_hub.utils import logging as journal_hub
from sklearn.metrics import f1_score  # type: ignore[import-untyped]
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    PreTrainedModel,
    PreTrainedTokenizerBase,
)
from transformers.utils import logging as journal_transformers

from att_spam.reseaux import Historique, ligne_epoque

NOM_MODELE = "distilbert/distilbert-base-uncased"
#: Avertissement du Hub quand on télécharge sans compte (motif insensible à la casse).
AVERTISSEMENT_HUB = ".*unauthenticated requests.*"
LONGUEUR_MAX = 64


def charger_distilbert(
    nom: str, graine: int
) -> tuple[PreTrainedTokenizerBase, PreTrainedModel]:
    """Tokeniseur et modèle à une sortie ; journaux masqués ; graine fixée avant la tête neuve."""
    journal_transformers.set_verbosity_error()  # type: ignore[no-untyped-call]
    journal_transformers.disable_progress_bar()  # type: ignore[no-untyped-call]
    journal_hub.set_verbosity_error()  # type: ignore[no-untyped-call]
    warnings.filterwarnings("ignore", message=AVERTISSEMENT_HUB)
    tokeniseur = AutoTokenizer.from_pretrained(nom)
    torch.manual_seed(graine)
    modele = AutoModelForSequenceClassification.from_pretrained(nom, num_labels=1)
    return tokeniseur, modele


def encoder_bert(
    tokeniseur: PreTrainedTokenizerBase, textes: list[str], cibles: npt.ArrayLike
) -> TensorDataset:
    """Texte normalisé -> identifiants WordPiece et masque d'attention (64 jetons au plus)."""
    codes = tokeniseur(
        textes, padding="max_length", truncation=True, max_length=LONGUEUR_MAX, return_tensors="pt"
    )
    return TensorDataset(
        codes["input_ids"], codes["attention_mask"], torch.tensor(cibles, dtype=torch.float32)
    )


def probabilites_bert(modele: nn.Module, jeu_tenseurs: TensorDataset) -> npt.NDArray[np.float32]:
    """Probabilités de spam prédites par DistilBERT, par lots de 64."""
    modele.eval()
    sorties = []
    with torch.no_grad():
        for ids, masque, _ in DataLoader(jeu_tenseurs, batch_size=64):
            sorties.append(
                torch.sigmoid(modele(input_ids=ids, attention_mask=masque).logits.squeeze(1))
            )
    return np.asarray(torch.cat(sorties).numpy(), dtype=np.float32)


def ajuster_distilbert(
    modele: nn.Module,
    jeu_app: TensorDataset,
    jeu_val: TensorDataset,
    perte: nn.Module,
    *,
    graine: int,
    epoques: int = 2,
) -> Historique:
    """Ajuste tout le réseau, garde l'état de la meilleure époque (F1 de validation à 0,5)."""
    if epoques < 1:
        raise ValueError("il faut au moins une époque pour retenir un meilleur état")
    y_app, y_val = jeu_app.tensors[2], jeu_val.tensors[2]
    optimiseur = torch.optim.AdamW(modele.parameters(), lr=5e-5)
    chargeur = DataLoader(
        jeu_app, batch_size=32, shuffle=True, generator=torch.Generator().manual_seed(graine)
    )
    historique: Historique = {"perte_app": [], "perte_val": [], "f1_app": [], "f1_val": []}
    meilleur_f1 = -math.inf
    meilleur_etat: dict[str, Any]  # affecté à la première époque, qui est toujours la meilleure
    for epoque in range(1, epoques + 1):
        modele.train()
        pertes = []
        for ids, masque, cibles in chargeur:
            optimiseur.zero_grad()
            valeur = perte(modele(input_ids=ids, attention_mask=masque).logits.squeeze(1), cibles)
            valeur.backward()
            optimiseur.step()
            pertes.append(valeur.item())
        p_app, p_val = probabilites_bert(modele, jeu_app), probabilites_bert(modele, jeu_val)
        historique["perte_app"].append(float(np.mean(pertes)))
        z_val = torch.logit(torch.tensor(p_val).clamp(1e-6, 1 - 1e-6))
        historique["perte_val"].append(perte(z_val, y_val).item())
        historique["f1_app"].append(f1_score(y_app.numpy(), p_app >= 0.5))
        historique["f1_val"].append(f1_score(y_val.numpy(), p_val >= 0.5))
        print(ligne_epoque("DistilBERT", epoque, historique, largeur=1))
        if historique["f1_val"][-1] > meilleur_f1:
            meilleur_f1 = historique["f1_val"][-1]
            meilleur_etat = copy.deepcopy(modele.state_dict())
    modele.load_state_dict(meilleur_etat)
    return historique
