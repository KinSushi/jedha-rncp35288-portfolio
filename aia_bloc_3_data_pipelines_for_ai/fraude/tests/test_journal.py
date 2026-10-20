# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Journaux d'audit structurés (critère 6.3) et défense en profondeur contre les fuites."""

from __future__ import annotations

import json
import logging

from detection_fraude.journal import (
    FormatJson,
    configurer_journal,
    info,
    nettoyer,
    obtenir_journal,
)


def test_nettoyer_masque_les_champs_sensibles_et_les_numeros_de_carte():
    donnees = {"cc_num": 4929123412341234, "montant": 12.5, "note": "carte 4929123412341234 vue"}
    propre = nettoyer(donnees)
    assert propre["cc_num"] == "[MASQUE]"
    assert propre["montant"] == 12.5
    assert "4929123412341234" not in propre["note"]


def test_nettoyer_est_recursif():
    propre = nettoyer({"a": {"first": "Jean", "liste": [{"street": "x"}, "4929123412341234"]}})
    assert propre["a"]["first"] == "[MASQUE]"
    assert propre["a"]["liste"][0]["street"] == "[MASQUE]"
    assert "4929123412341234" not in json.dumps(propre)


def test_nettoyer_laisse_un_identifiant_de_transaction_intact():
    identifiant = "c7e1d0a94f3b42e8a65d1f0b9e2c7a14"
    assert nettoyer({"trans_num": identifiant})["trans_num"] == identifiant


def test_une_ligne_json_par_evenement_avec_champs_normalises(tmp_path):
    fichier = tmp_path / "audit.jsonl"
    configurer_journal("INFO", "json", fichier)
    journal = obtenir_journal("test")
    info(journal, "evenement_test", lot="abc", cc_num=4929123412341234, duree_ms=12)
    for gestionnaire in logging.getLogger("fraude").handlers:
        gestionnaire.flush()
    lignes = fichier.read_text(encoding="utf-8").strip().splitlines()
    assert len(lignes) == 1
    evenement = json.loads(lignes[0])
    assert evenement["evenement"] == "evenement_test"
    assert evenement["niveau"] == "INFO"
    assert evenement["composant"] == "test"
    assert evenement["lot"] == "abc" and evenement["duree_ms"] == 12
    assert evenement["cc_num"] == "[MASQUE]"
    assert evenement["horodatage"].endswith("+00:00")
    assert "4929123412341234" not in lignes[0]


def test_le_format_json_range_les_exceptions_sans_donnee_sensible():
    try:
        raise ValueError("carte 4929123412341234 refusée")
    except ValueError:
        import sys

        enregistrement = logging.LogRecord(
            "fraude.t", logging.ERROR, __file__, 1, "echec", None, sys.exc_info()
        )
    texte = FormatJson().format(enregistrement)
    assert "4929123412341234" not in texte
    assert json.loads(texte)["exception"]
