# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Validation des documents NoSQL côté application, à partir des MÊMES schémas que MongoDB.

Pourquoi ce module : les schémas `nosql/schemas/*.json` sont écrits au format `$jsonSchema` de
MongoDB (mot-clé `bsonType`). Ils servent à trois endroits qui ne doivent jamais diverger :

1. le script `nosql/creer_collections.js`, qui les installe comme validateurs de collection ;
2. les tests, qui vérifient que les documents de la démonstration les respectent ;
3. l'application, quand le service cible ne prend pas en charge les validateurs (à confirmer
   pour Amazon DocumentDB) : la validation est alors faite avant l'écriture, par ce module.

Principe : on traduit `bsonType` en `type` (norme JSON Schema, brouillon 4, celui dont MongoDB
reprend le vocabulaire) et on apprend au validateur les types propres à BSON (date, ObjectId,
entiers 32 et 64 bits).

CHOIX
-----
- Retenu : une seule source de vérité, les fichiers `nosql/schemas/*.json` au format `$jsonSchema`
  de MongoDB, traduits à la lecture pour la bibliothèque `jsonschema`. Écarté : réécrire les
  règles en Python, qui divergeraient du validateur installé dans MongoDB.
- Retenu : le brouillon 4 de JSON Schema (le vocabulaire que MongoDB reprend) et des types BSON
  ajoutés au validateur : `int`, `long`, `number` (un booléen n'en est jamais un, bien qu'il soit
  un `int` en Python), `date`, `objectId`, `bool`.
- Retenu : rendre la LISTE des violations (chemin du champ en tête) plutôt que lever à la
  première. Écarté : une exception dès la première erreur, qui cacherait les suivantes.

OÙ
---
- Place : couche « validation des documents », filet côté application si le service cible ne prend
  pas en charge les validateurs (à confirmer pour Amazon DocumentDB). Appelants : les tests
  `tests/test_schemas_mongo.py` et `tests/test_validation_mongo.py` ; aucune écriture du code de
  production n'appelle encore ce module.
- Dépendances : `jsonschema` (brouillon 4), `bson` (via pymongo), les quatre schémas.

COMMENT
-------
- `schema` lit et traduit un schéma (mis en cache) ; `collections` liste les collections décrites ;
  `erreurs` rend les violations triées, chacune préfixée par le chemin du champ (`(document)`
  quand l'erreur porte sur le document entier) ; `est_valide` rend vrai sans violation.
- Ce qui le prouve : `tests/test_schemas_mongo.py` (les documents de la démonstration sont
  acceptés, seize documents faux sont refusés) et `tests/test_validation_mongo.py` (traduction
  exacte, types BSON, bornes incluses, format des messages, collection inconnue refusée, schéma
  gardé en mémoire).

POURQUOI
--------
- Validation des documents et point à confirmer sur DocumentDB :
  `livrables/aia2/04_nosql_modele.md:110-116`.
- Les schémas eux-mêmes : `nosql/schemas/evenements.json:1-43`.

JOURNALISATION
--------------
- Rien n'est journalisé (pas de module `logging`) : les violations sont RENDUES à l'appelant, qui
  décide quoi en faire. Une collection sans schéma lève `FileNotFoundError`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from bson import ObjectId
from jsonschema import Draft4Validator
from jsonschema.validators import extend

DOSSIER_SCHEMAS = Path(__file__).resolve().parent.parent / "nosql" / "schemas"

# `extend` n'est pas annoté dans les stubs de jsonschema : on l'appelle par un nom typé `Any`,
# pour que mypy --strict ne signale pas un appel non typé sans qu'aucun comportement ne change.
_etendre: Callable[..., Any] = extend


def _entier(_verificateur: Any, valeur: object) -> bool:
    """Entier BSON (int32 ou int64) : un booléen n'en est pas un, même s'il en hérite en Python."""
    return isinstance(valeur, int) and not isinstance(valeur, bool)


def _nombre(_verificateur: Any, valeur: object) -> bool:
    """« number » BSON tel que pymongo sait l'écrire : entier ou flottant, jamais un booléen."""
    return isinstance(valeur, int | float) and not isinstance(valeur, bool)


_TYPES = Draft4Validator.TYPE_CHECKER.redefine_many({
    "int": _entier,
    "long": _entier,
    "number": _nombre,
    "date": lambda _v, valeur: isinstance(valeur, datetime),
    "objectId": lambda _v, valeur: isinstance(valeur, ObjectId),
    "bool": lambda _v, valeur: isinstance(valeur, bool),
})
_Validateur = _etendre(Draft4Validator, type_checker=_TYPES)


def _vers_json_schema(noeud: Any) -> Any:
    """Remplace récursivement `bsonType` par `type` et retire les clés propres au dépôt."""
    if isinstance(noeud, dict):
        resultat = {}
        for cle, valeur in noeud.items():
            if cle == "_copyright":
                continue
            resultat["type" if cle == "bsonType" else cle] = _vers_json_schema(valeur)
        return resultat
    if isinstance(noeud, list):
        return [_vers_json_schema(element) for element in noeud]
    return noeud


@cache
def schema(collection: str) -> dict[str, Any]:
    """Schéma JSON Schema (brouillon 4) d'une collection, prêt pour le validateur."""
    chemin = DOSSIER_SCHEMAS / f"{collection}.json"
    brut = json.loads(chemin.read_text(encoding="utf-8"))
    converti: dict[str, Any] = _vers_json_schema(copy.deepcopy(brut))
    return converti


def collections() -> list[str]:
    """Noms des collections décrites par un schéma."""
    return sorted(chemin.stem for chemin in DOSSIER_SCHEMAS.glob("*.json"))


def erreurs(collection: str, document: dict[str, Any]) -> list[str]:
    """Liste des violations du schéma (vide si le document est valide), chemin en tête."""
    validateur = _Validateur(schema(collection))
    return sorted(
        f"{'.'.join(str(p) for p in e.absolute_path) or '(document)'} : {e.message}"
        for e in validateur.iter_errors(document))


def est_valide(collection: str, document: dict[str, Any]) -> bool:
    """Vrai si le document respecte le schéma de la collection."""
    return not erreurs(collection, document)
