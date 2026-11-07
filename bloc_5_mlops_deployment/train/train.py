# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Entraînement du modèle de prix Getaround, suivi et versionné avec MLflow.

Ce que fait ce script, et pourquoi :
1. lit `data/get_around_pricing_project.csv` (4 843 voitures, prix de location par jour) ;
2. corrige deux anomalies vérifiées (kilométrage négatif, puissance nulle → valeurs manquantes,
   imputées par la médiane DANS le pipeline, donc apprises sur l'apprentissage seulement) ;
3. compare quatre modèles dans un même pipeline scikit-learn (prétraitement + modèle) : une
   référence naïve (prix moyen), une régression Ridge, une forêt aléatoire et un gradient
   boosting, chacun optimisé par recherche sur grille en validation croisée à 5 plis ;
4. journalise chaque modèle dans **MLflow** (paramètres, métriques, modèle avec sa signature et
   un exemple d'entrée) : chaque entraînement est traçable et rejouable (compétence C5.1) ;
5. inscrit le meilleur modèle au **registre MLflow** sous le nom `getaround-pricing` avec
   l'alias `champion` : en local, l'API peut charger « le champion » du registre, et changer de
   version revient à déplacer l'alias. Le « meilleur » est celui dont le **R² de validation
   croisée** (5 plis, sur l'apprentissage seulement) est le plus élevé, et non celui dont le RMSE
   de test est
   le plus bas : si l'on choisit sur le jeu de test, ce jeu cesse d'être neuf, et l'erreur
   annoncée pour le champion devient optimiste. Le jeu de test ne sert qu'à mesurer, une fois,
   le modèle déjà choisi ;
6. exporte aussi le champion en fichier (`models/model.joblib`), avec l'ordre des colonnes
   d'entrée (`models/colonnes.json`) et un exemple réel (`models/exemple.json`) : l'image
   Docker de l'API (et la production) embarque ces fichiers et fonctionne sans serveur MLflow.

Usage (depuis la racine du dépôt) : ``python train/train.py``, ou dans un conteneur avec
``train/Dockerfile`` (données et base MLflow montées en volume, jamais copiées dans l'image).
Variable d'environnement facultative : ``MLFLOW_TRACKING_URI`` (par défaut, une base SQLite
locale ``mlflow.db`` ; sinon, l'URL d'un serveur MLflow partagé).

CHOIX
-----
- Retenu : le champion est choisi sur le R² de validation croisée, calculé sur l'apprentissage
  seul. Écarté : le choisir sur le RMSE de test, qui userait le jeu de test (voir ci-dessus).
- Retenu : le prétraitement vit DANS le pipeline (imputation par la médiane, standardisation,
  encodage one-hot). Écarté : le faire avant la séparation apprentissage/test, qui ferait fuir
  des informations du jeu de test vers l'apprentissage.
- Retenu : ``handle_unknown="ignore"`` pour l'encodage. Écarté : laisser planter sur une marque
  absente de l'apprentissage.
- Retenu : le modèle est sérialisé par MLflow au format sécurisé ``skops`` avec EXACTEMENT les
  types qu'il contient en types de confiance. Écarté : le format pickle, qui exécuterait
  n'importe quel code au chargement d'un fichier modèle piégé.
- Retenu : ``n_jobs=1`` partout, graine 42 : un entraînement rejoué donne les mêmes métriques.
  Écarté : le parallélisme, qui accélérerait mais rendrait l'exécution moins reproductible.
- Retenu : ``main()`` ne fait qu'enchaîner des fonctions courtes (``entrainer``, ``mesurer``,
  ``journaliser``, ``enregistrer_champion``, ``exporter``).

OÙ
---
- ``train/train.py``. Lancé à la main depuis la racine du dépôt. Écrit ``models/`` (lu par
  api/main.py au démarrage, par dashboard/app.py pour categories.json) et la base MLflow
  (``mlflow.db`` ou ``MLFLOW_TRACKING_URI``).
- Dépendances : pandas, numpy, scikit-learn, MLflow, skops, joblib (requirements.txt, version de
  développement complet). Données : data/get_around_pricing_project.csv.

COMMENT
-------
1. ``charger`` lit le CSV, remplace par des valeurs manquantes les kilométrages négatifs et les
   puissances nulles, convertit les booléens en 0/1.
2. ``main`` sépare 80 % / 20 % (graine 42), puis pour chaque candidat de ``CANDIDATS`` :
   ``entrainer`` (recherche sur grille si une grille est donnée), ``mesurer`` (RMSE, MAE, R² de
   test, R² d'apprentissage, R² de validation croisée sur une copie non entraînée du modèle),
   ``journaliser`` (une exécution MLflow), puis ``meilleur_que`` décide du champion.
3. ``enregistrer_champion`` inscrit la version au registre et déplace l'alias ``champion``.
4. ``exporter`` écrit le modèle, les colonnes, l'exemple, la comparaison et l'importance des
   variables par permutation.

POURQUOI
--------
- Compétence C5.1 (standardiser la construction et l'environnement d'un algorithme avec MLflow
  et Docker) : référentiel du titre CDSD RNCP35288, bloc 5.
- Le champion versionné (gradient boosting, R² de validation croisée 0,768 contre 0,755) :
  models/comparaison_modeles.csv.
- Anomalies corrigées (un kilométrage négatif, une puissance nulle) : constatées dans les
  données, explorées dans la section 7 du notebook Getaround_Analyse_Retards.ipynb.
- Limite du format skops : ``skops_trusted_types`` est exigé au rechargement par MLflow 3.17.0 ;
  comportement mesuré au moment de l'écriture du script.

JOURNALISATION
--------------
Le script est un point d'entrée : il affiche sur la sortie standard (``print``) une ligne par
modèle, le champion retenu, la comparaison et l'importance des variables. MLflow journalise en
plus, par exécution : paramètres, métriques, modèle (signature, exemple d'entrée). Aucun
``logging`` applicatif : le suivi est celui de MLflow.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import joblib
import mlflow
import mlflow.sklearn
import pandas as pd
import skops.io as sio
from mlflow.models import infer_signature
from mlflow.tracking import MlflowClient
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score, root_mean_squared_error
from sklearn.model_selection import GridSearchCV, KFold, cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

RACINE = Path(__file__).resolve().parent.parent
GRAINE = 42
CIBLE = "rental_price_per_day"
CATEGORIELLES = ["model_key", "fuel", "paint_color", "car_type"]
NUMERIQUES = ["mileage", "engine_power"]
BOOLEENNES = [
    "private_parking_available",
    "has_gps",
    "has_air_conditioning",
    "automatic_car",
    "has_getaround_connect",
    "has_speed_regulator",
    "winter_tires",
]
# Ordre des colonnes attendu par l'API (une ligne de "input" suit exactement cet ordre).
COLONNES = ["model_key", "mileage", "engine_power", "fuel", "paint_color", "car_type", *BOOLEENNES]
NOM_REGISTRE = "getaround-pricing"


def en_python(valeur: Any) -> Any:
    """Un scalaire numpy devient un nombre Python (``.item()``) ; le reste est rendu tel quel."""
    return valeur.item() if hasattr(valeur, "item") else valeur


def charger() -> tuple[pd.DataFrame, pd.Series[float]]:
    """Lit les données, corrige les anomalies, renvoie les variables et la cible."""
    donnees = pd.read_csv(RACINE / "data" / "get_around_pricing_project.csv", index_col=0)
    # Un kilométrage négatif ou une puissance nulle sont impossibles : valeurs manquantes.
    donnees["mileage"] = donnees["mileage"].mask(donnees["mileage"] < 0)
    donnees["engine_power"] = donnees["engine_power"].mask(donnees["engine_power"] == 0)
    x = donnees[COLONNES].copy()
    x[BOOLEENNES] = x[BOOLEENNES].astype(int)  # vrai/faux → 1/0
    return x, donnees[CIBLE]


def pretraitement() -> ColumnTransformer:
    """Prétraitement commun : imputation et standardisation des numériques, encodage des
    catégories (une catégorie inconnue à la prédiction est ignorée au lieu de planter)."""
    return ColumnTransformer(
        [
            (
                "numeriques",
                Pipeline(
                    [
                        ("imputation", SimpleImputer(strategy="median")),
                        ("echelle", StandardScaler()),
                    ]
                ),
                NUMERIQUES,
            ),
            (
                "categories",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                CATEGORIELLES,
            ),
            ("booleens", "passthrough", BOOLEENNES),
        ]
    )


def meilleur_que(mesures: dict[str, Any], champion: dict[str, Any] | None) -> bool:
    """Vrai si ce modèle doit devenir le champion : son R² de validation croisée est le plus haut.

    Le critère est calculé sur l'apprentissage seulement (5 plis) : le jeu de test reste neuf pour
    mesurer honnêtement le champion une fois choisi. Sans champion encore (premier candidat), le
    modèle l'est d'office. En cas d'égalité exacte, le premier candidat gardé l'emporte.
    """
    return champion is None or (
        mesures["r2_validation_croisee"] > champion["r2_validation_croisee"]
    )


# Modèles comparés et grilles d'hyperparamètres (volontairement compactes : 5 plis × grille).
CANDIDATS: dict[str, tuple[Any, dict[str, list[Any]]]] = {
    "reference_prix_moyen": (DummyRegressor(strategy="mean"), {}),
    "ridge": (Ridge(), {"modele__alpha": [0.1, 1.0, 10.0, 100.0]}),
    "foret_aleatoire": (
        RandomForestRegressor(n_estimators=300, random_state=GRAINE, n_jobs=1),
        {"modele__max_depth": [None, 15], "modele__min_samples_leaf": [1, 3]},
    ),
    "gradient_boosting": (
        HistGradientBoostingRegressor(random_state=GRAINE),
        {
            "modele__learning_rate": [0.05, 0.1],
            "modele__max_leaf_nodes": [15, 31],
            "modele__max_iter": [300],
        },
    ),
}


def entrainer(
    modele: Any,
    grille: dict[str, list[Any]],
    x_app: pd.DataFrame,
    y_app: pd.Series[float],
    plis: KFold,
) -> tuple[Any, dict[str, Any]]:
    """Ajuste un candidat dans son pipeline ; rend le modèle ajusté et ses hyperparamètres.

    Avec une grille, la recherche (validation croisée, RMSE) choisit les hyperparamètres ; sans
    grille (référence naïve), le pipeline est ajusté tel quel et les paramètres sont vides.
    """
    pipeline = Pipeline([("pretraitement", pretraitement()), ("modele", modele)])
    if grille:
        recherche = GridSearchCV(
            pipeline, grille, cv=plis, n_jobs=1, scoring="neg_root_mean_squared_error"
        )
        recherche.fit(x_app, y_app)
        return recherche.best_estimator_, recherche.best_params_
    return pipeline.fit(x_app, y_app), {}


def mesurer(
    ajuste: Any,
    x_app: pd.DataFrame,
    y_app: pd.Series[float],
    x_test: pd.DataFrame,
    y_test: pd.Series[float],
    plis: KFold,
) -> tuple[dict[str, float], Any]:
    """Métriques d'un modèle ajusté, et ses prédictions sur le jeu de test.

    Le R² de validation croisée est calculé sur une COPIE non entraînée du modèle : le modèle
    ajusté n'est pas modifié (sinon on enregistrerait un modèle réentraîné sur un seul pli).
    """
    r2_vc = cross_val_score(clone(ajuste), x_app, y_app, cv=plis, scoring="r2", n_jobs=1)
    prediction = ajuste.predict(x_test)
    mesures = {
        "rmse_test": root_mean_squared_error(y_test, prediction),
        "mae_test": mean_absolute_error(y_test, prediction),
        "r2_test": r2_score(y_test, prediction),
        "r2_validation_croisee": float(r2_vc.mean()),
        "r2_validation_croisee_ecart_type": float(r2_vc.std()),
        "r2_apprentissage": r2_score(y_app, ajuste.predict(x_app)),
    }
    return mesures, prediction


def journaliser(
    nom: str,
    ajuste: Any,
    parametres: dict[str, Any],
    mesures: dict[str, float],
    x_test: pd.DataFrame,
    prediction: Any,
) -> tuple[str, str]:
    """Une exécution MLflow par modèle ; rend l'adresse du modèle journalisé et l'exécution."""
    with mlflow.start_run(run_name=nom) as execution:
        mlflow.log_params(
            {"modele": nom, **{k.replace("modele__", ""): v for k, v in parametres.items()}}
        )
        mlflow.log_metrics(mesures)
        # MLflow sérialise au format sécurisé « skops », qui refuse au rechargement tout type
        # non déclaré (protection contre un fichier modèle piégé). Ici, le modèle vient d'être
        # créé par ce script même : on déclare de confiance EXACTEMENT les types qu'il
        # contient (calculés par skops : numpy.dtype, structures d'arbres...), plutôt que de
        # revenir au format pickle, qui exécuterait n'importe quel code au chargement.
        info = mlflow.sklearn.log_model(
            ajuste,
            name="modele",
            signature=infer_signature(x_test, prediction),
            input_example=x_test.head(2),
            skops_trusted_types=sio.get_untrusted_types(data=sio.dumps(ajuste)),
        )
    return info.model_uri, execution.info.run_id


def enregistrer_champion(uri: str) -> str:
    """Nouvelle version du modèle au registre MLflow, alias « champion » posé dessus."""
    version = mlflow.register_model(uri, NOM_REGISTRE)
    MlflowClient().set_registered_model_alias(NOM_REGISTRE, "champion", version.version)
    return str(version.version)


def exporter(
    dossier: Path,
    champion: Any,
    x_test: pd.DataFrame,
    y_test: pd.Series[float],
    resultats: list[dict[str, Any]],
) -> pd.Series[float]:
    """Export autonome pour l'API (image Docker sans serveur MLflow) et tableaux de comparaison.

    Écrit le champion, l'ordre des colonnes, un exemple réel et les deux CSV ; rend le classement
    des variables par importance de permutation sur le jeu de test.
    """
    dossier.mkdir(exist_ok=True)
    joblib.dump(champion, dossier / "model.joblib")
    (dossier / "colonnes.json").write_text(json.dumps(COLONNES, indent=2), encoding="utf-8")
    exemple = x_test.head(2).astype(object)
    exemple[BOOLEENNES] = exemple[BOOLEENNES].astype(bool)
    # Accents gardés, scalaires numpy convertis en types Python.
    (dossier / "exemple.json").write_text(
        json.dumps(
            {"input": exemple.to_numpy().tolist()},
            ensure_ascii=False,
            indent=2,
            default=en_python,
        ),
        encoding="utf-8",
    )
    # Variables les plus influentes du champion (importance par permutation sur le test).
    importance = permutation_importance(
        champion,
        x_test,
        y_test,
        n_repeats=5,
        random_state=GRAINE,
        scoring="neg_root_mean_squared_error",
    )
    classement = pd.Series(importance.importances_mean, index=COLONNES).sort_values(
        ascending=False
    )
    pd.DataFrame(resultats).to_csv(dossier / "comparaison_modeles.csv", index=False)
    classement.round(3).to_csv(dossier / "importance_variables.csv", header=["hausse_rmse_eur"])
    return classement


def main() -> None:
    # Console Windows : forcer l'UTF-8 pour afficher les accents et les flèches.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    mlflow.set_tracking_uri(
        os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{(RACINE / 'mlflow.db').as_posix()}")
    )
    mlflow.set_experiment("getaround-pricing")
    x, y = charger()
    x_app, x_test, y_app, y_test = train_test_split(x, y, test_size=0.2, random_state=GRAINE)
    plis = KFold(n_splits=5, shuffle=True, random_state=GRAINE)
    resultats: list[dict[str, Any]] = []
    meilleur: dict[str, Any] | None = None

    for nom, (modele, grille) in CANDIDATS.items():
        ajuste, parametres = entrainer(modele, grille, x_app, y_app, plis)
        mesures, prediction = mesurer(ajuste, x_app, y_app, x_test, y_test, plis)
        uri, _ = journaliser(nom, ajuste, parametres, mesures, x_test, prediction)
        resultats.append({"modele": nom, **{k: round(v, 4) for k, v in mesures.items()}})
        print(
            f"{nom:<22} RMSE test {mesures['rmse_test']:6.2f} € | MAE {mesures['mae_test']:6.2f} "
            f"€ | R² test {mesures['r2_test']:.3f} | R² VC {mesures['r2_validation_croisee']:.3f}"
        )
        if meilleur_que(mesures, meilleur):
            meilleur = {"nom": nom, "modele": ajuste, "uri": uri, **mesures}

    if meilleur is None:
        raise RuntimeError("aucun candidat à entraîner : CANDIDATS est vide")
    # Registre MLflow : nouvelle version du modèle, alias « champion » posé dessus.
    version = enregistrer_champion(meilleur["uri"])
    print(
        f"\nChampion (meilleur R² de validation croisée) : {meilleur['nom']} → registre "
        f"« {NOM_REGISTRE} », version {version}, alias « champion »"
    )
    classement = exporter(RACINE / "models", meilleur["modele"], x_test, y_test, resultats)
    print("\nComparaison :\n", pd.DataFrame(resultats).to_string(index=False))
    print(
        "\nImportance par permutation (hausse du RMSE en € si la variable est brouillée) :\n",
        classement.round(2).to_string(),
    )


if __name__ == "__main__":
    main()
