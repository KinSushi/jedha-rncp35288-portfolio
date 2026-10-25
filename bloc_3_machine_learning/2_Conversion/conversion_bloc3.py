# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Fonctions du notebook Conversion Rate (bloc 3 CDSD), sorties du notebook pour être testées.

CHOIX
-----
- Sortir du notebook les fonctions qui calculent (synthèse par segment, seuil de décision,
  évaluation d'un classifieur, résumé des clusters, lecture des diagnostics) et les mettre dans ce
  module, importé par la cellule d'imports. Les tests (dossier tests/) les appellent sans
  exécuter le notebook.
- Laisser dans le notebook ce qui se lit et se modifie à l'oral : RANDOM_STATE, les listes de
  variables num_cols et cat_cols, les préprocesseurs, la liste des modèles comparés, le seuil
  retenu, k des clusters et toute la mise en page. Un jury peut changer une valeur dans la
  cellule et relancer, sans ouvrir ce fichier.
- Écarté : tout mettre dans le module. Le raisonnement vit dans les cellules Markdown, et le
  jury lit le notebook.
- Écarté : garder des variables globales (X_train, y_valid) lues par les fonctions. Chaque
  fonction reçoit ses données en paramètres : elle se teste seule et ne dépend pas de l'ordre
  d'exécution des cellules.
- Écarté : avaler les entrées invalides. Une grille de seuils vide, des tailles incohérentes,
  un seuil hors de [0, 1] ou une cible autre que 0 / 1 lèvent ValueError au lieu de rendre un
  résultat trompeur.
- Retenu (20/10/2026) : evaluate_classifier refuse elle-même, avant tout ajustement, une cible
  y_train ou y_valid qui contient autre chose que 0 et 1, avec un message qui nomme l'argument
  et les valeurs refusées (_refuser_cible_non_binaire), comme les autres refus du projet
  (seuil hors de [0, 1], colonne absente). Écarté : laisser scikit-learn refuser plus loin.
  Son refus dépendait du type des prédictions internes (« [0 2] » contre « [0. 2.] » pour une
  cible sans classe 1, contrôle indépendant du 20/10/2026) et ne nommait ni l'argument ni la
  faute. Écarté aussi : retirer la conversion astype(int) des prédictions (remède par
  simplification) : le mutant disparaissait, mais la cible absurde restait acceptée.

OÙ
---
- Place : à côté du notebook, dans le dossier 2_Conversion. Appelant unique : le notebook
  Conversion_Rate_Bloc3_CDSD_v2.ipynb (cellules 3 à 53) ; les tests de
  tests/test_conversion_bloc3.py et le test d'intégration tests/test_notebook_conversion.py
  l'appellent aussi.
- Dépendances : numpy, pandas, scikit-learn et IPython (affichage), versions épinglées dans
  requirements.txt. Aucune lecture de fichier, aucun réseau, aucune écriture sur disque, sauf
  trouver_racine qui teste l'existence d'un fichier.
- Les fonctions sont pures : mêmes entrées, mêmes sorties ; evaluate_classifier ajuste le
  pipeline qu'on lui passe (c'est son rôle) et rend ce même pipeline ajusté.

COMMENT
-------
- optimize_threshold essaie chaque seuil d'une grille (0,01 à 0,99, 197 valeurs par défaut) et
  rend celui qui maximise le F1, avec ce F1. Preuves : tests/test_conversion_bloc3.py
  (test_optimize_threshold_*).
- evaluate_classifier ajuste le pipeline sur l'apprentissage, choisit le seuil sur la
  validation s'il n'est pas imposé, et rend les métriques train et validation. Preuves :
  test_evaluate_classifier_*.
- Avant tout ajustement, evaluate_classifier refuse une cible autre que 0 / 1. Preuves :
  test_evaluate_classifier_refuse_une_cible_autre_que_zero_ou_un (2, -1, 0,5, NaN, « 0 » / « 1 »
  en texte, en apprentissage comme en validation, message exact),
  test_evaluate_classifier_refuse_la_cible_avant_d_ajuster_le_modele (le modèle n'a rien vu)
  et test_evaluate_classifier_accepte_toute_cible_zero_un (entiers, flottants, booléens,
  Int64, catégories, une seule classe : mêmes métriques que la cible entière).
- conversion_summary, resume_clusters, statut_visiteurs, lecture_ecart_apprentissage et
  lecture_stabilite chiffrent et décrivent les segments et les diagnostics. Preuves : tests du
  même nom.
- make_one_hot_encoder et make_model_pipeline fabriquent l'encodeur et le pipeline : le
  prétraitement est cloné pour chaque modèle. Preuves : test_make_*.
- Intégration : tests/test_notebook_conversion.py exécute le notebook et vérifie ses chiffres
  clés (F1 0,7768 au seuil 0,45, 284 580 lignes, soumission de 31 620 lignes).

POURQUOI
--------
- Règle de documentation et de test : une fonction sans test direct ni test inverse n'est pas
  livrée (tests/test_conversion_bloc3.py : chaque fonction a les deux).
- Le seuil est optimisé sur la validation et jamais sur le test non étiqueté : voir la
  cellule Markdown 25 du notebook (le seuil est une décision métier).
- Référentiel RNCP35288, bloc 3 : https://www.francecompetences.fr/recherche/rncp/35288/ ;
  F1-score imposé par le challenge de conversion : énoncé du challenge, non redistribué.
- Cible 0 / 1 : la colonne converted de conversion_data_train.csv ne contient que 0 et 1
  (mesuré le 20/10/2026 sur les 284 580 lignes). Refus d'une autre étiquette : contrôle
  indépendant de la mutation du 20/10/2026, qui a montré qu'une cible sans classe 1 faisait
  dépendre le message d'erreur du type interne des prédictions (rapport de vérification hors
  du dépôt, cité dans MUTANTS_EQUIVALENTS.md).
- Seuils de lecture des diagnostics (0,02 pour l'écart apprentissage - validation et pour la
  dispersion entre plis ; 0,99 et 0,01 pour la part de nouveaux visiteurs) : conventions de
  lecture du notebook, source : inconnue.

JOURNALISATION
--------------
- Ce module ne journalise rien : ce sont des fonctions pures, appelées dans un notebook dont les
  affichages (print et tableaux) font office de journal. Aucune donnée personnelle n'est écrite.
- Aucun avertissement n'est masqué : zero_division=0 est passé explicitement aux métriques quand
  un modèle ne prédit aucun positif (la précision n'est alors pas définie).
- Un refus est une exception (ValueError), pas une ligne de journal : la cellule du notebook
  s'arrête et affiche le message. Celui d'une cible hors {0, 1} nomme l'argument (y_train ou
  y_valid) et les étiquettes refusées, jamais une ligne de données d'un visiteur.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from IPython.display import Markdown, display
from sklearn.base import clone  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    accuracy_score,
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import OneHotEncoder  # type: ignore[import-untyped]

__all__ = [
    "afficher",
    "afficher_markdown",
    "conversion_summary",
    "evaluate_classifier",
    "lecture_ecart_apprentissage",
    "lecture_stabilite",
    "make_model_pipeline",
    "make_one_hot_encoder",
    "optimize_threshold",
    "resume_clusters",
    "statut_visiteurs",
    "trouver_racine",
]


# --------------------------------------------------------------------------------------------
# Affichage dans le notebook
# --------------------------------------------------------------------------------------------


def afficher(*objets: object) -> None:
    """Affiche des objets dans le notebook (enveloppe typée de IPython.display.display).

    Pourquoi : `display` n'a pas d'annotations, et mypy --strict refuse de l'appeler depuis du
    code typé. Cette enveloppe n'ajoute aucun comportement.
    """
    display(*objets)  # type: ignore[no-untyped-call]


def afficher_markdown(texte: str) -> None:
    """Affiche un texte Markdown calculé (enveloppe typée de display(Markdown(texte)))."""
    display(Markdown(texte))  # type: ignore[no-untyped-call]


# --------------------------------------------------------------------------------------------
# Dossier du projet
# --------------------------------------------------------------------------------------------


def trouver_racine(
    nom_notebook: str, sous_dossier: str, depart: Path | None = None
) -> Path:
    """Dossier du projet : celui qui contient le notebook (ou son sous-dossier `sous_dossier`).

    On remonte depuis `depart` (par défaut le dossier courant) jusqu'au premier dossier qui
    contient le notebook, directement ou dans `sous_dossier`. À défaut, on garde `depart`.
    Le noyau Jupyter ne démarre pas toujours dans le dossier du notebook, d'où cette recherche.
    Lève ValueError si `nom_notebook` est vide.
    """
    if not nom_notebook:
        raise ValueError("nom_notebook est vide : tout dossier « contiendrait » ce notebook")
    point_depart = (depart if depart is not None else Path.cwd()).resolve()
    for dossier in (point_depart, *point_depart.parents):
        for candidat in (dossier, dossier / sous_dossier):
            if (candidat / nom_notebook).exists():
                return candidat
    return point_depart


# --------------------------------------------------------------------------------------------
# Synthèse par segment, encodeur, pipeline
# --------------------------------------------------------------------------------------------


def conversion_summary(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Volume et taux de conversion par modalité d'une variable, du plus fort taux au plus faible.

    Une fonction pour garantir la même définition du taux de conversion partout (moyenne de la
    colonne `converted`, 0/1) et éviter de répéter le même groupby. Lève KeyError si `column` ou
    `converted` est absent de `df`.

    Les modalités d'une colonne catégorielle qui n'apparaissent pas dans `df` sont omises : c'est
    le comportement par défaut de groupby depuis pandas 3.0 (observed=True).
    """
    return (
        df.groupby(column)["converted"]
        .agg(count="size", conversion_rate="mean")
        .sort_values("conversion_rate", ascending=False)
    )


def make_one_hot_encoder(drop: str | None = None) -> OneHotEncoder:
    """Crée un OneHotEncoder dense qui ignore les catégories inconnues.

    handle_unknown='ignore' : en production ou dans le test, une catégorie jamais vue au train
    peut apparaître ; l'ignorer (colonnes à zéro) évite que le pipeline échoue au moment de
    prédire. drop='first' seulement pour le modèle linéaire : cela évite la colinéarité parfaite
    entre dummies, et la modalité retirée devient la RÉFÉRENCE des coefficients.
    """
    return OneHotEncoder(handle_unknown="ignore", sparse_output=False, drop=drop)


def make_model_pipeline(preprocess: Any, estimator: Any) -> Pipeline:
    """Assemble un prétraitement et un estimateur dans un Pipeline.

    clone(preprocess) : un ColumnTransformer ajusté garde son état (moyennes, catégories). Si
    plusieurs pipelines partageaient le MÊME objet, chaque fit écraserait l'état des autres ; avec
    clone, chaque modèle ajuste son propre prétraitement, sans effet de bord.
    """
    return Pipeline([("preprocess", clone(preprocess)), ("model", estimator)])


# --------------------------------------------------------------------------------------------
# Seuil de décision et évaluation d'un classifieur
# --------------------------------------------------------------------------------------------


def optimize_threshold(
    y_true: Any, y_proba: Any, grid: Sequence[float] | np.ndarray[Any, Any] | None = None
) -> tuple[float, float]:
    """Seuil de probabilité qui maximise le F1-score, et ce F1.

    Pourquoi optimiser le seuil ? predict_proba donne une probabilité de conversion ; le seuil
    par défaut 0,50 n'est pas forcément optimal, et le challenge évalue le F1-score. Le seuil
    est optimisé uniquement sur la validation, jamais sur le fichier test non étiqueté.

    À égalité de F1, le plus petit seuil de la grille est rendu. Lève ValueError si la grille
    est vide.
    """
    if grid is None:
        # Grille de 0,01 à 0,99 avec un pas fin mais raisonnable (0,005). Inutile de tester
        # 10 000 seuils : le gain serait négligeable et moins lisible.
        grid = np.linspace(0.01, 0.99, 197)
    if len(grid) == 0:
        raise ValueError("la grille de seuils est vide")

    # zero_division=0 évite une erreur si un seuil ne prédit aucun positif.
    scores = np.array(
        [f1_score(y_true, y_proba >= threshold, zero_division=0) for threshold in grid]
    )
    best_idx = int(scores.argmax())
    return float(grid[best_idx]), float(scores[best_idx])


def _refuser_cible_non_binaire(nom: str, y: Any) -> None:
    """Lève ValueError si la cible `y` contient une valeur autre que 0 et 1.

    La cible du projet est `converted` : 1 si le visiteur s'abonne, 0 sinon. Les métriques de
    evaluate_classifier (F1, précision, rappel) prennent 1 pour classe positive ; une autre
    étiquette (2, -1, « 0 » écrit en texte, NaN) n'a pas de sens ici. On la refuse donc AVANT
    tout ajustement, avec un message qui nomme l'argument (`nom`) et les valeurs refusées.

    Domaine admis : toute valeur égale à 0 ou à 1 au sens de Python, donc les entiers, les
    flottants 0.0 / 1.0, les booléens (True vaut 1) et les catégories 0 / 1. Une cible d'une
    seule classe (que des 0, ou que des 1) reste admise : ce n'est pas une étiquette inconnue.

    Comment : `unique().tolist()` donne les valeurs distinctes en scalaires Python (2 et non
    np.int64(2), pour un message lisible) ; la différence d'ensembles avec {0, 1} garde les
    intrus. NaN et pd.NA ne sont égaux ni à 0 ni à 1 : ils sont refusés. `repr` distingue le
    texte « '0' » de l'entier 0 dans le message.
    """
    hors_domaine = set(pd.Series(y).unique().tolist()) - {0, 1}
    if hors_domaine:
        raise ValueError(
            f"{nom} ne doit contenir que 0 (non converti) et 1 (converti) ; valeurs refusées : "
            + ", ".join(sorted(map(repr, hors_domaine)))
        )


def evaluate_classifier(
    name: str,
    pipeline: Pipeline,
    x_train: pd.DataFrame,
    y_train: pd.Series,
    x_valid: pd.DataFrame,
    y_valid: pd.Series,
    threshold: float | None = None,
) -> tuple[dict[str, Any], Pipeline]:
    """Entraîne un pipeline et calcule les métriques train / validation, au même protocole.

    fit apprend à la fois le prétraitement et le modèle sur x_train seulement. Si le pipeline
    expose predict_proba, le seuil est celui qu'on impose, sinon celui qui maximise le F1 sur la
    validation ; sinon les classes sont prédites directement et seuil, ROC-AUC et PR-AUC valent
    NaN. Rend (métriques, pipeline ajusté).

    Lève ValueError, avant tout ajustement, si `threshold` n'est pas dans [0, 1], ou si
    `y_train` ou `y_valid` contient une valeur autre que 0 et 1 (_refuser_cible_non_binaire).
    """
    if threshold is not None and not 0.0 <= threshold <= 1.0:
        raise ValueError(f"le seuil doit être dans [0, 1], reçu {threshold}")
    # Cible hors {0, 1} : refusée ici, avec un message qui nomme la faute. Sans ce refus,
    # scikit-learn échouait plus loin, avec un message qui dépendait du type des prédictions.
    _refuser_cible_non_binaire("y_train", y_train)
    _refuser_cible_non_binaire("y_valid", y_valid)
    pipeline.fit(x_train, y_train)

    if hasattr(pipeline, "predict_proba"):
        proba_train = pipeline.predict_proba(x_train)[:, 1]
        proba_valid = pipeline.predict_proba(x_valid)[:, 1]
        if threshold is None:
            threshold, _ = optimize_threshold(y_valid, proba_valid)
        # Prédictions 0 / 1 (entiers). Le type n'a pas d'effet sur les métriques tant que la
        # cible est 0 / 1 : voir MUTANTS_EQUIVALENTS.md et le test de la prémisse.
        pred_train = (proba_train >= threshold).astype(int)
        pred_valid = (proba_valid >= threshold).astype(int)
        valid_auc = roc_auc_score(y_valid, proba_valid)
        # PR-AUC : aire sous la courbe précision-rappel. Contrairement à la ROC-AUC, elle ne se
        # gonfle pas avec la masse des négatifs : elle convient aux classes rares.
        valid_pr_auc = average_precision_score(y_valid, proba_valid)
    else:
        # Conservé pour la robustesse si un futur modèle ne fournit pas de probabilités.
        pred_train = pipeline.predict(x_train)
        pred_valid = pipeline.predict(x_valid)
        threshold = np.nan
        valid_auc = np.nan
        valid_pr_auc = np.nan

    return {
        "model": name,
        "threshold": threshold,
        "train_f1": f1_score(y_train, pred_train, zero_division=0),
        "valid_f1": f1_score(y_valid, pred_valid, zero_division=0),
        "valid_precision": precision_score(y_valid, pred_valid, zero_division=0),
        "valid_recall": recall_score(y_valid, pred_valid, zero_division=0),
        "valid_accuracy": accuracy_score(y_valid, pred_valid),
        "valid_roc_auc": valid_auc,
        "valid_pr_auc": valid_pr_auc,
    }, pipeline


# --------------------------------------------------------------------------------------------
# Clusters et lectures de diagnostic
# --------------------------------------------------------------------------------------------


def resume_clusters(x: pd.DataFrame, y: pd.Series, etiquettes: Any) -> pd.DataFrame:
    """Tableau de synthèse par cluster ; la cible n'est utilisée qu'ICI, pour interpréter.

    x porte age, new_user, total_pages_visited, country et source ; y est la cible converted ;
    `etiquettes` donne le cluster de chaque ligne. Rend un tableau trié par taux de conversion
    décroissant. Lève ValueError si les trois n'ont pas la même longueur.
    """
    if not len(x) == len(y) == len(etiquettes):
        raise ValueError("x, y et les étiquettes doivent avoir la même longueur")
    table = x.copy()
    table["converted"] = y.values
    table["cluster"] = etiquettes
    return (
        table.groupby("cluster")
        .agg(
            n=("converted", "size"),
            conversion_rate=("converted", "mean"),
            age_mean=("age", "mean"),
            new_user_rate=("new_user", "mean"),
            pages_mean=("total_pages_visited", "mean"),
            china_share=("country", lambda s: (s == "China").mean()),
            us_share=("country", lambda s: (s == "US").mean()),
            ads_share=("source", lambda s: (s == "Ads").mean()),
            direct_share=("source", lambda s: (s == "Direct").mean()),
            seo_share=("source", lambda s: (s == "Seo").mean()),
        )
        .sort_values("conversion_rate", ascending=False)
    )


def statut_visiteurs(taux_nouveaux: float) -> str:
    """Phrase courte sur la part de nouveaux visiteurs d'un cluster (seuils stricts 0,99 et 0,01).

    Lève ValueError si la part n'est pas dans [0, 1] ou est NaN.
    """
    if math.isnan(taux_nouveaux) or not 0.0 <= taux_nouveaux <= 1.0:
        raise ValueError(f"la part de nouveaux visiteurs doit être dans [0, 1] : {taux_nouveaux}")
    if taux_nouveaux > 0.99:
        return "uniquement des nouveaux visiteurs"
    if taux_nouveaux < 0.01:
        return "uniquement des visiteurs récurrents"
    return f"{taux_nouveaux:.0%} de nouveaux visiteurs"


def lecture_ecart_apprentissage(ecart: float) -> str:
    """« négligeable » si l'écart apprentissage - validation est sous 0,02 en valeur absolue.

    Convention de lecture du notebook (seuil indicatif). Lève ValueError si l'écart est NaN.
    """
    if math.isnan(ecart):
        raise ValueError("écart non défini (NaN)")
    return "négligeable" if abs(ecart) < 0.02 else "à surveiller"


def lecture_stabilite(dispersion: float) -> str:
    """Lecture de l'écart-type du F1 entre plis : stable en dessous de 0,02, sensible sinon.

    Convention de lecture du notebook (seuil indicatif). Lève ValueError si la dispersion est
    négative ou NaN : un écart-type ne peut pas l'être.
    """
    if math.isnan(dispersion) or dispersion < 0:
        raise ValueError(f"écart-type invalide : {dispersion}")
    if dispersion < 0.02:
        return "score stable, indépendant du découpage"
    return "score sensible au découpage"
