# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tout fichier texte du pipeline est lu et écrit en UTF-8 explicite, quel que soit le système.

Pourquoi ces tests existent : sous Windows, l'encodage par défaut d'``open`` est cp1252 ; sous
Linux, UTF-8. Un appel qui oublierait ``encoding="utf-8"`` fonctionnerait sur un poste et
corromprait les accents sur l'autre (noms de commerçants, sujets de notification, rapports). Les
tests n'attendent pas d'être sur la mauvaise plate-forme : ils enregistrent l'encodage RÉELLEMENT
demandé à chaque ouverture de fichier du projet et exigent la chaîne exacte ``utf-8``.

Chaque test vérifie aussi que le contenu non ASCII fait l'aller-retour intact.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from detection_fraude import modele as module_modele
from detection_fraude import producteur as module_producteur
from detection_fraude import rapport as module_rapport
from detection_fraude import supervision as module_supervision
from detection_fraude.configuration import RACINE_PROJET, charger_configuration, lire_secret
from detection_fraude.consommateur import Consommateur
from detection_fraude.file_attente import FileSQLite
from detection_fraude.notification import CanalFichier, Notificateur, Notification
from tests.helpers import ChargeurFactice

ACCENTS = "Générale d'été — çà et là, œuvre ñ"


class Ouvertures:
    """Journal des ouvertures de fichiers texte passées par ``Path`` ou ``os.fdopen``."""

    def __init__(self) -> None:
        self.appels: list[tuple[str, str, Any, Any]] = []

    def de(self, nom_fichier: str) -> list[tuple[str, Any, Any]]:
        """``(méthode, encodage, retour à la ligne)`` de chaque ouverture du fichier ``nom``."""
        return [(m, e, n) for m, f, e, n in self.appels if f == nom_fichier]


@pytest.fixture
def ouvertures(monkeypatch: pytest.MonkeyPatch) -> Ouvertures:
    """Enregistre l'encodage demandé à chaque lecture ou écriture de texte, puis délègue."""
    journal = Ouvertures()
    vrai_lire, vrai_ecrire, vrai_ouvrir = Path.read_text, Path.write_text, Path.open
    vrai_fdopen = os.fdopen
    profondeur = [0]  # read_text et write_text appellent open : on ne compte que l'appel externe

    def lire(self: Path, encoding: Any = None, errors: Any = None) -> str:
        journal.appels.append(("read_text", self.name, encoding, None))
        profondeur[0] += 1
        try:
            return vrai_lire(self, encoding=encoding, errors=errors)
        finally:
            profondeur[0] -= 1

    def ecrire(
        self: Path, data: str, encoding: Any = None, errors: Any = None, newline: Any = None
    ) -> int:
        journal.appels.append(("write_text", self.name, encoding, newline))
        profondeur[0] += 1
        try:
            return vrai_ecrire(self, data, encoding=encoding, errors=errors, newline=newline)
        finally:
            profondeur[0] -= 1

    def ouvrir(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: Any = None,
        errors: Any = None,
        newline: Any = None,
    ) -> Any:
        if "b" not in mode and profondeur[0] == 0:
            journal.appels.append((f"open:{mode}", self.name, encoding, newline))
        return vrai_ouvrir(self, mode, buffering, encoding, errors, newline)

    def fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        journal.appels.append((f"fdopen:{mode}", "<descripteur>", kwargs.get("encoding"), None))
        return vrai_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", lire)
    monkeypatch.setattr(Path, "write_text", ecrire)
    monkeypatch.setattr(Path, "open", ouvrir)
    monkeypatch.setattr(os, "fdopen", fdopen)
    return journal


def test_un_secret_lu_dans_un_fichier_est_decode_en_utf8(tmp_path, ouvertures):
    """Un secret monté par Docker peut contenir des accents : il est lu en UTF-8."""
    fichier = tmp_path / "secret_accents.txt"
    fichier.write_bytes((ACCENTS + "\n").encode("utf-8"))
    valeur = lire_secret("SEL", environ={"FRAUDE_SEL_FICHIER": str(fichier)})
    assert valeur == ACCENTS
    assert ouvertures.de("secret_accents.txt") == [("read_text", "utf-8", None)]


def test_le_sel_de_developpement_est_ecrit_puis_relu_en_utf8(config, monkeypatch, ouvertures):
    """Le sel local généré au premier lancement est écrit et relu avec le même encodage."""
    monkeypatch.delenv("FRAUDE_SEL_HACHAGE")  # sans secret fourni, le sel local est généré
    sel = config.sel_hachage()
    assert len(sel) > 0
    assert ouvertures.de(".sel_hachage_dev") == [
        ("write_text", "utf-8", None),
        ("read_text", "utf-8", None),
    ]


def test_les_fichiers_de_configuration_sont_lus_en_utf8(tmp_path, ouvertures):
    """Un commentaire ou une valeur accentuée du YAML ne doit pas dépendre de la plate-forme."""
    dossier = tmp_path / "cfg"
    dossier.mkdir()
    (dossier / "base.yaml").write_bytes(f"journal:\n  niveau: warning  # {ACCENTS}\n".encode())
    (dossier / "dev.yaml").write_bytes(b"file:\n  type: sqlite\n")
    configuration = charger_configuration("dev", dossier_config=dossier, environ={})
    assert configuration.journal.niveau == "WARNING"
    assert ouvertures.de("base.yaml") == [("read_text", "utf-8", None)]
    assert ouvertures.de("dev.yaml") == [("read_text", "utf-8", None)]


def test_l_etat_de_sante_du_producteur_est_ecrit_en_utf8(config, ouvertures):
    module_producteur.ecrire_etat(config, {"cause": ACCENTS})
    chemin = config.racine_donnees / "etat" / "producteur.json"
    assert json.loads(chemin.read_bytes().decode("utf-8")) == {"cause": ACCENTS}
    assert ouvertures.de("producteur.json") == [("write_text", "utf-8", None)]


def test_l_etat_de_sante_du_consommateur_est_ecrit_en_utf8(config, entrepot, ouvertures):
    file = FileSQLite(config.file.chemin_sqlite)
    try:
        consommateur = Consommateur(config, file, entrepot, ChargeurFactice(), Notificateur([]))
        consommateur.executer_une_fois(attente_s=0)
    finally:
        file.fermer()
    chemin = config.racine_donnees / "etat" / "consommateur.json"
    assert json.loads(chemin.read_bytes().decode("utf-8"))["modele"] == "factice-1"
    assert ouvertures.de("consommateur.json") == [("write_text", "utf-8", None)]


def test_les_metadonnees_et_le_pointeur_du_modele_sont_en_utf8(tmp_path, ouvertures):
    """Pointeur ``ACTUEL`` et métadonnées sont écrits (fdopen) et relus en UTF-8."""
    from sklearn.dummy import DummyClassifier

    pipeline = DummyClassifier().fit([[0], [1]], [0, 1])
    module_modele.exporter_modele(tmp_path, pipeline, 0.4, {"version": "v-é", "note": ACCENTS})
    module_modele.promouvoir(tmp_path, "v-é")
    assert module_modele.version_en_service(tmp_path) == "v-é"
    assert module_modele.lire_metadonnees(tmp_path, "v-é")["note"] == ACCENTS
    ecritures = [(m, e) for m, f, e, _ in ouvertures.appels if m.startswith("fdopen")]
    assert ecritures == [("fdopen:w", "utf-8"), ("fdopen:w", "utf-8")]
    assert ouvertures.de("ACTUEL") == [("read_text", "utf-8", None)]
    assert ouvertures.de("metadonnees.json") == [("read_text", "utf-8", None)]


def test_le_canal_fichier_ajoute_ses_lignes_en_utf8(tmp_path, ouvertures):
    chemin = tmp_path / "alertes.jsonl"
    CanalFichier(chemin).envoyer(Notification("fraude", "sujet é", ACCENTS, {}))
    assert ACCENTS in chemin.read_bytes().decode("utf-8")
    assert ouvertures.de("alertes.jsonl") == [("open:a", "utf-8", None)]


def test_le_csv_du_rapport_porte_un_bom_utf8_pour_excel_et_garde_ses_retours_a_la_ligne(
    tmp_path, ouvertures
):
    chemin = tmp_path / "fraudes.csv"
    module_rapport.ecrire_csv({"fraudes": []}, chemin)
    assert chemin.read_bytes().startswith(b"\xef\xbb\xbf")
    assert ouvertures.de("fraudes.csv") == [("open:w", "utf-8-sig", "")]


def test_la_page_html_du_rapport_est_ecrite_en_utf8(config, entrepot, ouvertures):
    rapport = module_rapport.calculer_rapport(entrepot, "2026-10-09")
    chemins = module_rapport.ecrire_rapport(config, entrepot, rapport)
    assert "Rapport du matin" in Path(chemins["html"]).read_bytes().decode("utf-8")
    assert ouvertures.de("rapport_2026-10-09.html") == [("write_text", "utf-8", None)]
    assert ouvertures.de("fraudes_2026-10-09.csv") == [("open:w", "utf-8-sig", "")]


def test_la_supervision_lit_l_etat_du_consommateur_en_utf8(config, entrepot, ouvertures):
    config.modele.dossier.mkdir(parents=True, exist_ok=True)
    (config.modele.dossier / "ACTUEL").write_bytes(b"v1\n")
    etat = config.racine_donnees / "etat" / "consommateur.json"
    etat.parent.mkdir(parents=True, exist_ok=True)
    etat.write_bytes(json.dumps({"modele": "v-é"}, ensure_ascii=False).encode("utf-8"))
    constats = module_supervision.controler(
        entrepot, config, datetime.now(UTC), surveiller_flux=False
    )
    assert "modele_non_applique" not in {c.code for c in constats}  # pointeur écrit à l'instant
    assert ouvertures.de("consommateur.json") == [("read_text", "utf-8", None)]


def test_le_script_de_secrets_ecrit_ses_fichiers_en_utf8(tmp_path, ouvertures):
    chemin_script = RACINE_PROJET / "scripts" / "initialiser_secrets.py"
    # Nom de chemin du module : l'outil de mutation relie ainsi ses mutants à ce test.
    spec = importlib.util.spec_from_file_location("scripts.initialiser_secrets", chemin_script)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    script.DOSSIER = tmp_path
    assert script.ecrire("secret.txt", ACCENTS) is True
    assert (tmp_path / "secret.txt").read_bytes().decode("utf-8") == ACCENTS
    assert ouvertures.de("secret.txt") == [("write_text", "utf-8", None)]
