# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Producteur : interroge l'API temps réel (ou rejoue le jeu), valide, masque, publie.

C'est la frontière entre le monde extérieur (une API publique, lente et parfois en erreur) et le
pipeline interne. Il remplit quatre rôles, dans cet ordre, pour chaque ligne reçue :

1. **Fiabiliser la collecte** (critère 4.3) : appel HTTP avec délai maximal, relances à attente
   exponentielle avec gigue, respect de ``Retry-After``, journal de chaque tentative. Une source
   indisponible ne fait jamais tomber le processus : on journalise, on attend, on recommence.
2. **Valider le schéma** (critère 4.1) : une ligne qui ne respecte pas le contrat va dans la file
   des rejets (DLQ, critère 4.2) avec la liste complète des erreurs ; le flux continue.
3. **Masquer dès l'ingestion** (critère 5.1) : une ligne valide est transformée en message masqué
   *avant* toute publication. La ligne brute n'existe qu'en mémoire, le temps de cette fonction.
4. **Publier** dans la file (Kafka en production, SQLite en local) avec la carte hachée pour clé,
   ce qui conserve l'ordre des transactions d'une même carte.

Format de la réponse de l'API (constaté) : un texte JSON qui contient lui-même un document JSON au
format ``DataFrame.to_json(orient="split")`` (``columns``, ``index``, ``data``).
``decoder_reponse`` accepte aussi bien ce double encodage que le JSON direct.

CHOIX
-----
- Retenu : un producteur Python avec ``requests``, plutôt qu'un connecteur générique : le délai
  maximal et les relances se maîtrisent mieux (ligne « Collecte » de
  livrables/aia3/01_architecture.md:178-188).
- Retenu : relances à attente exponentielle plafonnée avec gigue, sur les codes 408, 425, 429, 500,
  502, 503, 504, sur délai dépassé, erreur réseau et réponse illisible ; ``Retry-After`` est
  respecté mais plafonné.
- Écarté : relancer une erreur définitive (401, 403, 404...) ; elle ne servirait à rien.
- Retenu : un cycle abandonné après la dernière tentative n'arrête pas le processus : le suivant
  reprend à l'intervalle suivant, l'état de santé est écrit à chaque cycle.
- Retenu : une mémoire bornée de 10 000 identifiants récents pour écarter les doublons que l'API
  peut répéter.
- Écarté : recopier la ligne brute dans un rejet ; seule une version masquée y entre.

OÙ
---
- Place : première étape du flux, en amont de la file ; lancé comme service continu.
- Appelants : ligne de commande ``main``, demo (detection_fraude/local.py:188-299) (mode local) et
  les tests.
- Dépendances : ``requests``, ``configuration``, ``file_attente``, ``jeu_donnees``, ``journal``,
  ``masquage`` et ``schema``.
- Aval : le consommateur lit le sujet des messages valides ; l'archiveur lit le sujet des rejets.

COMMENT
-------
1. ``interroger_api`` appelle l'API jusqu'à ``max_tentatives`` fois ; entre deux tentatives
   l'attente est ``duree_attente`` (exponentielle, plafonnée, avec gigue) ou le ``Retry-After``
   de la réponse, jamais plus que le plafond ; aucune attente après la dernière tentative.
2. ``Producteur.traiter_ligne`` valide la ligne brute, écarte un doublon, masque, recontrôle le
   message masqué (contrôle de sortie), puis publie avec ``carte_hash`` pour clé.
3. ``boucle_api`` répète les appels et écrit l'état (``derniere_reussite`` ou
   ``derniere_tentative`` et ``echecs_consecutifs``) ; ``rejouer_jeu`` rejoue le jeu par lots de
   500, avec une cadence optionnelle ; ``lignes_du_jeu`` peut recaler les dates et casser des
   lignes pour la démonstration.
Tests qui le prouvent : test_relance_apres_pannes_reseau_puis_succes
(tests/test_producteur.py:124-133), test_erreur_definitive_non_relancee
(tests/test_producteur.py:170-175), test_ligne_invalide_vers_la_dlq_sans_bloquer_le_flux
(tests/test_producteur.py:212-229), test_la_dlq_ne_contient_aucune_donnee_identifiante
(tests/test_producteur.py:232-238), test_boucle_api_survit_a_une_source_indisponible
(tests/test_producteur.py:319-336),
test_chaque_corruption_est_refusee_par_le_schema_avec_son_code_et_le_rang_tourne
(tests/test_producteur_refus.py:750-758) et
test_l_aide_decrit_chaque_option_et_sort_avec_le_code_0_sans_rien_demarrer
(tests/test_producteur_cli.py:586-615).

POURQUOI
--------
- Politique de relance et scénarios de panne mesurés :
  livrables/aia3/02_pipeline_et_qualite.md:271-287 et livrables/aia3/01_architecture.md:190-199.
- Isolation des lignes corrompues, sept corruptions de démonstration :
  livrables/aia3/02_pipeline_et_qualite.md:232-269.
- Volumes et débit de la collecte : livrables/aia3/01_architecture.md:152-159.
- Constat sur l'API réelle (un 502 sur sept appels, réponses de 5,8 à 11 s) :
  livrables/aia3/01_architecture.md:152-159 ; la mesure est de ce projet (relevé brut non publié).
- Codes relançables : CODES_RELANCABLES (detection_fraude/producteur.py:131).
- Indicateurs 4.1, 4.2 et 4.3 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.producteur``) :
- ``api_appel_reussi`` (INFO) : ``tentative``, ``lignes``, ``duree_ms`` ;
- ``api_relance`` (WARNING) : ``tentative``, ``sur``, ``cause``, ``attente_s`` ;
- ``api_erreur_definitive`` (ERROR) : ``statut``, ``url`` ; ``api_indisponible`` (ERROR) :
  ``tentatives``, ``cause`` ;
- ``ligne_rejetee`` (WARNING) : ``raison``, ``trans_num``, ``champs``, ``codes`` ;
- ``corruption_injectee_demo`` et ``rebase_change_le_jour_de_semaine`` (WARNING) ;
- ``cycle_termine`` (INFO), ``cycle_abandonne`` (ERROR), ``rejeu_termine`` (INFO),
  ``producteur_demarre`` / ``producteur_arrete`` (INFO), ``configuration_invalide`` (ERROR).
Aucune valeur de champ sensible n'est journalisée.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from detection_fraude.configuration import (
    Configuration,
    ErreurConfiguration,
    Relances,
    charger_configuration,
)
from detection_fraude.file_attente import FileMessages, construire_file
from detection_fraude.jeu_donnees import lignes_depuis_jeu, lire_jeu_brut
from detection_fraude.journal import (
    avertissement,
    configurer_journal,
    erreur,
    info,
    obtenir_journal,
)
from detection_fraude.masquage import HacheurCarte, masquer, masquer_pour_rejet
from detection_fraude.schema import VERSION_SCHEMA, valider_brut, valider_masque

_journal = obtenir_journal("producteur")

CODES_RELANCABLES = frozenset({408, 425, 429, 500, 502, 503, 504})
TAILLE_MEMOIRE_DOUBLONS = 10_000


class SourceIndisponible(Exception):
    """Toutes les tentatives ont échoué ; le cycle est abandonné, pas le processus."""


class ReponseIllisible(Exception):
    """La réponse HTTP est arrivée mais n'a pas la forme attendue."""


# --------------------------------------------------------------------------------------------
# Décodage de la réponse de l'API
# --------------------------------------------------------------------------------------------
def decoder_reponse(corps: str | bytes) -> list[dict[str, Any]]:
    """Transforme la réponse de ``/current-transactions`` en liste de lignes (dictionnaires)."""
    try:
        document: Any = json.loads(corps)
        if isinstance(document, str):  # double encodage constaté sur l'API
            document = json.loads(document)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReponseIllisible("la réponse n'est pas un JSON valide") from exc
    if isinstance(document, list):
        return [ligne for ligne in document if isinstance(ligne, dict)] or _vide_ou_erreur(
            document
        )
    if not isinstance(document, dict) or "columns" not in document or "data" not in document:
        raise ReponseIllisible("structure inattendue (attendu : columns et data)")
    colonnes = document["columns"]
    if not isinstance(colonnes, list) or not isinstance(document["data"], list):
        raise ReponseIllisible("columns et data doivent être des listes")
    lignes = []
    for valeurs in document["data"]:
        if not isinstance(valeurs, list) or len(valeurs) != len(colonnes):
            raise ReponseIllisible("une ligne de data n'a pas autant de valeurs que de colonnes")
        lignes.append(dict(zip(colonnes, valeurs, strict=True)))
    return lignes


def _vide_ou_erreur(document: list[Any]) -> list[dict[str, Any]]:
    if document:
        raise ReponseIllisible("liste sans objet exploitable")
    return []


# --------------------------------------------------------------------------------------------
# Appel HTTP avec relances
# --------------------------------------------------------------------------------------------
def duree_attente(tentative: int, politique: Relances, aleatoire: Callable[[], float]) -> float:
    """Attente avant la relance n° ``tentative`` : exponentielle, plafonnée, avec gigue."""
    base = min(
        politique.attente_initiale_s * politique.facteur ** (tentative - 1),
        politique.attente_max_s,
    )
    return base * (1 + politique.gigue * aleatoire())


def _retry_after(reponse: Any) -> float | None:
    valeur = getattr(reponse, "headers", {}).get("Retry-After")
    try:
        return max(0.0, float(valeur)) if valeur is not None else None
    except (TypeError, ValueError):
        return None


def interroger_api(
    url: str,
    politique: Relances,
    delai_attente_s: float,
    *,
    get: Callable[..., Any] = requests.get,
    dormir: Callable[[float], None] = time.sleep,
    aleatoire: Callable[[], float] = random.random,
) -> list[dict[str, Any]]:
    """Un appel à l'API avec relances ; lève ``SourceIndisponible`` après la dernière tentative."""
    derniere_cause = "inconnue"
    for tentative in range(1, politique.max_tentatives + 1):
        pause_imposee: float | None = None
        debut = time.monotonic()
        try:
            reponse = get(url, timeout=delai_attente_s)
            if reponse.status_code in CODES_RELANCABLES:
                derniere_cause = f"HTTP {reponse.status_code}"
                pause_imposee = _retry_after(reponse)
                raise requests.HTTPError(derniere_cause)
            if reponse.status_code >= 400:
                # Erreur « définitive » (401, 403, 404…) : relancer ne servirait à rien.
                erreur(_journal, "api_erreur_definitive", statut=reponse.status_code, url=url)
                raise SourceIndisponible(f"HTTP {reponse.status_code} (non relançable)")
            lignes = decoder_reponse(reponse.content)
            info(
                _journal,
                "api_appel_reussi",
                tentative=tentative,
                lignes=len(lignes),
                duree_ms=int((time.monotonic() - debut) * 1000),
            )
            return lignes
        except SourceIndisponible:
            raise
        except (requests.RequestException, ReponseIllisible) as exc:
            if not derniere_cause.startswith("HTTP"):
                derniere_cause = type(exc).__name__
            if tentative == politique.max_tentatives:
                break
            attente = (
                pause_imposee
                if pause_imposee is not None
                else duree_attente(tentative, politique, aleatoire)
            )
            attente = min(attente, politique.attente_max_s)
            avertissement(
                _journal,
                "api_relance",
                tentative=tentative,
                sur=politique.max_tentatives,
                cause=derniere_cause,
                attente_s=round(attente, 2),
            )
            dormir(attente)
    erreur(_journal, "api_indisponible", tentatives=politique.max_tentatives, cause=derniere_cause)
    raise SourceIndisponible(f"{politique.max_tentatives} tentatives échouées ({derniere_cause})")


class PannesSimulees:
    """Enveloppe de démonstration : fait échouer les N premiers appels (panne réseau simulée)."""

    def __init__(self, get: Callable[..., Any], nb_echecs: int) -> None:
        self._get = get
        self._restantes = nb_echecs

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._restantes > 0:
            self._restantes -= 1
            raise requests.ConnectionError("panne réseau simulée pour la démonstration")
        return self._get(*args, **kwargs)


# --------------------------------------------------------------------------------------------
# Sources de lignes
# --------------------------------------------------------------------------------------------
_CORRUPTIONS = (
    ("amt", "abc"),  # type invalide
    ("trans_num", None),  # champ obligatoire absent
    ("gender", "X"),  # valeur hors liste
    ("dob", "31/02/1990"),  # date impossible
    ("lat", 123.4),  # hors bornes
    ("cc_num", "carte-invalide"),  # type invalide
    ("amt", -12.5),  # montant négatif
)


def corrompre(ligne: dict[str, Any], rang: int) -> dict[str, Any]:
    """Casse volontairement une ligne (démonstration de la file des rejets, tests)."""
    champ, valeur = _CORRUPTIONS[rang % len(_CORRUPTIONS)]
    abimee = dict(ligne)
    if valeur is None:
        abimee.pop(champ, None)
    else:
        abimee[champ] = valeur
    return abimee


def lignes_du_jeu(
    chemin: Path,
    *,
    jour: str | None,
    limite: int | None,
    corruptions: int = 0,
    graine: int = 7,
    rebaser_sur: str | None = None,
) -> Iterable[dict[str, Any]]:
    """Rejoue le jeu dans l'ordre chronologique, avec ``corruptions`` lignes cassées exprès.

    ``rebaser_sur`` (démonstration) décale toutes les dates d'un nombre entier de jours pour que
    le ``jour`` rejoué tombe à la date donnée (par exemple hier) en gardant l'heure de la journée :
    le DAG du rapport du matin, qui lit « la veille », a alors des données à traiter. La date de
    naissance est décalée du même nombre de jours pour que l'âge ne change pas. Le jour de la
    semaine, lui, change sauf si l'écart est un multiple de 7 jours (un avertissement le
    signale) : les scores d'un rejeu recalé ne sont représentatifs que dans ce cas.
    """
    decalage_ms = 0
    if rebaser_sur:
        if not jour:
            raise ValueError("rebaser_sur exige de préciser le jour rejoué (--jour).")
        ecart = date.fromisoformat(rebaser_sur) - date.fromisoformat(jour)
        decalage_ms = ecart.days * 86_400_000
        if ecart.days % 7:
            avertissement(
                _journal,
                "rebase_change_le_jour_de_semaine",
                jour=jour,
                rebaser_sur=rebaser_sur,
                consequence="scores non représentatifs (jour_semaine est une variable)",
            )
    df = lire_jeu_brut(chemin, jour=jour, limite=limite)
    lignes = list(lignes_depuis_jeu(df))
    for ligne in lignes:
        ligne["current_time"] += decalage_ms
        if decalage_ms:  # l'âge est conservé : la date de naissance suit la même translation
            naissance = date.fromisoformat(ligne["dob"]) + timedelta(days=ecart.days)
            ligne["dob"] = naissance.isoformat()
    positions = set(
        random.Random(graine).sample(range(len(lignes)), min(corruptions, len(lignes)))
    )
    for rang, ligne in enumerate(lignes):
        if rang in positions:
            avertissement(
                _journal, "corruption_injectee_demo", rang=rang, trans_num=ligne["trans_num"]
            )
            yield corrompre(ligne, rang)
        else:
            yield ligne


# --------------------------------------------------------------------------------------------
# Producteur
# --------------------------------------------------------------------------------------------
@dataclass
class BilanProduction:
    lues: int = 0
    publiees: int = 0
    rejetees: int = 0
    doublons: int = 0

    def ajouter(self, autre: BilanProduction) -> None:
        self.lues += autre.lues
        self.publiees += autre.publiees
        self.rejetees += autre.rejetees
        self.doublons += autre.doublons


def _trans_num_sur(ligne: Any) -> str | None:
    """Identifiant de transaction utilisable dans un rejet, seulement s'il est bien formé."""
    if isinstance(ligne, dict):
        valeur = ligne.get("trans_num")
        if (
            isinstance(valeur, str)
            and len(valeur) == 32
            and all(c in "0123456789abcdef" for c in valeur)
        ):
            return valeur
    return None


class Producteur:
    """Valide, masque et publie des lignes brutes ; envoie les invalides vers la DLQ."""

    def __init__(
        self,
        config: Configuration,
        file: FileMessages,
        hacheur: HacheurCarte,
        *,
        source: str,
    ) -> None:
        self._config = config
        self._file = file
        self._hacheur = hacheur
        self._source = source
        self._vus: OrderedDict[str, None] = OrderedDict()

    def _deja_vu(self, trans_num: str) -> bool:
        """Mémoire bornée des identifiants récents (l'API peut répéter une ligne)."""
        if trans_num in self._vus:
            return True
        self._vus[trans_num] = None
        if len(self._vus) > TAILLE_MEMOIRE_DOUBLONS:
            self._vus.popitem(last=False)
        return False

    def _vers_rejets(self, ligne: Any, raison: str, erreurs: list[dict[str, str]]) -> None:
        trans_num = _trans_num_sur(ligne)
        rejet = {
            "version_schema": VERSION_SCHEMA,
            "etape": "producteur",
            "raison": raison,
            "erreurs": erreurs,
            "trans_num": trans_num,
            "source": self._source,
            "rejete_le": datetime.now(UTC).isoformat(timespec="seconds"),
            "charge_utile_masquee": masquer_pour_rejet(ligne),  # jamais de donnée brute
        }
        self._file.publier(self._config.file.sujet_rejets, rejet, cle=trans_num)
        avertissement(
            _journal,
            "ligne_rejetee",
            raison=raison,
            trans_num=trans_num,
            champs=",".join(sorted({e["champ"] for e in erreurs})),
            codes=",".join(sorted({e["code"] for e in erreurs})),
        )

    def traiter_ligne(self, ligne: Any) -> str:
        """Renvoie ``"publiee"``, ``"rejetee"`` ou ``"doublon"``."""
        erreurs = valider_brut(ligne)
        if erreurs:
            self._vers_rejets(ligne, "schema_invalide", [e.en_dict() for e in erreurs])
            return "rejetee"
        if self._deja_vu(ligne["trans_num"]):
            return "doublon"
        message = masquer(ligne, self._hacheur, source=self._source)
        # Contrôle de sortie : le message masqué doit respecter le contrat de la file.
        erreurs_sortie = valider_masque(message)
        if erreurs_sortie:
            self._vers_rejets(ligne, "masquage_invalide", [e.en_dict() for e in erreurs_sortie])
            return "rejetee"
        self._file.publier(self._config.file.sujet_valides, message, cle=message["carte_hash"])
        return "publiee"

    def traiter_lignes(self, lignes: Iterable[Any]) -> BilanProduction:
        bilan = BilanProduction()
        for ligne in lignes:
            bilan.lues += 1
            resultat = self.traiter_ligne(ligne)
            if resultat == "publiee":
                bilan.publiees += 1
            elif resultat == "rejetee":
                bilan.rejetees += 1
            else:
                bilan.doublons += 1
        self._file.vider()  # accusé de réception du courtier avant de clore le lot
        return bilan


# --------------------------------------------------------------------------------------------
# État de santé (lu par la sonde de supervision et le contrôle de santé Docker)
# --------------------------------------------------------------------------------------------
def ecrire_etat(config: Configuration, etat: dict[str, Any]) -> None:
    chemin = config.racine_donnees / "etat" / "producteur.json"
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(etat, ensure_ascii=False), encoding="utf-8")


def boucle_api(
    config: Configuration,
    producteur: Producteur,
    arreter: threading.Event,
    *,
    nombre_appels: int | None = None,
    intervalle_s: float | None = None,
    get: Callable[..., Any] = requests.get,
    dormir: Callable[[float], None] | None = None,
) -> BilanProduction:
    """Interroge l'API à intervalle régulier jusqu'à arrêt (ou après ``nombre_appels``)."""
    total = BilanProduction()
    echecs_consecutifs = 0
    appels = 0
    pause = intervalle_s if intervalle_s is not None else config.source.intervalle_s

    def attendre_ou_arret(secondes: float) -> None:
        arreter.wait(secondes)

    attendre = dormir or attendre_ou_arret
    while not arreter.is_set() and (nombre_appels is None or appels < nombre_appels):
        appels += 1
        try:
            lignes = interroger_api(
                config.source.api_url,
                config.source.relances,
                config.source.delai_attente_s,
                get=get,
                dormir=attendre,
            )
            bilan = producteur.traiter_lignes(lignes)
            total.ajouter(bilan)
            echecs_consecutifs = 0
            info(_journal, "cycle_termine", **bilan.__dict__)
            ecrire_etat(
                config,
                {
                    "derniere_reussite": datetime.now(UTC).isoformat(),
                    "echecs_consecutifs": 0,
                    **total.__dict__,
                },
            )
        except SourceIndisponible as exc:
            echecs_consecutifs += 1
            erreur(
                _journal, "cycle_abandonne", cause=str(exc), echecs_consecutifs=echecs_consecutifs
            )
            ecrire_etat(
                config,
                {
                    "derniere_tentative": datetime.now(UTC).isoformat(),
                    "echecs_consecutifs": echecs_consecutifs,
                    **total.__dict__,
                },
            )
        if nombre_appels is not None and appels >= nombre_appels:
            break
        attendre(pause)
    return total


def rejouer_jeu(
    config: Configuration,
    producteur: Producteur,
    *,
    jour: str | None,
    limite: int | None,
    corruptions: int,
    graine: int,
    cadence: float,
    arreter: threading.Event,
    rebaser_sur: str | None = None,
) -> BilanProduction:
    """Rejoue le jeu ; ``cadence`` en lignes par seconde (0 = aussi vite que possible)."""
    total = BilanProduction()
    lot: list[dict[str, Any]] = []
    pas = 1.0 / cadence if cadence > 0 else 0.0
    for ligne in lignes_du_jeu(
        config.source.jeu_chemin,
        jour=jour,
        limite=limite,
        corruptions=corruptions,
        graine=graine,
        rebaser_sur=rebaser_sur,
    ):
        if arreter.is_set():
            break
        lot.append(ligne)
        if pas:
            total.ajouter(producteur.traiter_lignes(lot))
            lot = []
            time.sleep(pas)
        elif len(lot) >= 500:
            total.ajouter(producteur.traiter_lignes(lot))
            lot = []
    if lot:
        total.ajouter(producteur.traiter_lignes(lot))
    info(_journal, "rejeu_termine", **total.__dict__)
    return total


# --------------------------------------------------------------------------------------------
# Ligne de commande
# --------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Producteur : API ou rejeu du jeu -> validation -> masquage -> file."
    )
    analyseur.add_argument("--source", choices=("api", "jeu"), help="défaut : configuration")
    analyseur.add_argument(
        "--nombre", type=int, help="API : nombre d'appels ; jeu : nombre de lignes"
    )
    analyseur.add_argument("--intervalle", type=float, help="API : secondes entre deux appels")
    analyseur.add_argument("--jour", help="jeu : ne rejouer que ce jour (AAAA-MM-JJ)")
    analyseur.add_argument("--cadence", type=float, default=0.0, help="jeu : lignes par seconde")
    analyseur.add_argument(
        "--corruptions", type=int, default=0, help="jeu : lignes à casser (démo)"
    )
    analyseur.add_argument("--pannes", type=int, default=0, help="API : pannes simulées (démo)")
    analyseur.add_argument(
        "--rebaser-sur", help="jeu : décale les dates pour que --jour tombe à cette date (démo)"
    )
    analyseur.add_argument("--graine", type=int, default=7)
    arguments = analyseur.parse_args(argv)

    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    source = arguments.source or config.source.type
    arreter = threading.Event()
    for signal_arret in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_arret, lambda *_: arreter.set())

    try:
        hacheur = HacheurCarte(config.sel_hachage(), config.longueur_hash)
    except ErreurConfiguration as exc:
        erreur(_journal, "configuration_invalide", cause=str(exc))
        return 2
    file = construire_file(config)
    producteur = Producteur(config, file, hacheur, source=source)
    info(
        _journal,
        "producteur_demarre",
        source=source,
        environnement=config.environnement,
        file=config.file.type,
    )
    try:
        if source == "api":
            get = (
                PannesSimulees(requests.get, arguments.pannes)
                if arguments.pannes
                else requests.get
            )
            boucle_api(
                config,
                producteur,
                arreter,
                nombre_appels=arguments.nombre,
                intervalle_s=arguments.intervalle,
                get=get,
            )
        else:
            rejouer_jeu(
                config,
                producteur,
                jour=arguments.jour,
                limite=arguments.nombre,
                corruptions=arguments.corruptions,
                graine=arguments.graine,
                cadence=arguments.cadence,
                arreter=arreter,
                rebaser_sur=arguments.rebaser_sur,
            )
    finally:
        file.fermer()
        info(_journal, "producteur_arrete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
