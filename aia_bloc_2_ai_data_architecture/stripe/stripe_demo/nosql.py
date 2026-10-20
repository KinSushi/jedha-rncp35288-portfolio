# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Base orientée documents (NoSQL) : événements, retours clients, caractéristiques en ligne.

En production : MongoDB Atlas (ou Amazon DocumentDB). En démonstration : `mongomock`, qui offre
la même API que le pilote officiel `pymongo` (mêmes requêtes, mêmes pipelines d'agrégation).

Collections et choix de modélisation :
- `evenements` : flux de navigation et de session (schéma souple, un document par événement,
  index composé client + date, durée de vie limitée à 90 jours — minimisation RGPD) ;
- `retours_clients` : avis et réponses aux enquêtes ; le texte et son analyse (sentiment,
  thèmes) sont **imbriqués** dans le document, car toujours lus ensemble ;
- `caracteristiques_clients` : magasin de caractéristiques « en ligne » pour le score de fraude
  en temps réel (compteurs glissants), un document par client, **référencé** par `id_client`
  (la source de vérité du client reste l'OLTP) ;
- `signaux_fraude` : score attribué à chaque paiement, avec la version du modèle (traçabilité).

CHOIX
-----
- Retenu : `mongomock` en démonstration, parce qu'il offre la même API que `pymongo` (mêmes
  requêtes, mêmes pipelines d'agrégation) sans serveur ; la production visée est MongoDB Atlas
  ou Amazon DocumentDB.
- Retenu : mise à jour des caractéristiques par `find_one_and_update` avec `$inc`, `$addToSet`,
  `$max` et `upsert` : atomique pour un document, donc deux paiements simultanés du même client
  ne perdent aucune mise à jour. Écarté : lire puis réécrire le document (course entre deux
  paiements).
- Retenu : durée de vie de 90 jours sur `evenements` (minimisation RGPD). Écarté : conserver
  les événements sans limite.
- Retenu : des générateurs à graine fixe (7 pour les événements, 11 pour les retours), pour que
  les chiffres de la démonstration se reproduisent.
- Limite connue, non corrigée : `$inc` n'est pas idempotent (un message rejoué serait compté
  deux fois) ; la parade est décrite dans la note 04 mais pas codée.

OÙ
---
- Place : couche « documents » de la démonstration. Appelants : `stripe_demo/pipeline.py`
  (`base`, `evenements_synthetiques`, `retours_synthetiques`, `mettre_a_jour_caracteristiques`,
  `REQUETES`, `REQUETES_RETOURS`), `stripe_demo/generer_ddl.py` (les index déclarés doivent
  rester ceux de `base`), les tests `tests/test_nosql.py`, `tests/test_nosql_generateurs.py`.
- Dépendances : `mongomock` et `pymongo` (les constantes `ASCENDING` et `DESCENDING`).

COMMENT
-------
- `base()` crée la base `stripe` et les quatre index de la démonstration ; les deux générateurs
  insèrent des documents tirés avec `random.Random(graine)` ; `mettre_a_jour_caracteristiques`
  fait l'upsert atomique et rend le document APRÈS modification ; `REQUETES` et
  `REQUETES_RETOURS` sont les pipelines d'agrégation de la note 08.
- Ce qui le prouve : `tests/test_nosql.py` (requêtes sur de petits jeux écrits à la main dont
  on connaît la réponse, unicité, cumul sans perte) et `tests/test_nosql_generateurs.py`
  (index exacts, empreinte SHA-256 de 300 événements et de 50 retours, bornes des tirages avec
  un faux générateur aléatoire, un paiement incomplet est refusé).

POURQUOI
--------
- Les quatre collections et leurs choix d'imbrication : `livrables/aia2/04_nosql_modele.md:6-64`.
- Les index, dont le TTL de 7 776 000 s (90 jours) : `livrables/aia2/04_nosql_modele.md:97-108`.
- Atomicité par document et limite de `$inc` : `livrables/aia2/04_nosql_modele.md:118-123`.

JOURNALISATION
--------------
- Rien n'est journalisé (pas de module `logging`). Un paiement incomplet lève `KeyError`, un
  index unique violé lève `DuplicateKeyError` : aucune exception n'est avalée.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import mongomock
from pymongo import ASCENDING, DESCENDING

# Évaluée à la demande (instruction `type`) : `mongomock.Database` n'est pas générique à
# l'exécution, seul mypy lit le paramètre de type.
type BaseMongo = mongomock.Database[dict[str, Any]]


def base() -> BaseMongo:
    """Base NoSQL de démonstration et index (les mêmes appels créeraient les index MongoDB)."""
    db: BaseMongo = mongomock.MongoClient().stripe
    db.evenements.create_index([("id_client", ASCENDING), ("horodatage", DESCENDING)])
    db.evenements.create_index("horodatage", expireAfterSeconds=90 * 24 * 3600)
    db.caracteristiques_clients.create_index("id_client", unique=True)
    db.signaux_fraude.create_index([("score", DESCENDING)])
    return db


def evenements_synthetiques(db: BaseMongo, nombre: int, graine: int = 7) -> None:
    """Événements de navigation : schéma variable selon le type (atout du modèle document)."""
    alea = random.Random(graine)
    debut = datetime(2026, 9, 1, tzinfo=UTC)
    documents = []
    for _ in range(nombre):
        type_evenement = alea.choice(["page_paiement", "erreur_paiement", "connexion", "abandon"])
        doc = {"id_client": alea.randint(1, 2000), "type": type_evenement,
               "horodatage": debut + timedelta(minutes=alea.randint(0, 43_200)),
               "session": {"appareil": alea.choice(["mobile", "ordinateur"]),
                           "duree_s": alea.randint(5, 900)}}
        if type_evenement == "erreur_paiement":
            doc["erreur"] = {"code": alea.choice(["card_declined", "insufficient_funds",
                                                 "expired_card"]), "tentative": alea.randint(1, 3)}
        documents.append(doc)
    db.evenements.insert_many(documents)


def retours_synthetiques(db: BaseMongo, nombre: int, graine: int = 11) -> None:
    """Retours clients avec analyse imbriquée (sentiment et thèmes)."""
    alea = random.Random(graine)
    themes = ["frais", "rapidite", "support", "litige", "integration"]
    db.retours_clients.insert_many([
        {"id_marchand": alea.randint(1, 40), "note": (n := alea.randint(1, 5)),
         "texte": "avis de démonstration",
         "date": datetime(2026, 9, alea.randint(1, 28), tzinfo=UTC),
         "analyse": {"sentiment": "positif" if n >= 4 else "negatif" if n <= 2 else "neutre",
                     "themes": alea.sample(themes, k=2)}}
        for _ in range(nombre)])


def mettre_a_jour_caracteristiques(db: BaseMongo, paiement: dict[str, Any]) -> dict[str, Any]:
    """Met à jour les compteurs du client à chaque paiement (upsert atomique) ; renvoie le doc.

    `$inc`, `$addToSet`, `$max` sont des opérateurs atomiques : deux paiements simultanés du
    même client ne perdent aucune mise à jour.
    """
    document = db.caracteristiques_clients.find_one_and_update(
        {"id_client": paiement["id_client"]},
        {"$inc": {"nb_paiements": 1, "montant_total_usd": paiement["montant_usd"]},
         "$addToSet": {"pays_ip_vus": paiement["pays_ip"]},
         "$max": {"montant_max_usd": paiement["montant_usd"]},
         "$set": {"mis_a_jour_le": datetime.now(UTC)}},
        upsert=True, return_document=True)
    # Avec `upsert=True` et `return_document=True` (après modification), MongoDB renvoie toujours
    # le document : le `None` possible dans les types ne se produit pas.
    assert document is not None
    return document


REQUETES: dict[str, list[dict[str, Any]]] = {
    "erreurs de paiement les plus fréquentes (30 jours)": [
        {"$match": {"type": "erreur_paiement"}},
        {"$group": {"_id": "$erreur.code", "nombre": {"$sum": 1}}},
        {"$sort": {"nombre": -1}},
    ],
    "sessions mobiles abandonnées les plus longues": [
        {"$match": {"type": "abandon", "session.appareil": "mobile"}},
        {"$sort": {"session.duree_s": -1}}, {"$limit": 3},
        {"$project": {"_id": 0, "id_client": 1, "duree_s": "$session.duree_s"}},
    ],
}

REQUETES_RETOURS: dict[str, list[dict[str, Any]]] = {
    "thèmes des avis négatifs": [
        {"$match": {"analyse.sentiment": "negatif"}}, {"$unwind": "$analyse.themes"},
        {"$group": {"_id": "$analyse.themes", "nombre": {"$sum": 1}}},
        {"$sort": {"nombre": -1}},
    ],
}
