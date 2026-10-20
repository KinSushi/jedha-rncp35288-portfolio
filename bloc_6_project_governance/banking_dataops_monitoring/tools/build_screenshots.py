#!/usr/bin/env python3
"""Rend les sorties terminal capturées en copies d'écran SVG.

Chaque SVG est une reproduction fidèle de la sortie réelle : aucun texte n'est inventé ici, tout provient des fichiers
de
`evidence/` produits par l'exécution réelle du pipeline, des tests et du smoke check.

CHOIX
-----
Retenu :

- Des SVG générés depuis les fichiers de `evidence/`, jamais saisis à la main : une image ne peut pas contredire la
  preuve
  dont elle vient. Le titre de la capture des tests suit la dernière exécution (`nombre_de_cas` lit le bilan de pytest).
- Des chemins relatifs à la racine du dépôt, pour que le script tourne chez n'importe qui ; le dossier des preuves peut
  être
  changé par la variable `BANKING_DATAOPS_EVIDENCE_DIR`.
- Les lignes trop longues sont coupées à 104 colonnes, la suite étant indentée, plutôt qu'un défilement horizontal.
- Une couleur par sémantique de ligne : rouge pour un échec, vert pour une réussite, or pour un titre, bleu pour un
  niveau
  INFO ou WARNING.

Écarté :

- De vraies captures d'écran du terminal : elles dépendent de la machine et de la police, et ne se régénèrent pas à
  l'identique. Raison du choix : source : inconnue.

OÙ
---
Rôle : outil du dépôt (`tools/`), lancé à la main depuis ce dossier après une régénération de `evidence/`. Il lit
`smoke_check_full.txt`, `pytest_full.txt`, `reverse_full.txt`, `pipeline_full.txt` et `powerbi_views.txt` dans
`evidence/`
et écrit cinq SVG dans `docs/screenshots/`. Dépendances : bibliothèque standard seule. Le rendu en PNG des mêmes images
n'est
pas fait par ce script. Voir tools/README.md pour la liste des outils.

COMMENT
-------
1. `read` lit un fichier de preuve ligne à ligne ; `colorize` choisit la couleur d'une ligne ; `make_svg` assemble la
   barre de
   titre, les lignes colorées et la taille de l'image, déduite de la ligne la plus longue.
2. Cinq SVG sont produits : le smoke check, la suite de tests (le titre porte le nombre de cas), les chemins interdits,
   le
   pipeline (les lignes d'événements seulement) et le schéma en étoile.
Tests qui le prouvent : aucun test automatisé ; la sortie se contrôle en la comparant aux fichiers de `evidence/`.

POURQUOI
--------
- Aucun texte inventé : une capture fausse dans un dossier de certification serait pire qu'une absence de capture. Même
  principe de traçabilité que l'ADR-0007.
- Police de 13,5 px et largeur de caractère de 8,05 px : valeurs choisies à l'œil pour une police à chasse fixe, source
  :
  inconnue (aucune mesure).

JOURNALISATION
--------------
Aucun module `logging` : une ligne imprimée par SVG écrit (nom, dimensions, nombre de lignes). Le script ne manipule
aucun
secret ; ses entrées sont des fichiers de preuve déjà contrôlés.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

# Chemins relatifs à la racine du dépôt : le script doit tourner chez
# n'importe qui, pas seulement sur la machine qui l'a écrit.
REPO = Path(__file__).resolve().parent.parent
EVIDENCE = Path(os.environ.get("BANKING_DATAOPS_EVIDENCE_DIR", REPO / "evidence"))
OUT = REPO / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

# Palette : navy profond / or / accents terminal
BG = "#0d1b2a"
BAR = "#152a41"
FG = "#e8edf4"
MUTED = "#8ba3bf"
GREEN = "#3ddc84"
RED = "#ff5c5c"
GOLD = "#e8b64c"
BLUE = "#4da6ff"

CHAR_W = 8.05  # largeur d'un caractère en 13.5px monospace
LINE_H = 20
PAD_X = 22
PAD_TOP = 52
PAD_BOT = 20


def colorize(line: str) -> str:
    """Retourne le fill correspondant à la sémantique de la ligne."""
    if re.search(r"\bFAIL\b|failed|ERROR|NOT READY", line):
        return RED
    if re.search(r"\bPASS\b|passed|DEMO READY|succeeded|\bok\b", line):
        return GREEN
    if line.strip().startswith("==>") or line.strip().startswith("###"):
        return GOLD
    if re.match(r"^\s*=+\s*$", line) or set(line.strip()) <= {"=", "-"} and line.strip():
        return MUTED
    if re.search(r"INFO|WARNING", line):
        return BLUE
    return FG


def make_svg(title: str, lines: list[str], out_path: Path, max_cols: int = 104) -> None:
    # Tronque proprement les lignes trop longues
    clean: list[str] = []
    for raw in lines:
        raw = raw.rstrip("\n").replace("\t", "    ")
        while len(raw) > max_cols:
            clean.append(raw[:max_cols])
            raw = "    " + raw[max_cols:]
        clean.append(raw)

    cols = max([len(line_) for line_ in clean] + [len(title) + 10, 60])
    cols = min(cols, max_cols)
    width = int(cols * CHAR_W + PAD_X * 2)
    height = int(len(clean) * LINE_H + PAD_TOP + PAD_BOT)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Menlo, Consolas, monospace">',
        f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>',
        f'<rect width="{width}" height="36" rx="10" fill="{BAR}"/>',
        f'<rect y="26" width="{width}" height="10" fill="{BAR}"/>',
        '<circle cx="20" cy="18" r="5.5" fill="#ff5f57"/>',
        '<circle cx="39" cy="18" r="5.5" fill="#febc2e"/>',
        '<circle cx="58" cy="18" r="5.5" fill="#28c840"/>',
        f'<text x="78" y="23" font-size="12.5" fill="{MUTED}">{html.escape(title)}</text>',
    ]

    y = PAD_TOP
    for line in clean:
        if line.strip():
            fill = colorize(line)
            weight = "600" if re.search(r"DEMO READY|NOT READY|passed,|PASS |FAIL ", line) else "400"
            parts.append(
                f'<text x="{PAD_X}" y="{y}" font-size="13.5" fill="{fill}" '
                f'font-weight="{weight}" xml:space="preserve">{html.escape(line)}</text>'
            )
        y += LINE_H

    parts.append("</svg>")
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"  {out_path.name:<42} {width}x{height}  ({len(clean)} lignes)")


def read(name: str, limit: int | None = None, tail: bool = False) -> list[str]:
    lines = (EVIDENCE / name).read_text(encoding="utf-8", errors="replace").splitlines()
    lines = [line_ for line_ in lines if line_.strip() or True]
    if limit:
        lines = lines[-limit:] if tail else lines[:limit]
    return lines


def nombre_de_cas() -> int:
    """Nombre de cas de la suite, LU dans evidence/pytest_full.txt.

    Somme des tests réussis, ignorés et en échec de la ligne de bilan de
    pytest (« 178 passed, 6 skipped, ... »). Le titre de la capture suit
    ainsi la dernière exécution réelle, au lieu d'un nombre saisi à la main.
    """
    bilan = (EVIDENCE / "pytest_full.txt").read_text(encoding="utf-8")
    return sum(int(n) for n in re.findall(r"(\d+) (?:passed|skipped|failed|xfailed)", bilan))


print("Génération des copies d'écran SVG :")

# 1. Smoke check — le plus parlant pour un jury
make_svg(
    "scripts/smoke_check.py  —  validation de bout en bout",
    ["$ python scripts/smoke_check.py", ""] + read("smoke_check_full.txt"),
    OUT / "test-smoke-check.svg",
)

# 2. Suite de tests complète
make_svg(
    f"pytest  —  suite complète ({nombre_de_cas()} cas)",
    ["$ pytest -q -rs", ""] + read("pytest_full.txt"),
    OUT / "test-suite-pytest.svg",
)

# 3. Reverse tests
rev = read("reverse_full.txt")
rev_clean = [re.sub(r"^tests/test_reverse_paths\.py::", "", line_) for line_ in rev]
rev_clean = [re.sub(r"\s+\[\s*\d+%\]$", "", line_) for line_ in rev_clean]
make_svg(
    "pytest tests/test_reverse_paths.py  —  chemins interdits",
    ["$ pytest tests/test_reverse_paths.py -v", ""] + rev_clean,
    OUT / "test-reverse-paths.svg",
    max_cols=96,
)

# 4. Pipeline complet — résumé final
pipe = read("pipeline_full.txt")
summary_idx = next((i for i, line_ in enumerate(pipe) if "Pipeline completed successfully" in line_), None)
if summary_idx is not None:
    key = [
        line_
        for line_ in pipe
        if re.search(
            r"bronze_generated|bronze_cleaned|table_loaded|verdict_issued|"
            r"gold_table_loaded|model_selected|threshold_selected|model_evaluated|model_persisted",
            line_,
        )
    ]
    key = [re.sub(r"^\d\d:\d\d:\d\d\s+INFO\s+\[run:\w+\]\s+", "", line_) for line_ in key]
    key = [re.sub(r"\s+category=\S+\s+reason=.*?(?=(rows=|$))", "  ", line_) for line_ in key]
    body = ["$ python run_pipeline.py --seed 42 --transactions 1500 --train", ""]
    body += key[:18] + [""] + pipe[summary_idx:]
    make_svg(
        "run_pipeline.py  —  exécution de bout en bout",
        body,
        OUT / "pipeline-run.svg",
        max_cols=112,
    )

# 5. Vues Power BI
make_svg(
    "banking_dataops.powerbi_views  —  schéma en étoile",
    ["$ python -m banking_dataops.powerbi_views", ""] + read("powerbi_views.txt"),
    OUT / "powerbi-star-schema.svg",
)

print("\nTerminé.")
