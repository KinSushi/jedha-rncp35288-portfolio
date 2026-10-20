# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Traduction des schémas MongoDB en JSON Schema et messages d'erreur du validateur.

Complète `test_schemas_mongo.py` (qui vérifie les documents de la démonstration et un échantillon
de documents faux) : ici, on vérifie la TRADUCTION elle-même (bsonType devient type, la mention
de droits disparaît, les listes sont traduites élément par élément), la définition exacte des
types entiers et nombres de BSON, et le FORMAT des messages (chemin du champ fautif en tête).
Chaque groupe se termine par son contraire : une collection sans schéma est refusée.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from bson import ObjectId

from stripe_demo import validation_mongo as vm

MAINTENANT = datetime(2026, 9, 1, tzinfo=UTC)


def _evenement(**champs):
    document = {"id_client": 1, "type": "connexion", "horodatage": MAINTENANT,
                "session": {"appareil": "mobile", "duree_s": 30}}
    document.update(champs)
    return document


def _caracteristiques(**champs):
    document = {"id_client": 1, "nb_paiements": 1, "montant_total_usd": 5.0,
                "montant_max_usd": 5.0, "pays_ip_vus": ["FR"], "mis_a_jour_le": MAINTENANT}
    document.update(champs)
    return document


def _signal(**champs):
    document = {"id_paiement": 1, "score": 0.5, "version_modele": "fraude-hgb-1.0",
                "horodatage": MAINTENANT}
    document.update(champs)
    return document


def test_la_traduction_remplace_bsontype_garde_les_listes_et_refuse_une_collection_inconnue():
    # Aucune mention de droits ni bsonType ne subsiste, dans aucune des quatre collections.
    assert vm.collections() == sorted(vm.collections())
    assert len(vm.collections()) >= 4
    for collection in vm.collections():
        traduit = vm.schema(collection)
        assert "_copyright" not in traduit
        assert "bsonType" not in json.dumps(traduit)
        assert traduit["type"] == "object"

    # Dans les sous-documents, et élément par élément dans les listes.
    session = vm.schema("evenements")["properties"]["session"]
    assert session["type"] == "object"
    assert session["properties"]["duree_s"] == {"type": ["int", "long"], "minimum": 0}
    schema = vm.schema("evenements")
    assert schema["required"] == ["id_client", "type", "horodatage", "session"]
    assert schema["anyOf"][0]["required"] == ["erreur"]
    assert schema["anyOf"][0]["properties"]["type"] == {"enum": ["erreur_paiement"]}
    assert schema["anyOf"][1]["properties"]["type"] == {
        "enum": ["page_paiement", "connexion", "abandon"]}
    themes = vm.schema("retours_clients")["properties"]["analyse"]["properties"]["themes"]
    assert themes["type"] == "array"
    assert themes["items"] == {"enum": ["frais", "rapidite", "support", "litige", "integration"]}

    # Une collection sans schéma est refusée, par la traduction comme par la validation.
    with pytest.raises(FileNotFoundError):
        vm.schema("collection_qui_n_existe_pas")
    with pytest.raises(FileNotFoundError):
        vm.erreurs("collection_qui_n_existe_pas", {"id_client": 1})


def test_les_types_bson_sont_stricts_et_un_booleen_n_est_ni_un_entier_ni_un_nombre():
    # Entier : int32 et int64 acceptés ; booléen, flottant, texte, rien, liste refusés.
    assert vm.erreurs("evenements", _evenement(id_client=7)) == []
    assert vm.erreurs("evenements", _evenement(id_client=2**40)) == []  # int64
    for valeur in (True, False, 1.5, "1", None, [1]):
        erreurs = vm.erreurs("evenements", _evenement(id_client=valeur))
        assert erreurs and erreurs[0].startswith("id_client : "), valeur

    # Nombre : entier ou flottant acceptés ; booléen, texte, rien, liste refusés.
    for valeur in (0, 5, 5.0, 1e9):
        document = _caracteristiques(montant_total_usd=valeur)
        assert vm.erreurs("caracteristiques_clients", document) == [], valeur
    for valeur in (True, False, "5", None, [5.0]):
        erreurs = vm.erreurs("caracteristiques_clients",
                             _caracteristiques(montant_total_usd=valeur))
        assert erreurs and erreurs[0].startswith("montant_total_usd : "), valeur

    # Date : un `datetime` ; identifiant d'objet : un `ObjectId`, ni une chaîne ni un entier.
    assert vm.erreurs("signaux_fraude", _signal()) == []
    assert vm.erreurs("signaux_fraude", _signal(horodatage="2026-09-01"))
    assert vm.erreurs("signaux_fraude", _signal(horodatage=20260901))
    assert vm.erreurs("signaux_fraude", _signal(_id=ObjectId())) == []
    assert vm.erreurs("signaux_fraude", _signal(_id="507f1f77bcf86cd799439011"))
    assert vm.erreurs("signaux_fraude", _signal(_id=12))


def test_un_decimal_python_n_est_pas_un_nombre_bson_et_le_type_bool_est_reconnu(
        tmp_path, monkeypatch):
    """Deux types que les schémas livrés n'exercent pas complètement.

    - « number » : `jsonschema` accepterait un `decimal.Decimal`, que pymongo ne sait pas écrire
      (`InvalidDocument`). Le validateur le refuse donc, comme le ferait l'insertion.
    - « bool » : l'alias BSON de « boolean », absent de `jsonschema` ; aucun schéma livré ne
      l'emploie. Un schéma écrit pour ce test (dans un dossier jetable) vérifie qu'il est défini.
    """
    avec_decimal = vm.erreurs("caracteristiques_clients",
                              _caracteristiques(montant_total_usd=Decimal("5.0")))
    assert avec_decimal and avec_decimal[0].startswith("montant_total_usd : ")

    (tmp_path / "essai.json").write_text(json.dumps({
        "bsonType": "object", "required": ["actif"],
        "properties": {"actif": {"bsonType": "bool"}}}), encoding="utf-8")
    monkeypatch.setattr(vm, "DOSSIER_SCHEMAS", tmp_path)

    assert vm.erreurs("essai", {"actif": True}) == []
    assert vm.erreurs("essai", {"actif": False}) == []
    for valeur in (1, 0, "vrai", None):
        erreurs = vm.erreurs("essai", {"actif": valeur})
        assert erreurs and erreurs[0].startswith("actif : "), valeur


def test_les_messages_donnent_le_chemin_du_champ_fautif_et_les_bornes_sont_inclusives():
    # Erreur sur le document entier : « (document) » ; messages triés.
    manquants = vm.erreurs("evenements", {"id_client": 1, "type": "connexion"})
    assert manquants == sorted(manquants)
    assert len(manquants) >= 2  # champs obligatoires manquants : horodatage et session
    assert all(e.startswith("(document) : ") for e in manquants)
    assert any("horodatage" in e for e in manquants)
    assert any("session" in e for e in manquants)

    # Chemin imbriqué joint par des points ; rang d'un élément de liste.
    imbrique = vm.erreurs("evenements", _evenement(session={"appareil": "console", "duree_s": 30}))
    assert imbrique and imbrique[0].startswith("session.appareil : ")
    assert "console" in imbrique[0]
    liste = vm.erreurs("caracteristiques_clients", _caracteristiques(pays_ip_vus=["FR", "us"]))
    assert liste and liste[0].startswith("pays_ip_vus.1 : ")
    assert "us" in liste[0]

    # Plusieurs violations : toutes sont rendues, dans l'ordre.
    plusieurs = vm.erreurs("signaux_fraude", {"id_paiement": 0, "score": 2, "version_modele": "",
                                              "horodatage": "hier"})
    assert len(plusieurs) >= 4
    assert plusieurs == sorted(plusieurs)

    # Valide / invalide, et bornes incluses (0 et 1 sont des scores ; -0,0001 et 1,0001 non).
    assert vm.erreurs("signaux_fraude", _signal()) == []
    assert vm.est_valide("signaux_fraude", _signal()) is True
    assert vm.est_valide("signaux_fraude", _signal(score=1.5)) is False
    assert vm.est_valide("signaux_fraude", _signal(score=0))
    assert vm.est_valide("signaux_fraude", _signal(score=1))
    assert not vm.est_valide("signaux_fraude", _signal(score=-0.0001))
    assert not vm.est_valide("signaux_fraude", _signal(score=1.0001))
    assert vm.est_valide("signaux_fraude", _signal(id_paiement=1))
    assert not vm.est_valide("signaux_fraude", _signal(id_paiement=0))


def test_un_schema_deja_lu_est_garde_en_memoire_et_une_collection_inconnue_ne_l_est_pas():
    vm.schema.cache_clear()

    premier = vm.schema("evenements")
    assert vm.schema("evenements") is premier  # le même objet : le fichier n'est lu qu'une fois
    information = vm.schema.cache_info()
    assert (information.hits, information.misses) == (1, 1)
    assert information.maxsize is None  # cache sans limite, comme `lru_cache(maxsize=None)`

    # Une collection sans schéma lève la même erreur à chaque appel : rien n'est mis en cache.
    for _ in range(2):
        with pytest.raises(FileNotFoundError):
            vm.schema("collection_qui_n_existe_pas")
    assert vm.schema.cache_info().currsize == 1
