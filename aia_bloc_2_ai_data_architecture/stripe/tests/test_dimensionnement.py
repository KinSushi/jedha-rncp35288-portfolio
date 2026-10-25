# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Extrapolation du dimensionnement : arithmétique exacte, cohérence avec les mesures, refus.

Un test de calcul ne prouve rien s'il n'a que des cas qui réussissent : chaque scénario ci-dessous
est accompagné de son contraire (mesure absente, division par zéro, entrée qu'on ne doit pas
modifier), et les chiffres affichés sont comparés à ceux de la note de dimensionnement.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stripe_demo import dimensionnement as d

RACINE = Path(__file__).resolve().parent.parent
NOTE_09 = RACINE.parent / "livrables" / "aia2" / "09_dimensionnement_finops_greenops.md"

MESURES = {
    "postgres": {
        "paiements": 1000,
        "wal_octets": 500_000,
        "tables": {
            "paiements": {"lignes": 1000, "total_octets": 300_000, "index_octets": 170_000},
            "remboursements": {"lignes": 30, "total_octets": 6_000, "index_octets": 1_000},
            "litiges": {"lignes": 20, "total_octets": 4_000, "index_octets": 1_000},
        },
    },
    "demo": {"json_paiement_octets_moyens": 300.0, "parquet_octets_par_fait": 20.0},
}

SORTIE_ATTENDUE = """\
S1 : 100 000 paiements/jour
  débit moyen 1.16/s, pointe 5.79/s
  OLTP : 0.03 Gio/jour, 11.31 Gio en ligne, index du dernier mois 0.48 Gio
  WAL 0.06 Gio/jour ; Kafka 0.03 Gio/jour ; lac 0.0 Gio/jour, 0.62 Gio/an
S2 : 1 000 000 paiements/jour
  débit moyen 11.57/s, pointe 57.87/s
  OLTP : 0.29 Gio/jour, 113.07 Gio en ligne, index du dernier mois 4.8 Gio
  WAL 0.6 Gio/jour ; Kafka 0.28 Gio/jour ; lac 0.02 Gio/jour, 6.22 Gio/an
S3 : 10 000 000 paiements/jour
  débit moyen 115.74/s, pointe 578.70/s
  OLTP : 2.86 Gio/jour, 1130.67 Gio en ligne, index du dernier mois 47.96 Gio
  WAL 6.02 Gio/jour ; Kafka 2.82 Gio/jour ; lac 0.17 Gio/jour, 62.21 Gio/an
"""


def test_l_extrapolation_est_exacte_et_laisse_les_mesures_inchangees():
    """Chaque grandeur, avec des volumes tous différents : un mauvais nom de clé ou une mauvaise
    opération se verrait. Les mesures données ne sont ni modifiées ni réutilisées."""
    mesures = copy.deepcopy(MESURES)

    # Taille d'un paiement : (300 000 + 6 000 + 4 000) octets pour 1 000 paiements.
    assert d.octets_par_paiement_oltp(mesures["postgres"]) == pytest.approx(310.0)

    # Un exemple à la main : 86 400 paiements par jour = 1 paiement par seconde.
    exemple = d.extrapoler(mesures, d.Hypotheses("X", 86_400, 5.0, 12))
    assert exemple["debit_moyen_paiements_s"] == pytest.approx(1.0)
    assert exemple["debit_pointe_paiements_s"] == pytest.approx(5.0)
    assert exemple["oltp_octets_par_jour"] == pytest.approx(86_400 * 310.0)
    assert exemple["oltp_en_ligne_octets"] == pytest.approx(86_400 * 310.0 * 365)  # 12 mois
    assert exemple["wal_octets_par_jour"] == pytest.approx(86_400 * 500.0)
    assert exemple["kafka_octets_par_jour"] == pytest.approx(86_400 * 300.0)
    assert exemple["lac_octets_par_an"] == pytest.approx(86_400 * 20.0 * 365)
    assert exemple["index_paiements_dernier_mois_octets"] == pytest.approx(
        86_400 * (365 / 12) * 170.0)

    # Toutes les clés du résultat, hypothèses restituées comprises.
    complet = d.extrapoler(mesures, d.Hypotheses("X", 172_800, 3.0, 6))
    assert complet == {
        "hypotheses": {"nom": "X", "paiements_par_jour": 172_800, "facteur_pointe": 3.0,
                       "retention_en_ligne_mois": 6},
        "debit_moyen_paiements_s": pytest.approx(2.0),
        "debit_pointe_paiements_s": pytest.approx(6.0),
        "oltp_octets_par_jour": pytest.approx(172_800 * 310.0),
        "oltp_en_ligne_octets": pytest.approx(172_800 * 310.0 * 6 * 365 / 12),
        "index_paiements_dernier_mois_octets": pytest.approx(172_800 * (365 / 12) * 170.0),
        "wal_octets_par_jour": pytest.approx(172_800 * 500.0),
        "kafka_octets_par_jour": pytest.approx(172_800 * 300.0),
        "lac_octets_par_jour": pytest.approx(172_800 * 20.0),
        "lac_octets_par_an": pytest.approx(172_800 * 20.0 * 365),
    }
    assert mesures == MESURES


def test_le_calcul_est_lineaire_en_volume():
    """Multiplier le volume par 10 multiplie chaque grandeur par 10 (note 09, § 3), et les
    mesures versionnées donnent des ordres de grandeur cohérents."""
    petit = d.extrapoler(MESURES, d.Hypotheses("a", 1_000, 5.0, 13))
    grand = d.extrapoler(MESURES, d.Hypotheses("b", 10_000, 5.0, 13))
    for cle, valeur in petit.items():
        if cle != "hypotheses":
            assert grand[cle] == pytest.approx(10 * valeur), cle

    mesures = d.charger_mesures()
    s1, s2, s3 = (d.extrapoler(mesures, h) for h in d.SCENARIOS)
    assert s2["oltp_octets_par_jour"] == pytest.approx(10 * s1["oltp_octets_par_jour"])
    assert s3["oltp_octets_par_jour"] == pytest.approx(100 * s1["oltp_octets_par_jour"])
    # Une ligne de paiement fait quelques centaines d'octets, index compris (mesure PostgreSQL).
    assert 100 < d.octets_par_paiement_oltp(mesures["postgres"]) < 1000


def test_la_conversion_en_gibioctets_arrondit_et_les_scenarios_sont_ceux_de_la_note():
    assert d.gio(2**30) == 1.0
    assert d.gio(3 * 2**29) == 1.5
    assert d.gio(1.23456 * 2**30) == 1.23
    assert d.gio(1.239 * 2**30) == 1.24
    assert d.gio(0) == 0.0
    assert [(h.nom, h.paiements_par_jour, h.facteur_pointe, h.retention_en_ligne_mois)
            for h in d.SCENARIOS] == [
        ("S1", 100_000, 5.0, 13), ("S2", 1_000_000, 5.0, 13), ("S3", 10_000_000, 5.0, 13)]


def test_les_mesures_sont_lues_en_utf8_explicite(tmp_path, lecture_ecriture_en_utf8):
    """Un caractère accentué du fichier ne dépend pas de la page de code de la machine.

    La fixture `lecture_ecriture_en_utf8` refuse toute lecture sans l'encodage « utf-8 » écrit
    exactement ainsi : sous Windows, une lecture sans encodage explicite utiliserait cp1252.
    """
    (tmp_path / "mesures_postgres.json").write_text('{"serveur": "é"}', encoding="utf-8")
    (tmp_path / "mesures_demo.json").write_text('{"note": "ç"}', encoding="utf-8")

    assert d.charger_mesures(tmp_path) == {"postgres": {"serveur": "é"}, "demo": {"note": "ç"}}
    lectures = [o for o in lecture_ecriture_en_utf8 if o["mode"] == "r"]
    assert [Path(o["fichier"]).name for o in lectures] == [
        "mesures_postgres.json", "mesures_demo.json"]


def test_la_commande_affiche_les_trois_scenarios(capsys):
    d.main()
    dans_le_processus = capsys.readouterr().out

    resultat = subprocess.run(
        [sys.executable, "-m", "stripe_demo.dimensionnement"], cwd=RACINE, capture_output=True,
        check=False, timeout=60, env={**os.environ, "PYTHONPATH": str(RACINE)})

    assert dans_le_processus == SORTIE_ATTENDUE
    assert resultat.returncode == 0, resultat.stderr.decode("utf-8", "replace")
    assert resultat.stdout.decode("utf-8").replace("\r\n", "\n") == SORTIE_ATTENDUE


def _nombre(cellule: str) -> float:
    """« 1 130,7 » (espace insécable ou fine, virgule décimale) -> 1130.7."""
    return float(re.sub(r"\s", "", cellule).replace(",", "."))


def _ligne_du_tableau(texte: str, debut: str) -> list[float]:
    for ligne in texte.splitlines():
        if ligne.startswith(f"| {debut}"):
            return [_nombre(cellule) for cellule in ligne.strip("|").split("|")[1:]]
    raise AssertionError(f"ligne « {debut} » absente de la note de dimensionnement")


def test_les_chiffres_de_la_note_09_sont_ceux_du_calcul_et_une_ligne_absente_est_refusee():
    """Le tableau « Volumes extrapolés » de la note 09 reprend ce que calcule le code."""
    note = NOTE_09.read_text(encoding="utf-8")
    mesures = d.charger_mesures()
    resultats = [d.extrapoler(mesures, h) for h in d.SCENARIOS]

    # (début de la ligne dans la note, grandeur calculée, décimales affichées par la note)
    lignes = [
        ("Débit moyen", lambda r: r["debit_moyen_paiements_s"], 2),
        ("Débit de pointe", lambda r: r["debit_pointe_paiements_s"], 2),
        ("OLTP : croissance par jour", lambda r: d.gio(r["oltp_octets_par_jour"]), 2),
        ("OLTP : volume en ligne", lambda r: r["oltp_en_ligne_octets"] / 2**30, 1),
        ("Index des paiements du dernier mois",
         lambda r: d.gio(r["index_paiements_dernier_mois_octets"]), 2),
        ("Journal de transactions (WAL)", lambda r: d.gio(r["wal_octets_par_jour"]), 2),
        ("Flux Kafka par jour", lambda r: d.gio(r["kafka_octets_par_jour"]), 2),
        ("Lac : faits en Parquet par an", lambda r: d.gio(r["lac_octets_par_an"]), 2),
    ]
    for debut, grandeur, decimales in lignes:
        attendu = [round(grandeur(r), decimales) for r in resultats]
        assert _ligne_du_tableau(note, debut) == pytest.approx(attendu, abs=10**-decimales / 2), (
            debut)
    with pytest.raises(AssertionError):
        _ligne_du_tableau(note, "Cette grandeur n'existe pas")


def test_des_mesures_absentes_ou_illisibles_sont_refusees(tmp_path):
    with pytest.raises(FileNotFoundError):
        d.charger_mesures(tmp_path)

    (tmp_path / "mesures_postgres.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):  # une seule des deux mesures
        d.charger_mesures(tmp_path)

    (tmp_path / "mesures_postgres.json").write_text("pas du json", encoding="utf-8")
    (tmp_path / "mesures_demo.json").write_text("{}", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        d.charger_mesures(tmp_path)


def test_une_mesure_manquante_ou_nulle_arrete_le_calcul():
    sans_paiement = copy.deepcopy(MESURES)
    sans_paiement["postgres"]["tables"]["paiements"]["lignes"] = 0
    sans_wal = copy.deepcopy(MESURES)
    del sans_wal["postgres"]["wal_octets"]
    sans_annexe = copy.deepcopy(MESURES["postgres"])
    del sans_annexe["tables"]["litiges"]

    with pytest.raises(ZeroDivisionError):  # diviser par zéro paiement n'a pas de sens
        d.octets_par_paiement_oltp(sans_paiement["postgres"])
    with pytest.raises(KeyError):
        d.extrapoler(sans_wal, d.SCENARIOS[0])
    with pytest.raises(KeyError):
        d.octets_par_paiement_oltp(sans_annexe)


def test_les_hypotheses_ne_se_modifient_pas_apres_creation():
    hypotheses = d.Hypotheses("S", 100, 5.0, 13)

    with pytest.raises(dataclasses.FrozenInstanceError):
        hypotheses.paiements_par_jour = 999

    assert hypotheses.paiements_par_jour == 100
