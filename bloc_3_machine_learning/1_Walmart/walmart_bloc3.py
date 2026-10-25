# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Fonctions du notebook Walmart (bloc 3 CDSD), sorties du notebook pour être testées.

CHOIX
-----
- Sortir du notebook les fonctions qui calculent (nettoyage, métriques, recherche d'alpha,
  colinéarité, découpages répétés, diagnostic) et les mettre dans ce module, importé par la cellule
  d'imports du notebook. Les tests (dossier tests/) les appellent sans exécuter le notebook.
- Laisser dans le notebook ce qui se lit et se modifie à l'oral : la graine RANDOM_STATE, la liste
  OUTLIER_COLS, le seuil N_SIGMA, les listes de variables, le préprocesseur scikit-learn, les
  grilles d'alpha et toute la mise en page (graphiques, textes). Un jury peut ainsi changer une
  valeur dans la cellule et relancer, sans ouvrir ce fichier.
- Écarté : tout mettre dans le module. Le raisonnement vit dans les cellules Markdown, et un module
  de 800 lignes ne se défend pas en cinq minutes.
- Écarté : garder des variables globales (X_train, preprocessor) lues par les fonctions. Chaque
  fonction reçoit ses données en paramètres : elle se teste seule et ne dépend pas de l'ordre
  d'exécution des cellules.
- Écarté : avaler les entrées invalides. Une colonne inconnue ou un seuil de sigma non positif
  lève ValueError avec un message qui nomme la faute, au lieu d'un IndexError loin de la cause.

OÙ
---
- Place : à côté du notebook, dans le dossier 1_Walmart. Appelant unique : le notebook
  Walmart_Bloc3_CDSD_v2.ipynb (cellules 3 à 34) ; les tests de tests/test_walmart_bloc3.py et le
  test d'intégration tests/test_notebook_walmart.py l'appellent aussi.
- Dépendances : numpy, pandas, scikit-learn et IPython (affichage), versions épinglées dans
  requirements.txt. Aucune lecture de fichier, aucun accès réseau, aucune écriture sur disque,
  sauf trouver_racine qui teste l'existence d'un fichier.
- Les fonctions de ce module sont pures : mêmes entrées, mêmes sorties, aucun état global
  modifié (le seul état est la constante RMSE_SCORER, un objet immuable).

COMMENT
-------
- prepare_dataframe supprime les lignes sans cible, extrait Year, Month, Day et DayOfWeek de la
  date, puis retire les lignes hors de moyenne ± n_sigma écarts-types pour chaque colonne de
  outlier_cols ; elle rend le jeu nettoyé, le journal des étapes et les seuils appliqués. Preuves :
  tests/test_walmart_bloc3.py (test_prepare_dataframe_*, dont les refus de colonne inconnue et de
  sigma non positif).
- consequences_pretraitement dit quelles lignes et quels magasins la règle a retirés, et la borne
  haute du chômage si Unemployment est dans la règle, sinon None (c'était un IndexError dans la
  cellule 9 quand on retirait Unemployment de outlier_cols). Preuve : test_consequences_*.
- build_pipeline, chercher_alpha, evaluate_model, rmse reprennent le protocole du notebook :
  prétraitement cloné pour chaque modèle, alpha choisi par validation croisée sur
  l'apprentissage seul, RMSE négatif pour le scorer. Preuves : test_build_pipeline_*,
  test_chercher_alpha_*, test_evaluate_model_*, test_rmse_*.
- r2_regression, part_entre_magasins, phrase_variable chiffrent la colinéarité et la lecture des
  coefficients ; evaluer_un_decoupage rejoue toute la procédure sur une graine ;
  diagnostic_validation classe l'écart apprentissage - validation. Preuves : tests du même nom.
- Intégration : tests/test_notebook_walmart.py exécute le notebook et vérifie ses chiffres clés
  (131 lignes, 19 magasins, Lasso, R² de test).

POURQUOI
--------
- Règle de documentation et de test : une fonction non couverte par un test direct et un test
  inverse n'est pas livrée (tests/test_walmart_bloc3.py : chaque fonction a les deux).
- Choix du modèle sur la validation croisée de l'apprentissage et pas sur le test : voir la
  cellule Markdown 6 du notebook et la section 11 ; le test ne sert qu'à mesurer.
- Règle moyenne ± 3 écarts-types sur Temperature, Fuel_Price, CPI et Unemployment : imposée par
  l'énoncé du projet, non redistribué ici.
- Référentiel RNCP35288, bloc 3 : https://www.francecompetences.fr/recherche/rncp/35288/ ;
  documentation scikit-learn : https://scikit-learn.org/stable/modules/compose.html
- Seuils de lecture du diagnostic (0,5 ; 0,10 ; 0,02) : conventions de lecture du notebook,
  sans source externe, source : inconnue.

JOURNALISATION
--------------
- Ce module ne journalise rien : ce sont des fonctions pures, appelées dans un notebook dont les
  affichages (print et tableaux) font office de journal. Aucune information sensible n'est écrite.
- Les avertissements de scikit-learn sont capturés par evaluer_un_decoupage, qui les rend dans
  sa deuxième valeur de retour : « unknown categories » est compté, tout autre avertissement est
  renvoyé au notebook, qui l'affiche. Rien n'est masqué.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from IPython.display import Markdown, display
from sklearn.base import clone  # type: ignore[import-untyped]
from sklearn.linear_model import LinearRegression  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    make_scorer,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
)
from sklearn.model_selection import (  # type: ignore[import-untyped]
    GridSearchCV,
    KFold,
    cross_validate,
    train_test_split,
)
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]

__all__ = [
    "RMSE_SCORER",
    "ConsequencesPretraitement",
    "afficher",
    "afficher_markdown",
    "build_pipeline",
    "chercher_alpha",
    "consequences_pretraitement",
    "diagnostic_validation",
    "evaluate_model",
    "evaluer_un_decoupage",
    "fr",
    "part_entre_magasins",
    "phrase_variable",
    "prepare_dataframe",
    "r2_regression",
    "rmse",
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
# Prétraitement pandas imposé par l'énoncé
# --------------------------------------------------------------------------------------------


def prepare_dataframe(
    df_raw: pd.DataFrame, outlier_cols: Sequence[str], n_sigma: float = 3.0
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Prépare le dataset avant la séparation X/y.

    Paramètres
    ----------
    df_raw : pd.DataFrame
        Dataset brut chargé depuis le CSV. Il n'est pas modifié en place.
    outlier_cols : séquence de noms de colonnes
        Colonnes soumises à la règle moyenne ± n_sigma écarts-types (l'énoncé cite Temperature,
        Fuel_Price, CPI et Unemployment). Une séquence vide désactive le filtre.
    n_sigma : float
        Largeur de l'intervalle en écarts-types (3 dans l'énoncé).

    Retours
    -------
    model_df : jeu nettoyé au niveau pandas, prêt à être découpé en X et y.
    preprocessing_report : journal d'audit (lignes avant / après chaque opération).
    outlier_report : seuils appliqués pour chaque colonne de outlier_cols.

    Lève ValueError si n_sigma n'est pas strictement positif ou si une colonne de outlier_cols
    n'existe pas dans df_raw.

    Choix méthodologiques
    ---------------------
    1. La cible Weekly_Sales est supprimée si manquante, jamais imputée.
    2. Les variables X manquantes ne sont pas supprimées ici : elles seront imputées dans le
       Pipeline après le découpage, pour éviter la fuite de données.
    3. Les valeurs extrêmes ne sont filtrées que sur les colonnes de outlier_cols.
    4. Les dates sont transformées en variables numériques interprétables.
    """
    if not math.isfinite(n_sigma) or n_sigma <= 0:
        raise ValueError(f"n_sigma doit être strictement positif et fini, reçu {n_sigma}")
    inconnues = [col for col in outlier_cols if col not in df_raw.columns]
    if inconnues:
        raise ValueError(f"Colonnes absentes de df_raw pour la règle des sigmas : {inconnues}")

    # copy() évite de modifier df_raw : le brut doit rester disponible pour tout contrôle.
    df = df_raw.copy()

    # report_rows accumule les étapes : le nettoyage devient un processus auditable.
    report_rows = []

    # 6.1 Suppression des lignes où la cible est manquante (jamais imputée : ce serait fabriquer
    # des ventes et biaiser l'apprentissage).
    before = len(df)
    df = df.dropna(subset=["Weekly_Sales"]).copy()
    report_rows.append(
        {
            "step": "drop_missing_target_Weekly_Sales",
            "rows_before": before,
            "rows_after": len(df),
            "rows_removed": before - len(df),
            "reason": "La cible y ne doit jamais être imputée.",
        }
    )

    # 6.2 Date -> Year, Month, Day, DayOfWeek. errors="coerce" : une date invalide devient NaT,
    # les valeurs calendaires manquantes seront imputées ensuite dans le Pipeline.
    df["DateParsed"] = pd.to_datetime(df["Date"], format="%d-%m-%Y", errors="coerce")
    df["Year"] = df["DateParsed"].dt.year
    df["Month"] = df["DateParsed"].dt.month
    df["Day"] = df["DateParsed"].dt.day
    # DayOfWeek : lundi = 0, dimanche = 6 (convention de pandas).
    df["DayOfWeek"] = df["DateParsed"].dt.dayofweek
    report_rows.append(
        {
            "step": "create_calendar_features",
            "rows_before": len(df),
            "rows_after": len(df),
            "rows_removed": 0,
            "reason": "La date brute n'est pas exploitable directement par un modèle linéaire.",
        }
    )

    # 6.3 Filtrage selon moyenne ± n_sigma écarts-types, colonne par colonne.
    # keep_mask commence à True pour toutes les lignes et devient False dès qu'une ligne est
    # extrême sur une colonne au moins.
    keep_mask = pd.Series(True, index=df.index)
    outlier_rows = []
    for col in outlier_cols:
        # Moyenne et écart-type en ignorant les NaN (c'est le comportement par défaut de pandas) :
        # un NaN de X n'est pas un extrême, il sera imputé dans le Pipeline.
        mean = df[col].mean()
        std = df[col].std()
        lower = mean - n_sigma * std
        upper = mean + n_sigma * std

        # between renvoie False hors de l'intervalle ; notna() évite de confondre manquant et
        # valeur extrême.
        is_outlier = (~df[col].between(lower, upper)) & df[col].notna()

        # Comptage cumulatif : une ligne déjà exclue par une colonne n'est pas recomptée.
        rows_before_filter = int(keep_mask.sum())
        candidate_keep_mask = keep_mask & ~is_outlier
        rows_after_filter = int(candidate_keep_mask.sum())
        rows_removed_filter = rows_before_filter - rows_after_filter
        keep_mask = candidate_keep_mask

        outlier_rows.append(
            {
                "variable": col,
                "mean": mean,
                "std": std,
                "lower_bound": lower,
                "upper_bound": upper,
                "outliers_removed": rows_removed_filter,
            }
        )
        report_rows.append(
            {
                "step": f"outlier_filter_{col}",
                "rows_before": rows_before_filter,
                "rows_after": rows_after_filter,
                "rows_removed": rows_removed_filter,
                "reason": f"Règle imposée par l'énoncé : moyenne ± {n_sigma:g} écarts-types.",
            }
        )

    # Application finale du masque cumulé.
    before = len(df)
    df = df.loc[keep_mask].copy()
    report_rows.append(
        {
            "step": "after_all_outlier_filters",
            "rows_before": before,
            "rows_after": len(df),
            "rows_removed": before - len(df),
            "reason": "Dataset prêt pour X/y et pipeline sklearn.",
        }
    )

    # La date brute et la colonne datetime intermédiaire sont déjà converties : les garder créerait
    # une redondance, et une chaîne non encodée casserait la branche numérique.
    df = df.drop(columns=["Date", "DateParsed"])
    return df, pd.DataFrame(report_rows), pd.DataFrame(outlier_rows)


@dataclass(frozen=True)
class ConsequencesPretraitement:
    """Ce que la règle des écarts-types a retiré, et la borne haute du chômage si elle s'applique.

    lignes_retirees : lignes avec cible que la règle a supprimées (index de df_raw).
    magasins_avant, magasins_apres : nombre de magasins avant et après la règle.
    magasins_perdus : magasins supprimés EN ENTIER, triés, en entiers.
    borne_haute_chomage : borne haute de Unemployment, ou None si Unemployment n'est pas dans la
    règle (ce cas faisait planter la cellule 9 avec un IndexError).
    """

    lignes_retirees: pd.DataFrame
    magasins_avant: int
    magasins_apres: int
    magasins_perdus: list[int]
    borne_haute_chomage: float | None


def consequences_pretraitement(
    df_raw: pd.DataFrame, model_df: pd.DataFrame, outlier_report: pd.DataFrame
) -> ConsequencesPretraitement:
    """Compare le brut au jeu nettoyé : lignes retirées par la règle, magasins perdus, borne."""
    avec_cible = df_raw.dropna(subset=["Weekly_Sales"])
    retirees = avec_cible.drop(index=model_df.index)

    magasins_avant = set(avec_cible["Store"])
    magasins_apres = set(model_df["Store"])
    magasins_perdus = sorted(int(s) for s in magasins_avant - magasins_apres)

    borne_haute: float | None = None
    if "variable" in outlier_report.columns:
        masque_chomage = outlier_report["variable"] == "Unemployment"
        if masque_chomage.any():
            borne_haute = float(outlier_report.loc[masque_chomage, "upper_bound"].iloc[0])
    return ConsequencesPretraitement(
        retirees, len(magasins_avant), len(magasins_apres), magasins_perdus, borne_haute
    )


# --------------------------------------------------------------------------------------------
# Pipeline, métriques, choix d'alpha
# --------------------------------------------------------------------------------------------


def build_pipeline(model: Any, preprocessor: Any) -> Pipeline:
    """Assemble prétraitement et modèle dans un pipeline unique.

    clone(preprocessor) : chaque modèle a son propre objet de prétraitement. Sinon plusieurs
    pipelines partageraient un état interne après fit, ce qui fausserait les comparaisons.
    """
    return Pipeline(
        steps=[
            ("preprocess", clone(preprocessor)),
            ("model", model),
        ]
    )


def rmse(y_true: Any, y_pred: Any) -> float:
    """RMSE dans l'unité de la cible (racine de l'erreur quadratique moyenne)."""
    return float(np.sqrt(mean_squared_error(y_true, y_pred)))


# Scorer RMSE compatible avec GridSearchCV : scikit-learn maximise les scores, donc
# greater_is_better=False rend le RMSE négatif (les best_score_ se lisent en prenant l'opposé).
RMSE_SCORER = make_scorer(rmse, greater_is_better=False)


def evaluate_model(
    name: str,
    fitted_pipeline: Pipeline,
    x_train: pd.DataFrame,
    x_test: pd.DataFrame,
    y_train: pd.Series,
    y_test: pd.Series,
) -> dict[str, float | str]:
    """Évalue un pipeline déjà entraîné sur l'apprentissage ET sur le test.

    Train : capacité à apprendre les données vues. Test : généralisation. L'écart test - train
    du RMSE signale un risque de sur-apprentissage.
    """
    pred_train = fitted_pipeline.predict(x_train)
    pred_test = fitted_pipeline.predict(x_test)
    train_rmse = rmse(y_train, pred_train)
    test_rmse = rmse(y_test, pred_test)
    return {
        "model": name,
        "train_R2": float(r2_score(y_train, pred_train)),
        "test_R2": float(r2_score(y_test, pred_test)),
        "train_RMSE": train_rmse,
        "test_RMSE": test_rmse,
        "train_MAE": float(mean_absolute_error(y_train, pred_train)),
        "test_MAE": float(mean_absolute_error(y_test, pred_test)),
        "overfit_gap_RMSE": test_rmse - train_rmse,
    }


def chercher_alpha(
    estimateur: Any,
    alphas: Any,
    decoupage: Any,
    x_app: pd.DataFrame,
    y_app: pd.Series,
    preprocessor: Any,
) -> GridSearchCV:
    """Choisit alpha par validation croisée sur (x_app, y_app) uniquement.

    Rend le GridSearchCV ajusté ; `best_estimator_` est le pipeline complet (prétraitement +
    modèle) réajusté sur tout (x_app, y_app) avec le meilleur alpha.
    """
    recherche = GridSearchCV(
        build_pipeline(estimateur, preprocessor),
        param_grid={"model__alpha": alphas},
        scoring=RMSE_SCORER,
        cv=decoupage,
        n_jobs=1,  # n_jobs=1 maximise la portabilité
        return_train_score=True,  # utile pour auditer le sur-apprentissage
    )
    return recherche.fit(x_app, y_app)


def fr(valeur: float, decimales: int = 0) -> str:
    """Nombre au format français : espace pour les milliers."""
    return f"{valeur:,.{decimales}f}".replace(",", " ")


# --------------------------------------------------------------------------------------------
# Colinéarité et lecture des coefficients
# --------------------------------------------------------------------------------------------


def r2_regression(z: pd.DataFrame, colonne: str, explicatives: list[str]) -> float:
    """R² de la régression linéaire (avec constante) de `colonne` sur `explicatives`.

    Lève ValueError si `colonne` est constante : il n'y a alors aucune variance à expliquer.
    """
    cible = z[colonne].to_numpy()
    variance_totale = ((cible - cible.mean()) ** 2).sum()
    if variance_totale == 0:
        raise ValueError(f"la colonne {colonne!r} est constante : R² indéfini")
    matrice = np.column_stack([np.ones(len(z)), z[explicatives].to_numpy()])
    # rcond n'est pas précisé : depuis NumPy 2.0 son défaut est None (précision machine).
    beta = np.linalg.lstsq(matrice, cible)[0]
    residus = cible - matrice @ beta
    return float(1 - (residus**2).sum() / variance_totale)


def part_entre_magasins(x: pd.DataFrame, variable: str) -> float:
    """Part de la variance d'une variable (valeurs OBSERVÉES, sans imputation) due au magasin.

    Variance des moyennes par magasin / variance totale.
    Lève ValueError si la variable est constante (variance totale nulle).
    """
    observe = x[["Store", variable]].dropna()
    moyennes = observe.groupby("Store")[variable].transform("mean")
    centre = observe[variable] - observe[variable].mean()
    variance_totale = (centre**2).sum()
    if variance_totale == 0:
        raise ValueError(f"la variable {variable!r} est constante : part indéfinie")
    return float(((moyennes - observe[variable].mean()) ** 2).sum() / variance_totale)


def phrase_variable(
    nom: str, intervalles: pd.DataFrame, vif_par_nom: pd.Series, entre_par_nom: pd.Series
) -> str:
    """Une phrase de lecture pour une variable : lien avec le magasin, puis intervalle."""
    ligne = intervalles.loc[f"num__{nom}"]
    vif, entre = vif_par_nom[nom], entre_par_nom[nom]
    if vif > 10:
        lien = f"variable colinéaire avec les autres colonnes (VIF = {vif:.1f}, au-dessus de 10)"
    elif entre > 0.5:
        lien = (
            f"variable fortement liée au magasin ({entre:.0%} de sa variance observée est "
            f"entre magasins, VIF = {vif:.1f}, sous le seuil de 10)"
        )
    else:
        lien = (
            f"variable peu liée au magasin ({entre:.0%} de sa variance observée est entre "
            f"magasins, VIF = {vif:.1f})"
        )
    verdict = (
        "il exclut 0 : le signe est établi, mais la valeur est très incertaine"
        if ligne["signe établi"]
        else "il contient 0 : ni le signe ni l'ordre de grandeur ne sont établis"
    )
    return (
        f"- **{nom}** : {lien}. Coefficient {fr(ligne['coefficient'])} $ par écart-type, "
        f"intervalle à 95 % [{fr(ligne['ic95_bas'])} ; {fr(ligne['ic95_haut'])}] : {verdict}."
    )


# --------------------------------------------------------------------------------------------
# Stabilité sur plusieurs découpages, diagnostic
# --------------------------------------------------------------------------------------------


def evaluer_un_decoupage(
    graine: int,
    x: pd.DataFrame,
    y: pd.Series,
    preprocessor: Any,
    modeles: dict[str, tuple[Any, Any] | None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rejoue la procédure complète sur le découpage `graine`.

    Rend (une ligne par modèle, messages d'avertissement).

    `modeles` associe un nom à None (régression linéaire simple) ou à (estimateur, grille
    d'alpha). Le magasin du test absent de l'apprentissage produit l'avertissement « unknown
    categories » : il est compté (colonne magasin_inconnu_au_test), pas affiché. Tout autre
    avertissement est rendu dans la liste de messages. Lève ValueError si `modeles` est vide.
    """
    if not modeles:
        raise ValueError("aucun modèle à évaluer : `modeles` est vide")
    x_app, x_val, y_app, y_val = train_test_split(x, y, test_size=0.20, random_state=graine)
    # 5 plis : c'est la valeur par défaut de KFold, que le test compare à un calcul direct.
    decoupage = KFold(shuffle=True, random_state=graine)
    lignes_graine: list[dict[str, Any]] = []
    messages: list[str] = []
    with warnings.catch_warnings(record=True) as avertissements:
        warnings.simplefilter("always")
        for nom, config in modeles.items():
            if config is None:
                pipeline = build_pipeline(LinearRegression(), preprocessor)
                scores = cross_validate(
                    clone(pipeline), x_app, y_app, cv=decoupage, scoring=RMSE_SCORER
                )
                rmse_cv, alpha = -scores["test_score"].mean(), np.nan
                pipeline.fit(x_app, y_app)
            else:
                recherche = chercher_alpha(
                    config[0], config[1], decoupage, x_app, y_app, preprocessor
                )
                rmse_cv = -recherche.best_score_
                alpha = recherche.best_params_["model__alpha"]
                pipeline = recherche.best_estimator_
            prediction = pipeline.predict(x_val)
            lignes_graine.append(
                {
                    "graine": graine,
                    "modele": nom,
                    "alpha": alpha,
                    "RMSE_CV": rmse_cv,
                    "R2_test": r2_score(y_val, prediction),
                    "RMSE_test": rmse(y_val, prediction),
                }
            )
    inconnue = False
    for avertissement in avertissements:
        texte = str(avertissement.message)
        if "unknown categories" in texte:
            inconnue = True
        else:
            messages.append(texte)
    for ligne in lignes_graine:
        ligne["magasin_inconnu_au_test"] = inconnue
    return lignes_graine, messages


def diagnostic_validation(moyenne_r2: float, ecart_app_val: float) -> str:
    """Lecture automatique du sur- ou sous-apprentissage en validation croisée.

    Seuils INDICATIFS (convention de lecture, pas une loi) :
    - SOUS-apprentissage : R² de validation inférieur à 0,5 ;
    - SUR-apprentissage marqué : écart apprentissage - validation supérieur à 0,10 ;
    - SUR-apprentissage modéré : écart supérieur à 0,02 (et au plus 0,10) ;
    - sinon : ni sous-apprentissage ni sur-apprentissage visible.

    Lève ValueError si l'une des deux valeurs est NaN : un NaN fait échouer toutes les
    comparaisons et serait lu, à tort, comme « ni sous- ni sur-apprentissage ».
    """
    if math.isnan(moyenne_r2) or math.isnan(ecart_app_val):
        raise ValueError("diagnostic impossible : une des valeurs est NaN")
    if moyenne_r2 < 0.5:
        return "R² de validation faible : risque de sous-apprentissage."
    if ecart_app_val > 0.10:
        return "sur-apprentissage marqué."
    if ecart_app_val > 0.02:
        return "sur-apprentissage modéré (petit jeu de données), pas de sous-apprentissage."
    return "ni sous-apprentissage ni sur-apprentissage visible."
