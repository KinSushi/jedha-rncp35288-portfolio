# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Retraitement de l'historique depuis la couche bronze (critère 2.3).

Les règles de transformation changent : nouvelle caractéristique, nouveau seuil, nouveau modèle,
correction d'un bug. Il faut alors pouvoir **recalculer le passé sans rappeler l'API** (qui ne
fournit qu'une transaction « courante » par appel et n'a pas d'historique). La couche bronze est
conçue pour cela : immuable, déjà masquée, rejouable.

Le retraitement relit le bronze d'une période, recalcule les caractéristiques et les scores avec
le modèle demandé (par défaut celui en service), et réécrit la couche argent par *upsert*.

Commande : ``python -m detection_fraude.retraitement --depuis 2020-12-21 --jusqu 2020-12-22``

CHOIX
-----
- Retenu : rejouer depuis le bronze, sans rappeler l'API, qui ne fournit pas d'historique
  (ELT après le masquage à l'entrée).
- Retenu : l'idempotence : relancer la même commande donne le même résultat, sans doublon (upsert
  de l'argent et des alertes).
- Retenu : les alertes déjà envoyées sont conservées (piste d'audit) ; une fraude *nouvellement*
  détectée est enregistrée avec le statut ``rejouee`` et n'est **pas** notifiée : on ne prévient
  pas l'équipe d'événements vieux de plusieurs jours.
- Retenu : paquets de 2 000 lignes, un par transaction de base de données.
- Retenu : une période vide est refusée (``ValueError``) plutôt que silencieusement ignorée.
- Écarté : rappeler l'API pour reconstituer le passé (impossible : elle n'a pas d'historique) et
  renotifier les alertes rejouées.

OÙ
---
- Place : lot à la demande, hors chemin chaud ; il réutilise les fonctions du consommateur.
- Appelants : ligne de commande ``main`` et les tests ; régénération des rapports par
  produire_rapport (detection_fraude/rapport.py:435-478).
- Dépendances : ``consommateur`` (``enrichir``, ``noter_lignes``, ``ligne_alerte``), ``entrepot``,
  ``fonctionnalites.EtatCartes``, ``modele``, ``rapport`` et ``journal``.

COMMENT
-------
1. ``retraiter`` borne la période [début du premier jour, début du lendemain du dernier jour[ en
   UTC, mémorise les anciennes décisions, relit le bronze et recalcule paquet par paquet.
2. Chaque paquet est écrit dans une transaction avec ``remplacer=True`` pour l'argent et les
   alertes ; les décisions modifiées et les nouvelles fraudes sont comptées.
3. Avec ``regenerer_rapports``, chaque jour de la période est régénéré (un jour sans données est
   ignoré) ; une ligne de lignage ``retraitement`` est écrite.
Tests qui le prouvent : test_nouvelle_regle_appliquee_au_passe_sans_renotifier
(tests/test_retraitement.py:57-73), test_le_retraitement_est_idempotent
(tests/test_retraitement.py:76-83), test_retraitement_sur_une_sous_periode
(tests/test_retraitement.py:86-93), test_periode_vide_refusee (tests/test_retraitement.py:115-117),
test_un_jour_de_fin_la_veille_du_debut_est_vide_mais_le_meme_jour_est_accepte
(tests/test_retraitement_garanties.py:265-281) et
test_une_panne_du_modele_en_cours_de_route_ne_declare_rien_de_termine_et_la_relance_converge
(tests/test_retraitement_garanties.py:566-594).

POURQUOI
--------
- Garanties du retraitement (idempotent, sans renotifier, version des règles dans l'argent) :
  livrables/aia3/02_pipeline_et_qualite.md:109-117.
- ETL à l'entrée pour la confidentialité, ELT ensuite pour la souplesse :
  livrables/aia3/01_architecture.md:134-145.
- Rappel opérationnel de la commande : livrables/aia3/04_exploitation.md:182-191.
- Indicateur 2.3 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Événement écrit (journal ``fraude.retraitement``, INFO) : ``retraitement_termine`` avec ``depuis``,
``jusqu``, ``lignes``, ``decisions_modifiees``, ``nouvelles_fraudes`` et ``modele`` (version).
Le lignage de l'exécution est écrit dans la table ``meta_lignage`` (étape ``retraitement``). En
ligne de commande, le résultat est imprimé sur la sortie standard.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

from detection_fraude import VERSION_REGLES
from detection_fraude.configuration import Configuration, charger_configuration
from detection_fraude.consommateur import enrichir, ligne_alerte, noter_lignes
from detection_fraude.entrepot import Entrepot, maintenant_iso
from detection_fraude.fonctionnalites import EtatCartes
from detection_fraude.journal import configurer_journal, info, obtenir_journal
from detection_fraude.modele import ModeleFraude, charger_modele
from detection_fraude.rapport import DonneesAbsentes, produire_rapport

_journal = obtenir_journal("retraitement")
TAILLE_PAQUET = 2000


def _epoch_debut_de_jour(jour: str) -> int:
    return int(datetime.combine(date.fromisoformat(jour), datetime.min.time(), UTC).timestamp())


def retraiter(
    config: Configuration,
    entrepot: Entrepot,
    depuis: str,
    jusqu: str,
    *,
    modele: ModeleFraude | None = None,
    regenerer_rapports: bool = False,
) -> dict[str, Any]:
    """Recalcule la couche argent (et les alertes) de ``depuis`` à ``jusqu`` inclus."""
    debut_iso = maintenant_iso()
    modele = modele or charger_modele(config.modele.dossier)
    debut_s = _epoch_debut_de_jour(depuis)
    fin_s = _epoch_debut_de_jour((date.fromisoformat(jusqu) + timedelta(days=1)).isoformat())
    if fin_s <= debut_s:
        raise ValueError(
            "La période est vide : 'jusqu' doit être postérieure ou égale à 'depuis'."
        )

    anciennes = {
        ligne["trans_num"]: int(ligne["est_fraude"])
        for ligne in entrepot.requete(
            "SELECT trans_num, est_fraude FROM argent_transactions "
            "WHERE horodatage_s >= :debut AND horodatage_s < :fin",
            debut=debut_s,
            fin=fin_s,
        )
    }
    bronze = entrepot.lire_bronze(debut_s, fin_s)
    etat = EtatCartes(chargeur=entrepot.historique_carte)
    lot_id = "retraitement-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    nouvelles_fraudes = decisions_modifiees = 0

    for debut in range(0, len(bronze), TAILLE_PAQUET):
        paquet = bronze[debut : debut + TAILLE_PAQUET]
        argent = [enrichir(ligne, etat) for ligne in paquet]
        noter_lignes(argent, modele)
        for ligne in argent:
            ligne["lot_id"] = lot_id
            if (
                ligne["trans_num"] in anciennes
                and anciennes[ligne["trans_num"]] != ligne["est_fraude"]
            ):
                decisions_modifiees += 1
        alertes = []
        for ligne in argent:
            if ligne["est_fraude"]:
                # Aucun instant de détection : l'alerte est rejouée, pas détectée en temps réel.
                alerte = ligne_alerte(ligne, None)
                alerte["statut_notification"] = "rejouee"
                alertes.append(alerte)
        with entrepot.transaction() as conn:
            entrepot.inserer_argent(conn, argent, remplacer=True)
            entrepot.inserer_alertes(conn, alertes, remplacer=True)
        nouvelles_fraudes += sum(
            1 for a in argent if a["est_fraude"] and anciennes.get(a["trans_num"]) != 1
        )

    rapports = 0
    if regenerer_rapports:
        # Nombre de jours calculé d'avance : la boucle est bornée, une erreur de pas ne peut pas
        # la faire tourner sans fin.
        premier_jour = date.fromisoformat(depuis)
        for decalage in range((date.fromisoformat(jusqu) - premier_jour).days + 1):
            jour = premier_jour + timedelta(days=decalage)
            try:
                produire_rapport(config, entrepot, jour.isoformat())
                rapports += 1
            except DonneesAbsentes:
                pass

    entrepot.enregistrer_lignage(
        "retraitement",
        "bronze_transactions",
        "argent_transactions,or_alertes_fraude",
        nb_entrees=len(bronze),
        nb_sorties=len(bronze),
        debut=debut_iso,
        version_regles=VERSION_REGLES,
        version_modele=modele.version,
        details={
            "depuis": depuis,
            "jusqu": jusqu,
            "lot_id": lot_id,
            "decisions_modifiees": decisions_modifiees,
            "rapports": rapports,
        },
    )
    info(
        _journal,
        "retraitement_termine",
        depuis=depuis,
        jusqu=jusqu,
        lignes=len(bronze),
        decisions_modifiees=decisions_modifiees,
        nouvelles_fraudes=nouvelles_fraudes,
        modele=modele.version,
    )
    return {
        "lignes": len(bronze),
        "decisions_modifiees": decisions_modifiees,
        "nouvelles_fraudes": nouvelles_fraudes,
        "rapports": rapports,
        "version_modele": modele.version,
    }


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Recalcule argent et alertes depuis le bronze."
    )
    analyseur.add_argument("--depuis", required=True, help="premier jour (AAAA-MM-JJ, UTC)")
    analyseur.add_argument("--jusqu", required=True, help="dernier jour inclus (AAAA-MM-JJ, UTC)")
    analyseur.add_argument("--modele", help="version du modèle (défaut : celui en service)")
    analyseur.add_argument("--rapports", action="store_true", help="régénère les rapports du jour")
    arguments = analyseur.parse_args(argv)
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    entrepot = Entrepot(config.dsn_entrepot())
    entrepot.initialiser()
    try:
        modele = charger_modele(config.modele.dossier, arguments.modele)
        resultat = retraiter(
            config,
            entrepot,
            arguments.depuis,
            arguments.jusqu,
            modele=modele,
            regenerer_rapports=arguments.rapports,
        )
    finally:
        entrepot.fermer()
    print(resultat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
