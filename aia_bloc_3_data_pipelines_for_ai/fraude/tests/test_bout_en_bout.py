# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Parcours complet en mode local : collecte, DLQ, notation, couches, notification, rapport, DAG.

Ce fichier vérifie aussi l'engagement de confidentialité (critère 5.1) de la seule façon
convaincante : en cherchant, dans **tous** les fichiers produits par le pipeline, les données
personnelles qui figuraient dans le jeu d'entrée.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from detection_fraude import taches_airflow as taches
from detection_fraude.entrepot import Entrepot
from detection_fraude.journal import configurer_journal
from detection_fraude.local import demo
from detection_fraude.supervision import controler
from tests.helpers import MARQUEUR

# Lignes cassées exprès dans le rejeu : le même nombre pilote la fixture et les vérifications.
NB_CORRUPTIONS = 3


class FauxTi:
    """Doublure du task instance d'Airflow : lit les « XCom » d'un dictionnaire."""

    def __init__(self, valeurs):
        self.valeurs = valeurs

    def xcom_pull(self, task_ids):
        return self.valeurs[task_ids]


@pytest.fixture(scope="module")
def parcours(tmp_path_factory, modele_entraine, jeu_synthetique):
    """Exécute la démonstration locale une fois ; les tests examinent le résultat."""
    racine = tmp_path_factory.mktemp("parcours")
    mp = pytest.MonkeyPatch()
    mp.setenv("FRAUDE_DONNEES", str(racine / "donnees"))
    mp.setenv("FRAUDE_SEL_HACHAGE", "sel-de-test-uniquement-0123456789abcdef")
    mp.setenv("FRAUDE_JEU_CHEMIN", str(racine / "jeu.csv"))
    for variable in (
        "FRAUDE_DSN_ENTREPOT",
        "FRAUDE_WEBHOOK_URL",
        "FRAUDE_ENV",
        "FRAUDE_TYPE_FILE",
    ):
        mp.delenv(variable, raising=False)
    from detection_fraude.configuration import charger_configuration

    config = charger_configuration("dev")
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    shutil.copytree(modele_entraine["config"].modele.dossier, config.modele.dossier)
    jour = jeu_synthetique["trans_date_trans_time"].iloc[-1][:10]
    lignes_du_jour = int(jeu_synthetique["trans_date_trans_time"].str.startswith(jour).sum())
    arguments = argparse.Namespace(
        source="jeu",
        jour=jour,
        nombre=None,
        appels=1,
        intervalle=0,
        cadence=0.0,
        corruptions=NB_CORRUPTIONS,
        pannes=0,
        graine=5,
        reinitialiser=True,
    )
    try:
        code = demo(config, arguments)
        yield {
            "config": config,
            "jour": jour,
            "lignes": lignes_du_jour,
            "code": code,
            "racine": racine,
        }
    finally:
        mp.undo()


def test_la_demonstration_se_termine_correctement(parcours):
    assert parcours["code"] == 0


def test_les_lignes_valides_traversent_toutes_les_couches(parcours):
    entrepot = Entrepot(parcours["config"].dsn_entrepot())
    attendu = parcours["lignes"] - NB_CORRUPTIONS  # lignes corrompues volontairement
    try:
        for table in ("bronze_transactions", "argent_transactions"):
            assert entrepot.scalaire(f"SELECT COUNT(*) FROM {table}") == attendu
        assert entrepot.scalaire("SELECT COUNT(*) FROM or_rapport_quotidien") == 1
        assert entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage") >= 2
    finally:
        entrepot.fermer()


def test_les_lignes_corrompues_sont_en_dlq_et_archivees(parcours):
    entrepot = Entrepot(parcours["config"].dsn_entrepot())
    try:
        rejets = entrepot.requete("SELECT * FROM meta_rejets")
    finally:
        entrepot.fermer()
    assert len(rejets) == NB_CORRUPTIONS
    assert all(r["etape"] == "producteur" and r["raison"] == "schema_invalide" for r in rejets)
    assert all(r["date_evenement"] == parcours["jour"] for r in rejets)


def test_les_fraudes_sont_notifiees_dans_le_fichier_d_alertes(parcours):
    config = parcours["config"]
    entrepot = Entrepot(config.dsn_entrepot())
    try:
        alertes = entrepot.requete("SELECT * FROM or_alertes_fraude")
    finally:
        entrepot.fermer()
    assert alertes, "le jeu synthétique contient des fraudes apprenables"
    assert all(a["statut_notification"] == "envoyee" for a in alertes)
    lignes = config.notification.fichier_alertes.read_text(encoding="utf-8").splitlines()
    notifications = [json.loads(ligne) for ligne in lignes]
    assert len([n for n in notifications if n["type"] == "fraude"]) == len(alertes)
    assert any(n["type"] == "rapport" for n in notifications)


def test_le_rapport_du_matin_existe(parcours):
    config, jour = parcours["config"], parcours["jour"]
    page = (config.rapport.dossier / f"rapport_{jour}.html").read_text(encoding="utf-8")
    assert jour in page and "Fraudes détectées" in page
    assert (config.rapport.dossier / f"fraudes_{jour}.csv").exists()


def test_aucune_donnee_personnelle_dans_aucun_fichier_produit(parcours):
    """Cherche noms, adresses, métiers et cartes du jeu d'entrée dans tous les fichiers."""
    racine: Path = parcours["config"].racine_donnees
    fichiers = [f for f in racine.rglob("*") if f.is_file()]
    assert len(fichiers) >= 8, "le parcours doit avoir produit base, file, journaux, rapports…"
    motifs = [
        re.compile(rb"Prenom" + MARQUEUR.encode()),
        re.compile(rb"Nom" + MARQUEUR.encode()),
        re.compile(rb"Rue (Cachee|Secrete)"),
        re.compile(rb"Metier" + MARQUEUR.encode()),
        re.compile(rb"Ville" + MARQUEUR.encode()),
        re.compile(rb"\b4000000000[0-9]{6}\b"),
        re.compile(rb"\b(19[5-9][0-9])-0[1-9]-1[0-9]\b"),
    ]
    for fichier in fichiers:
        if fichier.name == ".sel_hachage_dev":
            continue
        contenu = fichier.read_bytes()
        for motif in motifs:
            assert not motif.search(contenu), (
                f"{fichier.name} contient une donnée personnelle ({motif.pattern!r})"
            )
    noms = {f.name for f in fichiers}
    assert {"entrepot.db", "file_messages.db", "alertes.jsonl", "audit.jsonl"} <= noms


def test_le_journal_d_audit_est_structure_et_complet(parcours):
    journal = parcours["config"].journal.fichier
    evenements = [json.loads(ligne) for ligne in journal.read_text(encoding="utf-8").splitlines()]
    noms = {e["evenement"] for e in evenements}
    assert {
        "ligne_rejetee",
        "lot_traite",
        "rapport_produit",
        "rejeu_termine",
        "notification_fraude",
        "rejets_archives",
    } <= noms
    lot = next(e for e in evenements if e["evenement"] == "lot_traite")
    assert {"lot", "recus", "nouveaux", "fraudes", "duree_ms", "modele", "horodatage"} <= set(lot)


def test_le_lignage_relie_les_etapes_aux_versions(parcours):
    entrepot = Entrepot(parcours["config"].dsn_entrepot())
    try:
        lignes = entrepot.requete(
            "SELECT etape, entree, sortie, version_regles, version_modele FROM meta_lignage"
        )
        silver = entrepot.requete(
            "SELECT DISTINCT version_modele, version_regles, lot_id FROM argent_transactions"
        )
    finally:
        entrepot.fermer()
    assert {"consommateur", "rapport_du_matin"} <= {ligne["etape"] for ligne in lignes}
    assert all(ligne["version_regles"] for ligne in lignes)
    assert len({s["version_modele"] for s in silver}) == 1 and all(s["lot_id"] for s in silver)


def test_supervision_apres_le_parcours(parcours):
    entrepot = Entrepot(parcours["config"].dsn_entrepot())
    try:
        assert controler(entrepot, parcours["config"]) == []
    finally:
        entrepot.fermer()


def test_les_taches_du_dag_du_matin_s_enchainent(parcours):
    jour = parcours["jour"]
    fin_intervalle = datetime.now(UTC) - timedelta(minutes=1)  # « 07:00 » vient d'arriver
    lendemain = datetime.fromisoformat(jour).replace(tzinfo=UTC) + timedelta(days=1, hours=7)
    assert taches.verifier_donnees_de_la_veille(data_interval_end=lendemain) == jour
    resume = taches.calculer_et_ecrire_rapport(ti=FauxTi({"verifier_donnees_de_la_veille": jour}))
    assert resume["jour"] == jour and resume["nb_paiements"] > 0
    taches.notifier_rapport_du_matin(ti=FauxTi({"calculer_et_ecrire_rapport": resume}))
    sla = taches.controler_sla_rapport(
        data_interval_end=fin_intervalle, ti=FauxTi({"verifier_donnees_de_la_veille": jour})
    )
    assert sla["respecte"] is True
    en_retard = taches.controler_sla_rapport(
        data_interval_end=datetime(2020, 1, 1, tzinfo=UTC),
        ti=FauxTi({"verifier_donnees_de_la_veille": jour}),
    )
    assert en_retard["respecte"] is False and en_retard["retard_min"] > 0


def test_le_dag_du_matin_echoue_sur_une_source_muette(parcours):
    from detection_fraude.rapport import DonneesAbsentes

    with pytest.raises(DonneesAbsentes):
        taches.verifier_donnees_de_la_veille(data_interval_end=datetime(2030, 1, 2, 7, tzinfo=UTC))


def test_les_taches_du_dag_de_reentrainement_et_la_barriere(parcours, modele_entraine):
    config = parcours["config"]
    chemin = taches.verifier_jeu_de_donnees()
    assert Path(chemin) == config.source.jeu_chemin
    version = taches.entrainer_candidats()
    verdict = taches.valider_barriere_qualite(ti=FauxTi({"entrainer_candidats": version}))
    assert verdict["accepte"] is True
    assert taches.promouvoir_modele(ti=FauxTi({"entrainer_candidats": version})) == version
    from detection_fraude.modele import version_en_service

    assert version_en_service(config.modele.dossier) == version
    taches.notifier_reentrainement(version=version)


def test_la_barriere_refusee_est_un_echec_definitif(parcours, monkeypatch):
    from detection_fraude.entrainement import BarriereQualiteRefusee

    def refuse(config, version):
        raise BarriereQualiteRefusee("trop bas")

    monkeypatch.setattr(taches, "valider_candidat", refuse)
    with pytest.raises(RuntimeError, match="Barrière de qualité refusée"):
        taches.valider_barriere_qualite(version="v")


def test_supervision_en_echec_notifie_et_fait_echouer_la_tache(parcours):
    config = parcours["config"]
    (config.modele.dossier / "ACTUEL").unlink()  # le modèle disparaît : anomalie critique
    with pytest.raises(RuntimeError, match="modele_absent"):
        taches.controler_sante()
