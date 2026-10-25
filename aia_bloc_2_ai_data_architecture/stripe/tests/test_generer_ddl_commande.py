# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Générateur de DDL : la commande (écrire, vérifier), la variante portable et les refus.

`test_generer_ddl.py` prouve que les fichiers livrés sont à jour et exécutables. Ici, on prouve le
comportement de la COMMANDE `python -m stripe_demo.generer_ddl` : elle écrit les trois fichiers
au bon endroit, en UTF-8 et fins de ligne LF ; avec `--verifier` elle n'écrit rien, rend le code 1
et nomme les fichiers périmés (ou manquants) ; une cible de DDL inconnue est refusée au lieu de
produire en silence le DDL Redshift.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stripe_demo import generer_ddl as g

RACINE_PROJET = Path(__file__).resolve().parent.parent
PORTABLE_ATTENDU = Path(__file__).resolve().parent / "donnees" / "olap_portable.sql"
TROIS_FICHIERS = ("oltp_schema_postgresql.sql, olap_schema.sql, creer_collections.js")


@pytest.fixture
def depot_temporaire(tmp_path, monkeypatch):
    """Redirige les trois fichiers générés vers un dossier jetable (pas encore créé)."""
    monkeypatch.setattr(g, "RACINE", tmp_path)
    monkeypatch.setattr(g, "FICHIER_OLTP", tmp_path / "sql" / "oltp_schema_postgresql.sql")
    monkeypatch.setattr(g, "FICHIER_OLAP", tmp_path / "sql" / "olap_schema.sql")
    monkeypatch.setattr(g, "FICHIER_MONGO", tmp_path / "nosql" / "creer_collections.js")
    return tmp_path


def _chemins(depot: Path) -> list[Path]:
    return [depot / "sql" / "oltp_schema_postgresql.sql", depot / "sql" / "olap_schema.sql",
            depot / "nosql" / "creer_collections.js"]


def test_la_commande_ecrit_les_trois_fichiers_en_utf8_lf_et_refuse_une_option_inconnue(
        depot_temporaire, capsys, lecture_ecriture_en_utf8):
    assert not (depot_temporaire / "sql").exists()

    assert g.main([]) == 0
    assert g.main([]) == 0  # dossiers déjà là : pas d'erreur

    attendu = g.fichiers_generes()
    assert list(attendu) == _chemins(depot_temporaire)
    for chemin, contenu in attendu.items():
        assert chemin.read_text(encoding="utf-8") == contenu
        octets = chemin.read_bytes()
        assert b"\r" not in octets, chemin.name  # fins de ligne LF sur tous les systèmes
        assert octets.endswith(b"\n") and not octets.endswith(b"\n\n"), chemin.name
    sortie = capsys.readouterr().out.splitlines()
    assert sortie == 2 * [f"écrit : {Path('sql') / 'oltp_schema_postgresql.sql'}",
                          f"écrit : {Path('sql') / 'olap_schema.sql'}",
                          f"écrit : {Path('nosql') / 'creer_collections.js'}"]
    ecritures = [o for o in lecture_ecriture_en_utf8 if o["mode"].startswith("w")]
    assert len(ecritures) == 2 * len(_chemins(depot_temporaire))
    assert all(o["fin_de_ligne"] == "\n" for o in ecritures)

    # Un fichier périmé est remplacé par le contenu attendu.
    oltp = _chemins(depot_temporaire)[0]
    oltp.write_text("contenu périmé\n", encoding="utf-8")
    assert g.main([]) == 0
    assert oltp.read_text(encoding="utf-8") == g.ddl_oltp_postgresql()

    # Une option inconnue arrête la commande (code 2) ; l'aide décrit la commande et --verifier.
    with pytest.raises(SystemExit) as sortie_erreur:
        g.main(["--effacer-tout"])
    assert sortie_erreur.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_verifier_nomme_les_fichiers_perimes_ou_manquants_sans_rien_ecrire_et_rend_un(
        depot_temporaire, capsys, monkeypatch, lecture_ecriture_en_utf8):
    # Rien n'existe : tout est périmé, et la vérification ne crée aucun dossier.
    assert g.main(["--verifier"]) == 1
    assert capsys.readouterr().out == f"fichiers périmés : {TROIS_FICHIERS}\n"
    assert not (depot_temporaire / "sql").exists()
    assert not (depot_temporaire / "nosql").exists()
    assert not [o for o in lecture_ecriture_en_utf8 if o["mode"].startswith("w")]

    # Tout est à jour : silence et code 0 ; les fichiers ne sont pas touchés.
    g.main([])
    capsys.readouterr()
    avant = [chemin.read_bytes() for chemin in _chemins(depot_temporaire)]
    assert g.main(["--verifier"]) == 0
    assert capsys.readouterr().out == ""
    assert [chemin.read_bytes() for chemin in _chemins(depot_temporaire)] == avant

    # Un fichier périmé, un seul octet de différence, plusieurs : nommés dans l'ordre.
    olap = _chemins(depot_temporaire)[1]
    olap.write_text(olap.read_text(encoding="utf-8") + "-- modification manuelle\n",
                    encoding="utf-8")
    assert g.main(["--verifier"]) == 1
    assert capsys.readouterr().out == "fichiers périmés : olap_schema.sql\n"
    assert olap.read_text(encoding="utf-8").endswith("-- modification manuelle\n")  # intact
    g.main([])
    capsys.readouterr()
    mongo = _chemins(depot_temporaire)[2]
    mongo.write_text(mongo.read_text(encoding="utf-8").replace("strict", "strict "),
                     encoding="utf-8")
    assert g.main(["--verifier"]) == 1
    assert capsys.readouterr().out == "fichiers périmés : creer_collections.js\n"
    for chemin in _chemins(depot_temporaire):
        chemin.write_text("vieux\n", encoding="utf-8")
    assert g.main(["--verifier"]) == 1
    assert capsys.readouterr().out == f"fichiers périmés : {TROIS_FICHIERS}\n"

    # Les fichiers livrés avec le dépôt sont à jour : code 0 par `python -m`.
    monkeypatch.undo()
    resultat = subprocess.run(
        [sys.executable, "-m", "stripe_demo.generer_ddl", "--verifier"], cwd=RACINE_PROJET,
        capture_output=True, check=False, timeout=120,
        env={**os.environ, "PYTHONPATH": str(RACINE_PROJET)})
    assert resultat.returncode == 0, resultat.stdout.decode("utf-8", "replace")
    assert resultat.stdout == b""

    # La commande est bien lancée par `python -m` : l'aide s'affiche (si le point d'entrée ne
    # démarrait rien, le processus se terminerait en silence avec le code 0, comme ci-dessus).
    aide = subprocess.run(
        [sys.executable, "-m", "stripe_demo.generer_ddl", "--help"], cwd=RACINE_PROJET,
        capture_output=True, check=False, timeout=120,
        env={**os.environ, "PYTHONPATH": str(RACINE_PROJET)})
    assert aide.returncode == 0
    assert b"--verifier" in aide.stdout


def test_l_aide_decrit_la_commande_et_l_option_verifier(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")

    with pytest.raises(SystemExit) as sortie:
        g.main(["--help"])

    aide = capsys.readouterr().out
    assert sortie.value.code == 0
    assert g.__doc__.splitlines()[0] in aide
    assert re.search(
        r"--verifier\s+ne rien écrire ; échouer si un fichier versionné est périmé$", aide,
        re.M)


def test_la_variante_portable_est_celle_du_fichier_de_reference_et_une_cible_inconnue_refusee():
    """Le DDL exécuté dans DuckDB par les tests ne change pas sans que cela se voie."""
    attendu = PORTABLE_ATTENDU.read_text(encoding="utf-8")

    assert g.ddl_olap("portable") == attendu
    assert attendu.splitlines()[0].startswith("-- Copyright ")
    assert g.ddl_olap() == g.ddl_olap("redshift")  # Redshift est la cible par défaut
    redshift = g.ddl_olap("redshift")
    assert redshift != attendu
    assert "CREATE TABLE IF NOT EXISTS stg_faits_paiements (LIKE faits_paiements);" in redshift
    assert "CREATE ROLE role_analyste;" in redshift
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO ROLE role_data_science;" in redshift
    assert "MERGE INTO faits_paiements" in redshift

    # Aucune faute de frappe ne donne en silence le DDL Redshift.
    for cible in ("Redshift", "PORTABLE", "postgres", "", "redshift ", "redshif", None):
        with pytest.raises(ValueError) as erreur:
            g.ddl_olap(cible)
        assert str(erreur.value) == (
            f"cible inconnue : {cible!r} (attendu : 'redshift' ou 'portable')")
        assert g.ddl_olap("redshift") == redshift


def test_les_dossiers_manquants_sont_crees_en_cascade_et_un_second_passage_les_reutilise(
        tmp_path, monkeypatch):
    cibles = {
        "FICHIER_OLTP": tmp_path / "a" / "b" / "oltp.sql",
        "FICHIER_OLAP": tmp_path / "c" / "olap.sql",
        "FICHIER_MONGO": tmp_path / "c" / "d" / "e" / "creer.js"}
    monkeypatch.setattr(g, "RACINE", tmp_path)
    for nom, chemin in cibles.items():
        monkeypatch.setattr(g, nom, chemin)

    assert g.main([]) == 0
    assert g.main([]) == 0  # les dossiers existent déjà : pas d'erreur

    attendu = g.fichiers_generes()
    assert list(attendu) == list(cibles.values())
    for chemin, contenu in attendu.items():
        assert chemin.read_text(encoding="utf-8") == contenu


def test_les_schemas_mongo_sont_lus_dans_l_ordre_sans_la_mention_de_droits(
        tmp_path, monkeypatch, lecture_ecriture_en_utf8):
    (tmp_path / "b.json").write_text('{"title": "b", "note": "é"}', encoding="utf-8")
    (tmp_path / "a.json").write_text('{"_copyright": "x", "title": "a"}', encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(g, "DOSSIER_SCHEMAS", tmp_path)

    schemas = g.schemas_mongo()

    assert list(schemas) == ["a", "b"]  # ordre alphabétique, seuls les fichiers .json
    # La clé « _copyright » est retirée du validateur ; le reste du schéma est intact.
    assert schemas == {"a": {"title": "a"}, "b": {"title": "b", "note": "é"}}
    lectures = [o for o in lecture_ecriture_en_utf8 if o["mode"] == "r"]
    assert [Path(o["fichier"]).name for o in lectures] == ["a.json", "b.json"]
