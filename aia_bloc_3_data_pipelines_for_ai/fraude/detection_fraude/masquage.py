# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Masquage des données personnelles dès l'ingestion (privacy by design), critère 5.1.

Le masquage est appliqué **à l'entrée du flux**, dans le producteur, avant toute publication
dans la file et toute écriture sur disque. Aucune couche située après lui (bronze, argent, or,
rapport, notification, journaux) n'a donc jamais accès aux données identifiantes : c'est la seule
façon de rendre l'engagement vérifiable.

Traitement champ par champ (principe de minimisation) :

* ``cc_num`` : HMAC-SHA256 à clé secrète (``carte_hash``) et 4 derniers chiffres (``carte_fin``) ;
* ``first``, ``last``, ``street``, ``zip``, ``city``, ``job``, ``gender`` : supprimés ;
* ``dob`` : remplacée par l'âge en années révolues (``age``) ;
* ``lat``, ``long`` : remplacées par ``distance_km`` (domicile -> commerçant), puis supprimées ;
* ``state`` : conservé (``etat``), les autres champs non identifiants aussi.

CHOIX
-----
- Retenu : une liste blanche. ``masquer`` construit un nouveau dictionnaire champ par champ : un
  champ ajouté un jour à la source n'est jamais recopié par inadvertance.
- Retenu : un hachage à clé (HMAC-SHA256) tronqué à 32 caractères hexadécimaux, avec mémoire des
  empreintes déjà calculées (au plus un million d'entrées).
- Écarté : un SHA-256 sans clé. Un numéro de carte a trop peu d'entropie (préfixe d'émetteur, clé
  de Luhn) pour qu'un hachage sans clé résiste à une énumération.
- Retenu : généraliser plutôt que supprimer quand l'information sert le modèle (âge, distance).
- Retenu : une version tolérante, ``masquer_pour_rejet``, pour la file des rejets : une ligne
  rejetée est une ligne dont on ne peut pas garantir la forme, donc aucune hypothèse de type, les
  champs inconnus sont nommés et jamais recopiés, tout nombre de 13 chiffres ou plus est effacé.
- Écarté : recopier la ligne brute dans la file des rejets pour faciliter le diagnostic.

OÙ
---
- Place : première transformation du flux, juste après ``schema.valider_brut`` ; son résultat est
  recontrôlé par ``schema.valider_masque`` avant publication.
- Appelants : Producteur.traiter_ligne (detection_fraude/producteur.py:425-440) (``masquer``),
  Producteur._vers_rejets (detection_fraude/producteur.py:403-423) (``masquer_pour_rejet``),
  preparer_jeu_entrainement (detection_fraude/jeu_donnees.py:164-191) (même code à l'entraînement).
- Dépendances : ``journal.nettoyer`` (effacement des suites de chiffres),
  ``schema.VERSION_SCHEMA``.

COMMENT
-------
1. ``HacheurCarte.empreinte`` calcule HMAC-SHA256(clé, numéro) et en garde les ``longueur``
   premiers caractères (16 à 64) ; une clé vide ou une longueur hors de cet intervalle est refusée.
2. ``masquer`` convertit l'horodatage de millisecondes en secondes, calcule l'âge à la date de la
   transaction, la distance de haversine (rayon terrestre 6371,0088 km) et construit le message.
3. ``masquer_pour_rejet`` garde la valeur tronquée (80 caractères) des champs sûrs, réduit les
   champs sensibles à leur nom dans ``champs_masques`` et liste les champs inconnus.
Tests qui le prouvent : test_le_message_ne_contient_que_les_champs_prevus
(tests/test_masquage.py:41-43), test_aucune_valeur_sensible_dans_le_message
(tests/test_masquage.py:46-57), test_hacheur_refuse_une_cle_vide_ou_une_longueur_absurde
(tests/test_masquage.py:82-86), test_masquage_pour_rejet_ne_garde_rien_d_identifiant
(tests/test_masquage.py:108-115) et
test_au_plafond_la_memoire_des_empreintes_reste_inchangee_et_le_numero_suivant_est_recalcule
(tests/test_masquage_garanties.py:165-191),
test_masquer_refuse_une_ligne_a_laquelle_il_manque_un_champ_utile_et_la_laisse_inchangee
(tests/test_masquage_garanties.py:404-428) et
test_une_ligne_a_l_heure_illisible_va_en_dlq_et_les_lignes_valides_voisines_passent
(tests/test_rejet_horodatage_illisible.py:38-60).

POURQUOI
--------
- Masquage dès l'ingestion, liste blanche et limites (pseudonymisation n'est pas anonymisation) :
  livrables/aia3/03_securite_conformite.md:21-73.
- Hachage à clé et non simple (exigence 3.5.1.1 de PCI DSS 4.0.1 telle que transposée) :
  livrables/aia3/03_securite_conformite.md:92-111 ; texte de la norme non reproduit ici.
- Principe de minimisation (RGPD, article 5, paragraphe 1, point c) : texte du règlement non
  reproduit ici ; correspondance avec les champs du projet :
  livrables/aia3/03_securite_conformite.md:10-19.
- Preuve par le résultat : test_aucune_donnee_personnelle_dans_aucun_fichier_produit
  (tests/test_bout_en_bout.py:138-161).
- Critère 5.1 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Ce module n'écrit aucun journal : ses fonctions sont pures. Ce qu'il produit est journalisé par
l'appelant (événement ``ligne_rejetee`` du producteur, niveau WARNING). Par défense en
profondeur, ``journal.nettoyer`` masque aussi toute suite de 13 à 19 chiffres si l'une d'elles
atteignait un journal.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from detection_fraude.journal import nettoyer
from detection_fraude.schema import VERSION_SCHEMA

# Champs de la source qui ne sortent jamais du masquage.
CHAMPS_SOURCE_SENSIBLES = (
    "cc_num",
    "first",
    "last",
    "street",
    "dob",
    "lat",
    "long",
    "zip",
    "city",
    "job",
    "gender",
)

# Champs conservés tels quels (non identifiants) avec leur nom dans le message masqué.
_CHAMPS_CONSERVES = {
    "merchant": "commercant",
    "category": "categorie",
    "amt": "montant",
    "state": "etat",
    "city_pop": "population_ville",
    "merch_lat": "lat_commercant",
    "merch_long": "long_commercant",
}

# Champs non sensibles dont on garde la valeur (tronquée) dans un enregistrement de rejet.
_CHAMPS_SURS_POUR_REJET = frozenset(
    {
        "merchant",
        "category",
        "amt",
        "state",
        "city_pop",
        "merch_lat",
        "merch_long",
        "trans_num",
        "is_fraud",
        "current_time",
    }
)
TEXTE_MASQUE = "[MASQUE]"
RAYON_TERRE_KM = 6371.0088
# Plafond de la mémoire des empreintes (le jeu réel compte quelques centaines de milliers
# de cartes).
TAILLE_MEMOIRE_EMPREINTES = 1_000_000


class HacheurCarte:
    """Calcule l'empreinte HMAC-SHA256 d'un numéro de carte, avec mémoire des résultats."""

    def __init__(self, cle: bytes, longueur: int = 32) -> None:
        if not cle:
            raise ValueError("La clé de hachage ne peut pas être vide.")
        if not 16 <= longueur <= 64:
            raise ValueError("La longueur d'empreinte doit être comprise entre 16 et 64.")
        self._cle = cle
        self._longueur = longueur
        self._memoire: dict[int, str] = {}

    def empreinte(self, numero: int) -> str:
        # Un jeu réel compte quelques centaines de milliers de cartes : on évite de recalculer.
        trouve = self._memoire.get(numero)
        if trouve is None:
            condense = hmac.new(self._cle, str(numero).encode("ascii"), hashlib.sha256)
            trouve = condense.hexdigest()[: self._longueur]
            if len(self._memoire) < TAILLE_MEMOIRE_EMPREINTES:
                self._memoire[numero] = trouve
        return trouve


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique (formule de haversine) entre deux points, en kilomètres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = p2 - p1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * RAYON_TERRE_KM * math.asin(min(1.0, math.sqrt(a)))


def calculer_age(naissance: date, reference: date) -> int:
    """Âge en années révolues à la date de référence (la date de la transaction)."""
    age = reference.year - naissance.year
    if (reference.month, reference.day) < (naissance.month, naissance.day):
        age -= 1
    return max(age, 0)


def horodatage_depuis_ms(milliseconds: int) -> int:
    return int(milliseconds) // 1000


def masquer(ligne: Mapping[str, Any], hacheur: HacheurCarte, *, source: str) -> dict[str, Any]:
    """Transforme une ligne brute *déjà validée* en message masqué.

    La fonction construit un nouveau dictionnaire champ par champ (liste blanche) : un champ
    ajouté un jour à la source n'est donc jamais recopié par inadvertance.
    """
    numero = int(ligne["cc_num"])
    secondes = horodatage_depuis_ms(ligne["current_time"])
    jour = datetime.fromtimestamp(secondes, UTC).date()
    age = calculer_age(date.fromisoformat(ligne["dob"]), jour)
    km = distance_km(ligne["lat"], ligne["long"], ligne["merch_lat"], ligne["merch_long"])
    message: dict[str, Any] = {
        "version_schema": VERSION_SCHEMA,
        "trans_num": ligne["trans_num"],
        "horodatage_s": secondes,
        "carte_hash": hacheur.empreinte(numero),
        "carte_fin": str(numero)[-4:],
        "age": age,
        "distance_km": round(km, 3),
        "source": source,
    }
    for nom_source, nom_cible in _CHAMPS_CONSERVES.items():
        message[nom_cible] = ligne[nom_source]
    message["montant"] = float(message["montant"])
    message["population_ville"] = int(message["population_ville"])
    message["lat_commercant"] = float(message["lat_commercant"])
    message["long_commercant"] = float(message["long_commercant"])
    etiquette = ligne.get("is_fraud")
    message["etiquette_source"] = None if etiquette is None else int(etiquette)
    return message


def masquer_pour_rejet(ligne: Any) -> dict[str, Any]:
    """Version tolérante du masquage pour la file des rejets (DLQ).

    Une ligne rejetée est précisément une ligne dont on ne peut pas garantir la forme : on ne
    s'appuie donc sur aucune hypothèse de type. Règles : seuls les champs de la liste blanche
    sont conservés (valeur tronquée) ; les champs sensibles connus sont réduits à ``[MASQUE]`` ;
    les champs inconnus sont seulement nommés, jamais recopiés.
    """
    if not isinstance(ligne, Mapping):
        return {"type_recu": type(ligne).__name__, "contenu": TEXTE_MASQUE}
    conserves: dict[str, Any] = {}
    masques: list[str] = []
    inconnus: list[str] = []
    for cle, valeur in ligne.items():
        nom = str(cle)
        if nom == "current_time":
            conserves[nom] = _horodatage_ms_ou_masque(valeur)
        elif nom in _CHAMPS_SURS_POUR_REJET:
            conserves[nom] = _tronquer(valeur)
        elif nom in CHAMPS_SOURCE_SENSIBLES:
            masques.append(nom)
        else:
            inconnus.append(nom)
    return {
        "champs_conserves": conserves,
        "champs_masques": sorted(masques),
        "champs_inconnus": sorted(inconnus),
    }


def _horodatage_ms_ou_masque(valeur: Any) -> Any:
    """Garde un horodatage en millisecondes plausible (13 chiffres) ; masque tout le reste.

    Les nombres de 13 chiffres ou plus sont d'ordinaire effacés (forme d'un numéro de carte),
    mais un horodatage d'époque en millisecondes en a 13 : on le reconnaît à sa plage de valeurs.
    """
    # Le type est contrôlé avant la comparaison : une heure absente ou textuelle (ce que le schéma
    # rejette) ne doit pas faire lever TypeError dans la voie des rejets.
    est_entier = isinstance(valeur, int) and not isinstance(valeur, bool)
    if est_entier and 946_684_800_000 <= valeur <= 4_102_444_800_000:
        return valeur
    return _tronquer(valeur)


def _tronquer(valeur: Any, longueur: int = 80) -> Any:
    """Tronque une valeur et efface tout ce qui ressemble à un numéro de carte."""
    if valeur is None or isinstance(valeur, bool):
        return valeur
    if isinstance(valeur, int | float):
        # Un « nombre » de 13 chiffres ou plus dans un champ numérique est suspect : on l'efface.
        return TEXTE_MASQUE if abs(valeur) >= 1e12 else valeur
    texte = nettoyer(str(valeur))
    return texte if len(texte) <= longueur else texte[: longueur - 1] + "…"
