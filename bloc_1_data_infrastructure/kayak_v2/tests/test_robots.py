# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de l'interprétation de robots.txt (RFC 9309) et de son archivage dans le data lake.

Le module standard ``urllib.robotparser`` ignore les jokers (``*``, ``$``) que Booking.com
emploie partout : l'interprétation est donc faite par ``kayak.collecte.robots``, et ces tests
la vérifient sur des fichiers synthétiques (aucun réseau) puis sur le robots.txt réellement
archivé dans le data lake livré.
"""

import hashlib
import json

import pytest

from kayak import config
from kayak.collecte.robots import (
    archiver_robots,
    jeton_produit,
    lire_groupes,
    regles_applicables,
    verdict,
)
from kayak.stockage.data_lake import DataLake

ROBOTS = """
# commentaire
Sitemap: https://exemple.test/sitemap.xml

User-agent: MauvaisRobot
Disallow: /

User-agent: KayakEtudeCertif
User-agent: AutreRobot
Disallow: /prive/

User-agent: *
Disallow: /alt_avail*
Disallow: /book.html
Disallow: /fin$
Disallow: /hotel/us/the-airstream-van.*.html
Disallow: /tout/
Allow: /tout/public
Disallow:
"""

ROBOTS_GENERIQUE = """
User-agent: *
Disallow: /alt_avail*
Disallow: /book.html
Disallow: /fin$
Disallow: /hotel/us/the-airstream-van.*.html
Disallow: /tout/
Allow: /tout/public
Disallow:
"""


def test_jeton_produit():
    """Le jeton produit est le premier mot du User-Agent, en minuscules."""
    assert jeton_produit("KayakEtudeCertif/1.0 (projet; robot)") == "kayaketudecertif"
    assert jeton_produit("Bot") == "bot"


def test_lire_groupes_regroupe_les_agents_consecutifs():
    """Deux « User-agent » de suite partagent les mêmes règles ; les commentaires sont ignorés."""
    groupes = lire_groupes(ROBOTS)
    assert [g["agents"] for g in groupes] == [
        ["mauvaisrobot"],
        ["kayaketudecertif", "autrerobot"],
        ["*"],
    ]
    # « Disallow: » à motif vide ne limite rien : il n'apparaît pas dans les règles.
    assert ("Disallow", "") not in groupes[2]["regles"]
    assert ("Disallow", "/alt_avail*") in groupes[2]["regles"]


def test_groupe_du_robot_prime_sur_le_groupe_generique():
    """Un groupe nommé d'après le robot l'emporte sur « * » (RFC 9309, § 2.2.1)."""
    agent, regles = regles_applicables(ROBOTS, config.USER_AGENT)
    assert agent == "kayaketudecertif"
    assert regles == [("Disallow", "/prive/")]
    # Sans groupe nommé, c'est le groupe générique.
    agent, _ = regles_applicables(ROBOTS_GENERIQUE, config.USER_AGENT)
    assert agent == "*"


def test_verdict_jokers_ancre_et_prefixe():
    """Le joker ``*``, l'ancre ``$`` et le préfixe se comportent comme dans la RFC."""
    ua = config.USER_AGENT
    # Préfixe avec joker final.
    assert not verdict(ROBOTS_GENERIQUE, ua, "/alt_avail_foo?x=1")["autorise"]
    # Joker au milieu du motif.
    assert not verdict(ROBOTS_GENERIQUE, ua, "/hotel/us/the-airstream-van.fr.html")["autorise"]
    assert verdict(ROBOTS_GENERIQUE, ua, "/hotel/fr/autre-hotel.fr.html")["autorise"]
    # Ancre de fin : « /fin$ » ne bloque que /fin exactement.
    assert not verdict(ROBOTS_GENERIQUE, ua, "/fin")["autorise"]
    assert verdict(ROBOTS_GENERIQUE, ua, "/fin/suite")["autorise"]
    # Aucune règle ne correspond : autorisé par défaut, sans règle décisive.
    resultat = verdict(ROBOTS_GENERIQUE, ua, "/searchresults.fr.html?ss=Paris")
    assert resultat == {
        "autorise": True,
        "regle": None,
        "correspondantes": [],
        "groupe": "*",
    }


def test_groupes_du_meme_agent_sont_fusionnes():
    """Un agent cité dans deux groupes cumule les règles des deux (RFC 9309, 2.2.1)."""
    texte = (
        "User-agent: KayakEtudeCertif\nDisallow: /a/\n\n"
        "User-agent: Autre\nDisallow: /autre/\n\n"
        "User-agent: kayaketudecertif\nDisallow: /b/\n"
    )
    agent, regles = regles_applicables(texte, config.USER_AGENT)
    assert agent == "kayaketudecertif"
    assert regles == [("Disallow", "/a/"), ("Disallow", "/b/")]
    assert not verdict(texte, config.USER_AGENT, "/a/x")["autorise"]
    assert not verdict(texte, config.USER_AGENT, "/b/x")["autorise"]
    assert verdict(texte, config.USER_AGENT, "/autre/x")["autorise"]
    # Les groupes génériques sont fusionnés aussi.
    generique = "User-agent: *\nDisallow: /x/\n\nUser-agent: *\nDisallow: /y/\n"
    assert regles_applicables(generique, "Inconnu/1.0")[1] == [
        ("Disallow", "/x/"),
        ("Disallow", "/y/"),
    ]


def test_verdict_regle_la_plus_longue_l_emporte():
    """« Allow » plus spécifique que « Disallow » ré-autorise ; sinon la plus longue gagne."""
    ua = config.USER_AGENT
    assert not verdict(ROBOTS_GENERIQUE, ua, "/tout/secret")["autorise"]
    resultat = verdict(ROBOTS_GENERIQUE, ua, "/tout/public/page")
    assert resultat["autorise"] and resultat["regle"] == ("Allow", "/tout/public")
    # les deux règles qui correspondent sont conservées comme preuve, dans l'ordre du fichier
    assert resultat["correspondantes"] == [("Disallow", "/tout/"), ("Allow", "/tout/public")]
    # À longueur égale, Allow l'emporte.
    egal = "User-agent: *\nDisallow: /a\nAllow: /a\n"
    assert verdict(egal, ua, "/a")["autorise"]


def test_verdict_groupe_nomme_interdit_tout():
    """Un groupe nommé « Disallow: / » interdit tout au robot visé, pas aux autres."""
    interdit = "User-agent: KayakEtudeCertif\nDisallow: /\n\nUser-agent: *\nDisallow:\n"
    assert not verdict(interdit, config.USER_AGENT, "/n-importe-quoi")["autorise"]
    assert verdict(interdit, "AutreRobot/1.0", "/n-importe-quoi")["autorise"]


class _ReponseFactice:
    """Réponse HTTP minimale, comme celle de requests."""

    def __init__(self, statut: int, contenu: bytes):
        self.status_code = statut
        self.content = contenu
        self.text = contenu.decode("utf-8")
        self.headers = {"Date": "Sun, 11 Oct 2026 05:15:11 GMT"}


class _SessionFactice:
    """Session qui rend toujours la même réponse et mémorise les URL demandées."""

    def __init__(self, reponse: _ReponseFactice):
        self.reponse = reponse
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.reponse


def test_archiver_robots_ecrit_le_fichier_exact_et_ses_metadonnees(tmp_path, monkeypatch):
    """Le fichier est archivé tel que reçu, daté, avec statut, date serveur et empreinte."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-11")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    contenu = ROBOTS_GENERIQUE.encode("utf-8")
    session = _SessionFactice(_ReponseFactice(200, contenu))
    resultat = archiver_robots(
        lake, "https://www.exemple.test/searchresults.html", "booking", session
    )
    assert session.urls == ["https://www.exemple.test/robots.txt"]
    assert lake.lire("raw/booking/date=2026-10-11/robots.txt") == contenu
    meta = json.loads(lake.lire("raw/booking/date=2026-10-11/robots_meta.json"))
    assert meta["statut_http"] == 200
    assert meta["date_reponse_serveur"] == "Sun, 11 Oct 2026 05:15:11 GMT"
    assert meta["taille_octets"] == len(contenu)
    assert meta["sha256"] == hashlib.sha256(contenu).hexdigest()
    assert resultat["texte"] == ROBOTS_GENERIQUE


@pytest.mark.parametrize("statut", [403, 429, 500, 503])
def test_archiver_robots_refuse_un_fichier_illisible(tmp_path, statut):
    """Un 403 (souvent un contrôle anti-robot) ou un 5xx (fichier injoignable) arrête la collecte.

    Pour un 5xx, la RFC 9309 demande de supposer tout interdit ; un 403 n'est pas une absence.
    Rien n'est archivé.
    """
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session = _SessionFactice(_ReponseFactice(statut, b"refus"))
    with pytest.raises(PermissionError):
        archiver_robots(lake, "https://www.exemple.test/", "booking", session)
    assert lake.lister("raw/booking") == []


@pytest.mark.parametrize("statut", [404, 410])
def test_archiver_robots_fichier_absent_n_impose_aucune_restriction(tmp_path, monkeypatch, statut):
    """Un 404 (ou 410) signifie « pas de robots.txt » : aucune restriction (RFC 9309, 2.3.1.3).

    Le statut est archivé (preuve), mais aucun fichier robots.txt n'est inventé ; le texte rendu
    est vide, donc tout est autorisé.
    """
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-11")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session = _SessionFactice(_ReponseFactice(statut, b"introuvable"))
    resultat = archiver_robots(lake, "https://www.exemple.test/", "destinations", session)
    assert resultat["texte"] == ""
    assert verdict(resultat["texte"], config.USER_AGENT, "/n-importe-quoi")["autorise"]
    meta = json.loads(lake.lire("raw/destinations/date=2026-10-11/robots_meta.json"))
    assert meta["statut_http"] == statut and meta["taille_octets"] == 0
    assert meta["restriction"] == "aucune (fichier absent)"
    assert not lake.existe("raw/destinations/date=2026-10-11/robots.txt")


def _robots_archive_livre():
    """Chemin du robots.txt de Booking archivé dans le data lake livré (None s'il manque)."""
    trouves = sorted((config.DATALAKE_LOCAL / "raw" / "booking").glob("date=*/robots.txt"))
    return trouves[-1] if trouves else None


@pytest.mark.skipif(_robots_archive_livre() is None, reason="robots.txt de Booking non archivé")
def test_booking_autorise_les_pages_demandees_et_interdit_d_autres():
    """Preuve sur le fichier réel archivé : règle par règle, pour un robot générique.

    Aucune règle du groupe « * » ne correspond à la page de résultats ni aux fiches d'hôtels
    (seule règle sous /hotel/ : un cas particulier aux États-Unis) ; des pages du site restent
    interdites (réservation) : le verdict n'est donc pas « tout est permis ».
    """
    texte = _robots_archive_livre().read_text(encoding="utf-8")
    ua = config.USER_AGENT
    recherche = verdict(
        texte, ua, "/searchresults.fr.html?ss=Paris%2C%20France&lang=fr&nflt=ht_id%3D204"
    )
    assert recherche["autorise"] and recherche["correspondantes"] == []
    fiche = verdict(texte, ua, "/hotel/fr/hotel-les-oliviers.fr.html")
    assert fiche["autorise"] and fiche["correspondantes"] == []
    assert not verdict(texte, ua, "/book.html")["autorise"]
    assert not verdict(texte, ua, "/hotel/us/the-airstream-van.fr.html")["autorise"]
    assert recherche["groupe"] == "*"


@pytest.mark.skipif(_robots_archive_livre() is None, reason="robots.txt de Booking non archivé")
def test_verdict_concorde_avec_le_parseur_de_scrapy():
    """Recoupement : notre interprétation donne le même verdict que Protego (celui de Scrapy)."""
    protego = pytest.importorskip("protego")
    texte = _robots_archive_livre().read_text(encoding="utf-8")
    parseur = protego.Protego.parse(texte)
    for chemin in (
        "/searchresults.fr.html?ss=Lyon%2C%20France&lang=fr&nflt=ht_id%3D204",
        "/hotel/fr/un-hotel.fr.html",
        "/book.html",
        "/mybooking.html",
        "/hotel/us/the-airstream-van.fr.html",
        "/hotelfeaturedreviews/x",
    ):
        attendu = parseur.can_fetch("https://www.booking.com" + chemin, config.USER_AGENT)
        assert verdict(texte, config.USER_AGENT, chemin)["autorise"] == attendu, chemin
