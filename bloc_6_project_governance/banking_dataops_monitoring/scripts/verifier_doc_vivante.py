#!/usr/bin/env python3
"""Check that every Python module of the project documents itself with five sections, and that its citations exist.

The five sections of a module docstring are CHOIX (options retained and rejected, with the reason), OÙ (role,
callers, dependencies), COMMENT (how it works, and the tests that prove it), POURQUOI (each reason with a
source), and JOURNALISATION (what is logged, at which level). This script is the mechanical half of that rule:
it judges the presence of the sections and the existence of what is cited. It cannot judge whether the text is
true; that stays a review task.

CHOIX
-----
Retained: the Python standard library only (``ast``, ``re``, ``pathlib``), so the check runs anywhere the tests
run. A section is a title line followed by a line of three or more dashes; the first word of the title,
lower-cased and stripped of accents, names the section. A section with an empty body counts as absent. Every
``path:line`` citation must point to an existing file and to a line inside it; every ``ADR-NNNN`` must have its file in
``docs/adr/``; every backticked ``test_*`` name must be a test function defined under ``tests/``; and no
unresolved citation marker may remain. ``source : inconnue`` is accepted as an honest source.
Rejected: judging the prose (not mechanical), a count threshold (it would hide which module is missing), and
checking cited files that the pipeline generates (``data/``, ``models/``, ``output/``), which do not exist in a
fresh checkout.

OÙ
---
Role: documentation gate for the Python code outside ``tests/`` (``src/``, ``scripts/``, ``tools/``,
``dashboard/``, ``huggingface-space/`` and ``run_pipeline.py``, the ``DEFAULT_ZONES``). Callers:
``python scripts/verifier_doc_vivante.py`` by hand or in CI, and ``tests/test_doc_vivante.py``. Depends on the
standard library only. Reads the Python files of the zones, ``docs/adr/`` and ``tests/``.

COMMENT
-------
``check_tree`` walks the Python files of the zones, skips ``__pycache__`` and the package files that hold no code,
and calls ``check_file`` on each. ``check_file`` parses the file, reads the module docstring, splits it into
sections with ``split_sections``, then checks the citations with ``check_citations``. The command line prints one
line per finding and exits 1 if there is any.
Tests that prove it: tests/test_doc_vivante.py.

POURQUOI
--------
- The five sections and the rule that every reason cites a source, or says ``source : inconnue``, are the
  living-documentation standard in five sections (CHOIX, OÙ, COMMENT, POURQUOI, JOURNALISATION) that is
  applied to this repository: the checker enforces it, it does not define it.
- Package files with no code are exempt (an empty ``__init__.py`` has nothing to document); a file with only
  a docstring, imports and ``__all__`` is exempt for the same reason.
- ``source : inconnue`` is counted as a source, not as a gap to hide: it tells the reader where the evidence stops.

JOURNALISATION
--------------
Entry point: findings are printed to standard output, one per line, and the exit code carries the verdict. The
library functions return findings and log nothing.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Section name (first word of the title, lower-case, accents removed) -> canonical name.
SECTION_NAMES = {
    "choix": "CHOIX",
    "ou": "OU",
    "comment": "COMMENT",
    "pourquoi": "POURQUOI",
    "journalisation": "JOURNALISATION",
}
REQUIRED = ("CHOIX", "OU", "COMMENT", "POURQUOI", "JOURNALISATION")

#: The zones checked by default: every Python file of the project outside ``tests/``. A zone is a directory or a file.
DEFAULT_ZONES = ("src", "scripts", "tools", "dashboard", "huggingface-space", "run_pipeline.py")

TITLE_UNDERLINE = re.compile(r"^[=-]{3,}$")
PATH_LINE = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.(?:py|md|sql|yml|toml|sh|ps1|json|txt))(?::(\d+)(?:-(\d+))?)?"
)
ADR = re.compile(r"\bADR-(\d{4})\b")
TEST_NAME = re.compile(r"``(test_\w+)``")
MARKER = re.compile(r"@@[^@]+@@")
#: Directories whose files are produced by running the project, so they are absent from a fresh checkout.
GENERATED_DIRS = ("data/", "models/", "output/")


@dataclass(frozen=True)
class Finding:
    """One problem found in one file."""

    path: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.kind}: {self.detail}"


def _normalise(word: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", word.lower()) if unicodedata.category(c) != "Mn")
    return plain.rstrip(",.:;!?")


def split_sections(docstring: str) -> dict[str, list[str]]:
    """Split a docstring into its recognised sections: ``{canonical name: body lines}``."""
    lines = docstring.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if i + 1 < len(lines) and TITLE_UNDERLINE.match(lines[i + 1].rstrip()) and line.strip():
            name = SECTION_NAMES.get(_normalise(line.strip().split()[0]))
            i += 2
            if name:
                current = name
                sections.setdefault(name, [])
            else:
                current = None
            continue
        if current is not None:
            sections[current].append(line)
        i += 1
    return sections


def check_citations(docstring: str, root: Path) -> list[tuple[str, str]]:
    """Return ``(kind, detail)`` for each citation of a docstring that does not resolve."""
    problems: list[tuple[str, str]] = []
    marker = MARKER.search(docstring)
    if marker:
        problems.append(("unresolved-marker", marker.group(0)))
    for match in PATH_LINE.finditer(docstring):
        relative, first, last = match.group(1), match.group(2), match.group(3)
        if "*" in relative or relative.startswith(GENERATED_DIRS):
            continue
        target = root / relative
        if not target.is_file():
            target = root / "src" / relative
        if not target.is_file():
            problems.append(("missing-file", relative))
            continue
        if first:
            count = len(target.read_text(encoding="utf-8").splitlines())
            for number in (first, last):
                if number and not 1 <= int(number) <= count:
                    problems.append(("line-out-of-range", f"{relative}:{number} (file has {count} lines)"))
    for adr in ADR.findall(docstring):
        if not list((root / "docs" / "adr").glob(f"{adr}-*.md")):
            problems.append(("missing-adr", f"ADR-{adr}"))
    defined = _defined_tests(root)
    for name in TEST_NAME.findall(docstring):
        if name not in defined:
            problems.append(("missing-test", name))
    return problems


_TEST_CACHE: dict[Path, set[str]] = {}


def _defined_tests(root: Path) -> set[str]:
    if root not in _TEST_CACHE:
        names: set[str] = set()
        for path in (root / "tests").rglob("*.py"):
            names.update(re.findall(r"^\s*def (test_\w+)", path.read_text(encoding="utf-8"), flags=re.M))
        _TEST_CACHE[root] = names
    return _TEST_CACHE[root]


def _holds_no_code(tree: ast.Module) -> bool:
    """True for a module whose body is only a docstring, imports and ``__all__``."""
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        return False
    return True


def check_file(path: Path, root: Path = ROOT) -> list[Finding]:
    """Return the findings for one Python file; an empty list means it conforms."""
    relative = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [Finding(relative, "unreadable", str(exc))]
    if path.name == "__init__.py" and _holds_no_code(tree):
        return []
    docstring = (ast.get_docstring(tree) or "").strip()
    if not docstring:
        return [Finding(relative, "no-docstring", "the module has no docstring")]
    findings: list[Finding] = []
    sections = split_sections(docstring)
    for name in REQUIRED:
        if name not in sections or not any(line.strip() for line in sections[name]):
            findings.append(Finding(relative, "missing-section", name))
    for kind, detail in check_citations(docstring, root):
        findings.append(Finding(relative, kind, detail))
    return findings


def _python_files(root: Path, zones: tuple[str, ...]) -> list[Path]:
    """The Python files of the zones, a zone being a directory (searched recursively) or a single file."""
    files: list[Path] = []
    for zone in zones:
        target = root / zone
        found = [target] if target.is_file() else sorted(target.rglob("*.py"))
        files.extend(path for path in found if "__pycache__" not in path.parts)
    return files


def check_tree(root: Path = ROOT, zones: tuple[str, ...] = DEFAULT_ZONES) -> list[Finding]:
    """Check every Python file of the zones and return all findings."""
    findings: list[Finding] = []
    for path in _python_files(root, zones):
        findings.extend(check_file(path, root))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: print the findings, exit 1 if there are any."""
    parser = argparse.ArgumentParser(description="Check the living documentation of the project's Python modules.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("zones", nargs="*", default=list(DEFAULT_ZONES))
    args = parser.parse_args(argv)
    findings = check_tree(args.root, tuple(args.zones))
    for finding in findings:
        print(finding)
    files = len(_python_files(args.root, tuple(args.zones)))
    print(f"{files} files checked, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
