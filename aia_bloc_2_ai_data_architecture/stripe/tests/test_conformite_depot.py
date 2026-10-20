# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Règles du dépôt vérifiées par des tests plutôt que par une relecture.

- mention de droits d'auteur dans chaque fichier du livrable (en tête pour le code, en pied de
  page pour les documents Markdown) ;
- aucun caractère d'encodage corrompu (les accents français doivent être intacts) ;
- les documents demandés existent et chaque diagramme est suivi de sa description textuelle.

Une règle qui ne sait rien refuser serait toujours verte : chacune est écrite comme une fonction
`exiger_...` (elle lève `AssertionError` quand le texte la viole), appliquée aux vrais fichiers,
puis à une copie VOLONTAIREMENT FAUTIVE du même texte, qu'elle doit refuser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent.parent
COPYRIGHT = "Copyright © 2026 · voir le fichier LICENSE du dépôt"
DOSSIERS = (RACINE / "stripe", RACINE / "livrables" / "aia2")
EXCLUS = {"sorties", "secrets", "__pycache__", ".pytest_cache", ".ruff_cache", ".terraform",
          ".venv", "_env", "_quarantaine"}
SUFFIXES = {".py", ".md", ".sql", ".js", ".json", ".tf", ".yml", ".yaml", ".sh", ".txt",
            ".exemple", ".toml", ".env"}
NOMS_SANS_SUFFIXE = {"Dockerfile", ".dockerignore"}

# Séquences typiques d'un texte UTF-8 relu en Latin-1 (« é », « è », « à », le début de « € » et
# de « · ») et caractère de remplacement, écrites par leurs codes Unicode pour que ce fichier ne
# les contienne pas lui-même.
SEQUENCES_CORROMPUES = (
    chr(0xFFFD), chr(0xC3) + chr(0xA9), chr(0xC3) + chr(0xA8), chr(0xC3) + chr(0xA0),
    chr(0xE2) + chr(0x20AC), chr(0xC2) + chr(0xB7))

DOCUMENTS = [
    "00_cahier_des_charges.md", "01_architecture_globale.md", "02_oltp_erd.md",
    "03_olap_schema.md", "04_nosql_modele.md", "05_pipeline.md", "06_securite_conformite.md",
    "07_integration_ml.md", "08_requetes.md", "09_dimensionnement_finops_greenops.md",
    "10_continuite_supervision_maintenance.md", "11_interfaces_responsabilites.md",
    "13_couverture_et_limites.md",
]


def _fichiers() -> list[Path]:
    trouves = []
    for dossier in DOSSIERS:
        for chemin in sorted(dossier.rglob("*")):
            if not chemin.is_file() or EXCLUS & set(chemin.relative_to(RACINE).parts):
                continue
            if chemin.suffix in SUFFIXES or chemin.name in NOMS_SANS_SUFFIXE:
                trouves.append(chemin)
    return trouves


FICHIERS = _fichiers()


def _id(chemin: Path) -> str:
    return str(chemin.relative_to(RACINE)).replace("\\", "/")


# ---------------------------------------------------------------- règles


def exiger_mention_de_droits(suffixe: str, texte: str) -> None:
    if suffixe == ".json":
        assert json.loads(texte)["_copyright"] == COPYRIGHT
    elif suffixe == ".md":
        derniere = [ligne for ligne in texte.splitlines() if ligne.strip()][-1]
        assert derniere == f"*{COPYRIGHT}*", "pied de page attendu"
    else:
        assert COPYRIGHT in "\n".join(texte.splitlines()[:5]), "mention attendue en tête"


def exiger_encodage_intact(texte: str) -> None:
    for corrompu in SEQUENCES_CORROMPUES:
        assert corrompu not in texte, f"caractère corrompu {corrompu!r}"


def exiger_checkov_sans_echec(texte: str) -> None:
    assert re.search(r"Failed checks: 0,", texte), "des contrôles Checkov sont en échec"
    assert "Passed checks:" in texte, "le bilan des contrôles réussis est absent"


def _blocs_mermaid(texte: str):
    for m in re.finditer(r"```mermaid\n.*?\n```", texte, re.S):
        suite = [ligne for ligne in texte[m.end():].splitlines() if ligne.strip()][:1]
        yield suite


def exiger_diagrammes_decrits(nom: str, texte: str) -> None:
    for suite in _blocs_mermaid(texte):
        assert suite and suite[0].startswith("**Description textuelle"), nom


def exiger_blocs_de_code_fermes(nom: str, texte: str) -> None:
    assert texte.count("```") % 2 == 0, nom


def exiger_aucun_marqueur_d_inachevement(nom: str, texte: str) -> None:
    for marqueur in ("TODO", "FIXME", "XXX", "@@", "lorem"):
        assert marqueur not in texte, (nom, marqueur)


# ---------------------------------------------------------------- les fichiers du dépôt


def test_le_depot_contient_des_fichiers_a_controler():
    assert len(FICHIERS) > 100
    assert all(not (EXCLUS & set(c.relative_to(RACINE).parts)) for c in FICHIERS)  # exclus écartés


@pytest.mark.parametrize("chemin", FICHIERS, ids=_id)
def test_mention_de_droits_d_auteur(chemin):
    texte = chemin.read_text(encoding="utf-8")
    exiger_mention_de_droits(chemin.suffix, texte)

    sans_mention = texte.replace(COPYRIGHT, "")
    if chemin.suffix == ".json":
        sans_mention = json.dumps({**json.loads(texte), "_copyright": "autre"})
    with pytest.raises((AssertionError, KeyError, ValueError)):
        exiger_mention_de_droits(chemin.suffix, sans_mention)


@pytest.mark.parametrize("chemin", FICHIERS, ids=_id)
def test_encodage_et_accents_intacts(chemin):
    texte = chemin.read_text(encoding="utf-8")
    exiger_encodage_intact(texte)

    for sequence in SEQUENCES_CORROMPUES:
        with pytest.raises((AssertionError, KeyError, ValueError)):
            exiger_encodage_intact(texte + "\n" + sequence)


# ---------------------------------------------------------------- les documents demandés


@pytest.mark.parametrize("nom", DOCUMENTS)
def test_les_documents_demandes_existent(nom):
    chemin = RACINE / "livrables" / "aia2" / nom
    assert chemin.exists() and chemin.stat().st_size > 1500, nom

    absent = RACINE / "livrables" / "aia2" / ("absent_" + nom)
    assert not absent.exists()


def test_le_resultat_checkov_est_joint_et_sans_echec():
    texte = (RACINE / "livrables" / "aia2" / "checkov.txt").read_text(encoding="utf-8")
    exiger_checkov_sans_echec(texte)

    # Un seul contrôle en échec, ou un fichier sans le bilan des contrôles réussis : refusés.
    with pytest.raises(AssertionError):
        exiger_checkov_sans_echec(texte.replace("Failed checks: 0,", "Failed checks: 3,"))
    with pytest.raises(AssertionError):
        exiger_checkov_sans_echec(texte.replace("Passed checks:", "Contrôles :"))
    with pytest.raises(AssertionError):
        exiger_checkov_sans_echec("")


@pytest.mark.parametrize("nom", [d for d in DOCUMENTS if d != "13_couverture_et_limites.md"])
def test_chaque_diagramme_est_suivi_de_sa_description(nom):
    texte = (RACINE / "livrables" / "aia2" / nom).read_text(encoding="utf-8")
    exiger_diagrammes_decrits(nom, texte)

    sans_description = texte + "\n```mermaid\ngraph TD; A-->B\n```\n\nUn paragraphe quelconque.\n"
    with pytest.raises((AssertionError, KeyError, ValueError)):
        exiger_diagrammes_decrits(nom, sans_description)


def test_les_blocs_de_code_sont_fermes_et_aucun_marqueur_d_inachevement_ne_subsiste():
    for nom in DOCUMENTS:
        texte = (RACINE / "livrables" / "aia2" / nom).read_text(encoding="utf-8")
        exiger_blocs_de_code_fermes(nom, texte)
        exiger_aucun_marqueur_d_inachevement(nom, texte)

        with pytest.raises((AssertionError, KeyError, ValueError)):
            exiger_blocs_de_code_fermes(nom, texte + "\n```python\nx = 1\n")
        for marqueur in ("TODO", "FIXME", "XXX", "@@", "lorem"):
            with pytest.raises((AssertionError, KeyError, ValueError)):
                exiger_aucun_marqueur_d_inachevement(nom, texte + "\n" + marqueur)
