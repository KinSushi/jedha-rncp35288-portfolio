# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Estimation FinOps : consommation mesurée x prix unitaires hypothétiques — critère 6.2.

« Combien coûte ce pipeline ? » se répond par une chaîne vérifiable : *consommation* (mesurée ou
déduite du volume) x *prix unitaire* (fourni par le fournisseur retenu). Le module sépare donc
strictement les deux :

* les **consommations** viennent de mesures (taille des lignes dans l'entrepôt, durée d'un
  entraînement, taille d'une réponse de l'API) ou d'une hypothèse de volume passée en argument ;
* les **prix unitaires** sont des paramètres (constante ``PRIX_HYPOTHETIQUES``). Les
  valeurs livrées sont des **hypothèses d'ordre de grandeur, pas une grille tarifaire** : elles
  doivent être remplacées par celles du fournisseur avant toute décision.

Usage : ``python -m detection_fraude.finops --tx-par-jour 1440``

CHOIX
-----
- Retenu : séparer strictement consommation (mesurée) et prix unitaire (hypothétique), pour que
  chaque nombre soit traçable à une mesure ou à une hypothèse déclarée.
- Retenu : les prix et les volumes sont des paramètres de la ligne de commande ; le résultat
  rappelle toujours que les prix sont hypothétiques.
- Retenu : le transfert entrant de l'API est compté à 0 € (hypothèse : le trafic entrant est
  généralement gratuit) et le transfert sortant des notifications repose sur 0,4 % d'alertes de
  1,5 Ko.
- Écarté : afficher un coût sans le détail des postes, et prétendre à une grille tarifaire.
- Non implémenté : le suivi continu des coûts par étiquetage des ressources du fournisseur
  (dépend du fournisseur ; voir le livrable cité plus bas).

OÙ
---
- Place : outil de décision hors chemin chaud, lancé à la main.
- Appelants : ligne de commande ``main`` et les tests.
- Dépendances : ``journal.configurer_journal`` seulement.

COMMENT
-------
1. ``estimer`` calcule le stockage de la file et de l'entrepôt, le transfert, les vCPU-heures
   permanentes (vCPU x 730 h) et d'entraînement (entraînements par mois x durée) puis le coût de
   chaque poste, arrondi, et le total mensuel.
2. ``main`` lit les options (valeurs par défaut mesurées : 1 440 transactions par jour,
   431 / 364 / 359 octets, 7,25 vCPU, 37,4 s d'entraînement) et imprime le résultat en JSON.
Tests qui le prouvent : test_finops_total_egal_somme_des_postes (tests/test_divers.py:29-34),
test_finops_le_cout_suit_les_prix_et_les_volumes (tests/test_divers.py:37-45),
test_estimer_applique_chaque_prix_au_bon_poste_sans_toucher_aux_prix_par_defaut
(tests/test_finops_local_garanties.py:115-131) et
test_une_consommation_oubliee_est_refusee_au_lieu_d_etre_devinee
(tests/test_finops_local_garanties.py:134-142).

POURQUOI
--------
- Méthode, consommations mesurées et estimation à 158,79 € par mois :
  livrables/aia3/04_exploitation.md:135-180.
- Constantes de prix : PRIX_HYPOTHETIQUES (detection_fraude/finops.py:78-82) (hypothèses
  déclarées).
- Épreuve du 11/10/2026 (trafic x100, prix du vCPU à 0,05 €) : compte rendu des épreuves non
  publié.
- Indicateur 6.2 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Le module configure la journalisation au niveau WARNING seulement (``configurer_journal``) et
n'écrit aucun événement : le résultat est imprimé sur la sortie standard au format JSON, sans
donnée personnelle.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from detection_fraude.journal import configurer_journal

HEURES_PAR_MOIS = 730.0
JOURS_PAR_MOIS = 30.0

PRIX_HYPOTHETIQUES = {
    "vcpu_heure_eur": 0.03,  # hypothèse : instance généraliste, par vCPU-heure
    "go_stockage_mois_eur": 0.10,  # hypothèse : stockage bloc, par Go et par mois
    "go_sortie_eur": 0.08,  # hypothèse : trafic sortant, par Go
}


def estimer(
    *,
    tx_par_jour: float,
    octets_message_file: float,
    octets_bronze_par_tx: float,
    octets_argent_par_tx: float,
    octets_reponse_api: float,
    appels_api_par_jour: float,
    retention_file_jours: float,
    retention_entrepot_jours: float,
    vcpu_services_permanents: float,
    duree_entrainement_s: float,
    entrainements_par_mois: float = 4.33,
    prix: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Consommations mensuelles et coût estimé, poste par poste."""
    p = {**PRIX_HYPOTHETIQUES, **(prix or {})}
    go = 1024**3
    stock_file_go = tx_par_jour * retention_file_jours * octets_message_file / go
    stock_entrepot_go = (
        tx_par_jour * retention_entrepot_jours * (octets_bronze_par_tx + octets_argent_par_tx) / go
    )
    sortie_api_go = appels_api_par_jour * JOURS_PAR_MOIS * octets_reponse_api / go
    vcpu_heures_permanent = vcpu_services_permanents * HEURES_PAR_MOIS
    vcpu_heures_entrainement = entrainements_par_mois * duree_entrainement_s / 3600
    postes: dict[str, dict[str, Any]] = {
        "calcul_services_permanents": {
            "consommation": round(vcpu_heures_permanent, 1),
            "unite": "vCPU-heure/mois",
            "cout_eur": round(vcpu_heures_permanent * p["vcpu_heure_eur"], 2),
        },
        "calcul_entrainement_hebdomadaire": {
            "consommation": round(vcpu_heures_entrainement, 3),
            "unite": "vCPU-heure/mois",
            "cout_eur": round(vcpu_heures_entrainement * p["vcpu_heure_eur"], 4),
        },
        "stockage_file": {
            "consommation": round(stock_file_go, 4),
            "unite": "Go (en continu)",
            "cout_eur": round(stock_file_go * p["go_stockage_mois_eur"], 4),
        },
        "stockage_entrepot": {
            "consommation": round(stock_entrepot_go, 4),
            "unite": "Go (en continu)",
            "cout_eur": round(stock_entrepot_go * p["go_stockage_mois_eur"], 4),
        },
        "transfert_api_entrant": {
            "consommation": round(sortie_api_go, 5),
            "unite": "Go/mois",
            "cout_eur": 0.0,
        },  # le trafic entrant est généralement gratuit : hypothèse
        "transfert_notifications_sortant": {
            "consommation": round(tx_par_jour * JOURS_PAR_MOIS * 0.004 * 1500 / go, 5),
            "unite": "Go/mois (hypothèse : 0,4 % d'alertes de 1,5 Ko)",
            "cout_eur": round(
                tx_par_jour * JOURS_PAR_MOIS * 0.004 * 1500 / go * p["go_sortie_eur"], 5
            ),
        },
    }
    return {
        "hypotheses_de_prix": p,
        "postes": postes,
        "total_mensuel_eur": round(sum(poste["cout_eur"] for poste in postes.values()), 2),
        "avertissement": (
            "Prix unitaires hypothétiques : à remplacer par la grille du fournisseur."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Estimation FinOps mensuelle.")
    analyseur.add_argument(
        "--tx-par-jour",
        type=float,
        default=1440.0,
        help="1440 = une transaction par minute (API en direct)",
    )
    analyseur.add_argument("--octets-message", type=float, default=431.0)
    analyseur.add_argument("--octets-bronze", type=float, default=364.0)
    analyseur.add_argument("--octets-argent", type=float, default=359.0)
    analyseur.add_argument("--octets-reponse-api", type=float, default=587.0)
    analyseur.add_argument("--appels-api-par-jour", type=float, default=1440.0)
    analyseur.add_argument("--retention-file-jours", type=float, default=7.0)
    analyseur.add_argument("--retention-entrepot-jours", type=float, default=90.0)
    analyseur.add_argument(
        "--vcpu",
        type=float,
        default=7.25,
        help="vCPU réservés aux services permanents (voir docker-compose.yml)",
    )
    analyseur.add_argument("--duree-entrainement-s", type=float, default=37.4)
    arguments = analyseur.parse_args(argv)
    configurer_journal("WARNING")
    resultat = estimer(
        tx_par_jour=arguments.tx_par_jour,
        octets_message_file=arguments.octets_message,
        octets_bronze_par_tx=arguments.octets_bronze,
        octets_argent_par_tx=arguments.octets_argent,
        octets_reponse_api=arguments.octets_reponse_api,
        appels_api_par_jour=arguments.appels_api_par_jour,
        retention_file_jours=arguments.retention_file_jours,
        retention_entrepot_jours=arguments.retention_entrepot_jours,
        vcpu_services_permanents=arguments.vcpu,
        duree_entrainement_s=arguments.duree_entrainement_s,
    )
    print(json.dumps(resultat, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
