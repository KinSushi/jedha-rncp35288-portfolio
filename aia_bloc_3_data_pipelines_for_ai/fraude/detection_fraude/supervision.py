# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Supervision : contrôles de santé et seuils d'alerte (SLA) — critère 6.1.

Un pipeline temps réel qui tombe en silence est pire qu'un pipeline qui échoue bruyamment. Ce
module regroupe des contrôles *purs* (ils reçoivent l'entrepôt, la configuration et l'heure) :

* ``flux_silencieux``        : aucune transaction traitée depuis plus de ``fraicheur_max_min`` ;
* ``notifications_bloquees`` : une alerte de fraude attend son envoi depuis trop longtemps ;
* ``notifications_en_echec`` : des alertes ont épuisé leurs tentatives (livraison perdue) ;
* ``taux_rejets_eleve``      : trop de lignes rejetées sur la dernière heure (source dégradée) ;
* ``modele_absent``          : aucun modèle en service ;
* ``modele_non_applique``    : le modèle promu n'est pas celui qu'utilise le consommateur.

CHOIX
-----
- Retenu : des contrôles qui reçoivent leurs dépendances (entrepôt, configuration, instant) et
  rendent une liste de constats ; ils servent trois consommateurs : la ligne de commande (code de
  sortie non nul sur alerte), le DAG ``fraude_supervision`` et les tests.
- Retenu : un seuil minimal de 20 lignes sur l'heure avant de juger le taux de rejets (pas de
  fausse alerte sur un faible volume) et un taux strictement supérieur au seuil de 2 %.
- Retenu : une gravité ``critique`` ou ``haute`` déclenche une notification ; les seuils sont dans
  la configuration (section ``sla``) et se règlent sans toucher au code.
- Retenu : un modèle promu depuis plus de 180 s sans être appliqué est une anomalie : un
  rechargement raté laisse le consommateur sur l'ancien modèle sans arrêter le flux, ce qui doit se
  voir.
- Retenu en complément de l'état de santé Docker (ligne « Disponibilité d'un service » du livrable
  cité plus bas) : ce dernier ne voit pas un flux muet ; aucune autre alternative n'est documentée
  (source : inconnue).

OÙ
---
- Place : garde-fou du flux temps réel, qui tourne hors d'Airflow.
- Appelants : controler_sante (detection_fraude/taches_airflow.py:269-279), demo
  (detection_fraude/local.py:188-299), ``main`` (sonde Docker) et les tests.
- Dépendances : ``entrepot``, ``modele`` (pointeur ``ACTUEL``), ``notification``,
  ``configuration`` et ``journal``.

COMMENT
-------
1. ``controler`` enchaîne les contrôles et rend la liste des constats (vide = tout va bien).
2. ``_modele_non_applique`` compare la version en service à celle que le consommateur écrit dans
   son état de santé et à l'ancienneté du pointeur.
3. ``notifier_constats`` envoie une notification par constat grave ; ``main`` imprime le résultat
   en JSON et retourne 1 s'il y a un constat, 0 sinon.
Tests qui le prouvent : test_tout_va_bien (tests/test_supervision.py:49-53), test_flux_silencieux
(tests/test_supervision.py:60-69), test_alerte_bloquee_et_alerte_en_echec
(tests/test_supervision.py:72-89), test_taux_de_rejets_eleve (tests/test_supervision.py:92-101),
test_peu_de_lignes_pas_de_fausse_alerte (tests/test_supervision.py:104-111),
test_modele_promu_mais_non_applique_par_le_consommateur (tests/test_supervision.py:123-140),
test_controler_ne_modifie_ni_la_base_ni_les_fichiers_et_renvoie_tous_les_constats_dans_l_ordre
(tests/test_supervision_garanties.py:441-468) et
test_main_sans_notifier_n_envoie_rien_meme_quand_il_y_a_des_constats
(tests/test_supervision_garanties.py:619-627).

POURQUOI
--------
- Les SLA, leurs valeurs et ce qui les contrôle : livrables/aia3/04_exploitation.md:41-90.
- Constats réels pendant la validation (dont ``modele_non_applique``) :
  livrables/aia3/04_exploitation.md:78-90.
- Valeurs des seuils : config/base.yaml:50.
- Indicateur 6.1 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Événement écrit (journal ``fraude.supervision``, WARNING) : ``constat_supervision`` avec ``code``
et ``gravite``, pour chaque constat de gravité haute ou critique notifié. Le détail du constat est
dans la notification envoyée aux canaux et dans la sortie JSON de la ligne de commande.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from detection_fraude.configuration import Configuration, charger_configuration
from detection_fraude.entrepot import Entrepot
from detection_fraude.journal import avertissement, configurer_journal, obtenir_journal
from detection_fraude.modele import NOM_POINTEUR, version_en_service
from detection_fraude.notification import Notificateur, Notification, construire_canaux

_journal = obtenir_journal("supervision")


@dataclass(frozen=True)
class Constat:
    code: str
    gravite: str  # "haute", "critique" ou "info"
    message: str


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="seconds")


def controler(
    entrepot: Entrepot,
    config: Configuration,
    maintenant: datetime | None = None,
    *,
    surveiller_flux: bool = True,
) -> list[Constat]:
    """Exécute tous les contrôles et renvoie la liste des constats (vide = tout va bien)."""
    maintenant = maintenant or datetime.now(UTC)
    constats: list[Constat] = []

    if version_en_service(config.modele.dossier) is None:
        constats.append(Constat("modele_absent", "critique", "Aucun modèle en service."))

    constat_modele = _modele_non_applique(config, maintenant)
    if constat_modele:
        constats.append(constat_modele)

    if surveiller_flux:
        derniere = entrepot.scalaire("SELECT MAX(traite_le) FROM argent_transactions")
        if derniere is None:
            constats.append(Constat("flux_silencieux", "haute", "Aucune transaction traitée."))
        else:
            age_min = (
                maintenant - datetime.fromisoformat(derniere).astimezone(UTC)
            ).total_seconds() / 60
            if age_min > config.sla.fraicheur_max_min:
                constats.append(
                    Constat(
                        "flux_silencieux",
                        "haute",
                        f"Dernière transaction traitée il y a {age_min:.0f} min "
                        f"(seuil : {config.sla.fraicheur_max_min:.0f} min).",
                    )
                )

    limite = _iso(maintenant - timedelta(minutes=config.sla.alertes_en_attente_max_min))
    bloquees = int(
        entrepot.scalaire(
            "SELECT COUNT(*) FROM or_alertes_fraude WHERE statut_notification = 'a_envoyer' "
            "AND cree_le < :limite",
            limite=limite,
        )
    )
    if bloquees:
        constats.append(
            Constat(
                "notifications_bloquees",
                "critique",
                f"{bloquees} alerte(s) de fraude en attente d'envoi depuis plus de "
                f"{config.sla.alertes_en_attente_max_min:.0f} min.",
            )
        )

    en_echec = int(
        entrepot.scalaire(
            "SELECT COUNT(*) FROM or_alertes_fraude WHERE statut_notification = 'echec'"
        )
    )
    if en_echec:
        constats.append(
            Constat(
                "notifications_en_echec",
                "critique",
                f"{en_echec} alerte(s) n'ont pas pu être envoyées (tentatives épuisées).",
            )
        )

    depuis = _iso(maintenant - timedelta(hours=1))
    rejets = int(
        entrepot.scalaire(
            "SELECT COUNT(*) FROM meta_rejets WHERE recu_le >= :depuis", depuis=depuis
        )
    )
    traites = int(
        entrepot.scalaire(
            "SELECT COUNT(*) FROM argent_transactions WHERE traite_le >= :depuis", depuis=depuis
        )
    )
    total = rejets + traites
    if total >= 20 and rejets / total > config.sla.taux_rejets_max:
        constats.append(
            Constat(
                "taux_rejets_eleve",
                "haute",
                f"{rejets} rejet(s) sur {total} lignes la dernière heure "
                f"({rejets / total * 100:.1f} % ; "
                f"seuil {config.sla.taux_rejets_max * 100:.1f} %).",
            )
        )
    return constats


def _modele_non_applique(config: Configuration, maintenant: datetime) -> Constat | None:
    """Le consommateur note-t-il avec le modèle promu ? Écart de plus de 3 min = anomalie.

    Un rechargement raté (fichier illisible ou altéré) laisse le consommateur sur l'ancien modèle
    sans interrompre le flux : c'est voulu, mais cela doit se voir.
    """
    en_service = version_en_service(config.modele.dossier)
    etat = config.racine_donnees / "etat" / "consommateur.json"
    if not en_service or not etat.exists():
        return None
    utilise = json.loads(etat.read_text(encoding="utf-8")).get("modele")
    pointeur = config.modele.dossier / NOM_POINTEUR
    promu_depuis = maintenant.timestamp() - pointeur.stat().st_mtime
    if utilise and utilise != en_service and promu_depuis > 180:
        return Constat(
            "modele_non_applique",
            "haute",
            f"Le modèle {en_service} est promu depuis {promu_depuis / 60:.0f} min, "
            f"mais le consommateur note encore avec {utilise}.",
        )
    return None


def notifier_constats(notificateur: Notificateur, constats: list[Constat]) -> None:
    """Envoie une notification par constat grave."""
    for constat in constats:
        if constat.gravite in ("haute", "critique"):
            avertissement(
                _journal, "constat_supervision", code=constat.code, gravite=constat.gravite
            )
            notificateur.notifier(
                Notification(
                    "supervision",
                    f"[SUPERVISION {constat.gravite.upper()}] {constat.code}",
                    constat.message,
                    asdict(constat),
                )
            )


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Contrôles de santé (code de sortie 1 si alerte)."
    )
    analyseur.add_argument("--sans-flux", action="store_true", help="ne contrôle pas la fraîcheur")
    analyseur.add_argument("--notifier", action="store_true", help="envoie les constats graves")
    arguments = analyseur.parse_args(argv)
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    entrepot = Entrepot(config.dsn_entrepot())
    entrepot.initialiser()
    try:
        constats = controler(entrepot, config, surveiller_flux=not arguments.sans_flux)
    finally:
        entrepot.fermer()
    if arguments.notifier and constats:
        notifier_constats(Notificateur(construire_canaux(config)), constats)
    sortie: dict[str, Any] = {"ok": not constats, "constats": [asdict(c) for c in constats]}
    print(json.dumps(sortie, ensure_ascii=False, indent=2))
    return 0 if not constats else 1


if __name__ == "__main__":
    sys.exit(main())
