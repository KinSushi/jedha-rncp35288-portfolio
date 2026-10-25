# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Validateurs JSON Schema des quatre collections MongoDB.

Deux familles de tests : les documents que produit la démonstration sont ACCEPTÉS (le schéma ne
bloque pas le trafic légitime) et des documents volontairement faux sont REFUSÉS (le schéma
protège réellement la collection).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from stripe_demo import nosql
from stripe_demo import validation_mongo as vm

# Mots-clés que `$jsonSchema` de MongoDB ne prend PAS en charge : un schéma qui les contiendrait
# serait refusé par le serveur au moment de créer la collection.
MOTS_CLES_NON_SUPPORTES = {"$ref", "$schema", "default", "definitions", "format", "id",
                           "if", "then", "else", "const", "contains", "propertyNames"}


TYPES_BSON_CONNUS = {"object", "array", "string", "bool", "date", "objectId", "int", "long",
                     "double", "number", "null"}


def _types_bson(noeud) -> set[str]:
    """Tous les `bsonType` déclarés dans un schéma (valeur simple ou liste)."""
    trouves: set[str] = set()
    if isinstance(noeud, dict):
        for cle, valeur in noeud.items():
            if cle == "bsonType":
                trouves |= set(valeur if isinstance(valeur, list) else [valeur])
            else:
                trouves |= _types_bson(valeur)
    elif isinstance(noeud, list):
        for element in noeud:
            trouves |= _types_bson(element)
    return trouves


def _tous_les_mots_cles(noeud):
    if isinstance(noeud, dict):
        for cle, valeur in noeud.items():
            yield cle
            if cle not in ("properties",):  # les noms de champs ne sont pas des mots-clés
                yield from _tous_les_mots_cles(valeur)
            else:
                for sous in valeur.values():
                    yield from _tous_les_mots_cles(sous)
    elif isinstance(noeud, list):
        for element in noeud:
            yield from _tous_les_mots_cles(element)


def exiger_sous_ensemble_mongodb(brut, collection: str) -> None:
    assert "Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC" == brut["_copyright"]
    assert brut["bsonType"] == "object"
    assert brut["title"] == collection
    assert not (set(_tous_les_mots_cles(brut)) & MOTS_CLES_NON_SUPPORTES), "mot-clé non supporté"
    assert _types_bson(brut) <= TYPES_BSON_CONNUS, "type BSON inventé"  # ou mal orthographié


def exiger_valide(collection: str, document) -> None:
    erreurs = vm.erreurs(collection, document)
    assert erreurs == [], f"document invalide pour {collection} : {erreurs}"


def test_quatre_collections_decrites():
    assert vm.collections() == ["caracteristiques_clients", "evenements", "retours_clients",
                                "signaux_fraude"]

    # Une cinquième collection n'a pas de schéma : elle est refusée, pas devinée.
    with pytest.raises(FileNotFoundError):
        vm.schema("cinquieme_collection")


@pytest.mark.parametrize("collection", vm.collections())
def test_schema_conforme_au_sous_ensemble_mongodb(collection):
    brut = json.loads((vm.DOSSIER_SCHEMAS / f"{collection}.json").read_text(encoding="utf-8"))
    exiger_sous_ensemble_mongodb(brut, collection)

    fautifs = {
        "une mention de droits modifiée": {**brut, "_copyright": "autre"},
        "un type racine qui n'est pas un objet": {**brut, "bsonType": "array"},
        "un titre qui n'est pas la collection": {**brut, "title": "autre"},
        "un mot-clé que MongoDB refuse": {**brut, "$ref": "#/definitions/x"},
        "un format que MongoDB refuse": {**brut, "properties": {"x": {"format": "date-time"}}},
        "un type BSON mal orthographié": {**brut, "properties": {"x": {"bsonType": "chaine"}}},
    }
    for description, fautif in fautifs.items():
        with pytest.raises(AssertionError):
            exiger_sous_ensemble_mongodb(fautif, collection)
        assert fautif != brut, description


def test_les_documents_de_la_demonstration_sont_valides():
    db = nosql.base()
    nosql.evenements_synthetiques(db, 500)
    nosql.retours_synthetiques(db, 200)
    for collection in ("evenements", "retours_clients"):
        for doc in db[collection].find():
            exiger_valide(collection, doc)

    # Le même contrôle refuse un document de la démonstration dont on a abîmé un champ.
    evenement = db.evenements.find_one()
    assert evenement is not None
    with pytest.raises(AssertionError, match="evenements"):
        exiger_valide("evenements", {**evenement, "id_client": 0})
    with pytest.raises(AssertionError):
        exiger_valide("evenements", {k: v for k, v in evenement.items() if k != "session"})


def test_les_caracteristiques_et_signaux_de_la_demonstration_sont_valides():
    db = nosql.base()
    for i in range(1, 30):
        doc = nosql.mettre_a_jour_caracteristiques(
            db, {"id_client": i, "montant_usd": 12.5 * i, "pays_ip": "FR"})
        exiger_valide("caracteristiques_clients", doc)
        signal = {"id_paiement": i, "score": round(0.03 * i, 4),
                  "version_modele": "fraude-hgb-1.0", "horodatage": datetime.now(UTC)}
        exiger_valide("signaux_fraude", signal)

    # Un score hors de [0, 1] et un cumul négatif sont refusés par les mêmes contrôles.
    with pytest.raises(AssertionError, match="signaux_fraude"):
        exiger_valide("signaux_fraude", {**signal, "score": 1.5})
    with pytest.raises(AssertionError, match="caracteristiques_clients"):
        exiger_valide("caracteristiques_clients", {**doc, "montant_total_usd": -1.0})


def _evenement(**champs):
    doc = {"id_client": 1, "type": "connexion", "horodatage": datetime(2026, 9, 1, tzinfo=UTC),
           "session": {"appareil": "mobile", "duree_s": 30}}
    doc.update(champs)
    return doc


@pytest.mark.parametrize("collection, document", [
    ("evenements", _evenement(type="virement")),
    ("evenements", _evenement(id_client=0)),
    ("evenements", _evenement(id_client="1")),
    ("evenements", _evenement(horodatage="2026-09-01")),
    ("evenements", _evenement(session={"appareil": "console", "duree_s": 30})),
    ("evenements", _evenement(session={"appareil": "mobile", "duree_s": -1})),
    ("evenements", _evenement(type="erreur_paiement")),  # une erreur exige le sous-document
    ("evenements", {"id_client": 1, "type": "connexion"}),  # champs obligatoires manquants
    ("retours_clients", {"id_marchand": 1, "note": 6, "texte": "x",
                         "date": datetime(2026, 9, 1), "analyse": {
                             "sentiment": "neutre", "themes": ["frais"]}}),
    ("retours_clients", {"id_marchand": 1, "note": 3, "texte": "x",
                         "date": datetime(2026, 9, 1), "analyse": {
                             "sentiment": "neutre", "themes": []}}),
    ("retours_clients", {"id_marchand": 1, "note": 3, "texte": "x",
                         "date": datetime(2026, 9, 1), "analyse": {
                             "sentiment": "furieux", "themes": ["frais"]}}),
    ("caracteristiques_clients", {"id_client": 1, "nb_paiements": 1, "montant_total_usd": 5.0,
                                  "montant_max_usd": 5.0, "pays_ip_vus": ["fr"],
                                  "mis_a_jour_le": datetime(2026, 9, 1)}),
    ("caracteristiques_clients", {"id_client": 1, "nb_paiements": 1, "montant_total_usd": -5.0,
                                  "montant_max_usd": 5.0, "pays_ip_vus": ["FR"],
                                  "mis_a_jour_le": datetime(2026, 9, 1)}),
    ("signaux_fraude", {"id_paiement": 1, "score": 1.5, "version_modele": "v1",
                        "horodatage": datetime(2026, 9, 1)}),
    ("signaux_fraude", {"id_paiement": 1, "score": True, "version_modele": "v1",
                        "horodatage": datetime(2026, 9, 1)}),
    ("signaux_fraude", {"id_paiement": 1, "score": 0.5, "version_modele": "",
                        "horodatage": datetime(2026, 9, 1)}),
])
def test_les_documents_invalides_sont_refuses(collection, document):
    with pytest.raises(AssertionError, match="document invalide"):
        exiger_valide(collection, document)
    assert not vm.est_valide(collection, document)


def test_un_evenement_d_erreur_complet_est_accepte():
    doc = _evenement(type="erreur_paiement", erreur={"code": "card_declined", "tentative": 2})
    exiger_valide("evenements", doc)

    # La même erreur sans numéro de tentative, ou avec une tentative 0, est refusée.
    with pytest.raises(AssertionError):
        exiger_valide("evenements", _evenement(type="erreur_paiement",
                                               erreur={"code": "card_declined"}))
    with pytest.raises(AssertionError):
        exiger_valide("evenements", _evenement(type="erreur_paiement",
                                               erreur={"code": "card_declined", "tentative": 0}))


def test_le_schema_souple_accepte_des_champs_supplementaires():
    assert vm.est_valide("evenements", _evenement(version_app="3.2.1"))

    # « Souple » ne veut pas dire « sans règle » : le noyau obligatoire reste exigé.
    with pytest.raises(AssertionError):
        exiger_valide("evenements", _evenement(version_app="3.2.1", type="virement"))
