# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Producteur : ce qui est refusé, relancé ou abandonné, et la preuve que rien n'est abîmé.

Chaque test inverse fixe une règle de refus avec ses messages exacts, ses événements d'audit
complets (niveau, nom, champs) et l'état de la file avant/après : une entrée interdite ne doit
rien publier dans le sujet des valides et ne doit jamais laisser fuiter de donnée brute.
"""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
import requests

import detection_fraude.producteur as producteur_module
from detection_fraude.configuration import Relances
from detection_fraude.jeu_donnees import COLONNES_API
from detection_fraude.masquage import masquer, masquer_pour_rejet
from detection_fraude.producteur import (
    BilanProduction,
    PannesSimulees,
    Producteur,
    ReponseIllisible,
    SourceIndisponible,
    boucle_api,
    corrompre,
    decoder_reponse,
    duree_attente,
    interroger_api,
    lignes_du_jeu,
)
from detection_fraude.schema import Erreur, valider_brut
from tests.helpers import MARQUEUR, ligne_brute

URL = "https://api.test/tx"
POLITIQUE = Relances(
    max_tentatives=4, attente_initiale_s=1.0, facteur=2.0, attente_max_s=30.0, gigue=0.0
)
MESSAGE_JSON_INVALIDE = "la réponse n'est pas un JSON valide"
MESSAGE_STRUCTURE = "structure inattendue (attendu : columns et data)"
MESSAGE_LISTES = "columns et data doivent être des listes"
MESSAGE_LONGUEUR = "une ligne de data n'a pas autant de valeurs que de colonnes"
MESSAGE_SANS_OBJET = "liste sans objet exploitable"
MESSAGE_PANNE = "panne réseau simulée pour la démonstration"
UN_JOUR_MS = 86_400_000

DOCUMENT = {
    "columns": ["cc_num", "amt", "trans_num"],
    "index": [7],
    "data": [[4929000000000001, 37.5, "c7e1d0a94f3b42e8a65d1f0b9e2c7a14"]],
}
LIGNE_DU_DOCUMENT = {
    "cc_num": 4929000000000001,
    "amt": 37.5,
    "trans_num": "c7e1d0a94f3b42e8a65d1f0b9e2c7a14",
}


# --------------------------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------------------------
class Reponse:
    """Réponse HTTP minimale : statut, corps et en-têtes."""

    def __init__(self, statut: int = 200, corps: str | bytes = b"", entetes: Any = None) -> None:
        self.status_code = statut
        self.content = corps if isinstance(corps, bytes) else corps.encode()
        self.headers = entetes if entetes is not None else {}


class ReponseSansEntetes:
    """Réponse dont l'objet n'a même pas d'attribut ``headers``."""

    def __init__(self, statut: int) -> None:
        self.status_code = statut
        self.content = b""


def reponse_api(lignes: list[dict[str, Any]]) -> Reponse:
    colonnes = list(lignes[0])
    document = {
        "columns": colonnes,
        "index": list(range(len(lignes))),
        "data": [[ligne[c] for c in colonnes] for ligne in lignes],
    }
    return Reponse(200, json.dumps(json.dumps(document)))


CORPS_VALIDE = json.dumps([{"a": 1}])


class GetScripte:
    """Renvoie (ou lève) les éléments d'un scénario et garde chaque appel reçu."""

    def __init__(self, *scenario: Any) -> None:
        self.scenario = list(scenario)
        self.appels: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.appels.append((args, kwargs))
        if not self.scenario:
            raise AssertionError("appel HTTP imprévu : le scénario est épuisé")
        element = self.scenario.pop(0)
        if isinstance(element, Exception):
            raise element
        return element


class HorlogeFausse:
    """Remplace le module ``time`` du producteur : l'horloge avance d'un pas à chaque lecture."""

    def __init__(self, pas: float = 0.0) -> None:
        self.maintenant = 0.0
        self.pas = pas
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        self.maintenant += self.pas
        return self.maintenant

    def sleep(self, secondes: float) -> None:
        self.pauses.append(secondes)


class FileEspion:
    """File en mémoire : garde ce qui est publié, compte les accusés de réception."""

    def __init__(self) -> None:
        self.publies: list[tuple[str, str | None, dict[str, Any]]] = []
        self.vidages = 0
        self.echec_publication: Exception | None = None

    def publier(self, sujet: str, valeur: dict[str, Any], cle: str | None = None) -> None:
        if self.echec_publication is not None:
            raise self.echec_publication
        self.publies.append((sujet, cle, valeur))

    def vider(self) -> None:
        self.vidages += 1

    def fermer(self) -> None:
        pass


@contextmanager
def chien_de_garde(arret: threading.Event, delai: float = 1.0):
    """Lève l'arrêt après ``delai`` s si la boucle ne s'est pas arrêtée d'elle-même."""
    minuterie = threading.Timer(delai, arret.set)
    minuterie.daemon = True
    minuterie.start()
    try:
        yield
    finally:
        minuterie.cancel()
    assert not arret.is_set(), "la boucle ne s'est pas arrêtée d'elle-même"


def figer_horloge(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Fige ``datetime.now`` du producteur ; renvoie la liste des fuseaux demandés."""
    fuseaux: list[Any] = []

    class DatetimeFixe:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            fuseaux.append(tz)
            return datetime(2026, 10, 10, 8, 30, 15, 123456, tzinfo=tz)

    monkeypatch.setattr(producteur_module, "datetime", DatetimeFixe)
    return fuseaux


def interroger(get: Any, pauses: list[float], politique: Relances = POLITIQUE, delai: float = 5.0):
    return interroger_api(
        URL, politique, delai, get=get, dormir=pauses.append, aleatoire=lambda: 0.0
    )


def relance(tentative: int, sur: int, cause: str, attente_s: float):
    return (
        "WARNING",
        "api_relance",
        {"tentative": tentative, "sur": sur, "cause": cause, "attente_s": attente_s},
    )


def indisponible(tentatives: int, cause: str):
    return ("ERROR", "api_indisponible", {"tentatives": tentatives, "cause": cause})


def date_apres(naissance: str, jours: int) -> str:
    return (date.fromisoformat(naissance) + timedelta(days=jours)).isoformat()


def ecrire_jeu(chemin, dates: list[str], surcharges: dict[int, dict[str, Any]] | None = None):
    """Écrit un petit jeu au format de ``fraudTest.csv`` : une ligne valide par date."""
    enregistrements = []
    for rang, date_heure in enumerate(dates):
        ligne = ligne_brute(rang, **(surcharges or {}).get(rang, {}))
        enregistrement: dict[str, Any] = {"trans_date_trans_time": date_heure}
        enregistrement.update({colonne: ligne[colonne] for colonne in COLONNES_API})
        enregistrements.append(enregistrement)
    pd.DataFrame(enregistrements).to_csv(chemin, index=False)
    return chemin


# --------------------------------------------------------------------------------------------
# Décodage de la réponse : chaque forme refusée, avec son message exact
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "corps", ["pas du json", "", "{", '{"columns": ["a"]', "<html>502</html>"]
)
def test_une_reponse_qui_n_est_pas_du_json_est_refusee(corps):
    """Une page d'erreur HTML ou un corps tronqué ne doit jamais être pris pour des données."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(corps)
    assert str(exc.value) == MESSAGE_JSON_INVALIDE


def test_des_octets_qui_ne_sont_pas_de_l_utf8_sont_refuses():
    """Un corps binaire (UnicodeDecodeError) est une réponse illisible, pas un plantage."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(b"\xff\xff\xff")
    assert str(exc.value) == MESSAGE_JSON_INVALIDE
    assert isinstance(exc.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize("interieur", ["pas du json", "", "{"])
def test_un_double_encodage_dont_l_interieur_est_illisible_est_refuse(interieur):
    """Le second décodage (chaîne JSON dans une chaîne JSON) est contrôlé comme le premier."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps(interieur))
    assert str(exc.value) == MESSAGE_JSON_INVALIDE
    assert isinstance(exc.value.__cause__, json.JSONDecodeError)


def test_un_document_sans_data_est_refuse():
    """Sans ``data`` il n'y a aucune ligne à traiter : on refuse plutôt que d'inventer."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps({"columns": ["a"], "index": []}))
    assert str(exc.value) == MESSAGE_STRUCTURE


def test_un_document_sans_columns_est_refuse():
    """Sans ``columns``, les valeurs de ``data`` ne peuvent pas être nommées."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps({"data": [[1]], "index": [0]}))
    assert str(exc.value) == MESSAGE_STRUCTURE


@pytest.mark.parametrize("document", [{}, {"index": [1]}, {"Columns": ["a"], "Data": []}])
def test_un_objet_sans_aucune_des_deux_cles_est_refuse(document):
    """Un objet JSON quelconque n'est pas un document ``orient=split``."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps(document))
    assert str(exc.value) == MESSAGE_STRUCTURE


@pytest.mark.parametrize(
    "corps",
    [
        json.dumps(42),
        json.dumps(3.5),
        "null",
        "true",
        json.dumps(json.dumps(json.dumps(DOCUMENT))),
        json.dumps(json.dumps(42)),
    ],
)
def test_un_document_qui_n_est_ni_objet_ni_liste_est_refuse(corps):
    """Un scalaire, ``null`` ou un triple encodage ne sont pas une réponse de l'API."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(corps)
    assert str(exc.value) == MESSAGE_STRUCTURE


@pytest.mark.parametrize("colonnes", ["a", {"a": 1}, None, 5])
def test_des_colonnes_qui_ne_sont_pas_une_liste_sont_refusees(colonnes):
    """``columns`` doit être une liste de noms."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps({"columns": colonnes, "data": []}))
    assert str(exc.value) == MESSAGE_LISTES


@pytest.mark.parametrize("donnees", [{}, "x", None, 0])
def test_des_donnees_qui_ne_sont_pas_une_liste_sont_refusees(donnees):
    """``data`` doit être une liste de lignes, même si ``columns`` est correct."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps({"columns": ["a"], "data": donnees}))
    assert str(exc.value) == MESSAGE_LISTES


def test_une_ligne_de_data_trop_courte_est_refusee():
    """Une valeur manquante décalerait les colonnes : la réponse entière est refusée."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps({"columns": ["a", "b"], "data": [[1]]}))
    assert str(exc.value) == MESSAGE_LONGUEUR


def test_une_ligne_de_data_trop_longue_est_refusee():
    """Une valeur en trop est aussi suspecte qu'une valeur manquante."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps({"columns": ["a"], "data": [[1, 2]]}))
    assert str(exc.value) == MESSAGE_LONGUEUR


@pytest.mark.parametrize("ligne", ["ab", {"a": 1, "b": 2}, 7, None])
def test_une_ligne_de_data_qui_n_est_pas_une_liste_est_refusee(ligne):
    """Même de la bonne longueur, une ligne qui n'est pas une liste est refusée."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps({"columns": ["a", "b"], "data": [ligne]}))
    assert str(exc.value) == MESSAGE_LONGUEUR


def test_une_seule_ligne_mauvaise_fait_refuser_toute_la_reponse():
    """Aucun résultat partiel : les bonnes lignes qui précèdent ne sont pas renvoyées."""
    corps = json.dumps({"columns": ["a", "b"], "data": [[1, 2], [3, 4], [5]]})
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(corps)
    assert str(exc.value) == MESSAGE_LONGUEUR


@pytest.mark.parametrize("liste", [[1, 2], ["a"], [None], [[{"a": 1}]], [1, "x", None, True]])
def test_une_liste_sans_aucun_objet_est_refusee(liste):
    """Une liste non vide qui ne contient aucun dictionnaire n'a rien d'exploitable."""
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(json.dumps(liste))
    assert str(exc.value) == MESSAGE_SANS_OBJET


@pytest.mark.parametrize(
    ("valide", "attendu", "casse", "message"),
    [
        (
            json.dumps(json.dumps(DOCUMENT)),
            [LIGNE_DU_DOCUMENT],
            json.dumps(json.dumps(DOCUMENT))[:-3],
            MESSAGE_JSON_INVALIDE,
        ),
        (
            json.dumps(DOCUMENT),
            [LIGNE_DU_DOCUMENT],
            json.dumps({"columns": DOCUMENT["columns"]}),
            MESSAGE_STRUCTURE,
        ),
        (
            json.dumps(DOCUMENT).encode("utf-8"),
            [LIGNE_DU_DOCUMENT],
            b"\xff\xff\xff",
            MESSAGE_JSON_INVALIDE,
        ),
        (
            json.dumps(json.dumps([{"a": 1}])),
            [{"a": 1}],
            json.dumps(json.dumps([1])),
            MESSAGE_SANS_OBJET,
        ),
        ("[]", [], "[1]", MESSAGE_SANS_OBJET),
        (b"[]", [], b"[null]", MESSAGE_SANS_OBJET),
        (
            json.dumps({"columns": ["a"], "data": []}),
            [],
            json.dumps({"columns": ["a"], "data": {}}),
            MESSAGE_LISTES,
        ),
        (
            json.dumps([{"a": 1}, 3, "x", None, [1], {"b": 2}]),
            [{"a": 1}, {"b": 2}],
            json.dumps([3, "x", None, [1]]),
            MESSAGE_SANS_OBJET,
        ),
        (
            json.dumps({"columns": ["a", "b"], "data": [[1, 2], [3, 4]]}),
            [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
            json.dumps({"columns": ["a", "b"], "data": [[1, 2], [3]]}),
            MESSAGE_LONGUEUR,
        ),
    ],
)
def test_decodage_accepte_chaque_forme_valide_et_refuse_sa_variante_cassee(
    valide, attendu, casse, message
):
    """Double encodage, JSON direct, octets, liste mixte : valides ; variante cassée refusée."""
    assert decoder_reponse(valide) == attendu
    with pytest.raises(ReponseIllisible) as exc:
        decoder_reponse(casse)
    assert str(exc.value) == message


# --------------------------------------------------------------------------------------------
# Formule de l'attente
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("politique", "aleatoire", "attendus"),
    [
        (POLITIQUE, 0.0, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]),
        (POLITIQUE, 1.0, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]),
        (
            Relances(attente_initiale_s=3.0, facteur=3.0, attente_max_s=100.0, gigue=0.5),
            0.5,
            [3.75, 11.25, 33.75, 101.25, 125.0],
        ),
        (
            Relances(attente_initiale_s=10.0, facteur=2.0, attente_max_s=60.0, gigue=0.1),
            1.0,
            [11.0],
        ),
        (
            Relances(attente_initiale_s=2.0, facteur=1.5, attente_max_s=60.0, gigue=0.0),
            0.7,
            [2.0, 3.0, 4.5],
        ),
    ],
)
def test_la_duree_d_attente_suit_la_formule_exponentielle_plafonnee_avec_gigue(
    politique, aleatoire, attendus
):
    """Base = initiale x facteur^(n-1), plafonnée, puis majorée de la gigue (plafond d'abord)."""
    obtenus = [duree_attente(n, politique, lambda: aleatoire) for n in range(1, len(attendus) + 1)]
    assert obtenus == pytest.approx(attendus)


def test_un_appel_reussi_est_journalise_en_detail_et_une_panne_de_plus_serait_un_abandon(
    monkeypatch, evenements
):
    """Succès à la 1re ou dernière tentative : détail journalisé ; un échec de plus abandonne."""
    horloge = HorlogeFausse(pas=2.0)
    monkeypatch.setattr(producteur_module, "time", horloge)
    get = GetScripte(reponse_api([ligne_brute(1), ligne_brute(2)]))
    pauses: list[float] = []
    assert interroger(get, pauses, delai=4.5) == [ligne_brute(1), ligne_brute(2)]
    assert get.appels == [((URL,), {"timeout": 4.5})]
    assert pauses == []
    assert evenements.tous() == [
        ("INFO", "api_appel_reussi", {"tentative": 1, "lignes": 2, "duree_ms": 2000})
    ]

    evenements.enregistrements.clear()
    get = GetScripte(
        requests.ConnectionError("a"),
        requests.Timeout("b"),
        requests.ConnectionError("c"),
        reponse_api([ligne_brute(3)]),
    )
    pauses = []
    assert interroger(get, pauses) == [ligne_brute(3)]
    assert pauses == [1.0, 2.0, 4.0]
    assert evenements.tous() == [
        relance(1, 4, "ConnectionError", 1.0),
        relance(2, 4, "Timeout", 2.0),
        relance(3, 4, "ConnectionError", 4.0),
        ("INFO", "api_appel_reussi", {"tentative": 4, "lignes": 1, "duree_ms": 2000}),
    ]

    perdu = GetScripte(*[requests.ConnectionError("d")] * 4)
    with pytest.raises(SourceIndisponible) as exc:
        interroger(perdu, [])
    assert str(exc.value) == "4 tentatives échouées (ConnectionError)"


def test_le_statut_399_est_une_reponse_et_le_statut_400_une_erreur_definitive(evenements):
    """Frontière des erreurs HTTP à 400 : en dessous on lit le corps, au-delà on abandonne."""
    assert interroger(GetScripte(Reponse(399, CORPS_VALIDE)), []) == [{"a": 1}]
    assert evenements.nommes("api_erreur_definitive") == []
    with pytest.raises(SourceIndisponible) as exc:
        interroger(GetScripte(Reponse(400, CORPS_VALIDE)), [])
    assert str(exc.value) == "HTTP 400 (non relançable)"
    assert evenements.nommes("api_erreur_definitive") == [("ERROR", {"statut": 400, "url": URL})]


# --------------------------------------------------------------------------------------------
# interroger_api : refus, relances et abandon
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("code", [400, 401, 403, 404, 407, 410, 418, 424, 426, 499, 501, 505, 599])
def test_un_code_definitif_n_est_jamais_relance_et_est_journalise(code, evenements):
    """Relancer un 401 ou un 404 ne servirait à rien : abandon immédiat, sans pause."""
    get = GetScripte(Reponse(code, CORPS_VALIDE), Reponse(200, CORPS_VALIDE))
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible) as exc:
        interroger(get, pauses)
    assert str(exc.value) == f"HTTP {code} (non relançable)"
    assert get.appels == [((URL,), {"timeout": 5.0})]
    assert pauses == []
    assert evenements.tous() == [("ERROR", "api_erreur_definitive", {"statut": code, "url": URL})]


@pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
def test_chaque_code_relancable_est_retente_meme_avec_un_corps_valide(code, evenements):
    """Le statut prime sur le corps : un 503 au corps lisible est quand même relancé."""
    politique = replace(POLITIQUE, max_tentatives=2)
    get = GetScripte(Reponse(code, CORPS_VALIDE), Reponse(code, CORPS_VALIDE))
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible) as exc:
        interroger(get, pauses, politique)
    assert str(exc.value) == f"2 tentatives échouées (HTTP {code})"
    assert pauses == [1.0]
    assert evenements.tous() == [
        relance(1, 2, f"HTTP {code}", 1.0),
        indisponible(2, f"HTTP {code}"),
    ]


def test_l_epuisement_des_tentatives_n_attend_pas_apres_la_derniere_et_journalise_tout(
    evenements,
):
    """4 échecs : 3 pauses (1, 2, 4 s), 3 relances, puis l'abandon journalisé une seule fois."""
    get = GetScripte(*[Reponse(503)] * 4)
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible) as exc:
        interroger(get, pauses)
    assert str(exc.value) == "4 tentatives échouées (HTTP 503)"
    assert pauses == [1.0, 2.0, 4.0]
    assert [appel[1] for appel in get.appels] == [{"timeout": 5.0}] * 4
    assert evenements.tous() == [
        relance(1, 4, "HTTP 503", 1.0),
        relance(2, 4, "HTTP 503", 2.0),
        relance(3, 4, "HTTP 503", 4.0),
        indisponible(4, "HTTP 503"),
    ]


def test_la_pause_imposee_par_retry_after_remplace_l_attente_et_est_plafonnee():
    """Retry-After de 7 s est respecté ; 999 s est ramené au plafond ; pile au plafond reste."""
    politique = replace(POLITIQUE, max_tentatives=5)
    get = GetScripte(
        Reponse(429, "", {"Retry-After": "7"}),
        Reponse(503, "", {"Retry-After": "999"}),
        Reponse(502, "", {"Retry-After": "30"}),
        Reponse(500, "", {"Retry-After": "2.5"}),
        Reponse(504, "", {"Retry-After": "11"}),
    )
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible):
        interroger(get, pauses, politique)
    assert pauses == [7.0, 30.0, 30.0, 2.5]


def test_un_retry_after_negatif_ou_nul_donne_une_pause_nulle_et_non_l_attente_exponentielle():
    """``Retry-After: -5`` ou ``0`` donne 0 s : le serveur autorise de rappeler tout de suite."""
    politique = replace(POLITIQUE, max_tentatives=3)
    get = GetScripte(
        Reponse(429, "", {"Retry-After": "-5"}),
        Reponse(429, "", {"Retry-After": "0"}),
        Reponse(429, "", {"Retry-After": "0"}),
    )
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible):
        interroger(get, pauses, politique)
    assert pauses == [0.0, 0.0]


@pytest.mark.parametrize(
    "valeur", ["abc", "", "1,5", "Wed, 21 Oct 2015 07:28:00 GMT", ["1"], None]
)
def test_un_retry_after_illisible_est_ignore_et_l_attente_exponentielle_s_applique(valeur):
    """Une valeur incompréhensible n'est ni crue ni fatale : on retombe sur l'attente prévue."""
    politique = replace(POLITIQUE, max_tentatives=3)
    get = GetScripte(*[Reponse(503, "", {"Retry-After": valeur})] * 3)
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible):
        interroger(get, pauses, politique)
    assert pauses == [1.0, 2.0]


@pytest.mark.parametrize("reponse", [ReponseSansEntetes(503), Reponse(503, "", {})])
def test_une_reponse_sans_retry_after_utilise_l_attente_exponentielle(reponse):
    """Sans en-tête (ou sans attribut ``headers``), l'attente exponentielle s'applique."""
    politique = replace(POLITIQUE, max_tentatives=3)
    get = GetScripte(reponse, reponse, reponse)
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible):
        interroger(get, pauses, politique)
    assert pauses == [1.0, 2.0]


def test_la_pause_imposee_d_une_tentative_ne_s_applique_pas_a_la_suivante():
    """Le Retry-After d'un 429 ne doit pas contaminer l'attente après une panne réseau."""
    politique = replace(POLITIQUE, max_tentatives=3)
    get = GetScripte(
        Reponse(429, "", {"Retry-After": "7"}),
        requests.ConnectionError("x"),
        requests.ConnectionError("y"),
    )
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible):
        interroger(get, pauses, politique)
    assert pauses == [7.0, 2.0]


def test_la_pause_est_plafonnee_meme_quand_la_gigue_depasse_le_plafond(evenements):
    """8 s x (1 + 0,5) = 12 s dépasserait le plafond de 10 s : la pause réelle est 10 s."""
    politique = Relances(
        max_tentatives=2, attente_initiale_s=8.0, facteur=2.0, attente_max_s=10.0, gigue=0.5
    )
    get = GetScripte(requests.ConnectionError("a"), requests.ConnectionError("b"))
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible):
        interroger_api(URL, politique, 5, get=get, dormir=pauses.append, aleatoire=lambda: 1.0)
    assert pauses == [10.0]
    assert evenements.nommes("api_relance")[0][1]["attente_s"] == 10.0


def test_l_attente_est_arrondie_au_centieme_dans_le_journal_mais_pas_dans_la_pause(evenements):
    """Le journal montre 1,01 s ; la vraie pause garde toute sa précision (1,0123456 s)."""
    politique = Relances(
        max_tentatives=2, attente_initiale_s=1.0, facteur=2.0, attente_max_s=60.0, gigue=0.1
    )
    get = GetScripte(requests.ConnectionError("a"), requests.ConnectionError("b"))
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible):
        interroger_api(
            URL, politique, 5, get=get, dormir=pauses.append, aleatoire=lambda: 0.123456
        )
    assert pauses == pytest.approx([1.0123456])
    assert pauses[0] != 1.01
    assert evenements.nommes("api_relance")[0][1]["attente_s"] == 1.01


@pytest.mark.parametrize(
    ("exception", "nom"),
    [
        (requests.ConnectionError("x"), "ConnectionError"),
        (requests.Timeout("x"), "Timeout"),
        (requests.exceptions.ChunkedEncodingError("x"), "ChunkedEncodingError"),
        (requests.exceptions.SSLError("x"), "SSLError"),
        (requests.HTTPError("x"), "HTTPError"),
    ],
)
def test_la_cause_d_abandon_nomme_l_exception_reseau(exception, nom, evenements):
    """Faute de code HTTP, la cause journalisée est le nom de l'exception du client."""
    politique = replace(POLITIQUE, max_tentatives=2)
    get = GetScripte(exception, exception)
    with pytest.raises(SourceIndisponible) as exc:
        interroger(get, [], politique)
    assert str(exc.value) == f"2 tentatives échouées ({nom})"
    assert evenements.tous() == [relance(1, 2, nom, 1.0), indisponible(2, nom)]


def test_une_reponse_illisible_est_relancee_puis_abandonnee_avec_sa_cause(evenements):
    """Un 200 dont le corps n'est pas du JSON est traité comme une panne transitoire."""
    politique = replace(POLITIQUE, max_tentatives=2)
    get = GetScripte(Reponse(200, "pas du json"), Reponse(200, '{"columns": 1, "data": 2}'))
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible) as exc:
        interroger(get, pauses, politique)
    assert str(exc.value) == "2 tentatives échouées (ReponseIllisible)"
    assert pauses == [1.0]
    assert evenements.tous() == [
        relance(1, 2, "ReponseIllisible", 1.0),
        indisponible(2, "ReponseIllisible"),
    ]


def test_sans_aucune_tentative_autorisee_la_source_n_est_pas_appelee(evenements):
    """``max_tentatives = 0`` : on n'appelle rien et la cause reste « inconnue »."""
    get = GetScripte(Reponse(200, CORPS_VALIDE))
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible) as exc:
        interroger(get, pauses, replace(POLITIQUE, max_tentatives=0))
    assert str(exc.value) == "0 tentatives échouées (inconnue)"
    assert get.appels == []
    assert pauses == []
    assert evenements.tous() == [indisponible(0, "inconnue")]


def test_une_seule_tentative_autorisee_abandonne_sans_pause_ni_relance(evenements):
    """``max_tentatives = 1`` : la première panne est la dernière, donc aucune attente."""
    get = GetScripte(requests.Timeout("lent"))
    pauses: list[float] = []
    with pytest.raises(SourceIndisponible) as exc:
        interroger(get, pauses, replace(POLITIQUE, max_tentatives=1))
    assert str(exc.value) == "1 tentatives échouées (Timeout)"
    assert pauses == []
    assert evenements.tous() == [indisponible(1, "Timeout")]


def test_une_exception_inattendue_du_client_n_est_ni_relancee_ni_avalee(evenements):
    """Un bug (ValueError) doit remonter tel quel : les relances ne couvrent que le réseau."""
    erreur = ValueError("bug du client")
    get = GetScripte(erreur, Reponse(200, CORPS_VALIDE))
    pauses: list[float] = []
    with pytest.raises(ValueError) as exc:
        interroger(get, pauses)
    assert exc.value is erreur
    assert len(get.appels) == 1
    assert pauses == []
    assert evenements.tous() == []


def test_le_delai_d_attente_est_transmis_a_chaque_tentative():
    """Le délai maximal d'une réponse s'applique à chacune des tentatives, pas à la première."""
    politique = replace(POLITIQUE, max_tentatives=3)
    get = GetScripte(*[requests.Timeout("lent")] * 3)
    with pytest.raises(SourceIndisponible):
        interroger(get, [], politique, delai=12.5)
    assert get.appels == [((URL,), {"timeout": 12.5})] * 3


# --------------------------------------------------------------------------------------------
# Pannes simulées
# --------------------------------------------------------------------------------------------
def test_les_premiers_appels_echouent_sans_atteindre_la_source_puis_les_suivants_passent():
    """La panne de démonstration ne touche pas la source ; ensuite l'appel passe tel quel."""
    source = GetScripte(Reponse(200, "premier"), Reponse(200, "second"))
    get = PannesSimulees(source, 2)
    for _ in range(2):
        with pytest.raises(requests.ConnectionError) as exc:
            get(URL, timeout=3)
        assert str(exc.value) == MESSAGE_PANNE
    assert source.appels == []
    assert get(URL, timeout=3).content == b"premier"
    assert source.appels == [((URL,), {"timeout": 3})]
    assert get(URL).content == b"second"


def test_zero_panne_simulee_laisse_passer_le_tout_premier_appel_et_les_pannes_sont_par_instance():
    """Sans panne demandée l'appel part tout de suite ; deux enveloppes ont chacune leur compte."""
    source = GetScripte(Reponse(200, "a"), Reponse(200, "b"), Reponse(200, "c"))
    assert PannesSimulees(source, 0)(URL).content == b"a"
    premiere, seconde = PannesSimulees(source, 1), PannesSimulees(source, 1)
    with pytest.raises(requests.ConnectionError):
        premiere(URL)
    with pytest.raises(requests.ConnectionError):
        seconde(URL)
    assert premiere(URL).content == b"b"
    assert seconde(URL).content == b"c"


# --------------------------------------------------------------------------------------------
# corrompre : les sept corruptions de démonstration
# --------------------------------------------------------------------------------------------
ERREURS_DE_CORRUPTION = [
    {"champ": "amt", "code": "type_invalide", "detail": "'abc'"},
    {"champ": "trans_num", "code": "absent", "detail": ""},
    {"champ": "gender", "code": "valeur_inconnue", "detail": ""},
    {"champ": "dob", "code": "format_invalide", "detail": ""},
    {"champ": "lat", "code": "hors_bornes", "detail": ""},
    {"champ": "cc_num", "code": "type_invalide", "detail": ""},
    {"champ": "amt", "code": "hors_bornes", "detail": "-12.5"},
]


@pytest.mark.parametrize("rang", [*range(7), 7, 8, 13, 14, 20, 700, -1, -7])
def test_chaque_corruption_est_refusee_par_le_schema_avec_son_code_et_le_rang_tourne(rang):
    """Le rang choisit la corruption modulo 7 ; chacune casse exactement une règle du schéma."""
    ligne = ligne_brute(rang % 50)
    ligne_avant = dict(ligne)
    abimee = corrompre(ligne, rang)
    assert [e.en_dict() for e in valider_brut(abimee)] == [ERREURS_DE_CORRUPTION[rang % 7]]
    assert ligne == ligne_avant
    assert abimee is not ligne


def test_corrompre_laisse_la_ligne_d_origine_intacte_et_tolere_un_champ_deja_absent():
    """La copie est abîmée, jamais l'original ; retirer un champ absent n'est pas une erreur."""
    ligne = ligne_brute(3)
    ligne_avant = dict(ligne)
    for rang in range(7):
        corrompre(ligne, rang)
    assert ligne == ligne_avant
    sans_identifiant = {"autre": 1}
    assert corrompre(sans_identifiant, 1) == {"autre": 1}
    assert sans_identifiant == {"autre": 1}


# --------------------------------------------------------------------------------------------
# lignes_du_jeu : refus, rebase, corruptions déterministes
# --------------------------------------------------------------------------------------------
def test_un_jeu_introuvable_leve_une_erreur_qui_indique_comment_le_telecharger(tmp_path):
    """Sans fichier, on dit où il manquait et la commande pour l'obtenir."""
    chemin = tmp_path / "absent.csv"
    with pytest.raises(FileNotFoundError) as exc:
        list(lignes_du_jeu(chemin, jour=None, limite=None))
    assert str(exc.value) == (
        f"Jeu de données introuvable : {chemin}. "
        "Le télécharger avec : python -m detection_fraude.telechargement"
    )
    assert not chemin.exists()


@pytest.mark.parametrize("jour", [None, ""])
def test_rebaser_sans_jour_est_refuse_et_le_fichier_reste_inchange(tmp_path, evenements, jour):
    """Sans jour de référence on ne peut pas calculer le décalage : refus, jeu non touché."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", ["2026-10-09 12:00:00"])
    octets_avant = chemin.read_bytes()
    with pytest.raises(ValueError) as exc:
        list(lignes_du_jeu(chemin, jour=jour, limite=None, rebaser_sur="2026-10-10"))
    assert str(exc.value) == "rebaser_sur exige de préciser le jour rejoué (--jour)."
    assert chemin.read_bytes() == octets_avant
    assert evenements.tous() == []


@pytest.mark.parametrize(("jour", "cible"), [("2026-10-09", "demain"), ("hier", "2026-10-10")])
def test_un_rebase_avec_une_date_illisible_est_refuse(tmp_path, evenements, jour, cible):
    """Une date qui n'est pas AAAA-MM-JJ lève ValueError avant tout calcul de décalage."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", ["2026-10-09 12:00:00"])
    with pytest.raises(ValueError):
        list(lignes_du_jeu(chemin, jour=jour, limite=None, rebaser_sur=cible))
    assert evenements.tous() == []


def test_rebaser_sur_le_jour_lui_meme_laisse_dates_et_naissances_inchangees(tmp_path, evenements):
    """Écart nul : aucune translation, aucun avertissement, lignes strictement identiques."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", ["2026-10-09 12:00:00", "2026-10-09 18:30:00"])
    lignes_avant = list(lignes_du_jeu(chemin, jour="2026-10-09", limite=None))
    lignes_apres = list(
        lignes_du_jeu(chemin, jour="2026-10-09", limite=None, rebaser_sur="2026-10-09")
    )
    assert lignes_apres == lignes_avant
    assert evenements.tous() == []


@pytest.mark.parametrize(
    ("cible", "jours", "avertissement"),
    [
        ("2026-10-10", 1, True),
        ("2026-10-12", 3, True),
        ("2026-10-15", 6, True),
        ("2026-10-16", 7, False),
        ("2026-10-17", 8, True),
        ("2026-10-23", 14, False),
        ("2026-10-08", -1, True),
        ("2026-10-06", -3, True),
        ("2026-10-02", -7, False),
        ("2026-09-25", -14, False),
    ],
)
def test_rebaser_translate_current_time_et_naissance_et_signale_le_jour_de_semaine(
    tmp_path, evenements, cible, jours, avertissement
):
    """Toutes les dates bougent du même nombre de jours ; écart non multiple de 7 signalé."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", ["2026-10-09 12:00:00", "2026-10-09 23:59:59"])
    lignes_avant = list(lignes_du_jeu(chemin, jour="2026-10-09", limite=None))
    lignes_apres = list(lignes_du_jeu(chemin, jour="2026-10-09", limite=None, rebaser_sur=cible))
    naissance = date_apres(lignes_avant[0]["dob"], jours)
    for rang in range(2):
        assert lignes_apres[rang]["current_time"] == (
            lignes_avant[rang]["current_time"] + jours * UN_JOUR_MS
        )
        assert lignes_apres[rang]["dob"] == naissance
        reste_apres = {
            k: v for k, v in lignes_apres[rang].items() if k not in ("current_time", "dob")
        }
        reste_avant = {
            k: v for k, v in lignes_avant[rang].items() if k not in ("current_time", "dob")
        }
        assert reste_apres == reste_avant
    attendu = []
    if avertissement:
        attendu = [
            (
                "WARNING",
                "rebase_change_le_jour_de_semaine",
                {
                    "jour": "2026-10-09",
                    "rebaser_sur": cible,
                    "consequence": "scores non représentatifs (jour_semaine est une variable)",
                },
            )
        ]
    assert evenements.tous() == attendu


def test_le_rebase_conserve_l_age_et_l_heure_de_la_journee(tmp_path, hacheur):
    """Après translation, l'âge masqué et l'heure UTC de chaque ligne n'ont pas bougé."""
    chemin = ecrire_jeu(
        tmp_path / "jeu.csv",
        ["2026-10-09 00:00:01", "2026-10-09 12:00:00", "2026-10-09 23:59:59"],
        {1: {"dob": "1990-10-12"}, 2: {"dob": "2008-02-29"}},
    )
    lignes_avant = list(lignes_du_jeu(chemin, jour="2026-10-09", limite=None))
    lignes_apres = list(
        lignes_du_jeu(chemin, jour="2026-10-09", limite=None, rebaser_sur="2026-10-30")
    )
    ages_avant = [masquer(x, hacheur, source="jeu")["age"] for x in lignes_avant]
    ages_apres = [masquer(x, hacheur, source="jeu")["age"] for x in lignes_apres]
    heures_avant = [x["current_time"] % UN_JOUR_MS for x in lignes_avant]
    heures_apres = [x["current_time"] % UN_JOUR_MS for x in lignes_apres]
    assert ages_apres == ages_avant
    assert heures_apres == heures_avant
    assert ages_avant == [41, 35, 18]


def test_les_lignes_non_choisies_restent_inchangees_et_les_corruptions_sont_deterministes(
    tmp_path, evenements
):
    """3 lignes sur 10 sont cassées, toujours les mêmes pour une graine ; les autres intactes."""
    dates = [f"2026-10-09 10:00:{n:02d}" for n in range(10)]
    chemin = ecrire_jeu(tmp_path / "jeu.csv", dates)
    lignes_avant = list(lignes_du_jeu(chemin, jour=None, limite=None))
    lignes_apres = list(lignes_du_jeu(chemin, jour=None, limite=None, corruptions=3, graine=7))
    positions = [r for r in range(10) if lignes_apres[r] != lignes_avant[r]]
    assert positions == [2, 5, 6]
    for rang in range(10):
        if rang in positions:
            assert lignes_apres[rang] == corrompre(lignes_avant[rang], rang)
        else:
            assert lignes_apres[rang] == lignes_avant[rang]
    assert evenements.tous() == [
        (
            "WARNING",
            "corruption_injectee_demo",
            {"rang": rang, "trans_num": lignes_avant[rang]["trans_num"]},
        )
        for rang in (2, 5, 6)
    ]


def test_la_graine_par_defaut_est_7_et_une_autre_graine_change_les_lignes_cassees(tmp_path):
    """Sans graine : graine 7 ; la graine 8 en casse d'autres ; le flux de base reste stable."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", [f"2026-10-09 10:00:{n:02d}" for n in range(10)])
    lignes_avant = list(lignes_du_jeu(chemin, jour=None, limite=None))
    par_defaut = list(lignes_du_jeu(chemin, jour=None, limite=None, corruptions=2))
    graine_8 = list(lignes_du_jeu(chemin, jour=None, limite=None, corruptions=2, graine=8))
    assert [r for r in range(10) if par_defaut[r] != lignes_avant[r]] == [2, 5]
    assert [r for r in range(10) if graine_8[r] != lignes_avant[r]] == [3, 5]
    assert list(lignes_du_jeu(chemin, jour=None, limite=None)) == lignes_avant


def test_demander_plus_de_corruptions_que_de_lignes_casse_tout_sans_erreur(tmp_path):
    """Le nombre de corruptions est borné par le nombre de lignes (sample ne doit pas échouer)."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", [f"2026-10-09 10:00:{n:02d}" for n in range(4)])
    lignes_avant = list(lignes_du_jeu(chemin, jour=None, limite=None))
    for corruptions in (4, 5, 100):
        lignes_apres = list(lignes_du_jeu(chemin, jour=None, limite=None, corruptions=corruptions))
        assert lignes_apres == [corrompre(x, r) for r, x in enumerate(lignes_avant)]


def test_sans_corruption_demandee_le_flux_reste_inchange_et_silencieux(tmp_path, evenements):
    """Par défaut aucune ligne n'est cassée, quelle que soit la graine : c'est le flux fidèle."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", [f"2026-10-09 10:00:{n:02d}" for n in range(5)])
    lignes_avant = list(lignes_du_jeu(chemin, jour=None, limite=None))
    assert list(lignes_du_jeu(chemin, jour=None, limite=None, corruptions=0, graine=3)) == (
        lignes_avant
    )
    assert evenements.tous() == []
    assert all(valider_brut(x) == [] for x in lignes_avant)


def test_la_limite_borne_le_nombre_de_lignes_rejouees_et_laisse_le_jeu_inchange(tmp_path):
    """``limite`` est transmise à la lecture : pas plus de lignes que demandé, jeu intact."""
    chemin = ecrire_jeu(tmp_path / "jeu.csv", [f"2026-10-09 10:00:{n:02d}" for n in range(6)])
    octets_avant = chemin.read_bytes()
    lignes_toutes = list(lignes_du_jeu(chemin, jour=None, limite=None))
    assert list(lignes_du_jeu(chemin, jour=None, limite=4)) == lignes_toutes[:4]
    assert list(lignes_du_jeu(chemin, jour="2026-10-09", limite=2)) == lignes_toutes[:2]
    assert chemin.read_bytes() == octets_avant


# --------------------------------------------------------------------------------------------
# BilanProduction
# --------------------------------------------------------------------------------------------
def test_ajouter_additionne_les_quatre_compteurs_et_laisse_l_autre_bilan_inchange():
    """Chaque compteur s'additionne à son homologue ; le bilan ajouté n'est pas modifié."""
    total = BilanProduction(lues=1, publiees=2, rejetees=3, doublons=4)
    autre = BilanProduction(lues=10, publiees=20, rejetees=30, doublons=40)
    autre_avant = BilanProduction(lues=10, publiees=20, rejetees=30, doublons=40)
    total.ajouter(autre)
    assert total == BilanProduction(lues=11, publiees=22, rejetees=33, doublons=44)
    assert autre == autre_avant
    assert BilanProduction() == BilanProduction(lues=0, publiees=0, rejetees=0, doublons=0)


# --------------------------------------------------------------------------------------------
# Producteur : rejets, doublons, contrôle de sortie
# --------------------------------------------------------------------------------------------
@pytest.fixture
def file_espion():
    return FileEspion()


@pytest.fixture
def producteur(config, file_espion, hacheur):
    return Producteur(config, file_espion, hacheur, source="api")


def sujet(file, nom):
    return [(cle, valeur) for s, cle, valeur in file.publies if s == nom]


def test_une_ligne_au_schema_invalide_va_a_la_dlq_avec_toutes_ses_erreurs_et_rien_aux_valides(
    config, file_espion, producteur, evenements, monkeypatch
):
    """Rejet complet : étape, raison, erreurs, clé, source, date UTC et charge masquée."""
    fuseaux = figer_horloge(monkeypatch)
    ligne = ligne_brute(4, cc_num="carte-invalide", amt="abc", gender="X")
    valides_avant = sujet(file_espion, config.file.sujet_valides)
    assert producteur.traiter_ligne(ligne) == "rejetee"
    assert sujet(file_espion, config.file.sujet_valides) == valides_avant
    assert file_espion.publies == [
        (
            config.file.sujet_rejets,
            ligne["trans_num"],
            {
                "version_schema": 1,
                "etape": "producteur",
                "raison": "schema_invalide",
                "erreurs": [
                    {"champ": "cc_num", "code": "type_invalide", "detail": ""},
                    {"champ": "amt", "code": "type_invalide", "detail": "'abc'"},
                    {"champ": "gender", "code": "valeur_inconnue", "detail": ""},
                ],
                "trans_num": ligne["trans_num"],
                "source": "api",
                "rejete_le": "2026-10-10T08:30:15+00:00",
                "charge_utile_masquee": masquer_pour_rejet(ligne),
            },
        )
    ]
    assert fuseaux == [UTC]
    assert evenements.tous() == [
        (
            "WARNING",
            "ligne_rejetee",
            {
                "raison": "schema_invalide",
                "trans_num": ligne["trans_num"],
                "champs": "amt,cc_num,gender",
                "codes": "type_invalide,valeur_inconnue",
            },
        )
    ]
    assert MARQUEUR not in json.dumps(file_espion.publies)


@pytest.mark.parametrize(
    ("ligne", "type_recu"), [("texte", "str"), (None, "NoneType"), (42, "int"), (["a"], "list")]
)
def test_une_ligne_qui_n_est_pas_un_objet_va_a_la_dlq_sans_identifiant(
    config, file_espion, producteur, evenements, monkeypatch, ligne, type_recu
):
    """Sans objet, pas d'identifiant : le rejet n'a ni clé ni trans_num, juste le type reçu."""
    figer_horloge(monkeypatch)
    valides_avant = sujet(file_espion, config.file.sujet_valides)
    assert producteur.traiter_ligne(ligne) == "rejetee"
    assert sujet(file_espion, config.file.sujet_valides) == valides_avant
    assert file_espion.publies == [
        (
            config.file.sujet_rejets,
            None,
            {
                "version_schema": 1,
                "etape": "producteur",
                "raison": "schema_invalide",
                "erreurs": [{"champ": "*", "code": "ligne_non_objet", "detail": type_recu}],
                "trans_num": None,
                "source": "api",
                "rejete_le": "2026-10-10T08:30:15+00:00",
                "charge_utile_masquee": {"type_recu": type_recu, "contenu": "[MASQUE]"},
            },
        )
    ]
    assert evenements.tous() == [
        (
            "WARNING",
            "ligne_rejetee",
            {
                "raison": "schema_invalide",
                "trans_num": None,
                "champs": "*",
                "codes": "ligne_non_objet",
            },
        )
    ]


BIEN_FORME = "0123456789abcdef" * 2


@pytest.mark.parametrize(
    ("trans_num", "attendu"),
    [
        (BIEN_FORME, BIEN_FORME),
        ("0" * 32, "0" * 32),
        ("f" * 32, "f" * 32),
        ("9" * 31 + "a", "9" * 31 + "a"),
        (BIEN_FORME.upper(), None),
        (BIEN_FORME[:-1], None),
        (BIEN_FORME + "0", None),
        ("0" * 31 + "g", None),
        ("0" * 31 + "X", None),
        ("X" * 32, None),
        ("0" * 31 + "/", None),
        ("0" * 31 + ":", None),
        ("0" * 31 + "`", None),
        ("0" * 31 + " ", None),
        ("z" * 32, None),
        ("", None),
        (123456789, None),
        (None, None),
    ],
)
def test_le_trans_num_n_est_recopie_dans_le_rejet_que_s_il_est_bien_forme(
    config, file_espion, producteur, trans_num, attendu
):
    """32 hexadécimaux minuscules exactement, sinon l'identifiant du rejet est ``None``."""
    ligne = ligne_brute(1, amt="abc", trans_num=trans_num)
    valides_avant = sujet(file_espion, config.file.sujet_valides)
    assert producteur.traiter_ligne(ligne) == "rejetee"
    assert sujet(file_espion, config.file.sujet_valides) == valides_avant
    ((sujet_publie, cle, rejet),) = file_espion.publies
    assert sujet_publie == config.file.sujet_rejets
    assert cle == attendu
    assert rejet["trans_num"] == attendu


def test_un_trans_num_absent_ne_donne_ni_cle_ni_identifiant_dans_le_rejet(
    config, file_espion, producteur
):
    """Une ligne sans ``trans_num`` est rejetée sans clé ; l'erreur ``absent`` est listée."""
    ligne = ligne_brute(1)
    del ligne["trans_num"]
    valides_avant = sujet(file_espion, config.file.sujet_valides)
    assert producteur.traiter_ligne(ligne) == "rejetee"
    assert sujet(file_espion, config.file.sujet_valides) == valides_avant
    ((_, cle, rejet),) = file_espion.publies
    assert cle is None
    assert rejet["trans_num"] is None
    assert rejet["erreurs"] == [{"champ": "trans_num", "code": "absent", "detail": ""}]


def test_un_doublon_n_est_pas_republie_et_ne_laisse_aucune_trace_dans_la_dlq(
    config, file_espion, producteur, evenements
):
    """L'API peut répéter une ligne : la seconde est comptée, jamais republiée ni rejetée."""
    assert producteur.traiter_ligne(ligne_brute(1)) == "publiee"
    publies_avant = list(file_espion.publies)
    assert producteur.traiter_ligne(ligne_brute(1)) == "doublon"
    assert file_espion.publies == publies_avant
    assert sujet(file_espion, config.file.sujet_rejets) == []
    assert evenements.tous() == []


def test_la_memoire_des_doublons_est_bornee_et_oublie_les_plus_anciens(
    config, file_espion, producteur, monkeypatch, hacheur
):
    """Avec 3 places, la 4e ligne chasse la plus ancienne, qui peut alors être republiée."""
    monkeypatch.setattr(producteur_module, "TAILLE_MEMOIRE_DOUBLONS", 3)
    ordre = [1, 2, 3, 1, 4, 1, 3, 4, 2]
    attendus = [
        "publiee",
        "publiee",
        "publiee",
        "doublon",  # 3 places pleines, rien n'est encore oublié (limite exacte)
        "publiee",  # la 4e chasse la ligne 1
        "publiee",  # 1 est oubliée, donc republiée, et chasse la ligne 2
        "doublon",
        "doublon",
        "publiee",  # 2 a été oubliée
    ]
    resultats = []
    for numero in ordre:
        publies_avant = list(file_espion.publies)
        resultat = producteur.traiter_ligne(ligne_brute(numero))
        resultats.append(resultat)
        if resultat == "doublon":
            assert file_espion.publies == publies_avant
    assert resultats == attendus
    assert [cle for cle, _ in sujet(file_espion, config.file.sujet_valides)] == [
        masquer(ligne_brute(n), hacheur, source="api")["carte_hash"] for n in (1, 2, 3, 4, 1, 2)
    ]


def test_une_ligne_rejetee_n_est_pas_memorisee_comme_deja_vue(config, file_espion, producteur):
    """Un rejet n'entre pas dans la mémoire : la version corrigée de la ligne sera acceptée."""
    mauvaise = ligne_brute(1, amt="abc")
    valides_avant = sujet(file_espion, config.file.sujet_valides)
    assert producteur.traiter_ligne(mauvaise) == "rejetee"
    assert producteur.traiter_ligne(mauvaise) == "rejetee"
    assert sujet(file_espion, config.file.sujet_valides) == valides_avant
    assert [cle for cle, _ in sujet(file_espion, config.file.sujet_rejets)] == [
        mauvaise["trans_num"]
    ] * 2
    assert producteur.traiter_ligne(ligne_brute(1)) == "publiee"
    assert [v["trans_num"] for _, v in sujet(file_espion, config.file.sujet_valides)] == [
        mauvaise["trans_num"]
    ]


def test_le_controle_de_sortie_arrete_un_champ_sensible_echappe_au_masquage(
    config, file_espion, producteur, evenements, monkeypatch, hacheur
):
    """Défense en profondeur : un cc_num qui fuirait du masquage est bloqué avant la file."""

    def masquage_defaillant(ligne, hacheur_utilise, *, source):
        message = masquer(ligne, hacheur_utilise, source=source)
        message["cc_num"] = ligne["cc_num"]
        return message

    monkeypatch.setattr(producteur_module, "masquer", masquage_defaillant)
    ligne = ligne_brute(2)
    valides_avant = sujet(file_espion, config.file.sujet_valides)
    assert producteur.traiter_ligne(ligne) == "rejetee"
    assert sujet(file_espion, config.file.sujet_valides) == valides_avant
    ((sujet_publie, cle, rejet),) = file_espion.publies
    assert (sujet_publie, cle) == (config.file.sujet_rejets, ligne["trans_num"])
    assert rejet["raison"] == "masquage_invalide"
    assert rejet["erreurs"] == [{"champ": "cc_num", "code": "champ_inattendu", "detail": ""}]
    assert str(ligne["cc_num"]) not in json.dumps(rejet)
    assert evenements.tous() == [
        (
            "WARNING",
            "ligne_rejetee",
            {
                "raison": "masquage_invalide",
                "trans_num": ligne["trans_num"],
                "champs": "cc_num",
                "codes": "champ_inattendu",
            },
        )
    ]


def test_un_message_masque_refuse_par_le_controle_de_sortie_va_a_la_dlq_avec_ses_erreurs(
    config, file_espion, producteur, hacheur, monkeypatch, evenements
):
    """Le contrôle de sortie reçoit le message masqué exact ; ses erreurs finissent en DLQ."""
    figer_horloge(monkeypatch)
    recus = []

    def controle(message):
        recus.append(message)
        return [Erreur("age", "hors_bornes", "trop vieux"), Erreur("etat", "format_invalide")]

    monkeypatch.setattr(producteur_module, "valider_masque", controle)
    ligne = ligne_brute(5)
    valides_avant = sujet(file_espion, config.file.sujet_valides)
    assert producteur.traiter_ligne(ligne) == "rejetee"
    assert recus == [masquer(ligne, hacheur, source="api")]
    assert evenements.tous() == [
        (
            "WARNING",
            "ligne_rejetee",
            {
                "raison": "masquage_invalide",
                "trans_num": ligne["trans_num"],
                "champs": "age,etat",
                "codes": "format_invalide,hors_bornes",
            },
        )
    ]
    assert sujet(file_espion, config.file.sujet_valides) == valides_avant
    assert file_espion.publies == [
        (
            config.file.sujet_rejets,
            ligne["trans_num"],
            {
                "version_schema": 1,
                "etape": "producteur",
                "raison": "masquage_invalide",
                "erreurs": [
                    {"champ": "age", "code": "hors_bornes", "detail": "trop vieux"},
                    {"champ": "etat", "code": "format_invalide", "detail": ""},
                ],
                "trans_num": ligne["trans_num"],
                "source": "api",
                "rejete_le": "2026-10-10T08:30:15+00:00",
                "charge_utile_masquee": masquer_pour_rejet(ligne),
            },
        )
    ]


@pytest.mark.parametrize("source", ["api", "jeu"])
def test_une_ligne_valide_est_publiee_masquee_sans_alterer_la_ligne_recue(
    config, file_espion, hacheur, source
):
    """Message publié = masquage exact, clé = empreinte de carte, DLQ vide, ligne reçue intacte."""
    producteur = Producteur(config, file_espion, hacheur, source=source)
    ligne = ligne_brute(6)
    ligne_avant = dict(ligne)
    assert producteur.traiter_ligne(ligne) == "publiee"
    assert ligne == ligne_avant
    attendu = masquer(ligne, hacheur, source=source)
    assert file_espion.publies == [(config.file.sujet_valides, attendu["carte_hash"], attendu)]
    assert attendu["source"] == source
    assert MARQUEUR not in json.dumps(file_espion.publies)


def test_traiter_lignes_compte_chaque_issue_et_accuse_reception_une_seule_fois(
    config, file_espion, producteur
):
    """Le bilan distingue publiées, rejetées et doublons ; ``vider`` clôt le lot, même vide."""
    lignes = [ligne_brute(1), ligne_brute(1), ligne_brute(2, amt="abc"), "texte", ligne_brute(3)]
    lignes_avant = copy.deepcopy(lignes)
    bilan = producteur.traiter_lignes(iter(lignes))
    assert bilan == BilanProduction(lues=5, publiees=2, rejetees=2, doublons=1)
    assert lignes == lignes_avant
    assert file_espion.vidages == 1
    assert producteur.traiter_lignes([]) == BilanProduction()
    assert file_espion.vidages == 2


def test_une_panne_de_publication_interrompt_le_lot_sans_accuse_de_reception(
    file_espion, producteur
):
    """Si la file refuse un message, l'erreur remonte et le lot n'est pas déclaré livré."""
    file_espion.echec_publication = OSError("disque plein")
    with pytest.raises(OSError) as exc:
        producteur.traiter_lignes([ligne_brute(1), ligne_brute(2)])
    assert str(exc.value) == "disque plein"
    assert file_espion.vidages == 0
    assert file_espion.publies == []


# --------------------------------------------------------------------------------------------
# boucle_api : échecs, arrêt, erreurs inattendues
# --------------------------------------------------------------------------------------------
def une_tentative(config):
    return replace(config, source=replace(config.source, relances=Relances(max_tentatives=1)))


def lire_etat(config):
    chemin = config.racine_donnees / "etat" / "producteur.json"
    return json.loads(chemin.read_text(encoding="utf-8"))


def test_une_source_toujours_en_panne_laisse_le_bilan_inchange_et_compte_les_echecs(
    config, file_espion, producteur, evenements, monkeypatch
):
    """3 cycles abandonnés : rien n'est publié, l'état compte 1, 2, 3 échecs, la boucle survit."""
    figer_horloge(monkeypatch)
    config_courte = une_tentative(config)
    etats_vus: list[dict[str, Any]] = []
    pauses: list[float] = []

    def dormir(secondes):
        pauses.append(secondes)
        etats_vus.append(lire_etat(config))

    get = GetScripte(*[requests.ConnectionError("x")] * 3)
    bilan_initial = BilanProduction()
    arret = threading.Event()
    with chien_de_garde(arret):
        bilan = boucle_api(
            config_courte,
            producteur,
            arret,
            nombre_appels=3,
            intervalle_s=5,
            get=get,
            dormir=dormir,
        )
    assert bilan == bilan_initial
    assert file_espion.publies == []
    assert pauses == [5, 5]
    assert (
        get.appels == [((config.source.api_url,), {"timeout": config.source.delai_attente_s})] * 3
    )
    comptes = {"lues": 0, "publiees": 0, "rejetees": 0, "doublons": 0}
    horodatage = "2026-10-10T08:30:15.123456+00:00"
    assert etats_vus == [
        {"derniere_tentative": horodatage, "echecs_consecutifs": 1, **comptes},
        {"derniere_tentative": horodatage, "echecs_consecutifs": 2, **comptes},
    ]
    assert lire_etat(config) == {
        "derniere_tentative": horodatage,
        "echecs_consecutifs": 3,
        **comptes,
    }
    cause = "1 tentatives échouées (ConnectionError)"
    assert evenements.nommes("cycle_abandonne") == [
        ("ERROR", {"cause": cause, "echecs_consecutifs": n}) for n in (1, 2, 3)
    ]
    assert evenements.nommes("cycle_termine") == []


def test_une_erreur_inattendue_du_producteur_n_est_pas_avalee_par_la_boucle(config, evenements):
    """Seule ``SourceIndisponible`` est absorbée : un bug de traitement arrête le processus."""

    class ProducteurQuiPlante:
        def traiter_lignes(self, lignes):
            raise RuntimeError("bug de traitement")

    get = GetScripte(reponse_api([ligne_brute(1)]), reponse_api([ligne_brute(2)]))
    with pytest.raises(RuntimeError) as exc:
        boucle_api(
            config,
            ProducteurQuiPlante(),
            threading.Event(),
            nombre_appels=2,
            intervalle_s=0,
            get=get,
            dormir=lambda s: None,
        )
    assert str(exc.value) == "bug de traitement"
    assert len(get.appels) == 1
    assert not (config.racine_donnees / "etat" / "producteur.json").exists()
    assert evenements.nommes("cycle_termine") == []
    assert evenements.nommes("cycle_abandonne") == []


def test_un_arret_deja_demande_laisse_tout_inchange(
    config, file_espion, producteur, evenements, monkeypatch
):
    """Arrêt avant le premier tour : rien ; arrêt pendant la pause : fin sans nouvel appel."""
    arret = threading.Event()
    arret.set()
    get = GetScripte(reponse_api([ligne_brute(1)]))
    bilan_initial = BilanProduction()
    publies_avant = list(file_espion.publies)
    assert boucle_api(config, producteur, arret, get=get, dormir=lambda s: None) == bilan_initial
    assert get.appels == []
    assert file_espion.publies == publies_avant
    assert not (config.racine_donnees / "etat").exists()
    assert evenements.tous() == []

    en_pause = threading.Event()
    monkeypatch.setattr(en_pause, "wait", lambda secondes=None: en_pause.set())
    get_unique = GetScripte(reponse_api([ligne_brute(3)]))
    assert boucle_api(config, producteur, en_pause, intervalle_s=7, get=get_unique) == (
        BilanProduction(lues=1, publiees=1)
    )
    assert len(get_unique.appels) == 1


def test_zero_appel_demande_ne_touche_ni_la_source_ni_l_etat(
    config, file_espion, producteur, evenements
):
    """``nombre_appels = 0`` borne la boucle avant le premier appel : état de santé inchangé."""
    get = GetScripte(reponse_api([ligne_brute(1)]))
    bilan_initial = BilanProduction()
    arret = threading.Event()
    with chien_de_garde(arret):
        bilan = boucle_api(
            config, producteur, arret, nombre_appels=0, get=get, dormir=lambda s: None
        )
    assert bilan == bilan_initial
    assert get.appels == []
    assert not (config.racine_donnees / "etat").exists()
    assert evenements.tous() == []
