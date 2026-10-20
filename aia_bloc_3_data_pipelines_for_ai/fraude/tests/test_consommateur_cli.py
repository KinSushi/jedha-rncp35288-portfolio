# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Consommateur en ligne de commande : aide, codes de sortie, démarrage, arrêt, assemblage."""

from __future__ import annotations

import json
import signal
import sqlite3
import threading
from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from detection_fraude import consommateur as module_consommateur
from detection_fraude.configuration import ErreurConfiguration
from detection_fraude.consommateur import construire_consommateur, main
from detection_fraude.entrepot import Entrepot
from detection_fraude.file_attente import FileSQLite
from detection_fraude.masquage import masquer
from detection_fraude.modele import ErreurModele, exporter_modele, promouvoir
from tests.helpers import ligne_brute


# --------------------------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------------------------
class PipelineParMontant:
    """Pipeline minimal et déterministe : fraude (0,9) au-dessus de 500, sinon 0,1."""

    def predict_proba(self, caracteristiques):
        p = np.where(caracteristiques["montant"].to_numpy() > 500, 0.9, 0.1)
        return np.column_stack([1 - p, p])


class Plomberie:
    """Ce que ``main`` fait de global : journal, signaux, assemblage, fermeture, boucle."""

    def __init__(self) -> None:
        self.ordre: list[str] = []
        self.journal: list[tuple[tuple, dict]] = []
        self.signaux: list[tuple[Any, Any]] = []
        self.assemblages: list[Any] = []
        self.fermetures = 0
        self.boucles: list[tuple[threading.Event, bool, bool]] = []

    def declencher_les_signaux(self) -> None:
        for numero, gestionnaire in self.signaux:
            gestionnaire(numero, None)


class FauxConsommateur:
    def __init__(self, plomberie: Plomberie, *, pendant=None, erreur=None) -> None:
        self.plomberie, self.pendant, self.erreur = plomberie, pendant, erreur

    def boucle(self, arreter, *, jusqu_a_vide=False):
        self.plomberie.ordre.append("boucle")
        self.plomberie.boucles.append((arreter, jusqu_a_vide, arreter.is_set()))
        if self.pendant:
            self.pendant(arreter)
        if self.erreur:
            raise self.erreur
        return 0


@pytest.fixture
def plomberie(monkeypatch):
    """Neutralise le journal global et les signaux (sinon ``main`` casserait pytest)."""
    p = Plomberie()

    def faux_journal(*args, **kwargs):
        p.ordre.append("journal")
        p.journal.append((args, kwargs))

    def faux_signal(numero, gestionnaire):
        p.ordre.append("signal")
        p.signaux.append((numero, gestionnaire))

    monkeypatch.setattr(module_consommateur, "configurer_journal", faux_journal)
    monkeypatch.setattr(module_consommateur.signal, "signal", faux_signal)
    monkeypatch.setenv("COLUMNS", "200")  # l'aide ne doit pas être coupée au milieu d'une phrase
    return p


def installer_assemblage(
    monkeypatch, plomberie: Plomberie, consommateur: Any, a_la_fermeture=None
) -> None:
    """Remplace ``construire_consommateur`` : rend le consommateur factice et une fermeture."""

    def fermer() -> None:
        plomberie.ordre.append("fermer")
        plomberie.fermetures += 1
        if a_la_fermeture:
            a_la_fermeture()

    def faux_assemblage(config):
        plomberie.ordre.append("assemblage")
        plomberie.assemblages.append(config)
        return consommateur, fermer

    monkeypatch.setattr(module_consommateur, "construire_consommateur", faux_assemblage)


def installer_assemblage_en_echec(monkeypatch, plomberie: Plomberie, erreur: Exception) -> None:
    def echec(config):
        plomberie.ordre.append("assemblage")
        raise erreur

    monkeypatch.setattr(module_consommateur, "construire_consommateur", echec)


def installer_espion_de_configuration(monkeypatch, plomberie: Plomberie) -> list[str]:
    appels: list[str] = []
    reel = module_consommateur.charger_configuration

    def espion(*args, **kwargs):
        appels.append("charger")
        plomberie.ordre.append("configuration")
        return reel(*args, **kwargs)

    monkeypatch.setattr(module_consommateur, "charger_configuration", espion)
    return appels


def evenements_de(evenements, *noms: str) -> list[tuple]:
    return [e for e in evenements.tous() if e[1] in noms]


# --------------------------------------------------------------------------------------------
# Analyse des arguments
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("option", ["--help", "-h"])
def test_l_aide_decrit_le_programme_et_l_option_puis_sort_proprement_sans_rien_charger(
    plomberie, monkeypatch, capsys, evenements, option
):
    """``--help`` affiche la description et l'option ``--jusqu-a-vide`` puis sort avec 0."""
    chargements = installer_espion_de_configuration(monkeypatch, plomberie)
    with pytest.raises(SystemExit) as sortie:
        main([option])
    assert sortie.value.code == 0
    texte = " ".join(capsys.readouterr().out.split())
    assert "usage:" in texte
    assert "[--jusqu-a-vide]" in texte
    assert "Consommateur : file -> notation -> entrepôt." in texte
    assert "--jusqu-a-vide s'arrête quand la file est vidée (démonstration, tests)" in texte
    assert "-h, --help" in texte
    assert chargements == [] and plomberie.ordre == [] and evenements.tous() == []


@pytest.mark.parametrize(
    "arguments", [["--option-inconnue"], ["positionnel"], ["--jusqu-a-vide=1"]]
)
def test_un_argument_inconnu_est_refuse_avec_le_code_2_et_rien_n_est_charge(
    plomberie, monkeypatch, capsys, evenements, arguments
):
    """Un argument inattendu fait sortir avec le code 2 d'argparse, avant tout chargement."""
    chargements = installer_espion_de_configuration(monkeypatch, plomberie)
    with pytest.raises(SystemExit) as sortie:
        main(arguments)
    assert sortie.value.code == 2
    erreur = capsys.readouterr().err
    assert "usage:" in erreur and "error:" in erreur
    assert chargements == [] and plomberie.ordre == [] and evenements.tous() == []


# --------------------------------------------------------------------------------------------
# Démarrage impossible : code de sortie 2
# --------------------------------------------------------------------------------------------
def test_sans_modele_en_service_main_sort_avec_2_et_journalise_la_cause_exacte(
    config, plomberie, evenements
):
    """Aucun modèle promu : arrêt net (code 2) avec la consigne d'entraînement, rien ne démarre."""
    code_sortie = main([])
    assert code_sortie == 2
    attendu = (
        f"Aucun modèle en service dans {config.modele.dossier}. "
        "Lancer l'entraînement : python -m detection_fraude.entrainement"
    )
    assert evenements.tous() == [("ERROR", "demarrage_impossible", {"cause": attendu})]
    assert plomberie.boucles == []
    assert plomberie.ordre == ["journal", "signal", "signal"]


def test_une_erreur_de_modele_au_demarrage_donne_le_code_2_et_sa_cause(
    plomberie, monkeypatch, evenements
):
    """Toute ``ErreurModele`` d'assemblage (empreinte fausse, etc.) arrête avec le code 2."""
    installer_assemblage_en_echec(
        monkeypatch, plomberie, ErreurModele("Empreinte SHA-256 incorrecte pour x : refusé.")
    )
    code_sortie = main(["--jusqu-a-vide"])
    assert code_sortie == 2
    assert evenements.tous() == [
        (
            "ERROR",
            "demarrage_impossible",
            {"cause": "Empreinte SHA-256 incorrecte pour x : refusé."},
        )
    ]
    assert plomberie.boucles == [] and plomberie.fermetures == 0


def test_une_erreur_de_configuration_au_demarrage_donne_le_code_2_et_sa_cause(
    plomberie, monkeypatch, evenements
):
    """Toute ``ErreurConfiguration`` d'assemblage (webhook en clair…) arrête avec le code 2."""
    installer_assemblage_en_echec(
        monkeypatch, plomberie, ErreurConfiguration("Le webhook doit utiliser HTTPS.")
    )
    code_sortie = main([])
    assert code_sortie == 2
    assert evenements.tous() == [
        ("ERROR", "demarrage_impossible", {"cause": "Le webhook doit utiliser HTTPS."})
    ]
    assert plomberie.boucles == []


def test_une_erreur_inattendue_au_demarrage_n_est_pas_deguisee_en_code_2(
    plomberie, monkeypatch, evenements
):
    """Une panne du système (disque plein) remonte telle quelle : l'orchestrateur la verra."""
    installer_assemblage_en_echec(monkeypatch, plomberie, OSError("disque plein"))
    with pytest.raises(OSError) as exc:
        main([])
    assert str(exc.value) == "disque plein"
    assert evenements_de(evenements, "demarrage_impossible", "consommateur_demarre") == []
    assert plomberie.boucles == []


# --------------------------------------------------------------------------------------------
# Démarrage et arrêt
# --------------------------------------------------------------------------------------------
def test_main_prepare_journal_et_signaux_avant_d_assembler_meme_si_l_assemblage_echoue(
    config, plomberie, monkeypatch
):
    """Ordre : configuration, journal (niveau, console, fichier), deux signaux, puis assemblage."""
    monkeypatch.setenv("FRAUDE_NIVEAU_JOURNAL", "debug")
    chargements = installer_espion_de_configuration(monkeypatch, plomberie)
    installer_assemblage_en_echec(monkeypatch, plomberie, ErreurModele("pas de modèle"))
    code_sortie = main([])
    assert code_sortie == 2
    assert chargements == ["charger"]
    assert plomberie.ordre == ["configuration", "journal", "signal", "signal", "assemblage"]
    assert plomberie.journal == [(("DEBUG", "lisible", config.journal.fichier), {})]
    assert [numero for numero, _ in plomberie.signaux] == [signal.SIGINT, signal.SIGTERM]
    assert all(callable(gestionnaire) for _, gestionnaire in plomberie.signaux)


@pytest.mark.parametrize(
    ("argv", "variables", "jusqu_a_vide", "environnement", "file"),
    [
        ([], {}, False, "dev", "sqlite"),
        (["--jusqu-a-vide"], {}, True, "dev", "sqlite"),
        (None, {}, True, "dev", "sqlite"),  # pas de liste : ``sys.argv`` du processus
        ([], {"FRAUDE_ENV": "prod"}, False, "prod", "kafka"),
    ],
)
def test_main_lance_la_boucle_selon_la_ligne_de_commande_la_configuration_et_les_signaux(
    plomberie, monkeypatch, evenements, argv, variables, jusqu_a_vide, environnement, file
):
    """Option, environnement, file arrivent à la boucle et au journal ; les signaux l'arrêtent."""
    for nom, valeur in variables.items():
        monkeypatch.setenv(nom, valeur)
    if argv is None:
        monkeypatch.setattr("sys.argv", ["consommateur", "--jusqu-a-vide"])
    observe: dict[str, bool] = {}

    def pendant(arreter):
        observe["au_depart"] = arreter.is_set()
        plomberie.signaux[0][1](signal.SIGINT, None)
        observe["apres_sigint"] = arreter.is_set()
        arreter.clear()
        plomberie.signaux[1][1](signal.SIGTERM, object())  # le second argument est un « cadre »
        observe["apres_sigterm"] = arreter.is_set()

    vus_a_la_fermeture: list[int] = []
    installer_assemblage(
        monkeypatch,
        plomberie,
        FauxConsommateur(plomberie, pendant=pendant),
        a_la_fermeture=lambda: vus_a_la_fermeture.append(len(evenements.tous())),
    )
    assert (main() if argv is None else main(argv)) == 0
    ((arreter, option, deja_leve),) = plomberie.boucles
    assert option is jusqu_a_vide and deja_leve is False
    assert observe == {"au_depart": False, "apres_sigint": True, "apres_sigterm": True}
    assert plomberie.assemblages[0].environnement == environnement
    assert evenements.tous() == [
        ("INFO", "consommateur_demarre", {"environnement": environnement, "file": file}),
        ("INFO", "consommateur_arrete", {}),
    ]
    assert vus_a_la_fermeture == [1]  # fermeture après l'annonce de démarrage, avant celle d'arrêt


def test_main_ferme_les_ressources_et_annonce_l_arret_meme_si_la_boucle_plante(
    plomberie, monkeypatch, evenements
):
    """Une exception de la boucle ressort, mais tout est fermé et l'arrêt est journalisé."""
    faux = FauxConsommateur(plomberie, erreur=RuntimeError("boucle cassée"))
    installer_assemblage(monkeypatch, plomberie, faux)
    with pytest.raises(RuntimeError) as exc:
        main([])
    assert str(exc.value) == "boucle cassée"
    assert plomberie.fermetures == 1
    assert evenements.tous() == [
        ("INFO", "consommateur_demarre", {"environnement": "dev", "file": "sqlite"}),
        ("INFO", "consommateur_arrete", {}),
    ]


def test_main_de_bout_en_bout_traite_la_file_puis_un_rejeu_complet_ne_change_rien(
    config, hacheur, plomberie, monkeypatch, evenements
):
    """Vrai modèle exporté : lot traité, alerte envoyée ; un rejeu complet ne change rien."""
    sans_attente = replace(config, file=replace(config.file, attente_lecture_s=0.0))
    monkeypatch.setattr(module_consommateur, "charger_configuration", lambda: sans_attente)
    exporter_modele(config.modele.dossier, PipelineParMontant(), 0.5, {"version": "v-cli"})
    promouvoir(config.modele.dossier, "v-cli")
    declenchements: list[str] = []

    def publier_les_trois_transactions() -> None:
        file = FileSQLite(config.file.chemin_sqlite)
        for rang, montant in ((1, 20.0), (2, 900.0), (3, 35.0)):
            valeur = masquer(ligne_brute(rang, amt=montant), hacheur, source="jeu")
            file.publier(config.file.sujet_valides, valeur, cle=valeur["carte_hash"])
        file.fermer()

    def executer() -> int:
        def chien_de_garde() -> None:  # filet : si la boucle ne s'arrête pas seule, on la stoppe
            declenchements.append("declenche")
            plomberie.declencher_les_signaux()

        minuteur = threading.Timer(1.0, chien_de_garde)
        minuteur.daemon = True
        minuteur.start()
        try:
            return main(["--jusqu-a-vide"])
        finally:
            minuteur.cancel()

    def photographie() -> dict[str, Any]:
        base = Entrepot(config.dsn_entrepot())
        try:
            return {
                t: base.requete(f"SELECT * FROM {t} ORDER BY trans_num")
                for t in ("bronze_transactions", "argent_transactions", "or_alertes_fraude")
            }
        finally:
            base.fermer()

    publier_les_trois_transactions()
    code_premiere = executer()
    assert declenchements == []  # arrêtée d'elle-même, pas par le chien de garde
    assert code_premiere == 0
    bronze_avant = photographie()
    assert [len(v) for v in bronze_avant.values()] == [3, 3, 1]
    (alerte,) = bronze_avant["or_alertes_fraude"]
    assert alerte["statut_notification"] == "envoyee" and alerte["montant"] == 900.0
    assert alerte["version_modele"] == "v-cli"
    lignes = bronze_avant["argent_transactions"]
    assert sorted((x["est_fraude"], x["version_modele"]) for x in lignes) == [
        (0, "v-cli"),
        (0, "v-cli"),
        (1, "v-cli"),
    ]
    etat = json.loads((config.racine_donnees / "etat" / "consommateur.json").read_text("utf-8"))
    assert etat["modele"] == "v-cli"
    publier_les_trois_transactions()  # la file rejoue les mêmes transactions
    code_rejeu = executer()
    assert declenchements == [] and code_rejeu == 0
    assert photographie() == bronze_avant  # ni doublon, ni seconde alerte
    noms = [e[1] for e in evenements_de(evenements, "consommateur_demarre", "consommateur_arrete")]
    assert noms == ["consommateur_demarre", "consommateur_arrete"] * 2


# --------------------------------------------------------------------------------------------
# construire_consommateur
# --------------------------------------------------------------------------------------------
class FileDouble:
    def __init__(self, ordre: list[str]) -> None:
        self.ordre = ordre

    def fermer(self) -> None:
        self.ordre.append("file.fermer")


class EntrepotDouble:
    instances: list[EntrepotDouble] = []
    ordre: list[str] = []

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        type(self).ordre.append("entrepot")
        type(self).instances.append(self)

    def initialiser(self) -> None:
        type(self).ordre.append("entrepot.initialiser")

    def fermer(self) -> None:
        type(self).ordre.append("entrepot.fermer")


class ChargeurDouble:
    instances: list[ChargeurDouble] = []
    ordre: list[str] = []

    def __init__(self, dossier) -> None:
        self.dossier = dossier
        self.obtentions = 0
        type(self).ordre.append("chargeur")
        type(self).instances.append(self)

    erreur: Exception | None = None

    def obtenir(self):
        self.obtentions += 1
        type(self).ordre.append("chargeur.obtenir")
        if type(self).erreur is not None:
            raise type(self).erreur


class NotificateurDouble:
    def __init__(self, canaux) -> None:
        self.canaux = canaux


class ConsommateurDouble:
    def __init__(self, *args, **kwargs) -> None:
        self.args, self.kwargs = args, kwargs


def test_l_assemblage_cree_chaque_piece_dans_l_ordre_et_la_fermeture_les_ferme_dans_l_ordre(
    config, monkeypatch
):
    """Tout est passé au consommateur ; sans modèle, ni canaux ni consommateur ne sont créés."""
    ordre: list[str] = []
    file = FileDouble(ordre)
    canaux = ["canal-1", "canal-2"]
    EntrepotDouble.instances, EntrepotDouble.ordre = [], ordre
    ChargeurDouble.instances, ChargeurDouble.ordre = [], ordre
    ChargeurDouble.erreur = None
    appels_file, appels_canaux = [], []

    def fausse_file(configuration):
        ordre.append("file")
        appels_file.append(configuration)
        return file

    def faux_canaux(configuration):
        ordre.append("canaux")
        appels_canaux.append(configuration)
        return canaux

    monkeypatch.setattr(module_consommateur, "construire_file", fausse_file)
    monkeypatch.setattr(module_consommateur, "Entrepot", EntrepotDouble)
    monkeypatch.setattr(module_consommateur, "ChargeurModele", ChargeurDouble)
    monkeypatch.setattr(module_consommateur, "construire_canaux", faux_canaux)
    monkeypatch.setattr(module_consommateur, "Notificateur", NotificateurDouble)
    monkeypatch.setattr(module_consommateur, "Consommateur", ConsommateurDouble)

    consommateur, fermer = construire_consommateur(config)

    assert ordre == [
        "file",
        "entrepot",
        "entrepot.initialiser",
        "chargeur",
        "chargeur.obtenir",
        "canaux",
    ]
    assert appels_file == [config] and appels_canaux == [config]
    (entrepot,) = EntrepotDouble.instances
    (chargeur,) = ChargeurDouble.instances
    assert entrepot.dsn == config.dsn_entrepot()
    assert chargeur.dossier == config.modele.dossier and chargeur.obtentions == 1
    assert isinstance(consommateur, ConsommateurDouble)
    (c, f, e, m, n) = consommateur.args
    assert (c, f, e, m) == (config, file, entrepot, chargeur)
    assert isinstance(n, NotificateurDouble) and n.canaux is canaux
    assert consommateur.kwargs == {}
    ordre.clear()
    fermer()
    assert ordre == ["file.fermer", "entrepot.fermer"]  # le modèle, lui, n'a rien à fermer

    # Même assemblage, mais le modèle est introuvable : l'erreur remonte, rien n'est construit.
    ordre.clear()
    ChargeurDouble.erreur = ErreurModele("aucun modèle")
    with pytest.raises(ErreurModele) as exc:
        construire_consommateur(config)
    assert str(exc.value) == "aucun modèle"
    assert ordre == ["file", "entrepot", "entrepot.initialiser", "chargeur", "chargeur.obtenir"]


def test_l_assemblage_sans_modele_leve_l_erreur_exacte_et_ne_construit_pas_les_canaux(
    config, monkeypatch
):
    """Le modèle est vérifié dès l'assemblage ; sans lui, ni canaux ni consommateur n'existent."""
    canaux_construits: list[Any] = []
    monkeypatch.setattr(
        module_consommateur, "construire_canaux", lambda c: canaux_construits.append(c) or []
    )
    with pytest.raises(ErreurModele) as exc:
        construire_consommateur(config)
    assert str(exc.value) == (
        f"Aucun modèle en service dans {config.modele.dossier}. "
        "Lancer l'entraînement : python -m detection_fraude.entrainement"
    )
    assert canaux_construits == []


def test_l_assemblage_reel_initialise_l_entrepot_ouvre_la_file_et_ferme_vraiment_la_file(config):
    """Avec un modèle exporté : entrepôt et file prêts ; ``fermer`` ferme réellement la file."""
    exporter_modele(config.modele.dossier, PipelineParMontant(), 0.5, {"version": "v-asm"})
    promouvoir(config.modele.dossier, "v-asm")
    assert not config.file.chemin_sqlite.exists()
    consommateur, fermer = construire_consommateur(config)
    try:
        assert isinstance(consommateur, module_consommateur.Consommateur)
        assert config.file.chemin_sqlite.exists()
        assert consommateur.envoyer_alertes() == {"envoyees": 0, "reportees": 0, "en_echec": 0}
        bilan = consommateur.executer_une_fois(attente_s=0)
        assert bilan.recus == 0
    finally:
        fermer()
    etat = json.loads((config.racine_donnees / "etat" / "consommateur.json").read_text("utf-8"))
    assert etat["modele"] == "v-asm"
    with pytest.raises(sqlite3.ProgrammingError) as exc:  # la file a bien été fermée
        consommateur.archiver_rejets()
    assert str(exc.value) == "Cannot operate on a closed database."
