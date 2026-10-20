# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests unitaires de l'extraction des hôtels Booking et du rapport de collecte.

ATTENTION, LIMITE DE CES TESTS : ceux qui portent sur ``extraire_hotels`` sont CIRCULAIRES. Le
HTML qu'ils analysent est fabriqué ici, avec le marqueur exact que l'extraction recherche
(``"results":[{"__typename":"SearchResultProperty"``) : ils prouvent que le code lit ce qu'il a
été écrit pour lire, pas que Booking.com sert réellement cette structure. L'extraction n'a donc
PAS été validée sur une page réelle : le 10/10/2026, le site a opposé son contrôle anti-robot
(AWS WAF) à toutes les requêtes et aucune page de résultats n'a pu être observée. Seuls les
tests du rapport (statut HTTP, issue par ville) et des réglages Scrapy ne dépendent pas de cette
hypothèse.
"""

import json

from scrapy.http import HtmlResponse, Request
from twisted.python.failure import Failure

from kayak import config
from kayak.collecte.booking_spider import (
    BookingHotelsSpider,
    extraire_hotels,
    url_recherche,
)


def _html_avec_json(results: list) -> str:
    """Construit un HTML minimal contenant le JSON embarqué Booking (marqueur fabriqué)."""
    payload = {"results": results}
    return (
        f"<html><body><script>window.bookingData = {json.dumps(payload, separators=(',', ':'))};"
        "</script></body></html>"
    )


def test_extraire_hotels_champs_et_filtres():
    """Vérifie les champs extraits, l'URL canonique et les exclusions."""
    results = [
        {
            "__typename": "SearchResultProperty",
            "basicPropertyData": {
                "id": 111,
                "isTestProperty": False,
                "pageName": "hotel-les-oliviers",
                "location": {
                    "countryCode": "fr",
                    "latitude": 43.5297,
                    "longitude": 5.4474,
                    "address": "12 rue de la Paix, Aix-en-Provence",
                },
                "reviews": {
                    "showScore": True,
                    "totalScore": 8.7,
                    "reviewsCount": 124,
                },
                "starRating": {"value": 3},
            },
            "displayName": {"text": "Hôtel Les Oliviers"},
            "description": {"text": "Charmant hôtel en centre-ville"},
        },
        {
            "__typename": "SearchResultProperty",
            "basicPropertyData": {
                "id": 222,
                "isTestProperty": False,
                "pageName": "chez-marie",
                "location": {
                    "countryCode": "fr",
                    "latitude": 43.5,
                    "longitude": 5.4,
                    "address": "Impasse privée",
                },
                "reviews": {"showScore": False},
                "starRating": None,
            },
            "displayName": {"text": "Chez Marie"},
            "description": {"text": "Chambre chez l'habitant"},
            "hostTraderLabel": "PRIVATE",
        },
        {
            "__typename": "SearchResultProperty",
            "basicPropertyData": {
                "id": 333,
                "isTestProperty": True,
                "pageName": "test-property",
                "location": {
                    "countryCode": "fr",
                    "latitude": 43.6,
                    "longitude": 5.5,
                    "address": "Test address",
                },
                "reviews": {},
                "starRating": {"value": 4},
            },
            "displayName": {"text": "Test Property"},
            "description": {"text": "Propriété de test"},
        },
    ]

    html = _html_avec_json(results)
    hotels = extraire_hotels(html, id_ville=21, ville="Marseille", date_collecte="2026-01-15")

    assert len(hotels) == 1
    hotel = hotels[0]
    assert hotel["hotel_id"] == 111
    assert hotel["nom"] == "Hôtel Les Oliviers"
    assert hotel["url"] == "https://www.booking.com/hotel/fr/hotel-les-oliviers.fr.html"
    assert hotel["latitude"] == 43.5297
    assert hotel["longitude"] == 5.4474
    assert hotel["adresse"] == "12 rue de la Paix, Aix-en-Provence"
    assert hotel["note"] == 8.7
    assert hotel["nb_avis"] == 124
    assert hotel["etoiles"] == 3
    assert hotel["description"] == "Charmant hôtel en centre-ville"
    assert hotel["id_ville"] == 21
    assert hotel["ville"] == "Marseille"
    assert hotel["date_collecte"] == "2026-01-15"


def test_extraire_hotels_repli_beautifulsoup():
    """Vérifie le repli BeautifulSoup en l'absence de JSON embarqué."""
    html = """
    <html><body>
        <div data-testid="property-card">
            <a data-testid="title-link"
               href="https://www.booking.com/hotel/fr/maison.fr.html?aid=123">
                <span data-testid="title">Maison Provence</span>
            </a>
            <div data-testid="review-score">8,9 Génial · 42 avis</div>
        </div>
    </body></html>
    """
    hotels = extraire_hotels(html, id_ville=21, ville="Marseille", date_collecte="2026-01-15")
    assert len(hotels) == 1
    assert hotels[0]["nom"] == "Maison Provence"
    assert hotels[0]["url"] == "https://www.booking.com/hotel/fr/maison.fr.html"
    assert hotels[0]["note"] == 8.9
    assert hotels[0]["latitude"] is None


# --- Rapport de collecte : statut HTTP de chaque réponse -------------------------------------


def _spider() -> BookingHotelsSpider:
    villes = [
        {"id_ville": 1, "ville": "Paris", "date_collecte": "2026-10-10"},
        {"id_ville": 2, "ville": "Lyon", "date_collecte": "2026-10-10"},
    ]
    return BookingHotelsSpider(villes=villes, sortie="inutilise")


def _reponse(statut: int, corps: str, ville: str = "Paris", id_ville: int = 1) -> HtmlResponse:
    """Réponse HTTP factice, avec la requête et ses métadonnées comme Scrapy les fournit."""
    url = url_recherche(ville)
    requete = Request(
        url, meta={"id_ville": id_ville, "ville": ville, "date_collecte": "2026-10-10"}
    )
    return HtmlResponse(url, status=statut, body=corps.encode("utf-8"), request=requete)


def test_rapport_consigne_le_statut_http_du_defi_anti_robot():
    """Un défi AWS WAF (202) est consigné avec son statut exact et sa taille, sans item."""
    spider = _spider()
    items = list(spider.parse(_reponse(202, "<script>AwsWafIntegration.getToken()</script>")))
    assert items == []
    entree = spider.rapport["Paris"]
    assert entree["statut_http"] == 202
    assert entree["issue"] == "bloquee_par_controle_anti_robot"
    assert entree["octets"] > 0 and entree["url"].startswith("https://www.booking.com/")


def test_rapport_consigne_un_refus_http():
    """Un 403 ou 429 n'est plus perdu : il atteint parse() et figure au rapport."""
    spider = _spider()
    list(spider.parse(_reponse(403, "<html>interdit</html>")))
    assert spider.rapport["Paris"]["statut_http"] == 403
    assert spider.rapport["Paris"]["issue"] == "refusee_http_403"


def test_rapport_page_ok_et_page_sans_hotel():
    """Une page 200 est comptée « ok » avec son nombre d'hôtels, sinon « page_sans_hotel »."""
    spider = _spider()
    assert list(spider.parse(_reponse(200, "<html><body>rien</body></html>"))) == []
    assert spider.rapport["Paris"]["issue"] == "page_sans_hotel"
    assert spider.rapport["Paris"]["statut_http"] == 200
    page = (
        '<html><div data-testid="property-card"><a data-testid="title-link" '
        'href="https://www.booking.com/hotel/fr/x.fr.html"><span data-testid="title">X</span>'
        "</a></div></html>"
    )
    items = list(spider.parse(_reponse(200, page, "Lyon", 2)))
    assert len(items) == 1
    assert spider.rapport["Lyon"]["issue"] == "ok" and spider.rapport["Lyon"]["nb_hotels"] == 1


def test_erreur_reseau_et_ville_sans_reponse_sont_consignees(tmp_path):
    """Une erreur avant réponse et une ville jamais servie figurent aussi au rapport."""
    spider = BookingHotelsSpider(
        villes=[
            {"id_ville": 1, "ville": "Paris", "date_collecte": "d"},
            {"id_ville": 2, "ville": "Lyon", "date_collecte": "d"},
        ],
        sortie=str(tmp_path / "hotels.jsonl"),
    )
    echec = Failure(TimeoutError("délai dépassé"))
    echec.request = Request(url_recherche("Paris"), meta={"ville": "Paris"})
    spider.echec(echec)
    assert spider.rapport["Paris"] == {
        "statut_http": None,
        "issue": "erreur_reseau",
        "erreur": "TimeoutError",
    }
    spider.closed("finished")  # Lyon n'a eu ni réponse ni erreur
    rapport = json.loads((tmp_path / "hotels.jsonl.rapport.json").read_text(encoding="utf-8"))
    assert rapport["villes"]["Lyon"]["issue"] == "aucune_reponse"
    assert rapport["villes"]["Paris"]["issue"] == "erreur_reseau"


def test_les_requetes_laissent_passer_tous_les_statuts():
    """Les requêtes demandent à Scrapy de ne pas écarter les 4xx/5xx avant parse()."""
    requetes = list(_spider().start_requests())
    assert [r.meta["ville"] for r in requetes] == ["Paris", "Lyon"]
    assert all(r.meta["handle_httpstatus_all"] is True for r in requetes)
    assert all(r.errback is not None for r in requetes)


def test_reglages_scrapy_connus():
    """Chaque réglage du spider existe dans la version de Scrapy installée.

    Un réglage inconnu serait ignoré sans message : le robot croirait être poli (délai aléatoire,
    limitation de débit) sans l'être. DOWNLOAD_DELAY_JITTER existe depuis Scrapy 2.19
    (RANDOMIZE_DOWNLOAD_DELAY, son prédécesseur, y est déprécié) ; requirements.txt fige 2.19.
    """
    from scrapy.settings import default_settings

    inconnus = [
        cle for cle in BookingHotelsSpider.custom_settings if not hasattr(default_settings, cle)
    ]
    assert inconnus == []
    # Le robot s'identifie, obéit à robots.txt, une requête à la fois, avec un délai aléatoire.
    reglages = BookingHotelsSpider.custom_settings
    assert reglages["ROBOTSTXT_OBEY"] is True
    assert reglages["USER_AGENT"] == config.USER_AGENT
    assert reglages["CONCURRENT_REQUESTS"] == 1
    assert reglages["DOWNLOAD_DELAY"] > 0 and reglages["DOWNLOAD_DELAY_JITTER"] > 0
