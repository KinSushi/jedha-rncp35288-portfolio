# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Exécute le notebook avec nbclient et compare ses sorties à celles de la version d'origine.

`tests/reference/sorties_origine.json` contient les sorties textuelles du notebook AVANT
l'extraction des fonctions dans `att_spam/` (exécution du 10/10/2026). Les durées et le nombre de
cœurs, qui dépendent de la machine, y sont remplacés par un marqueur ; tout le reste (effectifs,
F1, p-valeurs, SMS générés, coefficients, erreurs) doit être identique à l'exécution actuelle :
même graine, mêmes algorithmes déterministes.

Le corpus de SMS n'est pas redistribué dans ce dépôt : les lignes des cellules 6, 14 et 39 qui
recopient un SMS du corpus sont remplacées par un marqueur, dans la référence comme dans les
sorties enregistrées du notebook. La comparaison applique le même remplacement à l'exécution
(`sans_sms`) : le nombre et l'ordre des lignes restent vérifiés, pas le texte des SMS.

- `test_notebook_preparation_rapide` rejoue les cellules 0 à 16 (chargement, découpage, tenseurs,
  référence TF-IDF) : une dizaine de secondes.
- `test_notebook_complet` (marqueur `notebook`) rejoue tout : environ 30 minutes sur processeur.
  DistilBERT est lu dans le cache local de Hugging Face (lancer avec `HF_HUB_OFFLINE=1` pour
  interdire tout accès au réseau).
"""

import json
import re
import shutil
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient

RACINE = Path(__file__).resolve().parents[1]
REFERENCE = Path(__file__).resolve().parent / "reference"
DERNIERE_CELLULE_RAPIDE = 17  # exclue : on s'arrête avant la cellule 17 (section 4, réseaux)


def masquer(texte: str) -> str:
    """Remplace ce qui dépend de la machine : durées en secondes et nombre de cœurs."""
    texte = re.sub(r"\b\d+ s\b", "<durée> s", texte)
    return re.sub(r"cœurs utilisés : \d+", "cœurs utilisés : <n>", texte)


#: Cellules dont la sortie recopie des SMS du corpus, et remplacements appliqués ligne à ligne.
CELLULES_SMS = {"6", "14", "39"}
REMPLACEMENTS_SMS = [
    (re.compile(r"^(Exemple :) .*$"), r"\1 <SMS du corpus, non reproduit>"),
    (re.compile(r"^(\s+→\s+)\[.*$"), r"\1<découpage en mots, non reproduit>"),
    (re.compile(r"^(original|augmenté) : .*$"), r"\1 : <SMS du corpus, non reproduit>"),
    (re.compile(r"^( \[\d\.\d\d\]) .*$"), r"\1 <SMS du corpus, non reproduit>"),
]


def sans_sms(numero: str, texte: str) -> str:
    """Remplace par un marqueur les lignes qui recopient un SMS (cellules 6, 14 et 39 seulement)."""
    if numero not in CELLULES_SMS:
        return texte
    lignes = []
    for ligne in texte.split("\n"):
        for motif, remplacement in REMPLACEMENTS_SMS:
            ligne = motif.sub(remplacement, ligne)
        lignes.append(ligne)
    return "\n".join(lignes)


def sorties_par_cellule(notebook) -> dict[str, str]:
    """Texte des sorties de chaque cellule de code ; une figure est notée sans sa taille."""
    resultat: dict[str, str] = {}
    for i, cellule in enumerate(notebook.cells):
        if cellule.cell_type != "code":
            continue
        morceaux = []
        for sortie in cellule.outputs:
            if sortie.output_type == "stream":
                morceaux.append(sortie.text)
            elif sortie.output_type in ("execute_result", "display_data"):
                donnees = sortie.data
                if "text/plain" in donnees:
                    morceaux.append(f"[{sortie.output_type}] {donnees['text/plain']}\n")
                if "image/png" in donnees:
                    morceaux.append("[image png]\n")
            elif sortie.output_type == "error":
                morceaux.append(f"[ERREUR] {sortie.ename}: {sortie.evalue}\n")
        resultat[str(i)] = sans_sms(str(i), masquer("".join(morceaux)))
    return resultat


def executer(tmp_path: Path, nb_cellules: int | None = None):
    """Copie le notebook et son paquet dans un dossier propre, l'exécute, rend le notebook."""
    shutil.copy(RACINE / "spam.csv", tmp_path)
    shutil.copy(RACINE / "ATT_Spam_Bloc4_CDSD.ipynb", tmp_path)
    shutil.copytree(RACINE / "att_spam", tmp_path / "att_spam",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    notebook = nbformat.read(tmp_path / "ATT_Spam_Bloc4_CDSD.ipynb", as_version=4)
    if nb_cellules is not None:
        notebook.cells = notebook.cells[:nb_cellules]
    client = NotebookClient(
        notebook,
        timeout=3600,
        kernel_name="python3",
        resources={"metadata": {"path": str(tmp_path)}},
        record_timing=False,
    )
    client.execute()
    return notebook


def ecarts(obtenu: dict[str, str], attendu: dict[str, str]) -> list[str]:
    """Cellules dont la sortie diffère de la référence (ligne à ligne)."""
    liste = []
    for numero, texte in obtenu.items():
        if texte != attendu[numero]:
            lignes_obtenues, lignes_attendues = texte.splitlines(), attendu[numero].splitlines()
            for obtenue, attendue in zip(lignes_obtenues, lignes_attendues, strict=False):
                if obtenue != attendue:
                    liste.append(f"cellule {numero} : {obtenue!r} au lieu de {attendue!r}")
                    break
            else:
                liste.append(f"cellule {numero} : longueur différente")
    return liste


@pytest.fixture(scope="module")
def reference() -> dict[str, str]:
    return json.loads((REFERENCE / "sorties_origine.json").read_text(encoding="utf-8"))


def test_notebook_preparation_rapide(tmp_path, reference):
    notebook = executer(tmp_path, DERNIERE_CELLULE_RAPIDE)
    obtenu = sorties_par_cellule(notebook)
    attendu = {numero: reference[numero] for numero in obtenu}
    assert ecarts(obtenu, attendu) == []
    assert "Taille du vocabulaire : 2996 ; longueur des séquences : 33" in obtenu["12"]
    assert "Similarité maximale entre un SMS de test et l'apprentissage : 0.786" in obtenu["9"]
    assert "F1 de validation (seuil 0,5) : 0.947" in obtenu["16"]


@pytest.mark.notebook
def test_notebook_complet(tmp_path, reference):
    notebook = executer(tmp_path)
    obtenu = sorties_par_cellule(notebook)
    assert sorted(obtenu, key=int) == sorted(reference, key=int)
    assert ecarts(obtenu, reference) == []
    # Chiffres annoncés par les cellules Markdown 33 à 36 du notebook (tableau final)
    retenu = "Modèle retenu (meilleur F1 de validation) : DistilBERT (transfer learning)"
    assert retenu in obtenu["33"]
    assert "Rappel 92/93 = 0.989, IC 95 % [0.942 ; 0.998]" in obtenu["34"]
    assert "Messages légitimes bloqués à tort : 1 sur 646" in obtenu["34"]
    assert "DistilBERT contre TF-IDF + régression logistique : 6 messages" in obtenu["34"]
    assert "p = 0.125" in obtenu["34"]
    # Les modèles sont sauvegardés à côté du notebook, dans le dossier d'exécution
    sauvegardes = sorted(f.name for f in (tmp_path / "modeles").iterdir())
    assert sauvegardes == [
        "bigru.pt", "distilbert_ajuste", "resultats_test.csv", "textcnn.pt", "vocabulaire.pt",
    ]  # fmt: skip
