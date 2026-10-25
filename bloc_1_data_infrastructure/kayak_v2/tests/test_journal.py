# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests du journal des requêtes HTTP d'une collecte (``kayak.collecte.journal``).

Aucun réseau : les requêtes passent par un adaptateur de transport factice, branché sur une vraie
session ``requests`` (donc par le vrai chemin ``Session.send`` que le journal intercepte).
"""

import io
import json

import pytest
import requests

from kayak.collecte.journal import journaliser_requetes
from kayak.stockage.data_lake import DataLake

CHEMIN = "raw/journal_http/date=2026-10-11/journal_http.jsonl"


class _Transport(requests.adapters.BaseAdapter):
    """Adaptateur qui répond sans réseau : un statut fixe, ou une erreur de connexion."""

    def __init__(self, statut: int = 200, corps: bytes = b"ok", erreur: Exception | None = None):
        super().__init__()
        self.statut, self.corps, self.erreur = statut, corps, erreur

    def send(self, requete, **options):
        if self.erreur:
            raise self.erreur
        reponse = requests.Response()
        reponse.status_code = self.statut
        reponse.raw = io.BytesIO(self.corps)  # le corps est lu par ``content``, sans champ privé
        reponse.headers["Date"] = "Sun, 11 Oct 2026 05:15:11 GMT"
        reponse.url = requete.url
        reponse.request = requete
        return reponse

    def close(self):
        return None


def _session(**options) -> requests.Session:
    session = requests.Session()
    session.mount("https://", _Transport(**options))
    return session


@pytest.fixture(autouse=True)
def _date(monkeypatch):
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-11")


def _lignes(lake: DataLake) -> list[dict]:
    return [json.loads(ligne) for ligne in lake.lire(CHEMIN).decode("utf-8").splitlines()]


def test_chaque_requete_est_consignee_avec_son_statut(tmp_path):
    """Statut, taille reçue, hôte, chemin et date serveur sont archivés pour chaque requête."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with journaliser_requetes(lake):
        _session(statut=200, corps=b"x" * 123).get("https://exemple.test/search?q=Paris")
        _session(statut=429, corps=b"trop").get("https://exemple.test/autre")
    lignes = _lignes(lake)
    assert [(e["statut_http"], e["hote"], e["chemin"]) for e in lignes] == [
        (200, "exemple.test", "/search"),
        (429, "exemple.test", "/autre"),
    ]
    assert lignes[0]["octets_recus"] == 123 and lignes[0]["parametres"] == "q=Paris"
    assert lignes[0]["date_serveur"] == "Sun, 11 Oct 2026 05:15:11 GMT"
    assert lignes[0]["methode"] == "GET" and lignes[0]["erreur"] is None


def test_aucun_secret_ni_corps_dans_le_journal(tmp_path):
    """Une clé d'API et le corps d'une requête POST ne sont jamais écrits dans le journal."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with journaliser_requetes(lake):
        _session().get("https://exemple.test/onecall?lat=43.5&lon=4.2&appid=CLE-SECRETE")
        _session().post(
            "https://exemple.test/api/interpreter",
            data={"data": '[out:json];node(around:8000,43.1,5.5);out;'},
        )
    brut = lake.lire(CHEMIN).decode("utf-8")
    assert "CLE-SECRETE" not in brut and "appid" not in brut
    assert "out:json" not in brut  # le corps de la requête n'est pas gardé
    lignes = _lignes(lake)
    assert lignes[0]["parametres"] == "lat=43.5&lon=4.2"  # coordonnées conservées
    # Du corps Overpass, seul le centre de recherche est gardé (pour savoir de quelle ville).
    assert lignes[1]["parametres"] == "autour=43.1%2C5.5" and lignes[1]["octets_envoyes"] > 0


def test_une_erreur_reseau_est_consignee_puis_relancee(tmp_path):
    """Une coupure est journalisée (sans statut) et l'exception reste visible de l'appelant."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with pytest.raises(requests.ConnectionError):
        with journaliser_requetes(lake):
            _session(erreur=requests.ConnectionError("coupure")).get("https://exemple.test/x")
    ligne = _lignes(lake)[0]
    assert ligne["statut_http"] is None and ligne["erreur"] == "ConnectionError"


def test_le_journal_complete_celui_d_une_collecte_precedente(tmp_path):
    """Relancer une collecte le même jour AJOUTE au journal : la trace des essais est gardée."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with journaliser_requetes(lake):
        _session().get("https://exemple.test/un")
    with journaliser_requetes(lake):
        _session().get("https://exemple.test/deux")
    assert [e["chemin"] for e in _lignes(lake)] == ["/un", "/deux"]


def test_session_send_est_restauree_et_rien_n_est_ecrit_sans_requete(tmp_path):
    """Hors du bloc, requests est intact ; sans requête, aucun journal n'est créé."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    avant = requests.sessions.Session.send
    with journaliser_requetes(lake):
        assert requests.sessions.Session.send is not avant
    assert requests.sessions.Session.send is avant
    assert not lake.existe(CHEMIN)
