# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""File de messages : SQLite (local) et Kafka (production, client simulé) (critères 1.2 et 4.2)."""

from __future__ import annotations

import json
import sys
import types

import pytest

import detection_fraude.file_attente as file_module
from detection_fraude.file_attente import (
    ErreurLivraison,
    FileKafka,
    FileSQLite,
    Message,
    options_kafka_depuis_environnement,
)


@pytest.fixture(autouse=True)
def horloge_bornee(monkeypatch):
    """Horloge simulée : une pause fait avancer le temps sans attendre, et plus de cent pauses
    arrêtent le test. Une lecture qui ne se terminerait jamais échoue donc tout de suite."""
    etat = {"maintenant": 0.0, "pauses": 0}

    def dormir(secondes: float) -> None:
        etat["pauses"] += 1
        etat["maintenant"] += secondes
        assert etat["pauses"] < 100, "la lecture n'en finit pas : trop de pauses"

    horloge = types.SimpleNamespace(
        monotonic=lambda: etat["maintenant"], time=lambda: 1_700_000_000.0, sleep=dormir
    )
    monkeypatch.setattr(file_module, "time", horloge)


# ----- SQLite -----
def test_publication_puis_lecture_dans_l_ordre(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    for i in range(5):
        file.publier("sujet", {"i": i}, cle=f"k{i}")
    messages = file.lire("sujet", "g1", max_messages=10, attente_s=0)
    assert [m.valeur["i"] for m in messages] == [0, 1, 2, 3, 4]
    assert messages[0].cle == "k0" and messages[0].publie_le > 0
    file.fermer()


def test_sans_validation_les_messages_sont_relivres(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    file.publier("s", {"a": 1})
    premiere = file.lire("s", "g", attente_s=0)
    assert len(premiere) == 1
    assert len(file.lire("s", "g", attente_s=0)) == 1  # pas de commit : relivraison
    file.valider("s", "g", premiere)
    assert file.lire("s", "g", attente_s=0) == []


def test_les_groupes_sont_independants(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    file.publier("s", {"a": 1})
    lu = file.lire("s", "groupe-a", attente_s=0)
    file.valider("s", "groupe-a", lu)
    assert file.lire("s", "groupe-b", attente_s=0)[0].valeur == {"a": 1}


def test_les_sujets_sont_independants(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    file.publier("valides", {"v": 1})
    file.publier("rejets", {"r": 1})
    assert file.lire("rejets", "g", attente_s=0)[0].valeur == {"r": 1}


def test_validation_partielle_et_retard(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    for i in range(6):
        file.publier("s", {"i": i})
    lot = file.lire("s", "g", max_messages=4, attente_s=0)
    file.valider("s", "g", lot)
    assert file.retard("s", "g") == 2
    assert [m.valeur["i"] for m in file.lire("s", "g", attente_s=0)] == [4, 5]


def test_la_position_ne_recule_jamais(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    for i in range(3):
        file.publier("s", {"i": i})
    tous = file.lire("s", "g", attente_s=0)
    file.valider("s", "g", tous)
    file.valider("s", "g", tous[:1])  # validation tardive d'un ancien message
    assert file.retard("s", "g") == 0


def test_deux_connexions_partagent_la_meme_file(tmp_path):
    ecrivain, lecteur = FileSQLite(tmp_path / "f.db"), FileSQLite(tmp_path / "f.db")
    ecrivain.publier("s", {"x": 1})
    assert lecteur.lire("s", "g", attente_s=0)[0].valeur == {"x": 1}


def test_lecture_bloquante_courte_quand_vide(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    assert file.lire("s", "g", attente_s=0.2) == []


def test_les_accents_traversent_la_file(tmp_path):
    file = FileSQLite(tmp_path / "f.db")
    file.publier("s", {"texte": "Générale d'été"})
    assert file.lire("s", "g", attente_s=0)[0].valeur["texte"] == "Générale d'été"


# ----- Kafka (client simulé : aucune dépendance au courtier) -----
class FauxProducteur:
    def __init__(self, configuration, echec=None):
        self.configuration = configuration
        self.envoyes = []
        self.echec = echec
        self.rappel = None

    def produce(self, sujet, key=None, value=None, on_delivery=None):
        self.envoyes.append((sujet, key, value))
        self.rappel = on_delivery
        if on_delivery:
            on_delivery(self.echec, None)

    def poll(self, delai):
        return 0

    def flush(self, delai=None):
        return 0


class FauxMessageKafka:
    def __init__(self, partition, decalage, valeur, cle=None, erreur=None):
        self._p, self._o, self._v, self._k, self._e = partition, decalage, valeur, cle, erreur

    def error(self):
        return self._e

    def timestamp(self):
        return (1, 1_700_000_000_000)

    def key(self):
        return self._k

    def value(self):
        return self._v

    def partition(self):
        return self._p

    def offset(self):
        return self._o


class FauxConsommateur:
    def __init__(self, configuration, messages):
        self.configuration = configuration
        self.messages = messages
        self.souscrit = None
        self.valides = []

    def subscribe(self, sujets):
        self.souscrit = sujets

    def consume(self, num_messages, timeout):
        lot, self.messages = self.messages[:num_messages], self.messages[num_messages:]
        return lot

    def commit(self, offsets, asynchronous):
        self.valides.append((offsets, asynchronous))

    def close(self):
        pass


@pytest.fixture
def faux_module_kafka(monkeypatch):
    module = types.ModuleType("confluent_kafka")
    module.TopicPartition = lambda sujet, partition, decalage: (sujet, partition, decalage)
    monkeypatch.setitem(sys.modules, "confluent_kafka", module)


def test_kafka_publie_en_json_avec_cle_et_options_de_fiabilite():
    producteurs = []

    def fabrique(configuration):
        producteurs.append(FauxProducteur(configuration))
        return producteurs[0]

    file = FileKafka("h:9092", fabrique_producteur=fabrique)
    file.publier("valides", {"texte": "été", "n": 1}, cle="carte1")
    file.vider()
    sujet, cle, valeur = producteurs[0].envoyes[0]
    assert (sujet, cle) == ("valides", b"carte1")
    assert json.loads(valeur.decode("utf-8")) == {"texte": "été", "n": 1}
    assert producteurs[0].configuration["acks"] == "all"
    assert producteurs[0].configuration["enable.idempotence"] is True
    assert producteurs[0].configuration["bootstrap.servers"] == "h:9092"


def test_kafka_signale_un_message_non_livre():
    file = FileKafka(
        "h:9092", fabrique_producteur=lambda c: FauxProducteur(c, echec="courtier KO")
    )
    file.publier("s", {"a": 1})
    with pytest.raises(ErreurLivraison, match="courtier KO"):
        file.vider()


def test_kafka_lecture_et_validation_par_partition(faux_module_kafka):
    messages = [
        FauxMessageKafka(0, 10, b'{"i": 1}', b"k"),
        FauxMessageKafka(1, 4, b'{"i": 2}'),
        FauxMessageKafka(0, 11, b'{"i": 3}'),
        FauxMessageKafka(0, 12, b"\xff\xfe pas du json"),
        FauxMessageKafka(0, 13, b"{}", erreur="erreur de partition"),
    ]
    consommateurs = []

    def fabrique(configuration):
        consommateurs.append(FauxConsommateur(configuration, messages))
        return consommateurs[0]

    file = FileKafka("h:9092", fabrique_consommateur=fabrique)
    lus = file.lire("valides", "groupe", max_messages=10, attente_s=0)
    assert [m.position for m in lus] == ["0:10", "1:4", "0:11", "0:12"]  # l'erreur est écartée
    assert lus[0].cle == "k" and lus[0].valeur == {"i": 1}
    assert lus[3].valeur == {"__illisible__": True}  # charge illisible : orientée vers la DLQ
    assert consommateurs[0].souscrit == ["valides"]
    assert consommateurs[0].configuration["enable.auto.commit"] is False
    file.valider("valides", "groupe", lus)
    offsets, asynchrone = consommateurs[0].valides[0]
    assert asynchrone is False
    assert sorted(offsets) == [("valides", 0, 13), ("valides", 1, 5)]  # dernier lu + 1


def test_kafka_valider_rien_ne_fait_rien(faux_module_kafka):
    FileKafka("h:9092").valider("s", "g", [])


def test_options_de_securite_kafka_lues_dans_l_environnement(monkeypatch):
    monkeypatch.setenv("FRAUDE_KAFKA_PROTOCOLE_SECURITE", "SASL_SSL")
    monkeypatch.setenv("FRAUDE_KAFKA_UTILISATEUR", "utilisateur-test")
    monkeypatch.setenv("FRAUDE_KAFKA_MOT_DE_PASSE", "mot-de-passe-test")
    monkeypatch.setenv("FRAUDE_KAFKA_CA", "/chemin/ca.pem")
    options = options_kafka_depuis_environnement()
    assert options["security.protocol"] == "SASL_SSL"
    assert options["sasl.username"] == "utilisateur-test"
    assert options["sasl.password"] == "mot-de-passe-test"
    assert options["ssl.ca.location"] == "/chemin/ca.pem"


def test_message_est_immuable():
    message = Message("s", None, {}, "1", 0.0)
    with pytest.raises(AttributeError):
        message.sujet = "autre"  # type: ignore[misc]
