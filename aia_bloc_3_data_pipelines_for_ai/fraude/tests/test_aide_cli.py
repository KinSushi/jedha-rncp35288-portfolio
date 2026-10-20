# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Aide de chaque ligne de commande (``--help``) : un texte d'interface, fixé mot pour mot.

L'aide est ce que l'exploitant lit avant de lancer une commande (démonstration, supervision,
réentraînement, retraitement). Un texte d'aide altéré, une option qui change de nom ou de type, une
valeur par défaut qui bouge sont donc des changements de contrat. Chaque test compare la sortie
complète de ``--help`` au texte attendu, écrit en dur ci-dessous (largeur fixée à 80 colonnes, nom
de programme fixé : l'aide ne dépend ni du terminal ni de la manière de lancer le test).
"""

from __future__ import annotations

import importlib
import sys

import pytest

CAS = [
    (
        "detection_fraude.finops",
        (),
        (
            "usage: fraude [-h] [--tx-par-jour TX_PAR_JOUR]",
            "              [--octets-message OCTETS_MESSAGE]",
            "              [--octets-bronze OCTETS_BRONZE] [--octets-argent OCTETS_ARGENT]",
            "              [--octets-reponse-api OCTETS_REPONSE_API]",
            "              [--appels-api-par-jour APPELS_API_PAR_JOUR]",
            "              [--retention-file-jours RETENTION_FILE_JOURS]",
            "              [--retention-entrepot-jours RETENTION_ENTREPOT_JOURS]",
            "              [--vcpu VCPU] [--duree-entrainement-s DUREE_ENTRAINEMENT_S]",
            "",
            "Estimation FinOps mensuelle.",
            "",
            "options:",
            "  -h, --help            show this help message and exit",
            "  --tx-par-jour TX_PAR_JOUR",
            "                        1440 = une transaction par minute (API en direct)",
            "  --octets-message OCTETS_MESSAGE",
            "  --octets-bronze OCTETS_BRONZE",
            "  --octets-argent OCTETS_ARGENT",
            "  --octets-reponse-api OCTETS_REPONSE_API",
            "  --appels-api-par-jour APPELS_API_PAR_JOUR",
            "  --retention-file-jours RETENTION_FILE_JOURS",
            "  --retention-entrepot-jours RETENTION_ENTREPOT_JOURS",
            "  --vcpu VCPU           vCPU réservés aux services permanents (voir docker-",
            "                        compose.yml)",
            "  --duree-entrainement-s DUREE_ENTRAINEMENT_S",
        ),
    ),
    (
        "detection_fraude.local",
        (),
        (
            "usage: fraude [-h] {demo,etat,preparer} ...",
            "",
            "Mode local sans Docker.",
            "",
            "positional arguments:",
            "  {demo,etat,preparer}",
            "    demo                rejoue le flux de bout en bout",
            "    etat                compte les lignes de chaque couche",
            "    preparer            télécharge le jeu et entraîne le modèle si besoin",
            "",
            "options:",
            "  -h, --help            show this help message and exit",
        ),
    ),
    (
        "detection_fraude.local",
        ("demo",),
        (
            "usage: fraude demo [-h] [--source {jeu,api}] [--jour JOUR] [--nombre NOMBRE]",
            "                   [--appels APPELS] [--intervalle INTERVALLE]",
            "                   [--cadence CADENCE] [--corruptions CORRUPTIONS]",
            "                   [--pannes PANNES] [--rebaser-sur REBASER_SUR]",
            "                   [--graine GRAINE] [--reinitialiser]",
            "",
            "options:",
            "  -h, --help            show this help message and exit",
            "  --source {jeu,api}",
            "  --jour JOUR           jeu : jour à rejouer (AAAA-MM-JJ)",
            "  --nombre NOMBRE       jeu : nombre de lignes ; sinon toute la journée",
            "  --appels APPELS       api : nombre d'appels",
            "  --intervalle INTERVALLE",
            "                        api : secondes entre deux appels",
            "  --cadence CADENCE     jeu : lignes par seconde (0 = max)",
            "  --corruptions CORRUPTIONS",
            "                        lignes corrompues injectées (démo)",
            "  --pannes PANNES       api : pannes réseau simulées (démo)",
            "  --rebaser-sur REBASER_SUR",
            "                        jeu : date (AAAA-MM-JJ) sur laquelle recaler le jour",
            "                        rejoué",
            "  --graine GRAINE",
            "  --reinitialiser       repart d'une file et d'un entrepôt vides",
        ),
    ),
    (
        "detection_fraude.telechargement",
        (),
        (
            "usage: fraude [-h] [--forcer]",
            "",
            "Télécharge le jeu public fraudTest.csv.",
            "",
            "options:",
            "  -h, --help  show this help message and exit",
            "  --forcer    retélécharge même si présent",
        ),
    ),
    (
        "detection_fraude.consommateur",
        (),
        (
            "usage: fraude [-h] [--jusqu-a-vide]",
            "",
            "Consommateur : file -> notation -> entrepôt.",
            "",
            "options:",
            "  -h, --help      show this help message and exit",
            "  --jusqu-a-vide  s'arrête quand la file est vidée (démonstration, tests)",
        ),
    ),
    (
        "detection_fraude.entrainement",
        (),
        (
            "usage: fraude [-h] [--donnees DONNEES] [--limite LIMITE] [--sans-mlflow]",
            "              [--promouvoir]",
            "",
            "Entraîne, exporte et promeut le modèle.",
            "",
            "options:",
            "  -h, --help         show this help message and exit",
            "  --donnees DONNEES  chemin du jeu (défaut : configuration)",
            "  --limite LIMITE    ne lire que les N premières lignes (essais)",
            "  --sans-mlflow      désactive le suivi MLflow",
            "  --promouvoir       après la barrière de qualité, met le candidat en service",
        ),
    ),
    (
        "detection_fraude.producteur",
        (),
        (
            "usage: fraude [-h] [--source {api,jeu}] [--nombre NOMBRE]",
            "              [--intervalle INTERVALLE] [--jour JOUR] [--cadence CADENCE]",
            "              [--corruptions CORRUPTIONS] [--pannes PANNES]",
            "              [--rebaser-sur REBASER_SUR] [--graine GRAINE]",
            "",
            "Producteur : API ou rejeu du jeu -> validation -> masquage -> file.",
            "",
            "options:",
            "  -h, --help            show this help message and exit",
            "  --source {api,jeu}    défaut : configuration",
            "  --nombre NOMBRE       API : nombre d'appels ; jeu : nombre de lignes",
            "  --intervalle INTERVALLE",
            "                        API : secondes entre deux appels",
            "  --jour JOUR           jeu : ne rejouer que ce jour (AAAA-MM-JJ)",
            "  --cadence CADENCE     jeu : lignes par seconde",
            "  --corruptions CORRUPTIONS",
            "                        jeu : lignes à casser (démo)",
            "  --pannes PANNES       API : pannes simulées (démo)",
            "  --rebaser-sur REBASER_SUR",
            "                        jeu : décale les dates pour que --jour tombe à cette",
            "                        date (démo)",
            "  --graine GRAINE",
        ),
    ),
    (
        "detection_fraude.retraitement",
        (),
        (
            "usage: fraude [-h] --depuis DEPUIS --jusqu JUSQU [--modele MODELE]",
            "              [--rapports]",
            "",
            "Recalcule argent et alertes depuis le bronze.",
            "",
            "options:",
            "  -h, --help       show this help message and exit",
            "  --depuis DEPUIS  premier jour (AAAA-MM-JJ, UTC)",
            "  --jusqu JUSQU    dernier jour inclus (AAAA-MM-JJ, UTC)",
            "  --modele MODELE  version du modèle (défaut : celui en service)",
            "  --rapports       régénère les rapports du jour",
        ),
    ),
    (
        "detection_fraude.rapport",
        (),
        (
            "usage: fraude [-h] [--jour JOUR] [--sans-notification]",
            "",
            "Produit le rapport du matin pour un jour.",
            "",
            "options:",
            "  -h, --help           show this help message and exit",
            "  --jour JOUR          AAAA-MM-JJ (défaut : la veille, en UTC)",
            "  --sans-notification",
        ),
    ),
    (
        "detection_fraude.supervision",
        (),
        (
            "usage: fraude [-h] [--sans-flux] [--notifier]",
            "",
            "Contrôles de santé (code de sortie 1 si alerte).",
            "",
            "options:",
            "  -h, --help   show this help message and exit",
            "  --sans-flux  ne contrôle pas la fraîcheur",
            "  --notifier   envoie les constats graves",
        ),
    ),
]

IDENTIFIANTS = [f"{m.split('.')[-1]} {' '.join(p)}".strip() for m, p, _ in CAS]


@pytest.mark.parametrize(("module", "prefixe", "attendu"), CAS, ids=IDENTIFIANTS)
def test_l_aide_complete_de_chaque_commande_est_celle_documentee_et_sort_avec_le_code_0(
    module, prefixe, attendu, monkeypatch, capsys
):
    """``--help`` affiche l'usage, la description et chaque option, à l'identique, puis sort."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(sys, "argv", ["fraude"])
    with pytest.raises(SystemExit) as sortie:
        importlib.import_module(module).main([*prefixe, "--help"])
    assert sortie.value.code == 0
    lecture = capsys.readouterr()
    assert lecture.out == chr(10).join(attendu) + chr(10)
    assert lecture.err == ""
