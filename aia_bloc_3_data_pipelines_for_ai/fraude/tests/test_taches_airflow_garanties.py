# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tâches Airflow : refus définitifs, ressources rendues, valeurs transmises (critères 3.1 à 6.1).

Les tâches ne contiennent que de la plomberie, mais c'est elle qui décide si Airflow relance
(``RuntimeError`` / ``AirflowFailException`` = échec définitif), si la base est refermée et si
l'équipe est prévenue. Chaque test remplace Airflow par une doublure minimale et les canaux par un
enregistreur, puis compare l'état *avant* à l'état *après* quand une tâche est refusée.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, text

from detection_fraude import taches_airflow as taches
from detection_fraude.entrainement import BarriereQualiteRefusee
from detection_fraude.entrepot import Entrepot, construire_insertion
from detection_fraude.modele import ErreurModele, exporter_modele, promouvoir, version_en_service
from detection_fraude.rapport import DonneesAbsentes
from tests.helpers import CanalEnregistreur
from tests.test_rapport import ligne_argent
from tests.test_rapport_garanties import inserer, instantane
from tests.test_retraitement_garanties import PipelineMontant

JOUR = "2026-10-09"
PLANIFIE = datetime(2026, 10, 10, 5, 0, tzinfo=UTC)  # « 07:00 » de Paris, en UTC


# --------------------------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------------------------
class FauxTi:
    """Doublure du task instance : ``xcom_pull`` n'accepte que des identifiants connus."""

    def __init__(self, valeurs: dict[str, Any]) -> None:
        self.valeurs = valeurs
        self.demandes: list[str] = []

    def xcom_pull(self, task_ids: str) -> Any:
        self.demandes.append(task_ids)
        return self.valeurs[task_ids]


class TiInterdit:
    """Doublure qui échoue si on la consulte : prouve qu'un argument explicite est prioritaire."""

    def xcom_pull(self, task_ids: str) -> Any:
        raise AssertionError(f"xcom_pull({task_ids!r}) ne devait pas être appelé")


@pytest.fixture(autouse=True)
def journal_enregistre(monkeypatch):
    """``_contexte`` règle le journal : on garde ses arguments et on laisse le collecteur."""
    appels: list[tuple[Any, ...]] = []
    monkeypatch.setattr(taches, "configurer_journal", lambda *args: appels.append(args))
    return appels


@pytest.fixture
def fermetures(monkeypatch):
    """Enregistre chaque fermeture de l'entrepôt ouvert par une tâche."""
    appels: list[str] = []

    class EntrepotEspion(Entrepot):
        def fermer(self) -> None:
            appels.append(self.dialecte)
            super().fermer()

    monkeypatch.setattr(taches, "Entrepot", EntrepotEspion)
    return appels


@pytest.fixture
def canal(monkeypatch):
    """Un seul canal enregistreur ; garde aussi les configurations reçues par le constructeur."""
    enregistreur = CanalEnregistreur()
    enregistreur.configurations = []

    def construire(config):
        enregistreur.configurations.append(config)
        return [enregistreur]

    monkeypatch.setattr(taches, "construire_canaux", construire)
    return enregistreur


def installer_faux_airflow(monkeypatch) -> type[Exception]:
    """Installe un faux ``airflow.exceptions`` et rend sa classe d'échec définitif."""

    class AirflowFailException(Exception):
        pass

    exceptions = types.ModuleType("airflow.exceptions")
    exceptions.AirflowFailException = AirflowFailException
    monkeypatch.setitem(sys.modules, "airflow", types.ModuleType("airflow"))
    monkeypatch.setitem(sys.modules, "airflow.exceptions", exceptions)
    return AirflowFailException


@pytest.fixture
def faux_airflow(monkeypatch):
    """Un module ``airflow.exceptions`` minimal : ``AirflowFailException`` est la classe rendue."""
    return installer_faux_airflow(monkeypatch)


@pytest.fixture
def sans_airflow(monkeypatch):
    """Airflow est absent, même s'il est installé : l'import échoue."""
    monkeypatch.setitem(sys.modules, "airflow", None)
    monkeypatch.setitem(sys.modules, "airflow.exceptions", None)


def enregistrer_rapport(entrepot: Entrepot, genere_le: str, **valeurs: Any) -> None:
    ligne = {
        "jour": JOUR,
        "nb_paiements": 5,
        "nb_fraudes": 2,
        "montant_total": 1175.0,
        "montant_fraudes": 1000.0,
        "taux_fraude_nb": 0.4,
        "taux_fraude_montant": 0.85,
        "nb_rejets": 3,
        "nb_etiquetees": 0,
        "nb_vrais_positifs": 0,
        "nb_fraudes_reelles": 0,
        "genere_le": genere_le,
        "version_regles": "r",
        "chemin_html": "a.html",
        "chemin_csv": "b.csv",
    }
    ligne.update(valeurs)
    with entrepot.transaction() as conn:
        conn.execute(
            text(construire_insertion("or_rapport_quotidien", list(ligne), "jour")), ligne
        )


def exporter(config, version: str, **metadonnees: Any) -> None:
    exporter_modele(
        config.modele.dossier, PipelineMontant(100.0), 0.5, {"version": version, **metadonnees}
    )


# --------------------------------------------------------------------------------------------
# echec_definitif
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("airflow_present", [False, True], ids=["sans_airflow", "avec_airflow"])
def test_echec_definitif_rend_runtimeerror_hors_airflow_et_airflowfailexception_sinon(
    monkeypatch, airflow_present
):
    """Hors Airflow l'erreur est un ``RuntimeError`` ; dans Airflow, celle qu'il ne relance pas."""
    if airflow_present:
        attendue = installer_faux_airflow(monkeypatch)
    else:
        monkeypatch.setitem(sys.modules, "airflow", None)
        monkeypatch.setitem(sys.modules, "airflow.exceptions", None)
        attendue = RuntimeError
    with pytest.raises(attendue) as erreur:
        raise taches.echec_definitif("barrière refusée")
    assert type(erreur.value) is attendue
    assert str(erreur.value) == "barrière refusée"


# --------------------------------------------------------------------------------------------
# DAG « rapport du matin »
# --------------------------------------------------------------------------------------------
def test_verifier_donnees_de_la_veille_convertit_la_date_logique_et_refuse_une_source_muette(
    config, entrepot, evenements, fermetures
):
    """La veille se calcule en UTC quelle que soit la forme de la date ; sans donnée : refus."""
    inserer(entrepot, [ligne_argent(1, jour="2026-10-08")])
    inserer(entrepot, [ligne_argent(2), ligne_argent(3)])
    cas = [
        (datetime(2026, 10, 10, 7, 0, tzinfo=UTC), JOUR, 2),
        (datetime(2026, 10, 10, 1, 0), JOUR, 2),  # naïf = UTC
        (datetime(2026, 10, 10, 23, 30), JOUR, 2),  # naïf tard le soir : pas de conversion locale
        (datetime.fromisoformat("2026-10-10T01:00:00+02:00"), "2026-10-08", 1),
        (datetime.fromisoformat("2026-10-09T22:00:00-05:00"), JOUR, 2),
        ("2026-10-10T07:00:00+00:00", JOUR, 2),
        ("2026-10-10T01:00:00+02:00", "2026-10-08", 1),
        ("2026-10-10T01:00:00", JOUR, 2),  # chaîne sans fuseau : lue comme UTC par la veille
    ]
    for instant, jour_attendu, transactions in cas:
        evenements.enregistrements.clear()
        assert taches.verifier_donnees_de_la_veille(data_interval_end=instant) == jour_attendu
        assert evenements.tous() == [
            (
                "INFO",
                "donnees_de_la_veille_ok",
                {"jour": jour_attendu, "transactions": transactions},
            )
        ], instant
    assert fermetures == ["sqlite"] * len(cas)
    etat_avant = instantane(entrepot)
    evenements.enregistrements.clear()
    with pytest.raises(DonneesAbsentes) as erreur:
        taches.verifier_donnees_de_la_veille(
            data_interval_end=datetime(2026, 10, 12, 7, tzinfo=UTC)
        )
    assert str(erreur.value) == "0 transaction(s) le 2026-10-11 : source probablement muette."
    assert evenements.nommes("donnees_de_la_veille_ok") == []
    assert instantane(entrepot) == etat_avant
    assert fermetures == ["sqlite"] * (len(cas) + 1)


def test_les_taches_a_base_initialisent_l_entrepot_et_reglent_le_journal_avant_de_refuser(
    config, journal_enregistre, fermetures
):
    """Sur un dossier vierge, la tâche crée les tables et règle le journal, puis échoue net."""
    assert not config.racine_donnees.exists()
    with pytest.raises(DonneesAbsentes):
        taches.verifier_donnees_de_la_veille(data_interval_end=PLANIFIE)
    assert journal_enregistre == [("INFO", "lisible", config.journal.fichier)]
    verification = Entrepot(config.dsn_entrepot())
    try:
        tables = {
            ligne["name"]
            for ligne in verification.requete("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        verification.fermer()
    assert {"argent_transactions", "or_rapport_quotidien", "meta_lignage"} <= tables
    assert fermetures == ["sqlite"]


def test_calculer_et_ecrire_rapport_ecrit_les_fichiers_et_refuse_un_jour_invalide_sans_rien_ecrire(
    config, entrepot, fermetures
):
    """Le jour vient du XCom (ou de l'argument, prioritaire) ; un jour mal écrit n'écrit rien."""
    inserer(entrepot, [ligne_argent(1), ligne_argent(2, fraude=1)])
    ti = FauxTi({"verifier_donnees_de_la_veille": JOUR})
    resume = taches.calculer_et_ecrire_rapport(ti=ti, run_id="autre-argument-ignore")
    html = str(config.rapport.dossier / "rapport_2026-10-09.html")
    csv_ = str(config.rapport.dossier / "fraudes_2026-10-09.csv")
    assert resume == {"jour": JOUR, "nb_paiements": 2, "nb_fraudes": 1, "html": html, "csv": csv_}
    assert ti.demandes == ["verifier_donnees_de_la_veille"]
    assert Path(html).exists() and Path(csv_).exists()
    assert (
        entrepot.scalaire("SELECT nb_fraudes FROM or_rapport_quotidien WHERE jour = '2026-10-09'")
        == 1
    )
    # Un argument explicite est prioritaire : le XCom n'est pas consulté.
    assert taches.calculer_et_ecrire_rapport(ti=TiInterdit(), jour=JOUR)["nb_paiements"] == 2
    assert fermetures == ["sqlite", "sqlite"]
    etat_avant = instantane(entrepot)
    with pytest.raises(ValueError) as erreur:
        taches.calculer_et_ecrire_rapport(jour="10/10/2026")
    assert str(erreur.value) == "Invalid isoformat string: '10/10/2026'"
    assert instantane(entrepot) == etat_avant
    assert fermetures == ["sqlite", "sqlite", "sqlite"]


def test_notifier_rapport_du_matin_envoie_le_resume_de_la_base_et_refuse_un_rapport_absent(
    config, entrepot, canal, fermetures
):
    """Le message est construit depuis le rapport enregistré ; sans rapport, rien n'est envoyé."""
    etat_avant = instantane(entrepot)
    with pytest.raises(ValueError) as erreur:
        taches.notifier_rapport_du_matin(resume={"jour": JOUR})
    assert str(erreur.value) == "not enough values to unpack (expected 1, got 0)"
    assert canal.recus == []
    assert instantane(entrepot) == etat_avant
    assert fermetures == ["sqlite"]

    enregistrer_rapport(entrepot, "2026-10-10T06:00:00+00:00")
    ti = FauxTi({"calculer_et_ecrire_rapport": {"jour": JOUR}})
    assert taches.notifier_rapport_du_matin(ti=ti, extra="ignore") is None
    assert ti.demandes == ["calculer_et_ecrire_rapport"]
    (notification,) = canal.recus
    assert notification.type == "rapport"
    assert notification.sujet == "[Rapport du matin] 2026-10-09 : 2 fraude(s) sur 5 paiements"
    assert notification.corps == (
        "Rapport du matin pour le 2026-10-09 (jour UTC).\n"
        "- Paiements : 5 pour 1175.00\n"
        "- Fraudes détectées : 2 pour 1000.00 (40.000 % des paiements)\n"
        "- Lignes rejetées (DLQ) : 3\n"
        "- Rapport HTML : a.html\n"
        "- Liste CSV : b.csv"
    )
    assert notification.donnees == {"jour": JOUR, "nb_fraudes": 2}
    assert canal.configurations == [config]
    # Un résumé explicite est prioritaire sur le XCom.
    taches.notifier_rapport_du_matin(ti=TiInterdit(), resume={"jour": JOUR})
    assert [n.sujet[:16] for n in canal.recus] == ["[Rapport du mati"] * 2
    assert canal.configurations == [config, config]
    assert fermetures == ["sqlite", "sqlite", "sqlite"]


def test_le_controle_du_sla_laisse_passer_a_la_limite_alerte_au_dela_et_refuse_un_rapport_absent(
    config, entrepot, canal, evenements, fermetures, monkeypatch
):
    """Livré à l'heure + 60 min pile : respecté ; une seconde de plus : alerte ; rien : échec."""
    etat_avant = instantane(entrepot)
    with pytest.raises(TypeError) as erreur:
        taches.controler_sla_rapport(data_interval_end=PLANIFIE, jour=JOUR)
    assert str(erreur.value) == "fromisoformat: argument must be str"
    assert canal.recus == [] and evenements.nommes("sla_depasse") == []
    assert instantane(entrepot) == etat_avant

    ti = FauxTi({"verifier_donnees_de_la_veille": JOUR})
    enregistrer_rapport(entrepot, "2026-10-10T06:00:00+00:00")  # pile 05:00 UTC + 60 min
    assert taches.controler_sla_rapport(data_interval_end=PLANIFIE, ti=ti) == {
        "respecte": True,
        "retard_min": 0.0,
    }
    assert ti.demandes == ["verifier_donnees_de_la_veille"]
    naif = PLANIFIE.replace(tzinfo=None)  # un datetime naïf est lu comme UTC
    assert taches.controler_sla_rapport(data_interval_end=naif, jour=JOUR)["respecte"] is True
    assert canal.recus == [] and evenements.nommes("sla_depasse") == []

    def livre_a(genere_le: str) -> None:
        with entrepot.transaction() as conn:
            conn.execute(text("UPDATE or_rapport_quotidien SET genere_le = :g"), {"g": genere_le})

    livre_a("2026-10-10T06:00:01+00:00")  # une seconde de retard
    assert taches.controler_sla_rapport(data_interval_end=PLANIFIE, jour=JOUR) == {
        "respecte": False,
        "retard_min": 0.0,
    }
    assert [n.sujet for n in canal.recus] == [
        "[SLA] rapport du 2026-10-09 livré avec 0 min de retard"
    ]
    assert evenements.nommes("sla_depasse") == [
        ("ERROR", {"sla": "rapport_du_matin", "retard_min": 0.0})
    ]

    canal.recus.clear()
    evenements.enregistrements.clear()
    livre_a("2026-10-10T06:15:00+00:00")  # 15 minutes de retard
    resultat = taches.controler_sla_rapport(data_interval_end=PLANIFIE.isoformat(), jour=JOUR)
    assert resultat == {"respecte": False, "retard_min": 15.0}
    (notification,) = canal.recus
    assert notification.type == "supervision"
    assert notification.sujet == "[SLA] rapport du 2026-10-09 livré avec 15 min de retard"
    assert notification.corps == (
        "Le rapport du matin devait être livré 60 min après l'heure prévue."
    )
    assert notification.donnees == {"jour": JOUR, "retard_min": 15.0}
    assert evenements.nommes("sla_depasse") == [
        ("ERROR", {"sla": "rapport_du_matin", "retard_min": 15.0})
    ]

    canal.recus.clear()
    config_30 = replace(config, rapport=replace(config.rapport, sla_minutes=30))
    monkeypatch.setattr(taches, "charger_configuration", lambda: config_30)
    evenements.enregistrements.clear()
    livre_a("2026-10-10T05:45:33+00:00")  # 15,55 minutes après la limite de 30 minutes
    resultat = taches.controler_sla_rapport(data_interval_end=PLANIFIE, jour=JOUR)
    assert resultat == {"respecte": False, "retard_min": 15.6}  # arrondi au dixième
    assert canal.recus[0].sujet == "[SLA] rapport du 2026-10-09 livré avec 16 min de retard"
    assert canal.recus[0].corps == (
        "Le rapport du matin devait être livré 30 min après l'heure prévue."
    )
    assert canal.recus[0].donnees == {"jour": JOUR, "retard_min": 15.6}
    assert evenements.nommes("sla_depasse") == [
        ("ERROR", {"sla": "rapport_du_matin", "retard_min": 15.6})
    ]
    assert fermetures == ["sqlite"] * 6


# --------------------------------------------------------------------------------------------
# DAG « réentraînement hebdomadaire »
# --------------------------------------------------------------------------------------------
def test_verifier_jeu_de_donnees_transmet_la_configuration_et_laisse_remonter_un_refus(
    config, journal_enregistre, monkeypatch, fermetures
):
    """Un téléchargement refusé (taille, empreinte) fait échouer la tâche au lieu d'être masqué."""
    appels: list[Any] = []

    def telecharger(configuration):
        appels.append(configuration)
        return Path("data") / "fraudTest.csv"

    monkeypatch.setattr(taches, "telecharger_jeu", telecharger)
    assert taches.verifier_jeu_de_donnees(run_id="ignore") == str(Path("data") / "fraudTest.csv")
    assert appels == [config]
    assert journal_enregistre == [("INFO", "lisible", config.journal.fichier)]

    def refuse(configuration):
        raise RuntimeError(
            "Téléchargement refusé : empreinte SHA-256 différente de celle attendue"
        )

    monkeypatch.setattr(taches, "telecharger_jeu", refuse)
    with pytest.raises(RuntimeError) as erreur:
        taches.verifier_jeu_de_donnees()
    assert str(erreur.value) == (
        "Téléchargement refusé : empreinte SHA-256 différente de celle attendue"
    )
    assert fermetures == []  # cette tâche n'ouvre pas l'entrepôt


def test_entrainer_candidats_renvoie_la_version_en_texte_et_laisse_remonter_une_panne(
    config, journal_enregistre, monkeypatch
):
    """La version part en XCom sous forme de texte ; une panne d'entraînement n'est pas avalée."""
    appels: list[tuple[Any, ...]] = []

    def entrainer(*args, **kwargs):
        appels.append((args, kwargs))
        return {"version": 20261010, "autre": "ignoré"}

    monkeypatch.setattr(taches, "entrainer", entrainer)
    assert taches.entrainer_candidats(run_id="ignore") == "20261010"
    assert appels == [((config,), {})]
    assert journal_enregistre == [("INFO", "lisible", config.journal.fichier)]

    def en_panne(configuration):
        raise MemoryError("mémoire insuffisante (test)")

    monkeypatch.setattr(taches, "entrainer", en_panne)
    with pytest.raises(MemoryError) as erreur:
        taches.entrainer_candidats()
    assert str(erreur.value) == "mémoire insuffisante (test)"


@pytest.mark.parametrize("avec_airflow", [False, True], ids=["sans_airflow", "avec_airflow"])
def test_une_barriere_refusee_est_un_echec_definitif_et_ne_promeut_rien(
    config, monkeypatch, avec_airflow
):
    """PR-AUC sous le plancher : échec non relancé, message exact, cause gardée, rien promu."""
    if avec_airflow:
        classe = installer_faux_airflow(monkeypatch)
    else:
        monkeypatch.setitem(sys.modules, "airflow", None)
        monkeypatch.setitem(sys.modules, "airflow.exceptions", None)
        classe = RuntimeError
    exporter(config, "v-cand", metriques_test={"pr_auc": 0.1})
    pointeur_avant = version_en_service(config.modele.dossier)
    ti = FauxTi({"entrainer_candidats": "v-cand"})
    with pytest.raises(classe) as erreur:
        taches.valider_barriere_qualite(ti=ti)
    assert type(erreur.value) is classe
    assert str(erreur.value) == (
        "Barrière de qualité refusée : PR-AUC test 0.100 sous le plancher 0.300"
    )
    assert isinstance(erreur.value.__cause__, BarriereQualiteRefusee)
    assert ti.demandes == ["entrainer_candidats"]
    assert version_en_service(config.modele.dossier) == pointeur_avant is None


def test_barriere_acceptee_renvoie_le_verdict_et_une_autre_erreur_reste_relancable(
    config, monkeypatch, sans_airflow
):
    """Seul un refus de qualité est définitif : une autre panne garde son type et sera relancée."""
    exporter(config, "v-ok", metriques_test={"pr_auc": 0.35})
    assert taches.valider_barriere_qualite(version="v-ok") == {
        "version": "v-ok",
        "pr_auc_test": 0.35,
        "pr_auc_en_service": None,
        "accepte": True,
        "raisons": [],
    }

    def panne_disque(configuration, version):
        raise KeyError("metriques_test")

    monkeypatch.setattr(taches, "valider_candidat", panne_disque)
    with pytest.raises(KeyError) as erreur:
        taches.valider_barriere_qualite(version="v-ok")
    assert type(erreur.value) is KeyError
    assert erreur.value.args == ("metriques_test",)


def test_promouvoir_une_version_inconnue_est_refuse_pointeur_intact_puis_la_bonne_est_annoncee(
    config, canal
):
    """Promotion atomique : l'ancien modèle reste en service si la version n'existe pas."""
    exporter(config, "v0")
    exporter(config, "v1")
    promouvoir(config.modele.dossier, "v0")
    pointeur_avant = (config.modele.dossier / "ACTUEL").read_bytes()
    with pytest.raises(ErreurModele) as erreur:
        taches.promouvoir_modele(version="v-inconnue")
    assert str(erreur.value) == "Version inconnue : v-inconnue"
    assert (config.modele.dossier / "ACTUEL").read_bytes() == pointeur_avant
    assert canal.recus == []

    ti = FauxTi({"entrainer_candidats": "v1"})
    assert taches.promouvoir_modele(ti=ti, run_id="ignore") == "v1"
    assert ti.demandes == ["entrainer_candidats"]
    assert version_en_service(config.modele.dossier) == "v1"
    # Une version explicite est prioritaire sur le XCom.
    assert taches.promouvoir_modele(ti=TiInterdit(), version="v0") == "v0"
    assert version_en_service(config.modele.dossier) == "v0"

    ti_promotion = FauxTi({"promouvoir_modele": "v1"})
    assert taches.notifier_reentrainement(ti=ti_promotion, run_id="ignore") is None
    assert ti_promotion.demandes == ["promouvoir_modele"]
    (notification,) = canal.recus
    assert notification.type == "rapport"
    assert notification.sujet == "[Modèle] version v1 mise en service"
    assert notification.corps == (
        "Le réentraînement hebdomadaire a réussi et la barrière de qualité est franchie."
    )
    assert notification.donnees == {"version": "v1"}
    taches.notifier_reentrainement(ti=TiInterdit(), version="v9")
    assert canal.recus[1].sujet == "[Modèle] version v9 mise en service"
    assert canal.configurations == [config, config]


# --------------------------------------------------------------------------------------------
# DAG « supervision »
# --------------------------------------------------------------------------------------------
def test_controler_sante_renvoie_ok_puis_refuse_en_echec_definitif_et_notifie_quand_ca_se_degrade(
    config, entrepot, canal, fermetures, sans_airflow
):
    """Santé : ``{"ok": True}`` sans bruit ; anomalie : alerte envoyée puis échec non relancé."""
    pointeur = config.modele.dossier / "ACTUEL"
    config.modele.dossier.mkdir(parents=True, exist_ok=True)
    pointeur.write_text("v1\n", encoding="utf-8")
    inserer(entrepot, [ligne_argent(1)])  # traité à l'instant : le flux est frais
    assert taches.controler_sante(run_id="ignore") == {"ok": True}
    assert canal.recus == []
    assert fermetures == ["sqlite"]

    pointeur.unlink()  # le modèle disparaît
    etat_avant = instantane(entrepot)
    with pytest.raises(RuntimeError) as erreur:
        taches.controler_sante()
    assert type(erreur.value) is RuntimeError
    assert str(erreur.value) == "modele_absent: Aucun modèle en service."
    assert [(n.type, n.sujet, n.corps) for n in canal.recus] == [
        (
            "supervision",
            "[SUPERVISION CRITIQUE] modele_absent",
            "Aucun modèle en service.",
        )
    ]
    assert instantane(entrepot) == etat_avant
    assert fermetures == ["sqlite", "sqlite"]


def test_controler_sante_joint_les_constats_dans_l_ordre_et_leve_airflowfailexception(
    config, entrepot, canal, faux_airflow
):
    """Plusieurs anomalies : un seul message d'échec, constats séparés par « ; », tous notifiés."""
    with pytest.raises(faux_airflow) as erreur:
        taches.controler_sante()
    assert type(erreur.value) is faux_airflow
    assert str(erreur.value) == (
        "modele_absent: Aucun modèle en service.; flux_silencieux: Aucune transaction traitée."
    )
    assert [n.sujet for n in canal.recus] == [
        "[SUPERVISION CRITIQUE] modele_absent",
        "[SUPERVISION HAUTE] flux_silencieux",
    ]


# --------------------------------------------------------------------------------------------
# Rappels d'alerte
# --------------------------------------------------------------------------------------------
CAS_ALERTES = {
    "echec_complet": (
        lambda: taches.alerte_echec_tache(
            {
                "task_instance": SimpleNamespace(dag_id="d", task_id="t", try_number=3),
                "exception": ValueError("boum"),
            }
        ),
        "[AIRFLOW] échec de d.t",
        "Tentative n° 3 : boum",
        {"dag": "d", "tache": "t"},
    ),
    "echec_contexte_vide_valeurs_par_defaut": (
        lambda: taches.alerte_echec_tache({}),
        "[AIRFLOW] échec de ?.?",
        "Tentative n° ? : inconnue",
        {"dag": None, "tache": None},
    ),
    "echec_instance_partielle": (
        lambda: taches.alerte_echec_tache({"task_instance": SimpleNamespace(dag_id="d")}),
        "[AIRFLOW] échec de d.?",
        "Tentative n° ? : inconnue",
        {"dag": "d", "tache": None},
    ),
    "echec_cause_de_300_caracteres_gardee_entiere": (
        lambda: taches.alerte_echec_tache({"exception": "x" * 300}),
        "[AIRFLOW] échec de ?.?",
        "Tentative n° ? : " + "x" * 300,
        {"dag": None, "tache": None},
    ),
    "echec_cause_de_301_caracteres_tronquee_a_300": (
        lambda: taches.alerte_echec_tache({"exception": "x" * 299 + "yz"}),
        "[AIRFLOW] échec de ?.?",
        "Tentative n° ? : " + "x" * 299 + "y",
        {"dag": None, "tache": None},
    ),
    "echec_numero_de_carte_masque_avant_la_coupure": (
        # Un numéro à cheval sur la coupure ne doit pas laisser fuir ses premiers chiffres.
        lambda: taches.alerte_echec_tache({"exception": "x" * 289 + " 4929000000000001 fin"}),
        "[AIRFLOW] échec de ?.?",
        "Tentative n° ? : " + ("x" * 289 + " [PAN_MASQUE] fin")[:300],
        {"dag": None, "tache": None},
    ),
    "sla_complet": (
        lambda: taches.alerte_sla(
            SimpleNamespace(dag_id="fraude_rapport_du_matin"),
            "verifier_donnees",
            "calculer_rapport",
            [],
            [],
        ),
        "[SLA AIRFLOW] dépassement sur fraude_rapport_du_matin",
        "Tâches en retard :\nverifier_donnees\nBloquées par :\ncalculer_rapport",
        {"dag": "fraude_rapport_du_matin"},
    ),
    "sla_dag_sans_identifiant": (
        lambda: taches.alerte_sla(object(), "a", "b", None, None),
        "[SLA AIRFLOW] dépassement sur ?",
        "Tâches en retard :\na\nBloquées par :\nb",
        {"dag": None},
    ),
}


@pytest.mark.parametrize(
    ("appeler", "sujet", "corps", "donnees"), list(CAS_ALERTES.values()), ids=list(CAS_ALERTES)
)
def test_les_rappels_d_alerte_airflow_envoient_le_message_exact_avec_des_valeurs_par_defaut(
    config, canal, appeler, sujet, corps, donnees
):
    """Échec de tâche ou SLA : sujet, corps (cause nettoyée puis tronquée) et données exacts."""
    assert appeler() is None
    (notification,) = canal.recus
    assert notification.type == "supervision"
    assert notification.sujet == sujet
    assert notification.corps == corps
    assert notification.donnees == donnees
    assert canal.configurations == [config]


# --------------------------------------------------------------------------------------------
# Précisions : texte exact des requêtes des tâches (relevé par la mutation)
# --------------------------------------------------------------------------------------------
@pytest.fixture
def instructions_des_taches(monkeypatch):
    """Les requêtes de lecture envoyées par les tâches (la création des tables est écartée)."""
    vues: list[str] = []

    class EntrepotTrace(Entrepot):
        def __init__(self, dsn: str) -> None:
            super().__init__(dsn)
            event.listen(
                self.moteur,
                "before_cursor_execute",
                lambda connexion, curseur, instruction, *reste: vues.append(instruction),
            )

    monkeypatch.setattr(taches, "Entrepot", EntrepotTrace)
    return vues


def test_les_taches_du_rapport_lisent_le_rapport_enregistre_avec_les_requetes_documentees(
    config, entrepot, canal, instructions_des_taches
):
    """La notification relit la ligne du jour ; le contrôle du SLA, l'heure de génération."""
    enregistrer_rapport(entrepot, "2026-10-10T06:00:00+00:00")
    taches.notifier_rapport_du_matin(resume={"jour": JOUR})
    taches.controler_sla_rapport(data_interval_end=PLANIFIE, jour=JOUR)
    lectures = [v for v in instructions_des_taches if v.lstrip().startswith("SELECT")]
    assert lectures == [
        "SELECT * FROM or_rapport_quotidien WHERE jour = ?",
        "SELECT genere_le FROM or_rapport_quotidien WHERE jour = ?",
    ]
