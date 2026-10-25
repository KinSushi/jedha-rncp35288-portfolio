# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Garanties de l'entraînement (critères 2.2 et 3.3) : mesures exactes, câblage prouvé, refus nets.

Comment lire ces tests :

* les mesures (``rappel_a_precision_fixee``, ``evaluer``, ``decouper_chronologiquement``) sont
  vérifiées sur des cas **calculés à la main** (le calcul est dans le commentaire du cas) ;
* le câblage de ``entrainer`` est prouvé avec des **doublures qui enregistrent leurs arguments** :
  faux pipelines (aucun ajustement), faux MLflow, horloge et date figées. On compare ainsi le
  nom, le niveau et chaque champ des événements, chaque paramètre transmis à MLflow, chaque clé
  des métadonnées ;
* un seul test (``test_entrainement_reel_...``) fait tourner le vrai pipeline scikit-learn et un
  vrai MLflow SQLite sur ~400 lignes : le bout en bout ;
* les refus (données absentes, jeu sans ligne valide, candidat inconnu, barrière de qualité)
  vérifient le **message exact** et que rien n'est abîmé (instantané pris ``avant``).

Aucun test n'utilise ``modele_entraine`` : chaque test fabrique ses propres données minuscules.
"""

from __future__ import annotations

import copy
import hashlib
import platform
import sys
import warnings
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import (
    FunctionTransformer,
    KBinsDiscretizer,
    OneHotEncoder,
    OrdinalEncoder,
    StandardScaler,
)
from threadpoolctl import threadpool_limits

from detection_fraude import VERSION_REGLES, entrainement
from detection_fraude.entrainement import (
    HYPERPARAMETRES,
    BarriereQualiteRefusee,
    _journaliser_modele,
    _preparer_mlflow,
    construire_pipeline,
    decouper_chronologiquement,
    entrainer,
    evaluer,
    promouvoir_version,
    rappel_a_precision_fixee,
    valider_candidat,
)
from detection_fraude.fonctionnalites import COLONNES_MODELE
from detection_fraude.modele import (
    ErreurModele,
    charger_modele,
    exporter_modele,
    lire_metadonnees,
    promouvoir,
    version_en_service,
)
from tests.helpers import fabriquer_jeu

VERSION = "20261011T123456Z"
CREE_LE = "2026-10-11T12:34:56+00:00"
NOMS_CANDIDATS = ("regression_logistique", "gradient_boosting")
HYPERPARAMETRES_GB = {
    "learning_rate": 0.1,
    "max_iter": 200,
    "max_leaf_nodes": 31,
    "l2_regularization": 1.0,
    "min_samples_leaf": 40,
}
LIGNES_INVALIDES = (1, 100, 200)  # montants négatifs : rejetés par la validation du schéma

# Métriques « canned » : chaque clé a une valeur distincte pour que l'on voie où elle atterrit.
VALIDATION_1 = {
    "pr_auc": 0.123456,
    "roc_auc": 0.5,
    "cible_atteinte_sur_courbe": False,
    "nb_lignes": 50,
    "libelle": "validation 1",
}
TEST_1 = {"pr_auc": 0.654321, "nb_fraudes": 3}
VALIDATION_2 = {
    "pr_auc": 0.87654321,
    "roc_auc": 0.75,
    "cible_atteinte_sur_courbe": True,
    "nb_lignes": 50,
}
TEST_2 = {
    "pr_auc": 0.98765432,
    "rappel_au_seuil": 0.5,
    "nb_fraudes": 3,
    "nb_lignes": 149,
    # Un booléen dans les métriques du candidat retenu : MLflow ne doit pas le recevoir.
    "cible_atteinte_sur_courbe": False,
}


# --------------------------------------------------------------------------------------------
# Données et doublures
# --------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="module")
def un_seul_fil_de_calcul():
    """Les ajustements réels restent rapides même sur une machine chargée.

    Sans cette limite, les fils OpenMP du gradient boosting se disputent le processeur avec les
    autres processus (un ajustement de 240 lignes passe de 0,2 s à plus d'une minute).
    """
    with threadpool_limits(limits=1):
        yield


def fabriquer_csv(chemin: Path, nb_lignes: int = 400) -> pd.DataFrame:
    """Jeu au format ``fraudTest.csv`` : une fraude toutes les 12 lignes, 3 lignes invalides."""
    jeu = fabriquer_jeu(nb_lignes=nb_lignes, jours=24)
    fraude = np.arange(len(jeu)) % 12 == 5
    jeu["is_fraud"] = fraude.astype(int)
    jeu.loc[fraude, "amt"] = 900.0
    jeu.loc[fraude, "category"] = "shopping_net"
    jeu.loc[list(LIGNES_INVALIDES), "amt"] = -1.0
    # Une transaction par jour à 01 h UTC : la date de coupure se lit sans ambiguïté (en heure
    # locale d'un fuseau à l'ouest de Greenwich ce serait la veille) et deux lignes voisines
    # n'ont jamais la même date.
    jeu["trans_date_trans_time"] = [
        (datetime(2020, 6, 21, 1, 0, 0) + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(len(jeu))
    ]
    jeu.to_csv(chemin, index=False)
    return jeu


@pytest.fixture(scope="module")
def donnees(tmp_path_factory) -> SimpleNamespace:
    """Le CSV (400 lignes) et ses lignes retenues dans l'ordre chronologique attendu."""
    chemin = tmp_path_factory.mktemp("jeu_entrainement") / "jeu.csv"
    brut = fabriquer_csv(chemin)
    retenues = brut.drop(index=list(LIGNES_INVALIDES))
    retenues = retenues.sort_values(["trans_date_trans_time", "trans_num"], kind="stable")
    return SimpleNamespace(
        chemin=chemin,
        brut=brut,
        retenues=retenues.reset_index(drop=True),
        sha=hashlib.sha256(chemin.read_bytes()).hexdigest(),
    )


def config_banc(config, **modele):
    """Configuration de test : graine 7, précision cible 0,8, découpage 50 % / 12,5 % / 37,5 %."""
    reglages = {
        "graine": 7,
        "precision_cible": 0.8,
        "fraction_entrainement": 0.5,
        "fraction_validation": 0.125,
        **modele,
    }
    return replace(config, modele=replace(config.modele, **reglages))


class PipelineFactice:
    """Pipeline sans apprentissage : le score croît (``sens`` = 1) ou décroît avec le montant.

    Il enregistre ce qu'il reçoit. Défini au niveau du module pour pouvoir être sérialisé
    (``entrainer`` exporte le pipeline choisi).
    """

    def __init__(self, nom: str, graine: int, sens: int = 1) -> None:
        self.nom, self.graine, self.sens = nom, graine, sens
        self.ajustements: list[dict[str, Any]] = []
        self.predictions: list[dict[str, Any]] = []

    def fit(self, caracteristiques: pd.DataFrame, etiquettes: Any) -> PipelineFactice:
        self.ajustements.append(
            {
                "colonnes": caracteristiques.columns.tolist(),
                "montants": caracteristiques["montant"].tolist(),
                "etiquettes": np.asarray(etiquettes).tolist(),
            }
        )
        return self

    def predict_proba(self, caracteristiques: pd.DataFrame) -> np.ndarray:
        montants = caracteristiques["montant"].to_numpy(dtype=float)
        self.predictions.append(
            {"colonnes": caracteristiques.columns.tolist(), "montants": montants.tolist()}
        )
        score = montants / (montants + 300.0)
        score = score if self.sens > 0 else 1.0 - score
        return np.column_stack([1.0 - score, score])


class PipelineEnPanne(PipelineFactice):
    """Pipeline dont l'ajustement échoue toujours."""

    def fit(self, caracteristiques: pd.DataFrame, etiquettes: Any) -> PipelineFactice:
        raise RuntimeError("ajustement impossible (test)")


class RunFactice:
    """Run MLflow factice : sert d'objet (``info.run_id``) et de gestionnaire de contexte."""

    def __init__(self, mlflow: MlflowFactice, nom: str, run_id: str) -> None:
        self.mlflow = mlflow
        self.nom = nom
        self.info = SimpleNamespace(run_id=run_id)

    def __enter__(self) -> RunFactice:
        self.mlflow.appels.append(("entree", self.nom))
        return self

    def __exit__(self, *details: object) -> None:
        self.mlflow.appels.append(("sortie", self.nom))


class MlflowFactice:
    """Enregistre, dans l'ordre, chaque appel que ``entrainer`` adresse à MLflow."""

    def __init__(self) -> None:
        self.appels: list[tuple[Any, ...]] = []
        self.nb_runs = 0

    def start_run(self, **options: Any) -> RunFactice:
        self.appels.append(("start_run", options))
        self.nb_runs += 1
        return RunFactice(self, options.get("run_name", ""), f"run-{self.nb_runs}")

    def log_params(self, parametres: dict[str, Any]) -> None:
        self.appels.append(("log_params", parametres))

    def log_param(self, cle: str, valeur: Any) -> None:
        self.appels.append(("log_param", cle, valeur))

    def log_metric(self, cle: str, valeur: float) -> None:
        self.appels.append(("log_metric", cle, valeur))

    def set_tags(self, etiquettes: dict[str, str]) -> None:
        self.appels.append(("set_tags", etiquettes))

    def log_artifact(self, chemin: str) -> None:
        self.appels.append(("log_artifact", chemin))

    def end_run(self) -> None:
        self.appels.append(("end_run",))


class Banc:
    """Tout ce que les doublures de ``entrainer`` ont reçu."""

    def __init__(self) -> None:
        self.construits: list[tuple[str, int]] = []
        self.pipelines: dict[str, PipelineFactice] = {}
        self.rappels: list[tuple[np.ndarray, np.ndarray, float]] = []
        self.evaluations: list[tuple[np.ndarray, np.ndarray, float, float]] = []
        self.fuseaux: list[Any] = []
        self.horloge: list[float] = []


def brancher_banc(
    monkeypatch,
    *,
    sens: dict[str, int] | None = None,
    classe=PipelineFactice,
    rappels: list[dict[str, Any]] | None = None,
    evaluations: list[dict[str, Any]] | None = None,
    horloge: list[float] | None = None,
) -> Banc:
    """Remplace pipelines, version, date (et au choix mesures et horloge) par des doublures."""
    banc = Banc()
    sens_par_nom = {"regression_logistique": 1, "gradient_boosting": -1, **(sens or {})}

    def faux_pipeline(nom: str, graine: int) -> PipelineFactice:
        banc.construits.append((nom, graine))
        banc.pipelines[nom] = classe(nom, graine, sens_par_nom.get(nom, 1))
        return banc.pipelines[nom]

    class DatetimeFige(datetime):
        @classmethod
        def now(cls, tz=None):
            banc.fuseaux.append(tz)
            return datetime(2026, 10, 11, 12, 34, 56, 789012, tzinfo=tz)

    monkeypatch.setattr(entrainement, "construire_pipeline", faux_pipeline)
    monkeypatch.setattr(entrainement, "nouvelle_version", lambda: VERSION)
    monkeypatch.setattr(entrainement, "datetime", DatetimeFige)
    if rappels is not None:
        suite_rappels = iter(rappels)

        def faux_rappel(y_vrai, scores, precision_cible):
            banc.rappels.append((np.asarray(y_vrai), np.asarray(scores), precision_cible))
            return next(suite_rappels)

        monkeypatch.setattr(entrainement, "rappel_a_precision_fixee", faux_rappel)
    if evaluations is not None:
        suite_evaluations = iter(evaluations)

        def faux_evaluer(y_vrai, scores, seuil, cible):
            banc.evaluations.append((np.asarray(y_vrai), np.asarray(scores), seuil, cible))
            return next(suite_evaluations)

        monkeypatch.setattr(entrainement, "evaluer", faux_evaluer)
    if horloge is not None:
        banc.horloge = list(horloge)
        monkeypatch.setattr(
            entrainement, "time", SimpleNamespace(monotonic=lambda: banc.horloge.pop(0))
        )
    return banc


def brancher_mlflow(monkeypatch) -> tuple[MlflowFactice, list[Any], list[Any]]:
    """Faux MLflow + faux ``_journaliser_modele`` ; renvoie aussi les configurations reçues."""
    mlflow = MlflowFactice()
    configurations: list[Any] = []
    modeles: list[Any] = []

    def preparer(config):
        configurations.append(config)
        return mlflow

    def journaliser(mlflow_recu, pipeline, exemple, enregistrer):
        modeles.append(
            {
                "mlflow_est_le_faux": mlflow_recu is mlflow,
                "pipeline": pipeline.nom,
                "index": exemple.index.tolist(),
                "colonnes": exemple.columns.tolist(),
                "enregistrer": enregistrer,
            }
        )
        mlflow.appels.append(("journaliser_modele", pipeline.nom))

    monkeypatch.setattr(entrainement, "_preparer_mlflow", preparer)
    monkeypatch.setattr(entrainement, "_journaliser_modele", journaliser)
    return mlflow, configurations, modeles


def instantane(racine: Path) -> dict[str, bytes | None]:
    """Chemin relatif -> octets (``None`` pour un dossier) de tout ce que contient ``racine``."""
    if not racine.exists():
        return {}
    return {
        chemin.relative_to(racine).as_posix(): chemin.read_bytes() if chemin.is_file() else None
        for chemin in sorted(racine.rglob("*"))
    }


def pipeline_nul() -> DummyClassifier:
    return DummyClassifier(strategy="prior").fit(np.zeros((4, 1)), [0, 1, 0, 0])


def publier_candidat(config, version: str, pr_auc: float, *, en_service: bool = False) -> None:
    """Exporte une version dont seules les métriques de test comptent pour la barrière."""
    exporter_modele(
        config.modele.dossier,
        pipeline_nul(),
        0.5,
        {"version": version, "metriques_test": {"pr_auc": pr_auc}},
    )
    if en_service:
        promouvoir(config.modele.dossier, version)


def config_barriere(config, plancher: float = 0.25, tolerance: float = 0.125):
    """Valeurs binaires exactes : une comparaison pile à la limite n'a aucune erreur d'arrondi."""
    return replace(
        config,
        modele=replace(config.modele, pr_auc_minimum=plancher, tolerance_regression=tolerance),
    )


# --------------------------------------------------------------------------------------------
# construire_pipeline
# --------------------------------------------------------------------------------------------
def test_pipeline_regression_logistique_a_la_structure_exacte_et_ne_partage_rien():
    """Le modèle linéaire a les prétraitements que ses maths exigent ; la table des
    hyperparamètres reste intacte et deux pipelines ne partagent aucun estimateur."""
    hyperparametres_avant = copy.deepcopy(HYPERPARAMETRES)
    pipeline = construire_pipeline("regression_logistique", 7)
    assert isinstance(pipeline, Pipeline)
    assert [nom for nom, _ in pipeline.steps] == ["pretraitement", "estimateur"]
    pretraitement = pipeline.named_steps["pretraitement"]
    assert isinstance(pretraitement, ColumnTransformer)
    assert [(nom, colonnes) for nom, _, colonnes in pretraitement.transformers] == [
        ("montant_en_tranches", ["montant"]),
        ("longue_traine", ["population_ville", "distance_km", "nb_tx_24h", "montant_relatif"]),
        ("lineaires", ["age"]),
        ("indicatrices", ["categorie", "etat", "heure", "jour_semaine"]),
    ]
    montant, longue, lineaire, indicatrices = (t[1] for t in pretraitement.transformers)
    assert [nom for nom, _ in montant.steps] == ["log", "tranches"]
    assert isinstance(montant.named_steps["log"], FunctionTransformer)
    assert montant.named_steps["log"].func is np.log1p
    assert montant.named_steps["log"].validate is False
    tranches = montant.named_steps["tranches"]
    assert isinstance(tranches, KBinsDiscretizer)
    assert (tranches.n_bins, tranches.encode, tranches.strategy) == (12, "onehot", "uniform")
    assert tranches.subsample is None
    assert [nom for nom, _ in longue.steps] == ["log", "echelle"]
    assert longue.named_steps["log"].func is np.log1p
    assert longue.named_steps["log"].validate is False
    assert isinstance(longue.named_steps["echelle"], StandardScaler)
    assert [nom for nom, _ in lineaire.steps] == ["imputer", "echelle"]
    assert isinstance(lineaire.named_steps["imputer"], SimpleImputer)
    assert lineaire.named_steps["imputer"].strategy == "median"
    assert isinstance(lineaire.named_steps["echelle"], StandardScaler)
    assert isinstance(indicatrices, OneHotEncoder)
    assert (indicatrices.handle_unknown, indicatrices.min_frequency) == ("ignore", 20)
    estimateur = pipeline.named_steps["estimateur"]
    assert isinstance(estimateur, LogisticRegression)
    assert (estimateur.C, estimateur.max_iter, estimateur.random_state) == (1.0, 1000, 7)
    autre = construire_pipeline("regression_logistique", 11)
    assert autre.named_steps["estimateur"].random_state == 11  # la graine est bien transmise
    estimateur.set_params(C=5.0)
    assert autre.named_steps["estimateur"].C == 1.0
    assert hyperparametres_avant == HYPERPARAMETRES


def test_pipeline_gradient_boosting_a_la_structure_exacte_et_laisse_les_hyperparametres_intacts():
    """Le modèle à arbres n'a aucune mise à l'échelle, déclare ses catégories ; la table des
    hyperparamètres reste intacte."""
    hyperparametres_avant = copy.deepcopy(HYPERPARAMETRES)
    pipeline = construire_pipeline("gradient_boosting", 7)
    assert [nom for nom, _ in pipeline.steps] == ["pretraitement", "estimateur"]
    pretraitement = pipeline.named_steps["pretraitement"]
    assert isinstance(pretraitement, ColumnTransformer)
    assert [(nom, colonnes) for nom, _, colonnes in pretraitement.transformers] == [
        ("categories", ["categorie", "etat"]),
        (
            "numeriques",
            [
                "montant",
                "age",
                "distance_km",
                "population_ville",
                "heure",
                "jour_semaine",
                "nb_tx_24h",
                "montant_relatif",
            ],
        ),
    ]
    categories, numeriques = (t[1] for t in pretraitement.transformers)
    assert isinstance(categories, OrdinalEncoder)
    assert categories.handle_unknown == "use_encoded_value"
    assert np.isnan(categories.unknown_value)
    assert np.isnan(categories.encoded_missing_value)
    assert numeriques == "passthrough"
    estimateur = pipeline.named_steps["estimateur"]
    assert isinstance(estimateur, HistGradientBoostingClassifier)
    parametres = estimateur.get_params()
    assert {cle: parametres[cle] for cle in HYPERPARAMETRES_GB} == HYPERPARAMETRES_GB
    assert parametres["random_state"] == 7
    assert parametres["categorical_features"] == [True, True] + [False] * 8
    assert (
        construire_pipeline("gradient_boosting", 13).named_steps["estimateur"].random_state == 13
    )
    assert hyperparametres_avant == HYPERPARAMETRES


def test_construire_pipeline_refuse_un_candidat_inconnu_avec_le_message_exact():
    """Un nom de candidat inconnu (faute de frappe dans la configuration) est refusé net."""
    with pytest.raises(ValueError) as exc:
        construire_pipeline("devin", 0)
    assert str(exc.value) == "Candidat inconnu : devin"


@pytest.mark.parametrize(
    "nom", ["", "Regression_Logistique", "gradient-boosting", " gradient_boosting"]
)
def test_construire_pipeline_exige_le_nom_exact_du_candidat(nom):
    """Pas de correspondance approximative : casse, tiret ou espace en trop sont refusés."""
    with pytest.raises(ValueError) as exc:
        construire_pipeline(nom, 0)
    assert str(exc.value) == f"Candidat inconnu : {nom}"


def tableau_minuscule(nb_lignes: int = 240, graine: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    """Caractéristiques synthétiques au format du modèle et une cible apprenable."""
    hasard = np.random.default_rng(graine)
    montant = np.round(np.exp(hasard.normal(3.5, 0.9, nb_lignes)), 2)
    heure = hasard.integers(0, 24, nb_lignes)
    caracteristiques = pd.DataFrame(
        {
            "montant": montant,
            "age": hasard.integers(18, 80, nb_lignes).astype(float),
            "distance_km": np.round(hasard.uniform(0, 200, nb_lignes), 3),
            "population_ville": hasard.choice([300, 5_000, 90_000], nb_lignes),
            "heure": heure,
            "jour_semaine": hasard.integers(0, 7, nb_lignes),
            "nb_tx_24h": hasard.integers(0, 6, nb_lignes),
            "montant_relatif": np.round(hasard.uniform(0.2, 4, nb_lignes), 3),
            "categorie": hasard.choice(["grocery_pos", "travel", "misc_net"], nb_lignes),
            "etat": hasard.choice(["NY", "CA", "TX"], nb_lignes),
        }
    )[list(COLONNES_MODELE)]
    cible = ((montant > np.quantile(montant, 0.85)) | (heure < 3)).astype(int)
    return caracteristiques, cible


@pytest.mark.parametrize("nom", NOMS_CANDIDATS)
def test_pipelines_reels_notent_en_probabilites_gerent_l_inconnu_et_laissent_le_tableau_intact(
    nom,
):
    """Après ajustement : probabilités dans [0, 1], catégorie jamais vue et âge manquant admis,
    même graine = mêmes scores, et le tableau d'entraînement reste intact."""
    caracteristiques, cible = tableau_minuscule()
    caracteristiques_avant = caracteristiques.copy()
    pipeline = construire_pipeline(nom, 3).fit(caracteristiques, cible)
    scores = pipeline.predict_proba(caracteristiques)[:, 1]
    assert scores.shape == (240,)
    assert ((scores >= 0) & (scores <= 1)).all()
    ligne = caracteristiques.head(1).copy()
    ligne["categorie"], ligne["etat"], ligne["age"] = "jamais_vue", "ZZ", np.nan
    assert 0 <= pipeline.predict_proba(ligne)[0, 1] <= 1
    rejoue = construire_pipeline(nom, 3).fit(caracteristiques, cible)
    assert np.array_equal(rejoue.predict_proba(caracteristiques)[:, 1], scores)
    pd.testing.assert_frame_equal(caracteristiques, caracteristiques_avant)


@pytest.mark.parametrize("nom", NOMS_CANDIDATS)
def test_un_pipeline_ajuste_refuse_un_tableau_sans_la_colonne_montant(nom):
    """Le pipeline exporté exige ses colonnes : sans ``montant`` il lève au lieu de deviner."""
    caracteristiques, cible = tableau_minuscule()
    pipeline = construire_pipeline(nom, 3).fit(caracteristiques, cible)
    incomplet = caracteristiques.drop(columns=["montant"])
    incomplet_avant = incomplet.copy()
    with pytest.raises((ValueError, KeyError)) as exc:
        pipeline.predict_proba(incomplet)
    assert "montant" in str(exc.value)
    pd.testing.assert_frame_equal(incomplet, incomplet_avant)


# --------------------------------------------------------------------------------------------
# rappel_a_precision_fixee
# --------------------------------------------------------------------------------------------
Y_1 = [1, 1, 0, 1, 0, 0, 0, 0]
S_1 = [0.95, 0.90, 0.85, 0.80, 0.30, 0.20, 0.10, 0.05]
# Courbe de Y_1/S_1 (seuil : précision, rappel) : 0,05 : 3/8 ; 0,10 : 3/7 ; 0,20 : 3/6 ; 0,30 : 3/5
# (rappel 1) ; 0,80 : 3/4 (rappel 1) ; 0,85 : 2/3 ; 0,90 : 1 (rappel 2/3) ; 0,95 : 1 (rappel 1/3).
Y_BAS = [0, 0, 0, 0, 0, 1, 1, 0, 0]
S_BAS = [0.4, 0.6, 0.5, 0.9, 0.3, 0.1, 0.7, 0.2, 0.8]
# Y_BAS/S_BAS : seuil 0,1 -> précision 2/9, rappel 1 (F1 0,364) ; seuil 0,7 -> précision 1/3,
# rappel 1/2 (F1 0,4, le meilleur) ; seuils 0,8 et 0,9 -> précision 0 : aucun seuil n'atteint 0,99.


@pytest.mark.parametrize(
    ("y_vrai", "scores", "cible", "attendu"),
    [
        pytest.param(  # seuils 0,90 et 0,95 admissibles : le meilleur rappel est celui de 0,90
            Y_1,
            S_1,
            0.9,
            {"rappel": 2 / 3, "precision": 1.0, "seuil": 0.90, "cible_atteinte": True},
            id="cible_atteinte_au_meilleur_rappel",
        ),
        pytest.param(  # la précision 3/4 du seuil 0,80 égale la cible : admissible (>=)
            Y_1,
            S_1,
            0.75,
            {"rappel": 1.0, "precision": 0.75, "seuil": 0.80, "cible_atteinte": True},
            id="precision_egale_a_la_cible",
        ),
        pytest.param(  # un cheveu au-dessus de 3/4 : le seuil 0,80 n'est plus admissible
            Y_1,
            S_1,
            0.7500000001,
            {"rappel": 2 / 3, "precision": 1.0, "seuil": 0.90, "cible_atteinte": True},
            id="juste_au_dessus_de_la_precision",
        ),
        pytest.param(  # cible 1 : seuls 0,90 et 0,95 ont une précision de 1
            Y_1,
            S_1,
            1.0,
            {"rappel": 2 / 3, "precision": 1.0, "seuil": 0.90, "cible_atteinte": True},
            id="cible_un",
        ),
        pytest.param(  # seul le dernier seuil (0,9) est admissible : il doit être considéré
            [1, 0],
            [0.9, 0.1],
            0.9,
            {"rappel": 1.0, "precision": 1.0, "seuil": 0.9, "cible_atteinte": True},
            id="seul_le_dernier_seuil_est_admissible",
        ),
        pytest.param(  # cible inatteignable : on retient le seuil de meilleur F1 (0,7)
            Y_BAS,
            S_BAS,
            0.99,
            {"rappel": 0.5, "precision": 1 / 3, "seuil": 0.7, "cible_atteinte": False},
            id="cible_inatteignable_repli_sur_le_meilleur_f1",
        ),
        pytest.param(  # même point, cible atteignable (1/3 >= 0,3) : seul l'indicateur change
            Y_BAS,
            S_BAS,
            0.3,
            {"rappel": 0.5, "precision": 1 / 3, "seuil": 0.7, "cible_atteinte": True},
            id="meme_point_cible_atteinte",
        ),
        pytest.param(  # F1 : 0,1 -> 2/3 ; 0,7 -> 0,4 ; 0,8 -> 0,5 ; 0,9 -> 0
            [0, 1, 0, 1],
            [0.9, 0.8, 0.7, 0.1],
            0.99,
            {"rappel": 1.0, "precision": 0.5, "seuil": 0.1, "cible_atteinte": False},
            id="repli_f1_au_plus_bas_seuil",
        ),
    ],
)
def test_rappel_a_precision_fixee_cas_calcules_a_la_main_et_entrees_intactes(
    y_vrai, scores, cible, attendu
):
    """Seuil et mesures exacts sur des cas calculés à la main ; tableaux d'entrée intacts."""
    y, s = np.array(y_vrai), np.array(scores)
    y_avant, s_avant = y.copy(), s.copy()
    resultat = rappel_a_precision_fixee(y, s, cible)
    assert resultat == attendu
    assert [type(resultat[cle]) for cle in ("rappel", "precision", "seuil")] == [float] * 3
    assert resultat["cible_atteinte"] is attendu["cible_atteinte"]
    assert np.array_equal(y, y_avant)
    assert np.array_equal(s, s_avant)


def test_rappel_a_precision_fixee_une_egalite_de_f1_est_departagee_par_le_seuil_le_plus_bas():
    """Deux seuils de même F1 : le plus bas est retenu, et c'est stable.

    Le repli sur le F1 (quand la précision cible est inatteignable) compare des F1 qui
    peuvent être mathématiquement égaux. Multiplier le numérateur par 2 ou par 3 ne change
    pas leur ordre en arithmétique exacte, mais le calcul en virgule flottante arrondit :
    l'égalité peut disparaître d'un côté et pas de l'autre, et le seuil retenu change.

    Ce test fixe le cas : 8 transactions, 2 fraudes, précision cible 0,90 (inatteignable).
    - seuil 0,0 : 8 alertes dont 2 fraudes, précision 0,25, rappel 1,0, F1 = 0,4 ;
    - seuil 0,3 : 3 alertes dont 1 fraude, précision 1/3, rappel 0,5, F1 = 0,4.
    Les deux F1 sont égaux : le seuil le plus bas, 0,0, doit être retenu.
    """
    y_vrai = np.array([0, 0, 0, 0, 1, 1, 0, 0])
    scores = np.array([0.3, 0.0, 0.2, 0.2, 0.3, 0.0, 0.3, 0.0])
    resultat = rappel_a_precision_fixee(y_vrai, scores, 0.90)
    assert resultat["cible_atteinte"] is False
    assert resultat["seuil"] == 0.0
    assert resultat["rappel"] == 1.0
    assert resultat["precision"] == 0.25


def test_rappel_a_precision_fixee_refuse_des_longueurs_differentes_et_garde_les_entrees_intactes():
    """Des étiquettes et des scores de tailles différentes sont une erreur, pas un seuil."""
    y, s = np.array([1, 0, 1]), np.array([0.9, 0.8])
    y_avant, s_avant = y.copy(), s.copy()
    with pytest.raises(ValueError) as exc:
        rappel_a_precision_fixee(y, s, 0.5)
    assert str(exc.value) == "Found input variables with inconsistent numbers of samples: [3, 2]"
    assert np.array_equal(y, y_avant)
    assert np.array_equal(s, s_avant)


# --------------------------------------------------------------------------------------------
# evaluer
# --------------------------------------------------------------------------------------------
Y_E = [1, 0, 1, 0, 0, 0]
S_E = [0.9, 0.8, 0.7, 0.2, 0.1, 0.05]
# Y_E/S_E : précision-rappel AP = 1/2 * 1 + 1/2 * 2/3 = 5/6 ; ROC AUC : les deux positifs battent
# 4 et 3 des 4 négatifs, soit 7/8. Courbe : 0,2 -> 1/2 (rappel 1) ; 0,7 -> 2/3 ; 0,9 -> 1
# (rappel 1/2).


@pytest.mark.parametrize(
    ("y_vrai", "scores", "seuil", "cible", "attendu"),
    [
        pytest.param(  # seuil 0,75 : alertes {0,9 ; 0,8} dont 1 vraie ; cible 0,5 : rappel 1
            Y_E,
            S_E,
            0.75,
            0.5,
            {
                "pr_auc": 5 / 6,
                "roc_auc": 7 / 8,
                "rappel_a_precision_fixee": 1.0,
                "cible_atteinte_sur_courbe": True,
                "precision_au_seuil": 0.5,
                "rappel_au_seuil": 0.5,
                "nb_alertes_au_seuil": 2,
                "nb_fraudes": 2,
                "nb_lignes": 6,
            },
            id="seuil_au_milieu",
        ),
        pytest.param(  # seuil 0,7 = un score : alerte déclenchée (>=), 3 alertes dont 2 vraies
            Y_E,
            S_E,
            0.7,
            0.5,
            {
                "pr_auc": 5 / 6,
                "roc_auc": 7 / 8,
                "rappel_a_precision_fixee": 1.0,
                "cible_atteinte_sur_courbe": True,
                "precision_au_seuil": 2 / 3,
                "rappel_au_seuil": 1.0,
                "nb_alertes_au_seuil": 3,
                "nb_fraudes": 2,
                "nb_lignes": 6,
            },
            id="seuil_egal_a_un_score_inclusif",
        ),
        pytest.param(  # cible 0,99 : seul le seuil 0,9 (précision 1) convient, rappel 1/2
            Y_E,
            S_E,
            0.75,
            0.99,
            {
                "pr_auc": 5 / 6,
                "roc_auc": 7 / 8,
                "rappel_a_precision_fixee": 0.5,
                "cible_atteinte_sur_courbe": True,
                "precision_au_seuil": 0.5,
                "rappel_au_seuil": 0.5,
                "nb_alertes_au_seuil": 2,
                "nb_fraudes": 2,
                "nb_lignes": 6,
            },
            id="la_cible_est_transmise_a_la_courbe",
        ),
        pytest.param(  # aucun score >= 0,95 : 0 alerte, précision et rappel valent 0 sans alerte
            Y_E,
            S_E,
            0.95,
            0.5,
            {
                "pr_auc": 5 / 6,
                "roc_auc": 7 / 8,
                "rappel_a_precision_fixee": 1.0,
                "cible_atteinte_sur_courbe": True,
                "precision_au_seuil": 0.0,
                "rappel_au_seuil": 0.0,
                "nb_alertes_au_seuil": 0,
                "nb_fraudes": 2,
                "nb_lignes": 6,
            },
            id="aucune_alerte",
        ),
        pytest.param(  # cible inatteignable (repli F1 à 0,1) : l'indicateur doit passer à False
            [0, 1, 0, 1],
            [0.9, 0.8, 0.7, 0.1],
            0.75,
            0.99,
            {
                "pr_auc": 0.5,
                "roc_auc": 0.25,
                "rappel_a_precision_fixee": 1.0,
                "cible_atteinte_sur_courbe": False,
                "precision_au_seuil": 0.5,
                "rappel_au_seuil": 0.5,
                "nb_alertes_au_seuil": 2,
                "nb_fraudes": 2,
                "nb_lignes": 4,
            },
            id="cible_inatteignable",
        ),
    ],
)
def test_evaluer_cas_calcules_a_la_main_sans_alerte_de_bibliotheque_et_entrees_intactes(
    y_vrai, scores, seuil, cible, attendu
):
    """Toutes les clés et valeurs sur un cas calculé ; aucun avertissement ; entrées intactes."""
    y, s = np.array(y_vrai), np.array(scores)
    y_avant, s_avant = y.copy(), s.copy()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # une précision indéfinie ne doit pas lever d'alerte
        resultat = evaluer(y, s, seuil, cible)
    assert set(resultat) == set(attendu)
    assert resultat == pytest.approx(attendu, rel=1e-12, abs=1e-12)
    assert resultat["cible_atteinte_sur_courbe"] is attendu["cible_atteinte_sur_courbe"]
    for cle in ("nb_alertes_au_seuil", "nb_fraudes", "nb_lignes"):
        assert type(resultat[cle]) is int
    for cle in ("pr_auc", "roc_auc", "precision_au_seuil", "rappel_au_seuil"):
        assert type(resultat[cle]) is float
    assert np.array_equal(y, y_avant)
    assert np.array_equal(s, s_avant)


@pytest.mark.parametrize(
    "etiquettes",
    [
        pytest.param([0, 0, 0, 0, 0], id="aucune_fraude"),
        pytest.param([1, 1, 1], id="que_des_fraudes"),
        pytest.param([], id="jeu_vide"),
    ],
)
def test_evaluer_sans_les_deux_classes_donne_nan_et_laisse_les_entrees_intactes(etiquettes):
    """Sans fraude (ou sans non-fraude) PR-AUC et ROC-AUC n'existent pas : NaN, rien d'autre."""
    y = np.array(etiquettes, dtype=int)
    s = np.linspace(0, 1, len(etiquettes))
    s_avant = s.copy()
    resultat = evaluer(y, s, 0.5, 0.9)
    assert list(resultat) == ["pr_auc", "roc_auc"]
    assert np.isnan(resultat["pr_auc"]) and np.isnan(resultat["roc_auc"])
    assert np.array_equal(s, s_avant)


def test_evaluer_refuse_des_longueurs_differentes():
    """Étiquettes et scores désalignés : erreur franche, jamais des métriques trompeuses."""
    y, s = np.array([1, 0, 1]), np.array([0.9, 0.8])
    s_avant = s.copy()
    with pytest.raises(ValueError) as exc:
        evaluer(y, s, 0.5, 0.5)
    assert str(exc.value) == "Found input variables with inconsistent numbers of samples: [3, 2]"
    assert np.array_equal(s, s_avant)


# --------------------------------------------------------------------------------------------
# decouper_chronologiquement
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("n", "fraction_entrainement", "fraction_validation", "tailles"),
    [
        (10, 0.5, 0.25, (5, 2, 3)),  # 7,5 -> 7 : l'entier est tronqué, pas arrondi (round -> 8)
        (7, 0.5, 0.25, (3, 2, 2)),  # 3,5 -> 3 et 5,25 -> 5
        (9, 0.5, 0.25, (4, 2, 3)),  # 4,5 -> 4 (round -> 4) et 6,75 -> 6 (round -> 7)
        (100, 0.75, 0.125, (75, 12, 13)),  # 87,5 -> 87
        (1, 0.5, 0.25, (0, 0, 1)),  # le test garde toujours au moins la dernière ligne
        (0, 0.5, 0.25, (0, 0, 0)),
        (20, 0.25, 0.5, (5, 10, 5)),  # fractions distinctes : validation plus grande que le test
    ],
)
def test_decoupage_tailles_par_troncature_colonnes_et_tableau_intacts(
    n, fraction_entrainement, fraction_validation, tailles
):
    """Les tranches ont les tailles ``int(n * fraction)`` ; aucune colonne ajoutée ni retirée ;
    le tableau d'origine reste intact."""
    df = pd.DataFrame(
        {"horodatage_s": range(n), "trans_num": [f"t{i:03d}" for i in range(n)], "v": range(n)}
    )
    df_avant = df.copy()
    entrainement_, validation, test = decouper_chronologiquement(
        df, fraction_entrainement, fraction_validation
    )
    assert (len(entrainement_), len(validation), len(test)) == tailles
    for tranche in (entrainement_, validation, test):
        assert tranche.columns.tolist() == ["horodatage_s", "trans_num", "v"]
    assert pd.concat([entrainement_, validation, test])["v"].tolist() == list(range(n))
    pd.testing.assert_frame_equal(df, df_avant)


def test_decoupage_trie_par_horodatage_puis_trans_num_de_facon_stable_et_garde_le_tableau_intact():
    """Le passé entraîne, le futur teste : tri par heure puis ``trans_num``, ex æquo conservés."""
    df = pd.DataFrame(
        {
            "horodatage_s": [30, 10, 20, 10, 20, 10],
            "trans_num": ["b", "z", "a", "a", "a", "z"],
            "rang": [0, 1, 2, 3, 4, 5],
        }
    )
    df_avant = df.copy()
    entrainement_, validation, test = decouper_chronologiquement(df, 0.5, 0.25)
    # (10, a) -> 3 ; (10, z) -> 1 puis 5 (ex æquo parfait : ordre d'origine) ;
    # (20, a) -> 2 puis 4 ;
    # (30, b) -> 0.
    assert entrainement_["rang"].tolist() == [3, 1, 5]
    assert validation["rang"].tolist() == [2]
    assert test["rang"].tolist() == [4, 0]
    pd.testing.assert_frame_equal(df, df_avant)


def test_decoupage_ne_chevauche_jamais_les_periodes_meme_a_horodatages_egaux_tableau_intact():
    """Pas de fuite : le dernier instant d'une tranche n'est jamais après le premier de la
    suivante, et toutes les lignes sont conservées une fois ; le tableau reste intact."""
    df = pd.DataFrame(
        {
            "horodatage_s": np.random.default_rng(0).integers(0, 20, 200),
            "trans_num": [f"t{i:03d}" for i in range(200)],
        }
    )
    df_avant = df.copy()
    entrainement_, validation, test = decouper_chronologiquement(df, 0.6, 0.2)
    pd.testing.assert_frame_equal(df, df_avant)
    assert entrainement_["horodatage_s"].max() <= validation["horodatage_s"].min()
    assert validation["horodatage_s"].max() <= test["horodatage_s"].min()
    assert sorted(pd.concat([entrainement_, validation, test])["trans_num"].tolist()) == sorted(
        df["trans_num"].tolist()
    )


@pytest.mark.parametrize("manquante", ["horodatage_s", "trans_num"])
def test_decoupage_refuse_un_tableau_sans_cle_de_tri_et_le_laisse_intact(manquante):
    """Sans clé de tri on ne découpe pas au hasard : une colonne absente lève ``KeyError``."""
    df = pd.DataFrame({"horodatage_s": [3, 1, 2], "trans_num": ["a", "b", "c"]})
    incomplet = df.drop(columns=[manquante])
    incomplet_avant = incomplet.copy()
    with pytest.raises(KeyError) as exc:
        decouper_chronologiquement(incomplet, 0.5, 0.25)
    assert str(exc.value) == f"'{manquante}'"
    pd.testing.assert_frame_equal(incomplet, incomplet_avant)


# --------------------------------------------------------------------------------------------
# _preparer_mlflow
# --------------------------------------------------------------------------------------------
class ClientFactice:
    def __init__(self, mlflow: MlflowPrepare) -> None:
        self.mlflow = mlflow

    def get_experiment_by_name(self, nom: str) -> Any:
        self.mlflow.appels.append(("get_experiment_by_name", nom))
        return self.mlflow.experiences.get(nom)

    def create_experiment(self, nom: str, **options: Any) -> str:
        self.mlflow.appels.append(("create_experiment", nom, options))
        self.mlflow.experiences[nom] = SimpleNamespace(nom=nom, **options)
        return "id-1"


class MlflowPrepare:
    """Faux module ``mlflow`` pour ``_preparer_mlflow`` : enregistre ses appels dans l'ordre."""

    def __init__(self, experiences: dict[str, Any] | None = None) -> None:
        self.appels: list[tuple[Any, ...]] = []
        self.experiences = dict(experiences or {})

    def set_tracking_uri(self, uri: str) -> None:
        self.appels.append(("set_tracking_uri", uri))

    def MlflowClient(self, *args: Any, **options: Any) -> ClientFactice:  # noqa: N802
        self.appels.append(("MlflowClient", args, options))
        return ClientFactice(self)

    def set_experiment(self, nom: str) -> None:
        self.appels.append(("set_experiment", nom))


def config_mlflow(config, tracking_uri: str, experience: str = "exp-test"):
    return replace(
        config, mlflow=replace(config.mlflow, tracking_uri=tracking_uri, experience=experience)
    )


def test_preparer_mlflow_local_cree_l_experience_dans_mlartifacts_et_laisse_les_autres_intactes(
    config, monkeypatch, tmp_path
):
    """Magasin local : l'expérience est créée une fois, ses artefacts rangés dans le dossier de
    données (pas dans ``mlruns``) ; les autres expériences restent intactes."""
    faux = MlflowPrepare({"autre": SimpleNamespace(nom="autre")})
    monkeypatch.setitem(sys.modules, "mlflow", faux)
    experiences_avant = dict(faux.experiences)
    configuration = config_mlflow(config, "sqlite:///exemple.db")
    rendu = _preparer_mlflow(configuration)
    emplacement = (tmp_path / "donnees" / "mlartifacts").resolve().as_uri()
    assert emplacement.startswith("file:///")
    assert emplacement.endswith("/donnees/mlartifacts")
    assert faux.appels == [
        ("set_tracking_uri", "sqlite:///exemple.db"),
        ("MlflowClient", (), {}),
        ("get_experiment_by_name", "exp-test"),
        ("create_experiment", "exp-test", {"artifact_location": emplacement}),
        ("set_experiment", "exp-test"),
    ]
    assert rendu is faux
    assert {nom: faux.experiences[nom] for nom in experiences_avant} == experiences_avant


def test_preparer_mlflow_ne_recree_pas_une_experience_existante_et_la_laisse_intacte(
    config, monkeypatch
):
    """Une expérience déjà là (ses runs, ses artefacts) n'est ni recréée ni modifiée."""
    existante = SimpleNamespace(nom="exp-test", artifact_location="file:///ailleurs")
    faux = MlflowPrepare({"exp-test": existante})
    monkeypatch.setitem(sys.modules, "mlflow", faux)
    _preparer_mlflow(config_mlflow(config, "sqlite:///exemple.db"))
    assert faux.appels == [
        ("set_tracking_uri", "sqlite:///exemple.db"),
        ("MlflowClient", (), {}),
        ("get_experiment_by_name", "exp-test"),
        ("set_experiment", "exp-test"),
    ]
    assert faux.experiences == {"exp-test": existante}
    assert existante.artifact_location == "file:///ailleurs"


@pytest.mark.parametrize("uri", ["http://mlflow:5000", "https://suivi.exemple.test"])
def test_preparer_mlflow_distant_n_ouvre_aucun_client_et_ne_cree_aucune_experience(
    config, monkeypatch, uri
):
    """Un serveur MLflow distant range lui-même ses artefacts : on ne crée rien depuis ici."""
    faux = MlflowPrepare({"autre": SimpleNamespace(nom="autre")})
    experiences_avant = dict(faux.experiences)
    monkeypatch.setitem(sys.modules, "mlflow", faux)
    rendu = _preparer_mlflow(config_mlflow(config, uri))
    assert faux.appels == [("set_tracking_uri", uri), ("set_experiment", "exp-test")]
    assert faux.experiences == experiences_avant
    assert rendu is faux


# --------------------------------------------------------------------------------------------
# _journaliser_modele
# --------------------------------------------------------------------------------------------
class MlflowModele:
    """Faux ``mlflow.sklearn`` : refuse ``name`` comme MLflow 2 si ``refuse_name`` est vrai."""

    def __init__(self, refuse_name: bool = False, panne: BaseException | None = None) -> None:
        self.appels: list[tuple[Any, dict[str, Any]]] = []
        self.refuse_name = refuse_name
        self.panne = panne
        self.sklearn = SimpleNamespace(log_model=self.log_model)

    def log_model(self, pipeline: Any, **options: Any) -> str:
        self.appels.append((pipeline, options))
        if self.panne is not None:
            raise self.panne
        if self.refuse_name and "name" in options:
            raise TypeError("log_model() got an unexpected keyword argument 'name'")
        return f"modele-{len(self.appels)}"


def exemple_mixte() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "entier": [1, 2],
            "decimal": [0.5, 1.5],
            "texte": ["a", "b"],
            "booleen": [True, False],
        }
    )


@pytest.mark.parametrize("enregistrer", [False, True])
@pytest.mark.parametrize("refuse_name", [False, True])
def test_journaliser_modele_transmet_les_options_exactes_et_laisse_l_exemple_intact(
    enregistrer, refuse_name
):
    """Compatible MLflow 3 (``name``) et 2 (``artifact_path``) ; l'exemple fourni reste intact
    (les entiers sont convertis dans une copie) ; l'enregistrement n'a lieu que sur demande."""
    mlflow = MlflowModele(refuse_name=refuse_name)
    pipeline = object()
    exemple = exemple_mixte()
    exemple_avant = exemple.copy()
    rendu = _journaliser_modele(mlflow, pipeline, exemple, enregistrer)
    options = {
        "serialization_format": "cloudpickle",
        "pip_requirements": [
            f"scikit-learn=={sklearn.__version__}",
            f"numpy=={np.__version__}",
            f"pandas=={pd.__version__}",
        ],
    }
    if enregistrer:
        options["registered_model_name"] = "detection-fraude"
    essais = mlflow.appels
    assert [p for p, _ in essais] == [pipeline] * (2 if refuse_name else 1)
    noms = [{c: o[c] for c in ("name", "artifact_path") if c in o} for _, o in essais]
    assert noms == (
        [{"name": "modele"}, {"artifact_path": "modele"}] if refuse_name else [{"name": "modele"}]
    )
    for _, transmises in essais:
        transmises = dict(transmises)
        converti = transmises.pop("input_example")
        transmises.pop("name", None)
        transmises.pop("artifact_path", None)
        assert transmises == options
        assert converti.columns.tolist() == ["entier", "decimal", "texte", "booleen"]
        assert converti["entier"].dtype == "float64"  # entier converti en flottant
        assert converti["decimal"].dtype == "float64"
        assert converti["texte"].tolist() == ["a", "b"]  # le texte n'est pas touché
        assert converti["booleen"].dtype == bool  # un booléen n'est pas un nombre ici
        assert converti["entier"].tolist() == [1.0, 2.0]
    assert rendu == ("modele-2" if refuse_name else "modele-1")  # valeur du dernier appel
    pd.testing.assert_frame_equal(exemple, exemple_avant)
    assert exemple["entier"].dtype.kind == "i"  # l'original garde ses entiers


def test_journaliser_modele_ne_rejoue_pas_une_autre_erreur_que_l_incompatibilite_de_signature():
    """Seul un ``TypeError`` (signature MLflow 2) déclenche le repli ; un disque plein, non."""
    mlflow = MlflowModele(panne=OSError("disque plein (test)"))
    with pytest.raises(OSError) as exc:
        _journaliser_modele(mlflow, object(), exemple_mixte(), False)
    assert str(exc.value) == "disque plein (test)"
    assert len(mlflow.appels) == 1  # un seul essai, pas de seconde tentative


def test_journaliser_modele_propage_un_type_error_qui_persiste_au_second_essai():
    """Si ``artifact_path`` est lui aussi refusé, l'erreur remonte : rien n'est masqué."""
    mlflow = MlflowModele(panne=TypeError("signature incompatible (test)"))
    with pytest.raises(TypeError) as exc:
        _journaliser_modele(mlflow, object(), exemple_mixte(), True)
    assert str(exc.value) == "signature incompatible (test)"
    assert [sorted(set(o) & {"name", "artifact_path"}) for _, o in mlflow.appels] == [
        ["name"],
        ["artifact_path"],
    ]


# --------------------------------------------------------------------------------------------
# valider_candidat et promouvoir_version
# --------------------------------------------------------------------------------------------
def test_barriere_accepte_un_candidat_meilleur_et_laisse_le_service_et_les_versions_intacts(
    config, evenements
):
    """Le verdict est exact, l'événement aussi, et la barrière ne fait que lire."""
    config = config_barriere(config)
    publier_candidat(config, "v-service", 0.75, en_service=True)
    publier_candidat(config, "v-candidat", 0.875)
    avant = instantane(config.modele.dossier)
    verdict = valider_candidat(config, "v-candidat")
    assert verdict == {
        "version": "v-candidat",
        "pr_auc_test": 0.875,
        "pr_auc_en_service": 0.75,
        "accepte": True,
        "raisons": [],
    }
    assert evenements.tous() == [
        (
            "INFO",
            "barriere_qualite",
            {
                "version": "v-candidat",
                "pr_auc_test": 0.875,
                "pr_auc_en_service": 0.75,
                "accepte": True,
                "raisons": "",
            },
        )
    ]
    assert instantane(config.modele.dossier) == avant
    assert version_en_service(config.modele.dossier) == "v-service"


@pytest.mark.parametrize(
    "pr_auc_candidat",
    [
        pytest.param(
            0.625, id="pile_a_la_tolerance"
        ),  # 0,75 - 0,125 : strictement inférieur requis
        pytest.param(0.7, id="baisse_tolere"),
        pytest.param(0.75, id="egal_au_service"),
        pytest.param(1.0, id="meilleur_que_le_service"),
    ],
)
def test_barriere_accepte_la_regression_toleree_et_laisse_les_versions_intactes(
    config, pr_auc_candidat
):
    """Une baisse jusqu'à la tolérance incluse n'est pas une régression ; état intact."""
    config = config_barriere(config)
    publier_candidat(config, "v-service", 0.75, en_service=True)
    publier_candidat(config, "v-candidat", pr_auc_candidat)
    avant = instantane(config.modele.dossier)
    verdict = valider_candidat(config, "v-candidat")
    assert (verdict["accepte"], verdict["raisons"]) == (True, [])
    assert verdict["pr_auc_en_service"] == 0.75
    assert instantane(config.modele.dossier) == avant


def test_barriere_accepte_un_premier_modele_pile_au_plancher_sans_creer_de_pointeur(
    config, evenements
):
    """Sans modèle en service il n'y a rien à comparer ; le plancher est inclusif (>=)."""
    config = config_barriere(config)
    publier_candidat(config, "v-premier", 0.25)
    avant = instantane(config.modele.dossier)
    verdict = valider_candidat(config, "v-premier")
    assert verdict == {
        "version": "v-premier",
        "pr_auc_test": 0.25,
        "pr_auc_en_service": None,
        "accepte": True,
        "raisons": [],
    }
    assert evenements.nommes("barriere_qualite") == [
        (
            "INFO",
            {
                "version": "v-premier",
                "pr_auc_test": 0.25,
                "pr_auc_en_service": None,
                "accepte": True,
                "raisons": "",
            },
        )
    ]
    assert instantane(config.modele.dossier) == avant
    assert version_en_service(config.modele.dossier) is None


def test_barriere_ne_compare_pas_un_candidat_a_lui_meme_quand_il_est_deja_en_service(config):
    """Si la version validée est déjà en service, aucune comparaison de régression n'a lieu."""
    config = config_barriere(config)
    publier_candidat(config, "v-1", 0.5, en_service=True)
    avant = instantane(config.modele.dossier)
    verdict = valider_candidat(config, "v-1")
    assert verdict["pr_auc_en_service"] is None
    assert verdict["accepte"] is True
    assert instantane(config.modele.dossier) == avant


@pytest.mark.parametrize(
    ("pr_auc", "texte"),
    [
        pytest.param(0.0, "0.000", id="nul"),
        pytest.param(0.2499, "0.250", id="juste_sous_le_plancher"),
    ],
)
def test_barriere_refuse_sous_le_plancher_avec_le_message_exact_et_journalise_le_refus(
    config, evenements, pr_auc, texte
):
    """Sous le plancher (même d'un cheveu) le candidat est refusé : message, événement, état."""
    config = config_barriere(config)
    publier_candidat(config, "v-candidat", pr_auc)
    avant = instantane(config.modele.dossier)
    message = f"PR-AUC test {texte} sous le plancher 0.250"
    with pytest.raises(BarriereQualiteRefusee) as exc:
        valider_candidat(config, "v-candidat")
    assert str(exc.value) == message
    assert evenements.tous() == [
        (
            "INFO",
            "barriere_qualite",
            {
                "version": "v-candidat",
                "pr_auc_test": pr_auc,
                "pr_auc_en_service": None,
                "accepte": False,
                "raisons": message,
            },
        )
    ]
    assert instantane(config.modele.dossier) == avant
    assert version_en_service(config.modele.dossier) is None


def test_barriere_refuse_un_nan_et_laisse_le_service_intact(config, evenements):
    """Un NaN (jeu de test sans fraude) n'est jamais « au-dessus » du plancher : refusé."""
    config = config_barriere(config)
    publier_candidat(config, "v-service", 0.75, en_service=True)
    publier_candidat(config, "v-nan", float("nan"))
    avant = instantane(config.modele.dossier)
    # NaN n'est pas inférieur à 0,75 - 0,125 : seul le plancher peut le refuser
    with pytest.raises(BarriereQualiteRefusee) as exc:
        valider_candidat(config, "v-nan")
    assert str(exc.value) == "PR-AUC test nan sous le plancher 0.250"
    [(niveau, nom, champs)] = evenements.tous()
    assert (niveau, nom) == ("INFO", "barriere_qualite")
    assert np.isnan(champs.pop("pr_auc_test"))
    assert champs == {
        "version": "v-nan",
        "pr_auc_en_service": 0.75,
        "accepte": False,
        "raisons": "PR-AUC test nan sous le plancher 0.250",
    }
    assert instantane(config.modele.dossier) == avant


def test_barriere_refuse_une_regression_juste_au_dela_de_la_tolerance_avec_le_message_exact(
    config, evenements
):
    """Une baisse supérieure à la tolérance est refusée, sous le plancher ou non."""
    config = config_barriere(config)
    publier_candidat(config, "v-service", 0.75, en_service=True)
    publier_candidat(config, "v-candidat", 0.6249)
    avant = instantane(config.modele.dossier)
    message = "PR-AUC test 0.625 inférieure à celle du modèle en service (0.750) de plus de 0.125"
    with pytest.raises(BarriereQualiteRefusee) as exc:
        valider_candidat(config, "v-candidat")
    assert str(exc.value) == message
    assert evenements.tous() == [
        (
            "INFO",
            "barriere_qualite",
            {
                "version": "v-candidat",
                "pr_auc_test": 0.6249,
                "pr_auc_en_service": 0.75,
                "accepte": False,
                "raisons": message,
            },
        )
    ]
    assert instantane(config.modele.dossier) == avant
    assert version_en_service(config.modele.dossier) == "v-service"


def test_barriere_cumule_les_deux_raisons_dans_l_ordre_et_les_separe_comme_prevu(
    config, evenements
):
    """Sous le plancher ET en régression : deux raisons, jointes par « ; » dans l'exception et
    par « | » dans l'événement d'audit."""
    config = config_barriere(config)
    publier_candidat(config, "v-service", 0.75, en_service=True)
    publier_candidat(config, "v-candidat", 0.125)
    avant = instantane(config.modele.dossier)
    premiere = "PR-AUC test 0.125 sous le plancher 0.250"
    seconde = "PR-AUC test 0.125 inférieure à celle du modèle en service (0.750) de plus de 0.125"
    with pytest.raises(BarriereQualiteRefusee) as exc:
        valider_candidat(config, "v-candidat")
    assert str(exc.value) == f"{premiere}; {seconde}"
    assert evenements.nommes("barriere_qualite") == [
        (
            "INFO",
            {
                "version": "v-candidat",
                "pr_auc_test": 0.125,
                "pr_auc_en_service": 0.75,
                "accepte": False,
                "raisons": f"{premiere} | {seconde}",
            },
        )
    ]
    assert instantane(config.modele.dossier) == avant


def test_barriere_refuse_une_version_inconnue_sans_rien_journaliser_ni_modifier(
    config, evenements
):
    """Valider une version qui n'existe pas échoue avant tout verdict (aucun événement)."""
    config = config_barriere(config)
    publier_candidat(config, "v-service", 0.75, en_service=True)
    avant = instantane(config.modele.dossier)
    with pytest.raises(ErreurModele) as exc:
        valider_candidat(config, "v-fantome")
    assert str(exc.value) == "Métadonnées absentes pour la version v-fantome"
    assert evenements.tous() == []
    assert instantane(config.modele.dossier) == avant


def test_barriere_refuse_un_candidat_sans_metriques_de_test_et_laisse_le_service_intact(
    config, evenements
):
    """Des métadonnées sans ``metriques_test`` ne peuvent pas être acceptées : l'erreur remonte."""
    config = config_barriere(config)
    publier_candidat(config, "v-service", 0.75, en_service=True)
    exporter_modele(config.modele.dossier, pipeline_nul(), 0.5, {"version": "v-sans-metrique"})
    avant = instantane(config.modele.dossier)
    with pytest.raises(KeyError) as exc:
        valider_candidat(config, "v-sans-metrique")
    assert str(exc.value) == "'metriques_test'"
    assert evenements.tous() == []
    assert instantane(config.modele.dossier) == avant


def test_barriere_refuse_quand_les_metadonnees_du_modele_en_service_sont_perdues(
    config, evenements
):
    """Impossible de comparer au modèle en service : refus par exception, jamais acceptation."""
    config = config_barriere(config)
    publier_candidat(config, "v-candidat", 0.9)
    (config.modele.dossier / "ACTUEL").write_text("v-disparue\n", encoding="utf-8")
    avant = instantane(config.modele.dossier)
    with pytest.raises(ErreurModele) as exc:
        valider_candidat(config, "v-candidat")
    assert str(exc.value) == "Métadonnées absentes pour la version v-disparue"
    assert evenements.tous() == []
    assert instantane(config.modele.dossier) == avant


def test_promouvoir_version_bascule_le_pointeur_journalise_et_laisse_les_versions_intactes(
    config, evenements
):
    """La promotion écrit ``ACTUEL`` et l'événement ``modele_promu`` ; les dossiers de versions
    restent intacts."""
    publier_candidat(config, "v-1", 0.5, en_service=True)
    publier_candidat(config, "v-2", 0.6)
    versions_avant = {v: instantane(config.modele.dossier / v) for v in ("v-1", "v-2")}
    promouvoir_version(config, "v-2")
    assert version_en_service(config.modele.dossier) == "v-2"
    assert evenements.tous() == [("INFO", "modele_promu", {"version": "v-2"})]
    assert {v: instantane(config.modele.dossier / v) for v in ("v-1", "v-2")} == versions_avant


def test_promouvoir_version_inconnue_est_refuse_sans_evenement_et_laisse_le_pointeur_intact(
    config, evenements
):
    """Promouvoir une version absente échoue : pas d'événement mensonger, pointeur inchangé."""
    publier_candidat(config, "v-1", 0.5, en_service=True)
    avant = instantane(config.modele.dossier)
    with pytest.raises(ErreurModele) as exc:
        promouvoir_version(config, "v-fantome")
    assert str(exc.value) == "Version inconnue : v-fantome"
    assert evenements.tous() == []
    assert instantane(config.modele.dossier) == avant


# --------------------------------------------------------------------------------------------
# entrainer : câblage prouvé par des doublures
# --------------------------------------------------------------------------------------------
def test_entrainer_envoie_chaque_tranche_a_la_bonne_etape_et_laisse_le_jeu_intact(
    config, monkeypatch, donnees
):
    """Ajustement sur le passé, seuil sur la validation, mesure sur le test avec ce seuil ; le
    fichier de données reste intact."""
    config = config_banc(config)
    banc = brancher_banc(
        monkeypatch,
        rappels=[{"seuil": 0.25}, {"seuil": 0.75}],
        evaluations=[VALIDATION_1, TEST_1, VALIDATION_2, TEST_2],
    )
    jeu_avant = donnees.chemin.read_bytes()
    entrainer(config, donnees.chemin, suivre_mlflow=False)
    retenues = donnees.retenues
    entrainement_, validation, test = (
        retenues.iloc[:198],
        retenues.iloc[198:248],
        retenues.iloc[248:],
    )
    assert banc.construits == [("regression_logistique", 7), ("gradient_boosting", 7)]
    for nom in NOMS_CANDIDATS:
        ajustement = banc.pipelines[nom].ajustements
        assert ajustement == [
            {
                "colonnes": list(COLONNES_MODELE),
                "montants": entrainement_["amt"].tolist(),
                "etiquettes": entrainement_["is_fraud"].tolist(),
            }
        ]
        assert [p["colonnes"] for p in banc.pipelines[nom].predictions] == [
            list(COLONNES_MODELE)
        ] * 2
        assert [p["montants"] for p in banc.pipelines[nom].predictions] == [
            validation["amt"].tolist(),
            test["amt"].tolist(),
        ]
    assert [(len(y), cible) for y, _, cible in banc.rappels] == [(50, 0.8), (50, 0.8)]
    for (y, scores, _), pipeline_nom in zip(banc.rappels, NOMS_CANDIDATS, strict=True):
        assert y.tolist() == validation["is_fraud"].tolist()
        attendus = validation["amt"].to_numpy() / (validation["amt"].to_numpy() + 300.0)
        attendus = attendus if pipeline_nom == "regression_logistique" else 1.0 - attendus
        assert scores.tolist() == pytest.approx(attendus.tolist())
    # évaluations : validation puis test, avec le seuil de CE candidat et la précision cible
    assert [(len(y), seuil, cible) for y, _, seuil, cible in banc.evaluations] == [
        (50, 0.25, 0.8),
        (149, 0.25, 0.8),
        (50, 0.75, 0.8),
        (149, 0.75, 0.8),
    ]
    assert banc.evaluations[1][0].tolist() == test["is_fraud"].tolist()
    assert banc.evaluations[1][1].tolist() == pytest.approx(
        (test["amt"].to_numpy() / (test["amt"].to_numpy() + 300.0)).tolist()
    )
    assert donnees.chemin.read_bytes() == jeu_avant


def test_entrainer_ecrit_des_metadonnees_et_un_retour_exacts_sans_toucher_au_pointeur(
    config, monkeypatch, donnees
):
    """Chaque clé du retour et du fichier ``metadonnees.json`` est vérifiée ; sans demande de
    promotion le modèle en service reste intact (aucun pointeur)."""
    config = config_banc(config)
    brancher_banc(
        monkeypatch,
        rappels=[{"seuil": 0.25}, {"seuil": 0.75}],
        evaluations=[VALIDATION_1, TEST_1, VALIDATION_2, TEST_2],
        horloge=[100.0, 101.0, 103.5, 104.0, 108.0625, 130.0625],
    )
    resultat = entrainer(config, donnees.chemin, suivre_mlflow=False)
    dossier = config.modele.dossier / VERSION
    date_coupure = donnees.retenues.iloc[248]["trans_date_trans_time"][:10]
    assert resultat == {
        "version": VERSION,
        "modele": "gradient_boosting",  # 0,8765 > 0,1235 en validation
        "seuil": 0.75,
        "dossier": str(dossier),
        "metriques_test": TEST_2,
        "metriques_validation": VALIDATION_2,
        "candidats": {
            "regression_logistique": {
                "seuil": 0.25,
                "validation": VALIDATION_1,
                "test": TEST_1,
                "duree_s": 2.5,
            },
            "gradient_boosting": {
                "seuil": 0.75,
                "validation": VALIDATION_2,
                "test": TEST_2,
                "duree_s": 4.0625,
            },
        },
        "date_coupure_test": date_coupure,
        "duree_s": 30.0625,
    }
    metadonnees = lire_metadonnees(config.modele.dossier, VERSION)
    fichier_modele = dossier / "modele.pkl.gz"
    assert metadonnees == {
        "version": VERSION,
        "cree_le": CREE_LE,
        "modele": "gradient_boosting",
        "hyperparametres": HYPERPARAMETRES_GB,
        "precision_cible": 0.8,
        "metriques_validation": VALIDATION_2,
        "metriques_test": TEST_2,
        "comparaison_candidats": {
            "regression_logistique": {"pr_auc_validation": 0.123456, "pr_auc_test": 0.654321},
            "gradient_boosting": {"pr_auc_validation": 0.87654321, "pr_auc_test": 0.98765432},
        },
        "date_coupure_test": date_coupure,
        "decoupage": {"entrainement": 198, "validation": 50, "test": 149},
        "colonnes": list(COLONNES_MODELE),
        "graine": 7,
        "version_regles": VERSION_REGLES,
        "empreinte_donnees_sha256": donnees.sha,
        "bibliotheques": {
            "python": platform.python_version(),
            "scikit-learn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "rejets_a_la_preparation": {"amt:hors_bornes": 3},
        "seuil": 0.75,
        "sha256_modele": hashlib.sha256(fichier_modele.read_bytes()).hexdigest(),
    }
    assert charger_modele(config.modele.dossier, VERSION).pipeline.nom == "gradient_boosting"
    assert not (config.modele.dossier / "ACTUEL").exists()
    assert version_en_service(config.modele.dossier) is None


def test_entrainer_journalise_les_evenements_exacts_et_amorce_le_premier_modele(
    config, monkeypatch, donnees, evenements
):
    """Nom, niveau et champs de chaque événement d'audit, arrondis compris (4 décimales pour
    les PR-AUC, 1 pour les durées) ; la date de création est lue en UTC."""
    jeu_avant = donnees.chemin.read_bytes()
    config = config_banc(config)
    banc = brancher_banc(
        monkeypatch,
        rappels=[{"seuil": 0.25}, {"seuil": 0.75}],
        evaluations=[VALIDATION_1, TEST_1, VALIDATION_2, TEST_2],
        horloge=[100.0, 101.0, 103.5, 104.0, 108.0625, 130.0625],
    )
    entrainer(config, donnees.chemin, suivre_mlflow=False, promouvoir_si_premier=True)
    assert evenements.tous() == [
        ("INFO", "jeu_prepare", {"lignes_lues": 400, "lignes_retenues": 397, "rejets": 3}),
        (
            "INFO",
            "candidat_evalue",
            {
                "modele": "regression_logistique",
                "pr_auc_validation": 0.1235,
                "pr_auc_test": 0.6543,
                "duree_s": 2.5,
            },
        ),
        (
            "INFO",
            "candidat_evalue",
            {
                "modele": "gradient_boosting",
                "pr_auc_validation": 0.8765,
                "pr_auc_test": 0.9877,
                "duree_s": 4.1,
            },
        ),
        ("INFO", "modele_promu_amorcage", {"version": VERSION}),
        (
            "INFO",
            "entrainement_termine",
            {
                "version": VERSION,
                "modele": "gradient_boosting",
                "duree_s": 30.1,
                "pr_auc_test": 0.9877,
            },
        ),
    ]
    assert banc.fuseaux == [UTC]
    assert banc.horloge == []  # six lectures d'horloge exactement : début, 2 x 2, fin
    assert version_en_service(config.modele.dossier) == VERSION
    assert donnees.chemin.read_bytes() == jeu_avant


@pytest.mark.parametrize(
    ("ordre", "sens", "attendu"),
    [
        pytest.param(
            NOMS_CANDIDATS,
            {"regression_logistique": 1, "gradient_boosting": -1},
            "regression_logistique",
            id="le_premier_est_le_meilleur",
        ),
        pytest.param(
            NOMS_CANDIDATS,
            {"regression_logistique": -1, "gradient_boosting": 1},
            "gradient_boosting",
            id="le_second_est_le_meilleur",
        ),
        pytest.param(
            NOMS_CANDIDATS[::-1],
            {"regression_logistique": 1, "gradient_boosting": -1},
            "regression_logistique",
            id="l_ordre_de_la_configuration_n_influence_pas",
        ),
    ],
)
def test_entrainer_choisit_le_candidat_de_plus_haute_pr_auc_de_validation_et_exporte_celui_la(
    config, monkeypatch, donnees, ordre, sens, attendu
):
    """Avec la vraie mesure : le candidat qui classe le mieux la validation est exporté, quel que
    soit l'ordre de la configuration ; l'autre n'est pas exporté."""
    jeu_avant = donnees.chemin.read_bytes()
    config = config_banc(config, candidats=ordre)
    banc = brancher_banc(monkeypatch, sens=sens)
    resultat = entrainer(config, donnees.chemin, suivre_mlflow=False)
    ecarte = next(nom for nom in NOMS_CANDIDATS if nom != attendu)
    assert resultat["modele"] == attendu
    assert banc.construits == [(nom, 7) for nom in ordre]
    assert (
        resultat["candidats"][attendu]["validation"]["pr_auc"]
        > resultat["candidats"][ecarte]["validation"]["pr_auc"]
    )
    assert resultat["metriques_test"] == resultat["candidats"][attendu]["test"]
    assert resultat["seuil"] == resultat["candidats"][attendu]["seuil"]
    exporte = charger_modele(config.modele.dossier, resultat["version"])
    assert exporte.pipeline.nom == attendu
    assert exporte.metadonnees["modele"] == attendu
    assert exporte.seuil == resultat["seuil"]
    assert donnees.chemin.read_bytes() == jeu_avant


@pytest.mark.parametrize(
    ("pr_auc_validation", "attendu"),
    [
        pytest.param([float("nan"), 0.1], "gradient_boosting", id="nan_compte_zero_donc_perd"),
        pytest.param([0.1, float("nan")], "regression_logistique", id="nan_en_second"),
        pytest.param([0.2, 0.2], "regression_logistique", id="egalite_le_premier_garde_la_place"),
        pytest.param([float("nan"), float("nan")], "regression_logistique", id="deux_nan"),
    ],
)
def test_entrainer_traite_un_nan_comme_zero_pour_choisir_le_candidat(
    config, monkeypatch, donnees, pr_auc_validation, attendu
):
    """Une PR-AUC de validation NaN vaut 0 dans le classement ; à égalité, le premier candidat."""
    jeu_avant = donnees.chemin.read_bytes()
    config = config_banc(config)
    brancher_banc(
        monkeypatch,
        rappels=[{"seuil": 0.25}, {"seuil": 0.75}],
        evaluations=[
            {"pr_auc": pr_auc_validation[0]},
            {"pr_auc": 0.5},
            {"pr_auc": pr_auc_validation[1]},
            {"pr_auc": 0.5},
        ],
    )
    resultat = entrainer(config, donnees.chemin, suivre_mlflow=False)
    assert resultat["modele"] == attendu
    assert donnees.chemin.read_bytes() == jeu_avant


def test_entrainer_trace_dans_mlflow_les_parametres_metriques_et_tags_exacts(
    config, monkeypatch, donnees
):
    """Chaque appel MLflow, dans l'ordre : run parent, un run imbriqué par candidat (paramètres,
    seuil, durée, métriques numériques seulement), puis paramètres, tags et métriques du run
    parent, modèle, artefact et fermeture du run."""
    jeu_avant = donnees.chemin.read_bytes()
    config = config_banc(config)
    mlflow, configurations, modeles = brancher_mlflow(monkeypatch)
    brancher_banc(
        monkeypatch,
        rappels=[{"seuil": 0.25}, {"seuil": 0.75}],
        evaluations=[VALIDATION_1, TEST_1, VALIDATION_2, TEST_2],
        horloge=[100.0, 101.0, 103.5, 104.0, 108.0625, 130.0625],
    )
    entrainer(config, donnees.chemin, suivre_mlflow=True)
    date_coupure = donnees.retenues.iloc[248]["trans_date_trans_time"][:10]
    assert configurations == [config]
    assert mlflow.appels == [
        ("start_run", {"run_name": f"entrainement_{VERSION}"}),
        ("start_run", {"run_name": "regression_logistique", "nested": True}),
        ("entree", "regression_logistique"),
        ("log_params", {"hp_C": 1.0, "hp_max_iter": 1000}),
        ("log_param", "candidat", "regression_logistique"),
        ("log_metric", "seuil_decision", 0.25),
        ("log_metric", "duree_entrainement_s", 2.5),
        ("log_metric", "pr_auc_validation", 0.123456),
        ("log_metric", "roc_auc_validation", 0.5),
        ("log_metric", "nb_lignes_validation", 50.0),
        ("log_metric", "pr_auc_test", 0.654321),
        ("log_metric", "nb_fraudes_test", 3.0),
        ("sortie", "regression_logistique"),
        ("start_run", {"run_name": "gradient_boosting", "nested": True}),
        ("entree", "gradient_boosting"),
        ("log_params", {f"hp_{cle}": valeur for cle, valeur in HYPERPARAMETRES_GB.items()}),
        ("log_param", "candidat", "gradient_boosting"),
        ("log_metric", "seuil_decision", 0.75),
        ("log_metric", "duree_entrainement_s", 4.0625),
        ("log_metric", "pr_auc_validation", 0.87654321),
        ("log_metric", "roc_auc_validation", 0.75),
        ("log_metric", "nb_lignes_validation", 50.0),
        ("log_metric", "pr_auc_test", 0.98765432),
        ("log_metric", "rappel_au_seuil_test", 0.5),
        ("log_metric", "nb_fraudes_test", 3.0),
        ("log_metric", "nb_lignes_test", 149.0),
        ("sortie", "gradient_boosting"),
        (
            "log_params",
            {
                "modele_choisi": "gradient_boosting",
                "graine": 7,
                "precision_cible": 0.8,
                "date_coupure_test": date_coupure,
                "lignes_entrainement": 198,
                "lignes_validation": 50,
                "lignes_test": 149,
                "empreinte_donnees_sha256": donnees.sha,
                "version_regles": VERSION_REGLES,
                "version_modele": VERSION,
            },
        ),
        ("set_tags", {"jeu_de_donnees": "jeu.csv", "environnement": "dev"}),
        ("log_metric", "test_pr_auc", 0.98765432),
        ("log_metric", "test_rappel_au_seuil", 0.5),
        ("log_metric", "test_nb_fraudes", 3.0),
        ("log_metric", "test_nb_lignes", 149.0),
        ("log_metric", "test_seuil_decision", 0.75),
        ("journaliser_modele", "gradient_boosting"),
        ("log_artifact", str(config.modele.dossier / VERSION / "metadonnees.json")),
        ("end_run",),
    ]
    assert all(type(a[2]) is float for a in mlflow.appels if a[0] == "log_metric")
    assert modeles == [
        {
            "mlflow_est_le_faux": True,
            "pipeline": "gradient_boosting",
            "index": [248, 249, 250, 251, 252],  # les 5 premières lignes de la tranche de test
            "colonnes": list(COLONNES_MODELE),
            "enregistrer": False,
        }
    ]
    metadonnees = lire_metadonnees(config.modele.dossier, VERSION)
    assert metadonnees["mlflow_run_id"] == "run-1"  # l'identifiant du run PARENT
    assert donnees.chemin.read_bytes() == jeu_avant


def test_entrainer_transmet_l_option_d_enregistrement_du_modele_a_mlflow(
    config, monkeypatch, donnees
):
    """``enregistrer_modele`` de la configuration est transmis tel quel (pas une valeur fixe)."""
    jeu_avant = donnees.chemin.read_bytes()
    configuration = config_banc(
        replace(config, mlflow=replace(config.mlflow, enregistrer_modele=True))
    )
    mlflow, _, modeles = brancher_mlflow(monkeypatch)
    brancher_banc(monkeypatch)
    entrainer(configuration, donnees.chemin)
    assert [m["enregistrer"] for m in modeles] == [True]
    assert mlflow.appels[-1] == ("end_run",)
    assert donnees.chemin.read_bytes() == jeu_avant


def test_entrainer_par_defaut_suit_mlflow_lit_le_jeu_configure_et_laisse_le_service_intact(
    config, monkeypatch, donnees
):
    """Sans argument : jeu pris dans la configuration, suivi MLflow actif, aucune promotion
    (le modèle en service reste intact)."""
    config = config_banc(config)
    config.source.jeu_chemin.write_bytes(donnees.chemin.read_bytes())
    mlflow, configurations, _ = brancher_mlflow(monkeypatch)
    brancher_banc(monkeypatch)
    resultat = entrainer(config)
    assert configurations == [config]
    assert ("set_tags", {"jeu_de_donnees": "jeu.csv", "environnement": "dev"}) in mlflow.appels
    metadonnees = lire_metadonnees(config.modele.dossier, resultat["version"])
    assert metadonnees["empreinte_donnees_sha256"] == donnees.sha
    assert version_en_service(config.modele.dossier) is None
    assert not (config.modele.dossier / "ACTUEL").exists()


def test_entrainer_sans_suivi_n_appelle_jamais_mlflow_et_n_ecrit_que_dans_les_modeles(
    config, monkeypatch, donnees
):
    """``suivre_mlflow=False`` : MLflow n'est même pas préparé ; seul le dossier des modèles est
    écrit (ni base MLflow, ni artefacts), et le jeu reste intact."""

    def interdit(*args, **options):
        raise AssertionError("MLflow ne doit pas être préparé sans suivi")

    monkeypatch.setattr(entrainement, "_preparer_mlflow", interdit)
    config = config_banc(config)
    brancher_banc(monkeypatch)
    jeu_avant = donnees.chemin.read_bytes()
    resultat = entrainer(config, donnees.chemin, suivre_mlflow=False)
    assert sorted(p.name for p in config.racine_donnees.iterdir()) == ["modeles"]
    assert sorted(p.name for p in config.modele.dossier.iterdir()) == [resultat["version"]]
    assert "mlflow_run_id" not in lire_metadonnees(config.modele.dossier, resultat["version"])
    assert donnees.chemin.read_bytes() == jeu_avant


def test_entrainer_limite_le_nombre_de_lignes_lues_et_laisse_le_jeu_intact(
    config, monkeypatch, donnees, evenements
):
    """``limite`` ne lit que les N premières lignes du fichier (ici 50, dont 1 invalide)."""
    config = config_banc(config)
    brancher_banc(monkeypatch)
    jeu_avant = donnees.chemin.read_bytes()
    entrainer(config, donnees.chemin, limite=50, suivre_mlflow=False)
    assert evenements.nommes("jeu_prepare") == [
        ("INFO", {"lignes_lues": 50, "lignes_retenues": 49, "rejets": 1})
    ]
    assert donnees.chemin.read_bytes() == jeu_avant


def test_entrainer_promeut_d_office_le_premier_modele_quand_aucun_n_est_en_service(
    config, monkeypatch, donnees
):
    """Amorçage d'un environnement vierge : sans modèle en service, la version est promue."""
    jeu_avant = donnees.chemin.read_bytes()
    config = config_banc(config)
    brancher_banc(monkeypatch)
    resultat = entrainer(config, donnees.chemin, suivre_mlflow=False, promouvoir_si_premier=True)
    assert (config.modele.dossier / "ACTUEL").read_text(encoding="utf-8") == f"{VERSION}\n"
    assert version_en_service(config.modele.dossier) == resultat["version"] == VERSION
    assert donnees.chemin.read_bytes() == jeu_avant


def test_entrainer_ne_remplace_pas_un_modele_deja_en_service_meme_avec_promouvoir_si_premier(
    config, monkeypatch, donnees, evenements
):
    """Un modèle déjà en service n'est jamais écrasé par l'amorçage : pointeur et ancienne version
    intacts, nouvelle version exportée mais non promue, pas d'événement d'amorçage."""
    config = config_banc(config)
    publier_candidat(config, "v-ancienne", 0.9, en_service=True)
    avant = instantane(config.modele.dossier)
    brancher_banc(monkeypatch)
    resultat = entrainer(config, donnees.chemin, suivre_mlflow=False, promouvoir_si_premier=True)
    assert version_en_service(config.modele.dossier) == "v-ancienne"
    apres = instantane(config.modele.dossier)
    assert {k: v for k, v in apres.items() if not k.startswith(VERSION)} == avant
    assert resultat["version"] == VERSION
    assert (config.modele.dossier / VERSION / "modele.pkl.gz").exists()
    assert "modele_promu_amorcage" not in [nom for _, nom, _ in evenements.tous()]


def test_entrainer_sans_drapeau_n_ecrit_aucun_pointeur_et_n_emet_pas_d_amorcage(
    config, monkeypatch, donnees, evenements
):
    """Par défaut un entraînement ne promeut rien : le service reste inchangé (pas de pointeur)."""
    config = config_banc(config)
    brancher_banc(monkeypatch)
    entrainer(config, donnees.chemin, suivre_mlflow=False)
    assert version_en_service(config.modele.dossier) is None
    assert not (config.modele.dossier / "ACTUEL").exists()
    assert "modele_promu_amorcage" not in [nom for _, nom, _ in evenements.tous()]


# --------------------------------------------------------------------------------------------
# entrainer : refus
# --------------------------------------------------------------------------------------------
def test_entrainer_refuse_un_jeu_introuvable_avec_le_message_exact_sans_rien_ecrire(
    config, monkeypatch, tmp_path, evenements
):
    """Données absentes : le message donne la commande de téléchargement ; ni MLflow, ni modèle."""

    def interdit(*args, **options):
        raise AssertionError("MLflow ne doit pas être préparé quand le jeu est absent")

    monkeypatch.setattr(entrainement, "_preparer_mlflow", interdit)
    absent = tmp_path / "absent.csv"
    avant = instantane(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        entrainer(config, absent)
    assert str(exc.value) == (
        f"Jeu de données introuvable : {absent}. "
        "Le télécharger avec : python -m detection_fraude.telechargement"
    )
    assert evenements.tous() == []
    assert instantane(tmp_path) == avant


def test_entrainer_refuse_un_jeu_sans_ligne_valide_avec_le_message_exact_sans_rien_exporter(
    config, monkeypatch, donnees, tmp_path, evenements
):
    """Si toutes les lignes sont rejetées par le schéma, on s'arrête avant d'apprendre."""
    invalide = tmp_path / "invalide.csv"
    donnees.brut.head(4).assign(amt=-1.0).to_csv(invalide, index=False)
    brancher_banc(monkeypatch)
    with pytest.raises(ValueError) as exc:
        entrainer(config, invalide, suivre_mlflow=False)
    assert str(exc.value) == "Aucune ligne valide dans le jeu (4 rejet(s))."
    assert evenements.tous() == []
    assert not config.modele.dossier.exists()


def test_entrainer_refuse_un_candidat_inconnu_de_la_configuration_et_ferme_le_run_mlflow(
    config, monkeypatch, donnees, evenements
):
    """Un nom de candidat inconnu arrête l'entraînement : rien n'est exporté, le run MLflow ouvert
    est refermé, aucun modèle n'est promu."""
    configuration = config_banc(config, candidats=("devin",))
    mlflow, _, _ = brancher_mlflow(monkeypatch)
    with pytest.raises(ValueError) as exc:
        entrainer(configuration, donnees.chemin, promouvoir_si_premier=True)
    assert str(exc.value) == "Candidat inconnu : devin"
    assert [appel[0] for appel in mlflow.appels] == ["start_run", "end_run"]
    assert list(mlflow.appels[0][1]) == ["run_name"]
    assert mlflow.appels[0][1]["run_name"].startswith("entrainement_")
    assert [nom for _, nom, _ in evenements.tous()] == ["jeu_prepare"]
    assert not configuration.modele.dossier.exists()


def test_entrainer_ferme_le_run_mlflow_et_n_exporte_rien_si_l_ajustement_echoue(
    config, monkeypatch, donnees, evenements
):
    """Une panne pendant l'ajustement laisse le dossier des modèles et le service intacts."""
    config = config_banc(config)
    publier_candidat(config, "v-ancienne", 0.9, en_service=True)
    avant = instantane(config.modele.dossier)
    mlflow, _, _ = brancher_mlflow(monkeypatch)
    brancher_banc(monkeypatch, classe=PipelineEnPanne)
    with pytest.raises(RuntimeError) as exc:
        entrainer(config, donnees.chemin, promouvoir_si_premier=True)
    assert str(exc.value) == "ajustement impossible (test)"
    assert [appel[0] for appel in mlflow.appels] == ["start_run", "end_run"]
    assert [nom for _, nom, _ in evenements.tous()] == ["jeu_prepare"]
    assert instantane(config.modele.dossier) == avant


def test_entrainer_sans_aucun_candidat_est_refuse_et_n_exporte_rien(
    config, monkeypatch, donnees, evenements
):
    """Une configuration sans candidat ne produit aucun modèle : erreur, run refermé."""
    configuration = config_banc(config, candidats=())
    mlflow, _, _ = brancher_mlflow(monkeypatch)
    brancher_banc(monkeypatch)
    with pytest.raises(ValueError):
        entrainer(configuration, donnees.chemin)
    assert [appel[0] for appel in mlflow.appels] == ["start_run", "end_run"]
    assert [nom for _, nom, _ in evenements.tous()] == ["jeu_prepare"]
    assert not configuration.modele.dossier.exists()


# --------------------------------------------------------------------------------------------
# Bouts en bout réels (les deux seuls tests qui ajustent vraiment scikit-learn)
# --------------------------------------------------------------------------------------------
METRIQUES_ATTENDUES = {
    "pr_auc",
    "roc_auc",
    "rappel_a_precision_fixee",
    "cible_atteinte_sur_courbe",
    "precision_au_seuil",
    "rappel_au_seuil",
    "nb_alertes_au_seuil",
    "nb_fraudes",
    "nb_lignes",
}


def test_entrainement_reel_de_bout_en_bout_exporte_un_modele_rechargeable_et_inchange_le_jeu(
    config, donnees, evenements
):
    """~400 lignes, deux vrais candidats : métriques cohérentes avec le découpage, amorçage du
    premier modèle, rechargement par ``charger_modele`` ; le fichier de données est inchangé."""
    jeu_avant = donnees.chemin.read_bytes()
    resultat = entrainer(config, donnees.chemin, suivre_mlflow=False, promouvoir_si_premier=True)
    test = donnees.retenues.iloc[337:]  # 70 % + 15 % de 397 lignes retenues, tronqué
    assert resultat["modele"] in NOMS_CANDIDATS
    assert set(resultat["candidats"]) == set(NOMS_CANDIDATS)
    assert resultat["metriques_test"]["nb_lignes"] == len(test)
    assert resultat["metriques_test"]["nb_fraudes"] == int(test["is_fraud"].sum())
    assert resultat["metriques_test"]["pr_auc"] > 0.5  # la fraude synthétique est apprenable
    assert 0 < resultat["seuil"] < 1
    assert resultat["date_coupure_test"] == test.iloc[0]["trans_date_trans_time"][:10]
    assert set(resultat["metriques_validation"]) == METRIQUES_ATTENDUES
    assert set(resultat["metriques_test"]) == METRIQUES_ATTENDUES
    assert resultat["seuil"] == resultat["candidats"][resultat["modele"]]["seuil"]
    modele = charger_modele(config.modele.dossier)  # amorçage : le modèle est en service
    assert (modele.version, modele.seuil) == (resultat["version"], resultat["seuil"])
    assert modele.metadonnees["decoupage"] == {"entrainement": 277, "validation": 60, "test": 60}
    assert modele.metadonnees["metriques_test"] == resultat["metriques_test"]
    ligne = pd.DataFrame([dict.fromkeys(COLONNES_MODELE, 1.0)])
    ligne["categorie"], ligne["etat"] = "shopping_net", "NY"
    assert 0 <= modele.noter(ligne)[0] <= 1
    assert [nom for _, nom, _ in evenements.tous()] == [
        "jeu_prepare",
        "candidat_evalue",
        "candidat_evalue",
        "modele_promu_amorcage",
        "entrainement_termine",
    ]
    assert donnees.chemin.read_bytes() == jeu_avant


def test_suivi_mlflow_sqlite_reel_trace_parametres_metriques_tags_et_ne_duplique_rien(
    config, donnees, tmp_path
):
    """Un vrai MLflow SQLite : une expérience (jamais dupliquée), un run parent et un run par
    candidat, avec leurs paramètres, métriques et tags ; aucun modèle enregistré par défaut."""
    jeu_avant = donnees.chemin.read_bytes()
    import mlflow

    tracking_uri = f"sqlite:///{(tmp_path / 'suivi.db').as_posix()}"
    configuration = config_mlflow(config, tracking_uri, "exp-reel")
    uri_avant = mlflow.get_tracking_uri()
    try:
        resultat = entrainer(configuration, donnees.chemin, suivre_mlflow=True)
        assert _preparer_mlflow(configuration) is mlflow  # rejouer ne duplique pas l'expérience
        client = mlflow.MlflowClient(tracking_uri=tracking_uri)
        assert sorted(e.name for e in client.search_experiments()) == ["Default", "exp-reel"]
        experience = client.get_experiment_by_name("exp-reel")
        assert experience.artifact_location == (
            (tmp_path / "donnees" / "mlartifacts").resolve().as_uri()
        )
        runs = client.search_runs([experience.experiment_id])
        parent = next(
            r for r in runs if r.data.params.get("version_modele") == resultat["version"]
        )
        enfants = [r for r in runs if r.data.tags.get("mlflow.parentRunId") == parent.info.run_id]
        assert sorted(e.data.params["candidat"] for e in enfants) == sorted(NOMS_CANDIDATS)
        assert parent.data.params["modele_choisi"] == resultat["modele"]
        assert parent.data.params["graine"] == "42"
        assert parent.data.params["empreinte_donnees_sha256"] == donnees.sha
        assert parent.data.tags["jeu_de_donnees"] == "jeu.csv"
        assert parent.data.tags["environnement"] == "dev"
        assert parent.data.metrics["test_pr_auc"] == pytest.approx(
            resultat["metriques_test"]["pr_auc"]
        )
        assert parent.data.metrics["test_seuil_decision"] == pytest.approx(resultat["seuil"])
        for enfant in enfants:
            assert enfant.data.metrics["pr_auc_validation"] == pytest.approx(
                resultat["candidats"][enfant.data.params["candidat"]]["validation"]["pr_auc"]
            )
        assert "metadonnees.json" in {a.path for a in client.list_artifacts(parent.info.run_id)}
        assert client.search_registered_models() == []  # enregistrer_modele est faux par défaut
    finally:
        mlflow.set_tracking_uri(uri_avant)
    metadonnees = lire_metadonnees(configuration.modele.dossier, resultat["version"])
    assert metadonnees["mlflow_run_id"] == parent.info.run_id
    assert donnees.chemin.read_bytes() == jeu_avant


# --------------------------------------------------------------------------------------------
# Précisions : ce que les tests ci-dessus laissaient passer (relevé par la mutation)
# --------------------------------------------------------------------------------------------
def test_l_entrainement_hache_les_cartes_avec_le_sel_et_la_longueur_de_la_configuration(
    config, monkeypatch, tmp_path
):
    """La longueur d'empreinte vient de la configuration (24 ici), pas d'une valeur par défaut."""
    construits: list[tuple[bytes, int]] = []

    class HacheurEspion:
        def __init__(self, cle: bytes, longueur: int) -> None:
            construits.append((cle, longueur))

    def lecture_interrompue(*args: Any, **options: Any):
        raise RuntimeError("lecture interrompue (test)")

    monkeypatch.setattr(entrainement, "HacheurCarte", HacheurEspion)
    monkeypatch.setattr(entrainement, "lire_jeu_brut", lecture_interrompue)
    court = replace(config, longueur_hash=24)
    with pytest.raises(RuntimeError) as erreur:
        entrainer(court, tmp_path / "jeu.csv", suivre_mlflow=False)
    assert str(erreur.value) == "lecture interrompue (test)"
    assert construits == [(court.sel_hachage(), 24)]
