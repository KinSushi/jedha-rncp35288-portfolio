# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""FinOps et mode local : chiffres exacts, refus de la démonstration hors SQLite, attentes bornées.

* FinOps : chaque poste, chaque arrondi et chaque prix sont fixés par des valeurs écrites en
  toutes lettres ; une consommation oubliée est refusée au lieu d'être devinée.
* Mode local : la démonstration refuse une file qui n'est pas SQLite, ``reinitialiser`` ne touche
  jamais aux modèles, ``preparer`` ne télécharge ni n'entraîne sans nécessité et l'attente de la
  file ne dépasse jamais son délai. Aucun test n'attend en vrai : l'horloge est simulée.
"""

from __future__ import annotations

import argparse
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from detection_fraude import finops
from detection_fraude import local as module_local
from detection_fraude.entrepot import Entrepot
from detection_fraude.file_attente import FileSQLite
from detection_fraude.finops import PRIX_HYPOTHETIQUES, estimer
from detection_fraude.local import (
    _attendre_file_vide,
    _titre,
    demo,
    etat,
    preparer,
    reinitialiser,
)
from detection_fraude.modele import NOM_POINTEUR
from detection_fraude.producteur import BilanProduction, PannesSimulees
from detection_fraude.rapport import DonneesAbsentes
from tests.test_rapport import ligne_argent
from tests.test_rapport_garanties import inserer, instantane

# --------------------------------------------------------------------------------------------
# FinOps
# --------------------------------------------------------------------------------------------
CONSOMMATIONS = {
    "tx_par_jour": 6337657.0,
    "octets_message_file": 780.0,
    "octets_bronze_par_tx": 326.0,
    "octets_argent_par_tx": 394.0,
    "octets_reponse_api": 856.0,
    "appels_api_par_jour": 1835.0,
    "retention_file_jours": 11.0,
    "retention_entrepot_jours": 61.0,
    "vcpu_services_permanents": 1.144,
    "duree_entrainement_s": 116.2,
}
PRIX_AUTRES = {"vcpu_heure_eur": 0.0173, "go_stockage_mois_eur": 0.2371, "go_sortie_eur": 0.0919}
UNITE_NOTIFICATIONS = "Go/mois (hypothèse : 0,4 % d'alertes de 1,5 Ko)"


def poste(consommation: float, unite: str, cout: float) -> dict[str, Any]:
    return {"consommation": consommation, "unite": unite, "cout_eur": cout}


def postes_attendus(couts: tuple[float, float, float, float, float]) -> dict[str, Any]:
    """Les six postes de ``CONSOMMATIONS`` ; seuls les coûts changent avec les prix."""
    permanent, entrainement, file, entrepot, notifications = couts
    return {
        "calcul_services_permanents": poste(835.1, "vCPU-heure/mois", permanent),
        "calcul_entrainement_hebdomadaire": poste(0.14, "vCPU-heure/mois", entrainement),
        "stockage_file": poste(50.6426, "Go (en continu)", file),
        "stockage_entrepot": poste(259.2335, "Go (en continu)", entrepot),
        "transfert_api_entrant": poste(0.04389, "Go/mois", 0.0),
        "transfert_notifications_sortant": poste(1.06243, UNITE_NOTIFICATIONS, notifications),
    }


def test_estimer_ne_modifie_pas_les_prix_par_defaut_et_donne_chaque_poste_exact():
    """Six postes, chaque arrondi, prix par défaut ; les prix hypothétiques restent intacts."""
    prix_avant = dict(PRIX_HYPOTHETIQUES)
    resultat = estimer(**CONSOMMATIONS)
    assert prix_avant == PRIX_HYPOTHETIQUES
    assert resultat == {
        "hypotheses_de_prix": {
            "vcpu_heure_eur": 0.03,
            "go_stockage_mois_eur": 0.10,
            "go_sortie_eur": 0.08,
        },
        "postes": postes_attendus((25.05, 0.0042, 5.0643, 25.9234, 0.08499)),
        "total_mensuel_eur": 56.13,
        "avertissement": (
            "Prix unitaires hypothétiques : à remplacer par la grille du fournisseur."
        ),
    }
    assert list(resultat["postes"]) == [
        "calcul_services_permanents",
        "calcul_entrainement_hebdomadaire",
        "stockage_file",
        "stockage_entrepot",
        "transfert_api_entrant",
        "transfert_notifications_sortant",
    ]
    resultat["hypotheses_de_prix"]["vcpu_heure_eur"] = 99.0  # le résultat est une copie
    assert prix_avant == PRIX_HYPOTHETIQUES
    assert estimer(**CONSOMMATIONS, prix=None) == estimer(**CONSOMMATIONS, prix={})
    # Un entraînement par semaine = 4,33 par mois par défaut ; le paramètre est pris en compte.
    surcharge = estimer(**CONSOMMATIONS, entrainements_par_mois=10.0)
    assert surcharge["postes"]["calcul_entrainement_hebdomadaire"] == poste(
        0.323, "vCPU-heure/mois", 0.0097
    )
    assert surcharge["total_mensuel_eur"] == 56.13


def test_estimer_applique_chaque_prix_au_bon_poste_sans_toucher_aux_prix_par_defaut():
    """Les trois prix sont distincts : un prix appliqué au mauvais poste change le total."""
    prix_avant = dict(PRIX_HYPOTHETIQUES)
    resultat = estimer(**CONSOMMATIONS, prix=PRIX_AUTRES)
    assert prix_avant == PRIX_HYPOTHETIQUES
    assert resultat["hypotheses_de_prix"] == PRIX_AUTRES
    assert resultat["postes"] == postes_attendus((14.45, 0.0024, 12.0074, 61.4643, 0.09764))
    assert resultat["total_mensuel_eur"] == 88.02
    partiel = estimer(**CONSOMMATIONS, prix={"vcpu_heure_eur": 0.0173})
    assert partiel["hypotheses_de_prix"] == {
        "vcpu_heure_eur": 0.0173,
        "go_stockage_mois_eur": 0.10,
        "go_sortie_eur": 0.08,
    }
    assert partiel["postes"]["calcul_services_permanents"]["cout_eur"] == 14.45
    assert partiel["postes"]["stockage_file"]["cout_eur"] == 5.0643
    assert partiel["total_mensuel_eur"] == 45.53


@pytest.mark.parametrize("manquant", list(CONSOMMATIONS))
def test_une_consommation_oubliee_est_refusee_au_lieu_d_etre_devinee(manquant):
    """Aucune consommation n'a de valeur par défaut : il faut la mesurer ou la supposer."""
    incomplet = {cle: valeur for cle, valeur in CONSOMMATIONS.items() if cle != manquant}
    with pytest.raises(TypeError) as erreur:
        estimer(**incomplet)
    # Le début du message de Python cite le nom de la fonction, que l'instrumentation de mutmut
    # modifie : seul ce qui est propre au contrat (l'argument réclamé) est comparé.
    assert str(erreur.value).endswith(f"missing 1 required keyword-only argument: '{manquant}'")


def test_estimer_refuse_les_arguments_positionnels():
    """Tout est nommé : impossible d'intervertir deux consommations par mégarde."""
    with pytest.raises(TypeError) as erreur:
        estimer(1440.0)
    assert "takes 0 positional arguments but 1 was given" in str(erreur.value)


OPTIONS_FINOPS = {
    "--tx-par-jour": ("tx_par_jour", 1440.0),
    "--octets-message": ("octets_message_file", 431.0),
    "--octets-bronze": ("octets_bronze_par_tx", 364.0),
    "--octets-argent": ("octets_argent_par_tx", 359.0),
    "--octets-reponse-api": ("octets_reponse_api", 587.0),
    "--appels-api-par-jour": ("appels_api_par_jour", 1440.0),
    "--retention-file-jours": ("retention_file_jours", 7.0),
    "--retention-entrepot-jours": ("retention_entrepot_jours", 90.0),
    "--vcpu": ("vcpu_services_permanents", 7.25),
    "--duree-entrainement-s": ("duree_entrainement_s", 37.4),
}


@pytest.fixture
def estimation_enregistree(monkeypatch):
    """Remplace ``estimer`` et ``configurer_journal`` pour lire ce que ``main`` leur transmet."""
    appels: list[dict[str, Any]] = []
    journal: list[tuple[Any, ...]] = []

    def faux_estimer(**kwargs):
        appels.append(kwargs)
        return {"cout": "é"}

    monkeypatch.setattr(finops, "estimer", faux_estimer)
    monkeypatch.setattr(finops, "configurer_journal", lambda *args: journal.append(args))
    return SimpleNamespace(appels=appels, journal=journal)


def test_main_finops_transmet_les_valeurs_par_defaut_puis_chaque_option_au_bon_parametre(
    estimation_enregistree, capsys
):
    """Défauts documentés (1 tx/min, 7,25 vCPU...) ; dix options distinctes ne se mélangent pas."""
    code_sortie = finops.main([])
    assert code_sortie == 0
    assert estimation_enregistree.appels == [dict(OPTIONS_FINOPS.values())]
    assert estimation_enregistree.journal == [("WARNING",)]
    assert capsys.readouterr().out == '{\n  "cout": "é"\n}\n'
    estimation_enregistree.appels.clear()
    argv = []
    attendu = {}
    for rang, (option, (parametre, _)) in enumerate(OPTIONS_FINOPS.items(), start=1):
        argv += [option, str(rang + 0.5)]
        attendu[parametre] = rang + 0.5
    assert finops.main(argv) == 0
    assert estimation_enregistree.appels == [attendu]
    capsys.readouterr()
    estimation_enregistree.appels.clear()
    with pytest.raises(SystemExit):
        finops.main(["--vcpu", "beaucoup"])  # une valeur illisible n'estime plus rien
    assert estimation_enregistree.appels == []
    capsys.readouterr()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--tx-par-jour", "beaucoup"], "argument --tx-par-jour: invalid float value: 'beaucoup'"),
        (["--vcpu"], "argument --vcpu: expected one argument"),
        (["--inconnue", "1"], "unrecognized arguments: --inconnue 1"),
    ],
)
def test_main_finops_refuse_une_option_invalide_avec_le_code_2_et_n_estime_rien(
    estimation_enregistree, capsys, argv, message
):
    """Une erreur de saisie s'arrête à l'analyse : ni journal réglé, ni estimation, ni sortie."""
    with pytest.raises(SystemExit) as sortie:
        finops.main(argv)
    assert sortie.value.code == 2
    lecture = capsys.readouterr()
    assert f"error: {message}" in lecture.err
    assert lecture.out == ""
    assert estimation_enregistree.appels == []
    assert estimation_enregistree.journal == []


def test_main_finops_aide_decrit_la_commande_et_les_options_puis_quitte_avec_0(
    capsys, monkeypatch
):
    """``--help`` documente chaque option ; les trois textes d'aide existants sont fixés."""
    monkeypatch.setenv("COLUMNS", "250")
    with pytest.raises(SystemExit) as sortie:
        finops.main(["--help"])
    assert sortie.value.code == 0
    aide = capsys.readouterr().out
    assert "Estimation FinOps mensuelle." in aide
    for option in OPTIONS_FINOPS:
        assert option in aide
    assert "1440 = une transaction par minute (API en direct)" in aide
    assert "vCPU réservés aux services permanents (voir docker-compose.yml)" in aide


# --------------------------------------------------------------------------------------------
# Outils du mode local
# --------------------------------------------------------------------------------------------
class FauxTemps:
    """Horloge simulée : ``sleep`` fait avancer ``monotonic`` sans attendre, avec garde-fou."""

    def __init__(self, pas: float = 0.0, plafond_de_pauses: int = 2500) -> None:
        self.maintenant = 1000.0
        self.pas = pas
        self.pauses: list[float] = []
        self.plafond = plafond_de_pauses
        self.apres_pause: Any = None

    def monotonic(self) -> float:
        valeur = self.maintenant
        self.maintenant += self.pas
        return valeur

    def sleep(self, secondes: float) -> None:
        self.pauses.append(secondes)
        if len(self.pauses) > self.plafond:
            raise AssertionError("l'attente de la file ne s'arrête plus")
        self.maintenant += secondes
        if self.apres_pause:
            self.apres_pause(len(self.pauses))


class HorlogeFixe(datetime):
    """Remplace la classe ``datetime`` du module : ``now`` rend toujours 10/10/2026 07:20 UTC."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is None:  # ni now() ni now(None) : l'heure locale de la machine est refusée
            raise AssertionError("l'heure se lit en UTC : now(UTC)")
        return datetime(2026, 10, 10, 7, 20, tzinfo=UTC).astimezone(tz)


@pytest.fixture
def temps_simule(monkeypatch):
    faux = FauxTemps()
    monkeypatch.setattr(module_local, "time", faux)
    return faux


@pytest.fixture
def files_fermees(monkeypatch):
    """Compte les fermetures des ``FileSQLite`` ouvertes par le module ``local``."""
    fermees: list[str] = []

    class FileEspion(FileSQLite):
        def fermer(self) -> None:
            fermees.append("fermee")
            super().fermer()

    monkeypatch.setattr(module_local, "FileSQLite", FileEspion)
    return fermees


@pytest.fixture
def journal_enregistre(monkeypatch):
    appels: list[tuple[Any, ...]] = []
    monkeypatch.setattr(module_local, "configurer_journal", lambda *args: appels.append(args))
    return appels


def publier_et_lire(config, sujet: str) -> list[Any]:
    """Publie un message sur ``sujet`` et le relit sans le valider."""
    file = FileSQLite(config.file.chemin_sqlite)
    try:
        file.publier(sujet, {"n": 1})
        return file.lire(sujet, "lecteur-de-test", attente_s=0.0)
    finally:
        file.fermer()


def valider(config, sujet: str, groupe: str, messages: list[Any]) -> None:
    file = FileSQLite(config.file.chemin_sqlite)
    try:
        file.valider(sujet, groupe, messages)
    finally:
        file.fermer()


# --------------------------------------------------------------------------------------------
# _attendre_file_vide
# --------------------------------------------------------------------------------------------
def test_attendre_file_vide_leve_timeout_au_delai_et_rend_la_main_une_fois_la_file_vide(
    config, temps_simule, files_fermees
):
    """Un message non traité fait échouer l'attente au délai (message exact) ; traité, fini."""
    messages = publier_et_lire(config, config.file.sujet_valides)
    with pytest.raises(TimeoutError) as erreur:
        _attendre_file_vide(config, [], delai_s=0.5)
    assert str(erreur.value) == "Le consommateur n'a pas vidé la file dans le délai imparti."
    assert temps_simule.pauses == [0.3, 0.3]  # 0,3 s de pause, jusqu'à 0,5 s écoulées
    assert files_fermees == ["fermee"]
    valider(config, config.file.sujet_valides, config.file.groupe_consommateur, messages)
    temps_simule.pauses.clear()
    assert _attendre_file_vide(config, [], delai_s=0.5) is None
    assert temps_simule.pauses == []
    assert files_fermees == ["fermee", "fermee"]
    # Si le traitement finit pendant l'attente, la pause suivante est la dernière.
    messages = publier_et_lire(config, config.file.sujet_valides)
    temps_simule.apres_pause = lambda rang: valider(
        config, config.file.sujet_valides, config.file.groupe_consommateur, messages
    )
    assert _attendre_file_vide(config, [], delai_s=0.5) is None
    assert temps_simule.pauses == [0.3]


def test_un_message_en_attente_dans_la_file_des_rejets_empeche_aussi_de_conclure(
    config, temps_simule, files_fermees
):
    """Le retard est la somme des deux sujets : les rejets non archivés comptent."""
    messages = publier_et_lire(config, config.file.sujet_rejets)
    with pytest.raises(TimeoutError):
        _attendre_file_vide(config, [], delai_s=0.5)
    assert temps_simule.pauses == [0.3, 0.3]
    valider(config, config.file.sujet_rejets, config.file.groupe_rejets, messages)
    assert _attendre_file_vide(config, [], delai_s=0.5) is None


def test_un_message_valide_par_un_autre_groupe_ne_vide_pas_la_file(
    config, temps_simule, files_fermees
):
    """Seuls les groupes du consommateur et de l'archiveur comptent, pas un autre lecteur."""
    messages_valides = publier_et_lire(config, config.file.sujet_valides)
    messages_rejets = publier_et_lire(config, config.file.sujet_rejets)
    valider(config, config.file.sujet_valides, config.file.groupe_rejets, messages_valides)
    valider(config, config.file.sujet_rejets, config.file.groupe_consommateur, messages_rejets)
    with pytest.raises(TimeoutError):
        _attendre_file_vide(config, [], delai_s=0.5)
    assert files_fermees == ["fermee"]


def test_une_erreur_du_consommateur_est_relayee_aussitot_sans_attendre_le_delai(
    config, temps_simule, files_fermees
):
    """Le fil consommateur est tombé : son exception remonte telle quelle, file fermée."""
    publier_et_lire(config, config.file.sujet_valides)
    panne = ValueError("consommateur tombé (test)")
    with pytest.raises(ValueError) as erreur:
        _attendre_file_vide(config, [panne, RuntimeError("seconde erreur")], delai_s=0.5)
    assert erreur.value is panne
    assert temps_simule.pauses == []
    assert files_fermees == ["fermee"]


def test_une_erreur_prime_sur_une_file_deja_vide(config, temps_simule, files_fermees):
    """Même file vide, une panne signalée ne doit pas être passée sous silence."""
    panne = RuntimeError("panne tardive (test)")
    with pytest.raises(RuntimeError) as erreur:
        _attendre_file_vide(config, [panne], delai_s=0.5)
    assert erreur.value is panne
    assert files_fermees == ["fermee"]


def test_une_erreur_survenue_pendant_l_attente_est_relevee_a_la_pause_suivante(
    config, temps_simule, files_fermees
):
    """Le fil tombe pendant la 1re pause : l'erreur sort à la vérification suivante."""
    publier_et_lire(config, config.file.sujet_valides)
    erreurs: list[BaseException] = []
    temps_simule.apres_pause = lambda rang: erreurs.append(KeyError("tombé pendant l'attente"))
    with pytest.raises(KeyError) as erreur:
        _attendre_file_vide(config, erreurs, delai_s=0.5)
    assert erreur.value.args == ("tombé pendant l'attente",)
    assert temps_simule.pauses == [0.3]


class FileSansRetard:
    """File dont le retard est inconnu (None), comme Kafka : traité comme zéro."""

    def __init__(self, *_: Any) -> None:
        self.fermee = 0

    def retard(self, sujet: str, groupe: str) -> None:
        return None

    def fermer(self) -> None:
        self.fermee += 1


class FileToujoursEnRetard(FileSansRetard):
    def retard(self, sujet: str, groupe: str) -> int:
        return 1


def test_un_retard_inconnu_vaut_zero_et_le_delai_par_defaut_est_de_600_secondes(
    config, monkeypatch
):
    """Retard ``None`` : file considérée vide ; sinon l'attente par défaut s'arrête après 600 s."""
    inconnue = FileSansRetard()
    monkeypatch.setattr(module_local, "FileSQLite", lambda chemin: inconnue)
    faux = FauxTemps()
    monkeypatch.setattr(module_local, "time", faux)
    assert _attendre_file_vide(config, []) is None
    assert faux.pauses == [] and inconnue.fermee == 1

    bloquee = FileToujoursEnRetard()
    monkeypatch.setattr(module_local, "FileSQLite", lambda chemin: bloquee)
    with pytest.raises(TimeoutError):
        _attendre_file_vide(config, [])
    assert set(faux.pauses) == {0.3}
    assert 600.0 <= faux.maintenant - 1000.0 < 600.3 + 1e-6
    assert bloquee.fermee == 1


# --------------------------------------------------------------------------------------------
# demo : refus hors SQLite
# --------------------------------------------------------------------------------------------
def test_la_demo_refuse_une_file_non_sqlite_avec_le_code_2_et_ne_lance_rien(
    config, evenements, monkeypatch, capsys
):
    """Kafka en production : la démo (qui vide et rejoue) refuse net, sans rien préparer."""

    def interdit(*args, **kwargs):
        raise AssertionError("la démonstration ne devait rien lancer")

    for nom in ("preparer", "reinitialiser", "construire_consommateur", "rejouer_jeu", "etat"):
        monkeypatch.setattr(module_local, nom, interdit)
    config_kafka = replace(config, file=replace(config.file, type="kafka"))
    arguments = argparse.Namespace(reinitialiser=True)
    arguments_avant = dict(vars(arguments))
    code_sortie = demo(config_kafka, arguments)
    assert code_sortie == 2
    assert evenements.tous() == [
        (
            "ERROR",
            "demo_locale_uniquement",
            {"cause": "la démonstration suppose la file SQLite ; utiliser FRAUDE_ENV=dev"},
        )
    ]
    assert vars(arguments) == arguments_avant
    assert capsys.readouterr().out == ""
    assert not config.racine_donnees.exists()


# --------------------------------------------------------------------------------------------
# preparer
# --------------------------------------------------------------------------------------------
@pytest.fixture
def preparation(monkeypatch):
    """Remplace téléchargement et entraînement par des enregistreurs partageant un même journal."""
    ordre: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        module_local,
        "telecharger_jeu",
        lambda *args, **kwargs: ordre.append(("jeu", args, kwargs)),
    )
    monkeypatch.setattr(
        module_local, "entrainer", lambda *args, **kwargs: ordre.append(("modele", args, kwargs))
    )
    return ordre


def mettre_en_service(config) -> None:
    config.modele.dossier.mkdir(parents=True, exist_ok=True)
    (config.modele.dossier / NOM_POINTEUR).write_text("v1\n", encoding="utf-8")


def test_preparer_est_sans_effet_quand_le_jeu_et_le_modele_sont_deja_la(
    config, preparation, capsys
):
    """Tout est prêt : ni téléchargement de 150 Mo, ni entraînement, ni message."""
    config.source.jeu_chemin.parent.mkdir(parents=True, exist_ok=True)
    config.source.jeu_chemin.write_text("a;b\n", encoding="utf-8")
    mettre_en_service(config)
    preparer(config)
    assert preparation == []
    assert capsys.readouterr().out == ""
    assert config.source.jeu_chemin.read_text(encoding="utf-8") == "a;b\n"


def test_preparer_ne_telecharge_que_si_le_jeu_manque_et_n_entraine_que_sans_modele(
    config, preparation, capsys, monkeypatch
):
    """Quatre combinaisons jeu/modèle, puis un téléchargement refusé qui bloque l'entraînement."""
    titre_jeu = "\n=== Téléchargement du jeu fraudTest.csv (150 Mo, vérifié par SHA-256) ===\n"
    titre_modele = "\n=== Entraînement du premier modèle (suivi MLflow) ===\n"
    preparer(config)  # ni jeu ni modèle
    assert preparation == [
        ("jeu", (config,), {}),
        ("modele", (config,), {"promouvoir_si_premier": True}),
    ]
    assert capsys.readouterr().out == titre_jeu + titre_modele
    preparation.clear()

    mettre_en_service(config)  # modèle présent, jeu absent
    preparer(config)
    assert preparation == [("jeu", (config,), {})]
    assert capsys.readouterr().out == titre_jeu
    preparation.clear()

    (config.modele.dossier / NOM_POINTEUR).unlink()  # jeu présent, modèle absent
    config.source.jeu_chemin.write_text("a;b\n", encoding="utf-8")
    preparer(config)
    assert preparation == [("modele", (config,), {"promouvoir_si_premier": True})]
    assert capsys.readouterr().out == titre_modele
    preparation.clear()

    config.source.jeu_chemin.unlink()  # jeu absent, modèle absent, téléchargement refusé

    def refuse(*args, **kwargs):
        raise RuntimeError("Téléchargement refusé : taille 3 au lieu de 4")

    monkeypatch.setattr(module_local, "telecharger_jeu", refuse)
    with pytest.raises(RuntimeError) as erreur:
        preparer(config)
    assert str(erreur.value) == "Téléchargement refusé : taille 3 au lieu de 4"
    assert preparation == []  # l'entraînement n'a pas été lancé sur un jeu absent
    assert capsys.readouterr().out == titre_jeu


# --------------------------------------------------------------------------------------------
# reinitialiser
# --------------------------------------------------------------------------------------------
def test_reinitialiser_supprime_les_fichiers_generes_et_laisse_intacts_modeles_jeu_et_journaux(
    config,
):
    """Seuls la file, l'entrepôt, les alertes, les états et les rapports générés disparaissent."""
    racine = config.racine_donnees
    a_supprimer = []
    for cible in (
        config.file.chemin_sqlite,
        config.notification.fichier_alertes,
        racine / "entrepot.db",
    ):
        for suffixe in ("", "-wal", "-shm"):
            a_supprimer.append(Path(str(cible) + suffixe))
    a_supprimer += [racine / "etat" / "producteur.json", racine / "etat" / "consommateur.json"]
    a_supprimer += [config.rapport.dossier / n for n in ("rapport_x.html", "fraudes_x.csv")]
    a_conserver = [
        config.source.jeu_chemin,
        config.modele.dossier / NOM_POINTEUR,
        config.modele.dossier / "v1" / "modele.pkl.gz",
        config.journal.fichier,
        racine / ".sel_hachage_dev",
        racine / "etat" / "autre.json",
        racine / "file_messages.db.sauvegarde",
        config.rapport.dossier / "note.txt",
    ]
    for chemin in [*a_supprimer, *a_conserver]:
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(f"contenu de {chemin.name}", encoding="utf-8")
    protege_avant = {chemin: chemin.read_bytes() for chemin in a_conserver}
    reinitialiser(config)
    assert [chemin for chemin in a_supprimer if chemin.exists()] == []
    assert {chemin: chemin.read_bytes() for chemin in a_conserver} == protege_avant
    reinitialiser(config)  # relançable : rien à supprimer n'est une situation normale
    assert {chemin: chemin.read_bytes() for chemin in a_conserver} == protege_avant


def test_reinitialiser_est_sans_effet_sur_un_dossier_vierge(config):
    """Aucune donnée générée : la réinitialisation ne crée ni dossier ni fichier."""
    reinitialiser(config)
    assert not config.racine_donnees.exists()


# --------------------------------------------------------------------------------------------
# etat
# --------------------------------------------------------------------------------------------
def test_etat_compte_chaque_couche_dans_l_ordre_et_ne_modifie_pas_la_base(
    config, entrepot, monkeypatch
):
    """Six compteurs exacts, dans l'ordre d'affichage, lus sans rien écrire, connexion rendue."""
    fermetures: list[str] = []

    class EntrepotEspion(Entrepot):
        def fermer(self) -> None:
            fermetures.append(self.dialecte)
            super().fermer()

    monkeypatch.setattr(module_local, "Entrepot", EntrepotEspion)
    with entrepot.transaction() as conn:
        for cle in ("1", "2", "3", "4"):
            entrepot.enregistrer_rejet(conn, {"etape": "p", "raison": "r"}, cle, "s", "2026-10-09")
    inserer(entrepot, [ligne_argent(1), ligne_argent(2), ligne_argent(3)])
    base_avant = instantane(entrepot)
    comptes = etat(config)
    assert comptes == {
        "bronze_transactions": 0,
        "argent_transactions": 3,
        "or_alertes_fraude": 0,
        "or_rapport_quotidien": 0,
        "meta_rejets": 4,
        "meta_lignage": 0,
    }
    assert list(comptes) == [
        "bronze_transactions",
        "argent_transactions",
        "or_alertes_fraude",
        "or_rapport_quotidien",
        "meta_rejets",
        "meta_lignage",
    ]
    assert instantane(entrepot) == base_avant
    assert fermetures == ["sqlite"]


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
DEFAUTS_DEMO = {
    "commande": "demo",
    "source": "jeu",
    "jour": "2020-12-21",
    "nombre": None,
    "appels": 3,
    "intervalle": 13.0,
    "cadence": 0.0,
    "corruptions": 6,
    "pannes": 2,
    "rebaser_sur": None,
    "graine": 7,
    "reinitialiser": False,
}


@pytest.fixture
def commandes_enregistrees(monkeypatch):
    """Remplace ``demo``, ``preparer`` et ``etat`` : garde leurs arguments, rend des valeurs."""
    appels = SimpleNamespace(demo=[], preparer=[], etat=[], code_demo=3)

    def faux_demo(configuration, arguments):
        appels.demo.append((configuration, vars(arguments)))
        return appels.code_demo

    monkeypatch.setattr(module_local, "demo", faux_demo)
    monkeypatch.setattr(
        module_local, "preparer", lambda configuration: appels.preparer.append(configuration)
    )

    def faux_etat(configuration):
        appels.etat.append(configuration)
        return {"bronze_transactions": 3, "argent_transactions": 12345}

    monkeypatch.setattr(module_local, "etat", faux_etat)
    return appels


def test_main_local_renvoie_le_code_de_la_demo_et_dispatche_preparer_et_etat(
    config, commandes_enregistrees, journal_enregistre, capsys
):
    """``demo`` rend son code tel quel (3 ici) ; ``preparer`` et ``etat`` rendent 0."""
    code_sortie = module_local.main(["demo"])
    assert code_sortie == 3
    assert commandes_enregistrees.demo == [(config, DEFAUTS_DEMO)]
    assert commandes_enregistrees.preparer == [] and commandes_enregistrees.etat == []
    assert capsys.readouterr().out == ""

    assert module_local.main(["preparer"]) == 0
    assert commandes_enregistrees.preparer == [config]
    assert len(commandes_enregistrees.demo) == 1 and commandes_enregistrees.etat == []

    assert module_local.main(["etat"]) == 0
    assert commandes_enregistrees.etat == [config]
    assert capsys.readouterr().out == (
        "bronze_transactions" + " " * 6 + "3\n" + "argent_transactions" + " " * 6 + "12345\n"
    )
    assert journal_enregistre == [("INFO", "lisible", config.journal.fichier)] * 3


def test_main_local_transmet_chaque_option_de_la_demo_et_son_code_d_erreur(
    config, commandes_enregistrees, journal_enregistre
):
    """Onze options, onze valeurs distinctes ; le code 2 de la démo remonte jusqu'au shell."""
    commandes_enregistrees.code_demo = 2
    code_sortie = module_local.main(
        [
            "demo",
            "--source",
            "api",
            "--jour",
            "2021-01-05",
            "--nombre",
            "50",
            "--appels",
            "9",
            "--intervalle",
            "0.5",
            "--cadence",
            "12.5",
            "--corruptions",
            "1",
            "--pannes",
            "0",
            "--rebaser-sur",
            "2026-10-09",
            "--graine",
            "99",
            "--reinitialiser",
        ]
    )
    assert code_sortie == 2
    assert commandes_enregistrees.demo == [
        (
            config,
            {
                "commande": "demo",
                "source": "api",
                "jour": "2021-01-05",
                "nombre": 50,
                "appels": 9,
                "intervalle": 0.5,
                "cadence": 12.5,
                "corruptions": 1,
                "pannes": 0,
                "rebaser_sur": "2026-10-09",
                "graine": 99,
                "reinitialiser": True,
            },
        )
    ]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "the following arguments are required: commande"),
        (["inconnue"], "argument commande: invalid choice: 'inconnue' (choose from"),
        (["demo", "--source", "ftp"], "argument --source: invalid choice: 'ftp' (choose from"),
        (["demo", "--nombre", "beaucoup"], "argument --nombre: invalid int value: 'beaucoup'"),
        (["demo", "--cadence", "vite"], "argument --cadence: invalid float value: 'vite'"),
        (["etat", "--jour", "2020-12-21"], "unrecognized arguments: --jour 2020-12-21"),
    ],
)
def test_main_local_refuse_une_commande_ou_une_option_invalide_avec_le_code_2_sans_rien_lancer(
    config, commandes_enregistrees, journal_enregistre, capsys, argv, message
):
    """Une faute de frappe ne lance ni démonstration, ni préparation, ni journal."""
    with pytest.raises(SystemExit) as sortie:
        module_local.main(argv)
    assert sortie.value.code == 2
    assert f"error: {message}" in capsys.readouterr().err
    assert commandes_enregistrees.demo == [] and commandes_enregistrees.preparer == []
    assert commandes_enregistrees.etat == [] and journal_enregistre == []
    assert not config.racine_donnees.exists()


def test_main_local_aide_decrit_les_commandes_et_chaque_option_de_la_demo(capsys, monkeypatch):
    """Les textes d'aide de la ligne de commande sont fixés, commande par commande."""
    monkeypatch.setenv("COLUMNS", "250")
    with pytest.raises(SystemExit) as sortie:
        module_local.main(["--help"])
    assert sortie.value.code == 0
    aide = capsys.readouterr().out
    assert "Mode local sans Docker." in aide
    assert "rejoue le flux de bout en bout" in aide
    assert "compte les lignes de chaque couche" in aide
    assert "télécharge le jeu et entraîne le modèle si besoin" in aide
    with pytest.raises(SystemExit) as sortie_demo:
        module_local.main(["demo", "--help"])
    assert sortie_demo.value.code == 0
    aide_demo = capsys.readouterr().out
    for texte in (
        "--source {jeu,api}",
        "--jour JOUR",
        "jeu : jour à rejouer (AAAA-MM-JJ)",
        "jeu : nombre de lignes ; sinon toute la journée",
        "api : nombre d'appels",
        "api : secondes entre deux appels",
        "jeu : lignes par seconde (0 = max)",
        "lignes corrompues injectées (démo)",
        "api : pannes réseau simulées (démo)",
        "jeu : date (AAAA-MM-JJ) sur laquelle recaler le jour rejoué",
        "--graine GRAINE",
        "repart d'une file et d'un entrepôt vides",
    ):
        assert texte in aide_demo


# --------------------------------------------------------------------------------------------
# demo avec toutes ses dépendances remplacées (le vrai flux est couvert par test_bout_en_bout)
# --------------------------------------------------------------------------------------------
@pytest.fixture
def banc_demo(config, monkeypatch):
    """Doublures pour tout ce que ``demo`` appelle : on vérifie l'enchaînement, pas les couches."""
    appels: list[tuple[Any, ...]] = []
    banc = SimpleNamespace(
        appels=appels,
        bilan=BilanProduction(lues=5, publiees=4, rejetees=1, doublons=0),
        rapport={
            "nb_paiements": 10,
            "nb_fraudes": 2,
            "nb_rejets": 1,
            "chemins": {"html": "h.html", "csv": "c.csv"},
        },
        constats=[],
        erreur_apres_attente=None,
        controle_kwargs=[],
    )
    mettre_en_service_v_test(config)
    temps = FauxTemps(pas=1.5)
    monkeypatch.setattr(module_local, "time", temps)
    monkeypatch.setattr(module_local, "datetime", HorlogeFixe)
    monkeypatch.setattr(module_local, "preparer", lambda c: appels.append(("preparer", c)))
    monkeypatch.setattr(
        module_local, "reinitialiser", lambda c: appels.append(("reinitialiser", c))
    )

    class FauxConsommateur:
        def boucle(self, arreter):
            fil = threading.current_thread()
            appels.append(("boucle", arreter, fil.name, fil.daemon))

    monkeypatch.setattr(
        module_local,
        "construire_consommateur",
        lambda c: (FauxConsommateur(), lambda: appels.append(("fermer_consommateur",))),
    )

    class FauxProducteur:
        def __init__(self, c, file, hacheur, *, source):
            appels.append(("producteur", c, type(file).__name__, type(hacheur).__name__, source))

    monkeypatch.setattr(module_local, "Producteur", FauxProducteur)

    def faux_rejouer(c, producteur, **kwargs):
        appels.append(("rejouer_jeu", kwargs))
        return banc.bilan

    def faux_boucle_api(c, producteur, arreter, **kwargs):
        appels.append(("boucle_api", arreter, kwargs))
        return banc.bilan

    monkeypatch.setattr(module_local, "rejouer_jeu", faux_rejouer)
    monkeypatch.setattr(module_local, "boucle_api", faux_boucle_api)
    reponse = object()
    banc.reponse_http = reponse
    monkeypatch.setattr(module_local, "requests", SimpleNamespace(get=lambda *a, **k: reponse))

    def faux_attendre(c, erreurs, delai_s=600.0):
        appels.append(("attendre", delai_s))
        if banc.erreur_apres_attente:
            erreurs.append(banc.erreur_apres_attente)

    monkeypatch.setattr(module_local, "_attendre_file_vide", faux_attendre)

    def faux_produire(c, entrepot, jour, notificateur):
        appels.append(("rapport", jour, type(notificateur).__name__))
        if isinstance(banc.rapport, Exception):
            raise banc.rapport
        return banc.rapport

    monkeypatch.setattr(module_local, "produire_rapport", faux_produire)

    def faux_controler(entrepot, c, **kwargs):
        banc.controle_kwargs.append(kwargs)
        return banc.constats

    monkeypatch.setattr(module_local, "controler", faux_controler)
    monkeypatch.setattr(
        module_local, "etat", lambda c: {"bronze_transactions": 3, "argent_transactions": 4}
    )
    return banc


def mettre_en_service_v_test(config) -> None:
    config.modele.dossier.mkdir(parents=True, exist_ok=True)
    (config.modele.dossier / NOM_POINTEUR).write_text("v-test\n", encoding="utf-8")


def arguments_demo(**surcharges: Any) -> argparse.Namespace:
    base = {
        "source": "jeu",
        "jour": "2020-12-21",
        "nombre": 40,
        "appels": 4,
        "intervalle": 0.25,
        "cadence": 2.5,
        "corruptions": 3,
        "pannes": 0,
        "graine": 5,
        "reinitialiser": False,
    }
    return argparse.Namespace(**{**base, **surcharges})


def test_la_demo_jeu_enchaine_les_etapes_et_imprime_chaque_ligne_du_bilan(
    config, banc_demo, capsys, evenements
):
    """Source « jeu » : ordre des étapes, arguments transmis, texte exact, événement final."""
    arguments = arguments_demo(reinitialiser=True)
    arguments_avant = dict(vars(arguments))
    code_sortie = demo(config, arguments)
    assert code_sortie == 0
    assert vars(arguments) == arguments_avant  # la démonstration ne modifie pas ses arguments
    etapes = ("preparer", "reinitialiser", "producteur", "rejouer_jeu", "attendre", "rapport")
    assert [a[0] for a in banc_demo.appels if a[0] in etapes] == list(etapes)
    assert ("preparer", config) in banc_demo.appels
    assert ("reinitialiser", config) in banc_demo.appels
    producteur = next(a for a in banc_demo.appels if a[0] == "producteur")
    assert producteur == ("producteur", config, "FileSQLite", "HacheurCarte", "jeu")
    rejeu = next(a for a in banc_demo.appels if a[0] == "rejouer_jeu")[1]
    arreter = next(a for a in banc_demo.appels if a[0] == "boucle")[1]
    assert rejeu == {
        "jour": "2020-12-21",
        "limite": 40,
        "corruptions": 3,
        "graine": 5,
        "cadence": 2.5,
        "arreter": arreter,
        "rebaser_sur": None,
    }
    assert arreter.is_set()
    boucle = next(a for a in banc_demo.appels if a[0] == "boucle")
    assert boucle[2:] == ("consommateur", True)  # fil nommé, en arrière-plan (daemon)
    assert ("fermer_consommateur",) in banc_demo.appels
    assert ("attendre", 600.0) in banc_demo.appels
    assert ("rapport", "2020-12-21", "Notificateur") in banc_demo.appels
    assert banc_demo.controle_kwargs == [{"surveiller_flux": False}]
    assert evenements.nommes("demo_terminee") == [
        ("INFO", {"bronze_transactions": 3, "argent_transactions": 4, "duree_s": 3.0})
    ]
    assert capsys.readouterr().out == (
        "\n=== Démarrage du consommateur (modèle : v-test) ===\n"
        "\n=== Producteur : source « jeu », jour 2020-12-21 ===\n"
        "\n=== Attente du traitement complet par le consommateur ===\n"
        "\n=== Rapport du matin pour le 2020-12-21 ===\n"
        "Paiements : 10 · fraudes détectées : 2 · rejets : 1\n"
        "Rapport HTML : h.html\n"
        "Liste CSV    : c.csv\n"
        "\n=== Bilan ===\n"
        "Lignes lues : 5 · publiées : 4 · rejetées : 1 · doublons : 0\n"
        "Contenu de l'entrepôt : bronze_transactions = 3 · argent_transactions = 4\n"
        "Collecte : 1.5 s ; traitement complet : 3.0 s\n"
        "Supervision : aucune anomalie\n"
    )


def test_sans_reinitialiser_la_demo_ne_touche_pas_aux_donnees_et_rebaser_sur_remplace_le_jour(
    config, banc_demo, capsys
):
    """Pas de ``--reinitialiser`` : rien n'est supprimé ; ``--rebaser-sur`` impose le jour."""
    marqueur = config.notification.fichier_alertes
    marqueur.parent.mkdir(parents=True, exist_ok=True)
    marqueur.write_text("alerte précédente\n", encoding="utf-8")
    marqueur_avant = marqueur.read_bytes()
    assert demo(config, arguments_demo(rebaser_sur="2026-10-09")) == 0
    assert not any(a[0] == "reinitialiser" for a in banc_demo.appels)
    assert marqueur.read_bytes() == marqueur_avant
    rejeu = next(a for a in banc_demo.appels if a[0] == "rejouer_jeu")[1]
    assert rejeu["jour"] == "2026-10-09" and rejeu["rebaser_sur"] == "2026-10-09"
    assert ("rapport", "2026-10-09", "Notificateur") in banc_demo.appels
    assert "=== Producteur : source « jeu », jour 2026-10-09 ===\n" in capsys.readouterr().out


def test_la_demo_api_utilise_la_date_du_jour_les_pannes_simulees_et_signale_les_anomalies(
    config, banc_demo, capsys, evenements
):
    """Source « api » : jour = aujourd'hui (UTC), pannes simulées, anomalies affichées."""
    banc_demo.rapport = DonneesAbsentes("0 transaction(s) le 2026-10-10 : muette.")
    banc_demo.constats = ["constat-a", "constat-b"]
    assert demo(config, arguments_demo(source="api", pannes=2, appels=7, intervalle=0.5)) == 0
    sortie = capsys.readouterr().out
    assert "=== Producteur : source « api » ===\n" in sortie
    assert "jour 20" not in sortie
    assert (
        "=== Rapport du matin pour le 2026-10-10 ===\nPas de rapport : 0 transaction(s)" in sortie
    )
    assert "Supervision : ['constat-a', 'constat-b']\n" in sortie
    _, arreter, kwargs = next(a for a in banc_demo.appels if a[0] == "boucle_api")
    assert set(kwargs) == {"nombre_appels", "intervalle_s", "get"}
    assert (kwargs["nombre_appels"], kwargs["intervalle_s"]) == (7, 0.5)
    assert isinstance(kwargs["get"], PannesSimulees)
    for _ in range(2):
        with pytest.raises(requests.ConnectionError) as panne:
            kwargs["get"]("http://exemple.test")
        assert str(panne.value) == "panne réseau simulée pour la démonstration"
    assert kwargs["get"]("http://exemple.test") is banc_demo.reponse_http
    assert not any(a[0] == "rejouer_jeu" for a in banc_demo.appels)
    # Sans panne demandée, ``requests.get`` est appelé directement, sans enveloppe.
    banc_demo.appels.clear()
    assert demo(config, arguments_demo(source="api", pannes=0)) == 0
    _, _, kwargs = next(a for a in banc_demo.appels if a[0] == "boucle_api")
    assert not isinstance(kwargs["get"], PannesSimulees)
    assert kwargs["get"]("x") is banc_demo.reponse_http


def test_une_panne_du_consommateur_est_relevee_apres_l_attente_et_aucun_rapport_n_est_produit(
    config, banc_demo, capsys
):
    """Si le fil consommateur a échoué, la démo s'arrête sur son erreur : pas de faux rapport."""
    banc_demo.erreur_apres_attente = RuntimeError("consommateur en panne (test)")
    with pytest.raises(RuntimeError) as erreur:
        demo(config, arguments_demo())
    assert str(erreur.value) == "consommateur en panne (test)"
    assert not any(a[0] == "rapport" for a in banc_demo.appels)
    assert banc_demo.controle_kwargs == []
    arreter = next(a for a in banc_demo.appels if a[0] == "boucle")[1]
    assert arreter.is_set()  # le fil est arrêté même en cas d'échec
    assert "Rapport du matin" not in capsys.readouterr().out


def test_une_attente_expiree_arrete_le_consommateur_et_n_enchaine_pas_sur_le_rapport(
    config, banc_demo, monkeypatch
):
    """Délai dépassé : le fil est arrêté (``arreter`` levé) et la démo remonte le TimeoutError."""

    def attente_expiree(c, erreurs, delai_s=600.0):
        raise TimeoutError("Le consommateur n'a pas vidé la file dans le délai imparti.")

    monkeypatch.setattr(module_local, "_attendre_file_vide", attente_expiree)
    with pytest.raises(TimeoutError):
        demo(config, arguments_demo())
    arreter = next(a for a in banc_demo.appels if a[0] == "boucle")[1]
    assert arreter.is_set()
    assert not any(a[0] == "rapport" for a in banc_demo.appels)


# --------------------------------------------------------------------------------------------
# Précisions : ce que les tests ci-dessus laissaient passer (relevé par la mutation)
# --------------------------------------------------------------------------------------------
def test_mille_entrainements_d_une_heure_font_exactement_mille_vcpu_heures():
    """Le diviseur est 3 600 s : un diviseur voisin se verrait dès la troisième décimale."""
    resultat = estimer(
        **{**CONSOMMATIONS, "duree_entrainement_s": 3600.0}, entrainements_par_mois=1000.0
    )
    assert resultat["postes"]["calcul_entrainement_hebdomadaire"]["consommation"] == 1000.0


def test_un_titre_est_imprime_entre_deux_lignes_vides_et_vide_aussitot_le_tampon(monkeypatch):
    """``flush=True`` : la ligne de titre apparaît avant le travail long qu'elle annonce."""
    appels: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: appels.append((args, kwargs)))
    _titre("Bilan")
    assert appels == [(("\n=== Bilan ===",), {"flush": True})]


def test_reinitialiser_ignore_un_rapport_disparu_entre_la_liste_et_la_suppression(
    config, monkeypatch
):
    """Un rapport listé puis supprimé par un autre processus ne fait pas échouer le nettoyage."""
    dossier = config.rapport.dossier
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "rapport_x.csv").write_text("x", encoding="utf-8")
    (dossier / "notes.txt").write_text("à garder", encoding="utf-8")
    glob_reel = Path.glob

    def glob_avec_fantome(self: Path, motif: str):
        resultats = list(glob_reel(self, motif))
        if self == dossier:
            resultats.append(dossier / "fantome.html")  # listé, mais déjà supprimé
        return iter(resultats)

    monkeypatch.setattr(Path, "glob", glob_avec_fantome)
    reinitialiser(config)
    assert sorted(f.name for f in dossier.iterdir()) == ["notes.txt"]


def test_une_echeance_deja_atteinte_ne_laisse_aucune_pause(config, temps_simule, files_fermees):
    """Délai nul : on ne dort pas une fois de plus, l'attente échoue tout de suite."""
    publier_et_lire(config, config.file.sujet_valides)
    with pytest.raises(TimeoutError):
        _attendre_file_vide(config, [], delai_s=0.0)
    assert temps_simule.pauses == []
    assert files_fermees == ["fermee"]


class HorlogeUTCSeulement(datetime):
    """``now`` n'accepte que l'UTC : une heure locale (``now()`` ou ``now(None)``) est refusée."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is not UTC:
            raise AssertionError("la date du jour doit se lire en UTC")
        return datetime(2026, 10, 10, 23, 59, tzinfo=UTC)


def test_la_demo_api_lit_la_date_du_jour_en_utc(config, banc_demo, monkeypatch, capsys):
    """Le jour du rapport est celui de l'UTC, jamais celui du fuseau de la machine."""
    monkeypatch.setattr(module_local, "datetime", HorlogeUTCSeulement)
    assert demo(config, arguments_demo(source="api", pannes=0)) == 0
    assert ("rapport", "2026-10-10", "Notificateur") in banc_demo.appels
    assert "Rapport du matin pour le 2026-10-10" in capsys.readouterr().out


def test_la_demo_api_donne_a_la_boucle_la_configuration_le_producteur_et_le_signal_d_arret(
    config, banc_demo, monkeypatch
):
    """``boucle_api`` reçoit, dans l'ordre : la configuration, le producteur, le signal d'arrêt."""
    recus: list[tuple[Any, ...]] = []

    def boucle_espionne(c, producteur, arreter, **kwargs):
        recus.append((c, producteur, arreter))
        return banc_demo.bilan

    monkeypatch.setattr(module_local, "boucle_api", boucle_espionne)
    assert demo(config, arguments_demo(source="api", pannes=0)) == 0
    ((config_recue, producteur, arreter),) = recus
    assert config_recue is config
    assert type(producteur).__name__ == "FauxProducteur"
    assert isinstance(arreter, threading.Event) and arreter.is_set()


def test_la_demo_jeu_donne_a_rejouer_jeu_la_configuration_et_le_producteur(
    config, banc_demo, monkeypatch
):
    """``rejouer_jeu`` reçoit la configuration puis le producteur construit pour la démo."""
    recus: list[tuple[Any, ...]] = []

    def rejeu_espionne(c, producteur, **kwargs):
        recus.append((c, producteur))
        return banc_demo.bilan

    monkeypatch.setattr(module_local, "rejouer_jeu", rejeu_espionne)
    assert demo(config, arguments_demo()) == 0
    ((config_recue, producteur),) = recus
    assert config_recue is config
    assert type(producteur).__name__ == "FauxProducteur"


def test_la_demo_hache_les_cartes_avec_le_sel_et_la_longueur_de_la_configuration(
    config, banc_demo, monkeypatch
):
    """La longueur d'empreinte vient de la configuration (24 ici), pas de la valeur par défaut."""
    construits: list[tuple[bytes, int]] = []

    class HacheurEspion:
        def __init__(self, cle: bytes, longueur: int) -> None:
            construits.append((cle, longueur))

    monkeypatch.setattr(module_local, "HacheurCarte", HacheurEspion)
    court = replace(config, longueur_hash=24)
    assert demo(court, arguments_demo()) == 0
    assert construits == [(court.sel_hachage(), 24)]


def test_la_demo_attend_au_plus_trente_secondes_la_fin_du_fil_consommateur(
    config, banc_demo, monkeypatch
):
    """Le fil consommateur est joint avec un délai de 30 s : jamais indéfiniment, jamais plus."""
    delais: list[Any] = []
    fil_reel = threading.Thread

    class FilEspion(fil_reel):
        def join(self, timeout: float | None = None) -> None:
            delais.append(timeout)
            super().join(timeout)

    monkeypatch.setattr(threading, "Thread", FilEspion)
    assert demo(config, arguments_demo()) == 0
    assert delais == [30]


def test_une_panne_au_demarrage_du_consommateur_est_remontee_telle_quelle(
    config, banc_demo, monkeypatch
):
    """L'exception du fil consommateur est conservée (pas remplacée) et relevée par la démo."""
    panne = RuntimeError("démarrage impossible (test)")

    def construire_en_panne(c):
        raise panne

    monkeypatch.setattr(module_local, "construire_consommateur", construire_en_panne)
    with pytest.raises(RuntimeError) as erreur:
        demo(config, arguments_demo())
    assert erreur.value is panne
    assert not any(a[0] == "rapport" for a in banc_demo.appels)


def test_la_duree_totale_du_journal_est_arrondie_au_dixieme_de_seconde(
    config, banc_demo, evenements, monkeypatch
):
    """Deux lectures d'horloge de 1,2345 s font 2,469 s au total : le journal en garde 2,5."""
    monkeypatch.setattr(module_local, "time", FauxTemps(pas=1.2345))
    assert demo(config, arguments_demo()) == 0
    ((niveau, champs),) = evenements.nommes("demo_terminee")
    assert niveau == "INFO"
    assert champs["duree_s"] == 2.5 and isinstance(champs["duree_s"], float)
