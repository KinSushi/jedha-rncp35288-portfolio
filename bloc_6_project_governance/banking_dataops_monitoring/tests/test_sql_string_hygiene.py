"""Interdit les commentaires Python à l'intérieur des chaînes SQL.

POURQUOI CE TEST EXISTE
    Un bloc de commentaires écrit avec '#' à l'intérieur d'une chaîne SQL
    triple-quotée est du Python valide, il compile, il passe la revue de code :
    et PostgreSQL le rejette à l'exécution avec « syntax error at or near "#" ».

    Le défaut est resté invisible parce que le module concerné
    (``reconciliation``) n'est pas appelé par ``run_pipeline.py`` : seul
    l'entrypoint du conteneur l'exécute. En conteneur, le processus sortait en
    code 1 et redémarrait en boucle.

    PostgreSQL ne connaît que ``--`` et les blocs slash-étoile.

POURQUOI UN AST ET PAS UN GREP
    Un grep ne distingue pas un vrai commentaire Python d'un '#' situé dans une
    chaîne. Ce test ne regarde que des littéraux ``str`` repérés par l'AST, et
    ignore les docstrings : un exemple de code commenté y est légitime.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = [ROOT / "src", ROOT / "scripts", ROOT / "dashboard"]
ROOT_MODULES = [ROOT / "run_pipeline.py"]

# Un littéral n'est considéré comme du SQL que s'il contient un verbe SQL *et*
# un mot-clé de cible. Cela évite de traiter une phrase anglaise contenant
# « select » comme une requête.
SQL_VERB = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM"
    r"|CREATE\s+(TABLE|VIEW|INDEX|SCHEMA)|ALTER|TRUNCATE|DROP\s+(TABLE|VIEW)"
    r"|WITH|COPY)\b",
    re.IGNORECASE,
)
SQL_TARGET = ("FROM", "INTO", "TABLE", "VIEW")


def _looks_like_sql(text: str) -> bool:
    upper = text.upper()
    return bool(SQL_VERB.search(text)) and any(t in upper for t in SQL_TARGET)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Identité des littéraux qui sont des docstrings, à exclure."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _python_files() -> list[Path]:
    files: list[Path] = [p for p in ROOT_MODULES if p.exists()]
    for directory in SOURCE_DIRS:
        if directory.exists():
            files.extend(p for p in directory.rglob("*.py") if "__pycache__" not in str(p))
    return sorted(files)


def _offenders(path: Path) -> list[str]:
    """Retourne une description de chaque ligne fautive du fichier."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    problems: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        if not _looks_like_sql(node.value):
            continue
        for offset, line in enumerate(node.value.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                problems.append(f"{path.name}: chaîne SQL ligne {node.lineno}, +{offset} -> {line.strip()[:60]}")
    return problems


def _check_no_python_comment(path: Path) -> None:
    """Lève ``AssertionError`` si une chaîne SQL du fichier contient une ligne commençant par '#'."""
    problems = _offenders(path)
    assert not problems, (
        "Commentaire Python trouvé dans une chaîne SQL : PostgreSQL rejettera "
        "la requête à l'exécution. Utiliser '--' :\n  " + "\n  ".join(problems)
    )


@pytest.mark.limit(60)
def test_no_python_comment_inside_sql_string_and_the_detector_refuses_a_bad_one_and_accepts_a_sql_comment(
    tmp_path: Path,
) -> None:
    """Aucune chaîne SQL exécutée ne doit contenir de ligne commençant par '#'.

    Le détecteur est lui-même éprouvé : il doit refuser un cas fautif fabriqué et laisser passer un commentaire SQL
    '--'. Sans cela, une régression du détecteur rendrait la vérification des fichiers du projet verte par
    accident, et le garde-fou ne garderait plus rien.

    Limite de 60 s au lieu des 8 s de ``tests/conftest.py`` : le test analyse chaque fichier de ``src/``, et l'outil de
    mutation le lance sur une copie où chaque fonction figure dans toutes ses versions mutées ; il y a pris 6,25 s
    sur une machine peu chargée et dépassé 8 s sous charge, le 20/10/2026.
    """
    for path in _python_files():
        _check_no_python_comment(path)

    bad = tmp_path / "bad_module.py"
    bad.write_text(
        'QUERY = """\n# ceci est un commentaire Python dans du SQL\nSELECT id FROM transactions\n"""\n',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="Commentaire Python trouvé dans une chaîne SQL"):
        _check_no_python_comment(bad)
    assert _offenders(bad), "le détecteur a raté un cas fautif évident"

    good = tmp_path / "good_module.py"
    good.write_text(
        'QUERY = """\n-- ceci est un commentaire SQL valide\nSELECT id FROM transactions\n"""\n',
        encoding="utf-8",
    )
    _check_no_python_comment(good)  # un commentaire SQL '--' ne doit pas être signalé
    assert not _offenders(good), "le détecteur signale un commentaire SQL valide"
