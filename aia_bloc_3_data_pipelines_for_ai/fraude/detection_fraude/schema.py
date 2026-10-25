# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Contrats de données : validation de schéma à chaque étape (critères 4.1 et 4.2).

Deux contrats, deux étapes du flux :

1. ``valider_brut``   : la ligne telle que l'API (ou le jeu) la livre. Elle est contrôlée *avant*
   masquage, en mémoire seulement : une ligne brute n'est jamais écrite sur disque.
2. ``valider_masque`` : le message masqué qui circule dans la file. Le consommateur le recontrôle
   à l'entrée (on ne fait pas confiance au producteur : défense en profondeur).

CHOIX
-----
- Retenu : renvoyer la liste complète des erreurs ``{champ, code, detail}`` au lieu de lever à la
  première anomalie. La file des rejets reçoit ainsi toutes les raisons du rejet, ce qui accélère
  le diagnostic.
- Retenu : un type strict. Un booléen n'est pas un nombre, un flottant non entier n'est pas un
  entier, ``NaN`` et l'infini ne sont pas des nombres (la source est traitée comme hostile).
- Retenu : le détail d'une erreur ne reprend jamais la valeur fautive d'un champ sensible
  (``cc_num``, ``dob``, ``first``...).
- Retenu : le contrat masqué est une liste blanche : tout champ inattendu est refusé, c'est ainsi
  qu'un champ sensible échappé au masquage est arrêté avant l'entrepôt.
- Écarté : lever une exception à la première erreur. Elle masquerait les autres anomalies de la
  même ligne.
- Écarté : une bibliothèque de validation externe. Alternative non documentée dans le dépôt
  (source : inconnue) ; les contrôles élémentaires tiennent en quelques fonctions testables.
- Retenu : des contrôles de champ en fonctions nommées (``_controle_borne``,
  ``_controle_motif``...), liées à leurs bornes par ``functools.partial`` dans la table. Écarté :
  des fabriques de fonctions appelées une seule fois, à l'import du module : leur corps n'est pas
  rejoué par les tests, donc un défaut qui s'y glisse échappe à la mutation (constaté pendant la
  mise en conformité du 12/10/2026 : tous les mutants de ces trois fabriques survivaient).

OÙ
---
- Place : frontière d'entrée du pipeline (brut) et contrat de la file (masqué).
- Appelants : Producteur.traiter_ligne (detection_fraude/producteur.py:425-440) (les deux
  contrats), Consommateur.traiter_lot (detection_fraude/consommateur.py:263-354) (contrat masqué),
  preparer_jeu_entrainement (detection_fraude/jeu_donnees.py:164-191) (contrat brut).
- Dépendances : bibliothèque standard seulement ; aucune dépendance interne.

COMMENT
-------
1. ``_controle_brut`` applique, champ par champ, un type, un format (expressions régulières) et
   une borne ; ``valider_brut`` parcourt les 20 champs obligatoires (absent ou ``None`` : code
   ``absent``) puis le champ facultatif ``is_fraud``.
2. Bornes : montant dans ]0 ; MONTANT_MAX], ``cc_num`` de 8 à 19 chiffres et positif, date de
   naissance entre 1900-01-01 et aujourd'hui, ``current_time`` entre l'an 2000 et maintenant + 1
   jour.
3. ``valider_masque`` applique la table ``_CONTROLES_MASQUES`` aux 15 champs obligatoires, au champ
   facultatif ``etiquette_source``, puis refuse tout autre nom de champ.
Tests qui le prouvent : test_chaque_corruption_est_detectee (tests/test_schema.py:27-56),
test_message_masque_refuse_un_champ_sensible_qui_aurait_survecu (tests/test_schema.py:89-92),
test_le_detail_ne_cite_jamais_une_valeur_sensible (tests/test_schema.py:77-81),
test_les_valeurs_limites_exactes_de_la_ligne_brute_sont_acceptees_sans_alteration
(tests/test_schema_refus.py:516-562) et
test_ce_qui_n_est_pas_un_objet_est_refuse_en_nommant_son_type_et_reste_inchange
(tests/test_schema_refus.py:117-129).

POURQUOI
--------
- Contrôles de qualité à chaque étape, triplet ``{champ, code, detail}`` :
  livrables/aia3/02_pipeline_et_qualite.md:210-230.
- Isolation des lignes corrompues vers la file des rejets :
  livrables/aia3/02_pipeline_et_qualite.md:232-269.
- Source traitée comme hostile (types stricts, bornes, expressions régulières) :
  livrables/aia3/03_securite_conformite.md:158-168.
- Bornes physiques : detection_fraude/schema.py:91 et la ligne suivante
  (detection_fraude/schema.py:92) ; leur justification est dans le code (commentaires).
- Indicateurs 4.1 et 4.2 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Ce module ne journalise rien : ses fonctions sont pures et renvoient des erreurs. Les rejets
sont journalisés par l'appelant : ``ligne_rejetee`` (producteur, WARNING, champs ``raison``,
``trans_num``, ``champs``, ``codes``) et ``message_rejete`` (consommateur, WARNING).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

VERSION_SCHEMA = 1

# Bornes physiques ou métier : au-delà, la ligne est considérée corrompue.
MONTANT_MAX = 1_000_000.0
EPOCH_MIN_S = 946_684_800  # 2000-01-01
DECALAGE_FUTUR_MAX_S = 86_400  # on tolère un jour d'avance d'horloge, pas plus

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_CATEGORIE = re.compile(r"^[a-z]+(_[a-z]+)*$")
_ETAT = re.compile(r"^[A-Z]{2}$")
_FINALE_CARTE = re.compile(r"^\d{4}$")


@dataclass(frozen=True)
class Erreur:
    champ: str
    code: str
    detail: str = ""

    def en_dict(self) -> dict[str, str]:
        return {"champ": self.champ, "code": self.code, "detail": self.detail}


# --------------------------------------------------------------------------------------------
# Briques de contrôle élémentaires (chacune renvoie un code d'erreur ou None)
# --------------------------------------------------------------------------------------------
def _est_nombre(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _est_entier(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, float) and math.isfinite(v) and v.is_integer()


def _texte_non_vide(v: Any, longueur_max: int = 200) -> str | None:
    if not isinstance(v, str):
        return "type_invalide"
    if not v.strip():
        return "vide"
    if len(v) > longueur_max:
        return "trop_long"
    return None


def _date_iso(v: Any) -> date | None:
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------
# Contrat 1 : ligne brute de l'API
# --------------------------------------------------------------------------------------------
CHAMPS_BRUTS_OBLIGATOIRES = (
    "cc_num",
    "merchant",
    "category",
    "amt",
    "first",
    "last",
    "gender",
    "street",
    "city",
    "state",
    "zip",
    "lat",
    "long",
    "city_pop",
    "job",
    "dob",
    "trans_num",
    "merch_lat",
    "merch_long",
    "current_time",
)
CHAMPS_BRUTS_FACULTATIFS = ("is_fraud",)


def _controle_brut(champ: str, valeur: Any) -> str | None:
    """Code d'erreur pour un champ brut, ou ``None`` s'il est conforme."""
    if champ == "cc_num":
        if not _est_entier(valeur):
            return "type_invalide"
        chiffres = len(str(int(valeur)))
        return None if 8 <= chiffres <= 19 and int(valeur) > 0 else "longueur_invalide"
    if champ in ("merchant", "first", "last", "street", "city", "job"):
        return _texte_non_vide(valeur)
    if champ == "category":
        if not isinstance(valeur, str):
            return "type_invalide"
        return None if _CATEGORIE.match(valeur) else "format_invalide"
    if champ == "amt":
        if not _est_nombre(valeur):
            return "type_invalide"
        return None if 0 < valeur <= MONTANT_MAX else "hors_bornes"
    if champ == "gender":
        return None if valeur in ("M", "F") else "valeur_inconnue"
    if champ == "state":
        if not isinstance(valeur, str):
            return "type_invalide"
        return None if _ETAT.match(valeur) else "format_invalide"
    if champ == "zip":
        if not _est_entier(valeur):
            return "type_invalide"
        return None if 0 < int(valeur) <= 99_999 else "hors_bornes"
    if champ in ("lat", "merch_lat"):
        if not _est_nombre(valeur):
            return "type_invalide"
        return None if -90 <= valeur <= 90 else "hors_bornes"
    if champ in ("long", "merch_long"):
        if not _est_nombre(valeur):
            return "type_invalide"
        return None if -180 <= valeur <= 180 else "hors_bornes"
    if champ == "city_pop":
        if not _est_entier(valeur):
            return "type_invalide"
        return None if int(valeur) >= 0 else "hors_bornes"
    if champ == "dob":
        if not isinstance(valeur, str):
            return "type_invalide"
        jour = _date_iso(valeur)
        if jour is None:
            return "format_invalide"
        return None if date(1900, 1, 1) <= jour <= datetime.now(UTC).date() else "hors_bornes"
    if champ == "trans_num":
        if not isinstance(valeur, str):
            return "type_invalide"
        return None if _HEX32.match(valeur) else "format_invalide"
    if champ == "is_fraud":
        return None if _est_entier(valeur) and int(valeur) in (0, 1) else "valeur_inconnue"
    if champ == "current_time":
        if not _est_entier(valeur):
            return "type_invalide"
        secondes = int(valeur) / 1000
        limite = datetime.now(UTC).timestamp() + DECALAGE_FUTUR_MAX_S
        return None if EPOCH_MIN_S <= secondes <= limite else "hors_bornes"
    return None


# Champs dont la valeur fautive ne doit pas être recopiée dans un message d'erreur.
_CHAMPS_A_NE_PAS_CITER = frozenset(
    {"cc_num", "first", "last", "street", "city", "job", "dob", "zip", "lat", "long", "gender"}
)


def valider_brut(ligne: Mapping[str, Any]) -> list[Erreur]:
    """Contrôle une ligne brute ; liste vide = ligne conforme."""
    erreurs: list[Erreur] = []
    if not isinstance(ligne, Mapping):
        return [Erreur("*", "ligne_non_objet", type(ligne).__name__)]
    for champ in CHAMPS_BRUTS_OBLIGATOIRES:
        if champ not in ligne or ligne[champ] is None:
            erreurs.append(Erreur(champ, "absent"))
            continue
        code = _controle_brut(champ, ligne[champ])
        if code:
            detail = "" if champ in _CHAMPS_A_NE_PAS_CITER else _apercu(ligne[champ])
            erreurs.append(Erreur(champ, code, detail))
    for champ in CHAMPS_BRUTS_FACULTATIFS:
        if ligne.get(champ) is not None:
            code = _controle_brut(champ, ligne[champ])
            if code:
                erreurs.append(Erreur(champ, code, _apercu(ligne[champ])))
    return erreurs


def _apercu(valeur: Any, longueur: int = 40) -> str:
    """Représentation courte et sûre d'une valeur pour un message d'erreur."""
    texte = repr(valeur)
    return texte if len(texte) <= longueur else texte[: longueur - 1] + "…"


# --------------------------------------------------------------------------------------------
# Contrat 2 : message masqué qui circule dans la file
# --------------------------------------------------------------------------------------------
CHAMPS_MASQUES_OBLIGATOIRES = (
    "version_schema",
    "trans_num",
    "horodatage_s",
    "carte_hash",
    "carte_fin",
    "commercant",
    "categorie",
    "montant",
    "etat",
    "population_ville",
    "age",
    "distance_km",
    "lat_commercant",
    "long_commercant",
    "source",
)
CHAMPS_MASQUES_FACULTATIFS = ("etiquette_source",)

_ControleChamp = Callable[[Any], str | None]


# Les contrôles sont des fonctions nommées, appelées au moment de valider : leur corps est exécuté
# à chaque message (et donc éprouvé par les tests, un par un), pas une seule fois à l'import du
# module comme le serait une fabrique de fonctions. Les bornes et les motifs viennent de la table.
def _controle_borne(v: Any, minimum: float, maximum: float) -> str | None:
    if not _est_nombre(v):
        return "type_invalide"
    return None if minimum <= v <= maximum else "hors_bornes"


def _controle_entier_borne(v: Any, minimum: int, maximum: int) -> str | None:
    if not _est_entier(v):
        return "type_invalide"
    return None if minimum <= int(v) <= maximum else "hors_bornes"


def _controle_motif(v: Any, motif: re.Pattern[str]) -> str | None:
    if not isinstance(v, str):
        return "type_invalide"
    return None if motif.match(v) else "format_invalide"


def _controle_version(v: Any) -> str | None:
    return None if v == VERSION_SCHEMA else "version_inconnue"


def _controle_source(v: Any) -> str | None:
    return None if v in ("api", "jeu") else "valeur_inconnue"


def _controle_etiquette(v: Any) -> str | None:
    return None if v in (0, 1) and not isinstance(v, bool) else "valeur_inconnue"


_CONTROLES_MASQUES: dict[str, _ControleChamp] = {
    "version_schema": _controle_version,
    "trans_num": partial(_controle_motif, motif=_HEX32),
    "horodatage_s": partial(_controle_entier_borne, minimum=EPOCH_MIN_S, maximum=4_102_444_800),
    "carte_hash": partial(_controle_motif, motif=re.compile(r"^[0-9a-f]{16,64}$")),
    "carte_fin": partial(_controle_motif, motif=_FINALE_CARTE),
    "commercant": _texte_non_vide,
    "categorie": partial(_controle_motif, motif=_CATEGORIE),
    "montant": partial(_controle_borne, minimum=0.0, maximum=MONTANT_MAX),
    "etat": partial(_controle_motif, motif=_ETAT),
    "population_ville": partial(_controle_entier_borne, minimum=0, maximum=100_000_000),
    "age": partial(_controle_entier_borne, minimum=0, maximum=120),
    # demi-circonférence terrestre
    "distance_km": partial(_controle_borne, minimum=0.0, maximum=20_100.0),
    "lat_commercant": partial(_controle_borne, minimum=-90, maximum=90),
    "long_commercant": partial(_controle_borne, minimum=-180, maximum=180),
    "source": _controle_source,
    "etiquette_source": _controle_etiquette,
}


def valider_masque(message: Mapping[str, Any]) -> list[Erreur]:
    """Contrôle un message masqué ; tout champ inattendu (donc sensible) est refusé."""
    if not isinstance(message, Mapping):
        return [Erreur("*", "message_non_objet", type(message).__name__)]
    erreurs: list[Erreur] = []
    for champ in CHAMPS_MASQUES_OBLIGATOIRES:
        if champ not in message or message[champ] is None:
            erreurs.append(Erreur(champ, "absent"))
            continue
        code = _CONTROLES_MASQUES[champ](message[champ])
        if code:
            erreurs.append(Erreur(champ, code))
    if message.get("etiquette_source") is not None:
        code = _CONTROLES_MASQUES["etiquette_source"](message["etiquette_source"])
        if code:
            erreurs.append(Erreur("etiquette_source", code))
    autorises = set(CHAMPS_MASQUES_OBLIGATOIRES) | set(CHAMPS_MASQUES_FACULTATIFS)
    for champ in message:
        if champ not in autorises:
            # Un champ inconnu est refusé par principe : c'est ainsi qu'un champ sensible qui
            # aurait échappé au masquage est arrêté avant d'atteindre l'entrepôt.
            erreurs.append(Erreur(str(champ), "champ_inattendu"))
    return erreurs
