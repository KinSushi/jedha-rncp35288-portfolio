# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""File de messages : SQLite en local (sans Docker), Kafka en production (critères 1.2 et 4.2).

Le producteur et le consommateur ne se connaissent pas : ils ne partagent qu'une file. Cette
indirection apporte trois choses :

* le **découplage** (le consommateur peut s'arrêter, la collecte continue, rien n'est perdu) ;
* la **file des rejets** (DLQ) : un second sujet qui reçoit les lignes invalides sans bloquer le
  flux principal ;
* la **portabilité** : la démonstration locale (``FileSQLite``) et la production (``FileKafka``,
  compatible Redpanda) exposent la même interface, donc le même code métier tourne dans les deux.

CHOIX
-----
- Retenu : Kafka (ici Redpanda, même protocole) en production et SQLite en local, derrière une
  seule interface ``FileMessages`` (``Protocol``).
- Retenu : la sémantique « au moins une fois ». Un consommateur valide sa position après avoir
  écrit en base ; si le processus tombe entre les deux, le message est relivré, et c'est
  l'idempotence de l'entrepôt (clé primaire ``trans_num``) qui évite le doublon.
- Retenu : côté Kafka, ``acks=all`` et ``enable.idempotence`` à la publication,
  ``enable.auto.commit=false`` à la lecture, et ``vider`` (flush) avant de clore un lot.
- Retenu : le client Kafka est importé à la demande, pour que le mode local n'en ait pas besoin.
- Retenu : une charge illisible est transmise comme ``{"__illisible__": true}`` pour que le
  consommateur la dirige vers la file des rejets au lieu de bloquer.
- Écarté : valider la position avant l'écriture en base, qui perdrait un message en cas de panne
  (ligne « Consommation » de livrables/aia3/01_architecture.md:178-188).

OÙ
---
- Place : entre le producteur et le consommateur ; fabrique choisie par ``construire_file`` selon
  ``file.type`` de la configuration.
- Appelants : Producteur (detection_fraude/producteur.py:377-454), Consommateur
  (detection_fraude/consommateur.py:238-455), demo (detection_fraude/local.py:188-299).
- Dépendances : ``sqlite3`` et ``json`` (standard) ; ``confluent_kafka`` seulement en production ;
  ``configuration.lire_secret`` pour les options de sécurité de Kafka.

COMMENT
-------
1. ``FileSQLite`` passe la base en mode WAL (``activer_wal`` réessaie si la base est verrouillée),
   range les messages dans la table ``messages`` et les positions de lecture dans ``positions``
   par couple (sujet, groupe).
2. ``lire`` renvoie jusqu'à ``max_messages`` messages d'identifiant supérieur à la position du
   groupe, en attendant au plus ``attente_s`` ; ``valider`` fait avancer la position et ne la
   recule jamais.
3. ``FileKafka`` publie en JSON avec la clé encodée, lit par lots, valide ``dernier décalage + 1``
   par partition et signale par ``ErreurLivraison`` tout message non livré.
Tests qui le prouvent : test_sans_validation_les_messages_sont_relivres
(tests/test_file_attente.py:50-57), test_la_position_ne_recule_jamais
(tests/test_file_attente.py:85-92), test_kafka_signale_un_message_non_livre
(tests/test_file_attente.py:202-208), test_kafka_lecture_et_validation_par_partition
(tests/test_file_attente.py:211-235),
test_les_options_ne_peuvent_pas_affaiblir_la_fiabilite_du_producteur_et_restent_inchangees
(tests/test_file_attente_garanties.py:591-612) et
test_les_echecs_de_livraison_sont_rapportes_trois_par_trois_puis_oublies
(tests/test_file_attente_garanties.py:698-712).

POURQUOI
--------
- Brique de bus, garanties contre la perte, scénarios de panne :
  livrables/aia3/01_architecture.md:178-188 et livrables/aia3/01_architecture.md:190-199.
- Isolation des lignes corrompues (DLQ) : livrables/aia3/02_pipeline_et_qualite.md:232-269.
- Chiffrement des transferts et variables de sécurité de Kafka :
  livrables/aia3/03_securite_conformite.md:113-127.
- Commentaire d'origine sur le choix IPv4 de Redpanda : detection_fraude/file_attente.py:252.
- Indicateurs 1.2 et 4.2 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Ce module ne journalise pas lui-même. Il remonte les échecs par exception (``ErreurLivraison``,
``sqlite3.OperationalError`` hors verrou) et l'appelant journalise. Les erreurs de partition ou de
courtier transitoires lues par Kafka sont ignorées sans trace dans ce module : le consommateur et
la supervision (constat ``flux_silencieux``) rendent la panne visible.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Message:
    """Message lu dans la file."""

    sujet: str
    cle: str | None
    valeur: dict[str, Any]
    position: str  # SQLite : identifiant ; Kafka : « partition:décalage »
    publie_le: float  # secondes depuis l'époque (heure de publication)
    brut: Any = None  # objet natif du client, nécessaire à la validation Kafka


class FileMessages(Protocol):
    """Interface commune aux deux implémentations."""

    def publier(self, sujet: str, valeur: dict[str, Any], cle: str | None = None) -> None: ...

    def vider(self) -> None: ...

    # Les valeurs par défaut (500 messages, 1 s) sont celles des implémentations ; ici « ... »
    # dit seulement que les deux paramètres sont facultatifs.
    def lire(
        self, sujet: str, groupe: str, max_messages: int = ..., attente_s: float = ...
    ) -> list[Message]: ...

    def valider(self, sujet: str, groupe: str, messages: list[Message]) -> None: ...

    def retard(self, sujet: str, groupe: str) -> int | None: ...

    def fermer(self) -> None: ...


# --------------------------------------------------------------------------------------------
# Implémentation locale : SQLite
# --------------------------------------------------------------------------------------------
def activer_wal(base: sqlite3.Connection, essais: int = 100) -> None:
    """Passe la base en mode WAL (lecteurs et écrivain ne se bloquent plus).

    Le premier passage en WAL demande un verrou exclusif que le délai d'attente de SQLite
    n'applique pas : si deux processus ouvrent la base en même temps, l'un reçoit « locked ».
    On réessaie quelques dixièmes de seconde ; une fois WAL activé, l'instruction est sans effet.
    """
    for essai in range(essais):
        try:
            base.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or essai == essais - 1:
                raise
            time.sleep(0.1)


class FileSQLite:
    """File persistante dans un fichier SQLite, partageable entre deux processus.

    Le mode WAL permet à un producteur d'écrire pendant qu'un consommateur lit. Chaque instance
    ouvre sa propre connexion : une instance par processus ou par fil d'exécution.
    """

    def __init__(self, chemin: Path | str) -> None:
        self._chemin = Path(chemin)
        if str(self._chemin) != ":memory:":
            self._chemin.parent.mkdir(parents=True, exist_ok=True)
        self._base = sqlite3.connect(str(self._chemin), timeout=30, isolation_level=None)
        activer_wal(self._base)
        self._base.execute("PRAGMA synchronous=NORMAL")
        self._base.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sujet TEXT NOT NULL,
                cle TEXT,
                valeur TEXT NOT NULL,
                publie_le REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_sujet ON messages (sujet, id);
            CREATE TABLE IF NOT EXISTS positions (
                sujet TEXT NOT NULL,
                groupe TEXT NOT NULL,
                dernier_id INTEGER NOT NULL,
                PRIMARY KEY (sujet, groupe)
            );
            """
        )

    def publier(self, sujet: str, valeur: dict[str, Any], cle: str | None = None) -> None:
        self._base.execute(
            "INSERT INTO messages (sujet, cle, valeur, publie_le) VALUES (?, ?, ?, ?)",
            (sujet, cle, json.dumps(valeur, ensure_ascii=False), time.time()),
        )

    def vider(self) -> None:
        """Sans objet : SQLite écrit immédiatement (mode autocommit)."""

    def _position(self, sujet: str, groupe: str) -> int:
        ligne = self._base.execute(
            "SELECT dernier_id FROM positions WHERE sujet = ? AND groupe = ?", (sujet, groupe)
        ).fetchone()
        return int(ligne[0]) if ligne else 0

    def lire(
        self, sujet: str, groupe: str, max_messages: int = 500, attente_s: float = 1.0
    ) -> list[Message]:
        echeance = time.monotonic() + attente_s
        while True:
            lignes = self._base.execute(
                "SELECT id, cle, valeur, publie_le FROM messages "
                "WHERE sujet = ? AND id > ? ORDER BY id LIMIT ?",
                (sujet, self._position(sujet, groupe), max_messages),
            ).fetchall()
            if lignes or time.monotonic() >= echeance:
                break
            time.sleep(min(0.1, max(0.0, echeance - time.monotonic())))
        return [
            Message(sujet, cle, json.loads(valeur), str(identifiant), float(publie))
            for identifiant, cle, valeur, publie in lignes
        ]

    def valider(self, sujet: str, groupe: str, messages: list[Message]) -> None:
        if not messages:
            return
        plus_grand = max(int(m.position) for m in messages)
        self._base.execute(
            "INSERT INTO positions (sujet, groupe, dernier_id) VALUES (?, ?, ?) "
            "ON CONFLICT (sujet, groupe) DO UPDATE SET "
            "dernier_id = MAX(dernier_id, excluded.dernier_id)",
            (sujet, groupe, plus_grand),
        )

    def retard(self, sujet: str, groupe: str) -> int | None:
        """Nombre de messages publiés mais pas encore validés par le groupe."""
        ligne = self._base.execute(
            "SELECT COUNT(*) FROM messages WHERE sujet = ? AND id > ?",
            (sujet, self._position(sujet, groupe)),
        ).fetchone()
        return int(ligne[0])

    def fermer(self) -> None:
        self._base.close()


# --------------------------------------------------------------------------------------------
# Implémentation de production : Kafka (client confluent-kafka), compatible Redpanda
# --------------------------------------------------------------------------------------------
class ErreurLivraison(RuntimeError):
    """Le courtier n'a pas accusé réception d'un message : la collecte doit le savoir."""


class FileKafka:
    """File Kafka. Le client est importé à la demande : le mode local n'en a pas besoin.

    Paramètres de fiabilité côté producteur : ``acks=all`` (le courtier confirme l'écriture) et
    ``enable.idempotence`` (une relance réseau ne crée pas de doublon dans le sujet).
    Côté consommateur : ``enable.auto.commit=false`` : la position n'avance qu'après l'écriture
    en base (voir ``valider``).
    """

    def __init__(
        self,
        serveurs: str,
        options: dict[str, Any] | None = None,
        *,
        fabrique_producteur: Callable[[dict[str, Any]], Any] | None = None,
        fabrique_consommateur: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        # « v4 » : Redpanda écoute en IPv4 ; sans cela, librdkafka tente d'abord l'IPv6 que le DNS
        # de Docker renvoie et inonde les journaux de « connection refused » avant de se replier.
        self._base = {
            "bootstrap.servers": serveurs,
            "broker.address.family": "v4",
            **(options or {}),
        }
        self._fabrique_producteur = fabrique_producteur or self._producteur_natif
        self._fabrique_consommateur = fabrique_consommateur or self._consommateur_natif
        self._producteur: Any = None
        self._consommateurs: dict[tuple[str, str], Any] = {}
        self._echecs: list[str] = []

    @staticmethod
    def _producteur_natif(configuration: dict[str, Any]) -> Any:
        from confluent_kafka import Producer

        return Producer(configuration)

    @staticmethod
    def _consommateur_natif(configuration: dict[str, Any]) -> Any:
        from confluent_kafka import Consumer

        return Consumer(configuration)

    def assurer_sujets(self, sujets: dict[str, int], replication: int = 1) -> None:
        """Crée les sujets manquants (sans effet s'ils existent déjà)."""
        from confluent_kafka.admin import AdminClient
        from confluent_kafka.cimpl import NewTopic

        admin = AdminClient(self._base)
        futurs = admin.create_topics(
            [
                NewTopic(nom, num_partitions=p, replication_factor=replication)
                for nom, p in sujets.items()
            ]
        )
        for futur in futurs.values():
            try:
                futur.result(timeout=30)
            except Exception as erreur:  # noqa: BLE001 - « existe déjà » est le cas normal
                if "EXISTS" not in str(erreur).upper():
                    raise

    # ----- Publication -----
    def _rapport_livraison(self, erreur: Any, message: Any) -> None:
        if erreur is not None:
            self._echecs.append(str(erreur))

    def publier(self, sujet: str, valeur: dict[str, Any], cle: str | None = None) -> None:
        if self._producteur is None:
            self._producteur = self._fabrique_producteur(
                {**self._base, "acks": "all", "enable.idempotence": True, "linger.ms": 20}
            )
        self._producteur.produce(
            sujet,
            key=cle.encode("utf-8") if cle else None,
            value=json.dumps(valeur, ensure_ascii=False).encode("utf-8"),
            on_delivery=self._rapport_livraison,
        )
        self._producteur.poll(0)

    def vider(self) -> None:
        """Attend l'accusé de réception de tout ce qui a été publié ; lève en cas d'échec."""
        if self._producteur is not None:
            restants = self._producteur.flush(30)
            if restants:
                raise ErreurLivraison(f"{restants} message(s) non livré(s) après 30 s.")
        if self._echecs:
            echecs, self._echecs = self._echecs, []
            raise ErreurLivraison("; ".join(echecs[:3]))

    # ----- Lecture -----
    def _consommateur(self, sujet: str, groupe: str) -> Any:
        cle = (sujet, groupe)
        if cle not in self._consommateurs:
            consommateur = self._fabrique_consommateur(
                {
                    **self._base,
                    "group.id": groupe,
                    "enable.auto.commit": False,
                    "auto.offset.reset": "earliest",
                }
            )
            consommateur.subscribe([sujet])
            self._consommateurs[cle] = consommateur
        return self._consommateurs[cle]

    def lire(
        self, sujet: str, groupe: str, max_messages: int = 500, attente_s: float = 1.0
    ) -> list[Message]:
        consommateur = self._consommateur(sujet, groupe)
        resultat: list[Message] = []
        for brut in consommateur.consume(num_messages=max_messages, timeout=attente_s):
            if brut.error():
                # Erreurs de partition ou de courtier : transitoires, on les laisse au journal.
                continue
            horodatage = brut.timestamp()
            publie = horodatage[1] / 1000 if horodatage and horodatage[1] > 0 else time.time()
            cle = brut.key().decode("utf-8") if brut.key() else None
            try:
                valeur = json.loads(brut.value().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Charge illisible : elle est transmise telle quelle comme « non objet » pour que
                # le consommateur la dirige vers la file des rejets au lieu de bloquer.
                valeur = {"__illisible__": True}
            resultat.append(
                Message(
                    sujet, cle, valeur, f"{brut.partition()}:{brut.offset()}", float(publie), brut
                )
            )
        return resultat

    def valider(self, sujet: str, groupe: str, messages: list[Message]) -> None:
        if not messages:
            return
        from confluent_kafka import TopicPartition

        plus_grand: dict[int, int] = {}
        for message in messages:
            partition, decalage = (int(x) for x in message.position.split(":"))
            plus_grand[partition] = max(plus_grand.get(partition, -1), decalage)
        # Kafka attend « le prochain décalage à lire » : dernier traité + 1.
        positions = [TopicPartition(sujet, p, d + 1) for p, d in plus_grand.items()]
        self._consommateur(sujet, groupe).commit(offsets=positions, asynchronous=False)

    def retard(self, sujet: str, groupe: str) -> int | None:
        return None  # mesuré côté courtier (outils Kafka) ; non exposé par ce client

    def fermer(self) -> None:
        if self._producteur is not None:
            self._producteur.flush(10)
        for consommateur in self._consommateurs.values():
            consommateur.close()
        self._consommateurs.clear()


def options_kafka_depuis_environnement() -> dict[str, Any]:
    """Options de sécurité du client Kafka, lues dans l'environnement (jamais dans le code).

    Variables : ``FRAUDE_KAFKA_PROTOCOLE_SECURITE`` (``PLAINTEXT``, ``SSL``, ``SASL_SSL``…),
    ``FRAUDE_KAFKA_SASL_MECANISME``, ``FRAUDE_KAFKA_UTILISATEUR``, ``FRAUDE_KAFKA_MOT_DE_PASSE``
    (secret) et ``FRAUDE_KAFKA_CA`` (chemin du certificat de l'autorité).
    """
    from detection_fraude.configuration import lire_secret

    options: dict[str, Any] = {}
    correspondances = {
        "PROTOCOLE_SECURITE": "security.protocol",
        "SASL_MECANISME": "sasl.mechanism",
        "UTILISATEUR": "sasl.username",
        "MOT_DE_PASSE": "sasl.password",
    }
    for suffixe, option in correspondances.items():
        valeur = lire_secret(f"KAFKA_{suffixe}")
        if valeur:
            options[option] = valeur
    chemin_ca = os.environ.get("FRAUDE_KAFKA_CA")  # chemin du fichier de l'autorité, pas un secret
    if chemin_ca:
        options["ssl.ca.location"] = chemin_ca
    return options


def construire_file(config: Any, options_kafka: dict[str, Any] | None = None) -> FileMessages:
    """Fabrique la file demandée par la configuration (``file.type``)."""
    if config.file.type == "kafka":
        file = FileKafka(
            config.file.kafka_serveurs,
            options_kafka if options_kafka is not None else options_kafka_depuis_environnement(),
        )
        file.assurer_sujets(
            {
                config.file.sujet_valides: config.file.partitions_valides,
                config.file.sujet_rejets: 1,
            },
            config.file.replication,
        )
        return file
    return FileSQLite(config.file.chemin_sqlite)
