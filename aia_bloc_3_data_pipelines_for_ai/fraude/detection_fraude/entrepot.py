# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Entrepôt en couches bronze / argent / or (architecture médaillon), critères 2.1 et 7.1.

Il isole **tout le SQL** du projet. Le même code écrit dans SQLite (mode local) et dans
PostgreSQL (production) : seul le DSN change, car les requêtes se limitent à du SQL portable
(``INSERT ... ON CONFLICT``, ``GROUP BY``) et les tables n'utilisent que des types courants.

Les couches :

* ``bronze_transactions``  : les messages valides *tels que reçus*, déjà masqués à l'ingestion.
  Immuable, append-only. C'est la source de vérité pour tout retraitement.
* ``argent_transactions``  : lignes typées, normalisées, enrichies (caractéristiques) et notées
  (score, décision, version du modèle). Reconstructible à tout moment depuis le bronze.
* ``or_*``                 : tables prêtes pour le métier : alertes de fraude (avec l'état de leur
  notification) et rapport quotidien.
* ``meta_*``               : rejets (DLQ archivée) et lignage (une ligne par exécution d'étape).

CHOIX
-----
- Retenu : PostgreSQL en production et SQLite en local, avec du SQL portable : transactions ACID,
  volume modeste (ligne « Entrepôt » de livrables/aia3/01_architecture.md:178-188).
- Retenu : l'idempotence par clé primaire ``trans_num`` et ``INSERT ... ON CONFLICT`` : réécrire la
  même transaction ne crée jamais de doublon, quel que soit le nombre de relivraisons.
- Retenu : des requêtes paramétrées (``:nom``) partout ; seuls des noms de tables constants sont
  assemblés dans le texte SQL.
- Retenu : une rétention du bronze comptée depuis la **réception** (``recu_le``) et non depuis la
  date de la transaction : un rejeu d'historique n'est pas purgé le jour même.
- Écarté : du SQL propre à un moteur, qui ne tournerait pas dans les deux environnements ; le
  rapport des alternatives étudiées n'est pas dans le dépôt (source : inconnue).

OÙ
---
- Place : seul module qui parle SQL ; toutes les couches de stockage passent par lui.
- Appelants : Consommateur (detection_fraude/consommateur.py:238-455), calculer_rapport
  (detection_fraude/rapport.py:171-220), retraiter (detection_fraude/retraitement.py:94-198),
  controler (detection_fraude/supervision.py:99-189), envoyer_alertes_en_attente
  (detection_fraude/notification.py:336-378) et les tâches Airflow.
- Dépendances : SQLAlchemy (Core) et ``file_attente.activer_wal`` (mode WAL de SQLite).

COMMENT
-------
1. ``initialiser`` exécute le DDL (``CREATE TABLE IF NOT EXISTS``) : idempotent.
2. ``transaction`` ouvre une transaction : tout est validé ensemble, ou rien (annulation sur
   exception).
3. ``construire_insertion`` produit ``INSERT ... ON CONFLICT (clé) DO NOTHING`` ou, avec une liste
   de colonnes à remplacer, ``DO UPDATE SET c = excluded.c``.
4. ``identifiants_existants`` interroge par paquets de 500 ; ``historique_carte`` relit le bronze
   d'une carte strictement avant un instant ; ``marquer_alerte`` écrit le statut et la date
   de notification seulement pour ``envoyee`` ; ``purger_bronze`` supprime avant une date.
Tests qui le prouvent : test_bronze_ignore_les_doublons (tests/test_entrepot.py:40-45),
test_une_erreur_annule_toute_la_transaction (tests/test_entrepot.py:58-66),
test_argent_remplacer_ou_ignorer (tests/test_entrepot.py:89-110),
test_historique_d_une_carte_strictement_avant_et_dans_la_fenetre (tests/test_entrepot.py:69-86),
test_purge_du_bronze_selon_la_date_de_reception (tests/test_entrepot.py:133-140),
test_par_paquets_decoupe_sans_perdre_ni_dupliquer_et_refuse_une_taille_nulle
(tests/test_entrepot_garanties.py:204-219) et
test_une_interruption_clavier_dans_une_transaction_annule_aussi_les_ecritures
(tests/test_entrepot_garanties.py:360-366).

POURQUOI
--------
- Trois couches, qui écrit quoi, ce qui n'est jamais modifié :
  livrables/aia3/02_pipeline_et_qualite.md:7-36.
- Rétention de 90 jours depuis la réception et lignage :
  livrables/aia3/02_pipeline_et_qualite.md:109-117 et
  livrables/aia3/02_pipeline_et_qualite.md:289-330.
- Requêtes paramétrées contre l'injection SQL : livrables/aia3/03_securite_conformite.md:158-168.
- Clause ``DO NOTHING`` : construire_insertion (detection_fraude/entrepot.py:326-335). Épreuve du
  11/10/2026 (clause retirée) : deux tests de l'entrepôt échouent, ceux du consommateur restent
  verts, car il écarte déjà les doublons ; compte rendu des manipulations non publié.
- Indicateurs 2.1 et 7.1 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Ce module ne journalise rien : il lève les exceptions de SQLAlchemy et l'appelant décide. Le
lignage de chaque exécution d'étape est une donnée (table ``meta_lignage``), pas un événement de
journal. Les paramètres SQL ne sont jamais écrits dans un journal par ce module.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from detection_fraude.file_attente import activer_wal

# --------------------------------------------------------------------------------------------
# Colonnes (une seule définition, utilisée pour le DDL et pour les INSERT)
# --------------------------------------------------------------------------------------------
COLONNES_BRONZE = (
    "trans_num",
    "version_schema",
    "source",
    "horodatage_s",
    "carte_hash",
    "carte_fin",
    "commercant",
    "categorie",
    "montant",
    "etat",
    "population_ville",
    "age",
    "distance_km",
    "lat_commercant",
    "long_commercant",
    "etiquette_source",
    "sujet",
    "position_file",
    "publie_le_s",
    "recu_le",
    "lot_id",
)

COLONNES_ARGENT = (
    "trans_num",
    "date_transaction",
    "horodatage_utc",
    "horodatage_s",
    "carte_hash",
    "carte_fin",
    "commercant",
    "categorie",
    "montant",
    "etat",
    "population_ville",
    "age",
    "distance_km",
    "heure",
    "jour_semaine",
    "nb_tx_24h",
    "montant_relatif",
    "score",
    "est_fraude",
    "seuil",
    "version_modele",
    "version_regles",
    "etiquette_source",
    "source",
    "traite_le",
    "lot_id",
)

COLONNES_ALERTES = (
    "trans_num",
    "date_transaction",
    "horodatage_utc",
    "carte_fin",
    "commercant",
    "categorie",
    "montant",
    "score",
    "seuil",
    "version_modele",
    "source",
    "statut_notification",
    "tentatives",
    "derniere_tentative",
    "notifiee_le",
    "latence_notification_s",
    "canaux_ok",
    "cree_le",
    "detectee_le_s",
)

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS bronze_transactions (
        trans_num TEXT PRIMARY KEY,
        version_schema INTEGER NOT NULL,
        source TEXT NOT NULL,
        horodatage_s BIGINT NOT NULL,
        carte_hash TEXT NOT NULL,
        carte_fin TEXT NOT NULL,
        commercant TEXT NOT NULL,
        categorie TEXT NOT NULL,
        montant DOUBLE PRECISION NOT NULL,
        etat TEXT NOT NULL,
        population_ville BIGINT NOT NULL,
        age INTEGER NOT NULL,
        distance_km DOUBLE PRECISION NOT NULL,
        lat_commercant DOUBLE PRECISION NOT NULL,
        long_commercant DOUBLE PRECISION NOT NULL,
        etiquette_source INTEGER,
        sujet TEXT,
        position_file TEXT,
        publie_le_s DOUBLE PRECISION,
        recu_le TEXT NOT NULL,
        lot_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bronze_carte "
    "ON bronze_transactions (carte_hash, horodatage_s)",
    "CREATE INDEX IF NOT EXISTS idx_bronze_temps ON bronze_transactions (horodatage_s)",
    """
    CREATE TABLE IF NOT EXISTS argent_transactions (
        trans_num TEXT PRIMARY KEY,
        date_transaction TEXT NOT NULL,
        horodatage_utc TEXT NOT NULL,
        horodatage_s BIGINT NOT NULL,
        carte_hash TEXT NOT NULL,
        carte_fin TEXT NOT NULL,
        commercant TEXT NOT NULL,
        categorie TEXT NOT NULL,
        montant DOUBLE PRECISION NOT NULL,
        etat TEXT NOT NULL,
        population_ville BIGINT NOT NULL,
        age INTEGER NOT NULL,
        distance_km DOUBLE PRECISION NOT NULL,
        heure INTEGER NOT NULL,
        jour_semaine INTEGER NOT NULL,
        nb_tx_24h INTEGER NOT NULL,
        montant_relatif DOUBLE PRECISION NOT NULL,
        score DOUBLE PRECISION NOT NULL,
        est_fraude INTEGER NOT NULL,
        seuil DOUBLE PRECISION NOT NULL,
        version_modele TEXT NOT NULL,
        version_regles TEXT NOT NULL,
        etiquette_source INTEGER,
        source TEXT NOT NULL,
        traite_le TEXT NOT NULL,
        lot_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_argent_jour ON argent_transactions (date_transaction)",
    """
    CREATE TABLE IF NOT EXISTS or_alertes_fraude (
        trans_num TEXT PRIMARY KEY,
        date_transaction TEXT NOT NULL,
        horodatage_utc TEXT NOT NULL,
        carte_fin TEXT NOT NULL,
        commercant TEXT NOT NULL,
        categorie TEXT NOT NULL,
        montant DOUBLE PRECISION NOT NULL,
        score DOUBLE PRECISION NOT NULL,
        seuil DOUBLE PRECISION NOT NULL,
        version_modele TEXT NOT NULL,
        source TEXT NOT NULL,
        statut_notification TEXT NOT NULL,
        tentatives INTEGER NOT NULL,
        derniere_tentative TEXT,
        notifiee_le TEXT,
        latence_notification_s DOUBLE PRECISION,
        canaux_ok TEXT,
        cree_le TEXT NOT NULL,
        detectee_le_s DOUBLE PRECISION
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_alertes_statut ON or_alertes_fraude (statut_notification)",
    "CREATE INDEX IF NOT EXISTS idx_alertes_jour ON or_alertes_fraude (date_transaction)",
    """
    CREATE TABLE IF NOT EXISTS or_rapport_quotidien (
        jour TEXT PRIMARY KEY,
        nb_paiements INTEGER NOT NULL,
        nb_fraudes INTEGER NOT NULL,
        montant_total DOUBLE PRECISION NOT NULL,
        montant_fraudes DOUBLE PRECISION NOT NULL,
        taux_fraude_nb DOUBLE PRECISION NOT NULL,
        taux_fraude_montant DOUBLE PRECISION NOT NULL,
        nb_rejets INTEGER NOT NULL,
        nb_etiquetees INTEGER NOT NULL,
        nb_vrais_positifs INTEGER NOT NULL,
        nb_fraudes_reelles INTEGER NOT NULL,
        genere_le TEXT NOT NULL,
        version_regles TEXT NOT NULL,
        chemin_html TEXT,
        chemin_csv TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS or_rapport_categorie (
        jour TEXT NOT NULL,
        categorie TEXT NOT NULL,
        nb_paiements INTEGER NOT NULL,
        nb_fraudes INTEGER NOT NULL,
        montant_total DOUBLE PRECISION NOT NULL,
        montant_fraudes DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (jour, categorie)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta_rejets (
        sujet TEXT NOT NULL,
        cle_message TEXT NOT NULL,
        etape TEXT NOT NULL,
        raison TEXT NOT NULL,
        trans_num TEXT,
        erreurs TEXT NOT NULL,
        charge_utile TEXT NOT NULL,
        date_evenement TEXT NOT NULL,
        recu_le TEXT NOT NULL,
        PRIMARY KEY (sujet, cle_message)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rejets_jour ON meta_rejets (date_evenement)",
    """
    CREATE TABLE IF NOT EXISTS meta_lignage (
        id TEXT PRIMARY KEY,
        etape TEXT NOT NULL,
        entree TEXT NOT NULL,
        sortie TEXT NOT NULL,
        nb_entrees INTEGER NOT NULL,
        nb_sorties INTEGER NOT NULL,
        nb_rejets INTEGER NOT NULL,
        debut TEXT NOT NULL,
        fin TEXT NOT NULL,
        version_regles TEXT NOT NULL,
        version_modele TEXT,
        details TEXT
    )
    """,
]


def maintenant_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def construire_insertion(
    table: str, colonnes: Sequence[str], cle: str, remplacer: Sequence[str] | None = None
) -> str:
    """Construit un INSERT portable avec gestion du conflit sur la clé primaire."""
    noms = ", ".join(colonnes)
    valeurs = ", ".join(f":{c}" for c in colonnes)
    sql = f"INSERT INTO {table} ({noms}) VALUES ({valeurs}) ON CONFLICT ({cle}) "
    if remplacer is None:
        return sql + "DO NOTHING"
    return sql + "DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in remplacer)


def _par_paquets(valeurs: Sequence[Any], taille: int = 500) -> Iterator[Sequence[Any]]:
    for debut in range(0, len(valeurs), taille):
        yield valeurs[debut : debut + taille]


class Entrepot:
    """Accès à l'entrepôt. Une instance par processus (ou par fil d'exécution)."""

    def __init__(self, dsn: str) -> None:
        self.moteur: Engine = create_engine(dsn, future=True, pool_pre_ping=True)
        self.dialecte = self.moteur.dialect.name
        if self.dialecte == "sqlite":
            event.listen(self.moteur, "connect", self._regler_sqlite)
            fichier = self.moteur.url.database
            if fichier and fichier != ":memory:":
                Path(fichier).parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _regler_sqlite(connexion: Any, _enregistrement: Any) -> None:
        """WAL : lecteurs et écrivain ne se bloquent pas ; délai d'attente de 30 s sur verrou."""
        curseur = connexion.cursor()
        curseur.execute("PRAGMA busy_timeout=30000")
        activer_wal(connexion)
        curseur.execute("PRAGMA synchronous=NORMAL")
        curseur.close()

    def initialiser(self) -> None:
        """Crée les tables et index manquants (sans effet s'ils existent)."""
        with self.moteur.begin() as conn:
            for instruction in _DDL:
                conn.execute(text(instruction))

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Une transaction : tout est validé ensemble, ou rien (annulation sur exception)."""
        with self.moteur.begin() as conn:
            yield conn

    def fermer(self) -> None:
        self.moteur.dispose()

    # ----- Lecture générique -----
    def requete(self, sql: str, **parametres: Any) -> list[dict[str, Any]]:
        with self.moteur.connect() as conn:
            return [dict(ligne) for ligne in conn.execute(text(sql), parametres).mappings()]

    def scalaire(self, sql: str, **parametres: Any) -> Any:
        with self.moteur.connect() as conn:
            return conn.execute(text(sql), parametres).scalar()

    # ----- Bronze -----
    def identifiants_existants(
        self, conn: Connection, identifiants: Iterable[str], table: str = "bronze_transactions"
    ) -> set[str]:
        """Parmi ``identifiants``, ceux qui sont déjà présents dans ``table``."""
        liste = list(dict.fromkeys(identifiants))
        trouves: set[str] = set()
        requete = text(f"SELECT trans_num FROM {table} WHERE trans_num IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        for paquet in _par_paquets(liste):
            trouves.update(ligne[0] for ligne in conn.execute(requete, {"ids": list(paquet)}))
        return trouves

    def inserer_bronze(self, conn: Connection, lignes: list[dict[str, Any]]) -> None:
        if lignes:
            sql = construire_insertion("bronze_transactions", COLONNES_BRONZE, "trans_num")
            conn.execute(text(sql), lignes)

    def historique_carte(
        self, carte_hash: str, avant_s: int, depuis_s: int | None = None
    ) -> list[tuple[int, float]]:
        """Transactions antérieures d'une carte (bronze), de la plus ancienne à la plus récente."""
        sql = (
            "SELECT horodatage_s, montant FROM bronze_transactions "
            "WHERE carte_hash = :carte AND horodatage_s < :avant "
        )
        parametres: dict[str, Any] = {"carte": carte_hash, "avant": avant_s}
        if depuis_s is not None:
            sql += "AND horodatage_s >= :depuis "
            parametres["depuis"] = depuis_s
        sql += "ORDER BY horodatage_s, trans_num"
        return [
            (int(ligne["horodatage_s"]), float(ligne["montant"]))
            for ligne in self.requete(sql, **parametres)
        ]

    def lire_bronze(self, debut_s: int, fin_s: int) -> list[dict[str, Any]]:
        """Transactions bronze de [debut_s, fin_s[, en ordre chronologique."""
        return self.requete(
            "SELECT * FROM bronze_transactions "
            "WHERE horodatage_s >= :debut AND horodatage_s < :fin "
            "ORDER BY horodatage_s, trans_num",
            debut=debut_s,
            fin=fin_s,
        )

    # ----- Argent -----
    def inserer_argent(
        self, conn: Connection, lignes: list[dict[str, Any]], *, remplacer: bool = False
    ) -> None:
        if not lignes:
            return
        mise_a_jour = [c for c in COLONNES_ARGENT if c != "trans_num"] if remplacer else None
        sql = construire_insertion(
            "argent_transactions", COLONNES_ARGENT, "trans_num", mise_a_jour
        )
        conn.execute(text(sql), lignes)

    def transactions_du_jour(self, jour: str) -> list[dict[str, Any]]:
        return self.requete(
            "SELECT * FROM argent_transactions WHERE date_transaction = :jour "
            "ORDER BY horodatage_s, trans_num",
            jour=jour,
        )

    # ----- Or : alertes -----
    def inserer_alertes(
        self, conn: Connection, lignes: list[dict[str, Any]], *, remplacer: bool = False
    ) -> None:
        """Crée les alertes. En retraitement, on met à jour la décision sans toucher à l'envoi."""
        if not lignes:
            return
        remplacables = (
            "date_transaction",
            "horodatage_utc",
            "carte_fin",
            "commercant",
            "categorie",
            "montant",
            "score",
            "seuil",
            "version_modele",
            "source",
        )
        sql = construire_insertion(
            "or_alertes_fraude", COLONNES_ALERTES, "trans_num", remplacables if remplacer else None
        )
        conn.execute(text(sql), lignes)

    def alertes_a_envoyer(self, max_tentatives: int, limite: int = 100) -> list[dict[str, Any]]:
        return self.requete(
            "SELECT * FROM or_alertes_fraude WHERE statut_notification = 'a_envoyer' "
            "AND tentatives < :max ORDER BY detectee_le_s, trans_num LIMIT :limite",
            max=max_tentatives,
            limite=limite,
        )

    def marquer_alerte(
        self,
        trans_num: str,
        *,
        statut: str,
        tentatives: int,
        canaux_ok: list[str],
        latence_s: float | None,
    ) -> None:
        maintenant = maintenant_iso()
        with self.transaction() as conn:
            conn.execute(
                text(
                    "UPDATE or_alertes_fraude SET statut_notification = :statut, "
                    "tentatives = :tentatives, derniere_tentative = :maintenant, "
                    "notifiee_le = :notifiee, latence_notification_s = :latence, "
                    "canaux_ok = :canaux WHERE trans_num = :id"
                ),
                {
                    "statut": statut,
                    "tentatives": tentatives,
                    "maintenant": maintenant,
                    "notifiee": maintenant if statut == "envoyee" else None,
                    "latence": latence_s,
                    "canaux": ",".join(canaux_ok),
                    "id": trans_num,
                },
            )

    # ----- Méta : rejets et lignage -----
    def enregistrer_rejet(
        self,
        conn: Connection,
        rejet: dict[str, Any],
        cle_message: str,
        sujet: str,
        date_evenement: str,
    ) -> None:
        sql = construire_insertion(
            "meta_rejets",
            (
                "sujet",
                "cle_message",
                "etape",
                "raison",
                "trans_num",
                "erreurs",
                "charge_utile",
                "date_evenement",
                "recu_le",
            ),
            "sujet, cle_message",
        )
        conn.execute(
            text(sql),
            {
                "sujet": sujet,
                "cle_message": cle_message,
                "etape": str(rejet.get("etape", "inconnue")),
                "raison": str(rejet.get("raison", "inconnue")),
                "trans_num": rejet.get("trans_num"),
                "erreurs": json.dumps(rejet.get("erreurs", []), ensure_ascii=False),
                "charge_utile": json.dumps(
                    rejet.get("charge_utile_masquee", {}), ensure_ascii=False
                ),
                "date_evenement": date_evenement,
                "recu_le": maintenant_iso(),
            },
        )

    def enregistrer_lignage(
        self,
        etape: str,
        entree: str,
        sortie: str,
        *,
        nb_entrees: int,
        nb_sorties: int,
        nb_rejets: int = 0,
        debut: str,
        version_regles: str,
        version_modele: str | None = None,
        details: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Une ligne de lignage : quelle étape a lu quoi, écrit quoi, avec quelles versions."""
        valeurs = {
            "id": uuid.uuid4().hex,
            "etape": etape,
            "entree": entree,
            "sortie": sortie,
            "nb_entrees": nb_entrees,
            "nb_sorties": nb_sorties,
            "nb_rejets": nb_rejets,
            "debut": debut,
            "fin": maintenant_iso(),
            "version_regles": version_regles,
            "version_modele": version_modele,
            "details": json.dumps(details or {}, ensure_ascii=False, default=str),
        }
        instruction = text(
            "INSERT INTO meta_lignage (id, etape, entree, sortie, nb_entrees, nb_sorties, "
            "nb_rejets, debut, fin, version_regles, version_modele, details) VALUES "
            "(:id, :etape, :entree, :sortie, :nb_entrees, :nb_sorties, :nb_rejets, :debut, :fin, "
            ":version_regles, :version_modele, :details)"
        )
        if conn is not None:
            conn.execute(instruction, valeurs)
        else:
            with self.transaction() as propre:
                propre.execute(instruction, valeurs)

    # ----- Cycle de vie -----
    def purger_bronze(self, recu_avant: str) -> int:
        """Supprime le bronze reçu avant la date ISO donnée (limitation de conservation).

        La rétention se compte depuis la date de *réception* (``recu_le``), pas depuis la date de
        la transaction : un rejeu d'historique ne doit pas être purgé le jour même.
        """
        with self.transaction() as conn:
            resultat = conn.execute(
                text("DELETE FROM bronze_transactions WHERE recu_le < :avant"),
                {"avant": recu_avant},
            )
            return int(resultat.rowcount or 0)
