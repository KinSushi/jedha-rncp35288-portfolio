# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Génération des mots de passe locaux : créés une fois, jamais écrasés, longs et distincts.

Pourquoi : un mot de passe écrit dans un dépôt est public ; le script les crée donc au premier
lancement. Le danger inverse est tout aussi réel : relancer le script ne doit JAMAIS remplacer
un secret déjà posé, car la base initialisée avec l'ancien mot de passe deviendrait inaccessible.
Les tests vérifient les deux faces : ce qui est créé, et ce qui est refusé ou laissé intact.
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from deploiement import generer_secrets as gs


def _contenu(dossier, nom: str) -> str:
    return (dossier / f"{nom}.txt").read_text(encoding="utf-8")


def test_les_quatre_secrets_sont_aleatoires_longs_distincts_et_ecrits_proprement(
        tmp_path, lecture_ecriture_en_utf8):
    profond = tmp_path / "a" / "b" / "secrets"  # le dossier et ses parents sont créés

    crees = gs.generer(profond)

    assert crees == list(gs.NOMS)
    assert sorted(p.name for p in profond.iterdir()) == sorted(f"{n}.txt" for n in gs.NOMS)
    valeurs = [_contenu(profond, nom) for nom in gs.NOMS]
    for valeur in valeurs:
        # 24 octets aléatoires en base 64 « URL » : exactement 32 caractères, sans caractère
        # à protéger dans un fichier, une URL ou un shell.
        assert re.fullmatch(r"[A-Za-z0-9_-]{32}", valeur), valeur
    assert len(set(valeurs)) == len(valeurs)  # tous distincts
    for nom in gs.NOMS:
        octets = (profond / f"{nom}.txt").read_bytes()
        # Une fin de ligne ferait partie du mot de passe lu par les services.
        assert b"\n" not in octets and b"\r" not in octets
    if os.name == "posix":  # Windows n'a pas de bits de droits UNIX à comparer
        for nom in gs.NOMS:
            assert (profond / f"{nom}.txt").stat().st_mode & 0o777 == 0o644
    # UTF-8 explicite, aucune conversion de fin de ligne (`newline=""`).
    ecritures = [o for o in lecture_ecriture_en_utf8 if o["mode"].startswith("w")]
    assert len(ecritures) == len(gs.NOMS)
    assert all(o["fin_de_ligne"] == "" for o in ecritures)

    # Deux exécutions sur des dossiers différents : valeurs différentes (source aléatoire).
    gs.generer(tmp_path / "autre")
    for nom in gs.NOMS:
        assert _contenu(tmp_path / "autre", nom) != _contenu(profond, nom)


def test_un_secret_existant_n_est_jamais_ecrase_et_un_chemin_qui_est_un_fichier_est_refuse(
        tmp_path):
    """État inchangé : le secret déjà posé garde sa valeur, les manquants sont créés quand même."""
    existant = gs.NOMS[1]  # au milieu : un `break` au lieu d'un `continue` laisserait des trous
    (tmp_path / f"{existant}.txt").write_text("valeur-deja-initialisee", encoding="utf-8")

    crees = gs.generer(tmp_path)

    assert crees == [n for n in gs.NOMS if n != existant]
    assert _contenu(tmp_path, existant) == "valeur-deja-initialisee"
    for nom in crees:
        assert re.fullmatch(r"[A-Za-z0-9_-]{32}", _contenu(tmp_path, nom))

    # Relancer ne crée rien et ne change aucun secret.
    avant = {nom: _contenu(tmp_path, nom) for nom in gs.NOMS}
    assert gs.generer(tmp_path) == []
    assert {nom: _contenu(tmp_path, nom) for nom in gs.NOMS} == avant

    # Si `secrets` est déjà un fichier ordinaire, le script s'arrête au lieu de l'ignorer.
    obstacle = tmp_path / "secrets"
    obstacle.write_text("je ne suis pas un dossier", encoding="utf-8")
    with pytest.raises(FileExistsError):
        gs.generer(obstacle)
    assert obstacle.read_text(encoding="utf-8") == "je ne suis pas un dossier"


def test_le_dossier_par_defaut_et_le_programme_lance_en_ligne_de_commande(tmp_path):
    """`python deploiement/generer_secrets.py` crée les secrets dans `secrets/` et en rend compte.

    Le script est copié dans un faux projet : le vrai dossier `secrets/` du dépôt n'est pas touché.
    """
    racine = Path(gs.__file__).resolve().parent.parent
    assert gs.DOSSIER == racine / "secrets"
    assert inspect.signature(gs.generer).parameters["dossier"].default == gs.DOSSIER

    faux_projet = tmp_path / "projet"
    (faux_projet / "deploiement").mkdir(parents=True)
    script = faux_projet / "deploiement" / "generer_secrets.py"
    shutil.copyfile(gs.__file__, script)
    environnement = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    premier = subprocess.run([sys.executable, str(script)], capture_output=True, check=False,
                             timeout=60, cwd=racine, env=environnement)
    second = subprocess.run([sys.executable, str(script)], capture_output=True, check=False,
                            timeout=60, cwd=racine, env=environnement)

    assert premier.returncode == 0 and second.returncode == 0
    assert premier.stdout.decode("utf-8").replace("\r\n", "\n") == (
        "créés : " + ", ".join(gs.NOMS) + "\n")
    assert sorted(p.name for p in (faux_projet / "secrets").iterdir()) == sorted(
        f"{nom}.txt" for nom in gs.NOMS)
    # Relancé, le programme ne crée rien et le dit.
    assert second.stdout.decode("utf-8").replace("\r\n", "\n") == (
        "créés : aucun (tous existent déjà)\n")
