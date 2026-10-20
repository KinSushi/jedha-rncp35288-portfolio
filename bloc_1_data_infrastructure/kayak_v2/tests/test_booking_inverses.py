# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests INVERSES du robot Booking : ce qu'il refuse, ignore, écarte ou laisse intact.

Complète tests/test_booking.py (dont les cas d'extraction sont circulaires : le HTML est
fabriqué avec le marqueur que le code cherche). Ici, chaque test prouve un REFUS ou une absence
d'effet : un robots.txt qui interdit arrête la collecte avant tout sous-processus ; un contrôle
anti-robot (202, AwsWafIntegration) ou un statut 4xx/5xx n'est jamais extrait ; les annonces
privées ou de test n'entrent pas dans le résultat (RGPD) ; une réponse ou une requête
incomplète lève une erreur au lieu d'inventer une ville ; un sous-processus qui échoue ne
laisse rien dans le data lake.

Aucun vrai robot Scrapy, aucun vrai sous-processus, aucun réseau : ``subprocess.run``,
``archiver_robots`` et ``CrawlerProcess`` sont remplacés par des doublures qui enregistrent
leurs appels.
"""

import asyncio
import copy
import json
import logging
import runpy
import subprocess
import sys
import warnings
from pathlib import Path

import pandas as pd
import pytest
import scrapy.crawler
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse, Request
from twisted.python.failure import Failure

from kayak import config
from kayak.collecte import booking_spider
from kayak.collecte.booking_spider import (
    URL_RECHERCHE,
    BookingHotelsSpider,
    _executer_robot,
    extraire_hotels,
    lancer_collecte_booking,
    url_recherche,
)
from kayak.stockage.data_lake import DataLake

DATE = "2026-10-10"
ROBOTS_ACCUEILLANT = "User-agent: *\nDisallow: /autre-chemin\n"
ROBOTS_FERME = "User-agent: *\nDisallow: /searchresults\n"


@pytest.fixture(autouse=True)
def _date_figee(monkeypatch):
    """La date de collecte est fixée : aucun test ne dépend de l'horloge."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", DATE)


# --- fabrication de pages et de spiders -------------------------------------------------------


def _annonce(identifiant=1, nom="Hôtel A", description="Une description", **basic):
    """Une annonce « SearchResultProperty » complète ; ``basic`` surcharge basicPropertyData.

    Le premier champ est ``__typename`` : c'est le marqueur exact que l'extraction recherche.
    """
    donnees = {
        "id": identifiant,
        "isTestProperty": False,
        "pageName": f"hotel-{identifiant}",
        "location": {
            "countryCode": "fr",
            "latitude": 43.1,
            "longitude": 5.2,
            "address": "1 rue du Port",
        },
        "reviews": {"showScore": True, "totalScore": 8.4, "reviewsCount": 56},
        "starRating": {"value": 4},
    }
    donnees.update(basic)
    return {
        "__typename": "SearchResultProperty",
        "basicPropertyData": donnees,
        "displayName": {"text": nom},
        "description": {"text": description},
    }


def _html(resultats: list) -> str:
    """HTML minimal embarquant le JSON compact « results » que le code sait lire."""
    corps = json.dumps({"results": resultats}, separators=(",", ":"), ensure_ascii=False)
    return f"<html><body><script>window.donnees = {corps};</script></body></html>"


def _extraire(html: str):
    return extraire_hotels(html, id_ville=9, ville="Aix", date_collecte=DATE)


def _spider(sortie="inutilise") -> BookingHotelsSpider:
    villes = [
        {"id_ville": 1, "ville": "Paris", "date_collecte": DATE},
        {"id_ville": 2, "ville": "Lyon", "date_collecte": DATE},
    ]
    return BookingHotelsSpider(villes=villes, sortie=sortie)


def _reponse(statut: int, corps: str, ville="Paris", id_ville=1, date=DATE) -> HtmlResponse:
    """Réponse HTTP factice rattachée à sa requête, avec les métadonnées de Scrapy."""
    url = url_recherche(ville)
    requete = Request(url, meta={"id_ville": id_ville, "ville": ville, "date_collecte": date})
    return HtmlResponse(
        url, status=statut, body=corps.encode("utf-8"), encoding="utf-8", request=requete
    )


def _installer_voisin(spider: BookingHotelsSpider) -> dict:
    """Consigne une autre ville au rapport et renvoie une copie pour comparer après coup."""
    spider.rapport["Lyon"] = {"statut_http": 200, "issue": "ok", "nb_hotels": 3, "octets": 99}
    return copy.deepcopy(spider.rapport["Lyon"])


def _extraction_interdite(*_args, **_kwargs):
    raise AssertionError("l'extraction ne doit pas être appelée pour cette réponse")


# --- extraire_hotels --------------------------------------------------------------------------


def test_extraire_hotels_json_reprend_chaque_champ_et_construit_l_url():
    """Tous les champs de l'annonce sont repris au bon endroit, avec l'URL canonique.

    Zéro est une valeur, pas une absence : une note, un nombre d'avis ou une classe d'étoiles
    à 0 sont repris tels quels.
    """
    hotels = _extraire(_html([_annonce(identifiant=111, nom="Les Oliviers")]))
    assert hotels == [
        {
            "hotel_id": 111,
            "nom": "Les Oliviers",
            "url": "https://www.booking.com/hotel/fr/hotel-111.fr.html",
            "latitude": 43.1,
            "longitude": 5.2,
            "adresse": "1 rue du Port",
            "note": 8.4,
            "nb_avis": 56,
            "etoiles": 4,
            "description": "Une description",
            "id_ville": 9,
            "ville": "Aix",
            "date_collecte": DATE,
            "source": "booking",
        }
    ]
    zeros = _annonce(
        2,
        reviews={"showScore": True, "totalScore": 0, "reviewsCount": 0},
        starRating={"value": 0},
    )
    hotel_zero = _extraire(_html([zeros]))[0]
    assert (hotel_zero["note"], hotel_zero["nb_avis"], hotel_zero["etoiles"]) == (0, 0, 0)


def test_annonces_privees_de_test_ou_mal_formees_sont_sans_effet_sur_le_resultat():
    """Ajouter des annonces à écarter ne change rien à la liste extraite.

    Sont écartées : la propriété de test, l'hôte particulier (RGPD, ``PRIVATE``) et les
    éléments qui ne sont pas des objets (texte, null, nombre, liste). Un hôte
    ``PROFESSIONAL`` ou une propriété à ``isTestProperty`` faux restent retenus.
    """
    gardees = [_annonce(1, "G1"), _annonce(2, "G2")]
    professionnelle = _annonce(3, "G3")
    professionnelle["hostTraderLabel"] = "PROFESSIONAL"
    privee = _annonce(4, "Privé")
    privee["hostTraderLabel"] = "PRIVATE"
    de_test = _annonce(5, "Test", isTestProperty=True)
    avec = _extraire(
        _html([gardees[0], privee, de_test, "texte", None, 42, [1], professionnelle, gardees[1]])
    )
    sans = _extraire(_html([gardees[0], professionnelle, gardees[1]]))
    assert [h["nom"] for h in avec] == ["G1", "G3", "G2"]
    assert avec == sans


@pytest.mark.parametrize(
    ("avis", "reference"),
    [
        ({"showScore": False, "totalScore": 8.7, "reviewsCount": 10}, {"reviewsCount": 10}),
        ({"totalScore": 8.7, "reviewsCount": 10}, {"reviewsCount": 10}),
        ({"showScore": True, "reviewsCount": 10}, {"reviewsCount": 10}),
        ({"showScore": True, "totalScore": None, "reviewsCount": 10}, {"reviewsCount": 10}),
        ({"showScore": False, "totalScore": 9.9}, {}),
        ({}, {}),
        (None, {}),
    ],
)
def test_score_masque_ou_incomplet_est_sans_effet_et_la_note_reste_none(avis, reference):
    """Note publiée seulement si ``showScore`` est vrai ET ``totalScore`` renseigné.

    Un score que le site masque (``showScore`` faux ou absent), ou un drapeau sans score, est
    sans effet : l'hôtel obtenu est identique à celui d'une annonce sans aucun score, et sa
    note vaut None. Le nombre d'avis, lui, est repris indépendamment du score.
    """
    hotel = _extraire(_html([_annonce(reviews=avis)]))[0]
    sans_score = _extraire(_html([_annonce(reviews=reference)]))[0]
    assert hotel == sans_score
    assert hotel["note"] is None
    assert hotel["nb_avis"] == reference.get("reviewsCount")


AUCUNE_VALEUR = {
    "hotel_id": None,
    "nom": None,
    "url": None,
    "latitude": None,
    "longitude": None,
    "adresse": None,
    "note": None,
    "nb_avis": None,
    "etoiles": None,
    "description": None,
}


@pytest.mark.parametrize(
    ("annonce", "champs_attendus"),
    [
        (_annonce(1, starRating=None), {"etoiles": None}),
        (_annonce(1, starRating={}), {"etoiles": None}),
        (_annonce(1, starRating={"value": 4}), {"etoiles": 4}),
        (_annonce(1, pageName=None), {"url": None}),
        (_annonce(1, pageName=""), {"url": None}),
        (
            _annonce(1, location={"latitude": 1.5, "longitude": 2.5, "address": "Quai"}),
            {"url": None, "latitude": 1.5, "longitude": 2.5, "adresse": "Quai"},
        ),
        (
            _annonce(1, location={"countryCode": "", "latitude": 1.5}),
            {"url": None, "latitude": 1.5, "longitude": None, "adresse": None},
        ),
        (
            _annonce(1, location=None),
            {"url": None, "latitude": None, "longitude": None, "adresse": None},
        ),
        (
            _annonce(1, countryCode="de"),
            {"url": "https://www.booking.com/hotel/fr/hotel-1.fr.html"},
        ),
        ({"__typename": "SearchResultProperty"}, AUCUNE_VALEUR),
        (
            {"__typename": "SearchResultProperty", "basicPropertyData": {"id": 5}},
            {**AUCUNE_VALEUR, "hotel_id": 5},
        ),
    ],
)
def test_champ_absent_ou_vide_donne_none_sans_valeur_inventee(annonce, champs_attendus):
    """Un champ manquant donne None ; sans pays ni nom de page, aucune URL n'est fabriquée.

    Le code pays se lit dans ``location`` : un ``countryCode`` posé ailleurs est sans effet.
    Une annonce dépouillée donne un hôtel vide, mais garde le contexte de la ville.
    """
    hotel = _extraire(_html([annonce]))[0]
    for cle, valeur in champs_attendus.items():
        assert hotel[cle] == valeur
    assert (hotel["id_ville"], hotel["ville"], hotel["date_collecte"]) == (9, "Aix", DATE)
    assert hotel["source"] == "booking"


@pytest.mark.parametrize("html", [b"<html></html>", None, 42])
def test_extraire_hotels_refuse_un_contenu_qui_n_est_pas_du_texte(html):
    """Octets, None ou nombre à la place du texte HTML lèvent une erreur : rien n'est deviné."""
    with pytest.raises((TypeError, AttributeError), match="bytes-like|find"):
        _extraire(html)


@pytest.mark.parametrize(
    ("corps_carte", "attendu"),
    [
        # note à virgule, texte autour du nombre
        (
            '<a data-testid="title-link" href="https://b.test/h/a.fr.html?aid=1">'
            '<span data-testid="title">  Maison  </span></a>'
            '<div data-testid="review-score">Avec une note de 8,9 Génial</div>',
            {"nom": "Maison", "url": "https://b.test/h/a.fr.html", "note": 8.9},
        ),
        # note entière, fragment dans l'URL
        (
            '<a data-testid="title-link" href="https://b.test/h/b.fr.html#map">'
            '<span data-testid="title">B</span></a>'
            '<div data-testid="review-score">9</div>',
            {"nom": "B", "url": "https://b.test/h/b.fr.html", "note": 9.0},
        ),
        # paramètre puis fragment : le premier séparateur rencontré coupe
        (
            '<a data-testid="title-link" href="/h/c.fr.html?x=1#y">'
            '<span data-testid="title">C</span></a>',
            {"nom": "C", "url": "/h/c.fr.html", "note": None},
        ),
        # pas de lien : ni URL ni invention
        (
            '<span data-testid="title">D</span><div data-testid="review-score">7.5</div>',
            {"nom": "D", "url": None, "note": 7.5},
        ),
        # lien sans adresse
        (
            '<a data-testid="title-link" href=""><span data-testid="title">E</span></a>',
            {"nom": "E", "url": None, "note": None},
        ),
        # score sans aucun chiffre, et pas de titre
        (
            '<a data-testid="title-link" href="https://b.test/h/f.fr.html"></a>'
            '<div data-testid="review-score">Pas encore de note</div>',
            {"nom": None, "url": "https://b.test/h/f.fr.html", "note": None},
        ),
    ],
)
def test_repli_beautifulsoup_lit_titre_url_canonique_et_note(corps_carte, attendu):
    """Sans JSON embarqué, la carte donne titre, URL sans paramètres ni fragment, note décimale.

    La virgule décimale est comprise ; l'absence de lien, de titre ou de chiffre donne None.
    Les champs que le repli ne sait pas lire valent None, et le contexte de la ville est repris.
    """
    html = f'<html><div data-testid="property-card">{corps_carte}</div></html>'
    hotels = _extraire(html)
    assert hotels == [
        {
            "hotel_id": None,
            "nom": attendu["nom"],
            "url": attendu["url"],
            "latitude": None,
            "longitude": None,
            "adresse": None,
            "note": attendu["note"],
            "nb_avis": None,
            "etoiles": None,
            "description": None,
            "id_ville": 9,
            "ville": "Aix",
            "date_collecte": DATE,
            "source": "booking",
        }
    ]


# --- url_recherche ----------------------------------------------------------------------------


def test_url_recherche_encode_la_ville_et_fixe_langue_et_filtre_hotels():
    """L'URL porte la ville encodée, « , France », la langue et le filtre « hôtels ».

    Un nom de ville ne peut pas injecter un paramètre : « & » et « = » sont encodés.
    """
    assert url_recherche("Mont Saint Michel") == (
        "https://www.booking.com/searchresults.fr.html"
        "?ss=Mont%20Saint%20Michel%2C%20France&lang=fr&nflt=ht_id%3D204"
    )
    assert url_recherche("Besançon").startswith(f"{URL_RECHERCHE}?ss=Besan%C3%A7on%2C%20France&")
    assert url_recherche("Paris&lang=en") == (
        f"{URL_RECHERCHE}?ss=Paris%26lang%3Den%2C%20France&lang=fr&nflt=ht_id%3D204"
    )


def test_url_recherche_refuse_une_ville_absente():
    """Sans nom de ville (None), aucune URL « None, France » n'est fabriquée : TypeError."""
    with pytest.raises(TypeError, match="NoneType"):
        url_recherche(None)


# --- constructeur du spider -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "manquant"),
    [({"sortie": "x"}, "villes"), ({"villes": []}, "sortie"), ({}, "villes")],
)
def test_spider_refuse_d_etre_cree_sans_villes_ou_sans_fichier_de_sortie(arguments, manquant):
    """Les villes et le chemin de sortie sont obligatoires : un spider sans eux est refusé."""
    with pytest.raises(TypeError, match=f"required positional argument.*{manquant}"):
        BookingHotelsSpider(**arguments)


def test_deux_spiders_ne_partagent_pas_leur_rapport_et_restent_inchanges_l_un_pour_l_autre():
    """Le rapport est propre à chaque instance : écrire dans l'un laisse l'autre inchangé."""
    premier, second = _spider("a"), _spider("b")
    assert premier.rapport == {}
    premier.rapport["Paris"] = {"issue": "ok"}
    assert second.rapport == {}
    assert premier.rapport is not second.rapport
    assert (premier.sortie, second.sortie) == ("a", "b")


def test_reglages_de_politesse_exacts():
    """Le robot s'identifie, obéit à robots.txt, attend 3 s (±50 %), une requête à la fois."""
    assert BookingHotelsSpider.name == "booking_hotels"
    assert BookingHotelsSpider.custom_settings == {
        "ROBOTSTXT_OBEY": True,
        "USER_AGENT": config.USER_AGENT,
        "DOWNLOAD_DELAY": 3,
        "DOWNLOAD_DELAY_JITTER": 0.5,
        "CONCURRENT_REQUESTS": 1,
        "AUTOTHROTTLE_ENABLED": True,
        "DEFAULT_REQUEST_HEADERS": {"Accept-Language": "fr-FR"},
        "LOG_LEVEL": "INFO",
    }


# --- start_requests et start ------------------------------------------------------------------


async def _requetes_de_start(spider: BookingHotelsSpider) -> list:
    return [requete async for requete in spider.start()]


def test_requetes_de_depart_portent_les_metadonnees_exactes_et_laissent_les_villes_inchangees():
    """Une requête par ville : URL de recherche, callbacks, méta complète, tous statuts admis.

    ``start`` rend les mêmes requêtes que ``start_requests``. La liste des villes du spider
    n'est pas modifiée par la génération.
    """
    villes = [
        {"id_ville": 1, "ville": "Paris", "date_collecte": "2026-10-10"},
        {"id_ville": 2, "ville": "Besançon", "date_collecte": "2026-10-11"},
    ]
    spider = BookingHotelsSpider(villes=villes, sortie="s")
    avant = copy.deepcopy(spider.villes)
    boucle = asyncio.new_event_loop()
    try:
        via_start = boucle.run_until_complete(_requetes_de_start(spider))
    finally:
        boucle.close()
    for requetes in (list(spider.start_requests()), via_start):
        assert [r.url for r in requetes] == [url_recherche("Paris"), url_recherche("Besançon")]
        assert [r.meta for r in requetes] == [
            {
                "id_ville": 1,
                "ville": "Paris",
                "date_collecte": "2026-10-10",
                "handle_httpstatus_all": True,
            },
            {
                "id_ville": 2,
                "ville": "Besançon",
                "date_collecte": "2026-10-11",
                "handle_httpstatus_all": True,
            },
        ]
        assert all(r.callback == spider.parse for r in requetes)
        assert all(r.errback == spider.echec for r in requetes)
    assert spider.villes == avant


def test_sans_ville_aucune_requete_n_est_emise():
    """Une liste de villes vide ne produit aucune requête : le générateur s'arrête aussitôt."""
    spider = BookingHotelsSpider(villes=[], sortie="s")
    with pytest.raises(StopIteration):
        next(spider.start_requests())


@pytest.mark.parametrize("cle", ["id_ville", "ville", "date_collecte"])
def test_ville_incomplete_leve_une_erreur_au_lieu_d_envoyer_une_requete_bancale(cle):
    """Une ville sans identifiant, sans nom ou sans date est refusée : KeyError nomme la clé."""
    ville = {"id_ville": 1, "ville": "Paris", "date_collecte": DATE}
    del ville[cle]
    spider = BookingHotelsSpider(villes=[ville], sortie="s")
    with pytest.raises(KeyError, match=cle):
        list(spider.start_requests())


# --- parse ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statut", "corps"),
    [
        (202, "<html>défi quelconque</html>"),
        (200, "<script>AwsWafIntegration.getToken()</script>"),
        (403, "<script>AwsWafIntegration.forceRefreshToken()</script>"),
    ],
)
def test_defi_anti_robot_n_est_jamais_extrait_ni_contourne_et_les_autres_villes_restent_inchangees(
    monkeypatch, caplog, statut, corps
):
    """Statut 202 OU marqueur AwsWafIntegration (même avec 200 ou 403) : ville bloquée.

    Aucun hôtel n'est extrait, aucune tentative de résolution n'est faite : le rapport consigne
    le statut exact, l'URL et la taille en octets, et le journal avertit. Le rapport des autres
    villes n'est pas touché.
    """
    monkeypatch.setattr(booking_spider, "extraire_hotels", _extraction_interdite)
    caplog.set_level(logging.WARNING)
    spider = _spider()
    voisin_avant = _installer_voisin(spider)
    reponse = _reponse(statut, corps)
    assert list(spider.parse(reponse)) == []
    assert spider.rapport["Paris"] == {
        "statut_http": statut,
        "url": reponse.url,
        "octets": len(corps.encode("utf-8")),
        "issue": "bloquee_par_controle_anti_robot",
    }
    assert spider.rapport["Lyon"] == voisin_avant
    messages = [(r.name, r.levelno, r.getMessage()) for r in caplog.records]
    assert messages == [
        (
            "booking_hotels",
            logging.WARNING,
            "Paris : contrôle anti-robot du site, page non collectée",
        )
    ]


@pytest.mark.parametrize("statut", [400, 403, 404, 429, 500, 503, 199, 201, 204, 301])
def test_statut_autre_que_200_est_consigne_comme_refus_et_n_est_pas_extrait(
    monkeypatch, caplog, statut
):
    """Tout statut différent de 200 (4xx, 5xx, mais aussi 201, 204, 301) est un refus consigné.

    La page n'est pas analysée : un corps d'erreur ne doit jamais être pris pour des résultats.
    """
    monkeypatch.setattr(booking_spider, "extraire_hotels", _extraction_interdite)
    caplog.set_level(logging.WARNING)
    spider = _spider()
    voisin_avant = _installer_voisin(spider)
    corps = "<html>Accès refusé é</html>"
    reponse = _reponse(statut, corps)
    assert list(spider.parse(reponse)) == []
    assert spider.rapport["Paris"] == {
        "statut_http": statut,
        "url": reponse.url,
        "octets": len(corps.encode("utf-8")),
        "issue": f"refusee_http_{statut}",
    }
    assert spider.rapport["Lyon"] == voisin_avant
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f"Paris : réponse HTTP {statut}, page non collectée")
    ]


def test_page_200_consigne_ok_ou_page_sans_hotel_et_laisse_les_autres_villes_inchangees():
    """Une page 200 est « ok » avec son nombre d'hôtels, sinon « page_sans_hotel » (0 hôtel).

    Le contexte (identifiant, ville, date) vient des métadonnées de la requête, pas du spider.
    """
    spider = _spider()
    voisin_avant = _installer_voisin(spider)
    corps = _html([_annonce(1, "H1"), _annonce(2, "H2")])
    reponse = _reponse(200, corps, ville="Paris", id_ville=42, date="2030-01-01")
    items = list(spider.parse(reponse))
    assert [(i["nom"], i["id_ville"], i["ville"], i["date_collecte"]) for i in items] == [
        ("H1", 42, "Paris", "2030-01-01"),
        ("H2", 42, "Paris", "2030-01-01"),
    ]
    assert spider.rapport["Paris"] == {
        "statut_http": 200,
        "url": reponse.url,
        "octets": len(corps.encode("utf-8")),
        "issue": "ok",
        "nb_hotels": 2,
    }
    vide = _reponse(200, "", ville="Paris")
    assert list(spider.parse(vide)) == []
    assert spider.rapport["Paris"] == {
        "statut_http": 200,
        "url": vide.url,
        "octets": 0,
        "issue": "page_sans_hotel",
        "nb_hotels": 0,
    }
    assert spider.rapport["Lyon"] == voisin_avant


CARTE = (
    '<div data-testid="property-card"><a data-testid="title-link" '
    'href="https://www.booking.com/hotel/fr/x.fr.html"><span data-testid="title">X</span>'
    "</a></div>"
)


@pytest.mark.parametrize(
    "page",
    [
        # JSON coupé après le marqueur : abandonné, et le repli n'est PAS tenté malgré la carte
        '<html><script>{"results":[{"__typename":"SearchResultProperty","basicPropertyData":{'
        f"</script>{CARTE}</html>",
        # marqueur d'un autre type d'objet : ni JSON ni carte
        '<html><script>{"results":[{"__typename":"Autre","basicPropertyData":{"id":1}}]}</script>'
        "</html>",
        # bloc qui n'est pas une carte de propriété
        '<html><div data-testid="autre"><span data-testid="title">Pas un hôtel</span></div></html>',
        '<html>"results":[]</html>',
        "",
    ],
)
def test_page_200_sans_annonce_exploitable_est_consignee_sans_hotel_et_les_autres_villes_inchangees(
    page,
):
    """Une page 200 dont rien n'est extractible est « page_sans_hotel », jamais « ok ».

    Aucun hôtel n'est inventé ; le rapport de la ville dit 0 hôtel ; celui des autres villes n'est
    pas touché. La même carte, sans JSON abîmé, est bien lue par le repli (voir le test du repli).
    """
    spider = _spider()
    voisin_avant = _installer_voisin(spider)
    assert _extraire(page) == []
    reponse = _reponse(200, page)
    assert list(spider.parse(reponse)) == []
    assert spider.rapport["Paris"] == {
        "statut_http": 200,
        "url": reponse.url,
        "octets": len(page.encode("utf-8")),
        "issue": "page_sans_hotel",
        "nb_hotels": 0,
    }
    assert spider.rapport["Lyon"] == voisin_avant


def test_reponse_non_rattachee_a_une_requete_est_refusee_et_le_rapport_reste_inchange():
    """Sans requête d'origine, Scrapy n'offre pas de ``meta`` : AttributeError, rien consigné."""
    spider = _spider()
    voisin_avant = _installer_voisin(spider)
    orpheline = HtmlResponse(url_recherche("Paris"), status=200, body=b"<html></html>")
    with pytest.raises(AttributeError, match="not tied to any request"):
        list(spider.parse(orpheline))
    assert list(spider.rapport) == ["Lyon"]
    assert spider.rapport["Lyon"] == voisin_avant


@pytest.mark.parametrize("cle", ["id_ville", "ville", "date_collecte"])
def test_meta_incomplete_leve_une_erreur_et_le_rapport_reste_inchange(cle):
    """Une réponse dont la requête n'a pas toutes les métadonnées est refusée (KeyError)."""
    spider = _spider()
    meta = {"id_ville": 1, "ville": "Paris", "date_collecte": DATE}
    del meta[cle]
    url = url_recherche("Paris")
    reponse = HtmlResponse(
        url, status=200, body=b"<html></html>", request=Request(url, meta=meta)
    )
    with pytest.raises(KeyError, match=cle):
        list(spider.parse(reponse))
    assert spider.rapport == {}


# --- echec ------------------------------------------------------------------------------------


def _echec(erreur: Exception, meta: dict) -> Failure:
    """Échec Scrapy factice : l'exception et la requête d'origine, comme le fournit Twisted."""
    echec = Failure(erreur)
    echec.request = Request(url_recherche("Paris"), meta=meta)
    return echec


@pytest.mark.parametrize(
    ("erreur", "issue_attendue", "nom_erreur"),
    [
        (IgnoreRequest("Forbidden by robots.txt"), "interdite_par_robots_txt", "IgnoreRequest"),
        (TimeoutError("délai dépassé"), "erreur_reseau", "TimeoutError"),
        (ConnectionRefusedError("refusé"), "erreur_reseau", "ConnectionRefusedError"),
        (ValueError("autre"), "erreur_reseau", "ValueError"),
    ],
)
def test_echec_avant_reponse_est_classe_sans_statut_http_et_laisse_les_autres_villes_inchangees(
    caplog, erreur, issue_attendue, nom_erreur
):
    """Seule l'exception IgnoreRequest (robots.txt) est classée « interdite » ; sinon « réseau ».

    Il n'y a pas de statut HTTP puisque aucune réponse n'est arrivée. Le journal avertit, et le
    rapport des autres villes n'est pas touché.
    """
    caplog.set_level(logging.WARNING)
    spider = _spider()
    voisin_avant = _installer_voisin(spider)
    spider.echec(_echec(erreur, {"ville": "Paris"}))
    assert spider.rapport["Paris"] == {
        "statut_http": None,
        "issue": issue_attendue,
        "erreur": nom_erreur,
    }
    assert spider.rapport["Lyon"] == voisin_avant
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("booking_hotels", logging.WARNING, f"Paris : {issue_attendue} ({nom_erreur})")
    ]


def test_echec_dont_la_requete_n_a_pas_de_ville_est_refuse_et_le_rapport_reste_inchange():
    """Une requête sans métadonnée « ville » ne peut être rattachée : KeyError, rien consigné."""
    spider = _spider()
    with pytest.raises(KeyError, match="ville"):
        spider.echec(_echec(TimeoutError("délai"), {}))
    assert spider.rapport == {}


# --- closed -----------------------------------------------------------------------------------


def test_closed_complete_les_villes_sans_reponse_et_laisse_inchangees_celles_deja_consignees(
    tmp_path,
):
    """Le rapport dit la raison de fin ; seule une ville ABSENTE reçoit « aucune_reponse ».

    Une ville déjà consignée n'est pas écrasée. Le fichier est voisin de la sortie, en UTF-8 non
    échappé, indenté de 2 espaces, avec des fins de ligne « \\n ».
    """
    spider = BookingHotelsSpider(
        villes=[
            {"id_ville": 1, "ville": "Besançon", "date_collecte": DATE},
            {"id_ville": 2, "ville": "Lyon", "date_collecte": DATE},
        ],
        sortie=str(tmp_path / "hotels.jsonl"),
    )
    spider.rapport["Besançon"] = {"statut_http": 403, "issue": "refusee_http_403", "octets": 12}
    avant = copy.deepcopy(spider.rapport["Besançon"])
    spider.closed("shutdown")
    assert spider.rapport["Besançon"] == avant
    fichier = tmp_path / "hotels.jsonl.rapport.json"
    brut = fichier.read_bytes()
    assert json.loads(brut.decode("utf-8")) == {
        "fin": "shutdown",
        "villes": {
            "Besançon": {"statut_http": 403, "issue": "refusee_http_403", "octets": 12},
            "Lyon": {"statut_http": None, "issue": "aucune_reponse", "octets": 0},
        },
    }
    assert brut.startswith(b'{\n  "fin": "shutdown",\n  "villes": {\n    "Besan\xc3\xa7on"')
    assert b"\r" not in brut
    assert b"\\u00e7" not in brut
    assert [p.name for p in tmp_path.iterdir()] == ["hotels.jsonl.rapport.json"]


def test_closed_dans_un_dossier_absent_leve_une_erreur_et_ne_cree_rien(tmp_path):
    """Un dossier de sortie absent est une erreur : le rapport n'est pas écrit ailleurs."""
    dossier = tmp_path / "absent"
    spider = BookingHotelsSpider(
        villes=[{"id_ville": 1, "ville": "Paris", "date_collecte": DATE}],
        sortie=str(dossier / "hotels.jsonl"),
    )
    with pytest.raises(FileNotFoundError):
        spider.closed("finished")
    assert not dossier.exists()


# --- lancer_collecte_booking ------------------------------------------------------------------


class _Enregistreur:
    """Doublures de ``archiver_robots``, ``verdict`` et ``subprocess.run`` qui notent les appels."""

    def __init__(self, texte_robots=ROBOTS_ACCUEILLANT):
        self.texte_robots = texte_robots
        self.archivages = []
        self.verdicts = []
        self.sous_processus = []
        self.hotels_jsonl = b""
        self.rapport_json = b"{}"
        self.ecrire_hotels = True
        self.ecrire_rapport = True
        self.erreur_sous_processus = None
        self.villes_lues = None
        self.texte_brut_villes = None

    def archiver_robots(self, lake, url_site, source, session=None):
        self.archivages.append((lake, url_site, source, session))
        return {"texte": self.texte_robots, "meta": {}}

    def verdict(self, texte, user_agent, url):
        self.verdicts.append((texte, user_agent, url))
        return {"autorise": True, "regle": None}

    def run(self, commande, **kwargs):
        self.sous_processus.append((list(commande), kwargs))
        if self.erreur_sous_processus is not None:
            raise self.erreur_sous_processus
        entree, sortie = Path(commande[3]), Path(commande[4])
        self.texte_brut_villes = entree.read_bytes().decode("utf-8")
        self.villes_lues = json.loads(self.texte_brut_villes)
        if self.ecrire_hotels:
            sortie.write_bytes(self.hotels_jsonl)
        if self.ecrire_rapport:
            Path(str(sortie) + ".rapport.json").write_bytes(self.rapport_json)


@pytest.fixture
def lake(tmp_path) -> DataLake:
    return DataLake(bucket=None, racine_locale=tmp_path / "lake")


def _brancher(monkeypatch, enregistreur: _Enregistreur, *, verdict_reel=True) -> None:
    monkeypatch.setattr(booking_spider, "archiver_robots", enregistreur.archiver_robots)
    if not verdict_reel:
        monkeypatch.setattr(booking_spider, "verdict", enregistreur.verdict)
    monkeypatch.setattr(booking_spider.subprocess, "run", enregistreur.run)


def _villes_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id_ville": [1, 2],
            "ville": ["Besançon", "Lyon"],
            "latitude": [47.2, 45.7],
            "longitude": [6.0, 4.8],
        }
    )


def test_robots_txt_qui_interdit_arrete_la_collecte_avant_tout_sous_processus(
    monkeypatch, lake
):
    """Si robots.txt interdit l'URL de la première ville, PermissionError cite la règle.

    Aucun sous-processus n'est lancé et rien n'est écrit dans le data lake.
    """
    enregistreur = _Enregistreur(ROBOTS_FERME)
    _brancher(monkeypatch, enregistreur)
    with pytest.raises(
        PermissionError,
        match=r"robots\.txt interdit la collecte : règle \('Disallow', '/searchresults'\)",
    ):
        lancer_collecte_booking(_villes_df(), lake)
    assert enregistreur.sous_processus == []
    assert lake.lister("raw/") == []


def test_robots_txt_illisible_arrete_la_collecte_et_aucun_verdict_n_est_demande(
    monkeypatch, lake
):
    """Si l'archivage de robots.txt échoue (403, 5xx), l'erreur remonte : on ne collecte pas.

    Ni verdict ni sous-processus n'ont lieu.
    """
    enregistreur = _Enregistreur()

    def archivage_refuse(*_args, **_kwargs):
        raise PermissionError("robots.txt illisible (403) : https://www.booking.com/robots.txt")

    _brancher(monkeypatch, enregistreur, verdict_reel=False)
    monkeypatch.setattr(booking_spider, "archiver_robots", archivage_refuse)
    with pytest.raises(PermissionError, match=r"robots\.txt illisible \(403\)"):
        lancer_collecte_booking(_villes_df(), lake)
    assert enregistreur.verdicts == []
    assert enregistreur.sous_processus == []
    assert lake.lister("raw/") == []


def test_sous_processus_en_echec_propage_l_erreur_sans_rien_ecrire_dans_le_lake(
    monkeypatch, lake
):
    """Un code de sortie non nul du robot (CalledProcessError) remonte ; le lake reste vide.

    Le dossier temporaire du sous-processus est supprimé malgré l'échec.
    """
    enregistreur = _Enregistreur()
    enregistreur.erreur_sous_processus = subprocess.CalledProcessError(2, ["robot"])
    _brancher(monkeypatch, enregistreur)
    with pytest.raises(subprocess.CalledProcessError, match="non-zero exit status 2"):
        lancer_collecte_booking(_villes_df(), lake)
    commande, _ = enregistreur.sous_processus[0]
    assert not Path(commande[3]).parent.exists()
    assert lake.lister("raw/booking/") == []


def test_sous_processus_sans_fichier_d_hotels_leve_une_erreur_et_le_lake_reste_vide(
    monkeypatch, lake
):
    """Un robot qui « réussit » sans produire hotels.jsonl est une anomalie : FileNotFoundError."""
    enregistreur = _Enregistreur()
    enregistreur.ecrire_hotels = False
    _brancher(monkeypatch, enregistreur)
    with pytest.raises(FileNotFoundError, match="hotels.jsonl"):
        lancer_collecte_booking(_villes_df(), lake)
    assert lake.lister("raw/booking/") == []


def test_sous_processus_sans_rapport_n_archive_pas_un_jsonl_orphelin(monkeypatch, lake):
    """Sans rapport de collecte, rien n'est archivé : pas de hotels.jsonl sans sa preuve."""
    enregistreur = _Enregistreur()
    enregistreur.hotels_jsonl = b'{"nom": "A"}\n'
    enregistreur.ecrire_rapport = False
    _brancher(monkeypatch, enregistreur)
    with pytest.raises(FileNotFoundError, match="rapport.json"):
        lancer_collecte_booking(_villes_df(), lake)
    assert lake.lister("raw/booking/") == []


def test_collecte_reussie_archive_les_fichiers_et_laisse_le_tableau_des_villes_inchange(
    monkeypatch, lake
):
    """Succès : arguments exacts du sous-processus, fichiers archivés tels quels, tableau rendu.

    Le verdict robots est demandé pour la PREMIÈRE ville avec le User-Agent du projet. La liste
    des villes transmise est en UTF-8 non échappé avec des identifiants entiers. Les lignes
    vides du JSON Lines sont ignorées dans le tableau mais conservées dans l'archive. Le
    tableau des villes de l'appelant n'est pas modifié et le dossier temporaire disparaît.
    """
    enregistreur = _Enregistreur(ROBOTS_ACCUEILLANT)
    enregistreur.hotels_jsonl = (
        b'{"nom": "H\xc3\xb4tel A", "id_ville": 1}\n\n{"nom": "B", "id_ville": 2}\n'
    )
    enregistreur.rapport_json = b'{"fin": "finished", "villes": {}}'
    _brancher(monkeypatch, enregistreur, verdict_reel=False)
    villes_df = _villes_df()
    df_avant = villes_df.copy(deep=True)

    resultat = lancer_collecte_booking(villes_df, lake)

    pd.testing.assert_frame_equal(villes_df, df_avant)
    assert enregistreur.archivages == [(lake, URL_RECHERCHE, "booking", None)]
    assert enregistreur.verdicts == [
        (ROBOTS_ACCUEILLANT, config.USER_AGENT, url_recherche("Besançon"))
    ]
    [(commande, options)] = enregistreur.sous_processus
    assert commande[:3] == [sys.executable, "-m", "kayak.collecte.booking_spider"]
    assert Path(commande[3]).name == "villes.json"
    assert Path(commande[4]).name == "hotels.jsonl"
    assert Path(commande[3]).parent == Path(commande[4]).parent
    assert options == {"check": True, "cwd": config.RACINE}
    assert enregistreur.villes_lues == [
        {"id_ville": 1, "ville": "Besançon", "date_collecte": DATE},
        {"id_ville": 2, "ville": "Lyon", "date_collecte": DATE},
    ]
    assert "Besançon" in enregistreur.texte_brut_villes
    assert not Path(commande[3]).parent.exists()
    assert lake.lire(f"raw/booking/date={DATE}/hotels.jsonl") == enregistreur.hotels_jsonl
    assert lake.lire(f"raw/booking/date={DATE}/rapport_collecte.json") == enregistreur.rapport_json
    assert resultat.to_dict("records") == [
        {"nom": "Hôtel A", "id_ville": 1},
        {"nom": "B", "id_ville": 2},
    ]


def test_collecte_bloquee_archive_un_jsonl_vide_et_le_rapport_et_laisse_les_villes_inchangees(
    monkeypatch, lake
):
    """Site bloquant le robot : aucun hôtel, mais le rapport (la preuve du blocage) est archivé.

    Le tableau des villes de l'appelant n'est pas modifié."""
    enregistreur = _Enregistreur()
    enregistreur.rapport_json = (
        b'{"fin": "finished", "villes": {"Besan\xc3\xa7on": {"statut_http": 202}}}'
    )
    _brancher(monkeypatch, enregistreur)
    villes_df = _villes_df()
    df_avant = villes_df.copy(deep=True)
    resultat = lancer_collecte_booking(villes_df, lake)
    pd.testing.assert_frame_equal(villes_df, df_avant)
    assert resultat.empty
    assert lake.lire(f"raw/booking/date={DATE}/hotels.jsonl") == b""
    assert lake.lire(f"raw/booking/date={DATE}/rapport_collecte.json") == enregistreur.rapport_json


# --- _executer_robot --------------------------------------------------------------------------


def _processus_factices(monkeypatch, erreur_crawl=None) -> list:
    """Remplace ``CrawlerProcess`` par une classe qui enregistre au lieu de lancer un robot."""
    construits = []

    class _Processus:
        def __init__(self, settings=None):
            self.settings = settings
            self.evenements = []
            construits.append(self)

        def crawl(self, spider, **kwargs):
            self.evenements.append(("crawl", spider, kwargs))
            if erreur_crawl is not None:
                raise erreur_crawl

        def start(self):
            self.evenements.append(("start",))

    monkeypatch.setattr(booking_spider, "CrawlerProcess", _Processus)
    return construits


def test_executer_robot_configure_les_flux_lance_le_robot_et_laisse_intacts_fichier_et_reglages(
    tmp_path, monkeypatch
):
    """Le robot reçoit les réglages du spider plus le flux JSON Lines, puis démarre.

    Les villes sont relues en UTF-8. Le fichier des villes n'est pas modifié et les réglages de
    la classe ne sont pas pollués par le flux (``FEEDS``).
    """
    construits = _processus_factices(monkeypatch)
    villes = [{"id_ville": 1, "ville": "Besançon", "date_collecte": DATE}]
    chemin_villes = tmp_path / "villes.json"
    chemin_villes.write_text(json.dumps(villes, ensure_ascii=False), encoding="utf-8")
    contenu_avant = chemin_villes.read_bytes()
    reglages_avant = copy.deepcopy(BookingHotelsSpider.custom_settings)
    sortie = str(tmp_path / "hotels.jsonl")

    _executer_robot(str(chemin_villes), sortie)

    [processus] = construits
    assert processus.settings == {
        **reglages_avant,
        "FEEDS": {
            sortie: {
                "format": "jsonlines",
                "overwrite": True,
                "store_empty": True,
                "encoding": "utf8",
            }
        },
    }
    assert processus.evenements == [
        ("crawl", BookingHotelsSpider, {"villes": villes, "sortie": sortie}),
        ("start",),
    ]
    assert chemin_villes.read_bytes() == contenu_avant
    assert BookingHotelsSpider.custom_settings == reglages_avant


def test_executer_robot_sans_fichier_de_villes_leve_une_erreur_et_ne_cree_aucun_robot(
    tmp_path, monkeypatch
):
    """Fichier des villes absent : FileNotFoundError, et aucun processus Scrapy n'est construit."""
    construits = _processus_factices(monkeypatch)
    with pytest.raises(FileNotFoundError, match="villes.json"):
        _executer_robot(str(tmp_path / "villes.json"), str(tmp_path / "hotels.jsonl"))
    assert construits == []


def test_executer_robot_avec_un_fichier_de_villes_invalide_ne_cree_aucun_robot(
    tmp_path, monkeypatch
):
    """Un JSON des villes illisible lève JSONDecodeError : le robot n'est pas lancé à l'aveugle."""
    construits = _processus_factices(monkeypatch)
    chemin_villes = tmp_path / "villes.json"
    chemin_villes.write_text('[{"id_ville": 1,', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _executer_robot(str(chemin_villes), str(tmp_path / "hotels.jsonl"))
    assert construits == []


def test_executer_robot_ne_demarre_pas_si_la_programmation_du_robot_echoue(tmp_path, monkeypatch):
    """Si ``crawl`` échoue, l'erreur remonte et ``start`` (la boucle réseau) n'est jamais appelé."""
    construits = _processus_factices(monkeypatch, erreur_crawl=RuntimeError("spider refusé"))
    chemin_villes = tmp_path / "villes.json"
    chemin_villes.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="spider refusé"):
        _executer_robot(str(chemin_villes), str(tmp_path / "hotels.jsonl"))
    assert [evenement[0] for evenement in construits[0].evenements] == ["crawl"]


# --- point d'entrée « python -m kayak.collecte.booking_spider » -------------------------------


def _executer_comme_script(monkeypatch, arguments: list[str]) -> list:
    """Exécute le module comme un script (``__main__``) avec un ``CrawlerProcess`` factice."""
    construits = _processus_factices(monkeypatch)
    monkeypatch.setattr(scrapy.crawler, "CrawlerProcess", booking_spider.CrawlerProcess)
    monkeypatch.setattr(sys, "argv", ["booking_spider", *arguments])
    monkeypatch.delitem(sys.modules, "kayak.collecte.booking_spider")
    runpy.run_module("kayak.collecte.booking_spider", run_name="__main__")
    return construits


def test_lance_comme_script_relit_les_villes_vers_la_sortie_indiquee_et_laisse_le_fichier_intact(
    tmp_path, monkeypatch
):
    """``python -m ... villes.json sortie`` : le 1er argument est les villes, le 2e la sortie.

    Le fichier des villes n'est que lu : son contenu reste intact.
    """
    chemin_villes = tmp_path / "villes.json"
    villes = [{"id_ville": 7, "ville": "Nice", "date_collecte": DATE}]
    chemin_villes.write_text(json.dumps(villes), encoding="utf-8")
    contenu_avant = chemin_villes.read_bytes()
    sortie = str(tmp_path / "hotels.jsonl")
    construits = _executer_comme_script(monkeypatch, [str(chemin_villes), sortie])
    assert chemin_villes.read_bytes() == contenu_avant
    [processus] = construits
    assert list(processus.settings["FEEDS"]) == [sortie]
    [(_, spider, options), demarrage] = processus.evenements
    assert spider.__name__ == "BookingHotelsSpider"
    assert options == {"villes": villes, "sortie": sortie}
    assert demarrage == ("start",)


def test_lance_comme_script_sans_arguments_leve_une_erreur_et_ne_cree_aucun_robot(monkeypatch):
    """Sans les deux arguments, IndexError : le robot ne démarre pas avec une sortie inventée."""
    construits = _processus_factices(monkeypatch)
    monkeypatch.setattr(scrapy.crawler, "CrawlerProcess", booking_spider.CrawlerProcess)
    monkeypatch.setattr(sys, "argv", ["booking_spider"])
    monkeypatch.delitem(sys.modules, "kayak.collecte.booking_spider")
    with pytest.raises(IndexError):
        runpy.run_module("kayak.collecte.booking_spider", run_name="__main__")
    assert construits == []


# --- compléments : le marqueur JSON dans la page ------------------------------------------------


def test_un_results_anterieur_au_marqueur_est_sans_effet_sur_les_hotels_extraits():
    """Un autre « results » plus tôt dans la page ne détourne pas la lecture de la bonne liste.

    L'extraction repart du marqueur, pas du début du texte : la page avec le leurre donne les
    mêmes hôtels que la page sans lui.
    """
    annonces = [_annonce(1, "A"), _annonce(2, "B")]
    leurre = '<script>window.autre = {"results":[]};</script>'
    sans = _extraire(_html(annonces))
    avec = _extraire(leurre + _html(annonces))
    assert [h["nom"] for h in sans] == ["A", "B"]
    assert avec == sans


@pytest.mark.parametrize(("prefixe", "debut", "position"), [("", 1, 0), ("", 0, 1), ("x", 0, 2)])
def test_le_marqueur_est_lu_quelle_que_soit_sa_position_meme_en_tout_debut_de_page(
    prefixe, debut, position
):
    """Le marqueur est reconnu à la position 0, 1 ou 2 du texte : aucune borne arbitraire.

    Le texte est le JSON compact ``{"results":[...]}`` privé de ses ``debut`` premiers caractères
    et précédé de ``prefixe`` ; la position du marqueur est vérifiée.
    """
    corps = json.dumps({"results": [_annonce(1, "A")]}, separators=(",", ":"))
    page = prefixe + corps[debut:]
    assert page.find('"results":[{"__typename":"SearchResultProperty"') == position
    assert [h["nom"] for h in _extraire(page)] == ["A"]


# --- cas limites relevés par la mutation (17/10/2026) ------------------------------------------


def test_extraire_hotels_lit_le_premier_bloc_de_resultats_et_pas_un_bloc_suivant():
    """Deux blocs « results » dans la page : seul le premier, celui de la recherche, est lu.

    Une page peut embarquer d'autres listes plus bas (suggestions, pages voisines) : les lire
    à la place du premier bloc rendrait d'autres hôtels que ceux de la recherche.
    """
    page = _html([_annonce(1, "Premier")]) + _html([_annonce(2, "Second")])
    assert [h["nom"] for h in _extraire(page)] == ["Premier"]


def test_extraire_hotels_recolle_une_note_decoupee_en_plusieurs_balises():
    """Une note écrite « 8 » puis « ,5 » dans deux balises se relit 8,5, et non 8.

    Les morceaux de texte de la note sont recollés sans espace avant la lecture du nombre.
    """
    page = (
        '<html><div data-testid="property-card"><div data-testid="title">Hôtel B</div>'
        '<div data-testid="review-score"><span>8</span>\n<span>,5</span></div></div></html>'
    )
    [hotel] = _extraire(page)
    assert hotel["note"] == 8.5


def test_extraire_hotels_de_repli_n_emet_aucun_avertissement_de_parseur():
    """Le parseur HTML est nommé : aucun avertissement « parseur deviné » de BeautifulSoup.

    Sans parseur nommé, BeautifulSoup prend le meilleur installé (lxml s'il est là) : le
    résultat dépendrait alors du poste. L'avertissement est transformé en erreur ici.
    """
    page = '<html><div data-testid="property-card"><div data-testid="title">C</div></div></html>'
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert [h["nom"] for h in _extraire(page)] == ["C"]


def test_rapport_de_fin_ouvert_en_utf8_et_en_lf_sur_tous_les_systemes(tmp_path, espion_open):
    """Le rapport est ouvert en écriture texte, UTF-8 et fins de ligne « \\n » demandés.

    Sous Linux, l'oubli de ces réglages ne se verrait pas dans le fichier ; sous Windows, il
    écrirait en cp1252 et en « \\r\\n » : on vérifie donc ce qui est demandé à ``open``.
    """
    ouvertures = espion_open(booking_spider)
    _spider(str(tmp_path / "hotels.jsonl")).closed("finished")
    assert [(o.fichier, o.mode, o.en_utf8, o.newline) for o in ouvertures] == [
        (str(tmp_path / "hotels.jsonl.rapport.json"), "w", True, "\n")
    ]


def test_collecte_ecrit_les_villes_en_utf8_et_relit_les_sorties_en_octets(
    monkeypatch, lake, espion_open
):
    """La liste des villes est écrite en UTF-8 ; les sorties du robot sont relues en octets.

    Relues en octets, elles sont archivées telles quelles, sans décodage par l'encodage du poste
    ni conversion des fins de ligne.
    """
    enregistreur = _Enregistreur(ROBOTS_ACCUEILLANT)
    enregistreur.rapport_json = b'{"fin": "finished", "villes": {}}'
    _brancher(monkeypatch, enregistreur, verdict_reel=False)
    ouvertures = espion_open(booking_spider)
    lancer_collecte_booking(_villes_df(), lake)
    assert [(Path(o.fichier).name, o.mode, o.en_utf8) for o in ouvertures] == [
        ("villes.json", "w", True),
        ("hotels.jsonl", "rb", False),
        ("hotels.jsonl.rapport.json", "rb", False),
    ]


def test_executer_robot_relit_les_villes_en_utf8(tmp_path, monkeypatch, espion_open):
    """Le fichier des villes est relu en UTF-8 demandé, et non avec l'encodage du poste."""
    _processus_factices(monkeypatch)
    chemin_villes = tmp_path / "villes.json"
    chemin_villes.write_text('[{"id_ville": 1, "ville": "Besançon"}]', encoding="utf-8")
    ouvertures = espion_open(booking_spider)
    _executer_robot(str(chemin_villes), str(tmp_path / "hotels.jsonl"))
    assert [(o.fichier, o.mode, o.en_utf8) for o in ouvertures] == [
        (str(chemin_villes), "r", True)
    ]
