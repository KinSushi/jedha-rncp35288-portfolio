# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Mode local sans Docker : démonstration complète du flux, de la collecte au rapport du matin.

Pour la vidéo et pour toute personne qui clone le dépôt : une seule commande fait tourner
le pipeline *réel* (mêmes producteur, consommateur, entrepôt, modèle, notifications et rapport
que la production), avec une file SQLite et un entrepôt SQLite à la place de Kafka et PostgreSQL.

Commandes :

* ``python -m detection_fraude.local demo``     rejoue un jour du jeu (par défaut le dernier jour
  de la période de test) ; ``--source api`` interroge l'API réelle ;
* ``python -m detection_fraude.local etat``     compte les lignes de chaque couche ;
* ``python -m detection_fraude.local preparer`` télécharge le jeu et entraîne le modèle si besoin.

La démonstration injecte volontairement des lignes corrompues (``--corruptions``) pour montrer la
file des rejets, et, avec ``--pannes``, des pannes réseau simulées pour montrer les relances.
Ces deux options sont des outils de démonstration : en exploitation normale, elles valent zéro.

CHOIX
-----
- Retenu : exécuter le pipeline réel avec une file et un entrepôt SQLite, dans un seul processus
  (le consommateur tourne dans un fil), plutôt qu'une maquette : la démonstration prouve le vrai
  code.
- Retenu : le consommateur est lancé avant le producteur et la file est créée avant le fil, pour
  éviter une course à l'ouverture de la base.
- Retenu : ``reinitialiser`` ne supprime que les fichiers générés (file, entrepôt, alertes, états,
  rapports HTML et CSV, avec leurs fichiers ``-wal`` et ``-shm``), jamais les modèles.
- Retenu : refuser une file qui n'est pas SQLite (code de sortie 2) : la démonstration suppose le
  mode local.
- Écarté : lancer Kafka et PostgreSQL pour la démonstration (c'est le rôle du mode Docker).

OÙ
---
- Place : point d'entrée de démonstration et d'amorçage ; hors production.
- Appelants : ligne de commande ``main`` ; parcours (tests/test_bout_en_bout.py:41-86) et
  test_reinitialisation_supprime_les_donnees_generees_mais_pas_les_modeles
  (tests/test_divers.py:97-109).
- Dépendances : presque tout le paquet (producteur, consommateur, entraînement, rapport,
  supervision, téléchargement, entrepôt, file, notification).

COMMENT
-------
1. ``demo`` prépare (jeu et modèle), réinitialise si demandé, démarre le consommateur dans un fil,
   exécute le producteur (rejeu du jeu ou boucle sur l'API), attend que la file soit vide
   (``_attendre_file_vide``, avec relais de l'exception du fil), produit le rapport, contrôle la
   supervision, puis imprime le bilan.
2. ``_attendre_file_vide`` relève immédiatement l'exception du fil du consommateur s'il est
   tombé, et lève ``TimeoutError`` après le délai.
3. ``etat`` compte les lignes de chaque couche ; ``preparer`` télécharge le jeu et entraîne un
   premier modèle seulement si l'un ou l'autre manque.
Tests qui le prouvent : test_la_demonstration_se_termine_correctement
(tests/test_bout_en_bout.py:89-90), test_les_lignes_valides_traversent_toutes_les_couches
(tests/test_bout_en_bout.py:93-102), test_les_lignes_corrompues_sont_en_dlq_et_archivees
(tests/test_bout_en_bout.py:105-113), test_etat_compte_les_couches (tests/test_divers.py:112-114) ,
test_main_local_refuse_une_commande_ou_une_option_invalide_avec_le_code_2_sans_rien_lancer
(tests/test_finops_local_garanties.py:760-781) et
test_preparer_est_sans_effet_quand_le_jeu_et_le_modele_sont_deja_la
(tests/test_finops_local_garanties.py:505-515).

POURQUOI
--------
- Mode local, dépendances et commandes de démarrage : README.md:63.
- Environnements dev et prod identiques sauf la configuration, même rejeu aux mêmes 10 alertes :
  livrables/aia3/02_pipeline_et_qualite.md:189-208.
- Jour rejoué par défaut (période de test, 10 fraudes) : JOUR_PAR_DEFAUT
  (detection_fraude/local.py:108-110).
- Manipulations du 11/10/2026 réalisées avec cette démonstration : compte rendu non publié.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.local``) : ``demo_locale_uniquement`` (ERROR : ``cause``) et
``demo_terminee`` (INFO : les comptes de chaque table et ``duree_s``). Les sections de la
démonstration sont aussi imprimées sur la sortie standard (titres, bilan, rapport) ; les autres
événements viennent des modules appelés.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from detection_fraude.configuration import Configuration, charger_configuration
from detection_fraude.consommateur import construire_consommateur
from detection_fraude.entrainement import entrainer
from detection_fraude.entrepot import Entrepot
from detection_fraude.file_attente import FileSQLite
from detection_fraude.journal import configurer_journal, erreur, info, obtenir_journal
from detection_fraude.masquage import HacheurCarte
from detection_fraude.modele import version_en_service
from detection_fraude.notification import Notificateur, construire_canaux
from detection_fraude.producteur import (
    PannesSimulees,
    Producteur,
    boucle_api,
    rejouer_jeu,
)
from detection_fraude.rapport import DonneesAbsentes, produire_rapport
from detection_fraude.supervision import controler
from detection_fraude.telechargement import telecharger_jeu

_journal = obtenir_journal("local")
JOUR_PAR_DEFAUT = (
    "2020-12-21"  # période de test, 10 fraudes : jour rejoué par défaut de la démonstration
)


def _titre(texte: str) -> None:
    print(f"\n=== {texte} ===", flush=True)


def preparer(config: Configuration) -> None:
    """Télécharge le jeu et entraîne un premier modèle si nécessaire."""
    if not config.source.jeu_chemin.exists():
        _titre("Téléchargement du jeu fraudTest.csv (150 Mo, vérifié par SHA-256)")
        telecharger_jeu(config)
    if version_en_service(config.modele.dossier) is None:
        _titre("Entraînement du premier modèle (suivi MLflow)")
        entrainer(config, promouvoir_si_premier=True)


def reinitialiser(config: Configuration) -> None:
    """Supprime uniquement les fichiers générés par un précédent passage (pas les modèles)."""
    cibles = [config.file.chemin_sqlite, config.notification.fichier_alertes]
    racine = config.racine_donnees
    cibles += [
        racine / "entrepot.db",
        racine / "etat" / "producteur.json",
        racine / "etat" / "consommateur.json",
    ]
    for cible in cibles:
        for suffixe in ("", "-wal", "-shm"):
            Path(str(cible) + suffixe).unlink(missing_ok=True)
    for ancien in config.rapport.dossier.glob("*"):
        if ancien.suffix in (".html", ".csv"):
            ancien.unlink(missing_ok=True)


def etat(config: Configuration) -> dict[str, int]:
    entrepot = Entrepot(config.dsn_entrepot())
    entrepot.initialiser()
    try:
        return {
            table: int(entrepot.scalaire(f"SELECT COUNT(*) FROM {table}"))
            for table in (
                "bronze_transactions",
                "argent_transactions",
                "or_alertes_fraude",
                "or_rapport_quotidien",
                "meta_rejets",
                "meta_lignage",
            )
        }
    finally:
        entrepot.fermer()


def _attendre_file_vide(
    config: Configuration, erreurs: list[BaseException], delai_s: float = 600.0
) -> None:
    """Bloque jusqu'à ce que le consommateur ait traité tous les messages (démonstration).

    Si le fil du consommateur est tombé, son exception est relevée immédiatement au lieu
    d'attendre la fin du délai.
    """
    file = FileSQLite(config.file.chemin_sqlite)
    try:
        echeance = time.monotonic() + delai_s
        while time.monotonic() < echeance:
            if erreurs:
                raise erreurs[0]
            reste = (
                file.retard(config.file.sujet_valides, config.file.groupe_consommateur) or 0
            ) + (file.retard(config.file.sujet_rejets, config.file.groupe_rejets) or 0)
            if reste == 0:
                return
            time.sleep(0.3)
    finally:
        file.fermer()
    raise TimeoutError("Le consommateur n'a pas vidé la file dans le délai imparti.")


def demo(config: Configuration, arguments: argparse.Namespace) -> int:
    if config.file.type != "sqlite":
        erreur(
            _journal,
            "demo_locale_uniquement",
            cause="la démonstration suppose la file SQLite ; utiliser FRAUDE_ENV=dev",
        )
        return 2
    preparer(config)
    if arguments.reinitialiser:
        reinitialiser(config)

    source = arguments.source
    rebaser = getattr(arguments, "rebaser_sur", None)
    jour = (rebaser or arguments.jour) if source == "jeu" else datetime.now(UTC).date().isoformat()
    _titre(f"Démarrage du consommateur (modèle : {version_en_service(config.modele.dossier)})")
    arreter = threading.Event()
    erreur_consommateur: list[BaseException] = []

    def lancer_consommateur() -> None:
        try:
            consommateur, fermer = construire_consommateur(config)
            try:
                consommateur.boucle(arreter)
            finally:
                fermer()
        except BaseException as exc:  # noqa: BLE001 - remonté au fil principal
            erreur_consommateur.append(exc)

    file = FileSQLite(
        config.file.chemin_sqlite
    )  # créée avant le fil : pas de course à l'ouverture
    fil = threading.Thread(target=lancer_consommateur, name="consommateur", daemon=True)
    fil.start()

    _titre(f"Producteur : source « {source} »" + (f", jour {jour}" if source == "jeu" else ""))
    producteur = Producteur(
        config, file, HacheurCarte(config.sel_hachage(), config.longueur_hash), source=source
    )
    debut = time.monotonic()
    try:
        if source == "jeu":
            bilan = rejouer_jeu(
                config,
                producteur,
                jour=jour,
                limite=arguments.nombre,
                corruptions=arguments.corruptions,
                graine=arguments.graine,
                cadence=arguments.cadence,
                arreter=arreter,
                rebaser_sur=rebaser,
            )
        else:
            get = (
                PannesSimulees(requests.get, arguments.pannes)
                if arguments.pannes
                else requests.get
            )
            bilan = boucle_api(
                config,
                producteur,
                arreter,
                nombre_appels=arguments.appels,
                intervalle_s=arguments.intervalle,
                get=get,
            )
    finally:
        file.fermer()
    duree_production = time.monotonic() - debut

    _titre("Attente du traitement complet par le consommateur")
    try:
        _attendre_file_vide(config, erreur_consommateur)
    finally:
        arreter.set()
        fil.join(timeout=30)
    if erreur_consommateur:
        raise erreur_consommateur[0]
    duree_totale = time.monotonic() - debut

    _titre(f"Rapport du matin pour le {jour}")
    entrepot = Entrepot(config.dsn_entrepot())
    entrepot.initialiser()
    try:
        notificateur = Notificateur(construire_canaux(config))
        try:
            rapport = produire_rapport(config, entrepot, jour, notificateur)
            print(
                f"Paiements : {rapport['nb_paiements']} · "
                f"fraudes détectées : {rapport['nb_fraudes']} "
                f"· rejets : {rapport['nb_rejets']}"
            )
            print(f"Rapport HTML : {rapport['chemins']['html']}")
            print(f"Liste CSV    : {rapport['chemins']['csv']}")
        except DonneesAbsentes as exc:
            print(f"Pas de rapport : {exc}")
        constats = controler(entrepot, config, surveiller_flux=False)
    finally:
        entrepot.fermer()

    _titre("Bilan")
    comptes = etat(config)
    print(
        f"Lignes lues : {bilan.lues} · publiées : {bilan.publiees} · rejetées : {bilan.rejetees} "
        f"· doublons : {bilan.doublons}"
    )
    print("Contenu de l'entrepôt : " + " · ".join(f"{k} = {v}" for k, v in comptes.items()))
    print(f"Collecte : {duree_production:.1f} s ; traitement complet : {duree_totale:.1f} s")
    print(f"Supervision : {constats or 'aucune anomalie'}")
    info(_journal, "demo_terminee", **comptes, duree_s=round(duree_totale, 1))
    return 0


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Mode local sans Docker.")
    sous = analyseur.add_subparsers(dest="commande", required=True)
    d = sous.add_parser("demo", help="rejoue le flux de bout en bout")
    d.add_argument("--source", choices=("jeu", "api"), default="jeu")
    d.add_argument("--jour", default=JOUR_PAR_DEFAUT, help="jeu : jour à rejouer (AAAA-MM-JJ)")
    d.add_argument("--nombre", type=int, help="jeu : nombre de lignes ; sinon toute la journée")
    d.add_argument("--appels", type=int, default=3, help="api : nombre d'appels")
    d.add_argument(
        "--intervalle", type=float, default=13.0, help="api : secondes entre deux appels"
    )
    d.add_argument("--cadence", type=float, default=0.0, help="jeu : lignes par seconde (0 = max)")
    d.add_argument("--corruptions", type=int, default=6, help="lignes corrompues injectées (démo)")
    d.add_argument("--pannes", type=int, default=2, help="api : pannes réseau simulées (démo)")
    d.add_argument(
        "--rebaser-sur", help="jeu : date (AAAA-MM-JJ) sur laquelle recaler le jour rejoué"
    )
    d.add_argument("--graine", type=int, default=7)
    d.add_argument(
        "--reinitialiser", action="store_true", help="repart d'une file et d'un entrepôt vides"
    )
    sous.add_parser("etat", help="compte les lignes de chaque couche")
    sous.add_parser("preparer", help="télécharge le jeu et entraîne le modèle si besoin")
    arguments = analyseur.parse_args(argv)

    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    if arguments.commande == "demo":
        return demo(config, arguments)
    if arguments.commande == "preparer":
        preparer(config)
        return 0
    for table, nombre in etat(config).items():
        print(f"{table:<24} {nombre}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
