# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Base documents : index déclarés, générateurs reproductibles et bornes de leurs tirages.

Complète `test_nosql.py` (qui vérifie les requêtes sur des petits jeux écrits à la main) : ici,
on vérifie ce que la démonstration annonce de ses données synthétiques. Deux preuves
complémentaires : l'EMPREINTE d'un lot (la graine fixe redonne exactement les mêmes documents,
donc les chiffres des notes restent vrais) et les BORNES (un faux générateur aléatoire rend
toujours la valeur extrême et enregistre les intervalles demandés).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from aide_aleatoire import AleaEspion
from pymongo.errors import DuplicateKeyError, InvalidOperation

from stripe_demo import nosql

TYPES = ("page_paiement", "erreur_paiement", "connexion", "abandon")
CODES = ("card_declined", "insufficient_funds", "expired_card")
THEMES = ("frais", "rapidite", "support", "litige", "integration")

EMPREINTE_300_EVENEMENTS = "3076085076c7ecd52a85b377e524069cbcb141bcb227256870ec7f7525225730"
EMPREINTE_50_RETOURS = "3f2c42dd521c934058ab8b7055580a0bc2e50a8b4b3f389723c795198fee8c71"


@pytest.fixture
def db():
    return nosql.base()


def _empreinte(documents) -> str:
    """Empreinte SHA-256 des documents (sans _id, que la base tire au hasard)."""
    propres = [{cle: valeur for cle, valeur in doc.items() if cle != "_id"} for doc in documents]
    texte = json.dumps(propres, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def _index(collection) -> dict:
    """Index d'une collection par clé : {((champ, sens), ...): options}, sans l'index _id."""
    return {tuple(v["key"]): v for nom, v in collection.index_information().items()
            if nom != "_id_"}


def test_les_index_de_la_demonstration_sont_ceux_de_la_note_04_et_l_unicite_est_appliquee(db):
    assert db.name == "stripe"

    index = _index(db.evenements)
    assert set(index) == {(("id_client", 1), ("horodatage", -1)), (("horodatage", 1),)}
    ttl = index[(("horodatage", 1),)]
    assert ttl["expireAfterSeconds"] == 7_776_000  # 90 jours
    assert not ttl.get("unique")
    assert "expireAfterSeconds" not in index[(("id_client", 1), ("horodatage", -1))]

    index = _index(db.caracteristiques_clients)
    assert set(index) == {(("id_client", 1),)}
    assert index[(("id_client", 1),)]["unique"] is True

    index = _index(db.signaux_fraude)
    assert set(index) == {(("score", -1),)}  # du score le plus haut au plus bas
    assert not index[(("score", -1),)].get("unique")
    assert _index(db.retours_clients) == {}

    # L'index unique est appliqué : un second document du même client est refusé.
    db.caracteristiques_clients.insert_one({"id_client": 1})
    with pytest.raises(DuplicateKeyError):
        db.caracteristiques_clients.insert_one({"id_client": 1})
    assert db.caracteristiques_clients.count_documents({}) == 1


def test_les_evenements_sont_reproductibles_graine_par_graine(db, monkeypatch):
    """300 événements de la graine 7 : la démonstration et ses chiffres en dépendent."""
    nosql.evenements_synthetiques(db, 300)
    assert db.evenements.count_documents({}) == 300
    assert _empreinte(db.evenements.find()) == EMPREINTE_300_EVENEMENTS

    autre = nosql.base()
    nosql.evenements_synthetiques(autre, 300, graine=8)
    assert _empreinte(autre.evenements.find()) != EMPREINTE_300_EVENEMENTS

    nosql.evenements_synthetiques(db, 5, graine=9)  # ajoute, ne remplace pas
    assert db.evenements.count_documents({}) == 305

    # La graine par défaut des événements est 7, celle des retours 11 ; la graine demandée gagne.
    espion = AleaEspion()
    monkeypatch.setattr(nosql.random, "Random", espion)
    nosql.evenements_synthetiques(nosql.base(), 1)
    nosql.retours_synthetiques(nosql.base(), 1)
    nosql.evenements_synthetiques(nosql.base(), 1, graine=123)
    nosql.retours_synthetiques(nosql.base(), 1, graine=456)
    assert espion.graines == [7, 11, 123, 456]


def test_les_evenements_aux_bornes_et_pour_chaque_type_et_chaque_code(monkeypatch):
    """Tout au maximum : dernier client, dernière minute du mois, plus longue session ; tout au
    minimum ; chaque type d'événement ; chaque code d'erreur."""
    haut = AleaEspion(randint="max", choix=1)
    monkeypatch.setattr(nosql.random, "Random", haut)
    base_haute = nosql.base()
    nosql.evenements_synthetiques(base_haute, 1)
    assert base_haute.evenements.find_one({}, {"_id": 0}) == {
        "id_client": 2000, "type": "erreur_paiement",
        "horodatage": datetime(2026, 9, 1) + timedelta(minutes=43_200),
        "session": {"appareil": "ordinateur", "duree_s": 900},
        "erreur": {"code": "insufficient_funds", "tentative": 3}}
    assert haut.appels == [
        ("choice", (TYPES,), ()),
        ("randint", (1, 2000), ()), ("randint", (0, 43_200), ()),
        ("choice", (("mobile", "ordinateur"),), ()), ("randint", (5, 900), ()),
        ("choice", (CODES,), ()),
        ("randint", (1, 3), ())]

    bas = AleaEspion(randint="min", choix=0)
    monkeypatch.setattr(nosql.random, "Random", bas)
    base_basse = nosql.base()
    nosql.evenements_synthetiques(base_basse, 1)
    assert base_basse.evenements.find_one({}, {"_id": 0}) == {
        "id_client": 1, "type": "page_paiement", "horodatage": datetime(2026, 9, 1),
        "session": {"appareil": "mobile", "duree_s": 5}}
    assert bas.appels == [
        ("choice", (TYPES,), ()),
        ("randint", (1, 2000), ()), ("randint", (0, 43_200), ()),
        ("choice", (("mobile", "ordinateur"),), ()), ("randint", (5, 900), ())]

    # Chaque type est tiré ; seule l'erreur de paiement porte un sous-document « erreur ».
    for position, type_attendu in enumerate(TYPES):
        monkeypatch.setattr(nosql.random, "Random", AleaEspion(choix=position))
        courante = nosql.base()
        nosql.evenements_synthetiques(courante, 1)
        document = courante.evenements.find_one({}, {"_id": 0})
        assert document["type"] == type_attendu
        assert ("erreur" in document) == (type_attendu == "erreur_paiement")

    # Chaque code d'erreur est tiré (le type est forcé sur « erreur_paiement »).
    for position, code_attendu in enumerate(CODES):
        espion = AleaEspion(choix=position)
        choix_normal = espion.choice
        espion.choice = lambda sequence, normal=choix_normal: (
            "erreur_paiement" if tuple(sequence) == TYPES else normal(sequence))
        monkeypatch.setattr(nosql.random, "Random", espion)
        courante = nosql.base()
        nosql.evenements_synthetiques(courante, 1)
        assert courante.evenements.find_one({})["erreur"]["code"] == code_attendu


def test_zero_evenement_est_refuse_par_la_base_qui_exige_une_liste_non_vide(db):
    with pytest.raises((TypeError, InvalidOperation)):
        nosql.evenements_synthetiques(db, 0)
    with pytest.raises((TypeError, InvalidOperation)):
        nosql.retours_synthetiques(db, 0)

    assert db.evenements.count_documents({}) == 0
    assert db.retours_clients.count_documents({}) == 0


def test_les_retours_sont_reproductibles_et_leur_sentiment_suit_la_note(db, monkeypatch):
    nosql.retours_synthetiques(db, 50)
    assert db.retours_clients.count_documents({}) == 50
    assert _empreinte(db.retours_clients.find()) == EMPREINTE_50_RETOURS
    for document in db.retours_clients.find():
        themes = document["analyse"]["themes"]
        assert themes[0] != themes[1]  # deux thèmes distincts
        assert set(themes) <= set(THEMES)

    # Notes 1 à 5 : négatif, négatif, neutre, positif, positif ; deux thèmes parmi les cinq.
    espion = AleaEspion(randint="max", choix=-1, reponses={(1, 5): [1, 2, 3, 4, 5]})
    monkeypatch.setattr(nosql.random, "Random", espion)
    courante = nosql.base()
    nosql.retours_synthetiques(courante, 5)
    documents = list(courante.retours_clients.find({}, {"_id": 0}))
    assert [(d["note"], d["analyse"]["sentiment"]) for d in documents] == [
        (1, "negatif"), (2, "negatif"), (3, "neutre"), (4, "positif"), (5, "positif")]
    for document in documents:
        assert document == {
            "id_marchand": 40, "note": document["note"], "texte": "avis de démonstration",
            "date": datetime(2026, 9, 28),
            "analyse": {"sentiment": document["analyse"]["sentiment"],
                        "themes": ["litige", "integration"]}}
    # Quatre tirages par retour, dans cet ordre : marchand, note, jour, thèmes.
    assert espion.appels[:4] == [
        ("randint", (1, 40), ()), ("randint", (1, 5), ()), ("randint", (1, 28), ()),
        ("sample", (THEMES,), (("k", 2),))]
    assert len(espion.appels) == 4 * 5

    # Aux bornes basses.
    monkeypatch.setattr(nosql.random, "Random", AleaEspion(randint="min", choix=0))
    basse = nosql.base()
    nosql.retours_synthetiques(basse, 1)
    assert basse.retours_clients.find_one({}, {"_id": 0}) == {
        "id_marchand": 1, "note": 1, "texte": "avis de démonstration",
        "date": datetime(2026, 9, 1),
        "analyse": {"sentiment": "negatif", "themes": ["frais", "rapidite"]}}


def _paiement_de(client=7, montant=10.0, pays="FR"):
    return {"id_client": client, "montant_usd": montant, "pays_ip": pays}


def test_les_caracteristiques_cumulent_par_client_et_refusent_un_paiement_incomplet(db):
    avant = datetime.now(UTC).replace(tzinfo=None)
    premier = nosql.mettre_a_jour_caracteristiques(db, _paiement_de(client=3, montant=12.5))
    apres = datetime.now(UTC).replace(tzinfo=None)

    # Le premier paiement crée le document du client, daté d'à l'instant.
    assert premier["id_client"] == 3
    assert premier["nb_paiements"] == 1
    assert premier["montant_total_usd"] == 12.5
    assert premier["montant_max_usd"] == 12.5
    assert premier["pays_ip_vus"] == ["FR"]
    assert avant - timedelta(seconds=1) <= premier["mis_a_jour_le"] <= (
        apres + timedelta(seconds=1))

    # Le montant maximal ne baisse jamais ; le total et le nombre augmentent.
    nosql.mettre_a_jour_caracteristiques(db, _paiement_de(client=7, montant=50.0))
    second = nosql.mettre_a_jour_caracteristiques(db, _paiement_de(client=7, montant=20.0))
    assert (second["montant_max_usd"], second["montant_total_usd"], second["nb_paiements"]) == (
        50.0, 70.0, 2)

    # Deux clients, deux documents indépendants.
    nosql.mettre_a_jour_caracteristiques(db, _paiement_de(client=2, montant=9.0, pays="US"))
    deux = db.caracteristiques_clients.find_one({"id_client": 2})
    assert (deux["montant_total_usd"], deux["pays_ip_vus"]) == (9.0, ["US"])
    assert db.caracteristiques_clients.count_documents({}) == 3

    # Un paiement sans montant ou sans client est refusé et ne crée aucun document.
    with pytest.raises(KeyError):
        nosql.mettre_a_jour_caracteristiques(db, {"id_client": 4, "pays_ip": "FR"})
    with pytest.raises(KeyError):
        nosql.mettre_a_jour_caracteristiques(db, {"montant_usd": 1.0, "pays_ip": "FR"})
    assert db.caracteristiques_clients.count_documents({"id_client": 4}) == 0
    assert db.caracteristiques_clients.count_documents({}) == 3


# ---------------------------------------------------------------- dates en UTC, vues de l'appel


class CollectionEnregistreuse:
    """Fausse collection : garde les documents reçus et les arguments de chaque mise à jour."""

    def __init__(self):
        self.documents = []
        self.mises_a_jour = []

    def insert_many(self, documents):
        self.documents.extend(documents)

    def find_one_and_update(self, filtre, modification, **options):
        self.mises_a_jour.append((filtre, modification, options))
        return {"id_client": filtre["id_client"]}


class BaseEnregistreuse:
    def __init__(self):
        self.evenements = CollectionEnregistreuse()
        self.retours_clients = CollectionEnregistreuse()
        self.caracteristiques_clients = CollectionEnregistreuse()


def test_les_dates_fabriquees_et_celle_de_la_mise_a_jour_sont_en_utc(monkeypatch):
    """MongoDB range les dates en UTC ; une date sans fuseau serait lue dans celui de la machine.

    On regarde ce que le code envoie à la base (une fausse base enregistre les documents), pas ce
    que la base en fait : mongomock ramène toutes les dates à l'UTC sans fuseau."""
    base = BaseEnregistreuse()
    nosql.evenements_synthetiques(base, 20)
    nosql.retours_synthetiques(base, 20)

    assert all(doc["horodatage"].tzinfo is UTC for doc in base.evenements.documents)
    assert all(doc["date"].tzinfo is UTC for doc in base.retours_clients.documents)
    assert {doc["horodatage"].year for doc in base.evenements.documents} == {2026}

    instant = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

    class FauxDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is UTC else instant + timedelta(hours=5)

    monkeypatch.setattr(nosql, "datetime", FauxDatetime)
    nosql.mettre_a_jour_caracteristiques(base, _paiement_de(client=7, montant=12.5, pays="FR"))

    ((filtre, modification, options),) = base.caracteristiques_clients.mises_a_jour
    assert filtre == {"id_client": 7}
    assert modification == {
        "$inc": {"nb_paiements": 1, "montant_total_usd": 12.5},
        "$addToSet": {"pays_ip_vus": "FR"},
        "$max": {"montant_max_usd": 12.5},
        "$set": {"mis_a_jour_le": instant}}  # l'instant UTC, jamais l'heure locale (+ 5 h ici)
    assert options == {"upsert": True, "return_document": True}

    # Une base qui ne rend aucun document après la mise à jour est une anomalie, pas un résultat.
    base.caracteristiques_clients.find_one_and_update = lambda *args, **options: None
    with pytest.raises(AssertionError):
        nosql.mettre_a_jour_caracteristiques(base, _paiement_de())
