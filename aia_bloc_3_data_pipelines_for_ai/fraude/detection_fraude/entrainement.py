# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Entraînement reproductible du modèle, suivi MLflow, export et promotion — critères 2.2, 3.3.

L'énoncé le dit : l'essentiel est le pipeline de données, pas l'algorithme. Le modèle est donc
volontairement simple, mais entraîné *proprement* :

* les données passent par le **même code** que le flux de production (validation, masquage,
  caractéristiques) : aucun écart entre ce que le modèle apprend et ce qu'il verra ;
* le découpage est **chronologique** (70 % / 15 % / 15 %) : on entraîne sur le passé, on règle le
  seuil sur la période suivante, on mesure sur la plus récente, comme en production ;
* la **précision est fixée** (``precision_cible``) et l'on mesure le rappel : en fraude, la
  précision est la contrainte métier (chaque alerte coûte du temps d'analyste) ;
* tout est tracé dans MLflow : paramètres, empreinte des données, métriques, modèle ;
* un **candidat** n'est pas le modèle en service : ``valider_candidat`` puis ``promouvoir_version``
  (appelés par le DAG hebdomadaire) imposent une barrière de qualité avant bascule.

CHOIX
-----
- Retenu : deux familles de candidats aux prétraitements différents parce que leurs mathématiques
  l'exigent. Régression logistique : ``log1p`` des variables à longue traîne puis
  standardisation, montant découpé en 12 tranches, heure, jour, catégorie et État en
  indicatrices. Gradient boosting par histogrammes : aucune mise à l'échelle (invariant aux
  transformations monotones), catégories encodées par rang et déclarées catégorielles.
- Retenu : le meilleur candidat est celui de plus haute PR-AUC de validation (un NaN compte 0).
- Retenu : le seuil est choisi sur la validation (meilleur rappel à précision au moins égale à la
  cible, sinon meilleur F1) ; le test ne sert qu'à mesurer.
- Retenu : ``subsample=None`` pour ``KBinsDiscretizer`` : sans cela scikit-learn tire au hasard
  200 000 lignes et le résultat n'est plus reproductible.
- Retenu : une barrière de qualité avant promotion : plancher de PR-AUC et non-régression.
- Écarté : mélanger aléatoirement une série temporelle ; promouvoir automatiquement un candidat
  sans barrière.

OÙ
---
- Place : lot hebdomadaire (DAG ``fraude_reentrainement_hebdo``) et amorçage du mode local.
- Appelants : entrainer_candidats (detection_fraude/taches_airflow.py:227-231),
  valider_barriere_qualite (detection_fraude/taches_airflow.py:234-243), preparer
  (detection_fraude/local.py:117-124), ``main`` et les tests.
- Dépendances : scikit-learn, pandas, NumPy, MLflow (importé à la demande), ``jeu_donnees``,
  ``fonctionnalites``, ``masquage``, ``modele``, ``configuration`` et ``journal``.

COMMENT
-------
1. ``entrainer`` lit le jeu, le valide et le masque, le découpe chronologiquement, ajuste chaque
   candidat sur l'entraînement, fixe le seuil sur la validation, évalue sur le test, choisit le
   meilleur, exporte une version et, sous ``promouvoir_si_premier``, la promeut s'il n'y a pas
   encore de modèle en service.
2. ``valider_candidat`` refuse un candidat dont la PR-AUC de test est sous le plancher (un NaN
   est refusé) ou inférieure de plus de la tolérance à celle du modèle en service.
3. ``promouvoir_version`` fait basculer le pointeur ``ACTUEL``.
Tests qui le prouvent : test_rappel_a_precision_fixee_sur_un_cas_calculable_a_la_main
(tests/test_entrainement.py:38-48), test_decoupage_chronologique_sans_chevauchement
(tests/test_entrainement.py:71-78), test_barriere_refuse_sous_le_plancher
(tests/test_entrainement.py:226-230), test_barriere_refuse_une_regression
(tests/test_entrainement.py:233-240),
test_deux_entrainements_avec_la_meme_graine_donnent_les_memes_scores
(tests/test_entrainement.py:148-160) et
test_barriere_accepte_la_regression_toleree_et_laisse_les_versions_intactes
(tests/test_entrainement_garanties.py:1115-1137),
test_pipeline_gradient_boosting_a_la_structure_exacte_et_laisse_les_hyperparametres_intacts
(tests/test_entrainement_garanties.py:431-470) et
test_main_refuse_une_configuration_invalide_avant_de_configurer_le_journal
(tests/test_entrainement_cli.py:289-303).

POURQUOI
--------
- Besoins mathématiques des modèles, protocole d'entraînement et résultats mesurés :
  livrables/aia3/02_pipeline_et_qualite.md:38-107.
- Barrière de qualité (plancher 0,30, tolérance 0,02) et promotion atomique :
  livrables/aia3/02_pipeline_et_qualite.md:166-187.
- Valeurs du plancher et de la tolérance : config/base.yaml:39.
- Manipulations du 11/10/2026 sur la barrière et sur la précision cible : compte rendu des
  épreuves non publié ; code : valider_candidat (detection_fraude/entrainement.py:532-567).
- Indicateurs 2.2 et 3.3 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.entrainement``, INFO) : ``jeu_prepare`` (``lignes_lues``,
``lignes_retenues``, ``rejets``), ``candidat_evalue`` (``modele``, ``pr_auc_validation``,
``pr_auc_test``, ``duree_s``), ``modele_promu_amorcage`` (``version``), ``entrainement_termine``
(``version``, ``modele``, ``duree_s``, ``pr_auc_test``), ``barriere_qualite`` (``version``,
``pr_auc_test``, ``pr_auc_en_service``, ``accepte``, ``raisons``) et ``modele_promu``
(``version``). Les paramètres, métriques et l'empreinte du jeu sont aussi tracés dans MLflow.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import (
    FunctionTransformer,
    KBinsDiscretizer,
    OneHotEncoder,
    OrdinalEncoder,
    StandardScaler,
)

from detection_fraude import VERSION_REGLES
from detection_fraude.configuration import Configuration, charger_configuration
from detection_fraude.fonctionnalites import COLONNES_CATEGORIELLES, COLONNES_MODELE
from detection_fraude.jeu_donnees import lire_jeu_brut, preparer_jeu_entrainement
from detection_fraude.journal import configurer_journal, info, obtenir_journal
from detection_fraude.masquage import HacheurCarte
from detection_fraude.modele import (
    empreinte_fichier,
    exporter_modele,
    lire_metadonnees,
    nouvelle_version,
    promouvoir,
    version_en_service,
)

_journal = obtenir_journal("entrainement")

COLONNES_LONGUE_TRAINEE = ["population_ville", "distance_km", "nb_tx_24h", "montant_relatif"]
COLONNES_LINEAIRES = ["age"]
COLONNES_CATEGORIES = list(COLONNES_CATEGORIELLES)
# Pour la régression logistique, heure et jour sont traités comme des catégories (indicatrices).
COLONNES_INDICATRICES = [*COLONNES_CATEGORIES, "heure", "jour_semaine"]
COLONNES_NUMERIQUES_ARBRES = [c for c in COLONNES_MODELE if c not in COLONNES_CATEGORIES]

HYPERPARAMETRES = {
    "regression_logistique": {"C": 1.0, "max_iter": 1000},
    "gradient_boosting": {
        "learning_rate": 0.1,
        "max_iter": 200,
        "max_leaf_nodes": 31,
        "l2_regularization": 1.0,
        "min_samples_leaf": 40,
    },
}


class BarriereQualiteRefusee(Exception):
    """Le candidat ne satisfait pas la barrière de qualité : il ne sera pas promu."""


# --------------------------------------------------------------------------------------------
# Pipelines scikit-learn
# --------------------------------------------------------------------------------------------
def construire_pipeline(nom: str, graine: int) -> Pipeline:
    """Pipeline complet (prétraitements + estimateur) : c'est lui qui est exporté."""
    if nom == "regression_logistique":
        pretraitement = ColumnTransformer(
            [
                (
                    "montant_en_tranches",
                    Pipeline(
                        [
                            ("log", FunctionTransformer(np.log1p, validate=False)),
                            (
                                "tranches",
                                KBinsDiscretizer(
                                    n_bins=12,
                                    encode="onehot",
                                    strategy="uniform",
                                    # Sans cela, scikit-learn tire au sort 200 000 lignes
                                    # pour fixer les bornes : résultat non reproductible.
                                    subsample=None,
                                ),
                            ),
                        ]
                    ),
                    ["montant"],
                ),
                (
                    "longue_traine",
                    Pipeline(
                        [
                            ("log", FunctionTransformer(np.log1p, validate=False)),
                            ("echelle", StandardScaler()),
                        ]
                    ),
                    COLONNES_LONGUE_TRAINEE,
                ),
                (
                    "lineaires",
                    Pipeline(
                        [
                            ("imputer", SimpleImputer(strategy="median")),
                            ("echelle", StandardScaler()),
                        ]
                    ),
                    COLONNES_LINEAIRES,
                ),
                (
                    "indicatrices",
                    OneHotEncoder(handle_unknown="ignore", min_frequency=20),
                    COLONNES_INDICATRICES,
                ),
            ]
        )
        estimateur: Any = LogisticRegression(random_state=graine, **HYPERPARAMETRES[nom])
    elif nom == "gradient_boosting":
        pretraitement = ColumnTransformer(
            [
                (
                    "categories",
                    OrdinalEncoder(
                        handle_unknown="use_encoded_value",
                        unknown_value=np.nan,
                        encoded_missing_value=np.nan,
                    ),
                    COLONNES_CATEGORIES,
                ),
                ("numeriques", "passthrough", COLONNES_NUMERIQUES_ARBRES),
            ]
        )
        masque_categoriel = [True] * len(COLONNES_CATEGORIES) + [False] * len(
            COLONNES_NUMERIQUES_ARBRES
        )
        estimateur = HistGradientBoostingClassifier(
            categorical_features=masque_categoriel, random_state=graine, **HYPERPARAMETRES[nom]
        )
    else:
        raise ValueError(f"Candidat inconnu : {nom}")
    return Pipeline([("pretraitement", pretraitement), ("estimateur", estimateur)])


# --------------------------------------------------------------------------------------------
# Mesures
# --------------------------------------------------------------------------------------------
def rappel_a_precision_fixee(
    y_vrai: np.ndarray, scores: np.ndarray, precision_cible: float
) -> dict[str, float | bool]:
    """Meilleur rappel atteignable avec une précision au moins égale à ``precision_cible``.

    Renvoie aussi le seuil correspondant. Si la cible est inatteignable, le seuil retenu est celui
    qui maximise le score F1 et ``cible_atteinte`` vaut ``False``.
    """
    precision, rappel, seuils = precision_recall_curve(y_vrai, scores)
    p, r = precision[:-1], rappel[:-1]  # le dernier point (1, 0) n'a pas de seuil associé
    admissibles = np.flatnonzero(p >= precision_cible)
    if admissibles.size:
        meilleur = admissibles[np.argmax(r[admissibles])]
        return {
            "rappel": float(r[meilleur]),
            "precision": float(p[meilleur]),
            "seuil": float(seuils[meilleur]),
            "cible_atteinte": True,
        }
    f1 = 2 * p * r / np.maximum(p + r, 1e-12)
    meilleur = int(np.argmax(f1))
    return {
        "rappel": float(r[meilleur]),
        "precision": float(p[meilleur]),
        "seuil": float(seuils[meilleur]),
        "cible_atteinte": False,
    }


def evaluer(y_vrai: np.ndarray, scores: np.ndarray, seuil: float, cible: float) -> dict[str, Any]:
    """Métriques d'un jeu : PR-AUC, ROC-AUC, rappel à précision fixée, mesures au seuil."""
    if len(set(y_vrai)) < 2:
        return {"pr_auc": float("nan"), "roc_auc": float("nan")}
    decisions = (scores >= seuil).astype(int)
    sur_courbe = rappel_a_precision_fixee(y_vrai, scores, cible)
    return {
        "pr_auc": float(average_precision_score(y_vrai, scores)),
        "roc_auc": float(roc_auc_score(y_vrai, scores)),
        "rappel_a_precision_fixee": sur_courbe["rappel"],
        "cible_atteinte_sur_courbe": sur_courbe["cible_atteinte"],
        "precision_au_seuil": float(precision_score(y_vrai, decisions, zero_division=0)),
        "rappel_au_seuil": float(recall_score(y_vrai, decisions, zero_division=0)),
        "nb_alertes_au_seuil": int(decisions.sum()),
        "nb_fraudes": int(np.sum(y_vrai)),
        "nb_lignes": int(len(y_vrai)),
    }


def decouper_chronologiquement(
    df: pd.DataFrame, fraction_entrainement: float, fraction_validation: float
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Trois tranches contiguës dans le temps (jamais de mélange aléatoire sur des séries)."""
    ordonne = df.sort_values(["horodatage_s", "trans_num"], kind="stable").reset_index(drop=True)
    n = len(ordonne)
    fin_entrainement = int(n * fraction_entrainement)
    fin_validation = int(n * (fraction_entrainement + fraction_validation))
    return (
        ordonne.iloc[:fin_entrainement],
        ordonne.iloc[fin_entrainement:fin_validation],
        ordonne.iloc[fin_validation:],
    )


# --------------------------------------------------------------------------------------------
# MLflow (importé à la demande : les tests sans suivi n'en ont pas besoin)
# --------------------------------------------------------------------------------------------
def _preparer_mlflow(config: Configuration) -> Any:
    import mlflow

    mlflow.set_tracking_uri(config.mlflow.tracking_uri)
    if not config.mlflow.tracking_uri.startswith("http"):
        # Magasin local : on range explicitement les artefacts dans le dossier de données.
        client = mlflow.MlflowClient()
        if client.get_experiment_by_name(config.mlflow.experience) is None:
            emplacement = (config.racine_donnees / "mlartifacts").resolve().as_uri()
            client.create_experiment(config.mlflow.experience, artifact_location=emplacement)
    mlflow.set_experiment(config.mlflow.experience)
    return mlflow


def _journaliser_modele(
    mlflow: Any, pipeline: Pipeline, exemple: pd.DataFrame, enregistrer: bool
) -> Any:
    """Compatible MLflow 2 (``artifact_path``) et 3 (``name``)."""
    options: dict[str, Any] = {
        # Les entiers deviennent des flottants : une valeur manquante à l'inférence ne doit pas
        # faire échouer la vérification de schéma de MLflow.
        "input_example": exemple.astype(dict.fromkeys(exemple.select_dtypes("number"), "float64")),
        "serialization_format": "cloudpickle",
        "pip_requirements": [
            f"scikit-learn=={sklearn.__version__}",
            f"numpy=={np.__version__}",
            f"pandas=={pd.__version__}",
        ],
    }
    if enregistrer:
        options["registered_model_name"] = "detection-fraude"
    try:
        return mlflow.sklearn.log_model(pipeline, name="modele", **options)
    except TypeError:
        return mlflow.sklearn.log_model(pipeline, artifact_path="modele", **options)


# --------------------------------------------------------------------------------------------
# Entraînement
# --------------------------------------------------------------------------------------------
def entrainer(
    config: Configuration,
    chemin_donnees: Path | None = None,
    *,
    limite: int | None = None,
    suivre_mlflow: bool = True,
    promouvoir_si_premier: bool = False,
) -> dict[str, Any]:
    """Entraîne les candidats, choisit le meilleur, exporte une version (sans la promouvoir).

    Si ``promouvoir_si_premier`` est vrai et qu'aucun modèle n'est en service, la version est
    promue d'office (amorçage d'un environnement vierge).
    """
    debut = time.monotonic()
    chemin = chemin_donnees or config.source.jeu_chemin
    hacheur = HacheurCarte(config.sel_hachage(), config.longueur_hash)

    brut = lire_jeu_brut(chemin, limite=limite)
    jeu, rejets = preparer_jeu_entrainement(brut, hacheur)
    info(
        _journal,
        "jeu_prepare",
        lignes_lues=len(brut),
        lignes_retenues=len(jeu),
        rejets=sum(rejets.values()),
    )
    entrainement, validation, test = decouper_chronologiquement(
        jeu, config.modele.fraction_entrainement, config.modele.fraction_validation
    )
    colonnes = list(COLONNES_MODELE)
    y_ent, y_val, y_test = (
        t["etiquette_source"].to_numpy(dtype=int) for t in (entrainement, validation, test)
    )
    date_coupure = (
        datetime.fromtimestamp(int(test["horodatage_s"].iloc[0]), UTC).date().isoformat()
    )
    version = nouvelle_version()
    cible = config.modele.precision_cible

    mlflow = _preparer_mlflow(config) if suivre_mlflow else None
    run_parent = mlflow.start_run(run_name=f"entrainement_{version}") if mlflow else None
    resultats: dict[str, dict[str, Any]] = {}
    pipelines: dict[str, Pipeline] = {}
    try:
        for nom in config.modele.candidats:
            debut_candidat = time.monotonic()
            pipeline = construire_pipeline(nom, config.modele.graine)
            pipeline.fit(entrainement[colonnes], y_ent)
            scores_val = pipeline.predict_proba(validation[colonnes])[:, 1]
            seuil = rappel_a_precision_fixee(y_val, scores_val, cible)
            metriques_val = evaluer(y_val, scores_val, seuil["seuil"], cible)
            scores_test = pipeline.predict_proba(test[colonnes])[:, 1]
            metriques_test = evaluer(y_test, scores_test, seuil["seuil"], cible)
            duree = time.monotonic() - debut_candidat
            resultats[nom] = {
                "seuil": seuil["seuil"],
                "validation": metriques_val,
                "test": metriques_test,
                "duree_s": duree,
            }
            pipelines[nom] = pipeline
            info(
                _journal,
                "candidat_evalue",
                modele=nom,
                pr_auc_validation=round(metriques_val["pr_auc"], 4),
                pr_auc_test=round(metriques_test["pr_auc"], 4),
                duree_s=round(duree, 1),
            )
            if mlflow:
                with mlflow.start_run(run_name=nom, nested=True):
                    mlflow.log_params({f"hp_{k}": v for k, v in HYPERPARAMETRES[nom].items()})
                    mlflow.log_param("candidat", nom)
                    mlflow.log_metric("seuil_decision", seuil["seuil"])
                    mlflow.log_metric("duree_entrainement_s", duree)
                    for jeu_nom, metriques in (
                        ("validation", metriques_val),
                        ("test", metriques_test),
                    ):
                        for cle, valeur in metriques.items():
                            if isinstance(valeur, int | float) and not isinstance(valeur, bool):
                                mlflow.log_metric(f"{cle}_{jeu_nom}", float(valeur))

        choisi = max(resultats, key=lambda n: np.nan_to_num(resultats[n]["validation"]["pr_auc"]))
        seuil_final = resultats[choisi]["seuil"]
        metadonnees: dict[str, Any] = {
            "version": version,
            "cree_le": datetime.now(UTC).isoformat(timespec="seconds"),
            "modele": choisi,
            "hyperparametres": HYPERPARAMETRES[choisi],
            "precision_cible": cible,
            "metriques_validation": resultats[choisi]["validation"],
            "metriques_test": resultats[choisi]["test"],
            "comparaison_candidats": {
                n: {
                    "pr_auc_validation": r["validation"]["pr_auc"],
                    "pr_auc_test": r["test"]["pr_auc"],
                }
                for n, r in resultats.items()
            },
            "date_coupure_test": date_coupure,
            "decoupage": {
                "entrainement": len(entrainement),
                "validation": len(validation),
                "test": len(test),
            },
            "colonnes": colonnes,
            "graine": config.modele.graine,
            "version_regles": VERSION_REGLES,
            "empreinte_donnees_sha256": empreinte_fichier(chemin),
            "bibliotheques": {
                "python": platform.python_version(),
                "scikit-learn": sklearn.__version__,
                "numpy": np.__version__,
                "pandas": pd.__version__,
            },
            "rejets_a_la_preparation": dict(rejets),
        }
        if mlflow and run_parent:
            metadonnees["mlflow_run_id"] = run_parent.info.run_id
            mlflow.log_params(
                {
                    "modele_choisi": choisi,
                    "graine": config.modele.graine,
                    "precision_cible": cible,
                    "date_coupure_test": date_coupure,
                    "lignes_entrainement": len(entrainement),
                    "lignes_validation": len(validation),
                    "lignes_test": len(test),
                    "empreinte_donnees_sha256": metadonnees["empreinte_donnees_sha256"],
                    "version_regles": VERSION_REGLES,
                    "version_modele": version,
                }
            )
            mlflow.set_tags(
                {"jeu_de_donnees": Path(chemin).name, "environnement": config.environnement}
            )
            for cle, valeur in resultats[choisi]["test"].items():
                if isinstance(valeur, int | float) and not isinstance(valeur, bool):
                    mlflow.log_metric(f"test_{cle}", float(valeur))
            mlflow.log_metric("test_seuil_decision", seuil_final)
            _journaliser_modele(
                mlflow, pipelines[choisi], test[colonnes].head(5), config.mlflow.enregistrer_modele
            )
        dossier = exporter_modele(
            config.modele.dossier, pipelines[choisi], seuil_final, metadonnees
        )
        if mlflow and run_parent:
            mlflow.log_artifact(str(dossier / "metadonnees.json"))
    finally:
        if mlflow:
            mlflow.end_run()

    if promouvoir_si_premier and version_en_service(config.modele.dossier) is None:
        promouvoir(config.modele.dossier, version)
        info(_journal, "modele_promu_amorcage", version=version)
    duree_totale = time.monotonic() - debut
    info(
        _journal,
        "entrainement_termine",
        version=version,
        modele=choisi,
        duree_s=round(duree_totale, 1),
        pr_auc_test=round(resultats[choisi]["test"]["pr_auc"], 4),
    )
    return {
        "version": version,
        "modele": choisi,
        "seuil": seuil_final,
        "dossier": str(dossier),
        "metriques_test": resultats[choisi]["test"],
        "metriques_validation": resultats[choisi]["validation"],
        "candidats": resultats,
        "date_coupure_test": date_coupure,
        "duree_s": duree_totale,
    }


# --------------------------------------------------------------------------------------------
# Barrière de qualité et promotion (appelées par le DAG hebdomadaire)
# --------------------------------------------------------------------------------------------
def valider_candidat(config: Configuration, version: str) -> dict[str, Any]:
    """Compare le candidat au plancher et au modèle en service ; lève si refusé."""
    candidat = lire_metadonnees(config.modele.dossier, version)
    pr_auc = float(candidat["metriques_test"]["pr_auc"])
    raisons: list[str] = []
    if not pr_auc >= config.modele.pr_auc_minimum:  # écrit ainsi pour refuser aussi NaN
        raisons.append(
            f"PR-AUC test {pr_auc:.3f} sous le plancher {config.modele.pr_auc_minimum:.3f}"
        )
    en_service = version_en_service(config.modele.dossier)
    pr_auc_service = None
    if en_service and en_service != version:
        pr_auc_service = float(
            lire_metadonnees(config.modele.dossier, en_service)["metriques_test"]["pr_auc"]
        )
        if pr_auc < pr_auc_service - config.modele.tolerance_regression:
            raisons.append(
                f"PR-AUC test {pr_auc:.3f} inférieure à celle du modèle en service "
                f"({pr_auc_service:.3f}) de plus de {config.modele.tolerance_regression:.3f}"
            )
    verdict = {
        "version": version,
        "pr_auc_test": pr_auc,
        "pr_auc_en_service": pr_auc_service,
        "accepte": not raisons,
        "raisons": raisons,
    }
    info(
        _journal,
        "barriere_qualite",
        **{k: v for k, v in verdict.items() if k != "raisons"},
        raisons=" | ".join(raisons),
    )
    if raisons:
        raise BarriereQualiteRefusee("; ".join(raisons))
    return verdict


def promouvoir_version(config: Configuration, version: str) -> None:
    promouvoir(config.modele.dossier, version)
    info(_journal, "modele_promu", version=version)


# --------------------------------------------------------------------------------------------
# Ligne de commande
# --------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Entraîne, exporte et promeut le modèle.")
    analyseur.add_argument("--donnees", type=Path, help="chemin du jeu (défaut : configuration)")
    analyseur.add_argument(
        "--limite", type=int, help="ne lire que les N premières lignes (essais)"
    )
    analyseur.add_argument("--sans-mlflow", action="store_true", help="désactive le suivi MLflow")
    analyseur.add_argument(
        "--promouvoir",
        action="store_true",
        help="après la barrière de qualité, met le candidat en service",
    )
    arguments = analyseur.parse_args(argv)
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    resultat = entrainer(
        config,
        arguments.donnees,
        limite=arguments.limite,
        suivre_mlflow=not arguments.sans_mlflow,
        promouvoir_si_premier=arguments.promouvoir,
    )
    if arguments.promouvoir and version_en_service(config.modele.dossier) != resultat["version"]:
        valider_candidat(config, resultat["version"])
        promouvoir_version(config, resultat["version"])
    print(
        json.dumps(
            {
                k: resultat[k]
                for k in (
                    "version",
                    "modele",
                    "seuil",
                    "metriques_test",
                    "date_coupure_test",
                    "duree_s",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
