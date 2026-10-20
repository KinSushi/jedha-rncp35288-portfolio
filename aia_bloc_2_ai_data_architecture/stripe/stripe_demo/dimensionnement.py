# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Extrapolation du dimensionnement : mesures réelles multipliées par des hypothèses déclarées.

Principe de la note de dimensionnement (livrables/aia2/09_...) : aucun volume n'est « deviné ».
- Les tailles d'une ligne, le volume de journal de transactions par paiement, la taille d'un fait
  en Parquet et d'un message JSON sont MESURÉS (``stripe_demo.mesures``) ;
- le volume de paiements par jour, le facteur de pointe et la durée de conservation en ligne sont
  des HYPOTHÈSES, passées en paramètres et affichées avec le résultat.
Le calcul est linéaire : doubler le volume de paiements double toutes les grandeurs. Il ne
prédit donc ni la contention, ni la fragmentation, ni les écarts de compression à grande échelle ;
il fixe un ordre de grandeur à valider par un banc d'essai.

Usage (depuis stripe/) : ``python -m stripe_demo.dimensionnement`` (lit
``../livrables/aia2/donnees/mesures_postgres.json`` et ``mesures_demo.json``).

CHOIX
-----
- Retenu : un calcul LINÉAIRE, des mesures lues dans deux fichiers JSON versionnés et trois
  hypothèses portées par un objet immuable (`Hypotheses`, dataclass gelée) qui est restitué avec
  le résultat. Une personne qui lit un chiffre voit tout de suite l'hypothèse qui l'a produit.
- Retenu : trois scénarios fixes (S1 100 000, S2 1 000 000, S3 10 000 000 paiements par jour,
  facteur de pointe 5, rétention 13 mois), ceux de la note 09 ; le code les nomme `SCENARIOS`.
- Écarté : coder en dur des volumes « plausibles ». La note interdit de présenter comme un fait
  ce qui est supposé : tout ce qui n'est pas mesuré est un paramètre.
- Écarté : interroger PostgreSQL à chaque calcul. Les mesures sont des fichiers livrés, donc le
  calcul se rejoue sans Docker ni serveur (la mesure elle-même est dans `stripe_demo/mesures.py`).
- Écarté : un modèle non linéaire (contention, fragmentation des index, compression). Aucune
  mesure ne le calibrerait ; le module le dit (« ordre de grandeur à valider par un banc
  d'essai »).

OÙ
---
- Place : couche « dimensionnement » de la démonstration, en aval de `stripe_demo/mesures.py`
  (qui produit les fichiers de mesures) et en amont de la note 09 (qui cite les chiffres).
- Appelants : la commande `python -m stripe_demo.dimensionnement` (fonction `main`) et les tests
  `tests/test_dimensionnement.py`.
- Dépendances : bibliothèque standard, plus `stripe_demo/console.py` pour la sortie en UTF-8.
  Entrées : `livrables/aia2/donnees/mesures_postgres.json` et `mesures_demo.json`.

COMMENT
-------
- `charger_mesures` lit les deux fichiers ; `octets_par_paiement_oltp` additionne le tas et les
  index de `paiements`, `remboursements` et `litiges` puis divise par le nombre de paiements (un
  remboursement ou un litige n'existe que pour une fraction des paiements : la mesure les
  rapporte au paiement) ; `extrapoler` multiplie chaque mesure par le volume quotidien ;
  `gio` convertit en gibioctets (2^30) arrondis à deux décimales ; `main` affiche les trois
  scénarios.
- Ce qui le prouve (`tests/test_dimensionnement.py`) :
  `test_l_extrapolation_est_exacte_et_laisse_les_mesures_inchangees` (arithmétique exacte de
  toutes les clés), `test_le_calcul_est_lineaire_en_volume`,
  `test_les_chiffres_de_la_note_09_sont_ceux_du_calcul_et_une_ligne_absente_est_refusee` (le
  tableau de la note 09 est relu et comparé), `test_la_commande_affiche_les_trois_scenarios`
  (texte exact), et côté refus : `test_des_mesures_absentes_ou_illisibles_sont_refusees` et
  `test_une_mesure_manquante_ou_nulle_arrete_le_calcul` (division par zéro paiement, mesure
  manquante).

POURQUOI
--------
- Les hypothèses H1 à H15 et la règle « on part de mesures, on les multiplie par des hypothèses
  nommées » viennent de `livrables/aia2/09_dimensionnement_finops_greenops.md:4` et `:41-57`.
- Les volumes extrapolés, leur caractère linéaire et leur limite (« ordres de grandeur à valider
  par un banc d'essai ») sont à `livrables/aia2/09_dimensionnement_finops_greenops.md:59-73`.
- Les mesures qui alimentent le calcul sont à `livrables/aia2/donnees/mesures_postgres.json:1-10`
  et `livrables/aia2/donnees/mesures_demo.json:1-12`.
- Le choix de 365 / 12 jours par mois (moyenne annuelle) : convention de calcul ; il n'est pas
  discuté dans la note 09.

JOURNALISATION
--------------
- Rien n'est journalisé (pas de module `logging`) : le module est un calcul pur et une commande
  d'affichage. Un fichier de mesures absent ou illisible lève l'exception de lecture
  (`FileNotFoundError`, `json.JSONDecodeError`) sans la masquer ; la commande affiche ses
  résultats sur la sortie standard, ligne par ligne, sans niveau.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stripe_demo.console import forcer_utf8

SECONDES_PAR_JOUR = 86_400
JOURS_PAR_MOIS = 365 / 12  # moyenne annuelle : 30,4167 jours
DONNEES = Path(__file__).resolve().parent.parent.parent / "livrables" / "aia2" / "donnees"


@dataclass(frozen=True)
class Hypotheses:
    """Les trois paramètres qui ne sont pas des mesures."""

    nom: str
    paiements_par_jour: int  # H1 : volume quotidien moyen
    facteur_pointe: float  # H2 : débit de pointe / débit moyen
    retention_en_ligne_mois: int  # H4 : durée de conservation dans l'OLTP avant archivage


def charger_mesures(dossier: Path = DONNEES) -> dict[str, Any]:
    """Mesures réelles : tailles PostgreSQL (mesures_postgres) et démonstration (mesures_demo)."""
    postgres = json.loads((dossier / "mesures_postgres.json").read_text(encoding="utf-8"))
    demo = json.loads((dossier / "mesures_demo.json").read_text(encoding="utf-8"))
    return {"postgres": postgres, "demo": demo}


def octets_par_paiement_oltp(postgres: dict[str, Any]) -> float:
    """Octets d'OLTP (tas + index) par paiement, remboursements et litiges inclus.

    Un remboursement ou un litige n'existe que pour une fraction des paiements : on pondère sa
    taille par la fréquence MESURÉE dans le jeu de données (lignes / paiements).
    """
    tables = postgres["tables"]
    paiements = tables["paiements"]["lignes"]
    total = tables["paiements"]["total_octets"]
    for annexe in ("remboursements", "litiges"):
        total += tables[annexe]["total_octets"]
    return float(total / paiements)


def extrapoler(mesures: dict[str, Any], hypotheses: Hypotheses) -> dict[str, Any]:
    """Grandeurs dérivées pour un jeu d'hypothèses (octets, sauf indication contraire)."""
    postgres, demo = mesures["postgres"], mesures["demo"]
    n = hypotheses.paiements_par_jour
    paiements = postgres["tables"]["paiements"]
    index_par_paiement = paiements["index_octets"] / paiements["lignes"]
    oltp_par_jour = n * octets_par_paiement_oltp(postgres)
    jours_en_ligne = hypotheses.retention_en_ligne_mois * JOURS_PAR_MOIS
    tps_moyen = n / SECONDES_PAR_JOUR
    return {
        "hypotheses": asdict(hypotheses),
        "debit_moyen_paiements_s": tps_moyen,
        "debit_pointe_paiements_s": tps_moyen * hypotheses.facteur_pointe,
        "oltp_octets_par_jour": oltp_par_jour,
        "oltp_en_ligne_octets": oltp_par_jour * jours_en_ligne,
        # Ensemble « chaud » : index des paiements du dernier mois, à loger en mémoire cache.
        "index_paiements_dernier_mois_octets": n * JOURS_PAR_MOIS * index_par_paiement,
        "wal_octets_par_jour": n * postgres["wal_octets"] / postgres["paiements"],
        "kafka_octets_par_jour": n * demo["json_paiement_octets_moyens"],
        "lac_octets_par_jour": n * demo["parquet_octets_par_fait"],
        "lac_octets_par_an": n * demo["parquet_octets_par_fait"] * 365,
    }


def gio(octets: float) -> float:
    """Octets -> gibioctets (2^30), arrondi à deux décimales pour l'affichage."""
    return round(octets / 2**30, 2)


SCENARIOS = (
    Hypotheses("S1", 100_000, 5.0, 13),
    Hypotheses("S2", 1_000_000, 5.0, 13),
    Hypotheses("S3", 10_000_000, 5.0, 13),
)


def main() -> None:
    forcer_utf8()
    mesures = charger_mesures()
    for scenario in SCENARIOS:
        r = extrapoler(mesures, scenario)
        print(f"{scenario.nom} : {scenario.paiements_par_jour:,} paiements/jour".replace(",", " "))
        print(f"  débit moyen {r['debit_moyen_paiements_s']:.2f}/s, "
              f"pointe {r['debit_pointe_paiements_s']:.2f}/s")
        print(f"  OLTP : {gio(r['oltp_octets_par_jour'])} Gio/jour, "
              f"{gio(r['oltp_en_ligne_octets'])} Gio en ligne, "
              f"index du dernier mois {gio(r['index_paiements_dernier_mois_octets'])} Gio")
        print(f"  WAL {gio(r['wal_octets_par_jour'])} Gio/jour ; Kafka "
              f"{gio(r['kafka_octets_par_jour'])} Gio/jour ; lac "
              f"{gio(r['lac_octets_par_jour'])} Gio/jour, {gio(r['lac_octets_par_an'])} Gio/an")


if __name__ == "__main__":
    main()
