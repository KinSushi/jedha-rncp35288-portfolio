# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Consommateur : note chaque transaction, stocke en bronze / argent / or, notifie.

C'est le cœur du « temps réel » : dès qu'une transaction masquée arrive dans la file, elle est
contrôlée, enrichie, notée par le modèle et, si le score dépasse le seuil, une alerte est créée
et envoyée.

CHOIX
-----
- Retenu : des **lots courts** (jusqu'à 500 messages, 1 s d'attente au plus) : la latence reste de
  l'ordre de la seconde et le modèle note le lot d'un seul appel vectorisé.
- Retenu : **une transaction de base de données par lot** : bronze, argent, alertes et lignage
  sont écrits ensemble ou pas du tout. La position dans la file n'est validée *qu'après* ce
  commit : en cas de panne, les messages sont relivrés (« au moins une fois »).
- Retenu : l'**idempotence** : un ``trans_num`` déjà présent dans le bronze est ignoré. Une
  relivraison ne produit ni doublon, ni seconde notification.
- Retenu : l'**isolation des anomalies** : un message qui viole le contrat va dans la file des
  rejets avec la raison et le lot continue. Une erreur du système (modèle illisible, base
  indisponible) n'est pas masquée : le processus s'arrête sans valider la position.
- Retenu : la **boîte d'envoi** : l'alerte est écrite dans la transaction, envoyée ensuite.
- Écarté : traiter message par message (latence et appels du modèle inutiles) ; micro-lots retenus
  (ligne « Consommation » de livrables/aia3/01_architecture.md:178-188).
- Écarté : valider la position avant d'écrire en base (perte possible d'un message).

OÙ
---
- Place : entre la file et l'entrepôt ; service continu lancé par ``main`` ou par le mode local.
- Appelants : demo (detection_fraude/local.py:188-299), ``main``, les tests ; ``enrichir``,
  ``noter_lignes`` et ``ligne_alerte`` sont réutilisés par retraiter
  (detection_fraude/retraitement.py:94-198).
- Dépendances : ``file_attente``, ``entrepot``, ``fonctionnalites``, ``modele``, ``notification``,
  ``schema`` (contrat de la file), ``journal`` et ``configuration``.

COMMENT
-------
1. ``traiter_lot`` contrôle chaque message (``valider_masque``), envoie les non conformes en
   rejets, écarte les doublons du lot puis ceux de l'entrepôt, trie par horodatage, construit les
   lignes bronze et argent, note, crée les alertes, écrit tout dans une seule transaction avec le
   lignage, et seulement alors valide la position dans la file.
2. En cas d'exception pendant la transaction, l'état en mémoire des cartes est oublié (il sera
   reconstruit depuis le bronze) et l'exception est relancée.
3. ``executer_une_fois`` enchaîne lecture, lot, archivage des rejets, envoi des alertes et
   écriture de l'état de santé ; ``boucle`` répète jusqu'à l'arrêt.
Tests qui le prouvent : test_idempotence_meme_message_deux_fois (tests/test_consommateur.py:81-88),
test_idempotence_apres_relivraison_d_un_lot_deja_traite (tests/test_consommateur.py:91-117),
test_atomicite_une_panne_d_ecriture_n_ecrit_rien_et_ne_valide_pas_la_file
(tests/test_consommateur.py:120-135), test_message_hors_contrat_vers_la_dlq_sans_bloquer_le_lot
(tests/test_consommateur.py:138-159),
test_boite_d_envoi_un_canal_en_panne_est_reessaye_sans_renvoyer_aux_autres
(tests/test_consommateur.py:180-196),
test_la_relivraison_d_un_lot_deja_traite_ne_change_aucune_couche_et_ne_renvoie_rien
(tests/test_consommateur_garanties.py:467-502) et
test_main_ferme_les_ressources_et_annonce_l_arret_meme_si_la_boucle_plante
(tests/test_consommateur_cli.py:296-309).

POURQUOI
--------
- Micro-lots, latence mesurée de la publication à la notification :
  livrables/aia3/01_architecture.md:101-145.
- Scénarios de panne et résultat (tous couverts par un test) :
  livrables/aia3/01_architecture.md:190-199.
- Couches et fonctions qui les écrivent : livrables/aia3/02_pipeline_et_qualite.md:7-36.
- Écriture en base : transaction annulée, position non validée, lot rejoué :
  livrables/aia3/02_pipeline_et_qualite.md:271-287.
- Indicateurs 4.2 et 6.1 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.consommateur``) :
- ``lot_traite`` (INFO) : ``lot``, ``recus``, ``nouveaux``, ``doublons``, ``rejets``, ``fraudes``,
  ``duree_ms``, ``modele`` ;
- ``message_rejete`` (WARNING) : ``raison``, ``trans_num``, ``codes`` ;
- ``rejets_archives`` (INFO) : ``nombre`` ;
- ``demarrage_impossible`` (ERROR, code de sortie 2), ``consommateur_demarre`` et
  ``consommateur_arrete`` (INFO).
Le détail des erreurs reste dans la file des rejets et dans ``meta_rejets``, jamais une valeur
sensible.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from detection_fraude import VERSION_REGLES
from detection_fraude.configuration import (
    Configuration,
    ErreurConfiguration,
    charger_configuration,
)
from detection_fraude.entrepot import Entrepot, maintenant_iso
from detection_fraude.file_attente import FileMessages, Message, construire_file
from detection_fraude.fonctionnalites import (
    COLONNES_MODELE,
    EtatCartes,
    derivees_temporelles,
    normaliser_commercant,
)
from detection_fraude.journal import (
    avertissement,
    configurer_journal,
    erreur,
    info,
    obtenir_journal,
)
from detection_fraude.modele import ChargeurModele, ErreurModele
from detection_fraude.notification import (
    Notificateur,
    construire_canaux,
    envoyer_alertes_en_attente,
)
from detection_fraude.schema import VERSION_SCHEMA, valider_masque

_journal = obtenir_journal("consommateur")


@dataclass
class BilanLot:
    recus: int = 0
    nouveaux: int = 0
    doublons: int = 0
    rejets: int = 0
    fraudes: int = 0
    duree_ms: int = 0
    lot_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def construire_ligne_bronze(message: Message, lot_id: str, recu_le: str) -> dict[str, Any]:
    v = message.valeur
    return {
        "trans_num": v["trans_num"],
        "version_schema": v["version_schema"],
        "source": v["source"],
        "horodatage_s": int(v["horodatage_s"]),
        "carte_hash": v["carte_hash"],
        "carte_fin": v["carte_fin"],
        "commercant": v["commercant"],
        "categorie": v["categorie"],
        "montant": float(v["montant"]),
        "etat": v["etat"],
        "population_ville": int(v["population_ville"]),
        "age": int(v["age"]),
        "distance_km": float(v["distance_km"]),
        "lat_commercant": float(v["lat_commercant"]),
        "long_commercant": float(v["long_commercant"]),
        "etiquette_source": v.get("etiquette_source"),
        "sujet": message.sujet,
        "position_file": message.position,
        "publie_le_s": message.publie_le,
        "recu_le": recu_le,
        "lot_id": lot_id,
    }


def enrichir(ligne_bronze: dict[str, Any], etat: EtatCartes) -> dict[str, Any]:
    """Couche argent, étape 1 : normalisation et caractéristiques (sans le score)."""
    t = int(ligne_bronze["horodatage_s"])
    date_iso, instant_iso, heure, jour_semaine = derivees_temporelles(t)
    nb_24h, relatif = etat.caracteristiques(
        ligne_bronze["carte_hash"], t, float(ligne_bronze["montant"])
    )
    etat.enregistrer(ligne_bronze["carte_hash"], t, float(ligne_bronze["montant"]))
    return {
        "trans_num": ligne_bronze["trans_num"],
        "date_transaction": date_iso,
        "horodatage_utc": instant_iso,
        "horodatage_s": t,
        "carte_hash": ligne_bronze["carte_hash"],
        "carte_fin": ligne_bronze["carte_fin"],
        "commercant": normaliser_commercant(ligne_bronze["commercant"]),
        "categorie": ligne_bronze["categorie"],
        "montant": float(ligne_bronze["montant"]),
        "etat": ligne_bronze["etat"],
        "population_ville": int(ligne_bronze["population_ville"]),
        "age": int(ligne_bronze["age"]),
        "distance_km": float(ligne_bronze["distance_km"]),
        "heure": heure,
        "jour_semaine": jour_semaine,
        "nb_tx_24h": nb_24h,
        "montant_relatif": relatif,
        "etiquette_source": ligne_bronze.get("etiquette_source"),
        "source": ligne_bronze["source"],
        "lot_id": ligne_bronze.get("lot_id"),
    }


def noter_lignes(lignes: list[dict[str, Any]], modele: Any) -> None:
    """Couche argent, étape 2 : ajoute ``score``, ``est_fraude``, ``seuil`` et les versions."""
    if not lignes:
        return
    scores = modele.noter(pd.DataFrame(lignes)[list(COLONNES_MODELE)])
    maintenant = maintenant_iso()
    for ligne, score in zip(lignes, scores, strict=True):
        ligne["score"] = float(score)
        ligne["est_fraude"] = int(score >= modele.seuil)
        ligne["seuil"] = float(modele.seuil)
        ligne["version_modele"] = modele.version
        ligne["version_regles"] = VERSION_REGLES
        ligne["traite_le"] = maintenant


def ligne_alerte(ligne_argent: dict[str, Any], detectee_le_s: float | None) -> dict[str, Any]:
    return {
        "trans_num": ligne_argent["trans_num"],
        "date_transaction": ligne_argent["date_transaction"],
        "horodatage_utc": ligne_argent["horodatage_utc"],
        "carte_fin": ligne_argent["carte_fin"],
        "commercant": ligne_argent["commercant"],
        "categorie": ligne_argent["categorie"],
        "montant": ligne_argent["montant"],
        "score": ligne_argent["score"],
        "seuil": ligne_argent["seuil"],
        "version_modele": ligne_argent["version_modele"],
        "source": ligne_argent["source"],
        "statut_notification": "a_envoyer",
        "tentatives": 0,
        "derniere_tentative": None,
        "notifiee_le": None,
        "latence_notification_s": None,
        "canaux_ok": "",
        "cree_le": maintenant_iso(),
        "detectee_le_s": detectee_le_s,
    }


class Consommateur:
    """Traite les messages de la file : contrôle, enrichissement, scoring, stockage, alertes."""

    def __init__(
        self,
        config: Configuration,
        file: FileMessages,
        entrepot: Entrepot,
        chargeur_modele: ChargeurModele,
        notificateur: Notificateur,
        *,
        horloge: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._file = file
        self._entrepot = entrepot
        self._chargeur = chargeur_modele
        self._notificateur = notificateur
        self._horloge = horloge
        self._etat = self._nouvel_etat()

    def _nouvel_etat(self) -> EtatCartes:
        return EtatCartes(chargeur=self._entrepot.historique_carte)

    # ----- Lot de transactions -----
    def traiter_lot(self, messages: list[Message]) -> BilanLot:
        debut = time.monotonic()
        debut_iso = maintenant_iso()
        lot_id = uuid.uuid4().hex[:12]
        bilan = BilanLot(recus=len(messages), lot_id=lot_id)
        if not messages:
            return bilan

        # 1. Contrôle du contrat de la file (on ne fait pas confiance au producteur).
        valides: list[Message] = []
        for message in messages:
            erreurs = (
                [{"champ": "*", "code": "charge_illisible", "detail": ""}]
                if not isinstance(message.valeur, dict) or "__illisible__" in message.valeur
                else [e.en_dict() for e in valider_masque(message.valeur)]
            )
            if erreurs:
                self._vers_rejets(message, erreurs)
                bilan.rejets += 1
            else:
                valides.append(message)

        # 2. Doublons : dans le lot, puis dans l'entrepôt.
        uniques: dict[str, Message] = {}
        for message in valides:
            uniques.setdefault(message.valeur["trans_num"], message)
        bilan.doublons = len(valides) - len(uniques)

        modele = self._chargeur.obtenir()  # peut recharger une nouvelle version promue
        try:
            with self._entrepot.transaction() as conn:
                existants = self._entrepot.identifiants_existants(conn, uniques)
                nouveaux = [m for t, m in uniques.items() if t not in existants]
                bilan.doublons += len(uniques) - len(nouveaux)
                bilan.nouveaux = len(nouveaux)
                nouveaux.sort(key=lambda m: int(m.valeur["horodatage_s"]))

                recu_le = maintenant_iso()
                bronze = [construire_ligne_bronze(m, lot_id, recu_le) for m in nouveaux]
                argent = [enrichir(ligne, self._etat) for ligne in bronze]
                noter_lignes(argent, modele)
                alertes = [
                    ligne_alerte(a, m.publie_le)
                    for a, m in zip(argent, nouveaux, strict=True)
                    if a["est_fraude"]
                ]
                bilan.fraudes = len(alertes)
                self._entrepot.inserer_bronze(conn, bronze)
                self._entrepot.inserer_argent(conn, argent)
                self._entrepot.inserer_alertes(conn, alertes)
                bilan.duree_ms = int((time.monotonic() - debut) * 1000)
                self._entrepot.enregistrer_lignage(
                    "consommateur",
                    f"file:{self._config.file.sujet_valides}",
                    "bronze_transactions,argent_transactions,or_alertes_fraude",
                    nb_entrees=bilan.recus,
                    nb_sorties=bilan.nouveaux,
                    nb_rejets=bilan.rejets,
                    debut=debut_iso,
                    version_regles=VERSION_REGLES,
                    version_modele=modele.version,
                    details={
                        "lot_id": lot_id,
                        "doublons": bilan.doublons,
                        "fraudes": bilan.fraudes,
                        "duree_ms": bilan.duree_ms,
                    },
                    conn=conn,
                )
        except BaseException:
            # L'état en mémoire a pu être modifié pour un lot qui n'est pas validé : on l'oublie.
            # Il sera reconstruit à la demande depuis le bronze (voir EtatCartes).
            self._etat = self._nouvel_etat()
            raise

        # 3. La position n'avance qu'après le commit de la base.
        self._file.valider(
            self._config.file.sujet_valides, self._config.file.groupe_consommateur, messages
        )
        info(
            _journal,
            "lot_traite",
            lot=lot_id,
            recus=bilan.recus,
            nouveaux=bilan.nouveaux,
            doublons=bilan.doublons,
            rejets=bilan.rejets,
            fraudes=bilan.fraudes,
            duree_ms=bilan.duree_ms,
            modele=modele.version,
        )
        return bilan

    def _vers_rejets(self, message: Message, erreurs: list[dict[str, str]]) -> None:
        valeur = message.valeur if isinstance(message.valeur, dict) else {}
        trans_num_brut = valeur.get("trans_num")
        trans_num = trans_num_brut if isinstance(trans_num_brut, str) else None
        rejet = {
            "version_schema": VERSION_SCHEMA,
            "etape": "consommateur",
            "raison": "contrat_file_viole",
            "erreurs": erreurs,
            "trans_num": trans_num,
            "source": valeur.get("source"),
            "rejete_le": maintenant_iso(),
            "position_origine": message.position,
            # Un message masqué a déjà perdu ses données sensibles ; on ne recopie que les champs
            # connus et sûrs, jamais le message entier.
            "charge_utile_masquee": {
                "champs_conserves": {
                    k: valeur[k]
                    for k in ("montant", "categorie", "etat", "horodatage_s")
                    if isinstance(valeur.get(k), int | float | str)
                },
                "champs_inconnus": sorted(
                    str(k)
                    for k in valeur
                    if k not in {"montant", "categorie", "etat", "horodatage_s", "trans_num"}
                ),
            },
        }
        self._file.publier(self._config.file.sujet_rejets, rejet, cle=trans_num)
        avertissement(
            _journal,
            "message_rejete",
            raison=rejet["raison"],
            trans_num=trans_num,
            codes=",".join(sorted({e["code"] for e in erreurs})),
        )

    # ----- Archivage de la file des rejets -----
    def archiver_rejets(self, attente_s: float = 0.0) -> int:
        """Copie la file des rejets (DLQ) dans ``meta_rejets`` pour la requêter et la compter."""
        sujet = self._config.file.sujet_rejets
        groupe = self._config.file.groupe_rejets
        messages = self._file.lire(sujet, groupe, self._config.file.taille_lot, attente_s)
        if not messages:
            return 0
        with self._entrepot.transaction() as conn:
            for message in messages:
                rejet = message.valeur if isinstance(message.valeur, dict) else {}
                date = _date_evenement(rejet)
                self._entrepot.enregistrer_rejet(conn, rejet, message.position, sujet, date)
        self._file.valider(sujet, groupe, messages)
        info(_journal, "rejets_archives", nombre=len(messages))
        return len(messages)

    # ----- Boucle -----
    def envoyer_alertes(self) -> dict[str, int]:
        return envoyer_alertes_en_attente(
            self._entrepot, self._notificateur, self._config, horloge=self._horloge
        )

    def executer_une_fois(self, attente_s: float | None = None) -> BilanLot:
        attente = self._config.file.attente_lecture_s if attente_s is None else attente_s
        # Détecte une promotion de modèle dans la seconde, même sans trafic (voir ChargeurModele).
        self._chargeur.obtenir()
        messages = self._file.lire(
            self._config.file.sujet_valides,
            self._config.file.groupe_consommateur,
            self._config.file.taille_lot,
            attente,
        )
        bilan = self.traiter_lot(messages)
        self.archiver_rejets()
        self.envoyer_alertes()
        self._ecrire_etat(bilan)
        return bilan

    def _ecrire_etat(self, bilan: BilanLot) -> None:
        chemin = self._config.racine_donnees / "etat" / "consommateur.json"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(
            json.dumps(
                {
                    "derniere_boucle": datetime.now(UTC).isoformat(),
                    "dernier_lot": bilan.lot_id,
                    "recus": bilan.recus,
                    "modele": self._chargeur.obtenir().version,  # lu par la supervision
                }
            ),
            encoding="utf-8",
        )

    def boucle(self, arreter: threading.Event, *, jusqu_a_vide: bool = False) -> int:
        """Consomme jusqu'à l'arrêt ; avec ``jusqu_a_vide``, s'arrête quand la file est vidée."""
        total = 0
        while not arreter.is_set():
            bilan = self.executer_une_fois()
            total += bilan.recus
            if jusqu_a_vide and bilan.recus == 0:
                break
        return total


def _date_evenement(rejet: dict[str, Any]) -> str:
    """Jour d'un rejet : celui de la transaction si lisible, sinon celui du rejet."""
    conserves = (rejet.get("charge_utile_masquee") or {}).get("champs_conserves") or {}
    for cle, diviseur in (("current_time", 1000), ("horodatage_s", 1)):
        valeur = conserves.get(cle)
        if isinstance(valeur, int | float) and not isinstance(valeur, bool):
            try:
                return datetime.fromtimestamp(valeur / diviseur, UTC).date().isoformat()
            except (OverflowError, OSError, ValueError):
                break
    rejete_le = str(rejet.get("rejete_le", ""))
    return rejete_le[:10] if len(rejete_le) >= 10 else datetime.now(UTC).date().isoformat()


def construire_consommateur(config: Configuration) -> tuple[Consommateur, Callable[[], None]]:
    """Assemble le consommateur et renvoie aussi la fonction de fermeture des ressources."""
    file = construire_file(config)
    entrepot = Entrepot(config.dsn_entrepot())
    entrepot.initialiser()
    chargeur = ChargeurModele(config.modele.dossier)
    chargeur.obtenir()  # échoue tout de suite, clairement, si aucun modèle n'est en service
    notificateur = Notificateur(construire_canaux(config))

    def fermer() -> None:
        file.fermer()
        entrepot.fermer()

    return Consommateur(config, file, entrepot, chargeur, notificateur), fermer


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Consommateur : file -> notation -> entrepôt.")
    analyseur.add_argument(
        "--jusqu-a-vide",
        action="store_true",
        help="s'arrête quand la file est vidée (démonstration, tests)",
    )
    arguments = analyseur.parse_args(argv)
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    arreter = threading.Event()
    for signal_arret in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_arret, lambda *_: arreter.set())
    try:
        consommateur, fermer = construire_consommateur(config)
    except (ErreurModele, ErreurConfiguration) as exc:
        erreur(_journal, "demarrage_impossible", cause=str(exc))
        return 2
    info(
        _journal, "consommateur_demarre", environnement=config.environnement, file=config.file.type
    )
    try:
        consommateur.boucle(arreter, jusqu_a_vide=arguments.jusqu_a_vide)
    finally:
        fermer()
        info(_journal, "consommateur_arrete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
