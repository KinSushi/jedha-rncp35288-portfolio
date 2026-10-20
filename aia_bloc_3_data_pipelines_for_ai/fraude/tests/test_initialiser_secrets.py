# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Génération des secrets de production locale : jamais d'écrasement, jamais d'affichage.

Le script ``scripts/initialiser_secrets.py`` crée les fichiers montés dans les conteneurs. Sa règle
d'or est qu'un secret existant n'est **jamais** remplacé (sinon la base déjà initialisée et ses
fichiers de connexion divergeraient) et qu'aucune valeur ne s'affiche. Chaque test charge une copie
neuve du module, redirige ``DOSSIER`` vers un dossier temporaire et compare l'état *avant* à l'état
*après* : le vrai dossier ``secrets/`` du projet n'est jamais touché.
"""

from __future__ import annotations

import base64
import importlib.util
import itertools
import os
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from detection_fraude.configuration import RACINE_PROJET

CHEMIN_SCRIPT = RACINE_PROJET / "scripts" / "initialiser_secrets.py"
OCTETS_CLE_FERNET = 32  # taille d'une clé Fernet décodée
NB_TIRAGES = 5  # mots de passe tirés pour vérifier qu'ils diffèrent
NOMS_ATTENDUS = [
    "airflow_admin_motdepasse.txt",
    "airflow_fernet_key.txt",
    "airflow_secret_key.txt",
    "airflow_sql_conn.txt",
    "dsn_entrepot.txt",
    "mlflow_backend_uri.txt",
    "pg_airflow_motdepasse.txt",
    "pg_fraude_motdepasse.txt",
    "pg_mlflow_motdepasse.txt",
    "pg_superuser_motdepasse.txt",
    "sel_hachage.txt",
    "smtp_mot_de_passe.txt",
    "smtp_utilisateur.txt",
    "webhook_url.txt",
]
FAMILLE_POSTGRES = [
    "airflow_sql_conn.txt",
    "dsn_entrepot.txt",
    "mlflow_backend_uri.txt",
    "pg_airflow_motdepasse.txt",
    "pg_fraude_motdepasse.txt",
    "pg_mlflow_motdepasse.txt",
]
FACULTATIFS = ["smtp_mot_de_passe.txt", "smtp_utilisateur.txt", "webhook_url.txt"]
CLE_FERNET_DETERMINISTE = base64.urlsafe_b64encode(b"\x01" * 32).decode()


def charger_script() -> ModuleType:
    """Charge une copie neuve du script depuis son chemin réel, sans écrire de cache d'octets."""
    ancien = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        # Le module porte son nom de chemin (``scripts.initialiser_secrets``) : c'est sous ce nom
        # que l'outil de mutation relie ses mutants aux tests qui exécutent le script.
        spec = importlib.util.spec_from_file_location("scripts.initialiser_secrets", CHEMIN_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = ancien
    return module


@pytest.fixture
def script(tmp_path, monkeypatch):
    """Le module, avec ``DOSSIER`` redirigé dans le dossier temporaire du test."""
    module = charger_script()
    monkeypatch.setattr(module, "DOSSIER", tmp_path / "secrets")
    return module


@pytest.fixture
def aleatoire_fixe(script, monkeypatch):
    """Aléa déterministe : ``token_hex(n)`` rend ``hex<n>x<rang>``, ``urandom`` rend des 0x01."""
    rang = itertools.count(1)
    monkeypatch.setattr(
        script, "secrets", SimpleNamespace(token_hex=lambda n: f"hex{n}x{next(rang)}")
    )
    monkeypatch.setattr(script, "os", SimpleNamespace(urandom=lambda n: b"\x01" * n))
    return script


@pytest.fixture
def droits_enregistres(monkeypatch):
    """Garde chaque appel à ``Path.chmod`` (nom, mode), puis l'exécute vraiment."""
    appels: list[tuple[str, int]] = []
    original = Path.chmod

    def espion(self, mode, *args, **kwargs):
        appels.append((self.name, mode))
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", espion)
    return appels


def contenus(dossier: Path) -> dict[str, str]:
    return {f.name: f.read_text(encoding="utf-8") for f in sorted(dossier.iterdir())}


def vieillir(dossier: Path) -> dict[str, float]:
    """Date tous les fichiers de 1er janvier 2020 : un fichier réécrit se verrait aussitôt."""
    ancien = 1_577_836_800.0
    for fichier in dossier.iterdir():
        os.utime(fichier, (ancien, ancien))
    return {f.name: f.stat().st_mtime for f in dossier.iterdir()}


# --------------------------------------------------------------------------------------------
# Chargement et ecrire
# --------------------------------------------------------------------------------------------
def test_le_dossier_par_defaut_est_secrets_a_la_racine_du_projet_et_l_import_ne_cree_rien():
    """Importer le script n'écrit ni secret ni cache dans le projet ; la cible est ``secrets/``."""
    scripts_avant = sorted(p.name for p in CHEMIN_SCRIPT.parent.iterdir())
    secrets_existait_avant = (RACINE_PROJET / "secrets").exists()
    module = charger_script()
    assert module.DOSSIER == RACINE_PROJET / "secrets"
    assert (RACINE_PROJET / "secrets").exists() == secrets_existait_avant
    assert sorted(p.name for p in CHEMIN_SCRIPT.parent.iterdir()) == scripts_avant


def test_ecrire_cree_le_fichier_puis_ne_remplace_jamais_un_secret_existant(
    script, droits_enregistres
):
    """Premier appel : créé (True, 0644) ; ensuite : refusé (False), contenu et date intacts."""
    script.DOSSIER.mkdir()
    assert script.ecrire("a.txt", "valeur é") is True
    chemin = script.DOSSIER / "a.txt"
    assert chemin.read_bytes() == "valeur é".encode()
    assert droits_enregistres == [("a.txt", 0o644)]
    dates_avant = vieillir(script.DOSSIER)
    contenu_avant = chemin.read_bytes()
    assert script.ecrire("a.txt", "AUTRE VALEUR") is False
    assert chemin.read_bytes() == contenu_avant
    assert {f.name: f.stat().st_mtime for f in script.DOSSIER.iterdir()} == dates_avant
    assert droits_enregistres == [("a.txt", 0o644)]  # aucun nouvel appel : le fichier est laissé
    (script.DOSSIER / "vide.txt").write_text("", encoding="utf-8")  # un secret vide existe aussi
    assert script.ecrire("vide.txt", "x") is False
    assert (script.DOSSIER / "vide.txt").read_text(encoding="utf-8") == ""


def test_ecrire_sans_dossier_est_refuse_et_ne_cree_ni_dossier_ni_fichier(script, tmp_path):
    """Sans ``DOSSIER`` (jamais créé par ``main``), rien n'est écrit ailleurs par accident."""
    entrees_avant = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(FileNotFoundError):
        script.ecrire("a.txt", "valeur")
    assert sorted(p.name for p in tmp_path.iterdir()) == entrees_avant
    assert not script.DOSSIER.exists()


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
def test_main_cree_les_quatorze_secrets_attendus_puis_laisse_tout_inchange_a_la_relance(
    aleatoire_fixe, tmp_path, capsys, droits_enregistres
):
    """Contenus et droits exacts ; une relance ne change ni un octet ni une date de fichier."""
    script = aleatoire_fixe
    entrees_avant = sorted(p.name for p in tmp_path.iterdir())
    code_sortie = script.main()
    assert code_sortie == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*entrees_avant, "secrets"])
    assert sorted(f.name for f in script.DOSSIER.iterdir()) == NOMS_ATTENDUS
    assert contenus(script.DOSSIER) == {
        "airflow_admin_motdepasse.txt": "hex24x7",
        "airflow_fernet_key.txt": CLE_FERNET_DETERMINISTE,
        "airflow_secret_key.txt": "hex32x6",
        "airflow_sql_conn.txt": "postgresql+psycopg2://airflow:hex24x2@postgres:5432/airflow",
        "dsn_entrepot.txt": "postgresql+psycopg2://fraude:hex24x4@postgres:5432/fraude",
        "mlflow_backend_uri.txt": "postgresql+psycopg2://mlflow:hex24x3@postgres:5432/mlflow",
        "pg_airflow_motdepasse.txt": "hex24x2",
        "pg_fraude_motdepasse.txt": "hex24x4",
        "pg_mlflow_motdepasse.txt": "hex24x3",
        "pg_superuser_motdepasse.txt": "hex24x1",
        "sel_hachage.txt": "hex32x5",
        "smtp_mot_de_passe.txt": "",
        "smtp_utilisateur.txt": "",
        "webhook_url.txt": "",
    }
    assert droits_enregistres == [
        ("secrets", 0o700),
        ("pg_superuser_motdepasse.txt", 0o644),
        ("pg_airflow_motdepasse.txt", 0o644),
        ("pg_mlflow_motdepasse.txt", 0o644),
        ("pg_fraude_motdepasse.txt", 0o644),
        ("dsn_entrepot.txt", 0o644),
        ("airflow_sql_conn.txt", 0o644),
        ("mlflow_backend_uri.txt", 0o644),
        ("sel_hachage.txt", 0o644),
        ("airflow_fernet_key.txt", 0o644),
        ("airflow_secret_key.txt", 0o644),
        ("airflow_admin_motdepasse.txt", 0o644),
        ("smtp_utilisateur.txt", 0o644),
        ("smtp_mot_de_passe.txt", 0o644),
        ("webhook_url.txt", 0o644),
    ]
    if os.name == "posix":
        assert script.DOSSIER.stat().st_mode & 0o777 == 0o700
        assert {f.stat().st_mode & 0o777 for f in script.DOSSIER.iterdir()} == {0o644}
    sortie = capsys.readouterr()
    assert sortie.out == (
        f"Secrets prêts dans {script.DOSSIER} (valeurs non affichées).\n"
        "Étape suivante : docker compose up -d --build\n"
    )
    assert sortie.err == ""
    for valeur in set(contenus(script.DOSSIER).values()) - {""}:
        assert valeur not in sortie.out

    contenus_avant = contenus(script.DOSSIER)
    dates_avant = vieillir(script.DOSSIER)
    droits_enregistres.clear()
    assert script.main() == 0
    assert contenus(script.DOSSIER) == contenus_avant
    assert {f.name: f.stat().st_mtime for f in script.DOSSIER.iterdir()} == dates_avant
    assert droits_enregistres == [("secrets", 0o700)]  # seul le dossier est re-protégé
    assert capsys.readouterr().out == sortie.out


def test_main_avec_un_alea_reel_donne_des_secrets_forts_coherents_et_garde_ceux_qui_existent(
    script, capsys
):
    """Formats (48/64 hex, clé Fernet de 32 octets), DSN cohérents, secrets humains intacts."""
    script.DOSSIER.mkdir()
    existants = {
        "sel_hachage.txt": "sel-choisi-par-l-exploitant",
        "airflow_admin_motdepasse.txt": "mot-de-passe-admin-choisi",
        "smtp_utilisateur.txt": "alice@exemple.test",
    }
    for nom, valeur in existants.items():
        (script.DOSSIER / nom).write_text(valeur, encoding="utf-8")
    dates_avant = vieillir(script.DOSSIER)
    assert script.main() == 0
    lus = contenus(script.DOSSIER)
    assert sorted(lus) == NOMS_ATTENDUS
    for nom, valeur in existants.items():  # les secrets existants ne bougent pas
        assert lus[nom] == valeur
        assert (script.DOSSIER / nom).stat().st_mtime == dates_avant[nom]
    mots_de_passe = {
        nom: lus[f"pg_{nom}_motdepasse.txt"]
        for nom in ("superuser", "airflow", "mlflow", "fraude")
    }
    for mot in mots_de_passe.values():
        assert re.fullmatch(r"[0-9a-f]{48}", mot)
    assert len(set(mots_de_passe.values())) == len(mots_de_passe)  # quatre mots de passe distincts
    assert lus["dsn_entrepot.txt"] == (
        f"postgresql+psycopg2://fraude:{mots_de_passe['fraude']}@postgres:5432/fraude"
    )
    assert lus["airflow_sql_conn.txt"] == (
        f"postgresql+psycopg2://airflow:{mots_de_passe['airflow']}@postgres:5432/airflow"
    )
    assert lus["mlflow_backend_uri.txt"] == (
        f"postgresql+psycopg2://mlflow:{mots_de_passe['mlflow']}@postgres:5432/mlflow"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", lus["airflow_secret_key.txt"])
    assert len(base64.urlsafe_b64decode(lus["airflow_fernet_key.txt"])) == OCTETS_CLE_FERNET
    assert lus["airflow_fernet_key.txt"].endswith("=")
    assert (lus["smtp_mot_de_passe.txt"], lus["webhook_url.txt"]) == ("", "")
    sortie = capsys.readouterr().out
    for valeur in [*lus.values(), *existants.values()]:
        if valeur:
            assert valeur not in sortie
    assert re.fullmatch(r"[0-9a-f]{48}", script.mot_de_passe())
    assert len({script.mot_de_passe() for _ in range(NB_TIRAGES)}) == NB_TIRAGES


def test_main_ne_reecrit_pas_les_secrets_postgres_si_le_superuser_existe_deja(aleatoire_fixe):
    """Base déjà initialisée : ses mots de passe et URI ne sont ni régénérés ni recréés."""
    script = aleatoire_fixe
    script.DOSSIER.mkdir()
    (script.DOSSIER / "pg_superuser_motdepasse.txt").write_text(
        "ancien-superuser", encoding="utf-8"
    )
    superuser_avant = (script.DOSSIER / "pg_superuser_motdepasse.txt").read_bytes()
    assert script.main() == 0
    lus = contenus(script.DOSSIER)
    assert (script.DOSSIER / "pg_superuser_motdepasse.txt").read_bytes() == superuser_avant
    assert lus["pg_superuser_motdepasse.txt"] == "ancien-superuser"
    assert [nom for nom in FAMILLE_POSTGRES if nom in lus] == []  # aucun nouveau mot de passe
    assert sorted(lus) == sorted(
        ["pg_superuser_motdepasse.txt", "sel_hachage.txt", "airflow_fernet_key.txt",
         "airflow_secret_key.txt", "airflow_admin_motdepasse.txt", *FACULTATIFS]
    )  # fmt: skip
    assert lus["sel_hachage.txt"] == "hex32x5"  # les autres secrets sont bien créés


def test_main_laisse_intacts_un_dossier_existant_et_les_fichiers_qui_ne_sont_pas_des_secrets(
    aleatoire_fixe, droits_enregistres
):
    """Un dossier déjà là et ses fichiers étrangers ne sont ni supprimés ni modifiés."""
    script = aleatoire_fixe
    script.DOSSIER.mkdir()
    (script.DOSSIER / "LISEZ_MOI.txt").write_text("notes de l'exploitant", encoding="utf-8")
    (script.DOSSIER / "pg_autre.txt").write_text("pas un secret du projet", encoding="utf-8")
    etrangers_avant = {
        nom: (script.DOSSIER / nom).read_bytes() for nom in ("LISEZ_MOI.txt", "pg_autre.txt")
    }
    assert script.main() == 0
    assert {nom: (script.DOSSIER / nom).read_bytes() for nom in etrangers_avant} == etrangers_avant
    assert sorted(f.name for f in script.DOSSIER.iterdir()) == sorted(
        [*NOMS_ATTENDUS, "LISEZ_MOI.txt", "pg_autre.txt"]
    )
    assert ("LISEZ_MOI.txt", 0o644) not in droits_enregistres


def test_un_secret_efface_est_recree_seul_les_autres_restent_inchanges(aleatoire_fixe):
    """Perte d'un seul fichier : lui seul est régénéré, les autres secrets ne bougent pas."""
    script = aleatoire_fixe
    assert script.main() == 0
    (script.DOSSIER / "sel_hachage.txt").unlink()
    (script.DOSSIER / "webhook_url.txt").write_text(
        "https://hooks.exemple.test/jeton", encoding="utf-8"
    )
    autres_avant = dict(contenus(script.DOSSIER))
    assert script.main() == 0
    apres = contenus(script.DOSSIER)
    assert apres.pop("sel_hachage.txt") == "hex32x12"  # nouveau tirage (12e de la séquence)
    assert apres == autres_avant
    assert apres["webhook_url.txt"] == "https://hooks.exemple.test/jeton"
