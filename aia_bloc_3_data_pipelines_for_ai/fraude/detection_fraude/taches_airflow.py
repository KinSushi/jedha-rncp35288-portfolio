# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Fonctions appelées par les DAG Airflow, testables sans Airflow (critères 3.1, 3.2, 4.3, 6.1).

Un DAG est de la plomberie : il dit *quand* et *dans quel ordre*. Le travail lui-même vit ici, dans
des fonctions ordinaires qui reçoivent des paramètres simples et renvoient de petits résultats
(un jour, un numéro de version), faciles à transmettre par XCom. Résultat : toute la logique est
couverte par des tests unitaires, et les fichiers de ``dags/`` restent courts et lisibles.

Ce module n'importe **jamais** Airflow au chargement : seul ``echec_definitif`` le fait, au moment
d'un échec qu'il ne faut pas relancer.

CHOIX
-----
- Retenu : la logique dans des fonctions du paquet, les DAG ne faisant qu'ordonnancer ; chaque
  tâche d'un DAG appelle une fonction de ce module (vérifié par un test).
- Retenu : importer Airflow à la demande, dans ``echec_definitif`` seulement, avec repli sur
  ``RuntimeError`` hors Airflow (tests, ligne de commande) ; Airflow ne s'installe pas partout
  (Windows notamment).
- Retenu : un refus de la barrière de qualité et une anomalie de supervision sont des **échecs
  définitifs** (``AirflowFailException``), non relancés : réessayer ne changerait rien.
- Retenu : un nouveau contexte (configuration, journal, entrepôt) par tâche, fermé à la fin.
- Écarté : mettre la logique dans les fichiers de DAG (non testable sans Airflow).

OÙ
---
- Place : entre les DAG et le paquet métier ; chaque fonction est une tâche.
- Appelants : les trois fichiers du dossier ``dags`` (``PythonOperator``) et les tests.
- Dépendances : ``rapport``, ``entrainement``, ``supervision``, ``notification``,
  ``telechargement``, ``entrepot``, ``configuration`` et ``journal`` ; Airflow seulement dans
  ``echec_definitif``.

COMMENT
-------
1. Rapport du matin : ``verifier_donnees_de_la_veille`` (rend le jour par XCom),
   ``calculer_et_ecrire_rapport``, ``notifier_rapport_du_matin``, ``controler_sla_rapport``.
2. Réentraînement : ``verifier_jeu_de_donnees``, ``entrainer_candidats``,
   ``valider_barriere_qualite`` (refus -> échec définitif), ``promouvoir_modele``,
   ``notifier_reentrainement``.
3. Supervision : ``controler_sante`` notifie puis échoue s'il y a un constat.
4. Rappels : ``alerte_echec_tache`` (relances épuisées) et ``alerte_sla`` (SLA Airflow dépassé).
Tests qui le prouvent : test_les_taches_du_dag_du_matin_s_enchainent
(tests/test_bout_en_bout.py:204-220), test_le_dag_du_matin_echoue_sur_une_source_muette
(tests/test_bout_en_bout.py:223-227), test_la_barriere_refusee_est_un_echec_definitif
(tests/test_bout_en_bout.py:244-252), test_supervision_en_echec_notifie_et_fait_echouer_la_tache
(tests/test_bout_en_bout.py:255-259), test_chaque_tache_appelle_une_fonction_du_paquet
(tests/test_dags.py:127-131), test_une_barriere_refusee_est_un_echec_definitif_et_ne_promeut_rien
(tests/test_taches_airflow_garanties.py:425-447) et
test_verifier_donnees_de_la_veille_convertit_la_date_logique_et_refuse_une_source_muette
(tests/test_taches_airflow_garanties.py:175-211).

POURQUOI
--------
- DAG, dépendances, relances et SLA par DAG : livrables/aia3/02_pipeline_et_qualite.md:143-208.
- Rapport du matin, étapes et contrôle du SLA : livrables/aia3/04_exploitation.md:41-90.
- Refus de la barrière = échec définitif, promotion atomique :
  livrables/aia3/02_pipeline_et_qualite.md:166-187.
- Environnement de tests sans Airflow, analyse par le vrai Airflow 2.10.5 dans l'image :
  livrables/aia3/07_couverture_et_limites.md:31-51.
- Indicateurs 3.1, 3.2, 4.3 et 6.1 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.airflow``) : ``donnees_de_la_veille_ok`` (INFO : ``jour``,
``transactions``) et ``sla_depasse`` (ERROR : ``sla``, ``retard_min``). Les autres journaux
viennent des modules appelés (rapport, entraînement, supervision). La cause d'un échec de tâche est
nettoyée par ``journal.nettoyer`` et tronquée à 300 caractères avant d'être notifiée.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from detection_fraude.configuration import Configuration, charger_configuration
from detection_fraude.entrainement import (
    BarriereQualiteRefusee,
    entrainer,
    promouvoir_version,
    valider_candidat,
)
from detection_fraude.entrepot import Entrepot
from detection_fraude.journal import configurer_journal, erreur, info, nettoyer, obtenir_journal
from detection_fraude.notification import Notificateur, Notification, construire_canaux
from detection_fraude.rapport import (
    calculer_rapport,
    ecrire_rapport,
    jour_de_la_veille,
    verifier_delai_rapport,
    verifier_donnees_du_jour,
)
from detection_fraude.supervision import controler, notifier_constats
from detection_fraude.telechargement import telecharger_jeu

_journal = obtenir_journal("airflow")


def _contexte() -> tuple[Configuration, Entrepot]:
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    entrepot = Entrepot(config.dsn_entrepot())
    entrepot.initialiser()
    return config, entrepot


def echec_definitif(message: str) -> Exception:
    """Exception qu'Airflow ne relance pas (inutile de réessayer une barrière de qualité)."""
    try:
        from airflow.exceptions import AirflowFailException
    except ImportError:  # exécution hors Airflow (tests, ligne de commande)
        return RuntimeError(message)
    return AirflowFailException(message)


def _instant(valeur: Any) -> datetime:
    """Date logique fournie par Airflow (pendulum) ou par un test (datetime)."""
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(valeur))


# --------------------------------------------------------------------------------------------
# DAG « rapport du matin » (07:00, jour précédent)
# --------------------------------------------------------------------------------------------
def verifier_donnees_de_la_veille(data_interval_end: Any, **_: Any) -> str:
    """Étape 1 : la veille contient-elle des transactions ? Renvoie le jour (XCom)."""
    config, entrepot = _contexte()
    try:
        jour = jour_de_la_veille(_instant(data_interval_end))
        nombre = verifier_donnees_du_jour(entrepot, jour)
        info(_journal, "donnees_de_la_veille_ok", jour=jour, transactions=nombre)
        return jour
    finally:
        entrepot.fermer()


def calculer_et_ecrire_rapport(
    ti: Any = None, jour: str | None = None, **_: Any
) -> dict[str, Any]:
    """Étape 2 : agrégats, fichiers HTML/CSV et couche or. Idempotent : relançable sans risque."""
    jour = jour or ti.xcom_pull(task_ids="verifier_donnees_de_la_veille")
    config, entrepot = _contexte()
    try:
        rapport = calculer_rapport(entrepot, jour)
        chemins = ecrire_rapport(config, entrepot, rapport)
        return {
            "jour": jour,
            "nb_paiements": rapport["nb_paiements"],
            "nb_fraudes": rapport["nb_fraudes"],
            **chemins,
        }
    finally:
        entrepot.fermer()


def notifier_rapport_du_matin(
    ti: Any = None, resume: dict[str, Any] | None = None, **_: Any
) -> None:
    """Étape 3 : envoie le résumé aux canaux de notification."""
    resume = resume or ti.xcom_pull(task_ids="calculer_et_ecrire_rapport")
    config, entrepot = _contexte()
    try:
        (ligne,) = entrepot.requete(
            "SELECT * FROM or_rapport_quotidien WHERE jour = :jour", jour=resume["jour"]
        )
        corps = (
            f"Rapport du matin pour le {ligne['jour']} (jour UTC).\n"
            f"- Paiements : {ligne['nb_paiements']} pour {ligne['montant_total']:.2f}\n"
            f"- Fraudes détectées : {ligne['nb_fraudes']} pour {ligne['montant_fraudes']:.2f} "
            f"({ligne['taux_fraude_nb'] * 100:.3f} % des paiements)\n"
            f"- Lignes rejetées (DLQ) : {ligne['nb_rejets']}\n"
            f"- Rapport HTML : {ligne['chemin_html']}\n- Liste CSV : {ligne['chemin_csv']}"
        )
        sujet = (
            f"[Rapport du matin] {ligne['jour']} : {ligne['nb_fraudes']} fraude(s) "
            f"sur {ligne['nb_paiements']} paiements"
        )
        Notificateur(construire_canaux(config)).notifier(
            Notification(
                "rapport", sujet, corps, {"jour": ligne["jour"], "nb_fraudes": ligne["nb_fraudes"]}
            )
        )
    finally:
        entrepot.fermer()


def controler_sla_rapport(
    data_interval_end: Any, ti: Any = None, jour: str | None = None, **_: Any
) -> dict[str, Any]:
    """Étape 4 : le rapport a-t-il été livré dans l'heure qui suit 07:00 ? Alerte sinon."""
    jour = jour or ti.xcom_pull(task_ids="verifier_donnees_de_la_veille")
    config, entrepot = _contexte()
    try:
        genere_le = entrepot.scalaire(
            "SELECT genere_le FROM or_rapport_quotidien WHERE jour = :jour", jour=jour
        )
        respecte, retard = verifier_delai_rapport(
            datetime.fromisoformat(genere_le),
            _instant(data_interval_end),
            config.rapport.sla_minutes,
        )
        if not respecte:
            Notificateur(construire_canaux(config)).notifier(
                Notification(
                    "supervision",
                    f"[SLA] rapport du {jour} livré avec {retard:.0f} min de retard",
                    f"Le rapport du matin devait être livré {config.rapport.sla_minutes} min "
                    "après l'heure prévue.",
                    {"jour": jour, "retard_min": round(retard, 1)},
                )
            )
            erreur(_journal, "sla_depasse", sla="rapport_du_matin", retard_min=round(retard, 1))
        return {"respecte": respecte, "retard_min": round(retard, 1)}
    finally:
        entrepot.fermer()


# --------------------------------------------------------------------------------------------
# DAG « réentraînement hebdomadaire »
# --------------------------------------------------------------------------------------------
def verifier_jeu_de_donnees(**_: Any) -> str:
    """Étape 1 : le jeu d'entraînement est présent et conforme ; sinon il est téléchargé."""
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    return str(telecharger_jeu(config))


def entrainer_candidats(**_: Any) -> str:
    """Étape 2 : entraîne les candidats, suit MLflow, exporte une version (sans la promouvoir)."""
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    return str(entrainer(config)["version"])


def valider_barriere_qualite(
    ti: Any = None, version: str | None = None, **_: Any
) -> dict[str, Any]:
    """Étape 3 : plancher de PR-AUC et non-régression ; un refus n'est pas relancé."""
    version = version or ti.xcom_pull(task_ids="entrainer_candidats")
    config = charger_configuration()
    try:
        return valider_candidat(config, version)
    except BarriereQualiteRefusee as exc:
        raise echec_definitif(f"Barrière de qualité refusée : {exc}") from exc


def promouvoir_modele(ti: Any = None, version: str | None = None, **_: Any) -> str:
    """Étape 4 : bascule atomique du pointeur ; le consommateur recharge sans redémarrage."""
    version = version or ti.xcom_pull(task_ids="entrainer_candidats")
    promouvoir_version(charger_configuration(), version)
    return version


def notifier_reentrainement(ti: Any = None, version: str | None = None, **_: Any) -> None:
    version = version or ti.xcom_pull(task_ids="promouvoir_modele")
    config = charger_configuration()
    Notificateur(construire_canaux(config)).notifier(
        Notification(
            "rapport",
            f"[Modèle] version {version} mise en service",
            "Le réentraînement hebdomadaire a réussi et la barrière de qualité est franchie.",
            {"version": version},
        )
    )


# --------------------------------------------------------------------------------------------
# DAG « supervision » (toutes les 10 minutes)
# --------------------------------------------------------------------------------------------
def controler_sante(**_: Any) -> dict[str, Any]:
    """Applique les contrôles de supervision ; échoue (sans relance) s'il y a une anomalie."""
    config, entrepot = _contexte()
    try:
        constats = controler(entrepot, config)
        if constats:
            notifier_constats(Notificateur(construire_canaux(config)), constats)
            raise echec_definitif("; ".join(f"{c.code}: {c.message}" for c in constats))
        return {"ok": True}
    finally:
        entrepot.fermer()


# --------------------------------------------------------------------------------------------
# Rappels d'alerte communs
# --------------------------------------------------------------------------------------------
def alerte_echec_tache(contexte: dict[str, Any]) -> None:
    """``on_failure_callback`` : prévient l'équipe quand une tâche a épuisé ses relances."""
    instance = contexte.get("task_instance")
    config = charger_configuration()
    cause = str(nettoyer(str(contexte.get("exception", "inconnue"))))[:300]
    dag_id, task_id = getattr(instance, "dag_id", "?"), getattr(instance, "task_id", "?")
    sujet = f"[AIRFLOW] échec de {dag_id}.{task_id}"
    Notificateur(construire_canaux(config)).notifier(
        Notification(
            "supervision",
            sujet,
            f"Tentative n° {getattr(instance, 'try_number', '?')} : {cause}",
            {
                "dag": getattr(instance, "dag_id", None),
                "tache": getattr(instance, "task_id", None),
            },
        )
    )


def alerte_sla(
    dag: Any, task_list: str, blocking_task_list: str, slas: Any, blocking_tis: Any
) -> None:
    """``sla_miss_callback`` : une tâche n'a pas fini dans le délai promis (SLA Airflow)."""
    config = charger_configuration()
    Notificateur(construire_canaux(config)).notifier(
        Notification(
            "supervision",
            f"[SLA AIRFLOW] dépassement sur {getattr(dag, 'dag_id', '?')}",
            f"Tâches en retard :\n{task_list}\nBloquées par :\n{blocking_task_list}",
            {"dag": getattr(dag, "dag_id", None)},
        )
    )
