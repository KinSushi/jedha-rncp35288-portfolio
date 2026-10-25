// Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
// Fichier GÉNÉRÉ par `python -m stripe_demo.generer_ddl` depuis nosql/schemas/*.json.
// Ne pas modifier à la main. Usage : mongosh "$URI_MONGO" nosql/creer_collections.js
// Exécution idempotente : une collection existante reçoit le validateur à jour (collMod).
// Remarque : $jsonSchema est une fonction de MongoDB ; pour Amazon DocumentDB, vérifier sa prise
// en charge avant bascule (à défaut, la validation reste applicative).

const base = db.getSiblingDB("stripe");

const schemas = {
  "caracteristiques_clients": {
    "title": "caracteristiques_clients",
    "description": "Magasin de caractéristiques en ligne pour le score de fraude : un document par client, mis à jour de façon atomique à chaque paiement.",
    "bsonType": "object",
    "required": [
      "id_client",
      "nb_paiements",
      "montant_total_usd",
      "montant_max_usd",
      "pays_ip_vus",
      "mis_a_jour_le"
    ],
    "properties": {
      "_id": {
        "bsonType": "objectId"
      },
      "id_client": {
        "bsonType": [
          "int",
          "long"
        ],
        "minimum": 1,
        "description": "Référence vers clients.id_client ; unique (index unique)."
      },
      "nb_paiements": {
        "bsonType": [
          "int",
          "long"
        ],
        "minimum": 1
      },
      "montant_total_usd": {
        "bsonType": "number",
        "minimum": 0
      },
      "montant_max_usd": {
        "bsonType": "number",
        "minimum": 0
      },
      "pays_ip_vus": {
        "bsonType": "array",
        "minItems": 1,
        "uniqueItems": true,
        "items": {
          "bsonType": "string",
          "pattern": "^[A-Z]{2}$"
        }
      },
      "mis_a_jour_le": {
        "bsonType": "date"
      }
    },
    "additionalProperties": true
  },
  "evenements": {
    "title": "evenements",
    "description": "Flux de navigation et de session : un document par événement. Noyau obligatoire validé, champs propres à chaque type d'événement libres (schéma souple).",
    "bsonType": "object",
    "required": [
      "id_client",
      "type",
      "horodatage",
      "session"
    ],
    "properties": {
      "_id": {
        "bsonType": "objectId"
      },
      "id_client": {
        "bsonType": [
          "int",
          "long"
        ],
        "minimum": 1,
        "description": "Référence vers clients.id_client de l'OLTP (source de vérité)."
      },
      "type": {
        "enum": [
          "page_paiement",
          "erreur_paiement",
          "connexion",
          "abandon"
        ],
        "description": "Type d'événement ; détermine les champs facultatifs présents."
      },
      "horodatage": {
        "bsonType": "date",
        "description": "Instant de l'événement ; sert aussi à l'index à durée de vie (90 jours)."
      },
      "session": {
        "bsonType": "object",
        "required": [
          "appareil",
          "duree_s"
        ],
        "properties": {
          "appareil": {
            "enum": [
              "mobile",
              "ordinateur"
            ]
          },
          "duree_s": {
            "bsonType": [
              "int",
              "long"
            ],
            "minimum": 0
          }
        }
      },
      "erreur": {
        "bsonType": "object",
        "required": [
          "code",
          "tentative"
        ],
        "properties": {
          "code": {
            "bsonType": "string",
            "minLength": 1
          },
          "tentative": {
            "bsonType": [
              "int",
              "long"
            ],
            "minimum": 1
          }
        }
      }
    },
    "anyOf": [
      {
        "properties": {
          "type": {
            "enum": [
              "erreur_paiement"
            ]
          }
        },
        "required": [
          "erreur"
        ]
      },
      {
        "properties": {
          "type": {
            "enum": [
              "page_paiement",
              "connexion",
              "abandon"
            ]
          }
        }
      }
    ],
    "additionalProperties": true
  },
  "retours_clients": {
    "title": "retours_clients",
    "description": "Avis et réponses aux enquêtes. Le texte et son analyse (sentiment, thèmes) sont imbriqués : ils sont toujours lus ensemble.",
    "bsonType": "object",
    "required": [
      "id_marchand",
      "note",
      "texte",
      "date",
      "analyse"
    ],
    "properties": {
      "_id": {
        "bsonType": "objectId"
      },
      "id_marchand": {
        "bsonType": [
          "int",
          "long"
        ],
        "minimum": 1
      },
      "note": {
        "bsonType": [
          "int",
          "long"
        ],
        "minimum": 1,
        "maximum": 5
      },
      "texte": {
        "bsonType": "string",
        "maxLength": 2000
      },
      "date": {
        "bsonType": "date"
      },
      "analyse": {
        "bsonType": "object",
        "required": [
          "sentiment",
          "themes"
        ],
        "properties": {
          "sentiment": {
            "enum": [
              "positif",
              "neutre",
              "negatif"
            ]
          },
          "themes": {
            "bsonType": "array",
            "minItems": 1,
            "maxItems": 5,
            "uniqueItems": true,
            "items": {
              "enum": [
                "frais",
                "rapidite",
                "support",
                "litige",
                "integration"
              ]
            }
          }
        }
      }
    },
    "additionalProperties": true
  },
  "signaux_fraude": {
    "title": "signaux_fraude",
    "description": "Score de fraude attribué à chaque paiement, avec la version du modèle qui l'a calculé (traçabilité).",
    "bsonType": "object",
    "required": [
      "id_paiement",
      "score",
      "version_modele",
      "horodatage"
    ],
    "properties": {
      "_id": {
        "bsonType": "objectId"
      },
      "id_paiement": {
        "bsonType": [
          "int",
          "long"
        ],
        "minimum": 1,
        "description": "Référence vers paiements.id_paiement de l'OLTP."
      },
      "score": {
        "bsonType": "number",
        "minimum": 0,
        "maximum": 1
      },
      "version_modele": {
        "bsonType": "string",
        "minLength": 1
      },
      "horodatage": {
        "bsonType": "date"
      }
    },
    "additionalProperties": true
  }
};

for (const [nom, schema] of Object.entries(schemas)) {
  const options = {
    validator: { $jsonSchema: schema },
    validationLevel: "strict",   // tout document inséré ou modifié est contrôlé
    validationAction: "error",   // un document invalide est refusé (et non seulement signalé)
  };
  if (base.getCollectionNames().includes(nom)) {
    base.runCommand({ collMod: nom, ...options });
  } else {
    base.createCollection(nom, options);
  }
}

// Index (le champ TTL supprime automatiquement les événements de plus de 90 jours : RGPD).
base.evenements.createIndex({"id_client": 1, "horodatage": -1}, {});
base.evenements.createIndex({"horodatage": 1}, {"expireAfterSeconds": 7776000});
base.caracteristiques_clients.createIndex({"id_client": 1}, {"unique": true});
base.signaux_fraude.createIndex({"score": -1}, {});
base.signaux_fraude.createIndex({"id_paiement": 1}, {});
base.retours_clients.createIndex({"id_marchand": 1, "date": -1}, {});
