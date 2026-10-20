# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests INVERSES de la collecte OpenStreetMap (Overpass) : refus, reprises et absences d'effet.

Complète tests/test_osm.py. Chaque test prouve un refus ou une absence d'effet : une réponse en
erreur est réessayée selon un calendrier exact (5, 20 puis 45 s) puis basculée sur le serveur
miroir, sans jamais réessayer un refus définitif (403, 404) ; une réponse partielle est
redemandée ; un hôtel sans nom, une étoile illisible ou une ville sans coordonnées ne
fabriquent aucune valeur ; en mode hors ligne, aucune requête n'est émise et une archive n'est
jamais réécrite ; l'option ``--reconvertir`` est obligatoire en ligne de commande.

Aucun appel réseau : la session HTTP est factice et ``time.sleep`` est remplacé par un
enregistreur, ce qui permet de vérifier les attentes exactes sans attendre.
"""

import copy
import json
import logging
import os
import re
import runpy
import sys

import pandas as pd
import pytest
import requests

import kayak.stockage.data_lake as module_lake
from kayak import config
from kayak.collecte import hotels_osm
from kayak.collecte.hotels_osm import (
    DECALAGE_WAY,
    MARQUE_REQUETE,
    ReponseIncomplete,
    archive_reutilisable,
    collecter_hotels_osm,
    construire_hotels,
    convertir,
    diagnostic_troncature,
    ecrire_hotels,
    interroger,
    reconvertir_depuis_archive,
    requete_overpass,
    verifier_reponse,
)
from kayak.stockage.data_lake import DataLake

DATE = "2026-10-10"
PRINCIPAL = "https://overpass-api.de/api/interpreter"
MIROIR = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
CHAMPS_HOTEL = [
    "hotel_id",
    "nom",
    "url",
    "latitude",
    "longitude",
    "adresse",
    "commune",
    "pays",
    "note",
    "nb_avis",
    "etoiles",
    "description",
    "id_ville",
    "ville",
    "date_collecte",
    "source",
]


@pytest.fixture(autouse=True)
def pauses(monkeypatch) -> list[float]:
    """Date fixée ; les attentes sont enregistrées dans une liste au lieu d'être subies."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", DATE)
    enregistrees: list[float] = []
    monkeypatch.setattr(hotels_osm.time, "sleep", enregistrees.append)
    return enregistrees


class _Reponse:
    """Réponse HTTP minimale : statut, JSON, ou erreur de décodage du JSON."""

    def __init__(self, statut=200, donnees=None, erreur_json=None):
        self.status_code = statut
        self.donnees = donnees if donnees is not None else {}
        self.erreur_json = erreur_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} erreur simulée")

    def json(self):
        if self.erreur_json is not None:
            raise self.erreur_json
        return self.donnees


class _Session:
    """Session factice : rejoue un scénario (réponses ou exceptions) et note chaque appel."""

    def __init__(self, *scenario):
        self.scenario = list(scenario)
        self.appels = []
        self.headers = {}

    def post(self, url, data=None, timeout=None):
        self.appels.append({"url": url, "data": data, "timeout": timeout})
        suivant = self.scenario.pop(0)
        if isinstance(suivant, Exception):
            raise suivant
        return suivant


def _lake(dossier) -> DataLake:
    return DataLake(bucket=None, racine_locale=dossier)


def _noeud(identifiant: int, nom: str | None = "Hôtel", **etiquettes) -> dict:
    """Un point OpenStreetMap ; sans nom si ``nom`` est None."""
    tags = {"tourism": "hotel", **etiquettes}
    if nom:
        tags["name"] = f"{nom} {identifiant}"
    return {"type": "node", "id": identifiant, "lat": 43.0, "lon": 2.0, "tags": tags}


def _archiver(lake: DataLake, id_ville: int, reponse: dict, date=DATE) -> str:
    """Écrit l'archive brute d'une ville et renvoie son chemin logique."""
    chemin = f"raw/osm/date={date}/ville_{id_ville:02d}.json"
    lake.ecrire(chemin, json.dumps(reponse, ensure_ascii=False))
    return chemin


def _coordonnees(*villes) -> pd.DataFrame:
    """Un tuple (id_ville, ville, latitude, longitude) par ville."""
    return pd.DataFrame(villes, columns=["id_ville", "ville", "latitude", "longitude"])


# --- requete_overpass, verifier_reponse, archive_reutilisable ---------------------------------


def test_requete_overpass_exacte_pour_un_point_et_un_rayon_de_8_km():
    """La requête est exacte : zone France, points ET contours nommés, rayon 8000 m, sans limite.

    La latitude précède la longitude dans le filtre « around ».
    """
    assert requete_overpass(45.9, 6.1) == (
        "[out:json][timeout:90];"
        'area["ISO3166-1"="FR"][admin_level=2]->.pays;'
        '(node["tourism"="hotel"]["name"](area.pays)(around:8000,45.9,6.1);'
        'way["tourism"="hotel"]["name"](area.pays)(around:8000,45.9,6.1););'
        "out center tags;"
    )


@pytest.mark.parametrize(
    "remarque",
    [
        'runtime error: Query timed out in "query" at line 1.',
        "Runtime Error: out of memory",
        "RUNTIME ERROR",
        "avertissement : un runtime error est survenu en cours de route",
    ],
)
def test_reponse_avec_erreur_d_execution_est_refusee_quelle_que_soit_la_casse(remarque):
    """Un ``remark`` qui mentionne « runtime error » (majuscules comprises) est refusé.

    L'erreur ReponseIncomplete est une RuntimeError qui porte la remarque telle quelle.
    """
    partielle = {"elements": [{"id": 1}], "remark": remarque}
    with pytest.raises(ReponseIncomplete, match=f"^{re.escape(remarque)}$") as erreur:
        verifier_reponse(partielle)
    assert isinstance(erreur.value, RuntimeError)


@pytest.mark.parametrize(
    "reponse",
    [
        {"elements": []},
        {"elements": [], "remark": ""},
        {"elements": [], "remark": "autre remarque sans gravité"},
        {"elements": [], "remark": 12345},
        {"elements": [], "remark": None},
    ],
)
def test_reponse_complete_est_rendue_telle_quelle_et_reste_inchangee(reponse):
    """Sans erreur d'exécution, la réponse est rendue (le même objet) sans être modifiée."""
    avant = copy.deepcopy(reponse)
    assert verifier_reponse(reponse) is reponse
    assert reponse == avant


@pytest.mark.parametrize(
    ("reponse", "attendu"),
    [
        ({"elements": [1] * 39}, True),
        ({"elements": [1] * 40}, False),
        ({"elements": [1] * 41}, False),
        ({"elements": []}, True),
        ({}, True),
        ({MARQUE_REQUETE: "q", "elements": [1] * 500}, True),
        ({MARQUE_REQUETE: None, "elements": [1] * 500}, True),
    ],
)
def test_archive_reutilisable_aux_bornes_du_plafond_et_laisse_la_reponse_inchangee(
    reponse, attendu
):
    """Moins de 40 éléments ou marque de la nouvelle requête : réutilisable ; dès 40 : suspecte.

    Une réponse sans clé ``elements`` compte pour zéro élément. La réponse n'est pas modifiée.
    """
    avant = copy.deepcopy(reponse)
    assert archive_reutilisable(reponse) is attendu
    assert reponse == avant


# --- convertir --------------------------------------------------------------------------------


def test_convertir_point_complet_reprend_chaque_champ():
    """Un point nommé donne les 16 champs du schéma commun, avec les valeurs d'OpenStreetMap."""
    point = {
        "type": "node",
        "id": 42,
        "lat": 45.9,
        "lon": 6.12,
        "tags": {
            "name": "Hôtel du Lac",
            "stars": "3",
            "website": "https://lac.example",
            "contact:website": "https://autre.example",
            "addr:housenumber": "12",
            "addr:street": "Quai",
            "addr:postcode": "74000",
            "addr:city": "Annecy",
            "addr:country": "FR",
            "description": "Vue sur le lac",
        },
    }
    hotel = convertir(point, 15, "Annecy", DATE)
    assert hotel == {
        "hotel_id": 42,
        "nom": "Hôtel du Lac",
        "url": "https://lac.example",
        "latitude": 45.9,
        "longitude": 6.12,
        "adresse": "12 Quai 74000 Annecy",
        "commune": "Annecy",
        "pays": "FR",
        "note": None,
        "nb_avis": None,
        "etoiles": 3,
        "description": "Vue sur le lac",
        "id_ville": 15,
        "ville": "Annecy",
        "date_collecte": DATE,
        "source": "openstreetmap",
    }
    assert list(hotel) == CHAMPS_HOTEL


def test_convertir_contour_decale_l_identifiant_garde_l_url_d_origine_et_l_element_inchange():
    """Un contour (way) a un identifiant décalé de 10**12 et ses coordonnées dans ``center``.

    L'URL de repli porte l'identifiant OpenStreetMap d'origine, pas l'identifiant décalé. Un
    point sans ``type`` est un node (identifiant non décalé).
    """
    contour = {"type": "way", "id": 7, "center": {"lat": 45.8, "lon": 6.1}, "tags": {"name": "G"}}
    sans_type = {"id": 7, "lat": 1.5, "lon": 2.5, "tags": {"name": "P"}}
    avant = copy.deepcopy((contour, sans_type))
    hotel_contour = convertir(contour, 1, "V", DATE)
    hotel_point = convertir(sans_type, 1, "V", DATE)
    assert (contour, sans_type) == avant
    assert DECALAGE_WAY == 1_000_000_000_000
    assert hotel_contour["hotel_id"] == 1_000_000_000_007
    assert (hotel_contour["latitude"], hotel_contour["longitude"]) == (45.8, 6.1)
    assert hotel_contour["url"] == "https://www.openstreetmap.org/way/7"
    assert hotel_point["hotel_id"] == 7
    assert (hotel_point["latitude"], hotel_point["longitude"]) == (1.5, 2.5)
    assert hotel_point["url"] == "https://www.openstreetmap.org/node/7"


def test_convertir_contour_sans_centre_donne_des_coordonnees_none_et_l_element_reste_inchange():
    """Un contour sans ``center`` ni lat/lon n'a pas de position : None, pas de zéro inventé."""
    contour = {"type": "way", "id": 7, "tags": {"name": "Sans centre"}}
    avant = copy.deepcopy(contour)
    hotel = convertir(contour, 1, "V", DATE)
    assert contour == avant
    assert (hotel["latitude"], hotel["longitude"]) == (None, None)


@pytest.mark.parametrize(
    "etiquettes",
    [
        {},
        {"name": ""},
        {"name": None},
        {"tourism": "hotel", "stars": "4"},
    ],
)
def test_hotel_sans_nom_est_ecarte_et_l_element_reste_inchange(etiquettes):
    """Sans nom (absent, vide ou nul), l'élément n'est pas un hôtel exploitable : None."""
    element = {"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": etiquettes}
    avant = copy.deepcopy(element)
    assert convertir(element, 1, "X", "d") is None
    assert element == avant


def test_element_sans_etiquettes_est_ecarte_et_reste_inchange():
    """Un élément sans bloc ``tags`` n'a pas de nom : écarté sans erreur."""
    element = {"type": "node", "id": 1, "lat": 0, "lon": 0}
    avant = copy.deepcopy(element)
    assert convertir(element, 1, "X", "d") is None
    assert element == avant


def test_element_nomme_sans_identifiant_est_refuse():
    """Un hôtel nommé mais sans ``id`` lève KeyError : on ne fabrique pas d'identifiant."""
    with pytest.raises(KeyError, match="id"):
        convertir({"type": "node", "lat": 0, "lon": 0, "tags": {"name": "Anonyme"}}, 1, "X", "d")


@pytest.mark.parametrize(
    ("stars", "attendu"),
    [
        ("4S", 4),
        ("3", 3),
        ("5+", 5),
        ("0", 0),
        ("", None),
        ("S4", None),
        (" 4", None),
        ("quatre", None),
        ("*", None),
    ],
)
def test_etoiles_lit_le_premier_chiffre_et_refuse_le_reste_sans_modifier_l_element(
    stars, attendu
):
    """Le nombre d'étoiles est le premier caractère s'il est un chiffre ; sinon None."""
    element = {"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": {"name": "H", "stars": stars}}
    avant = copy.deepcopy(element)
    assert convertir(element, 1, "X", "d")["etoiles"] == attendu
    assert element == avant


def test_etoiles_absentes_donnent_none_et_l_element_reste_inchange():
    """Sans étiquette ``stars``, aucune classe d'étoiles n'est supposée."""
    element = {"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": {"name": "H"}}
    avant = copy.deepcopy(element)
    assert convertir(element, 1, "X", "d")["etoiles"] is None
    assert element == avant


@pytest.mark.parametrize(
    ("etiquettes", "adresse"),
    [
        (
            {
                "addr:housenumber": "12",
                "addr:street": "Rue Haute",
                "addr:postcode": "30000",
                "addr:city": "Nîmes",
            },
            "12 Rue Haute 30000 Nîmes",
        ),
        ({"addr:street": "Rue Haute", "addr:city": "Nîmes"}, "Rue Haute Nîmes"),
        ({"addr:postcode": "30000"}, "30000"),
        ({"addr:housenumber": "12", "addr:street": ""}, "12"),
        ({"addr:housenumber": "", "addr:street": "", "addr:city": ""}, None),
        ({"addr:country": "FR"}, None),
        ({}, None),
    ],
)
def test_adresse_assemble_les_champs_presents_dans_l_ordre_et_none_si_aucun_sans_toucher_l_element(
    etiquettes, adresse
):
    """Numéro, rue, code postal, commune dans cet ordre ; champs vides ignorés ; None si rien."""
    element = {"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": {"name": "H", **etiquettes}}
    avant = copy.deepcopy(element)
    assert convertir(element, 1, "X", "d")["adresse"] == adresse
    assert element == avant


@pytest.mark.parametrize(
    ("etiquettes", "url"),
    [
        ({"website": "https://a.example", "contact:website": "https://b.example"}, "https://a.example"),
        ({"website": "", "contact:website": "https://b.example"}, "https://b.example"),
        ({"contact:website": "https://b.example"}, "https://b.example"),
        ({"website": "", "contact:website": ""}, "https://www.openstreetmap.org/node/9"),
        ({}, "https://www.openstreetmap.org/node/9"),
    ],
)
def test_url_prefere_website_puis_contact_website_puis_la_fiche_osm_sans_modifier_l_element(
    etiquettes, url
):
    """Le site web déclaré prime, puis le site de contact ; à défaut, la fiche OpenStreetMap."""
    element = {"type": "node", "id": 9, "lat": 0, "lon": 0, "tags": {"name": "H", **etiquettes}}
    avant = copy.deepcopy(element)
    assert convertir(element, 1, "X", "d")["url"] == url
    assert element == avant


def test_commune_pays_et_description_absents_donnent_none_et_l_element_reste_inchange():
    """Sans addr:city, addr:country ni description, les champs valent None (pas de chaîne vide)."""
    element = {"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": {"name": "H"}}
    avant = copy.deepcopy(element)
    hotel = convertir(element, 1, "X", "d")
    assert (hotel["commune"], hotel["pays"], hotel["description"]) == (None, None, None)
    assert (hotel["note"], hotel["nb_avis"]) == (None, None)
    assert element == avant


# --- interroger -------------------------------------------------------------------------------


def test_interroger_reussit_du_premier_coup_sans_attente_et_poste_la_requete_exacte(pauses):
    """Une réponse 200 est rendue après un seul appel, sans attente, vers le serveur principal.

    La requête part en formulaire (``data={"data": requete}``) avec un délai de 120 s.
    """
    complete = {"elements": [{"type": "node", "id": 1}]}
    session = _Session(_Reponse(200, complete))
    assert interroger(session, "requete-test") == complete
    assert session.appels == [{"url": PRINCIPAL, "data": {"data": "requete-test"}, "timeout": 120}]
    assert pauses == [0]


@pytest.mark.parametrize(
    "premier_essai",
    [
        _Reponse(429),
        _Reponse(500),
        _Reponse(502),
        _Reponse(503),
        _Reponse(504),
        requests.ConnectionError("coupure"),
        requests.Timeout("délai"),
        _Reponse(erreur_json=requests.exceptions.JSONDecodeError("html", "<html>", 0)),
        _Reponse(200, {"remark": "runtime error: out of memory"}),
    ],
    ids=["429", "500", "502", "503", "504", "coupure", "delai", "json_illisible", "partielle"],
)
def test_panne_transitoire_est_reessayee_apres_cinq_secondes_puis_la_reponse_est_rendue(
    pauses, premier_essai
):
    """Saturation (429, 500, 502, 503, 504), coupure, délai, JSON illisible ou réponse partielle.

    Chacune est suivie d'un nouvel essai sur le même serveur, après 5 s d'attente (0 s avant le
    premier essai) ; la réponse complète qui suit est celle qui est rendue.
    """
    complete = {"elements": []}
    session = _Session(premier_essai, _Reponse(200, complete))
    assert interroger(session, "q") == complete
    assert [appel["url"] for appel in session.appels] == [PRINCIPAL, PRINCIPAL]
    assert pauses == [0, 5]


@pytest.mark.parametrize("statut", [400, 401, 403, 404, 410, 501])
def test_refus_definitif_n_est_jamais_reessaye_et_l_erreur_http_remonte(pauses, statut):
    """Un 4xx (ou 501) n'est pas une saturation : HTTPError remonte au premier essai.

    Aucune attente, aucun second appel, aucun basculement sur le miroir : insister ne servirait
    qu'à surcharger le service.
    """
    session = _Session(_Reponse(statut), _Reponse(200, {"elements": []}))
    with pytest.raises(requests.HTTPError, match=f"^{statut} erreur simulée$"):
        interroger(session, "q")
    assert [appel["url"] for appel in session.appels] == [PRINCIPAL]
    assert pauses == [0]


def test_serveur_principal_sature_quatre_fois_bascule_sur_le_miroir_avec_les_attentes_exactes(
    pauses,
):
    """Quatre essais (attentes 0, 5, 20, 45 s) sur le principal, puis le miroir repart à 0 s."""
    complete = {"elements": [{"type": "node", "id": 5}]}
    session = _Session(*[_Reponse(503)] * 4, _Reponse(200, complete))
    assert interroger(session, "q") == complete
    assert [appel["url"] for appel in session.appels] == [PRINCIPAL] * 4 + [MIROIR]
    assert pauses == [0, 5, 20, 45, 0]


def test_epuisement_des_deux_serveurs_leve_une_erreur_qui_cite_le_dernier_echec(pauses):
    """Huit réponses 429 : RuntimeError dont le message nomme le dernier statut et le miroir.

    Calendrier complet : 0, 5, 20, 45 s sur le principal, puis 0, 5, 20, 45 s sur le miroir.
    """
    session = _Session(*[_Reponse(429)] * 8)
    with pytest.raises(
        RuntimeError, match=f"^Overpass indisponible après plusieurs essais : 429 {MIROIR}$"
    ):
        interroger(session, "q")
    assert [appel["url"] for appel in session.appels] == [PRINCIPAL] * 4 + [MIROIR] * 4
    assert pauses == [0, 5, 20, 45, 0, 5, 20, 45]


@pytest.mark.parametrize(
    "essai",
    [
        requests.ConnectionError("coupure réseau"),
        requests.Timeout("délai dépassé"),
        _Reponse(erreur_json=requests.exceptions.JSONDecodeError("corps illisible", "<html>", 0)),
        _Reponse(200, {"remark": "runtime error: out of memory"}),
    ],
    ids=["coupure", "delai", "json_illisible", "reponse_partielle"],
)
def test_coupure_delai_json_illisible_ou_reponse_partielle_sont_reessayes_puis_epuises(
    pauses, essai
):
    """Chaque panne est réessayée selon le calendrier complet ; l'erreur finale la cite.

    Huit essais (0, 5, 20, 45 s sur le principal, puis sur le miroir), puis RuntimeError.
    """
    session = _Session(*[essai] * 8)
    with pytest.raises(RuntimeError, match=r"^Overpass indisponible après plusieurs essais : .+$"):
        interroger(session, "q")
    assert pauses == [0, 5, 20, 45, 0, 5, 20, 45]
    assert [appel["url"] for appel in session.appels] == [PRINCIPAL] * 4 + [MIROIR] * 4


def test_erreur_de_decodage_d_un_autre_type_n_est_pas_reessayee(pauses):
    """Seule l'erreur JSON de ``requests`` est réessayée : une ValueError quelconque remonte."""
    session = _Session(_Reponse(erreur_json=ValueError("autre problème")), _Reponse(200, {}))
    with pytest.raises(ValueError, match="autre problème"):
        interroger(session, "q")
    assert [appel["url"] for appel in session.appels] == [PRINCIPAL]
    assert pauses == [0]


def test_le_message_final_cite_l_erreur_du_tout_dernier_essai_meme_d_un_autre_type(pauses):
    """Le message final reprend la dernière erreur, même d'un autre type que les précédentes."""
    session = _Session(
        *[_Reponse(503)] * 4,
        _Reponse(503),
        _Reponse(504),
        _Reponse(429),
        requests.ConnectionError("coupure finale"),
    )
    with pytest.raises(RuntimeError, match=r": coupure finale$"):
        interroger(session, "q")
    assert pauses == [0, 5, 20, 45, 0, 5, 20, 45]


def test_reponses_partielles_epuisees_levent_une_erreur_qui_cite_la_remarque(pauses):
    """Huit réponses 200 « runtime error » : refusées, redemandées, puis erreur avec la remarque."""
    partielle = {"elements": [{"id": 1}], "remark": "runtime error: out of memory"}
    session = _Session(*[_Reponse(200, partielle)] * 8)
    with pytest.raises(
        RuntimeError,
        match="^Overpass indisponible après plusieurs essais : runtime error: out of memory$",
    ):
        interroger(session, "q")
    assert pauses == [0, 5, 20, 45, 0, 5, 20, 45]


# --- construire_hotels et _reponse_de_ville ---------------------------------------------------


def test_hors_ligne_sans_archive_ignore_la_ville_journalise_et_ne_cree_aucun_fichier(
    tmp_path, caplog, pauses
):
    """Sans session et sans archive, la ville est sautée avec un avertissement ; rien n'est écrit.

    Les autres villes, elles, sont converties. Aucune attente n'est prise.
    """
    lake = _lake(tmp_path)
    _archiver(lake, 2, {"elements": [_noeud(10), _noeud(11)]})
    avant = sorted(lake.lister("raw/"))
    caplog.set_level(logging.INFO)
    coordonnees = _coordonnees((1, "Nice", 43.7, 7.27), (2, "Lyon", 45.7, 4.8))
    hotels = construire_hotels(coordonnees, lake, None)
    assert [(h["hotel_id"], h["ville"]) for h in hotels] == [(10, "Lyon"), (11, "Lyon")]
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            "kayak.collecte.hotels_osm",
            logging.WARNING,
            "Nice : aucune archive, ville ignorée (mode hors ligne)",
        )
    ]
    assert sorted(lake.lister("raw/")) == avant
    assert pauses == []


def test_hors_ligne_archive_plafonnee_est_signalee_mais_utilisee_et_reste_inchangee(
    tmp_path, caplog, pauses
):
    """Hors ligne, une archive de 40 éléments sans marque est relue telle quelle, avec alerte.

    L'avertissement nomme le fichier et le plafond ; le fichier n'est pas réécrit.
    """
    lake = _lake(tmp_path)
    chemin = _archiver(lake, 1, {"elements": [_noeud(i) for i in range(40)]})
    contenu_avant = lake.lire(chemin)
    caplog.set_level(logging.INFO)
    hotels = construire_hotels(_coordonnees((1, "Nice", 43.7, 7.27)), lake, None)
    assert [h["hotel_id"] for h in hotels] == list(range(40))
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.WARNING,
            f"{chemin} : archive issue de l'ancienne requête plafonnée à 40 éléments, "
            "possiblement tronquée",
        )
    ]
    assert lake.lire(chemin) == contenu_avant
    assert pauses == []


def test_hors_ligne_archive_complete_ne_journalise_rien_et_reste_inchangee(tmp_path, caplog):
    """Une archive de 39 éléments était complète : aucune alerte, aucune réécriture."""
    lake = _lake(tmp_path)
    chemin = _archiver(lake, 1, {"elements": [_noeud(i) for i in range(39)]})
    contenu_avant = lake.lire(chemin)
    caplog.set_level(logging.INFO)
    hotels = construire_hotels(_coordonnees((1, "Nice", 43.7, 7.27)), lake, None)
    assert [h["hotel_id"] for h in hotels] == list(range(39))
    assert caplog.records == []
    assert lake.lire(chemin) == contenu_avant


def test_archive_deja_marquee_n_est_pas_redemandee_ni_reecrite_et_aucune_pause_n_est_prise(
    tmp_path, pauses
):
    """Avec une session, une archive portant la marque de la requête actuelle est réutilisée.

    Aucune requête, aucune pause d'1 s, fichier inchangé.
    """
    lake = _lake(tmp_path)
    chemin = _archiver(lake, 1, {MARQUE_REQUETE: "q", "elements": [_noeud(i) for i in range(60)]})
    contenu_avant = lake.lire(chemin)
    session = _Session()
    hotels = construire_hotels(_coordonnees((1, "Nice", 43.7, 7.27)), lake, session)
    assert [h["hotel_id"] for h in hotels] == list(range(60))
    assert session.appels == []
    assert lake.lire(chemin) == contenu_avant
    assert pauses == []


def test_archive_plafonnee_remplacee_en_ligne_marquee_et_archive_voisine_inchangee(
    tmp_path, caplog, pauses
):
    """En ligne, une archive de 40 éléments sans marque est signalée puis REMPLACÉE.

    La nouvelle requête vise les coordonnées de la ville ; la réponse archivée porte la marque
    (la requête elle-même) ; une pause d'1 s ménage ensuite l'API publique. L'archive d'une
    autre ville, déjà marquée, n'est ni redemandée ni réécrite.
    """
    lake = _lake(tmp_path)
    chemin = _archiver(lake, 1, {"elements": [_noeud(i) for i in range(40)]})
    chemin_voisin = _archiver(lake, 2, {MARQUE_REQUETE: "q", "elements": [_noeud(900)]})
    voisin_avant = lake.lire(chemin_voisin)
    complete = {"elements": [_noeud(i, nom="Hôtel é") for i in range(57)]}
    session = _Session(_Reponse(200, complete))
    caplog.set_level(logging.INFO)
    coordonnees = _coordonnees((1, "Nice", 43.7, 7.27), (2, "Lyon", 45.7, 4.8))
    hotels = construire_hotels(coordonnees, lake, session)
    requete = requete_overpass(43.7, 7.27)
    assert [appel["data"] for appel in session.appels] == [{"data": requete}]
    assert [h["hotel_id"] for h in hotels] == [*range(57), 900]
    assert lake.lire(chemin_voisin) == voisin_avant
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    brut = lake.lire(chemin)
    archive = json.loads(brut)
    assert archive[MARQUE_REQUETE] == requete
    assert [e["id"] for e in archive["elements"]] == list(range(57))
    assert "Hôtel é".encode() in brut
    assert pauses == [0, 1.0]


def test_ville_sans_archive_est_interrogee_et_archivee_a_deux_chiffres_sans_toucher_la_voisine(
    tmp_path, pauses
):
    """Sans archive, la ville est interrogée avec SA position ; la réponse est archivée marquée.

    L'identifiant 7 donne ``ville_07.json``. L'archive déjà marquée d'une autre ville reste
    inchangée et n'est pas redemandée.
    """
    lake = _lake(tmp_path)
    chemin_voisin = _archiver(lake, 8, {MARQUE_REQUETE: "q", "elements": []})
    voisin_avant = lake.lire(chemin_voisin)
    session = _Session(_Reponse(200, {"elements": [_noeud(3)]}))
    construire_hotels(_coordonnees((7, "Sète", 43.4, 3.7), (8, "Lyon", 45.7, 4.8)), lake, session)
    assert sorted(lake.lister("raw/osm/")) == [
        f"raw/osm/date={DATE}/ville_07.json",
        f"raw/osm/date={DATE}/ville_08.json",
    ]
    assert lake.lire(chemin_voisin) == voisin_avant
    assert [appel["data"] for appel in session.appels] == [
        {"data": requete_overpass(43.4, 3.7)}
    ]
    archive = json.loads(lake.lire(f"raw/osm/date={DATE}/ville_07.json"))
    assert archive[MARQUE_REQUETE] == requete_overpass(43.4, 3.7)
    assert pauses == [0, 1.0]


def test_echec_d_overpass_remonte_sans_archive_ni_pause_d_une_seconde(tmp_path, pauses):
    """Si Overpass reste indisponible, l'erreur remonte : aucune archive vide n'est écrite."""
    lake = _lake(tmp_path)
    session = _Session(*[_Reponse(503)] * 8)
    with pytest.raises(RuntimeError, match="Overpass indisponible après plusieurs essais"):
        construire_hotels(_coordonnees((1, "Nice", 43.7, 7.27)), lake, session)
    assert lake.lister("raw/osm/") == []
    assert 1.0 not in pauses


def test_archive_illisible_leve_une_erreur_au_lieu_d_etre_ignoree(tmp_path):
    """Une archive tronquée n'est pas devinée : l'erreur de JSON remonte, sans conversion."""
    lake = _lake(tmp_path)
    lake.ecrire(f"raw/osm/date={DATE}/ville_01.json", '{"elements": [{"id"')
    with pytest.raises(json.JSONDecodeError):
        construire_hotels(_coordonnees((1, "Nice", 43.7, 7.27)), lake, None)


def test_villes_sans_coordonnees_sont_ignorees_sans_requete_ni_trace_et_le_tableau_reste_inchange(
    tmp_path, caplog, pauses
):
    """Latitude OU longitude absente : la ville est écartée en amont, sans trace ni requête.

    Le tableau de coordonnées de l'appelant n'est pas modifié."""
    lake = _lake(tmp_path)
    session = _Session(_Reponse(200, {"elements": [_noeud(1)]}))
    caplog.set_level(logging.INFO)
    coordonnees = _coordonnees(
        (1, "SansLat", float("nan"), 7.0),
        (2, "SansLon", 43.0, None),
        (3, "Valide", 44.0, 3.0),
    )
    avant = coordonnees.copy(deep=True)
    hotels = construire_hotels(coordonnees, lake, session)
    pd.testing.assert_frame_equal(coordonnees, avant)
    assert [(h["ville"], h["id_ville"]) for h in hotels] == [("Valide", 3)]
    assert [appel["data"] for appel in session.appels] == [{"data": requete_overpass(44.0, 3.0)}]
    assert lake.lister("raw/osm/") == [f"raw/osm/date={DATE}/ville_03.json"]
    assert caplog.records == []


def test_hotels_gardent_l_ordre_des_villes_ignorent_les_sans_nom_et_archives_inchangees(
    tmp_path,
):
    """L'ordre est celui du tableau (3, puis 1, puis 2), pas celui des identifiants.

    Les éléments sans nom sont écartés ; une archive sans clé ``elements`` ne donne aucun hôtel.
    Les archives relues hors ligne ne sont pas réécrites.
    """
    lake = _lake(tmp_path)
    chemins = [
        _archiver(lake, 3, {"elements": [_noeud(30), _noeud(31, nom=None), _noeud(32)]}),
        _archiver(lake, 1, {"elements": [_noeud(10)]}),
        _archiver(lake, 2, {}),
    ]
    contenus_avant = [lake.lire(chemin) for chemin in chemins]
    coordonnees = _coordonnees((3, "C", 1.0, 1.0), (1, "A", 1.0, 1.0), (2, "B", 1.0, 1.0))
    hotels = construire_hotels(coordonnees, lake, None)
    assert [(h["id_ville"], h["ville"], h["hotel_id"], h["date_collecte"]) for h in hotels] == [
        (3, "C", 30, DATE),
        (3, "C", 32, DATE),
        (1, "A", 10, DATE),
    ]
    assert [lake.lire(chemin) for chemin in chemins] == contenus_avant


# --- ecrire_hotels et collecter_hotels_osm ----------------------------------------------------


def test_ecrire_hotels_une_ligne_par_hotel_en_utf8_fichier_vide_sans_hotel_et_liste_inchangee(
    tmp_path,
):
    """``hotels.jsonl`` : une ligne par hôtel, accents non échappés, saut de ligne final.

    Sans hôtel, le fichier existe mais est vide (zéro octet, pas un saut de ligne), pour que
    Spark le relise. La liste d'hôtels reçue n'est pas modifiée.
    """
    lake = _lake(tmp_path)
    hotels = [{"hotel_id": 1, "nom": "Hôtel é"}, {"hotel_id": 2, "nom": "B"}]
    avant = copy.deepcopy(hotels)
    ecrire_hotels(lake, hotels)
    assert hotels == avant
    assert lake.lister("raw/osm/") == [f"raw/osm/date={DATE}/hotels.jsonl"]
    brut = lake.lire(f"raw/osm/date={DATE}/hotels.jsonl")
    assert brut == '{"hotel_id": 1, "nom": "Hôtel é"}\n{"hotel_id": 2, "nom": "B"}\n'.encode()
    ecrire_hotels(lake, [])
    assert lake.lire(f"raw/osm/date={DATE}/hotels.jsonl") == b""


def test_collecte_en_ligne_identifie_le_robot_ecrit_les_fichiers_et_laisse_les_villes_inchangees(
    tmp_path, monkeypatch
):
    """La session de collecte porte le User-Agent du projet ; archives et ``hotels.jsonl`` suivent.

    Le tableau rendu a les 16 colonnes du schéma commun, dans l'ordre ; le tableau des villes
    reçu n'est pas modifié.
    """
    session = _Session(_Reponse(200, {"elements": [_noeud(1)]}))
    monkeypatch.setattr(requests, "Session", lambda: session)
    lake = _lake(tmp_path)
    villes = _coordonnees((1, "Nice", 43.7, 7.27))
    avant = villes.copy(deep=True)
    resultat = collecter_hotels_osm(villes, lake)
    pd.testing.assert_frame_equal(villes, avant)
    assert session.headers == {"User-Agent": config.USER_AGENT}
    assert resultat.columns.tolist() == CHAMPS_HOTEL
    assert resultat["hotel_id"].tolist() == [1]
    assert sorted(lake.lister("raw/osm/")) == [
        f"raw/osm/date={DATE}/hotels.jsonl",
        f"raw/osm/date={DATE}/ville_01.json",
    ]
    lignes = lake.lire(f"raw/osm/date={DATE}/hotels.jsonl").decode("utf-8").splitlines()
    assert [json.loads(ligne)["hotel_id"] for ligne in lignes] == [1]


def test_collecte_sans_aucune_coordonnee_n_emet_aucune_requete_et_laisse_les_villes_inchangees(
    tmp_path, monkeypatch
):
    """Aucune ville géocodée : aucune requête ; fichier des hôtels vide, tableau vide.

    Le tableau des villes reçu n'est pas modifié.
    """
    session = _Session()
    monkeypatch.setattr(requests, "Session", lambda: session)
    lake = _lake(tmp_path)
    villes = _coordonnees((1, "Nice", None, None))
    avant = villes.copy(deep=True)
    resultat = collecter_hotels_osm(villes, lake)
    pd.testing.assert_frame_equal(villes, avant)
    assert session.appels == []
    assert resultat.empty
    assert lake.lister("raw/osm/") == [f"raw/osm/date={DATE}/hotels.jsonl"]
    assert lake.lire(f"raw/osm/date={DATE}/hotels.jsonl") == b""


# --- diagnostic_troncature --------------------------------------------------------------------


def test_diagnostic_d_une_zone_vide_compte_zero_partout_et_rappelle_le_plafond(tmp_path):
    """Sans aucune archive, tous les comptes sont nuls ; le plafond de 40 est rappelé."""
    assert diagnostic_troncature(_lake(tmp_path)) == {
        "villes": 0,
        "archives_ancienne_requete": 0,
        "villes_plafonnees": 0,
        "plafond": 40,
    }


def test_diagnostic_ignore_les_fichiers_qui_ne_sont_pas_des_archives_de_ville(tmp_path):
    """Seuls ``ville_<nombre>.json`` comptent : hotels.jsonl, .tmp, notes et lettres sont ignorés.

    Les archives d'une autre date ne sont pas comptées non plus, et le lake reste inchangé.
    """
    lake = _lake(tmp_path)
    plafonnee = json.dumps({"elements": [_noeud(i) for i in range(40)]})
    lake.ecrire(f"raw/osm/date={DATE}/ville_7.json", plafonnee)
    lake.ecrire(f"raw/osm/date={DATE}/hotels.jsonl", plafonnee)
    lake.ecrire(f"raw/osm/date={DATE}/ville_ab.json", plafonnee)
    lake.ecrire(f"raw/osm/date={DATE}/ville_01.json.tmp", plafonnee)
    lake.ecrire(f"raw/osm/date={DATE}/notes.txt", "texte")
    lake.ecrire("raw/osm/date=2020-01-01/ville_02.json", plafonnee)
    avant = sorted(lake.lister("raw/"))
    assert diagnostic_troncature(lake) == {
        "villes": 1,
        "archives_ancienne_requete": 1,
        "villes_plafonnees": 1,
        "plafond": 40,
    }
    assert sorted(lake.lister("raw/")) == avant


@pytest.mark.parametrize(
    ("reponse", "anciennes", "plafonnees"),
    [
        ({"elements": [1] * 39}, 1, 0),
        ({"elements": [1] * 40}, 1, 1),
        ({"elements": [1] * 41}, 1, 1),
        ({}, 1, 0),
        ({MARQUE_REQUETE: "q", "elements": [1] * 100}, 0, 0),
    ],
)
def test_diagnostic_aux_bornes_du_plafond(tmp_path, reponse, anciennes, plafonnees):
    """Plafonnée dès 40 éléments sans marque ; la marque ou moins de 40 éléments la dispense."""
    lake = _lake(tmp_path)
    lake.ecrire(f"raw/osm/date={DATE}/ville_01.json", json.dumps(reponse))
    assert diagnostic_troncature(lake) == {
        "villes": 1,
        "archives_ancienne_requete": anciennes,
        "villes_plafonnees": plafonnees,
        "plafond": 40,
    }


def test_diagnostic_d_une_archive_illisible_leve_une_erreur(tmp_path):
    """Une archive qui n'est pas du JSON fait échouer le diagnostic au lieu de l'ignorer."""
    lake = _lake(tmp_path)
    lake.ecrire(f"raw/osm/date={DATE}/ville_01.json", "pas du json")
    with pytest.raises(json.JSONDecodeError):
        diagnostic_troncature(lake)


# --- reconvertir_depuis_archive ---------------------------------------------------------------


def _lake_reconvertible(dossier, date=DATE) -> DataLake:
    """Lake avec le CSV de géocodage (3 villes, dont une sans archive) et deux archives."""
    lake = _lake(dossier)
    coordonnees = _coordonnees(
        (1, "Nice", 43.7, 7.27), (2, "Lyon", 45.7, 4.8), (3, "Inconnue", None, None)
    )
    lake.ecrire(f"raw/geocodage/date={date}/coordonnees.csv", coordonnees.to_csv(index=False))
    _archiver(lake, 1, {"elements": [_noeud(10, **{"addr:city": "Nice"})]}, date)
    _archiver(lake, 2, {"elements": [_noeud(20), _noeud(21, nom=None)]}, date)
    return lake


def test_reconversion_sans_fichier_de_geocodage_leve_une_erreur_et_n_ecrit_rien(tmp_path):
    """Sans le CSV de géocodage de la date, FileNotFoundError ; aucun hotels.jsonl n'est créé."""
    lake = _lake(tmp_path)
    _archiver(lake, 1, {"elements": [_noeud(10)]})
    with pytest.raises(FileNotFoundError, match="coordonnees.csv"):
        reconvertir_depuis_archive(lake)
    assert lake.lister("raw/osm/") == [f"raw/osm/date={DATE}/ville_01.json"]


def test_reconversion_rejoue_les_archives_sans_les_reecrire_ni_ouvrir_de_session(
    tmp_path, monkeypatch
):
    """Hors ligne : seules les villes archivées sont converties ; les archives restent inchangées.

    La ville sans coordonnées et celle sans archive sont ignorées ; aucune session n'est créée.
    """

    def interdit(*_args, **_kwargs):
        raise AssertionError("aucune session réseau n'est permise en reconversion")

    monkeypatch.setattr(requests, "Session", interdit)
    lake = _lake_reconvertible(tmp_path)
    archives_avant = {
        chemin: lake.lire(chemin) for chemin in lake.lister(f"raw/osm/date={DATE}/")
    }
    resultat = reconvertir_depuis_archive(lake)
    assert resultat["hotel_id"].tolist() == [10, 20]
    assert resultat["commune"].isna().tolist() == [False, True]
    assert resultat["commune"].iloc[0] == "Nice"
    assert resultat.columns.tolist() == CHAMPS_HOTEL
    for chemin, contenu in archives_avant.items():
        assert lake.lire(chemin) == contenu
    ecrit = lake.lire(f"raw/osm/date={DATE}/hotels.jsonl").decode("utf-8").splitlines()
    assert [json.loads(ligne)["hotel_id"] for ligne in ecrit] == [10, 20]


# --- main -------------------------------------------------------------------------------------


def _lancer_main(monkeypatch, tmp_path, *arguments):
    """Appelle ``main`` avec ces arguments, sur un data lake local de ``tmp_path``."""
    monkeypatch.setattr(sys, "argv", ["hotels_osm", *arguments])
    monkeypatch.setattr(hotels_osm, "DataLake", lambda: DataLake(None, racine_locale=tmp_path))
    hotels_osm.main()


def test_main_sans_reconvertir_quitte_avec_un_code_non_nul_et_ne_lit_rien(
    monkeypatch, tmp_path, capsys
):
    """L'option ``--reconvertir`` est obligatoire : sans elle, argparse quitte avec le code 2.

    Rien n'est lu ni écrit dans le data lake ; le message d'usage cite l'option manquante.
    """
    lake = _lake_reconvertible(tmp_path)
    avant = sorted(lake.lister("raw/"))
    with pytest.raises(SystemExit) as sortie:
        _lancer_main(monkeypatch, tmp_path, "--date", DATE)
    assert sortie.value.code == 2
    assert "--reconvertir" in capsys.readouterr().err
    assert sorted(lake.lister("raw/")) == avant


@pytest.mark.parametrize(
    "arguments", [["--reconvertir", "--inconnue"], ["--reconvertir", "--date"]]
)
def test_main_refuse_une_option_inconnue_ou_une_date_sans_valeur(monkeypatch, tmp_path, arguments):
    """Une option inconnue, ou ``--date`` sans valeur, arrête le programme (code 2)."""
    _lake_reconvertible(tmp_path)
    with pytest.raises(SystemExit) as sortie:
        _lancer_main(monkeypatch, tmp_path, *arguments)
    assert sortie.value.code == 2


def test_main_aide_decrit_les_options_et_quitte_avec_le_code_zero(monkeypatch, tmp_path, capsys):
    """``--help`` affiche la description et les deux options, puis quitte proprement (code 0)."""
    with pytest.raises(SystemExit) as sortie:
        _lancer_main(monkeypatch, tmp_path, "--help")
    assert sortie.value.code == 0
    aide = " ".join(capsys.readouterr().out.split())
    assert "Source de repli pour les hôtels : OpenStreetMap, via l'API Overpass." in aide
    assert "Pourquoi ce module existe" not in aide  # la description est le 1er paragraphe seul
    assert "--reconvertir" in aide
    assert "--date DATE date de collecte AAAA-MM-JJ" in aide
    assert "XX" not in aide


def test_main_avec_date_positionne_la_variable_d_environnement_et_laisse_les_archives_inchangees(
    monkeypatch, tmp_path, capsys
):
    """``--date`` fixe KAYAK_DATE_COLLECTE (on relit donc les archives de CETTE date) et imprime.

    Le texte rappelle le nombre d'hôtels reconstruits et l'absence d'appel réseau. Les archives
    brutes ne sont pas réécrites.
    """
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "1999-01-01")
    lake = _lake_reconvertible(tmp_path, DATE)
    archives = lake.lister(f"raw/osm/date={DATE}/")
    contenus_avant = {chemin: lake.lire(chemin) for chemin in archives}
    _lancer_main(monkeypatch, tmp_path, "--reconvertir", "--date", DATE)
    assert os.environ["KAYAK_DATE_COLLECTE"] == DATE
    for chemin, contenu in contenus_avant.items():
        assert lake.lire(chemin) == contenu
    assert capsys.readouterr().out == (
        "2 hôtels reconstruits depuis les réponses archivées (aucun appel réseau)\n"
    )


@pytest.mark.parametrize("arguments", [["--reconvertir"], ["--reconvertir", "--date", ""]])
def test_main_sans_date_ou_avec_date_vide_laisse_la_variable_d_environnement_inchangee(
    monkeypatch, tmp_path, capsys, arguments
):
    """Sans date (ou date vide), la variable d'environnement n'est pas touchée.

    Les archives relues sont celles de la date déjà fixée dans l'environnement.
    """
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "1999-01-01")
    _lake_reconvertible(tmp_path, "1999-01-01")
    _lancer_main(monkeypatch, tmp_path, *arguments)
    assert os.environ["KAYAK_DATE_COLLECTE"] == "1999-01-01"
    assert capsys.readouterr().out.startswith("2 hôtels reconstruits")


def test_main_avec_une_date_sans_archive_leve_une_erreur_au_lieu_d_imprimer_un_bilan(
    monkeypatch, tmp_path, capsys
):
    """Si la date demandée n'a pas de géocodage archivé, FileNotFoundError ; rien n'est affiché."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "1999-01-01")
    _lake_reconvertible(tmp_path, "1999-01-01")
    with pytest.raises(FileNotFoundError, match="coordonnees.csv"):
        _lancer_main(monkeypatch, tmp_path, "--reconvertir", "--date", "2000-02-02")
    assert capsys.readouterr().out == ""


def test_lance_comme_script_reconvertit_imprime_le_bilan_et_laisse_les_archives_inchangees(
    monkeypatch, tmp_path, capsys
):
    """``python -m kayak.collecte.hotels_osm --reconvertir --date ...`` exécute ``main``.

    Les archives brutes ne sont pas réécrites.
    """
    lake = _lake_reconvertible(tmp_path, DATE)
    archives = lake.lister(f"raw/osm/date={DATE}/")
    contenus_avant = {chemin: lake.lire(chemin) for chemin in archives}
    monkeypatch.setattr(sys, "argv", ["hotels_osm", "--reconvertir", "--date", DATE])
    monkeypatch.setattr(module_lake, "DataLake", lambda: DataLake(None, racine_locale=tmp_path))
    monkeypatch.delitem(sys.modules, "kayak.collecte.hotels_osm")
    runpy.run_module("kayak.collecte.hotels_osm", run_name="__main__")
    for chemin, contenu in contenus_avant.items():
        assert lake.lire(chemin) == contenu
    assert capsys.readouterr().out == (
        "2 hôtels reconstruits depuis les réponses archivées (aucun appel réseau)\n"
    )


# --- compléments : formats persistants, colonnes annexes et journalisation ---------------------


def test_cle_litterale_kayak_requete_des_archives_deja_ecrites_est_reconnue_et_reste_inchangee(
    tmp_path,
):
    """Les archives déjà sur disque portent la clé littérale « kayak_requete » : elle fait foi.

    Renommer cette clé rendrait suspectes (donc redemandées) toutes les archives existantes.
    """
    reponse = {"kayak_requete": "q", "elements": [1] * 50}
    avant = copy.deepcopy(reponse)
    assert archive_reutilisable(reponse) is True
    assert reponse == avant
    lake = _lake(tmp_path)
    lake.ecrire(f"raw/osm/date={DATE}/ville_01.json", json.dumps(reponse))
    assert diagnostic_troncature(lake)["archives_ancienne_requete"] == 0


def test_seules_les_coordonnees_comptent_une_colonne_annexe_vide_n_ecarte_pas_la_ville(
    tmp_path,
):
    """Une valeur manquante dans une AUTRE colonne du tableau n'écarte pas la ville.

    Seules latitude et longitude décident ; le tableau reçu reste inchangé.
    """
    lake = _lake(tmp_path)
    _archiver(lake, 1, {"elements": [_noeud(10)]})
    coordonnees = _coordonnees((1, "Nice", 43.7, 7.27))
    coordonnees["commentaire"] = [None]
    avant = coordonnees.copy(deep=True)
    hotels = construire_hotels(coordonnees, lake, None)
    pd.testing.assert_frame_equal(coordonnees, avant)
    assert [h["hotel_id"] for h in hotels] == [10]


def test_main_configure_la_journalisation_au_niveau_info_avec_horodatage_niveau_et_message(
    monkeypatch, tmp_path, capsys
):
    """``main`` installe une journalisation INFO au format « horodatage NIVEAU message ».

    Le test retire les gestionnaires de pytest le temps de l'appel, puis restitue l'état d'origine
    du journal racine.
    """
    _lake_reconvertible(tmp_path, DATE)
    niveau_avant = logging.root.level
    monkeypatch.setattr(logging.root, "handlers", [])
    try:
        _lancer_main(monkeypatch, tmp_path, "--reconvertir", "--date", DATE)
        capsys.readouterr()
        logging.getLogger("essai.journal").info("bonjour")
        logging.getLogger("essai.journal").debug("trop bavard")
        sortie = capsys.readouterr().err
    finally:
        logging.root.setLevel(niveau_avant)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO bonjour\n", sortie)


def test_interroger_sans_aucun_serveur_ne_poste_rien_et_dit_qu_aucune_erreur_n_a_eu_lieu(
    pauses, monkeypatch
):
    """Liste de serveurs vide : aucune requête, et RuntimeError sans erreur réseau à citer.

    Le message cite la dernière erreur rencontrée ; sans aucun essai, il n'y en a pas : il le
    dit par « None », au lieu d'inventer une erreur.
    """
    monkeypatch.setattr(hotels_osm, "SERVEURS_OVERPASS", [])
    session = _Session()
    with pytest.raises(RuntimeError) as erreur:
        interroger(session, "q")
    assert str(erreur.value) == "Overpass indisponible après plusieurs essais : None"
    assert session.appels == [] and pauses == []
