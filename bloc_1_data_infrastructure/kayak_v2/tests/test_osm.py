# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests de la collecte OpenStreetMap : requête sans troncature, conversion, archives.

Contexte : la première requête (``out center tags 40``) coupait chaque réponse à 40 éléments,
points avant contours, de sorte que le « top 20 » reposait sur un échantillon tronqué. Ces tests
verrouillent la correction (aucun plafond dans la requête, réponse partielle refusée) et la
lecture hors ligne des archives de l'ancienne requête. Aucun test n'appelle le réseau : la
session HTTP est factice et ``time.sleep`` est neutralisé.
"""

import json
import re

import pandas as pd
import pytest
import requests

from kayak.collecte import hotels_osm
from kayak.collecte.hotels_osm import (
    DECALAGE_WAY,
    MARQUE_REQUETE,
    PLAFOND_ANCIENNE_REQUETE,
    ReponseIncomplete,
    archive_reutilisable,
    construire_hotels,
    convertir,
    diagnostic_troncature,
    interroger,
    reconvertir_depuis_archive,
    requete_overpass,
    verifier_reponse,
)
from kayak.stockage.data_lake import DataLake

DATE = "2026-10-10"


def test_convertir_point_et_contour():
    point = {
        "type": "node",
        "id": 42,
        "lat": 45.9,
        "lon": 6.12,
        "tags": {
            "name": "Hôtel du Lac",
            "stars": "3",
            "website": "https://lac.example",
            "addr:street": "Quai",
            "addr:city": "Annecy",
        },
    }
    contour = {
        "type": "way",
        "id": 7,
        "center": {"lat": 45.8, "lon": 6.1},
        "tags": {"name": "Grand Hôtel", "stars": "4S"},
    }
    h1 = convertir(point, 15, "Annecy", "2026-10-10")
    h2 = convertir(contour, 15, "Annecy", "2026-10-10")
    assert h1["hotel_id"] == 42 and h1["etoiles"] == 3 and h1["url"] == "https://lac.example"
    assert h1["adresse"] == "Quai Annecy" and h1["note"] is None
    assert h1["source"] == "openstreetmap"
    # Un contour a son propre espace d'identifiants et ses coordonnées dans « center ».
    assert h2["hotel_id"] == DECALAGE_WAY + 7 and h2["latitude"] == 45.8
    assert h2["etoiles"] == 4
    assert h2["url"] == "https://www.openstreetmap.org/way/7"


def test_convertir_garde_la_commune_a_part():
    """addr:city devient le champ « commune » (None si absent), en plus de l'adresse."""
    avec = {
        "type": "node",
        "id": 1,
        "lat": 1,
        "lon": 1,
        "tags": {"name": "A", "addr:city": "Nîmes"},
    }
    sans = {"type": "node", "id": 2, "lat": 1, "lon": 1, "tags": {"name": "B"}}
    assert convertir(avec, 1, "Nimes", DATE)["commune"] == "Nîmes"
    assert convertir(sans, 1, "Nimes", DATE)["commune"] is None


def test_convertir_garde_le_pays_declare():
    """addr:country devient le champ « pays » (None si absent) : l'ETL s'en sert."""
    kehl = {
        "type": "node",
        "id": 3,
        "lat": 48.57,
        "lon": 7.82,
        "tags": {"name": "Europa-Hotel", "addr:city": "Kehl", "addr:country": "DE"},
    }
    sans = {"type": "node", "id": 4, "lat": 1, "lon": 1, "tags": {"name": "B"}}
    assert convertir(kehl, 9, "Strasbourg", DATE)["pays"] == "DE"
    assert convertir(sans, 9, "Strasbourg", DATE)["pays"] is None


def test_hotel_sans_nom_ignore():
    assert (
        convertir({"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": {}}, 1, "X", "d") is None
    )


def test_requete_overpass():
    requete = requete_overpass(45.9, 6.1)
    assert '"tourism"="hotel"' in requete and "around:8000,45.9,6.1" in requete


def test_requete_limitee_a_la_france():
    """La recherche est restreinte à la zone administrative de la France.

    Sans ce filtre, le rayon de 8 km autour de Strasbourg traversait le Rhin et ramenait les
    hôtels de Kehl (Allemagne), que le rectangle « France » de l'ETL laissait passer.
    """
    requete = requete_overpass(48.58, 7.75)
    assert 'area["ISO3166-1"="FR"][admin_level=2]->.pays;' in requete
    # Le filtre porte sur les points ET sur les contours.
    assert 'node["tourism"="hotel"]["name"](area.pays)' in requete
    assert 'way["tourism"="hotel"]["name"](area.pays)' in requete


def test_requete_sans_plafond_ni_troncature():
    """La requête demande points ET contours, sans limite de nombre.

    L'ancienne se terminait par « out center tags 40 » : Overpass renvoie les points avant les
    contours puis coupe, donc des villes entières ne rendaient que des points.
    """
    requete = requete_overpass(43.6, 1.44)
    assert 'node["tourism"="hotel"]' in requete and 'way["tourism"="hotel"]' in requete
    assert requete.endswith("out center tags;")
    # Aucun nombre dans l'instruction de sortie (la dernière) : pas de limite arbitraire. Les
    # réglages du début (« [timeout:90] ») portent des nombres, pas elle.
    instruction_sortie = requete.split(";")[-2]
    assert instruction_sortie == "out center tags"
    assert re.search(r"\d", instruction_sortie) is None


def test_reponse_partielle_refusee():
    """Une réponse HTTP 200 qui signale une erreur d'exécution est partielle : on la refuse."""
    partielle = {
        "elements": [{"id": 1}],
        "remark": 'runtime error: Query timed out in "query" at line 1 after 91 seconds.',
    }
    with pytest.raises(ReponseIncomplete):
        verifier_reponse(partielle)
    complete = {"elements": [{"id": 1}], "remark": "autre remarque sans erreur"}
    assert verifier_reponse(complete) is complete


class _Reponse:
    """Réponse HTTP minimale (statut, JSON) comme celle de requests."""

    def __init__(self, statut=200, donnees=None):
        self.status_code = statut
        self._donnees = donnees if donnees is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._donnees


class _Session:
    """Session factice : rend les réponses programmées l'une après l'autre."""

    def __init__(self, *reponses):
        self.reponses = list(reponses)
        self.appels = 0

    def post(self, url, data=None, timeout=None):
        self.appels += 1
        return self.reponses.pop(0)


@pytest.fixture(autouse=True)
def _sans_attente(monkeypatch):
    """Aucune attente réelle dans les tests, et la date de collecte est fixée."""
    monkeypatch.setattr(hotels_osm.time, "sleep", lambda _s: None)
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", DATE)


def test_interroger_redemande_une_reponse_partielle():
    """Réponse partielle puis réponse complète : la seconde est retenue, après un nouvel essai."""
    partielle = {"elements": [], "remark": "runtime error: Query ran out of memory"}
    complete = {"elements": [{"type": "node", "id": 1}]}
    session = _Session(_Reponse(200, partielle), _Reponse(200, complete))
    assert interroger(session, "requete") == complete
    assert session.appels == 2


def _element(identifiant: int, nom: str | None = "Hôtel", commune: str | None = None) -> dict:
    etiquettes = {"tourism": "hotel"}
    if nom:
        etiquettes["name"] = f"{nom} {identifiant}"
    if commune:
        etiquettes["addr:city"] = commune
    return {"type": "node", "id": identifiant, "lat": 43.0, "lon": 2.0, "tags": etiquettes}


def _lake_avec_archive_ancienne(dossier, nb_elements: int) -> tuple[DataLake, pd.DataFrame]:
    """Lake avec une archive produite par l'ANCIENNE requête (sans marque) pour une ville."""
    lake = DataLake(bucket=None, racine_locale=dossier)
    coordonnees = pd.DataFrame(
        {"id_ville": [1], "ville": ["Toulouse"], "latitude": [43.6], "longitude": [1.44]}
    )
    lake.ecrire(f"raw/geocodage/date={DATE}/coordonnees.csv", coordonnees.to_csv(index=False))
    ancienne = {"elements": [_element(i, commune="Toulouse") for i in range(nb_elements)]}
    lake.ecrire(f"raw/osm/date={DATE}/ville_01.json", json.dumps(ancienne))
    return lake, coordonnees


def test_archive_reutilisable():
    """Archive de l'ancienne requête : fiable sous le plafond, suspecte au plafond."""
    assert archive_reutilisable({"elements": [1] * (PLAFOND_ANCIENNE_REQUETE - 1)})
    assert not archive_reutilisable({"elements": [1] * PLAFOND_ANCIENNE_REQUETE})
    # La marque de la nouvelle requête rend l'archive fiable quelle que soit sa taille.
    assert archive_reutilisable({MARQUE_REQUETE: "q", "elements": [1] * 500})


def test_collecte_remplace_une_archive_plafonnee(tmp_path):
    """Une archive à 40 éléments (ancienne requête) est redemandée, puis réutilisée ensuite."""
    lake, coordonnees = _lake_avec_archive_ancienne(tmp_path, PLAFOND_ANCIENNE_REQUETE)
    complete = {"elements": [_element(i) for i in range(57)]}  # plus de 40 : plus de plafond
    session = _Session(_Reponse(200, complete))
    hotels = construire_hotels(coordonnees, lake, session)
    assert session.appels == 1 and [h["hotel_id"] for h in hotels] == list(range(57))
    archive = json.loads(lake.lire(f"raw/osm/date={DATE}/ville_01.json"))
    assert MARQUE_REQUETE in archive
    assert [e["id"] for e in archive["elements"]] == list(range(57))
    # Seconde exécution : l'archive porte la marque, aucune nouvelle requête.
    relus = construire_hotels(coordonnees, lake, _Session())
    assert [h["hotel_id"] for h in relus] == list(range(57))


def test_collecte_garde_une_archive_complete(tmp_path):
    """Une archive sous le plafond était complète : elle est relue sans requête."""
    lake, coordonnees = _lake_avec_archive_ancienne(tmp_path, 12)
    session = _Session()
    relus = construire_hotels(coordonnees, lake, session)
    assert [h["hotel_id"] for h in relus] == list(range(12))
    assert session.appels == 0


def test_reconversion_hors_ligne_sans_aucun_reseau(tmp_path, monkeypatch):
    """La reconversion rejoue les archives (même plafonnées) sans jamais ouvrir de session."""

    def interdit(*_args, **_kwargs):
        raise AssertionError("aucun appel réseau n'est permis en mode hors ligne")

    monkeypatch.setattr(requests, "Session", interdit)
    lake, _ = _lake_avec_archive_ancienne(tmp_path, PLAFOND_ANCIENNE_REQUETE)
    hotels = reconvertir_depuis_archive(lake)
    assert len(hotels) == PLAFOND_ANCIENNE_REQUETE
    assert (hotels["commune"] == "Toulouse").all()
    ecrit = lake.lire(f"raw/osm/date={DATE}/hotels.jsonl").decode("utf-8").splitlines()
    assert len(ecrit) == PLAFOND_ANCIENNE_REQUETE
    assert json.loads(ecrit[0])["commune"] == "Toulouse"


def test_diagnostic_troncature(tmp_path):
    """Le diagnostic compte les villes dont l'archive ancienne atteint le plafond."""
    lake, _ = _lake_avec_archive_ancienne(tmp_path, PLAFOND_ANCIENNE_REQUETE)
    lake.ecrire(
        f"raw/osm/date={DATE}/ville_02.json",
        json.dumps({"elements": [_element(i) for i in range(5)]}),
    )
    lake.ecrire(
        f"raw/osm/date={DATE}/ville_03.json",
        json.dumps({MARQUE_REQUETE: "q", "elements": [_element(i) for i in range(60)]}),
    )
    # Une seconde archive ancienne au plafond : les villes plafonnées s'additionnent.
    lake.ecrire(
        f"raw/osm/date={DATE}/ville_04.json",
        json.dumps({"elements": [_element(i) for i in range(PLAFOND_ANCIENNE_REQUETE + 3)]}),
    )
    assert diagnostic_troncature(lake) == {
        "villes": 4,
        "archives_ancienne_requete": 3,
        "villes_plafonnees": 2,
        "plafond": PLAFOND_ANCIENNE_REQUETE,
    }
