# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Ligne de commande d'entraînement (critère 3.3) : options, barrière de qualité, codes de sortie.

``main`` est testé avec des **doublures qui enregistrent leurs arguments** (configuration,
journal, ``entrainer``, ``valider_candidat``, ``promouvoir_version``) dans un seul journal
d'appels ordonné : on prouve ainsi quoi est appelé, avec quelles valeurs, et dans quel ordre.
Les refus (option inconnue, entier invalide, barrière qui refuse, entraînement en échec,
configuration invalide) vérifient le code de sortie ou l'exception, et que rien n'est promu.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from detection_fraude import entrainement
from detection_fraude.configuration import ErreurConfiguration
from detection_fraude.entrainement import BarriereQualiteRefusee, main
from detection_fraude.modele import ErreurModele

VERSION = "20261011T123456Z"
RESULTAT = {
    "version": VERSION,
    "modele": "modèle-éprouvé",
    "seuil": 0.75,
    "metriques_test": {"pr_auc": 0.5, "nb_lignes": 60},
    "date_coupure_test": "2020-07-11",
    "duree_s": 12.5,
    # clés internes que la commande ne doit PAS afficher :
    "dossier": "/chemin/interne/modeles/20261011T123456Z",
    "metriques_validation": {"pr_auc": 0.4},
    "candidats": {"regression_logistique": {"seuil": 0.25}},
}
SORTIE_JSON = (
    "{\n"
    '  "version": "20261011T123456Z",\n'
    '  "modele": "modèle-éprouvé",\n'
    '  "seuil": 0.75,\n'
    '  "metriques_test": {\n'
    '    "pr_auc": 0.5,\n'
    '    "nb_lignes": 60\n'
    "  },\n"
    '  "date_coupure_test": "2020-07-11",\n'
    '  "duree_s": 12.5\n'
    "}\n"
)
EN_SERVICE_INCONNU = object()


def config_cli(config, tmp_path: Path):
    """Configuration dont les réglages de journal sont distincts des valeurs par défaut."""
    return replace(
        config,
        journal=replace(
            config.journal, niveau="WARNING", console="json", fichier=tmp_path / "a.log"
        ),
    )


class Journal:
    """Journal ordonné de tous les appels reçus par les doublures de ``main``."""

    def __init__(self) -> None:
        self.appels: list[tuple[Any, ...]] = []

    def noms(self) -> list[str]:
        return [appel[0] for appel in self.appels]


def brancher(
    monkeypatch,
    config,
    *,
    en_service: Any = EN_SERVICE_INCONNU,
    panne_entrainer: BaseException | None = None,
    panne_valider: BaseException | None = None,
) -> Journal:
    """Remplace configuration, journal, entraînement, barrière et promotion par des doublures."""
    journal = Journal()

    def charger(*args, **options):
        journal.appels.append(("charger_configuration", args, options))
        return config

    def configurer(*args, **options):
        journal.appels.append(("configurer_journal", args, options))

    def entrainer_faux(*args, **options):
        journal.appels.append(("entrainer", args, options))
        if panne_entrainer is not None:
            raise panne_entrainer
        return dict(RESULTAT)

    def version_en_service(*args, **options):
        journal.appels.append(("version_en_service", args, options))
        return VERSION if en_service is EN_SERVICE_INCONNU else en_service

    def valider(*args, **options):
        journal.appels.append(("valider_candidat", args, options))
        if panne_valider is not None:
            raise panne_valider
        return {"accepte": True}

    def promouvoir_faux(*args, **options):
        journal.appels.append(("promouvoir_version", args, options))

    monkeypatch.setattr(entrainement, "charger_configuration", charger)
    monkeypatch.setattr(entrainement, "configurer_journal", configurer)
    monkeypatch.setattr(entrainement, "entrainer", entrainer_faux)
    monkeypatch.setattr(entrainement, "version_en_service", version_en_service)
    monkeypatch.setattr(entrainement, "valider_candidat", valider)
    monkeypatch.setattr(entrainement, "promouvoir_version", promouvoir_faux)
    return journal


# --------------------------------------------------------------------------------------------
# Câblage nominal
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "donnees", "limite", "suivre_mlflow", "promouvoir"),
    [
        pytest.param([], None, None, True, False, id="valeurs_par_defaut"),
        pytest.param(
            ["--donnees", "jeu/essai.csv", "--limite", "50"],
            Path("jeu/essai.csv"),
            50,
            True,
            False,
            id="donnees_et_limite",
        ),
        pytest.param(["--sans-mlflow"], None, None, False, False, id="sans_mlflow"),
        pytest.param(["--limite", "0"], None, 0, True, False, id="limite_zero_reste_un_entier"),
    ],
)
def test_main_transmet_les_options_a_entrainer_sans_effet_de_bord_et_imprime_les_cles_retenues(
    monkeypatch, config, tmp_path, capsys, argv, donnees, limite, suivre_mlflow, promouvoir
):
    """Chaque option arrive à ``entrainer`` sous son bon nom, avec son bon type ; la sortie est
    le JSON exact des six clés retenues (accents conservés, indentation de 2 espaces)."""
    configuration = config_cli(config, tmp_path)
    journal = brancher(monkeypatch, configuration)
    assert main(argv) == 0
    assert journal.appels == [
        ("charger_configuration", (), {}),
        ("configurer_journal", ("WARNING", "json", tmp_path / "a.log"), {}),
        (
            "entrainer",
            (configuration, donnees),
            {
                "limite": limite,
                "suivre_mlflow": suivre_mlflow,
                "promouvoir_si_premier": promouvoir,
            },
        ),
    ]
    _, args, options = journal.appels[2]
    assert type(args[1]) is type(donnees)  # Path quand une valeur est fournie
    assert type(options["limite"]) is type(limite)
    captured = capsys.readouterr()
    assert captured.out == SORTIE_JSON
    assert captured.err == ""


def test_main_lit_les_arguments_de_la_ligne_de_commande_quand_aucune_liste_n_est_fournie(
    monkeypatch, config, tmp_path, capsys
):
    """Sans liste explicite, ``main`` analyse ``sys.argv[1:]`` (le nom du programme est ignoré)."""
    journal = brancher(monkeypatch, config_cli(config, tmp_path))
    monkeypatch.setattr(sys, "argv", ["entrainement", "--limite", "7", "--sans-mlflow"])
    argv_avant = list(sys.argv)
    assert main() == 0
    assert sys.argv == argv_avant  # l'analyse des arguments ne modifie pas ``sys.argv``
    _, args, options = journal.appels[2]
    assert (args[1], options["limite"], options["suivre_mlflow"]) == (None, 7, False)
    capsys.readouterr()


@pytest.mark.parametrize(
    "en_service",
    [
        pytest.param(None, id="aucun_modele_en_service"),
        pytest.param("v-autre", id="autre_version"),
    ],
)
def test_main_promouvoir_applique_la_barriere_puis_la_promotion_dans_cet_ordre(
    monkeypatch, config, tmp_path, capsys, en_service
):
    """Avec ``--promouvoir`` et une version différente de celle en service : la barrière de
    qualité s'exécute, puis seulement la promotion, sur le dossier de la configuration."""
    configuration = config_cli(config, tmp_path)
    journal = brancher(monkeypatch, configuration, en_service=en_service)
    assert main(["--promouvoir"]) == 0
    assert journal.appels[2:] == [
        (
            "entrainer",
            (configuration, None),
            {"limite": None, "suivre_mlflow": True, "promouvoir_si_premier": True},
        ),
        ("version_en_service", (configuration.modele.dossier,), {}),
        ("valider_candidat", (configuration, VERSION), {}),
        ("promouvoir_version", (configuration, VERSION), {}),
    ]
    assert capsys.readouterr().out == SORTIE_JSON


# --------------------------------------------------------------------------------------------
# La barrière et la promotion ne s'exécutent que si elles le doivent
# --------------------------------------------------------------------------------------------
def test_main_sans_promouvoir_la_barriere_et_la_promotion_restent_sans_effet(
    monkeypatch, config, tmp_path, capsys
):
    """Sans ``--promouvoir`` un entraînement ne met rien en service : ni lecture du pointeur,
    ni barrière, ni promotion."""
    journal = brancher(monkeypatch, config_cli(config, tmp_path), en_service="v-autre")
    assert main([]) == 0
    assert journal.noms() == ["charger_configuration", "configurer_journal", "entrainer"]
    capsys.readouterr()


def test_main_promouvoir_est_sans_effet_si_la_version_est_deja_en_service(
    monkeypatch, config, tmp_path, capsys
):
    """Si l'amorçage a déjà promu la version, la barrière ne tourne pas et rien n'est re-promu."""
    configuration = config_cli(config, tmp_path)
    journal = brancher(monkeypatch, configuration, en_service=VERSION)
    assert main(["--promouvoir"]) == 0
    assert journal.noms() == [
        "charger_configuration",
        "configurer_journal",
        "entrainer",
        "version_en_service",
    ]
    assert journal.appels[3] == ("version_en_service", (configuration.modele.dossier,), {})
    assert capsys.readouterr().out == SORTIE_JSON


def test_main_ne_promeut_pas_et_n_imprime_rien_quand_la_barriere_refuse(
    monkeypatch, config, tmp_path, capsys
):
    """Barrière de qualité en échec : l'exception remonte telle quelle, la promotion n'a pas lieu
    et rien n'est affiché comme un succès."""
    refus = BarriereQualiteRefusee("PR-AUC test 0.100 sous le plancher 0.300")
    journal = brancher(
        monkeypatch, config_cli(config, tmp_path), en_service="v-autre", panne_valider=refus
    )
    with pytest.raises(BarriereQualiteRefusee) as exc:
        main(["--promouvoir"])
    assert exc.value is refus
    assert str(exc.value) == "PR-AUC test 0.100 sous le plancher 0.300"
    assert "promouvoir_version" not in journal.noms()
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_main_ne_promeut_pas_quand_la_version_a_valider_est_inconnue(
    monkeypatch, config, tmp_path, capsys
):
    """Une erreur de lecture du modèle pendant la barrière arrête tout : aucune promotion."""
    erreur = ErreurModele("Métadonnées absentes pour la version v-fantome")
    journal = brancher(
        monkeypatch, config_cli(config, tmp_path), en_service=None, panne_valider=erreur
    )
    with pytest.raises(ErreurModele) as exc:
        main(["--promouvoir"])
    assert str(exc.value) == "Métadonnées absentes pour la version v-fantome"
    assert "promouvoir_version" not in journal.noms()
    assert capsys.readouterr().out == ""


def test_main_laisse_remonter_un_echec_d_entrainement_sans_barriere_ni_promotion(
    monkeypatch, config, tmp_path, capsys
):
    """Si l'entraînement échoue, même avec ``--promouvoir`` : ni lecture du pointeur, ni barrière,
    ni promotion, ni sortie."""
    panne = FileNotFoundError("Jeu de données introuvable : absent.csv")
    journal = brancher(monkeypatch, config_cli(config, tmp_path), panne_entrainer=panne)
    with pytest.raises(FileNotFoundError) as exc:
        main(["--promouvoir", "--donnees", "absent.csv"])
    assert exc.value is panne
    assert journal.noms() == ["charger_configuration", "configurer_journal", "entrainer"]
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_main_refuse_une_configuration_invalide_avant_de_configurer_le_journal(
    monkeypatch, config, tmp_path, capsys
):
    """Environnement inconnu : l'erreur de configuration arrête la commande avant tout travail."""
    journal = Journal()
    monkeypatch.setenv("FRAUDE_ENV", "staging")
    for nom in ("configurer_journal", "entrainer"):
        monkeypatch.setattr(
            entrainement, nom, lambda *a, _n=nom, **k: journal.appels.append((_n, a, k))
        )
    with pytest.raises(ErreurConfiguration) as exc:
        main([])
    assert str(exc.value) == "Environnement inconnu : 'staging'. Valeurs admises : dev, prod."
    assert journal.appels == []
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------------------------
# Refus de l'analyseur d'arguments (code de sortie 2) et aide
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(["--inconnu"], "unrecognized arguments: --inconnu", id="option_inconnue"),
        pytest.param(
            ["--limite", "abc"], "argument --limite: invalid int value: 'abc'", id="limite_texte"
        ),
        pytest.param(
            ["--limite", "1.5"],
            "argument --limite: invalid int value: '1.5'",
            id="limite_flottante",
        ),
        pytest.param(["--donnees"], "argument --donnees: expected one argument", id="sans_valeur"),
        pytest.param(
            ["--promouvoir=oui"],
            "argument --promouvoir: ignored explicit argument 'oui'",
            id="drapeau_avec_valeur",
        ),
    ],
)
def test_main_refuse_des_arguments_invalides_avec_le_code_de_sortie_2_sans_rien_lancer(
    monkeypatch, config, tmp_path, capsys, argv, message
):
    """Un argument invalide est refusé par l'analyseur (code 2, usage sur la sortie d'erreur) avant
    de charger la configuration ou de lancer quoi que ce soit."""
    journal = brancher(monkeypatch, config_cli(config, tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert journal.appels == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: ")
    assert captured.err.endswith(f": error: {message}\n")
    usage = " ".join(captured.err.split())  # l'usage peut passer à la ligne selon le terminal
    assert "[--donnees DONNEES] [--limite LIMITE] [--sans-mlflow] [--promouvoir]" in usage


def test_main_aide_decrit_chaque_option_et_sort_avec_le_code_0_sans_rien_lancer(
    monkeypatch, config, tmp_path, capsys
):
    """``--help`` décrit la commande et chaque option, puis quitte sans rien lancer."""
    monkeypatch.setenv("COLUMNS", "200")  # pas de retour à la ligne dans l'aide
    journal = brancher(monkeypatch, config_cli(config, tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert journal.appels == []
    captured = capsys.readouterr()
    assert captured.err == ""
    aide = " ".join(captured.out.split())
    for phrase in (
        "[-h] [--donnees DONNEES] [--limite LIMITE] [--sans-mlflow] [--promouvoir]",
        "Entraîne, exporte et promeut le modèle.",
        "--donnees DONNEES chemin du jeu (défaut : configuration)",
        "--limite LIMITE ne lire que les N premières lignes (essais)",
        "--sans-mlflow désactive le suivi MLflow",
        "--promouvoir après la barrière de qualité, met le candidat en service",
    ):
        assert phrase in aide, phrase
