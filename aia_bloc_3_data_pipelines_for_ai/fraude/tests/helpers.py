# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Fabriques de données synthétiques et doublures partagées par les tests.

Toutes les personnes, adresses et numéros de carte sont **fictifs**. Les noms contiennent le
marqueur ``Zx`` : les tests de confidentialité cherchent ce marqueur dans tous les fichiers
produits ; s'il y apparaît, une donnée identifiante a fuité.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

MARQUEUR = "Zx"
CATEGORIES = [
    "grocery_pos",
    "gas_transport",
    "shopping_net",
    "misc_net",
    "entertainment",
    "food_dining",
    "home",
    "travel",
    "personal_care",
    "health_fitness",
]


def identifiant(graine: str) -> str:
    return hashlib.md5(graine.encode(), usedforsecurity=False).hexdigest()


def ligne_brute(rang: int = 0, **surcharges: Any) -> dict[str, Any]:
    """Une ligne au format de l'API, valide, avec des données personnelles fictives marquées."""
    base = {
        "cc_num": 4_929_000_000_000_000 + rang,
        "merchant": f"fraud_Marchand {rang % 7}",
        "category": CATEGORIES[rang % len(CATEGORIES)],
        "amt": 20.0 + (rang % 13) * 3.5,
        "first": f"Prenom{MARQUEUR}{rang}",
        "last": f"Nom{MARQUEUR}{rang}",
        "gender": "F" if rang % 2 else "M",
        "street": f"{rang} Rue Secrete{MARQUEUR}",
        "city": f"Ville{MARQUEUR}",
        "state": "NY",
        "zip": 10_001 + rang % 50,
        "lat": 40.7128,
        "long": -74.0060,
        "city_pop": 8_000_000,
        "job": f"Metier{MARQUEUR}",
        "dob": "1985-06-15",
        "trans_num": identifiant(f"tx{rang}"),
        "merch_lat": 40.80,
        "merch_long": -73.95,
        "is_fraud": 0,
        "current_time": int(datetime(2026, 10, 9, 12, 0, tzinfo=UTC).timestamp() * 1000)
        + rang * 1000,
    }
    base.update(surcharges)
    return base


def fabriquer_jeu(nb_lignes: int = 6000, jours: int = 24, graine: int = 1) -> pd.DataFrame:
    """Jeu synthétique au format de ``fraudTest.csv`` avec une fraude apprenable.

    Règle cachée : nuit (22 h-3 h), catégorie ``shopping_net`` ou ``misc_net``, montant élevé.
    """
    hasard = random.Random(graine)
    debut = datetime(2020, 6, 21, tzinfo=UTC)
    cartes = []
    for i in range(40):
        cartes.append(
            {
                "cc_num": 4_000_000_000_000_000 + i * 7919,
                "first": f"Prenom{MARQUEUR}{i}",
                "last": f"Nom{MARQUEUR}{i}",
                "street": f"{i} Rue Cachee{MARQUEUR}",
                "city": f"Ville{MARQUEUR}{i % 5}",
                "state": ["NY", "CA", "TX", "FL", "WA"][i % 5],
                "zip": 10_000 + i,
                "lat": 30 + hasard.random() * 15,
                "long": -120 + hasard.random() * 40,
                "city_pop": hasard.choice([300, 5_000, 90_000, 2_000_000]),
                "job": f"Metier{MARQUEUR}{i}",
                "dob": f"{1950 + i}-0{1 + i % 9}-1{i % 9}",
                "gender": "F" if i % 2 else "M",
            }
        )
    instants = sorted(hasard.uniform(0, jours * 86400) for _ in range(nb_lignes))
    lignes = []
    for rang, secondes in enumerate(instants):
        carte = hasard.choice(cartes)
        moment = debut + timedelta(seconds=secondes)
        fraude = hasard.random() < 0.02
        if fraude:
            moment = moment.replace(hour=hasard.choice([22, 23, 0, 1, 2, 3]))
            categorie = hasard.choice(["shopping_net", "misc_net"])
            montant = round(hasard.uniform(300, 1200), 2)
        else:
            categorie = hasard.choice(CATEGORIES)
            montant = round(float(np.exp(hasard.gauss(3.5, 0.9))), 2)
        lignes.append(
            {
                "Unnamed: 0": rang,
                "trans_date_trans_time": moment.strftime("%Y-%m-%d %H:%M:%S"),
                "cc_num": carte["cc_num"],
                "merchant": f"fraud_Commerce {hasard.randint(1, 60)}",
                "category": categorie,
                "amt": max(montant, 1.0),
                "first": carte["first"],
                "last": carte["last"],
                "gender": carte["gender"],
                "street": carte["street"],
                "city": carte["city"],
                "state": carte["state"],
                "zip": carte["zip"],
                "lat": round(carte["lat"], 4),
                "long": round(carte["long"], 4),
                "city_pop": carte["city_pop"],
                "job": carte["job"],
                "dob": carte["dob"],
                "trans_num": identifiant(f"jeu{graine}-{rang}"),
                "unix_time": 1_371_816_865 + rang,
                "merch_lat": round(carte["lat"] + hasard.uniform(-0.8, 0.8), 6),
                "merch_long": round(carte["long"] + hasard.uniform(-0.8, 0.8), 6),
                "is_fraud": int(fraude),
            }
        )
    df = pd.DataFrame(lignes).sort_values("trans_date_trans_time", kind="stable")
    return df.reset_index(drop=True)


class ModeleFactice:
    """Modèle déterministe : fraude si le montant dépasse 500 (aucun entraînement nécessaire)."""

    seuil = 0.5
    version = "factice-1"

    def noter(self, caracteristiques: pd.DataFrame) -> np.ndarray:
        return np.where(caracteristiques["montant"].to_numpy() > 500, 0.95, 0.05)


class ChargeurFactice:
    def __init__(self, modele: Any | None = None) -> None:
        self.modele = modele or ModeleFactice()

    def obtenir(self) -> Any:
        return self.modele


class CanalEnregistreur:
    """Canal de notification qui garde tout en mémoire ; peut échouer à la demande."""

    def __init__(self, nom: str = "enregistreur", echecs: int = 0) -> None:
        self.nom = nom
        self.recus: list[Any] = []
        self.echecs_restants = echecs

    def envoyer(self, notification: Any) -> None:
        if self.echecs_restants > 0:
            self.echecs_restants -= 1
            raise ConnectionError("canal en panne (test)")
        self.recus.append(notification)
