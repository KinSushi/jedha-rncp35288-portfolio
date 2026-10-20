# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Base documents : index, mises à jour atomiques et requêtes d'agrégation.

Les requêtes sont testées sur de PETITS jeux écrits à la main dont on connaît la réponse : un test
sur des données aléatoires ne prouverait pas que la requête répond à la question posée.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pymongo.errors import DuplicateKeyError

from stripe_demo import nosql


@pytest.fixture
def db():
    return nosql.base()


def _evenement(type_evenement, appareil="mobile", duree=60, client=1, erreur=None):
    doc = {"id_client": client, "type": type_evenement,
           "horodatage": datetime(2026, 9, 1, tzinfo=UTC),
           "session": {"appareil": appareil, "duree_s": duree}}
    if erreur:
        doc["erreur"] = {"code": erreur, "tentative": 1}
    return doc


def exiger_index_de_la_demonstration(db) -> None:
    index = db.evenements.index_information()
    cles = {tuple(v["key"]) for v in index.values()}
    assert (("id_client", 1), ("horodatage", -1)) in cles
    ttl = [v for v in index.values() if v.get("expireAfterSeconds") is not None]
    assert len(ttl) == 1
    assert ttl[0]["expireAfterSeconds"] == 90 * 24 * 3600  # minimisation RGPD : 90 jours
    assert any(v.get("unique") for v in db.caracteristiques_clients.index_information().values())


def exiger_sous_document_erreur_reserve_aux_erreurs(db) -> None:
    avec_erreur = db.evenements.count_documents({"erreur": {"$exists": True}})
    assert avec_erreur == db.evenements.count_documents({"type": "erreur_paiement"})


def test_index_de_la_demonstration(db):
    exiger_index_de_la_demonstration(db)

    # Sans l'index à durée de vie, les événements ne seraient jamais effacés (RGPD) : refusé.
    nom_ttl = next(nom for nom, v in db.evenements.index_information().items()
                   if v.get("expireAfterSeconds") is not None)
    db.evenements.drop_index(nom_ttl)
    with pytest.raises(AssertionError):
        exiger_index_de_la_demonstration(db)
    # Sans l'index unique, deux fiches pour le même client seraient possibles : refusé aussi.
    db_complete = nosql.base()
    nom_unique = next(nom for nom, v in db_complete.caracteristiques_clients
                      .index_information().items() if v.get("unique"))
    db_complete.caracteristiques_clients.drop_index(nom_unique)
    with pytest.raises(AssertionError):
        exiger_index_de_la_demonstration(db_complete)


def test_une_caracteristique_par_client(db):
    db.caracteristiques_clients.insert_one({"id_client": 5, "nb_paiements": 1})
    with pytest.raises(DuplicateKeyError):
        db.caracteristiques_clients.insert_one({"id_client": 5, "nb_paiements": 2})


def test_mise_a_jour_des_caracteristiques_cumule_sans_perte(db):
    def paiement(montant, pays):
        return {"id_client": 7, "montant_usd": montant, "pays_ip": pays}

    nosql.mettre_a_jour_caracteristiques(db, paiement(10.0, "FR"))
    nosql.mettre_a_jour_caracteristiques(db, paiement(30.0, "FR"))
    doc = nosql.mettre_a_jour_caracteristiques(db, paiement(20.0, "US"))
    assert doc["nb_paiements"] == 3
    assert doc["montant_total_usd"] == pytest.approx(60.0)
    assert doc["montant_max_usd"] == pytest.approx(30.0)
    assert sorted(doc["pays_ip_vus"]) == ["FR", "US"]  # $addToSet : pas de doublon
    assert db.caracteristiques_clients.count_documents({"id_client": 7}) == 1

    # Un paiement sans montant est refusé avant toute écriture : la fiche reste celle d'avant.
    avant = db.caracteristiques_clients.find_one({"id_client": 7})
    with pytest.raises(KeyError, match="montant_usd"):
        nosql.mettre_a_jour_caracteristiques(db, {"id_client": 7, "pays_ip": "FR"})
    assert db.caracteristiques_clients.find_one({"id_client": 7}) == avant


def test_requete_erreurs_les_plus_frequentes(db):
    """La requête répond à sa question et laisse les données inchangées (lecture seule)."""
    db.evenements.insert_many([
        _evenement("erreur_paiement", erreur="card_declined"),
        _evenement("erreur_paiement", erreur="card_declined"),
        _evenement("erreur_paiement", erreur="card_declined"),
        _evenement("erreur_paiement", erreur="expired_card"),
        _evenement("erreur_paiement", erreur="expired_card"),
        _evenement("erreur_paiement", erreur="insufficient_funds"),
        _evenement("connexion"),  # ignoré : ce n'est pas une erreur
    ])
    avant = list(db.evenements.find())
    pipeline = nosql.REQUETES["erreurs de paiement les plus fréquentes (30 jours)"]
    resultat = list(db.evenements.aggregate(pipeline))
    assert [(r["_id"], r["nombre"]) for r in resultat] == [
        ("card_declined", 3), ("expired_card", 2), ("insufficient_funds", 1)]
    assert list(db.evenements.find()) == avant
    assert not any(set(etape) & {"$out", "$merge"} for etape in pipeline)  # aucune écriture


def test_requete_abandons_mobiles_les_plus_longs(db):
    """La requête répond à sa question et laisse les données inchangées (lecture seule)."""
    db.evenements.insert_many([
        _evenement("abandon", "mobile", 100, client=1),
        _evenement("abandon", "mobile", 500, client=2),
        _evenement("abandon", "mobile", 300, client=3),
        _evenement("abandon", "mobile", 50, client=4),
        _evenement("abandon", "ordinateur", 9000, client=5),  # exclu : pas un mobile
        _evenement("connexion", "mobile", 9999, client=6),  # exclu : pas un abandon
    ])
    avant = list(db.evenements.find())
    pipeline = nosql.REQUETES["sessions mobiles abandonnées les plus longues"]
    assert list(db.evenements.aggregate(pipeline)) == [
        {"id_client": 2, "duree_s": 500},
        {"id_client": 3, "duree_s": 300},
        {"id_client": 1, "duree_s": 100},
    ]
    assert list(db.evenements.find()) == avant
    assert not any(set(etape) & {"$out", "$merge"} for etape in pipeline)  # aucune écriture


def test_requete_themes_des_avis_negatifs(db):
    """La requête répond à sa question et laisse les données inchangées (lecture seule)."""
    def avis(note, sentiment, themes):
        return {"id_marchand": 1, "note": note, "texte": "t",
                "date": datetime(2026, 9, 1, tzinfo=UTC),
                "analyse": {"sentiment": sentiment, "themes": themes}}

    db.retours_clients.insert_many([
        avis(1, "negatif", ["frais", "support"]),
        avis(2, "negatif", ["frais", "litige"]),
        avis(5, "positif", ["frais", "rapidite"]),  # exclu : avis positif
    ])
    avant = list(db.retours_clients.find())
    pipeline = nosql.REQUETES_RETOURS["thèmes des avis négatifs"]
    resultat = {r["_id"]: r["nombre"] for r in db.retours_clients.aggregate(pipeline)}
    assert resultat == {"frais": 2, "support": 1, "litige": 1}
    assert list(db.retours_clients.find()) == avant
    assert not any(set(etape) & {"$out", "$merge"} for etape in pipeline)  # aucune écriture


def test_les_generateurs_remplissent_les_collections(db):
    nosql.evenements_synthetiques(db, 300)
    nosql.retours_synthetiques(db, 50)
    assert db.evenements.count_documents({}) == 300
    assert db.retours_clients.count_documents({}) == 50
    # Schéma souple : seuls les événements d'erreur portent le sous-document « erreur ».
    exiger_sous_document_erreur_reserve_aux_erreurs(db)

    # Une connexion qui porterait un sous-document d'erreur est refusée par la même règle.
    db.evenements.insert_one(_evenement("connexion", erreur="card_declined"))
    with pytest.raises(AssertionError):
        exiger_sous_document_erreur_reserve_aux_erreurs(db)
