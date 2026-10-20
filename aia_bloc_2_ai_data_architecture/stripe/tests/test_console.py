# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Sortie console en UTF-8 : un flux ouvert dans la page de code de Windows passe en UTF-8.

Pourquoi : les commandes de la démonstration affichent des accents. Sur Windows, une sortie
redirigée garde la page de code locale (cp1252) ; sans `forcer_utf8`, ces accents sortiraient
mal codés. Le cas inverse compte autant : là où il n'y a rien à reconfigurer (pas de sortie
standard, flux en mémoire), la commande ne doit pas échouer.
"""

from __future__ import annotations

import io
import sys

from stripe_demo.console import forcer_utf8


def test_un_flux_ouvert_en_cp1252_passe_en_utf8(monkeypatch):
    octets = io.BytesIO()
    flux = io.TextIOWrapper(octets, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", flux)

    forcer_utf8()

    assert flux.encoding == "utf-8"
    print("é", end="")
    flux.flush()
    # « é » en UTF-8 : deux octets (c3 a9) ; en cp1252 : un seul (e9).
    assert octets.getvalue() == b"\xc3\xa9"


def test_un_flux_deja_en_utf8_reste_en_utf8(monkeypatch):
    flux = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", flux)

    forcer_utf8()

    assert flux.encoding == "utf-8"


def test_un_flux_en_memoire_reste_intact_et_ne_provoque_pas_d_erreur(monkeypatch):
    """`StringIO` n'est pas un fichier texte à encodage : rien à reconfigurer, rien ne casse."""
    flux = io.StringIO()
    monkeypatch.setattr(sys, "stdout", flux)

    forcer_utf8()

    print("accentué", end="")
    assert flux.getvalue() == "accentué"


def test_sans_sortie_standard_l_appel_est_sans_effet(monkeypatch):
    """`pythonw.exe` (Windows) démarre sans sortie standard : `sys.stdout` vaut `None`."""
    monkeypatch.setattr(sys, "stdout", None)

    forcer_utf8()  # ne doit rien lever

    assert sys.stdout is None
