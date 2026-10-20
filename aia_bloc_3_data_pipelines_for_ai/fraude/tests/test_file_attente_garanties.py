# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""File de messages : garanties de la file SQLite et du client Kafka simulé (critères 1.2, 4.2).

Les tests inverses prouvent ce qui est refusé (valeur non sérialisable, verrou persistant,
sujet refusé par le courtier, secret illisible) et que la file reste intacte. Le temps est
piloté par une horloge factice qui compte les pauses sans jamais dormir, et qui lève si une
boucle d'attente ne se termine pas : aucun test n'attend réellement ni n'ouvre de connexion.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from dataclasses import replace
from typing import Any

import pytest

import detection_fraude.file_attente as file_module
from detection_fraude.configuration import ErreurConfiguration
from detection_fraude.file_attente import (
    ErreurLivraison,
    FileKafka,
    FileSQLite,
    Message,
    activer_wal,
    construire_file,
    options_kafka_depuis_environnement,
)

VERROU = "database is locked"
HEURE = 1_700_000_000.0
VARIABLES_KAFKA = (
    "FRAUDE_KAFKA_PROTOCOLE_SECURITE",
    "FRAUDE_KAFKA_PROTOCOLE_SECURITE_FICHIER",
    "FRAUDE_KAFKA_SASL_MECANISME",
    "FRAUDE_KAFKA_SASL_MECANISME_FICHIER",
    "FRAUDE_KAFKA_UTILISATEUR",
    "FRAUDE_KAFKA_UTILISATEUR_FICHIER",
    "FRAUDE_KAFKA_MOT_DE_PASSE",
    "FRAUDE_KAFKA_MOT_DE_PASSE_FICHIER",
    "FRAUDE_KAFKA_CA",
)


# --------------------------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------------------------
class HorlogeFausse:
    """Remplace le module ``time`` : les pauses sont notées, jamais attendues, et bornées."""

    def __init__(self) -> None:
        self.maintenant = 0.0
        self.heure = HEURE
        self.pauses: list[float] = []
        self.a_chaque_pause: Any = None

    def time(self) -> float:
        return self.heure

    def monotonic(self) -> float:
        return self.maintenant

    def sleep(self, secondes: float) -> None:
        self.pauses.append(secondes)
        if len(self.pauses) > 200:
            raise AssertionError("trop de pauses : la boucle d'attente ne se termine pas")
        self.maintenant = round(self.maintenant + secondes, 9)
        if self.a_chaque_pause is not None:
            self.a_chaque_pause(len(self.pauses))


@pytest.fixture
def horloge(monkeypatch):
    fausse = HorlogeFausse()
    monkeypatch.setattr(file_module, "time", fausse)
    return fausse


class ConnexionScriptee:
    """Connexion dont chaque ``execute`` lève ou réussit selon un scénario."""

    def __init__(self, *resultats: Exception | None) -> None:
        self.resultats = list(resultats)
        self.requetes: list[str] = []

    def execute(self, sql: str) -> None:
        self.requetes.append(sql)
        resultat = self.resultats.pop(0) if self.resultats else None
        if resultat is not None:
            raise resultat


class FauxProducteur:
    def __init__(self, configuration: dict[str, Any], restants: int = 0) -> None:
        self.configuration = configuration
        self.restants = restants
        self.envoyes: list[tuple[str, Any, Any]] = []
        self.rappels: list[Any] = []
        self.polls: list[Any] = []
        self.flushs: list[Any] = []

    def produce(self, sujet, key=None, value=None, on_delivery=None):
        self.envoyes.append((sujet, key, value))
        self.rappels.append(on_delivery)

    def poll(self, delai):
        self.polls.append(delai)
        return 0

    def flush(self, delai=None):
        self.flushs.append(delai)
        return self.restants


class FauxMessageKafka:
    def __init__(self, partition, decalage, valeur, cle=None, erreur=None, horodatage=(1, 1_000)):
        self._p, self._o, self._v, self._k, self._e = partition, decalage, valeur, cle, erreur
        self._t = horodatage

    def error(self):
        return self._e

    def timestamp(self):
        return self._t

    def key(self):
        return self._k

    def value(self):
        return self._v

    def partition(self):
        return self._p

    def offset(self):
        return self._o


class FauxConsommateur:
    def __init__(self, configuration: dict[str, Any], messages: list[Any]) -> None:
        self.configuration = configuration
        self.messages = list(messages)
        self.souscriptions: list[Any] = []
        self.consommations: list[dict[str, Any]] = []
        self.valides: list[tuple[Any, Any]] = []
        self.fermetures = 0

    def subscribe(self, sujets):
        self.souscriptions.append(sujets)

    def consume(self, num_messages, timeout):
        self.consommations.append({"num_messages": num_messages, "timeout": timeout})
        lot, self.messages = self.messages[:num_messages], self.messages[num_messages:]
        return lot

    def commit(self, offsets, asynchronous):
        self.valides.append((offsets, asynchronous))

    def close(self):
        self.fermetures += 1


class FabriquesEspionnes:
    """Fabriques de producteur et de consommateur qui gardent les clients créés."""

    def __init__(self, messages: list[Any] | None = None, restants: int = 0) -> None:
        self.messages = messages or []
        self.restants = restants
        self.producteurs: list[FauxProducteur] = []
        self.consommateurs: list[FauxConsommateur] = []

    def producteur(self, configuration):
        self.producteurs.append(FauxProducteur(configuration, self.restants))
        return self.producteurs[-1]

    def consommateur(self, configuration):
        self.consommateurs.append(FauxConsommateur(configuration, self.messages))
        return self.consommateurs[-1]

    def file(self, serveurs="h:9092", options=None) -> FileKafka:
        return FileKafka(
            serveurs,
            options,
            fabrique_producteur=self.producteur,
            fabrique_consommateur=self.consommateur,
        )


class FauxFutur:
    def __init__(self, erreur: Exception | None) -> None:
        self.erreur = erreur
        self.delais: list[Any] = []

    def result(self, timeout=None):
        self.delais.append(timeout)
        if self.erreur is not None:
            raise self.erreur


@pytest.fixture
def faux_kafka(monkeypatch):
    """Injecte de faux modules ``confluent_kafka``, ``.admin`` et ``.cimpl`` (aucun courtier)."""
    etat = types.SimpleNamespace(
        configs=[], nouveaux=[], creations=[], erreurs={}, futurs={}, partitions=[]
    )

    class FauxNewTopic:
        def __init__(self, *args, **options):
            self.args = args
            self.options = options
            etat.nouveaux.append((args, options))

    class FauxAdminClient:
        def __init__(self, configuration):
            etat.configs.append(configuration)

        def create_topics(self, nouveaux):
            etat.creations.append(list(nouveaux))
            futurs = {n.args[0]: FauxFutur(etat.erreurs.get(n.args[0])) for n in nouveaux}
            etat.futurs.update(futurs)
            return futurs

    racine = types.ModuleType("confluent_kafka")
    racine.TopicPartition = lambda sujet, partition, decalage: (sujet, partition, decalage)
    admin = types.ModuleType("confluent_kafka.admin")
    admin.AdminClient = FauxAdminClient
    cimpl = types.ModuleType("confluent_kafka.cimpl")
    cimpl.NewTopic = FauxNewTopic
    monkeypatch.setitem(sys.modules, "confluent_kafka", racine)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", admin)
    monkeypatch.setitem(sys.modules, "confluent_kafka.cimpl", cimpl)
    return etat


def lignes_de(chemin, requete: str):
    """Résultat d'une requête sur la base de la file, lue sans passer par ``FileSQLite``."""
    base = sqlite3.connect(chemin)
    try:
        return base.execute(requete).fetchall()
    finally:
        base.close()


def message_sqlite(identifiant: str, valeur: dict[str, Any], cle: str | None = None) -> Message:
    return Message("s", cle, valeur, identifiant, HEURE)


# --------------------------------------------------------------------------------------------
# activer_wal
# --------------------------------------------------------------------------------------------
def test_un_verrou_persistant_epuise_les_essais_puis_remonte_l_erreur_et_un_verrou_bref_passe(
    horloge,
):
    """Trois verrous de suite : l'erreur finale remonte ; deux verrous puis un succès : aucune."""
    erreurs = [sqlite3.OperationalError(VERROU) for _ in range(3)]
    base = ConnexionScriptee(*erreurs)
    with pytest.raises(sqlite3.OperationalError) as exc:
        activer_wal(base, essais=3)
    assert exc.value is erreurs[2]
    assert base.requetes == ["PRAGMA journal_mode=WAL"] * 3
    assert horloge.pauses == [0.1, 0.1]

    horloge.pauses.clear()
    passagere = ConnexionScriptee(
        sqlite3.OperationalError(VERROU), sqlite3.OperationalError("database table is locked")
    )
    assert activer_wal(passagere, essais=3) is None
    assert passagere.requetes == ["PRAGMA journal_mode=WAL"] * 3
    assert horloge.pauses == [0.1, 0.1]


def test_cent_essais_par_defaut_avant_de_renoncer_a_un_verrou(horloge):
    """Sans argument, ``activer_wal`` essaie 100 fois (99 pauses) puis laisse passer l'erreur."""
    erreurs = [sqlite3.OperationalError(VERROU) for _ in range(100)]
    base = ConnexionScriptee(*erreurs)
    with pytest.raises(sqlite3.OperationalError) as exc:
        activer_wal(base)
    assert exc.value is erreurs[99]
    assert base.requetes == ["PRAGMA journal_mode=WAL"] * 100
    assert horloge.pauses == [0.1] * 99


def test_une_seule_tentative_autorisee_remonte_le_verrou_sans_pause(horloge):
    """``essais = 1`` : le premier verrou est le dernier, donc on n'attend pas."""
    verrou = sqlite3.OperationalError(VERROU)
    base = ConnexionScriptee(verrou)
    with pytest.raises(sqlite3.OperationalError) as exc:
        activer_wal(base, essais=1)
    assert exc.value is verrou
    assert base.requetes == ["PRAGMA journal_mode=WAL"]
    assert horloge.pauses == []


def test_une_erreur_autre_qu_un_verrou_n_est_jamais_retentee(horloge):
    """Un disque défaillant n'est pas un verrou : l'erreur remonte au premier essai."""
    panne = sqlite3.OperationalError("disk I/O error")
    base = ConnexionScriptee(panne, None)
    with pytest.raises(sqlite3.OperationalError) as exc:
        activer_wal(base)
    assert exc.value is panne
    assert str(exc.value) == "disk I/O error"
    assert base.requetes == ["PRAGMA journal_mode=WAL"]
    assert horloge.pauses == []


# --------------------------------------------------------------------------------------------
# FileSQLite : construction
# --------------------------------------------------------------------------------------------
def test_rouvrir_une_file_existante_en_wal_laisse_son_contenu_inchange(tmp_path):
    """Un second FileSQLite sur le même fichier active WAL sans rien effacer de la file."""
    chemin = tmp_path / "a" / "b" / "file.db"
    premiere = FileSQLite(chemin)
    premiere.publier("s", {"i": 1}, cle="k")
    premiere.valider("s", "g", premiere.lire("s", "g", attente_s=0))
    messages_avant = lignes_de(chemin, "SELECT id, sujet, cle, valeur FROM messages")
    positions_avant = lignes_de(chemin, "SELECT sujet, groupe, dernier_id FROM positions")
    deuxieme = FileSQLite(chemin)
    assert lignes_de(chemin, "SELECT id, sujet, cle, valeur FROM messages") == messages_avant
    assert lignes_de(chemin, "SELECT sujet, groupe, dernier_id FROM positions") == positions_avant
    assert messages_avant == [(1, "s", "k", '{"i": 1}')]
    assert positions_avant == [("s", "g", 1)]
    assert lignes_de(chemin, "PRAGMA journal_mode") == [("wal",)]
    objets = lignes_de(
        chemin, "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )
    assert sorted(objets) == [
        ("index", "idx_messages_sujet"),
        ("table", "messages"),
        ("table", "positions"),
    ]
    premiere.fermer()
    deuxieme.fermer()


def test_la_connexion_a_un_delai_de_30_s_et_un_chemin_impossible_n_en_ouvre_aucune(
    tmp_path, monkeypatch
):
    """Verrou attendu 30 s, transactions laissées à SQLite ; dossier impossible : rien d'ouvert."""
    appels: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    connecter = sqlite3.connect

    def connecter_espion(*args, **options):
        appels.append((args, options))
        return connecter(*args, **options)

    monkeypatch.setattr(sqlite3, "connect", connecter_espion)
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    assert appels == [((str(chemin),), {"timeout": 30, "isolation_level": None})]
    file.fermer()
    fichier = tmp_path / "pas_un_dossier"
    fichier.write_text("x", encoding="utf-8")
    appels_avant = list(appels)
    with pytest.raises(OSError):
        FileSQLite(fichier / "file.db")
    assert appels == appels_avant


def test_le_chemin_memoire_ne_cree_ni_dossier_ni_fichier_et_laisse_le_dossier_courant_inchange(
    tmp_path, monkeypatch
):
    """``:memory:`` n'est pas un fichier : rien n'apparaît dans le dossier courant."""
    monkeypatch.chdir(tmp_path)
    contenu_avant = sorted(tmp_path.iterdir())
    file = FileSQLite(":memory:")
    file.publier("s", {"i": 1})
    assert [m.valeur for m in file.lire("s", "g", attente_s=0)] == [{"i": 1}]
    assert sorted(tmp_path.iterdir()) == contenu_avant
    file.fermer()


# --------------------------------------------------------------------------------------------
# FileSQLite : publication, lecture, validation
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("valeur", "message"),
    [
        ({"x": {1, 2}}, "Object of type set is not JSON serializable"),
        ({"x": object()}, "Object of type object is not JSON serializable"),
        ({(1, 2): "tuple"}, "keys must be str, int, float, bool or None, not tuple"),
    ],
)
def test_une_valeur_non_serialisable_est_refusee_et_la_file_reste_inchangee(
    tmp_path, horloge, valeur, message
):
    """Rien d'à moitié écrit : le refus a lieu avant l'insertion, la base garde ses messages."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    file.publier("s", {"ok": 1})
    messages_avant = lignes_de(chemin, "SELECT id, sujet, cle, valeur, publie_le FROM messages")
    with pytest.raises(TypeError) as exc:
        file.publier("s", valeur, cle="k")
    assert str(exc.value) == message
    assert lignes_de(chemin, "SELECT id, sujet, cle, valeur, publie_le FROM messages") == (
        messages_avant
    )
    file.fermer()


def test_publier_range_sujet_cle_json_lisible_et_heure_sans_toucher_aux_positions(
    tmp_path, horloge
):
    """Le message est stocké tel quel (UTF-8 lisible, heure de publication) ; les positions non."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    file.publier("valides", {"texte": "Générale d'été", "n": 1}, cle="carte-1")
    file.publier("rejets", {"r": 1})
    positions_avant = lignes_de(chemin, "SELECT * FROM positions")
    file.publier("valides", {"i": 2})
    assert lignes_de(chemin, "SELECT sujet, cle, valeur, publie_le FROM messages ORDER BY id") == [
        ("valides", "carte-1", '{"texte": "Générale d\'été", "n": 1}', HEURE),
        ("rejets", None, '{"r": 1}', HEURE),
        ("valides", None, '{"i": 2}', HEURE),
    ]
    assert lignes_de(chemin, "SELECT * FROM positions") == positions_avant
    assert positions_avant == []
    file.fermer()


def test_lire_ne_consomme_rien_et_renvoie_les_messages_complets_dans_l_ordre(tmp_path, horloge):
    """Lire n'avance aucune position : mêmes messages au second appel, retard intact."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    file.publier("s", {"i": 0}, cle="k0")
    file.publier("autre", {"i": 9})
    file.publier("s", {"i": 1})
    file.publier("s", {"i": 2}, cle="k2")
    retard_avant = file.retard("s", "g")
    premiere = file.lire("s", "g", attente_s=0)
    assert premiere == [
        Message("s", "k0", {"i": 0}, "1", HEURE),
        Message("s", None, {"i": 1}, "3", HEURE),
        Message("s", "k2", {"i": 2}, "4", HEURE),
    ]
    assert premiere[0].brut is None
    assert file.lire("s", "g", attente_s=0) == premiere
    assert file.retard("s", "g") == retard_avant == 3
    assert lignes_de(chemin, "SELECT * FROM positions") == []
    file.fermer()


def test_lire_borne_le_lot_et_ne_renvoie_que_ce_qui_suit_la_position_du_groupe(tmp_path, horloge):
    """``max_messages`` borne le lot (500 par défaut) ; seuls les ids après la position sortent."""
    file = FileSQLite(tmp_path / "file.db")
    for i in range(6):
        file.publier("s", {"i": i})
    retard_avant = file.retard("s", "g")
    assert [m.valeur["i"] for m in file.lire("s", "g", max_messages=1, attente_s=0)] == [0]
    assert [m.valeur["i"] for m in file.lire("s", "g", max_messages=3, attente_s=0)] == [0, 1, 2]
    assert file.lire("s", "g", max_messages=0, attente_s=0) == []
    assert file.retard("s", "g") == retard_avant
    lot = file.lire("s", "g", max_messages=2, attente_s=0)
    file.valider("s", "g", lot)
    assert [m.valeur["i"] for m in file.lire("s", "g", attente_s=0)] == [2, 3, 4, 5]
    autre = file.lire("s", "autre-groupe", max_messages=2, attente_s=0)
    assert [m.valeur["i"] for m in autre] == [0, 1]
    for i in range(6, 501):
        file.publier("s", {"i": i})
    par_defaut = file.lire("s", "groupe-neuf", attente_s=0)
    assert [m.valeur["i"] for m in par_defaut] == list(range(500))
    file.fermer()


def test_la_lecture_bloquante_attend_par_pas_de_100_ms_sans_rien_creer_dans_la_file(
    tmp_path, horloge
):
    """Vide : pauses de 0,1 s puis le reste ; un message arrivé pendant l'attente est renvoyé."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    messages_avant = lignes_de(chemin, "SELECT * FROM messages")
    assert file.lire("s", "g", attente_s=0.25) == []
    assert horloge.pauses == pytest.approx([0.1, 0.1, 0.05])
    assert lignes_de(chemin, "SELECT * FROM messages") == messages_avant

    horloge.pauses.clear()
    horloge.maintenant = 0.0
    assert file.lire("s", "g") == []
    assert sum(horloge.pauses) == pytest.approx(1.0)

    horloge.pauses.clear()
    horloge.maintenant = 0.0
    assert file.lire("s", "g", attente_s=0) == []
    assert file.lire("s", "g", attente_s=-1) == []
    assert horloge.pauses == []

    auteur = FileSQLite(chemin)

    def publier_a_la_deuxieme_pause(numero):
        if numero == 2:
            auteur.publier("s", {"arrive": True})

    horloge.a_chaque_pause = publier_a_la_deuxieme_pause
    horloge.maintenant = 0.0
    lus = file.lire("s", "g", attente_s=5)
    assert [m.valeur for m in lus] == [{"arrive": True}]
    assert horloge.pauses == [0.1, 0.1]
    auteur.fermer()
    file.fermer()


def test_valider_une_liste_vide_laisse_les_positions_inchangees(tmp_path):
    """Valider « rien » ne crée aucune position et ne fait pas avancer le groupe."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    file.publier("s", {"i": 1})
    positions_avant = lignes_de(chemin, "SELECT * FROM positions")
    assert file.valider("s", "g", []) is None
    assert lignes_de(chemin, "SELECT * FROM positions") == positions_avant == []
    assert file.retard("s", "g") == 1
    file.fermer()


def test_la_position_se_compare_en_nombre_et_ne_recule_jamais(tmp_path):
    """Positions « 9 » et « 10 » : 10 l'emporte (nombre, pas texte) ; une ancienne n'écrase pas."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    file.valider(
        "s", "g", [message_sqlite("9", {}), message_sqlite("10", {}), message_sqlite("2", {})]
    )
    assert lignes_de(chemin, "SELECT sujet, groupe, dernier_id FROM positions") == [("s", "g", 10)]
    dernier_id_avant = lignes_de(chemin, "SELECT dernier_id FROM positions")
    file.valider("s", "g", [message_sqlite("3", {})])
    assert lignes_de(chemin, "SELECT dernier_id FROM positions") == dernier_id_avant
    file.valider("s", "g", [message_sqlite("11", {})])
    assert lignes_de(chemin, "SELECT sujet, groupe, dernier_id FROM positions") == [("s", "g", 11)]
    file.fermer()


def test_valider_un_groupe_ou_un_sujet_ne_change_ni_le_retard_ni_la_lecture_des_autres(tmp_path):
    """Les positions sont par (sujet, groupe) : un groupe qui avance n'entraîne pas un autre."""
    file = FileSQLite(tmp_path / "file.db")
    for i in range(4):
        file.publier("s", {"i": i})
    file.publier("t", {"i": 99})
    retard_autre_avant = file.retard("s", "autre")
    retard_t_avant = file.retard("t", "g")
    file.valider("s", "g", file.lire("s", "g", max_messages=3, attente_s=0))
    assert file.retard("s", "g") == 1
    assert file.retard("s", "autre") == retard_autre_avant
    assert file.retard("t", "g") == retard_t_avant
    assert (retard_autre_avant, retard_t_avant) == (4, 1)
    assert file.retard("inconnu", "g") == 0
    assert isinstance(file.retard("s", "g"), int)
    file.fermer()


def test_apres_fermeture_toute_operation_est_refusee_et_la_base_reste_inchangee(tmp_path):
    """Une file fermée ne publie ni ne lit plus : l'erreur de SQLite remonte, rien n'est écrit."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    file.publier("s", {"i": 1})
    messages_avant = lignes_de(chemin, "SELECT id, valeur FROM messages")
    file.fermer()
    with pytest.raises(sqlite3.ProgrammingError) as exc:
        file.publier("s", {"i": 2})
    assert str(exc.value) == "Cannot operate on a closed database."
    with pytest.raises(sqlite3.ProgrammingError):
        file.lire("s", "g", attente_s=0)
    assert lignes_de(chemin, "SELECT id, valeur FROM messages") == messages_avant


def test_vider_sur_sqlite_ne_fait_rien_et_laisse_la_file_inchangee(tmp_path):
    """SQLite écrit tout de suite : ``vider`` ne renvoie rien et ne touche pas aux messages."""
    chemin = tmp_path / "file.db"
    file = FileSQLite(chemin)
    file.publier("s", {"i": 1})
    messages_avant = lignes_de(chemin, "SELECT id, valeur FROM messages")
    assert file.vider() is None
    assert lignes_de(chemin, "SELECT id, valeur FROM messages") == messages_avant
    file.fermer()


def test_aucun_client_n_est_cree_tant_que_rien_n_est_publie_ou_lu_les_fabriques_restent_intactes():
    """Le mode local n'a pas besoin de Kafka : vider, retard, fermer, valider([]) sont inertes."""
    fabriques = FabriquesEspionnes()
    producteurs_avant = list(fabriques.producteurs)
    consommateurs_avant = list(fabriques.consommateurs)
    file = fabriques.file()
    file.vider()
    assert file.retard("s", "g") is None
    file.valider("s", "g", [])
    file.fermer()
    file.fermer()
    assert fabriques.producteurs == producteurs_avant
    assert fabriques.consommateurs == consommateurs_avant
    assert producteurs_avant == consommateurs_avant == []


def test_les_options_ne_peuvent_pas_affaiblir_la_fiabilite_du_producteur_et_restent_inchangees():
    """acks=all, idempotence et 20 ms l'emportent ; seule la famille d'adresse est modifiable."""
    fabriques = FabriquesEspionnes()
    options = {"acks": "0", "enable.idempotence": False, "linger.ms": 500, "sasl.username": "u"}
    options_avant = dict(options)
    file = fabriques.file("h:9092", options)
    file.publier("s", {"i": 1}, cle="c")
    file.publier("s", {"i": 2})
    assert options == options_avant
    assert fabriques.producteurs[0].configuration == {
        "bootstrap.servers": "h:9092",
        "broker.address.family": "v4",
        "sasl.username": "u",
        "acks": "all",
        "enable.idempotence": True,
        "linger.ms": 20,
    }
    assert fabriques.producteurs[1:] == []
    autre = FabriquesEspionnes()
    autre.file("k:1", {"broker.address.family": "any"}).publier("s", {})
    assert autre.producteurs[0].configuration["broker.address.family"] == "any"
    assert autre.producteurs[0].configuration["bootstrap.servers"] == "k:1"


def test_publier_envoie_json_utf8_avec_cle_et_interroge_le_client_sans_jamais_vider():
    """Valeur JSON UTF-8, clé encodée (absente si vide), poll(0) et jamais de flush."""
    fabriques = FabriquesEspionnes()
    file = fabriques.file()
    file.publier("valides", {"texte": "été"}, cle="carte-é")
    producteur = fabriques.producteurs[0]
    flushs_avant = list(producteur.flushs)
    file.publier("rejets", {"n": 1})
    file.publier("rejets", {"n": 2}, cle="")
    assert producteur.flushs == flushs_avant
    assert producteur.envoyes == [
        ("valides", "carte-é".encode(), '{"texte": "été"}'.encode()),
        ("rejets", None, b'{"n": 1}'),
        ("rejets", None, b'{"n": 2}'),
    ]
    assert producteur.polls == [0, 0, 0]
    assert all(callable(rappel) for rappel in producteur.rappels)


def test_une_file_locale_pleine_remonte_l_erreur_du_client_sans_interroger_les_livraisons():
    """Si ``produce`` refuse (file locale pleine), l'erreur remonte sans appeler ``poll``."""
    fabriques = FabriquesEspionnes()
    file = fabriques.file()
    file.publier("s", {"i": 1})
    producteur = fabriques.producteurs[0]
    polls_avant = list(producteur.polls)
    saturation = BufferError("Local: Queue full")

    def produce_sature(*args, **options):
        raise saturation

    producteur.produce = produce_sature
    with pytest.raises(BufferError) as exc:
        file.publier("s", {"i": 2})
    assert exc.value is saturation
    assert producteur.polls == polls_avant
    assert producteur.envoyes == [("s", None, b'{"i": 1}')]


def test_une_valeur_non_serialisable_n_est_jamais_envoyee_au_courtier():
    """Le refus a lieu avant ``produce`` : rien ne part, et le client n'est pas interrogé."""
    fabriques = FabriquesEspionnes()
    file = fabriques.file()
    with pytest.raises(TypeError) as exc:
        file.publier("s", {"x": {1, 2}}, cle="k")
    assert str(exc.value) == "Object of type set is not JSON serializable"
    assert fabriques.producteurs[0].envoyes == []
    assert fabriques.producteurs[0].polls == []


def test_une_panne_du_client_a_la_lecture_remonte_sans_rien_valider():
    """``consume`` qui échoue (courtier injoignable) n'est pas masqué ; aucune position engagée."""
    fabriques = FabriquesEspionnes()
    file = fabriques.file()
    file.lire("s", "g", attente_s=0)
    consommateur = fabriques.consommateurs[0]
    engagements_avant = list(consommateur.valides)
    panne = ConnectionError("courtier injoignable")

    def consume_en_panne(num_messages, timeout):
        raise panne

    consommateur.consume = consume_en_panne
    with pytest.raises(ConnectionError) as exc:
        file.lire("s", "g", attente_s=0)
    assert exc.value is panne
    assert consommateur.valides == engagements_avant


# --------------------------------------------------------------------------------------------
# FileKafka : accusés de réception
# --------------------------------------------------------------------------------------------
def test_des_messages_non_livres_apres_30_secondes_font_echouer_vider_avec_leur_nombre():
    """``flush(30)`` qui laisse 2 messages : ErreurLivraison avec le compte et le délai."""
    fabriques = FabriquesEspionnes(restants=2)
    file = fabriques.file()
    file.publier("s", {"i": 1})
    with pytest.raises(ErreurLivraison) as exc:
        file.vider()
    assert str(exc.value) == "2 message(s) non livré(s) après 30 s."
    assert fabriques.producteurs[0].flushs == [30]


def test_les_echecs_de_livraison_sont_rapportes_trois_par_trois_puis_oublies():
    """Au plus trois causes dans le message ; une fois signalés, les échecs ne reviennent pas."""
    fabriques = FabriquesEspionnes()
    file = fabriques.file()
    file.publier("s", {"i": 1})
    rapport = fabriques.producteurs[0].rappels[0]
    for cause in ("e1", "e2", "e3", "e4"):
        rapport(cause, None)
    rapport(None, "livré")
    with pytest.raises(ErreurLivraison) as exc:
        file.vider()
    assert str(exc.value) == "e1; e2; e3"
    echecs_avant = fabriques.producteurs[0].flushs[:]
    file.vider()
    assert fabriques.producteurs[0].flushs == [*echecs_avant, 30]


# --------------------------------------------------------------------------------------------
# FileKafka : lecture et validation
# --------------------------------------------------------------------------------------------
def test_lire_cree_un_consommateur_sans_validation_automatique_et_ne_valide_rien():
    """Un consommateur par (sujet, groupe), auto-commit coupé, position intacte après lecture."""
    fabriques = FabriquesEspionnes(messages=[FauxMessageKafka(0, 1, b"{}")])
    options = {"enable.auto.commit": True, "auto.offset.reset": "latest", "group.id": "intrus"}
    file = fabriques.file("h:9092", options)
    file.lire("valides", "g")
    consommateur = fabriques.consommateurs[0]
    assert consommateur.configuration == {
        "bootstrap.servers": "h:9092",
        "broker.address.family": "v4",
        "group.id": "g",
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    }
    engagements_avant = list(consommateur.valides)
    file.lire("valides", "g", max_messages=7, attente_s=0.25)
    assert consommateur.valides == engagements_avant == []
    assert consommateur.souscriptions == [["valides"]]
    assert consommateur.consommations == [
        {"num_messages": 500, "timeout": 1.0},
        {"num_messages": 7, "timeout": 0.25},
    ]
    assert len(fabriques.consommateurs) == 1
    file.lire("valides", "autre")
    file.lire("rejets", "g")
    assert [c.configuration["group.id"] for c in fabriques.consommateurs] == ["g", "autre", "g"]
    assert [c.souscriptions for c in fabriques.consommateurs] == [
        [["valides"]],
        [["valides"]],
        [["rejets"]],
    ]


def test_lire_convertit_horodatage_cle_et_valeur_et_ecarte_les_erreurs_sans_rien_valider(horloge):
    """Horodatage en secondes (heure locale si absent ou nul), clé décodée, valeur JSON brute."""
    horloge.heure = 1_700_000_500.0
    messages = [
        FauxMessageKafka(0, 10, b'{"i": 1}', b"carte-1", horodatage=(1, 1_700_000_000_123)),
        FauxMessageKafka(1, 4, b'{"i": 2}', None, horodatage=(0, -1)),
        FauxMessageKafka(0, 11, b"[1, 2]", b"", horodatage=None),
        FauxMessageKafka(2, 0, b"\xff\xfe", b"k", horodatage=(1, 0)),
        FauxMessageKafka(0, 12, b"{pas json", None, horodatage=(1, 1)),
        FauxMessageKafka(0, 13, b"{}", erreur="erreur de partition"),
        FauxMessageKafka(0, 14, b"\xff", erreur="autre erreur"),
        FauxMessageKafka(3, 7, b'"texte"', None, horodatage=()),
    ]
    fabriques = FabriquesEspionnes(messages=messages)
    file = fabriques.file()
    lus = file.lire("valides", "g", max_messages=100, attente_s=0)
    engagements_avant: list[Any] = []
    assert fabriques.consommateurs[0].valides == engagements_avant
    assert lus == [
        Message("valides", "carte-1", {"i": 1}, "0:10", 1_700_000_000.123, messages[0]),
        Message("valides", None, {"i": 2}, "1:4", 1_700_000_500.0, messages[1]),
        Message("valides", None, [1, 2], "0:11", 1_700_000_500.0, messages[2]),
        Message("valides", "k", {"__illisible__": True}, "2:0", 1_700_000_500.0, messages[3]),
        Message("valides", None, {"__illisible__": True}, "0:12", 0.001, messages[4]),
        Message("valides", None, "texte", "3:7", 1_700_000_500.0, messages[7]),
    ]
    origines = [*messages[:5], messages[7]]
    assert all(lu.brut is origine for lu, origine in zip(lus, origines, strict=True))
    assert all(isinstance(lu.publie_le, float) for lu in lus)


def test_valider_n_engage_que_le_dernier_decalage_par_partition_et_refuse_une_position_illisible(
    faux_kafka,
):
    """Dernier traité + 1 par partition, en synchrone ; une position mal formée n'engage rien."""
    messages = [
        FauxMessageKafka(0, 10, b"{}"),
        FauxMessageKafka(1, 4, b"{}"),
        FauxMessageKafka(0, 12, b"{}"),
        FauxMessageKafka(0, 11, b"{}"),
        FauxMessageKafka(2, 0, b"{}"),
    ]
    fabriques = FabriquesEspionnes(messages=messages)
    file = fabriques.file()
    lus = file.lire("valides", "g", max_messages=10, attente_s=0)
    file.valider("valides", "g", lus)
    consommateur = fabriques.consommateurs[0]
    assert consommateur.valides == [
        ([("valides", 0, 13), ("valides", 1, 5), ("valides", 2, 1)], False)
    ]
    file.valider("valides", "g", lus[:2])
    assert consommateur.valides[1] == ([("valides", 0, 11), ("valides", 1, 5)], False)
    assert fabriques.consommateurs[1:] == []
    engagements_avant = list(consommateur.valides)
    for position in ("abc", "1:2:3", "x:1", "1:y", ""):
        illisible = Message("valides", None, {}, position, HEURE)
        with pytest.raises(ValueError):
            file.valider("valides", "g", [illisible])
    assert consommateur.valides == engagements_avant


def test_valider_un_groupe_n_engage_pas_les_positions_d_un_autre_groupe(faux_kafka):
    """La validation passe par le consommateur du bon groupe ; l'autre reste intact."""
    fabriques = FabriquesEspionnes(messages=[FauxMessageKafka(0, 5, b"{}")])
    file = fabriques.file()
    lus = file.lire("valides", "a", attente_s=0)
    engagements_avant = list(fabriques.consommateurs[0].valides)
    file.valider("valides", "b", lus)
    assert fabriques.consommateurs[0].valides == engagements_avant == []
    assert fabriques.consommateurs[1].configuration["group.id"] == "b"
    assert fabriques.consommateurs[1].souscriptions == [["valides"]]
    assert fabriques.consommateurs[1].valides == [([("valides", 0, 6)], False)]


def test_valider_rien_ne_cree_aucun_consommateur_et_n_engage_rien():
    """Une liste vide ne doit ni créer de client ni importer le module Kafka."""
    fabriques = FabriquesEspionnes()
    file = fabriques.file()
    consommateurs_avant = list(fabriques.consommateurs)
    file.valider("valides", "g", [])
    assert fabriques.consommateurs == consommateurs_avant
    assert consommateurs_avant == []


def test_fermer_vide_le_producteur_ferme_chaque_consommateur_une_fois_et_oublie_les_clients(
    faux_kafka,
):
    """flush(10) du producteur, close() de chaque consommateur ; refermer est sans effet."""
    fabriques = FabriquesEspionnes(messages=[FauxMessageKafka(0, 1, b"{}")])
    file = fabriques.file()
    file.publier("s", {"i": 1})
    file.lire("valides", "a", attente_s=0)
    file.lire("rejets", "b", attente_s=0)
    file.fermer()
    assert fabriques.producteurs[0].flushs == [10]
    fermetures_avant = [c.fermetures for c in fabriques.consommateurs]
    assert fermetures_avant == [1, 1]
    file.fermer()
    assert [c.fermetures for c in fabriques.consommateurs] == fermetures_avant
    file.lire("valides", "a", attente_s=0)
    assert fabriques.consommateurs[2:] != []


def test_les_sujets_kafka_sont_crees_avec_partitions_replication_delai_et_options(
    faux_kafka, config_kafka, env_kafka
):
    """Chaque sujet : ses partitions, la réplication, 30 s d'attente ; options intactes."""
    file = FileKafka("k:9092", {"sasl.username": "u"})
    file.assurer_sujets({"valides": 3, "rejets": 1}, replication=2)
    assert faux_kafka.configs == [
        {"bootstrap.servers": "k:9092", "broker.address.family": "v4", "sasl.username": "u"}
    ]
    assert faux_kafka.nouveaux == [
        (("valides",), {"num_partitions": 3, "replication_factor": 2}),
        (("rejets",), {"num_partitions": 1, "replication_factor": 2}),
    ]
    assert len(faux_kafka.creations) == 1
    assert [f.delais for f in faux_kafka.futurs.values()] == [[30], [30]]
    FileKafka("k:9092").assurer_sujets({"seul": 4})
    assert faux_kafka.nouveaux[2] == (("seul",), {"num_partitions": 4, "replication_factor": 1})

    faux_kafka.configs.clear()
    faux_kafka.nouveaux.clear()
    options = {"sasl.username": "explicite"}
    options_avant = dict(options)
    assert isinstance(construire_file(config_kafka, options), FileKafka)
    assert options == options_avant
    base = {"bootstrap.servers": "k:9092", "broker.address.family": "v4"}
    assert faux_kafka.configs == [{**base, "sasl.username": "explicite"}]
    assert faux_kafka.nouveaux == [
        (("v",), {"num_partitions": 5, "replication_factor": 2}),
        (("r",), {"num_partitions": 1, "replication_factor": 2}),
    ]
    faux_kafka.configs.clear()
    env_kafka.setenv("FRAUDE_KAFKA_UTILISATEUR", "depuis-l-environnement")
    construire_file(config_kafka)
    construire_file(config_kafka, {})
    assert faux_kafka.configs == [{**base, "sasl.username": "depuis-l-environnement"}, base]


@pytest.mark.parametrize(
    ("message", "tolere"),
    [
        ("TOPIC_ALREADY_EXISTS", True),
        ("topic already exists", True),
        ("KafkaError{code=TOPIC_ALREADY_EXISTS,val=36,str=\"Topic 'x' already exists.\"}", True),
        ("Exists", True),
        ("Broker: Authorization failed", False),
        ("Timed out", False),
        ("TOPIC_AUTHORIZATION_FAILED", False),
        ("", False),
    ],
)
def test_seul_un_sujet_deja_existant_est_tolere_toute_autre_erreur_est_propagee_telle_quelle(
    faux_kafka, message, tolere
):
    """« Existe déjà » (toute casse) est normal à un redémarrage ; un refus ne l'est jamais."""
    erreur = RuntimeError(message)
    faux_kafka.erreurs["valides"] = erreur
    if tolere:
        FileKafka("k:9092").assurer_sujets({"valides": 3, "rejets": 1})
        assert [f.delais for f in faux_kafka.futurs.values()] == [[30], [30]]
    else:
        with pytest.raises(RuntimeError) as exc:
            FileKafka("k:9092").assurer_sujets({"valides": 3, "rejets": 1})
        assert exc.value is erreur


def test_une_erreur_apres_un_sujet_existant_est_quand_meme_propagee(faux_kafka):
    """Tolérer le premier sujet ne dispense pas de contrôler le suivant."""
    faux_kafka.erreurs["valides"] = RuntimeError("TOPIC_ALREADY_EXISTS")
    refus = RuntimeError("Broker: Authorization failed")
    faux_kafka.erreurs["rejets"] = refus
    with pytest.raises(RuntimeError) as exc:
        FileKafka("k:9092").assurer_sujets({"valides": 3, "rejets": 1})
    assert exc.value is refus
    assert [f.delais for f in faux_kafka.futurs.values()] == [[30], [30]]


# --------------------------------------------------------------------------------------------
# options_kafka_depuis_environnement
# --------------------------------------------------------------------------------------------
@pytest.fixture
def env_kafka(monkeypatch):
    for variable in VARIABLES_KAFKA:
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


@pytest.mark.parametrize("valeur", ["", "   ", "\n"])
def test_un_secret_vide_ou_blanc_est_ignore_et_les_options_restent_inchangees(env_kafka, valeur):
    """Une valeur vide ou blanche n'est pas un secret : elle n'ajoute aucune option."""
    options_avant = options_kafka_depuis_environnement()
    for variable in (
        "FRAUDE_KAFKA_PROTOCOLE_SECURITE",
        "FRAUDE_KAFKA_SASL_MECANISME",
        "FRAUDE_KAFKA_UTILISATEUR",
        "FRAUDE_KAFKA_MOT_DE_PASSE",
    ):
        env_kafka.setenv(variable, valeur)
    env_kafka.setenv("FRAUDE_KAFKA_CA", "")
    assert options_kafka_depuis_environnement() == options_avant == {}


def test_options_kafka_de_l_environnement_fichier_prioritaire_et_secret_illisible_refuse(
    env_kafka, tmp_path
):
    """Une variable = une option ; le fichier prime sur la variable ; un fichier absent bloque."""
    tout = {
        "FRAUDE_KAFKA_PROTOCOLE_SECURITE": "SASL_SSL",
        "FRAUDE_KAFKA_SASL_MECANISME": "SCRAM-SHA-512",
        "FRAUDE_KAFKA_UTILISATEUR": "fraude",
        "FRAUDE_KAFKA_MOT_DE_PASSE": "secret-test",
        "FRAUDE_KAFKA_CA": "/etc/ca.pem",
    }
    options_de = {
        "FRAUDE_KAFKA_PROTOCOLE_SECURITE": "security.protocol",
        "FRAUDE_KAFKA_SASL_MECANISME": "sasl.mechanism",
        "FRAUDE_KAFKA_UTILISATEUR": "sasl.username",
        "FRAUDE_KAFKA_MOT_DE_PASSE": "sasl.password",
        "FRAUDE_KAFKA_CA": "ssl.ca.location",
    }
    scenarios = [({}, {}), *[({v: tout[v]}, {options_de[v]: tout[v]}) for v in tout]]
    scenarios.append((tout, {options_de[v]: valeur for v, valeur in tout.items()}))
    for environnement, attendu in scenarios:
        for variable in VARIABLES_KAFKA:
            env_kafka.delenv(variable, raising=False)
        for variable, valeur in environnement.items():
            env_kafka.setenv(variable, valeur)
        assert options_kafka_depuis_environnement() == attendu

    for variable in VARIABLES_KAFKA:
        env_kafka.delenv(variable, raising=False)
    fichier = tmp_path / "mot_de_passe"
    fichier.write_text("depuis-le-fichier\n", encoding="utf-8")
    env_kafka.setenv("FRAUDE_KAFKA_MOT_DE_PASSE", "depuis-la-variable")
    env_kafka.setenv("FRAUDE_KAFKA_MOT_DE_PASSE_FICHIER", str(fichier))
    assert options_kafka_depuis_environnement() == {"sasl.password": "depuis-le-fichier"}
    env_kafka.setenv("FRAUDE_KAFKA_MOT_DE_PASSE_FICHIER", str(tmp_path / "absent"))
    with pytest.raises(ErreurConfiguration) as exc:
        options_kafka_depuis_environnement()
    assert str(exc.value) == (
        "Le fichier de secret indiqué par FRAUDE_KAFKA_MOT_DE_PASSE_FICHIER est illisible "
        "(FileNotFoundError)."
    )


# --------------------------------------------------------------------------------------------
# construire_file
# --------------------------------------------------------------------------------------------
@pytest.fixture
def config_kafka(config):
    fichier = replace(
        config.file,
        type="kafka",
        kafka_serveurs="k:9092",
        sujet_valides="v",
        sujet_rejets="r",
        partitions_valides=5,
        replication=2,
    )
    return replace(config, file=fichier)


def test_construire_file_kafka_propage_un_refus_du_courtier_sans_renvoyer_de_file(
    config_kafka, faux_kafka, env_kafka
):
    """Si les sujets ne peuvent pas être créés, le démarrage échoue avec l'erreur d'origine."""
    refus = RuntimeError("Broker: Authorization failed")
    faux_kafka.erreurs["v"] = refus
    with pytest.raises(RuntimeError) as exc:
        construire_file(config_kafka, {})
    assert exc.value is refus


def test_construire_file_sqlite_ne_touche_pas_a_kafka_et_laisse_les_sujets_inchanges(
    config, faux_kafka
):
    """Hors ``kafka``, la file est la base SQLite du chemin configuré ; aucun courtier contacté."""
    configs_avant = list(faux_kafka.configs)
    creations_avant = list(faux_kafka.creations)
    assert not config.file.chemin_sqlite.exists()
    file = construire_file(config)
    assert isinstance(file, FileSQLite)
    assert config.file.chemin_sqlite.exists()
    assert faux_kafka.configs == configs_avant
    assert faux_kafka.creations == creations_avant
    file.fermer()


def test_le_message_ecrit_sous_forme_json_est_relu_a_l_identique(tmp_path):
    """Aller-retour complet : accents, imbrication et nombres traversent la file intacts."""
    file = FileSQLite(tmp_path / "file.db")
    valeur = {"texte": "Générale d'été", "liste": [1, 2.5, None], "objet": {"a": True}}
    file.publier("s", valeur)
    assert file.lire("s", "g", attente_s=0)[0].valeur == valeur
    file.fermer()


# --------------------------------------------------------------------------------------------
# Précisions : ce que les tests ci-dessus laissaient passer (relevé par la mutation)
# --------------------------------------------------------------------------------------------
class ConnexionEspionne(sqlite3.Connection):
    """Une connexion SQLite qui note le texte de chaque instruction passée à ``execute``."""

    instructions: list[str] = []

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        ConnexionEspionne.instructions.append(sql)
        return super().execute(sql, *args, **kwargs)


@pytest.fixture
def instructions_sqlite(monkeypatch):
    """Les instructions envoyées par ``FileSQLite`` (le texte exact, pas leur effet)."""
    vues: list[str] = []
    ConnexionEspionne.instructions = vues
    connexion_reelle = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: connexion_reelle(*a, factory=ConnexionEspionne, **k)
    )
    return vues


POSITION_SQL = "SELECT dernier_id FROM positions WHERE sujet = ? AND groupe = ?"


def test_publier_envoie_l_insertion_documentee(tmp_path, instructions_sqlite):
    """Une seule insertion, colonnes dans l'ordre sujet, clé, valeur, heure de publication."""
    file = FileSQLite(tmp_path / "f.db")
    instructions_sqlite.clear()
    file.publier("s", {"a": 1}, cle="k")
    assert instructions_sqlite == [
        "INSERT INTO messages (sujet, cle, valeur, publie_le) VALUES (?, ?, ?, ?)"
    ]
    file.fermer()


def test_lire_consulte_la_position_puis_envoie_la_selection_documentee(
    tmp_path, instructions_sqlite
):
    """La position du groupe d'abord, puis les messages suivants triés par identifiant."""
    file = FileSQLite(tmp_path / "f.db")
    instructions_sqlite.clear()
    assert file.lire("s", "g", attente_s=0.0) == []
    assert instructions_sqlite == [
        POSITION_SQL,
        "SELECT id, cle, valeur, publie_le FROM messages "
        "WHERE sujet = ? AND id > ? ORDER BY id LIMIT ?",
    ]
    file.fermer()


def test_valider_envoie_la_mise_a_jour_qui_ne_recule_jamais(tmp_path, instructions_sqlite):
    """Insertion ou mise à jour de la position, avec le plus grand des deux identifiants."""
    file = FileSQLite(tmp_path / "f.db")
    instructions_sqlite.clear()
    file.valider("s", "g", [message_sqlite("3", {})])
    assert instructions_sqlite == [
        "INSERT INTO positions (sujet, groupe, dernier_id) VALUES (?, ?, ?) "
        "ON CONFLICT (sujet, groupe) DO UPDATE SET "
        "dernier_id = MAX(dernier_id, excluded.dernier_id)"
    ]
    file.fermer()


def test_le_retard_consulte_la_position_puis_compte_les_messages_suivants(
    tmp_path, instructions_sqlite
):
    """Retard = messages du sujet au-delà de la position du groupe (et de ce groupe seulement)."""
    file = FileSQLite(tmp_path / "f.db")
    instructions_sqlite.clear()
    assert file.retard("s", "g") == 0
    assert instructions_sqlite == [
        POSITION_SQL,
        "SELECT COUNT(*) FROM messages WHERE sujet = ? AND id > ?",
    ]
    file.fermer()


def test_le_retard_se_calcule_avec_le_sujet_et_le_groupe_demandes(tmp_path):
    """Un groupe qui a tout validé n'a plus de retard ; un autre groupe garde le sien entier."""
    file = FileSQLite(tmp_path / "f.db")
    for numero in range(3):
        file.publier("s", {"n": numero})
    lus = file.lire("s", "a", attente_s=0.0)
    file.valider("s", "a", lus)
    assert (file.retard("s", "a"), file.retard("s", "b"), file.retard("autre", "a")) == (0, 3, 0)
    file.fermer()


def test_sans_fabrique_la_file_kafka_construit_ses_clients_natifs_avec_leur_configuration(
    monkeypatch,
):
    """Le producteur et le consommateur du client réel reçoivent la configuration complète."""
    configurations: dict[str, list[Any]] = {"producteur": [], "consommateur": []}

    class Producer:
        def __init__(self, configuration: dict[str, Any]) -> None:
            configurations["producteur"].append(configuration)

        def produce(self, *args: Any, **kwargs: Any) -> None:
            pass

        def poll(self, delai: float) -> None:
            pass

        def flush(self, delai: float) -> int:
            return 0

    class Consumer:
        def __init__(self, configuration: dict[str, Any]) -> None:
            configurations["consommateur"].append(configuration)

        def subscribe(self, sujets: list[str]) -> None:
            pass

        def consume(self, num_messages: int, timeout: float) -> list[Any]:
            return []

        def close(self) -> None:
            pass

    module = types.ModuleType("confluent_kafka")
    module.Producer = Producer
    module.Consumer = Consumer
    monkeypatch.setitem(sys.modules, "confluent_kafka", module)
    file = FileKafka("k:9092")
    file.publier("s", {"a": 1}, cle="c")
    assert file.lire("s", "g", 5, 0.0) == []
    assert configurations["producteur"] == [
        {
            "bootstrap.servers": "k:9092",
            "broker.address.family": "v4",
            "acks": "all",
            "enable.idempotence": True,
            "linger.ms": 20,
        }
    ]
    assert configurations["consommateur"] == [
        {
            "bootstrap.servers": "k:9092",
            "broker.address.family": "v4",
            "group.id": "g",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    ]
    file.fermer()


def test_un_decalage_negatif_n_enfonce_jamais_la_position_sous_zero(faux_kafka):
    """Aucun message lu = prochain décalage 0 : un décalage invalide (-5) ne fait pas descendre."""
    fabriques = FabriquesEspionnes()
    file = fabriques.file()
    invalide = Message("valides", None, {}, "0:-5", HEURE)
    file.valider("valides", "g", [invalide])
    assert fabriques.consommateurs[0].valides == [([("valides", 0, 0)], False)]
