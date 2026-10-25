# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Garanties du journal d'audit (critères 5.1 et 6.3) : ce qu'il efface, ce qu'il refuse d'écrire.

Défense en profondeur : même si un champ sensible arrivait jusqu'au journal, il serait remplacé
par ``[MASQUE]``, et toute suite de 13 à 19 chiffres isolée par ``[PAN_MASQUE]``. Les tests
ci-dessous fixent les bornes exactes de cette règle (12 chiffres intacts, 13 et 19 effacés,
20 intacts, collé à une lettre ou à un point : intact) et prouvent que le journal ne modifie
jamais ce qu'on lui confie.

Le journal ``fraude`` est global : chaque test qui le reconfigure passe par la fixture
``racine_fraude``, qui retire les gestionnaires existants puis rétablit l'état initial.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import sys
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest

from detection_fraude.journal import (
    FormatJson,
    FormatLisible,
    avertissement,
    configurer_journal,
    console_utf8,
    erreur,
    info,
    journaliser,
    nettoyer,
    obtenir_journal,
)

CLES_SENSIBLES = (
    "cc_num",
    "first",
    "last",
    "street",
    "dob",
    "city",
    "zip",
    "lat",
    "long",
    "job",
    "gender",
)
PAN = "4929123412341234"
CREATION = 1_700_000_000.123456  # 2023-11-14 22:13:20.123 UTC


class FluxConsole:
    """Faux flux de console : garde ce qu'on écrit et les reconfigurations demandées."""

    def __init__(self) -> None:
        self.ecritures: list[str] = []
        self.options: list[dict[str, Any]] = []

    def reconfigure(self, **options: Any) -> None:
        self.options.append(options)

    def write(self, texte: str) -> int:
        self.ecritures.append(texte)
        return len(texte)

    def flush(self) -> None:
        return None


class Collecteur(logging.Handler):
    """Garde les enregistrements reçus (le journal testé n'a pas d'autre gestionnaire)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.enregistrements: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.enregistrements.append(record)


@pytest.fixture
def racine_fraude():
    """Le journal ``fraude`` vidé de ses gestionnaires, puis remis tel qu'il était."""
    racine = logging.getLogger("fraude")
    anciens = list(racine.handlers)
    niveau, propage = racine.level, racine.propagate
    for gestionnaire in anciens:
        racine.removeHandler(gestionnaire)
    try:
        yield racine
    finally:
        for gestionnaire in list(racine.handlers):
            racine.removeHandler(gestionnaire)
            gestionnaire.close()
        for gestionnaire in anciens:
            racine.addHandler(gestionnaire)
        racine.setLevel(niveau)
        racine.propagate = propage


def installer_consoles(monkeypatch: pytest.MonkeyPatch) -> tuple[FluxConsole, FluxConsole]:
    """Remplace ``sys.stdout`` et ``sys.stderr`` par de faux flux observables.

    À appeler dans le corps du test : pytest réinstalle ses propres flux au début de l'appel.
    """
    sortie, erreurs = FluxConsole(), FluxConsole()
    monkeypatch.setattr(sys, "stdout", sortie)
    monkeypatch.setattr(sys, "stderr", erreurs)
    return sortie, erreurs


def enregistrement(
    nom: str = "fraude.consommateur",
    niveau: int = logging.INFO,
    message: str = "evt",
    *,
    champs: dict[str, Any] | None = None,
    exc_info: Any = None,
    creation: float = CREATION,
    args: Any = None,
) -> logging.LogRecord:
    """Un enregistrement de journal à l'instant fixé ``creation``."""
    record = logging.LogRecord(nom, niveau, "fichier.py", 1, message, args, exc_info)
    record.created = creation
    if champs is not None:
        record.champs = champs
    return record


def exc_info_avec_carte() -> Any:
    try:
        raise ValueError(f"carte {PAN} refusée")
    except ValueError:
        return sys.exc_info()


def journal_isole(niveau: int = logging.DEBUG) -> tuple[logging.Logger, Collecteur]:
    """Un journal hors hiérarchie : son seul gestionnaire est le collecteur."""
    journal = logging.Logger("fraude.isole", level=niveau)
    collecteur = Collecteur()
    journal.addHandler(collecteur)
    return journal, collecteur


# --------------------------------------------------------------------------------------------
# nettoyer : clés sensibles
# --------------------------------------------------------------------------------------------
def test_chaque_cle_sensible_est_masquee_quelle_que_soit_la_valeur_et_l_entree_reste_inchangee():
    """Les onze noms de champs identifiants sont masqués, même vides, imbriqués ou numériques."""
    valeurs = [None, 0, "texte", 4929123412341234, [1, 2], {"a": 1}, "", False]
    for cle in CLES_SENSIBLES:
        for valeur in valeurs:
            entree = {cle: valeur, "montant": 12.5}
            entree_avant = copy.deepcopy(entree)
            assert nettoyer(entree) == {cle: "[MASQUE]", "montant": 12.5}
            assert entree == entree_avant
            assert nettoyer(valeur, cle) == "[MASQUE]"


def test_une_cle_non_sensible_laisse_sa_valeur_intacte():
    """Montant, identifiant, marchand, empreinte... ne sont pas des données identifiantes."""
    entree = {
        "montant": 12.5,
        "trans_num": "c7e1d0a94f3b42e8a65d1f0b9e2c7a14",
        "commercant": "fraud_Marchand 3",
        "carte_hash": "0123456789abcdef0123456789abcdef",
        "carte_fin": "1234",
        "age": 41,
        "etat": "NY",
        "lot": None,
        "vrai": True,
    }
    entree_avant = copy.deepcopy(entree)
    assert nettoyer(entree) == entree_avant
    assert entree == entree_avant
    for cle, valeur in entree.items():
        assert nettoyer(valeur, cle) == valeur


def test_nettoyer_descend_dans_les_dictionnaires_et_les_listes_sans_modifier_l_entree():
    """Une clé sensible est masquée à toute profondeur ; les éléments d'une liste, non."""
    entree = {
        "a": {"first": "Jean", "liste": [{"street": "1 rue X"}, "ok", PAN, ("t", {"job": "x"})]},
        "first": ["a", "b"],
        5: "cinq",
    }
    entree_avant = copy.deepcopy(entree)
    assert nettoyer(entree) == {
        "a": {
            "first": "[MASQUE]",
            "liste": [{"street": "[MASQUE]"}, "ok", "[PAN_MASQUE]", ["t", {"job": "[MASQUE]"}]],
        },
        "first": "[MASQUE]",
        "5": "cinq",
    }
    assert entree == entree_avant


def test_nettoyer_rend_une_nouvelle_liste_pour_un_tuple_et_laisse_la_liste_d_origine_inchangee():
    """Un tuple devient une liste nettoyée ; la liste d'origine n'est pas modifiée sur place."""
    origine = [PAN, ["x", PAN], "ok"]
    origine_avant = copy.deepcopy(origine)
    propre = nettoyer(origine)
    assert propre == ["[PAN_MASQUE]", ["x", "[PAN_MASQUE]"], "ok"]
    assert propre is not origine
    assert origine == origine_avant
    assert nettoyer((PAN, "ok")) == ["[PAN_MASQUE]", "ok"]
    assert nettoyer(()) == []


def test_les_valeurs_non_textuelles_et_petites_restent_intactes():
    """Nombres courts, booléens, vide : aucune raison d'y toucher."""
    for valeur in (0, 42, -7, 12.5, True, False, None, Decimal("1.5")):
        assert nettoyer(valeur) is valeur
        assert nettoyer({"k": valeur}) == {"k": valeur}


# --------------------------------------------------------------------------------------------
# nettoyer : motif de numéro de carte
# --------------------------------------------------------------------------------------------
def test_douze_chiffres_restent_intacts():
    """Douze chiffres (par exemple un identifiant court) ne sont pas pris pour une carte."""
    for texte in ("ref 123456789012 ok", "123456789012", "a 000000000000 b"):
        texte_avant = texte
        assert nettoyer(texte) == texte_avant


def test_vingt_chiffres_et_plus_restent_intacts():
    """Au-delà de 19 chiffres, ce n'est plus un numéro de carte : le texte reste inchangé."""
    for texte in ("ref 12345678901234567890 ok", "1" * 32, "9" * 25):
        texte_avant = texte
        assert nettoyer(texte) == texte_avant


def test_treize_et_dix_neuf_chiffres_isoles_sont_effaces_et_le_texte_autour_reste_intact():
    """Les bornes 13 et 19 sont incluses ; tout ce qui entoure le numéro n'est pas touché."""
    assert nettoyer("ref 1234567890123 ok") == "ref [PAN_MASQUE] ok"
    assert nettoyer("ref 1234567890123456789 ok") == "ref [PAN_MASQUE] ok"
    assert nettoyer("ref 12345678901234 ok") == "ref [PAN_MASQUE] ok"
    assert nettoyer("ref 123456789012345678 ok") == "ref [PAN_MASQUE] ok"
    assert nettoyer(PAN) == "[PAN_MASQUE]"
    assert nettoyer(f"début {PAN} fin") == "début [PAN_MASQUE] fin"
    assert nettoyer(f"({PAN})") == "([PAN_MASQUE])"
    assert nettoyer(f"carte:{PAN},{PAN}") == "carte:[PAN_MASQUE],[PAN_MASQUE]"
    assert nettoyer(f"x={PAN};-{PAN}") == "x=[PAN_MASQUE];-[PAN_MASQUE]"


def test_un_nombre_colle_a_des_lettres_ou_a_un_tiret_bas_reste_intact():
    """Un identifiant alphanumérique n'est pas une carte : collé à une lettre, rien ne change."""
    for texte in (f"x{PAN}", f"{PAN}x", f"_{PAN}", f"{PAN}_", "c7e1d0a94f3b42e8a65d1f0b9e2c7a14"):
        texte_avant = texte
        assert nettoyer(texte) == texte_avant


def test_un_nombre_colle_a_un_point_reste_intact():
    """Un nombre décimal ou pointé (version, mesure) n'est pas une carte : texte inchangé."""
    for texte in (f"{PAN}.5", f"0.{PAN}", f"v.{PAN}", "1234567890123.45"):
        texte_avant = texte
        assert nettoyer(texte) == texte_avant


# --------------------------------------------------------------------------------------------
# FormatJson
# --------------------------------------------------------------------------------------------
def test_le_format_json_rend_la_ligne_exacte_et_ne_modifie_pas_l_enregistrement():
    """Quatre champs standard dans l'ordre, puis les champs de l'événement ; UTF-8 conservé."""
    sans_champs = enregistrement()
    sans_champs_avant = dict(vars(sans_champs))
    assert FormatJson().format(sans_champs) == (
        '{"horodatage": "2023-11-14T22:13:20.123+00:00", "niveau": "INFO", '
        '"composant": "consommateur", "evenement": "evt"}'
    )
    assert vars(sans_champs) == sans_champs_avant
    champs = {"lot": "abc", "n": 3, "montant": Decimal("12.50"), "libelle": "refusée"}
    champs["vide"] = None
    avec_champs = enregistrement(niveau=logging.WARNING, champs=champs)
    avec_champs_avant = dict(vars(avec_champs))
    assert FormatJson().format(avec_champs) == (
        '{"horodatage": "2023-11-14T22:13:20.123+00:00", "niveau": "WARNING", '
        '"composant": "consommateur", "evenement": "evt", "lot": "abc", "n": 3, '
        '"montant": "12.50", "libelle": "refusée", "vide": null}'
    )
    assert vars(avec_champs) == avec_champs_avant


def test_le_format_json_nomme_le_composant_sans_le_prefixe_fraude_et_formate_le_message():
    """Seul le préfixe « fraude. » est retiré ; le message est formaté avec ses arguments."""
    attendus = {
        "fraude.consommateur": "consommateur",
        "fraude.a.b": "a.b",
        "fraude": "fraude",
        "autre.fraude.x": "autre.fraude.x",
        "xfraude.x": "xfraude.x",
    }
    for nom, composant in attendus.items():
        record = enregistrement(nom=nom)
        record_avant = dict(vars(record))
        assert json.loads(FormatJson().format(record))["composant"] == composant
        assert vars(record) == record_avant
    formate = enregistrement(message="lot %s sur %d", args=("abc", 3))
    formate_avant = dict(vars(formate))
    assert json.loads(FormatJson().format(formate))["evenement"] == "lot abc sur 3"
    assert vars(formate) == formate_avant
    for niveau, nom_du_niveau in (
        (logging.DEBUG, "DEBUG"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
    ):
        sortie = json.loads(FormatJson().format(enregistrement(niveau=niveau)))
        assert sortie["niveau"] == nom_du_niveau


def test_le_format_json_horodate_en_utc_a_la_milliseconde_en_tronquant():
    """L'horodatage est ISO 8601 UTC avec trois décimales, tronqué et non arrondi."""
    attendus = {
        0.0: "1970-01-01T00:00:00.000+00:00",
        1_700_000_000.0: "2023-11-14T22:13:20.000+00:00",
        1_700_000_000.9999: "2023-11-14T22:13:20.999+00:00",
        1_700_000_000.123456: "2023-11-14T22:13:20.123+00:00",
        1_700_003_599.5: "2023-11-14T23:13:19.500+00:00",
    }
    for creation, attendu in attendus.items():
        record = enregistrement(creation=creation)
        record_avant = dict(vars(record))
        assert json.loads(FormatJson().format(record))["horodatage"] == attendu
        assert vars(record) == record_avant


def test_le_format_json_efface_la_carte_de_l_exception_et_n_ajoute_rien_sans_exception():
    """La trace d'une exception est nettoyée ; sans exception, aucune clé « exception »."""
    avec = enregistrement(niveau=logging.ERROR, exc_info=exc_info_avec_carte())
    avec_avant = dict(vars(avec))
    brut = FormatJson().format(avec)
    assert PAN not in brut
    trace = json.loads(brut)["exception"]
    lignes = trace.splitlines()
    assert lignes[0] == "Traceback (most recent call last):"
    assert lignes[-1] == "ValueError: carte [PAN_MASQUE] refusée"
    assert "refusée" in brut and "\\u00e9" not in brut
    assert vars(avec) == avec_avant
    sans = json.loads(FormatJson().format(enregistrement()))
    assert list(sans) == ["horodatage", "niveau", "composant", "evenement"]


# --------------------------------------------------------------------------------------------
# FormatLisible
# --------------------------------------------------------------------------------------------
def test_le_format_lisible_rend_la_ligne_exacte_et_ne_modifie_pas_l_enregistrement():
    """Heure UTC, niveau sur 7 colonnes, composant sur 12, message, puis champs « clé=valeur »."""
    cas = [
        (enregistrement(), "22:13:20 INFO    consommateur evt"),
        (
            enregistrement(champs={"lot": "abc", "n": 3, "vide": None}),
            "22:13:20 INFO    consommateur evt lot=abc n=3 vide=None",
        ),
        (
            enregistrement(nom="fraude.t", niveau=logging.WARNING, champs={"a": 1}),
            "22:13:20 WARNING t            evt a=1",
        ),
        (
            enregistrement(nom="fraude", niveau=logging.CRITICAL),
            "22:13:20 CRITICAL fraude       evt",
        ),
        (
            enregistrement(nom="fraude.un_nom_tres_long", niveau=logging.ERROR),
            "22:13:20 ERROR   un_nom_tres_long evt",
        ),
        (enregistrement(creation=1_700_003_599.9999), "23:13:19 INFO    consommateur evt"),
        (enregistrement(creation=0.0), "00:00:00 INFO    consommateur evt"),
        (
            enregistrement(message="lot %s", args=("abc",), champs={}),
            "22:13:20 INFO    consommateur lot abc",
        ),
    ]
    for record, attendu in cas:
        record_avant = dict(vars(record))
        assert FormatLisible().format(record) == attendu
        assert vars(record) == record_avant


def test_le_format_lisible_ajoute_la_trace_nettoyee_apres_un_saut_de_ligne():
    """La trace d'une exception suit la ligne, sans numéro de carte ; sans exception, rien."""
    record = enregistrement(exc_info=exc_info_avec_carte())
    record_avant = dict(vars(record))
    texte = FormatLisible().format(record)
    assert PAN not in texte
    assert texte.startswith(
        "22:13:20 INFO    consommateur evt \nTraceback (most recent call last):\n"
    )
    assert texte.endswith("\nValueError: carte [PAN_MASQUE] refusée")
    assert vars(record) == record_avant
    assert "Traceback" not in FormatLisible().format(enregistrement())


# --------------------------------------------------------------------------------------------
# console_utf8
# --------------------------------------------------------------------------------------------
def test_console_utf8_reconfigure_les_flux_qui_le_permettent_et_laisse_intacts_les_autres(
    monkeypatch,
):
    """Sortie et erreur passent en UTF-8 (remplacement des octets invalides) ; sinon, rien."""
    sortie, erreurs = FluxConsole(), FluxConsole()
    monkeypatch.setattr(sys, "stdout", sortie)
    monkeypatch.setattr(sys, "stderr", erreurs)
    console_utf8()
    assert sortie.options == [{"encoding": "utf-8", "errors": "replace"}]
    assert erreurs.options == [{"encoding": "utf-8", "errors": "replace"}]
    texte_sortie, texte_erreurs = io.StringIO("sortie"), io.StringIO("erreurs")
    monkeypatch.setattr(sys, "stdout", texte_sortie)
    monkeypatch.setattr(sys, "stderr", texte_erreurs)
    contenu_avant = (texte_sortie.getvalue(), texte_erreurs.getvalue())
    console_utf8()
    assert (texte_sortie.getvalue(), texte_erreurs.getvalue()) == contenu_avant
    assert sortie.options == [{"encoding": "utf-8", "errors": "replace"}]


# --------------------------------------------------------------------------------------------
# configurer_journal
# --------------------------------------------------------------------------------------------
def test_configurer_journal_installe_deux_sorties_est_idempotent_et_refuse_un_niveau_inconnu(
    racine_fraude, tmp_path, monkeypatch
):
    """Console + archive tournante ; reconfigurer remplace sans doublon ; niveau inconnu refusé."""
    sortie, erreurs = installer_consoles(monkeypatch)
    fichier = tmp_path / "a" / "b" / "audit.jsonl"
    configurer_journal("debug", "json", fichier)
    assert sortie.options == [{"encoding": "utf-8", "errors": "replace"}]
    assert erreurs.options == [{"encoding": "utf-8", "errors": "replace"}]
    assert racine_fraude.level == logging.DEBUG
    assert racine_fraude.propagate is False
    premiers = list(racine_fraude.handlers)
    assert [type(g) for g in premiers] == [logging.StreamHandler, RotatingFileHandler]
    console, archive = premiers
    assert console.stream is erreurs
    assert type(console.formatter) is FormatJson
    assert type(archive.formatter) is FormatJson
    assert archive.maxBytes == 5_000_000
    assert archive.backupCount == 5
    assert archive.encoding == "utf-8"
    assert archive.baseFilename == str(fichier.resolve())
    assert fichier.parent.is_dir()
    # Reconfigurer : mêmes sorties, anciens gestionnaires retirés et fermés, aucun doublon.
    configurer_journal("INFO", "lisible", fichier)
    seconds = list(racine_fraude.handlers)
    assert [type(g) for g in seconds] == [logging.StreamHandler, RotatingFileHandler]
    assert all(neuf is not ancien for neuf in seconds for ancien in premiers)
    assert archive.stream is None
    assert type(seconds[0].formatter) is FormatLisible
    assert racine_fraude.level == logging.INFO
    info(obtenir_journal("idem"), "evenement_unique")
    assert len(erreurs.ecritures) == 1
    lignes = fichier.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ligne)["evenement"] for ligne in lignes] == ["evenement_unique"]
    # Un niveau inconnu est refusé avant de toucher aux gestionnaires installés.
    gestionnaires_avant = list(racine_fraude.handlers)
    niveau_avant = racine_fraude.level
    with pytest.raises(ValueError) as refus:
        configurer_journal("TRACE", "json", fichier)
    assert str(refus.value) == "Unknown level: 'TRACE'"
    assert racine_fraude.handlers == gestionnaires_avant
    assert racine_fraude.level == niveau_avant
    assert seconds[1].stream is not None


def test_sans_fichier_la_configuration_par_defaut_est_lisible_et_ne_cree_rien_sur_disque(
    racine_fraude, tmp_path, monkeypatch
):
    """Par défaut : niveau INFO, console lisible, aucun fichier créé (état du dossier inchangé)."""
    sortie, erreurs = installer_consoles(monkeypatch)
    monkeypatch.chdir(tmp_path)
    dossier_avant = sorted(os.listdir(tmp_path))
    configurer_journal()
    assert [type(g) for g in racine_fraude.handlers] == [logging.StreamHandler]
    assert racine_fraude.handlers[0].stream is erreurs
    assert type(racine_fraude.handlers[0].formatter) is FormatLisible
    assert racine_fraude.level == logging.INFO
    assert racine_fraude.propagate is False
    journal = obtenir_journal("defaut")
    ecritures_avant = list(erreurs.ecritures)
    journal.debug("trop_bavard")
    assert erreurs.ecritures == ecritures_avant
    info(journal, "lot_traite", lot="abc")
    assert erreurs.ecritures[-1].endswith(" INFO    defaut       lot_traite lot=abc\n")
    assert sortie.ecritures == []
    assert sorted(os.listdir(tmp_path)) == dossier_avant


def test_un_format_console_autre_que_json_est_lisible_et_json_ecrit_du_json(
    racine_fraude, monkeypatch
):
    """« json » donne une ligne JSON sur la console ; toute autre valeur, le format lisible."""
    _, erreurs = installer_consoles(monkeypatch)
    configurer_journal("INFO", "json")
    info(obtenir_journal("c"), "evt_json", lot="abc")
    sortie_json = json.loads(erreurs.ecritures[-1])
    assert (sortie_json["evenement"], sortie_json["composant"], sortie_json["lot"]) == (
        "evt_json",
        "c",
        "abc",
    )
    configurer_journal("INFO", "texte")
    assert type(racine_fraude.handlers[0].formatter) is FormatLisible
    ecritures_avant = list(erreurs.ecritures)
    assert ecritures_avant[-1].startswith("{")
    info(obtenir_journal("c"), "evt_lisible")
    assert erreurs.ecritures[: len(ecritures_avant)] == ecritures_avant
    nouvelle = erreurs.ecritures[len(ecritures_avant) :][0]
    assert nouvelle.endswith(" INFO    c            evt_lisible\n")


def test_le_niveau_filtre_l_archive_quelle_que_soit_la_casse_et_le_fichier_reste_inchange(
    racine_fraude, tmp_path, monkeypatch
):
    """Avec « warning », « Warning » ou « WARNING », une information n'est pas archivée."""
    installer_consoles(monkeypatch)
    for rang, niveau in enumerate(("warning", "Warning", "WARNING")):
        fichier = tmp_path / f"audit_{rang}.jsonl"
        configurer_journal(niveau, "json", fichier)
        assert racine_fraude.level == logging.WARNING
        journal = obtenir_journal("filtre")
        contenu_avant = fichier.read_text(encoding="utf-8")
        info(journal, "ignore")
        assert fichier.read_text(encoding="utf-8") == contenu_avant
        avertissement(journal, "retenu", cc_num=4929123412341234, lot="abc")
        erreur(journal, "retenu_aussi")
        evenements = [
            json.loads(ligne) for ligne in fichier.read_text(encoding="utf-8").splitlines()
        ]
        assert [(e["niveau"], e["evenement"]) for e in evenements] == [
            ("WARNING", "retenu"),
            ("ERROR", "retenu_aussi"),
        ]
        assert evenements[0]["cc_num"] == "[MASQUE]"
        assert evenements[0]["lot"] == "abc"


def test_les_evenements_du_journal_fraude_n_atteignent_pas_le_journal_racine(
    racine_fraude, monkeypatch
):
    """Le journal d'audit ne se propage pas : le journal racine de l'application reste vide."""
    installer_consoles(monkeypatch)
    configurer_journal("INFO", "json")
    racine_python = logging.getLogger()
    collecteur = Collecteur()
    racine_python.addHandler(collecteur)
    try:
        enregistrements_avant = list(collecteur.enregistrements)
        info(obtenir_journal("propagation"), "evt")
        assert collecteur.enregistrements == enregistrements_avant
    finally:
        racine_python.removeHandler(collecteur)
    assert racine_fraude.propagate is False


# --------------------------------------------------------------------------------------------
# obtenir_journal, journaliser, info, avertissement, erreur
# --------------------------------------------------------------------------------------------
def test_obtenir_journal_rend_un_journal_enfant_de_fraude_toujours_le_meme(racine_fraude):
    """Chaque composant a son journal ``fraude.<composant>`` ; l'obtenir ne change rien."""
    gestionnaires_avant = list(racine_fraude.handlers)
    niveau_avant = racine_fraude.level
    journal = obtenir_journal("consommateur")
    assert journal.name == "fraude.consommateur"
    assert journal is logging.getLogger("fraude.consommateur")
    assert journal is obtenir_journal("consommateur")
    assert journal.parent is racine_fraude
    assert obtenir_journal("autre") is not journal
    assert obtenir_journal("a.b").name == "fraude.a.b"
    assert racine_fraude.handlers == gestionnaires_avant
    assert racine_fraude.level == niveau_avant


def test_un_journal_dont_le_niveau_est_desactive_n_ecrit_rien_et_reste_inchange():
    """Sous le seuil du journal, l'événement n'est pas émis : aucun enregistrement ajouté."""
    journal, collecteur = journal_isole(logging.WARNING)
    enregistrements_avant = list(collecteur.enregistrements)
    info(journal, "ignore", lot="abc")
    journaliser(journal, logging.DEBUG, "ignore")
    journaliser(journal, logging.INFO, "ignore_aussi")
    assert collecteur.enregistrements == enregistrements_avant
    avertissement(journal, "limite")
    erreur(journal, "au_dessus")
    assert [r.getMessage() for r in collecteur.enregistrements] == ["limite", "au_dessus"]
    journal_erreurs, collecteur_erreurs = journal_isole(logging.ERROR)
    avertissement(journal_erreurs, "ignore")
    assert collecteur_erreurs.enregistrements == []
    erreur(journal_erreurs, "retenu")
    assert [r.getMessage() for r in collecteur_erreurs.enregistrements] == ["retenu"]


def test_les_champs_sont_nettoyes_avant_l_ecriture_et_les_donnees_passees_restent_inchangees():
    """Champ sensible et numéro de carte n'atteignent pas l'enregistrement ; entrée intacte."""
    journal, collecteur = journal_isole()
    donnees = {"first": "Jean", "liste": [{"street": "1 rue X"}, PAN]}
    donnees_avant = copy.deepcopy(donnees)
    note = f"carte {PAN}"
    info(journal, "evt", donnees=donnees, cc_num=4929123412341234, note=note, duree_ms=12)
    (record,) = collecteur.enregistrements
    assert vars(record)["champs"] == {
        "donnees": {"first": "[MASQUE]", "liste": [{"street": "[MASQUE]"}, "[PAN_MASQUE]"]},
        "cc_num": "[MASQUE]",
        "note": "carte [PAN_MASQUE]",
        "duree_ms": 12,
    }
    assert donnees == donnees_avant


def test_info_avertissement_et_erreur_emettent_le_bon_niveau_le_bon_nom_et_les_champs():
    """Chaque fonction d'aide fixe son niveau ; l'événement est le message, sans mise en forme."""
    journal, collecteur = journal_isole()
    contexte = {"lot": "def", "n": 2}
    contexte_avant = dict(contexte)
    info(journal, "taux 100% %s", lot="abc")
    avertissement(journal, "avertissement_evt", **contexte)
    erreur(journal, "erreur_evt")
    journaliser(journal, logging.DEBUG, "debug_evt", evenement="autre")
    resume = [
        (r.levelno, r.levelname, r.getMessage(), vars(r)["champs"], r.name)
        for r in collecteur.enregistrements
    ]
    assert resume == [
        (logging.INFO, "INFO", "taux 100% %s", {"lot": "abc"}, "fraude.isole"),
        (logging.WARNING, "WARNING", "avertissement_evt", {"lot": "def", "n": 2}, "fraude.isole"),
        (logging.ERROR, "ERROR", "erreur_evt", {}, "fraude.isole"),
        (logging.DEBUG, "DEBUG", "debug_evt", {"evenement": "autre"}, "fraude.isole"),
    ]
    assert contexte == contexte_avant


def test_un_journal_deja_configure_ecrit_chaque_evenement_une_seule_fois_dans_le_fichier(
    racine_fraude, tmp_path, monkeypatch
):
    """Configurer trois fois de suite ne multiplie pas les lignes : l'audit reste sans doublon."""
    _, erreurs = installer_consoles(monkeypatch)
    fichier = tmp_path / "audit.jsonl"
    ecritures_avant = list(erreurs.ecritures)
    for _ in range(3):
        configurer_journal("INFO", "json", Path(fichier))
    assert erreurs.ecritures == ecritures_avant
    assert fichier.read_text(encoding="utf-8") == ""
    info(obtenir_journal("unique"), "un_seul")
    lignes = fichier.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ligne)["evenement"] for ligne in lignes] == ["un_seul"]
    assert [json.loads(ecriture)["evenement"] for ecriture in erreurs.ecritures] == ["un_seul"]
