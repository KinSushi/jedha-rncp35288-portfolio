# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Entraînement, métriques, export, intégrité et barrière de qualité (critères 2.2 et 3.3)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from detection_fraude.configuration import RACINE_PROJET
from detection_fraude.entrainement import (
    BarriereQualiteRefusee,
    construire_pipeline,
    decouper_chronologiquement,
    evaluer,
    promouvoir_version,
    rappel_a_precision_fixee,
    valider_candidat,
)
from detection_fraude.fonctionnalites import COLONNES_MODELE
from detection_fraude.modele import (
    ChargeurModele,
    ErreurModele,
    charger_modele,
    exporter_modele,
    lire_metadonnees,
    promouvoir,
    version_en_service,
)


# ----- Mesures -----
def test_rappel_a_precision_fixee_sur_un_cas_calculable_a_la_main():
    y = np.array([1, 1, 0, 1, 0, 0, 0, 0])
    s = np.array([0.95, 0.90, 0.85, 0.80, 0.30, 0.20, 0.10, 0.05])
    # Seuil 0,90 : 2 alertes, 2 vraies -> précision 1, rappel 2/3.
    # Seuil 0,80 : 4 alertes, 3 vraies -> précision 0,75.
    resultat = rappel_a_precision_fixee(y, s, precision_cible=0.9)
    assert resultat["cible_atteinte"] is True
    assert resultat["precision"] == 1.0 and resultat["rappel"] == pytest.approx(2 / 3)
    assert resultat["seuil"] == pytest.approx(0.90)
    plus_souple = rappel_a_precision_fixee(y, s, precision_cible=0.7)
    assert plus_souple["rappel"] == 1.0 and plus_souple["precision"] == pytest.approx(0.75)


def test_cible_inatteignable_repli_sur_le_meilleur_f1():
    y = np.array([0, 1, 0, 1])
    s = np.array([0.9, 0.8, 0.7, 0.1])
    resultat = rappel_a_precision_fixee(y, s, precision_cible=0.99)
    assert resultat["cible_atteinte"] is False and 0 < resultat["seuil"] <= 0.9


def test_evaluer_donne_les_metriques_attendues():
    y = np.array([1, 0, 1, 0, 0, 0])
    s = np.array([0.9, 0.8, 0.7, 0.2, 0.1, 0.05])
    m = evaluer(y, s, seuil=0.75, cible=0.5)
    assert m["nb_fraudes"] == 2 and m["nb_lignes"] == 6 and m["nb_alertes_au_seuil"] == 2
    assert m["precision_au_seuil"] == 0.5 and m["rappel_au_seuil"] == 0.5
    assert 0.5 < m["pr_auc"] <= 1 and 0.5 < m["roc_auc"] <= 1


def test_evaluer_sans_fraude_ne_plante_pas():
    assert np.isnan(evaluer(np.zeros(5, dtype=int), np.linspace(0, 1, 5), 0.5, 0.9)["pr_auc"])


def test_decoupage_chronologique_sans_chevauchement():
    df = pd.DataFrame(
        {"horodatage_s": np.random.default_rng(0).permutation(1000), "trans_num": range(1000)}
    )
    ent, val, test = decouper_chronologiquement(df, 0.7, 0.15)
    assert (len(ent), len(val), len(test)) == (700, 150, 150)
    assert ent["horodatage_s"].max() < val["horodatage_s"].min()
    assert val["horodatage_s"].max() < test["horodatage_s"].min()


# ----- Pipelines -----
@pytest.mark.parametrize("nom", ["regression_logistique", "gradient_boosting"])
def test_pipelines_s_ajustent_et_notent(nom, modele_entraine):
    from detection_fraude.fonctionnalites import construire_jeu_modele
    from detection_fraude.jeu_donnees import lignes_depuis_jeu
    from detection_fraude.masquage import HacheurCarte, masquer

    jeu = pd.read_csv(modele_entraine["csv"]).head(1500)
    hacheur = HacheurCarte(b"cle-de-test-pour-le-pipeline")
    masques = pd.DataFrame(
        [masquer(ligne, hacheur, source="jeu") for ligne in lignes_depuis_jeu(jeu)]
    )
    caracteristiques = construire_jeu_modele(masques)
    pipeline = construire_pipeline(nom, 0)
    pipeline.fit(caracteristiques[list(COLONNES_MODELE)], caracteristiques["etiquette_source"])
    scores = pipeline.predict_proba(caracteristiques[list(COLONNES_MODELE)])[:, 1]
    assert scores.shape == (1500,) and ((scores >= 0) & (scores <= 1)).all()


def test_candidat_inconnu():
    with pytest.raises(ValueError, match="inconnu"):
        construire_pipeline("devin", 0)


def test_le_pipeline_gere_une_categorie_inconnue_et_des_manques(modele_entraine):
    config = modele_entraine["config"]
    modele = charger_modele(config.modele.dossier)
    ligne = pd.DataFrame([dict.fromkeys(COLONNES_MODELE, 1)])
    ligne["categorie"], ligne["etat"] = "categorie_jamais_vue", "ZZ"
    ligne["age"] = np.nan
    assert 0 <= modele.noter(ligne)[0] <= 1


# ----- Entraînement complet -----
def test_entrainement_reproductible_et_efficace_sur_le_jeu_synthetique(modele_entraine):
    resultat = modele_entraine["resultat"]
    assert resultat["modele"] in ("regression_logistique", "gradient_boosting")
    assert resultat["metriques_test"]["pr_auc"] > 0.5  # la fraude synthétique est apprenable
    assert set(resultat["candidats"]) == {"regression_logistique", "gradient_boosting"}
    assert 0 < resultat["seuil"] < 1
    assert resultat["date_coupure_test"]


def test_export_metadonnees_et_pointeur(modele_entraine):
    config, resultat = modele_entraine["config"], modele_entraine["resultat"]
    dossier = config.modele.dossier
    assert version_en_service(dossier) == resultat["version"]  # amorçage : premier modèle promu
    meta = lire_metadonnees(dossier, resultat["version"])
    for cle in (
        "sha256_modele",
        "empreinte_donnees_sha256",
        "seuil",
        "colonnes",
        "graine",
        "version_regles",
        "bibliotheques",
        "metriques_test",
        "metriques_validation",
        "date_coupure_test",
        "decoupage",
        "comparaison_candidats",
    ):
        assert cle in meta, cle
    assert meta["colonnes"] == list(COLONNES_MODELE)
    assert sum(meta["decoupage"].values()) > 5000


def test_deux_entrainements_avec_la_meme_graine_donnent_les_memes_scores(
    modele_entraine, tmp_path, monkeypatch
):
    from detection_fraude.configuration import charger_configuration
    from detection_fraude.entrainement import entrainer

    monkeypatch.setenv("FRAUDE_DONNEES", str(tmp_path / "autre"))
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE", "sel-de-test-uniquement-0123456789abcdef")
    config = charger_configuration("dev")
    second = entrainer(config, modele_entraine["csv"], suivre_mlflow=False)
    premier = modele_entraine["resultat"]
    assert second["seuil"] == pytest.approx(premier["seuil"])
    assert second["metriques_test"]["pr_auc"] == pytest.approx(premier["metriques_test"]["pr_auc"])


def test_chargement_refuse_un_fichier_modifie(modele_entraine, tmp_path):
    import shutil

    config = modele_entraine["config"]
    copie = tmp_path / "modeles"
    shutil.copytree(config.modele.dossier, copie)
    version = version_en_service(copie)
    fichier = copie / version / "modele.pkl.gz"
    fichier.write_bytes(fichier.read_bytes() + b"altere")
    with pytest.raises(ErreurModele, match="Empreinte"):
        charger_modele(copie)


def test_aucun_modele_en_service(tmp_path):
    with pytest.raises(ErreurModele, match="Aucun modèle"):
        charger_modele(tmp_path)
    with pytest.raises(ErreurModele, match="inconnue"):
        promouvoir(tmp_path, "v-inexistante")


def test_le_chargeur_recharge_apres_une_promotion(modele_entraine, tmp_path):
    import shutil

    copie = tmp_path / "modeles"
    shutil.copytree(modele_entraine["config"].modele.dossier, copie)
    version = version_en_service(copie)
    chargeur = ChargeurModele(copie)
    assert chargeur.obtenir().version == version
    assert chargeur.obtenir() is chargeur.obtenir()  # pas de rechargement inutile
    nouvelle = "20990101T000000Z"
    shutil.copytree(copie / version, copie / nouvelle)
    meta = json.loads((copie / nouvelle / "metadonnees.json").read_text(encoding="utf-8"))
    meta["version"] = nouvelle
    (copie / nouvelle / "metadonnees.json").write_text(json.dumps(meta), encoding="utf-8")
    promouvoir(copie, nouvelle)
    assert chargeur.obtenir().version == nouvelle  # bascule sans redémarrage


# ----- Barrière de qualité -----
def _copie_avec_candidat(modele_entraine, tmp_path, pr_auc_candidat):
    import shutil

    config = modele_entraine["config"]
    dossier = tmp_path / "modeles"
    shutil.copytree(config.modele.dossier, dossier)
    en_service = version_en_service(dossier)
    candidat = "20990101T000000Z"
    shutil.copytree(dossier / en_service, dossier / candidat)
    meta = json.loads((dossier / candidat / "metadonnees.json").read_text(encoding="utf-8"))
    meta["version"] = candidat
    meta["metriques_test"]["pr_auc"] = pr_auc_candidat
    (dossier / candidat / "metadonnees.json").write_text(json.dumps(meta), encoding="utf-8")
    return replace(config, modele=replace(config.modele, dossier=dossier)), en_service, candidat


def test_barriere_accepte_un_candidat_au_moins_aussi_bon(modele_entraine, tmp_path):
    config, en_service, candidat = _copie_avec_candidat(modele_entraine, tmp_path, 0.99)
    verdict = valider_candidat(config, candidat)
    assert verdict["accepte"] is True
    promouvoir_version(config, candidat)
    assert version_en_service(config.modele.dossier) == candidat


def test_barriere_refuse_sous_le_plancher(modele_entraine, tmp_path):
    config, en_service, candidat = _copie_avec_candidat(modele_entraine, tmp_path, 0.01)
    with pytest.raises(BarriereQualiteRefusee, match="plancher"):
        valider_candidat(config, candidat)
    assert version_en_service(config.modele.dossier) == en_service  # l'ancien reste en service


def test_barriere_refuse_une_regression(modele_entraine, tmp_path):
    config, en_service, candidat = _copie_avec_candidat(modele_entraine, tmp_path, 0.31)
    pr_auc_service = lire_metadonnees(config.modele.dossier, en_service)["metriques_test"][
        "pr_auc"
    ]
    assert pr_auc_service > 0.31 + config.modele.tolerance_regression
    with pytest.raises(BarriereQualiteRefusee, match="en service"):
        valider_candidat(config, candidat)


def test_barriere_refuse_nan(modele_entraine, tmp_path):
    config, _, candidat = _copie_avec_candidat(modele_entraine, tmp_path, float("nan"))
    with pytest.raises(BarriereQualiteRefusee):
        valider_candidat(config, candidat)


# ----- Suivi MLflow -----
def test_le_suivi_mlflow_enregistre_parametres_metriques_et_modele(config, modele_entraine):
    import mlflow

    from detection_fraude.entrainement import entrainer

    config = replace(
        config,
        mlflow=replace(
            config.mlflow, tracking_uri=f"sqlite:///{config.racine_donnees.as_posix()}/mlflow.db"
        ),
    )
    resultat = entrainer(config, modele_entraine["csv"], suivre_mlflow=True)
    client = mlflow.MlflowClient(tracking_uri=config.mlflow.tracking_uri)
    experience = client.get_experiment_by_name("detection-fraude")
    runs = client.search_runs([experience.experiment_id])
    principal = next(r for r in runs if r.data.params.get("version_modele") == resultat["version"])
    assert principal.data.metrics["test_pr_auc"] == pytest.approx(
        resultat["metriques_test"]["pr_auc"]
    )
    assert "test_rappel_a_precision_fixee" in principal.data.metrics
    assert principal.data.params["empreinte_donnees_sha256"]
    enfants = [r for r in runs if r.data.tags.get("mlflow.parentRunId") == principal.info.run_id]
    assert {e.data.params["candidat"] for e in enfants} == {
        "regression_logistique",
        "gradient_boosting",
    }
    artefacts = {a.path for a in client.list_artifacts(principal.info.run_id)}
    assert "metadonnees.json" in artefacts


def test_exporter_modele_ecrit_l_empreinte(tmp_path):
    from sklearn.dummy import DummyClassifier

    pipeline = DummyClassifier().fit([[0], [1]], [0, 1])
    dossier = exporter_modele(tmp_path, pipeline, 0.4, {"version": "v-test"})
    meta = json.loads((dossier / "metadonnees.json").read_text(encoding="utf-8"))
    assert meta["seuil"] == 0.4
    assert re.fullmatch(r"[0-9a-f]{64}", meta["sha256_modele"])  # empreinte SHA-256


def test_le_decoupage_en_tranches_n_utilise_aucun_tirage_aleatoire():
    """Sans ``subsample=None``, scikit-learn tire 200 000 lignes au hasard au-delà de ce seuil."""
    pipeline = construire_pipeline("regression_logistique", 0)
    tranches = pipeline.named_steps["pretraitement"].transformers[0][1].named_steps["tranches"]
    assert tranches.subsample is None


def test_la_reference_lineaire_est_reproductible(modele_entraine):
    from detection_fraude.fonctionnalites import construire_jeu_modele
    from detection_fraude.jeu_donnees import lignes_depuis_jeu
    from detection_fraude.masquage import HacheurCarte, masquer

    jeu = pd.read_csv(modele_entraine["csv"]).head(2500)
    hacheur = HacheurCarte(b"cle-de-test-pour-la-reproductibilite")
    masques = pd.DataFrame(
        [masquer(ligne, hacheur, source="jeu") for ligne in lignes_depuis_jeu(jeu)]
    )
    donnees = construire_jeu_modele(masques)
    scores = []
    for _ in range(2):
        pipeline = construire_pipeline("regression_logistique", 42)
        pipeline.fit(donnees[list(COLONNES_MODELE)], donnees["etiquette_source"])
        scores.append(pipeline.predict_proba(donnees[list(COLONNES_MODELE)])[:, 1])
    assert np.array_equal(scores[0], scores[1])


def test_un_jeu_sans_ligne_valide_est_refuse_clairement(modele_entraine):
    from detection_fraude.jeu_donnees import preparer_jeu_entrainement
    from detection_fraude.masquage import HacheurCarte

    jeu = pd.read_csv(modele_entraine["csv"]).head(20)
    jeu["amt"] = -1.0  # tous les montants deviennent invalides
    with pytest.raises(ValueError, match="Aucune ligne valide"):
        preparer_jeu_entrainement(jeu, HacheurCarte(b"cle-de-test"))


def test_le_rechargement_rate_garde_l_ancien_modele_sans_arreter_le_flux(
    modele_entraine, tmp_path, evenements
):
    import shutil

    copie = tmp_path / "modeles"
    shutil.copytree(modele_entraine["config"].modele.dossier, copie)
    bonne = version_en_service(copie)
    chargeur = ChargeurModele(copie)
    assert chargeur.obtenir().version == bonne
    mauvaise = "20990101T000000Z"
    shutil.copytree(copie / bonne, copie / mauvaise)
    fichier = copie / mauvaise / "modele.pkl.gz"
    fichier.write_bytes(fichier.read_bytes() + b"altere")  # empreinte incorrecte
    promouvoir(copie, mauvaise)
    assert chargeur.obtenir().version == bonne  # l'ancien modèle continue de noter
    assert chargeur.obtenir().version == bonne  # deuxième lot : même version refusée
    echecs = evenements.nommes("rechargement_modele_impossible")
    refus = [champs["version"] for _, champs in echecs]
    assert refus == [mauvaise]  # l'échec est journalisé une seule fois, pas à chaque lot
    meilleure = "20990102T000000Z"
    shutil.copytree(copie / bonne, copie / meilleure)
    meta = json.loads((copie / meilleure / "metadonnees.json").read_text(encoding="utf-8"))
    meta["version"] = meilleure
    (copie / meilleure / "metadonnees.json").write_text(json.dumps(meta), encoding="utf-8")
    promouvoir(copie, meilleure)
    assert chargeur.obtenir().version == meilleure  # la reprise est automatique


def test_le_premier_chargement_sans_modele_echoue_franchement(tmp_path):
    with pytest.raises(ErreurModele, match="Aucun modèle"):
        ChargeurModele(tmp_path).obtenir()


def test_l_export_reste_independant_de_dill_meme_importe_apres_joblib(tmp_path):
    """Régression constatée sous Airflow : dill, importé après joblib, altérait la sérialisation.

    Le test tourne dans un sous-processus pour ne pas modifier le ``pickle`` des autres tests.
    """
    import subprocess
    import sys

    pytest.importorskip("dill")
    programme = f"""
import joblib.numpy_pickle  # joblib d'abord : sa table de sérialisation est copiée à l'import
import dill  # puis dill, qui enrichit pickle comme sous Airflow
from pathlib import Path
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from detection_fraude.modele import exporter_modele, NOM_FICHIER_MODELE

x = np.random.default_rng(0).normal(size=(300, 3))
y = (x[:, 0] > 0.5).astype(int)
modele = HistGradientBoostingClassifier(max_iter=5).fit(x, y)
dossier = exporter_modele(Path(r"{tmp_path}"), modele, 0.5, {{"version": "v-dill"}})
import gzip
octets = gzip.open(dossier / NOM_FICHIER_MODELE).read()
print("dill" in octets.decode("latin-1"))
"""
    # -I (mode isolé) ignorerait PYTHONPATH : on lance donc depuis la racine du projet.
    resultat = subprocess.run(
        [sys.executable, "-c", programme],
        capture_output=True,
        text=True,
        cwd=str(RACINE_PROJET),
        env={**os.environ, "PYTHONPATH": str(RACINE_PROJET)},
    )
    assert resultat.returncode == 0, resultat.stderr[-500:]
    assert resultat.stdout.strip().endswith("False"), "l'artefact référence dill"


def test_le_modele_exporte_ne_depend_que_des_bibliotheques_de_scoring(modele_entraine):
    """Enregistre chaque module demandé au chargement : ni dill, ni cloudpickle, ni mlflow."""
    import gzip
    import pickle

    modules = set()

    class Espion(pickle.Unpickler):
        def find_class(self, module, nom):
            modules.add(module.split(".")[0])
            return super().find_class(module, nom)

    dossier = modele_entraine["config"].modele.dossier
    with gzip.open(dossier / version_en_service(dossier) / "modele.pkl.gz", "rb") as flux:
        Espion(flux).load()
    assert modules, "l'espion n'a rien enregistré"
    assert modules.isdisjoint({"dill", "cloudpickle", "mlflow", "airflow"}), modules
    assert modules <= {"sklearn", "numpy", "scipy", "pandas", "builtins", "collections",
                       "copyreg", "detection_fraude", "_codecs", "functools",
                       "itertools", "datetime", "_pickle", "__builtin__"}, modules  # fmt: skip
