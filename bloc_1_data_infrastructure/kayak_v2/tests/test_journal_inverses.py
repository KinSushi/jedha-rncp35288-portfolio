# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests INVERSES de ``kayak.collecte.journal`` : ce que le journal HTTP ne doit jamais garder.

Le fichier prouve que tout paramètre hors de la liste blanche, tout corps de requête, tout
en-tête et tout corps de réponse sont écartés du journal alors que la requête réelle part et
revient intacte ; qu'une erreur est consignée puis relancée sans être avalée ; que ``Session.send``
est toujours restaurée (même après une exception) ; et qu'un journal existant n'est jamais
écrasé ni altéré par une collecte qui n'a rien émis. Aucun réseau : les requêtes traversent
un adaptateur de transport factice branché sur une vraie session ``requests``.
"""

import io
import json
from datetime import datetime

import pytest
import requests

from kayak.collecte import journal
from kayak.collecte.journal import PARAMETRES_CONSERVES, _entree, journaliser_requetes
from kayak.stockage.data_lake import DataLake

DATE_SERVEUR = "Sun, 11 Oct 2026 05:15:11 GMT"


def _chemin(date="2026-10-10"):
    """Chemin du journal dans le lake pour une date de collecte."""
    return f"raw/journal_http/date={date}/journal_http.jsonl"


class _Transport(requests.adapters.BaseAdapter):
    """Adaptateur sans réseau : répond avec un statut fixe ou lève l'erreur donnée.

    Il mémorise chaque couple (requête reçue, options d'envoi) pour prouver ce qui part
    réellement vers le « serveur ».
    """

    def __init__(self, statut=200, corps=b"ok", erreur=None, entetes=None):
        super().__init__()
        self.statut = statut
        self.corps = corps
        self.erreur = erreur
        self.entetes = {"Date": DATE_SERVEUR} if entetes is None else entetes
        self.recus = []

    def send(self, requete, **options):
        self.recus.append((requete, options))
        if self.erreur is not None:
            raise self.erreur
        reponse = requests.Response()
        reponse.status_code = self.statut
        reponse.raw = io.BytesIO(self.corps)
        reponse.headers.update(self.entetes)
        reponse.url = requete.url
        reponse.request = requete
        return reponse

    def close(self):
        return None


def _session(**options):
    """Vraie session ``requests`` au transport https factice ; renvoie (session, transport)."""
    transport = _Transport(**options)
    session = requests.Session()
    session.mount("https://", transport)
    return session, transport


class _Horloge:
    """Remplace ``datetime`` dans le module : un instant fixe, microsecondes comprises."""

    @staticmethod
    def now(tz=None):
        return datetime(2026, 10, 10, 8, 30, 15, 987654, tzinfo=tz)


class _Chrono:
    """Remplace ``time`` dans le module : ``perf_counter`` rend les valeurs données."""

    def __init__(self, *valeurs):
        self._suite = iter(valeurs)

    def perf_counter(self):
        return next(self._suite)


def _figer(monkeypatch, *instants):
    """Fige l'horodatage UTC et la suite des lectures du chronomètre (valeurs en secondes)."""
    monkeypatch.setattr(journal, "datetime", _Horloge)
    monkeypatch.setattr(journal, "time", _Chrono(*instants))


def _lignes(lake, date="2026-10-10"):
    """Entrées du journal d'une date, relues depuis le lake."""
    texte = lake.lire(_chemin(date)).decode("utf-8")
    return [json.loads(ligne) for ligne in texte.splitlines()]


@pytest.fixture(autouse=True)
def _date(monkeypatch):
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")


# --------------------------------------------------------------------------------------------
# _entree : les champs d'une entrée
# --------------------------------------------------------------------------------------------


def test_entree_get_complete_champs_formats_et_valeurs(monkeypatch):
    """Une entrée GET a exactement sept champs : heure UTC à la seconde, méthode, hôte avec
    port, chemin, paramètres de la liste blanche, 0 octet envoyé et durée au millième."""
    _figer(monkeypatch, 100.123456)
    url = "https://exemple.test:8443/api/v1/search?q=Paris&appid=SECRET&format=jsonv2"
    requete = requests.Request("GET", url).prepare()
    assert _entree(requete, 100.0) == {
        "horodatage_utc": "2026-10-10T08:30:15+00:00",
        "methode": "GET",
        "hote": "exemple.test:8443",
        "chemin": "/api/v1/search",
        "parametres": "q=Paris&format=jsonv2",
        "octets_envoyes": 0,
        "duree_s": 0.123,
    }


def test_parametres_hors_liste_blanche_ecartes_requete_intacte():
    """Clé d'API, jeton, unités, langue : tout paramètre hors liste est écarté du journal.

    La requête réelle n'est pas touchée (son URL garde la clé, sinon l'appel échouerait).
    """
    url = (
        "https://exemple.test/api?lat=43.5&appid=CLE-SECRETE&key=K&api_key=A"
        "&token=T&units=metric&lon=4.2&lang=fr"
    )
    requete = requests.Request("GET", url).prepare()
    url_avant = requete.url
    entree = _entree(requete, 0.0)
    assert entree["parametres"] == "lat=43.5&lon=4.2"
    assert "SECRETE" not in json.dumps(entree)
    assert requete.url == url_avant
    seuls_secrets = requests.Request("GET", "https://exemple.test/api?appid=Z&units=metric")
    assert _entree(seuls_secrets.prepare(), 0.0)["parametres"] == ""


def test_liste_blanche_complete_ordre_et_encodage_conserves():
    """Les dix paramètres de la liste blanche sont gardés, dans l'ordre de l'URL, doublons
    compris, ré-encodés (espace en « + », « / » en « %2F », accent en %C3%A8)."""
    assert PARAMETRES_CONSERVES == {
        "q",
        "format",
        "limit",
        "countrycodes",
        "latitude",
        "longitude",
        "lat",
        "lon",
        "timezone",
        "forecast_days",
    }
    url = (
        "https://exemple.test/x?q=Aix+en+Provence&format=jsonv2&limit=1&countrycodes=fr"
        "&latitude=43.5&longitude=5.4&lat=43.6&lon=5.5&timezone=Europe%2FParis"
        "&forecast_days=7&q=Uz%C3%A8s"
    )
    entree = _entree(requests.Request("GET", url).prepare(), 0.0)
    assert entree["parametres"] == (
        "q=Aix+en+Provence&format=jsonv2&limit=1&countrycodes=fr&latitude=43.5"
        "&longitude=5.4&lat=43.6&lon=5.5&timezone=Europe%2FParis&forecast_days=7&q=Uz%C3%A8s"
    )


def test_nom_de_parametre_exact_casse_et_prefixes_sans_effet():
    """Le nom doit être EXACTEMENT celui de la liste : « Q », « LAT », « qq », « lat2 » ou
    « xlimit » ne passent pas (sinon une clé nommée « lat_secret » serait conservée)."""
    url = (
        "https://exemple.test/x?Q=Paris&LAT=1&qq=2&lat2=3&latitude_x=4&xlimit=5&Format=json"
        "&timezone2=6&forecast_days_=7"
    )
    assert _entree(requests.Request("GET", url).prepare(), 0.0)["parametres"] == ""


def test_requete_sans_url_ni_corps_sans_effet_sur_hote_chemin_et_parametres():
    """Une requête non préparée (ni URL ni méthode) donne des champs vides, sans exception."""
    entree = _entree(requests.PreparedRequest(), 0.0)
    assert (entree["hote"], entree["chemin"], entree["parametres"]) == ("", "", "")
    assert entree["methode"] is None
    assert entree["octets_envoyes"] == 0


# --------------------------------------------------------------------------------------------
# _entree : le corps de la requête (Overpass en POST)
# --------------------------------------------------------------------------------------------

CORPS_OVERPASS = b"data=%5Bout%3Ajson%5D%3Bnode%28around%3A8000%2C43.1%2C5.5%29%3Bout%3B"
CORPS_ACCENT = b"data=node(around:8000,43.1,5.5)" + "é".encode()


@pytest.mark.parametrize(
    ("corps", "autour"),
    [
        (CORPS_OVERPASS, "43.1,5.5"),
        (CORPS_ACCENT, "43.1,5.5"),
        ("data=node(around:500,-1.25,-2.5)", "-1.25,-2.5"),
        ("data=node(around:500,43,5)", "43,5"),
        ("data=node+(around:8000,45.9,6.1)", "45.9,6.1"),
        (b"\xff\xfedata=around:8000,1.5,2.5", "1.5,2.5"),
        ("around:100,1.0,2.0 puis around:200,3.0,4.0", "1.0,2.0"),
    ],
)
def test_centre_de_recherche_overpass_extrait_du_corps_apres_les_parametres_d_url(corps, autour):
    """Du corps POST, seul le centre « around:<rayon>,<lat>,<lon> » est gardé (lat puis lon,
    signe et décimales compris, premier centre seulement), ajouté après les paramètres d'URL.

    Un corps en octets invalides est décodé avec remplacement, sans exception.
    """
    requete = requests.Request("POST", "https://exemple.test/api?lat=1", data=corps).prepare()
    entree = _entree(requete, 0.0)
    attendu = "lat=1&" + "autour=" + autour.replace(",", "%2C")
    assert entree["parametres"] == attendu
    assert entree["octets_envoyes"] == len(corps)


@pytest.mark.parametrize(
    "corps",
    [
        "data=[out:json];out;",
        "around:8000,43.1",
        "around:,1,2",
        "around:8000,a,b",
        "AROUND:8000,1,2",
        "around:8000;1;2",
        "around 8000,1,2",
    ],
)
def test_corps_sans_centre_valide_n_ajoute_aucun_parametre_sans_effet(corps):
    """Un corps sans « around:<rayon>,<lat>,<lon> » bien formé ne change pas les paramètres ;
    seule sa taille est comptée."""
    requete = requests.Request("POST", "https://exemple.test/api?lat=1", data=corps).prepare()
    entree = _entree(requete, 0.0)
    assert entree["parametres"] == "lat=1"
    assert entree["octets_envoyes"] == len(corps)


@pytest.mark.parametrize("corps", [None, b"", ""])
def test_corps_absent_ou_vide_sans_effet_zero_octet_envoye(corps):
    """Pas de corps (GET) ou corps vide : 0 octet envoyé et aucun centre de recherche."""
    requete = requests.Request("POST", "https://exemple.test/api?lat=1").prepare()
    requete.body = corps
    entree = _entree(requete, 0.0)
    assert entree["octets_envoyes"] == 0
    assert entree["parametres"] == "lat=1"


def test_corps_en_flux_n_est_ni_lu_ni_consomme_flux_intact():
    """Un corps en flux (itérateur, fichier) n'est pas du texte : le journal le laisse intact.

    Le lire pour y chercher un centre volerait des octets à la vraie requête.
    """
    requete = requests.Request("POST", "https://exemple.test/api?lat=1").prepare()
    flux = iter([b"data=around:8000,43.1,5.5", b"suite"])
    requete.body = flux
    entree = _entree(requete, 0.0)
    assert (entree["octets_envoyes"], entree["parametres"]) == (0, "lat=1")
    assert next(flux) == b"data=around:8000,43.1,5.5"
    fichier = io.BytesIO(b"data=around:8000,43.1,5.5")
    requete.body = fichier
    assert _entree(requete, 0.0)["parametres"] == "lat=1"
    assert fichier.tell() == 0


def test_corps_secret_jamais_recopie_et_requete_intacte():
    """Mot de passe et jeton d'un corps POST n'entrent jamais dans l'entrée ; la méthode est
    conservée telle quelle (« POST ») et la requête réelle garde son corps."""
    corps = "password=hunter2&token=T0K3N&data=node(around:8000,43.1,5.5)"
    requete = requests.Request("POST", "https://exemple.test/api/interpreter", data=corps).prepare()
    corps_avant = requete.body
    entree = _entree(requete, 0.0)
    brut = json.dumps(entree)
    assert "hunter2" not in brut and "T0K3N" not in brut and "password" not in brut
    assert entree["parametres"] == "autour=43.1%2C5.5"
    assert entree["methode"] == "POST"
    assert requete.body == corps_avant


# --------------------------------------------------------------------------------------------
# journaliser_requetes : ce qui est consigné et ce qui ne l'est pas
# --------------------------------------------------------------------------------------------


def test_requete_reussie_est_consignee_avec_tous_ses_champs(tmp_path, monkeypatch):
    """Succès : onze champs (sept communs + statut, octets reçus, date serveur, erreur nulle).

    Les octets reçus sont des OCTETS (« é » en pèse 2), le fichier est un JSON par ligne, terminé
    par « \\n », sans échappement des accents, et n'est écrit qu'à la sortie du bloc.
    """
    _figer(monkeypatch, 50.0, 50.123456)
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session(
        statut=201, corps="héllo".encode(), entetes={"Date": "mer. 14 oct. 2026 à 05:15 GMT"}
    )
    with journaliser_requetes(lake) as entrees:
        session.get("https://exemple.test/search?q=Paris&appid=S")
        assert not lake.existe(_chemin())
    attendu = {
        "horodatage_utc": "2026-10-10T08:30:15+00:00",
        "methode": "GET",
        "hote": "exemple.test",
        "chemin": "/search",
        "parametres": "q=Paris",
        "octets_envoyes": 0,
        "duree_s": 0.123,
        "statut_http": 201,
        "octets_recus": len("héllo".encode()),
        "date_serveur": "mer. 14 oct. 2026 à 05:15 GMT",
        "erreur": None,
    }
    assert entrees == [attendu]
    brut = lake.lire(_chemin()).decode("utf-8")
    assert brut.endswith("\n")
    assert [json.loads(ligne) for ligne in brut.splitlines()] == [attendu]
    assert "à" in brut and "\\u00e0" not in brut


def test_echec_reseau_consigne_sans_champs_de_reponse_puis_relance(tmp_path, monkeypatch):
    """Une coupure est consignée (statut nul, nom de l'erreur, aucun champ de réponse) puis la
    MÊME exception remonte à l'appelant : le journal n'avale rien."""
    _figer(monkeypatch, 10.0, 10.5)
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    coupure = requests.ConnectionError("coupure réseau")
    session, _ = _session(erreur=coupure)
    with pytest.raises(requests.ConnectionError, match="coupure réseau") as info:
        with journaliser_requetes(lake) as entrees:
            session.post("https://exemple.test/api?lat=1", data="x")
    assert info.value is coupure
    assert entrees == [
        {
            "horodatage_utc": "2026-10-10T08:30:15+00:00",
            "methode": "POST",
            "hote": "exemple.test",
            "chemin": "/api",
            "parametres": "lat=1",
            "octets_envoyes": 1,
            "duree_s": 0.5,
            "statut_http": None,
            "erreur": "ConnectionError",
        }
    ]
    assert _lignes(lake) == entrees


@pytest.mark.parametrize(
    "erreur",
    [
        requests.Timeout("trop lent"),
        requests.exceptions.SSLError("certificat refusé"),
        ValueError("valeur absurde"),
        OSError("disque plein"),
    ],
)
def test_toute_exception_est_consignee_avec_son_nom_de_type_puis_relancee(tmp_path, erreur):
    """Pas seulement les erreurs de ``requests`` : tout échec d'envoi laisse sa trace (le nom
    de son type) puis remonte inchangé."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session(erreur=erreur)
    with pytest.raises(type(erreur), match=str(erreur)):
        with journaliser_requetes(lake) as entrees:
            session.get("https://exemple.test/x")
    assert [(e["statut_http"], e["erreur"]) for e in entrees] == [(None, type(erreur).__name__)]


def test_statut_d_erreur_http_est_consigne_sans_lever_d_exception(tmp_path):
    """Un 404 n'est pas une panne de transport : le journal l'enregistre et rend la réponse,
    sans appeler ``raise_for_status`` à la place de l'appelant."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session(statut=404, corps=b"introuvable")
    with journaliser_requetes(lake) as entrees:
        reponse = session.get("https://exemple.test/absent")
    assert reponse.status_code == 404
    assert entrees[0]["statut_http"] == 404
    assert entrees[0]["erreur"] is None
    assert entrees[0]["octets_recus"] == len(b"introuvable")


def test_requete_reelle_et_reponse_intactes_malgre_le_journal(tmp_path):
    """Le journal écarte la clé, le mot de passe, l'en-tête et la réponse de SON fichier, mais
    la requête part intacte vers le serveur et la réponse revient intacte à l'appelant."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, transport = _session(corps=b"contenu de la reponse")
    with journaliser_requetes(lake):
        reponse = session.post(
            "https://exemple.test/api?appid=SECRET&lat=1",
            data="mot-de-passe=hunter2",
            headers={"Authorization": "Bearer JETON"},
        )
    envoyee, _ = transport.recus[0]
    assert envoyee.url == "https://exemple.test/api?appid=SECRET&lat=1"
    assert envoyee.body == "mot-de-passe=hunter2"
    assert envoyee.headers["Authorization"] == "Bearer JETON"
    assert reponse.status_code == 200 and reponse.content == b"contenu de la reponse"
    brut = lake.lire(_chemin())
    for secret in (b"SECRET", b"hunter2", b"JETON", b"contenu de la reponse"):
        assert secret not in brut, secret


def test_options_d_envoi_transmises_a_l_adaptateur_sans_perte(tmp_path):
    """Le délai, la vérification TLS et le mode flux de l'appelant atteignent le transport :
    le journal enveloppe ``send`` sans perdre ses options."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, transport = _session()
    with journaliser_requetes(lake):
        session.get("https://exemple.test/x", timeout=7, verify=False, stream=False)
    _, options = transport.recus[0]
    assert options["timeout"] == 7
    assert options["verify"] is False
    assert options["stream"] is False


def test_send_restaure_et_journal_ecrit_meme_si_le_bloc_leve(tmp_path):
    """Une panne dans le bloc ne laisse pas ``Session.send`` remplacée et n'efface pas la trace
    des requêtes déjà faites : c'est justement alors qu'on a besoin du journal."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session()
    avant = requests.sessions.Session.send
    with pytest.raises(RuntimeError, match="panne en cours de collecte"):
        with journaliser_requetes(lake):
            session.get("https://exemple.test/un")
            raise RuntimeError("panne en cours de collecte")
    assert requests.sessions.Session.send is avant
    assert [e["chemin"] for e in _lignes(lake)] == ["/un"]


def test_exception_dans_le_bloc_sans_requete_n_ecrit_aucun_journal(tmp_path):
    """Sans requête émise, même une exception ne crée aucun fichier de journal."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    avant = requests.sessions.Session.send
    with pytest.raises(RuntimeError, match="avant toute requête"):
        with journaliser_requetes(lake):
            raise RuntimeError("avant toute requête")
    assert requests.sessions.Session.send is avant
    assert not lake.existe(_chemin())
    assert lake.lister("") == []


def test_requetes_apres_le_bloc_ne_sont_plus_consignees_journal_inchange(tmp_path):
    """Une fois le bloc fermé, une requête n'ajoute rien : liste et fichier restent inchangés."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session()
    with journaliser_requetes(lake) as entrees:
        session.get("https://exemple.test/dans")
    fichier_avant = lake.lire(_chemin())
    entrees_avant = list(entrees)
    session.get("https://exemple.test/apres")
    assert lake.lire(_chemin()) == fichier_avant
    assert entrees == entrees_avant


def test_journal_precedent_conserve_octet_pour_octet_intact(tmp_path):
    """Une collecte relancée le même jour complète le journal : l'ancien contenu (accents
    compris) reste intact en tête du fichier, les nouvelles lignes viennent après."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    precedent = '{"chemin": "/hier", "note": "é"}\n'.encode()
    lake.ecrire(_chemin(), precedent)
    session, _ = _session()
    with journaliser_requetes(lake):
        session.get("https://exemple.test/nouveau")
    assert lake.lire(_chemin()).startswith(precedent)
    assert [e["chemin"] for e in _lignes(lake)] == ["/hier", "/nouveau"]


def test_second_bloc_sans_requete_laisse_le_journal_du_premier_intact(tmp_path):
    """Un bloc qui n'émet rien n'écrit rien : le journal du premier est identique."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session()
    with journaliser_requetes(lake):
        session.get("https://exemple.test/un")
    avant = lake.lire(_chemin())
    with journaliser_requetes(lake) as entrees:
        pass
    assert entrees == []
    assert lake.lire(_chemin()) == avant


def test_journal_d_une_autre_date_reste_intact(tmp_path, monkeypatch):
    """Chaque jour a son fichier : collecter un autre jour n'altère pas le journal de la veille."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session()
    with journaliser_requetes(lake):
        session.get("https://exemple.test/lundi")
    veille_avant = lake.lire(_chemin("2026-10-10"))
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-11")
    with journaliser_requetes(lake):
        session.get("https://exemple.test/mardi")
    assert lake.lire(_chemin("2026-10-10")) == veille_avant
    assert [e["chemin"] for e in _lignes(lake, "2026-10-11")] == ["/mardi"]


def test_deux_blocs_ne_partagent_pas_leurs_entrees_la_premiere_liste_reste_inchangee(tmp_path):
    """Chaque bloc a sa propre liste : le second ne modifie pas celle du premier."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session, _ = _session()
    with journaliser_requetes(lake) as premieres:
        session.get("https://exemple.test/un")
    premieres_avant = list(premieres)
    with journaliser_requetes(lake) as secondes:
        session.get("https://exemple.test/deux")
    assert premieres == premieres_avant
    assert [e["chemin"] for e in premieres] == ["/un"]
    assert [e["chemin"] for e in secondes] == ["/deux"]


def test_requete_refusee_avant_l_envoi_n_est_pas_consignee(tmp_path):
    """Une URL sans schéma est refusée par ``requests`` avant ``send`` : rien à consigner, et
    aucun journal n'est créé."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with pytest.raises(requests.exceptions.MissingSchema, match="No scheme supplied"):
        with journaliser_requetes(lake) as entrees:
            requests.Session().get("exemple.test/sans-schema")
    assert entrees == []
    assert lake.lister("") == []


def test_schema_sans_adaptateur_est_consigne_comme_erreur_puis_relance(tmp_path):
    """Un schéma non pris en charge (ftp) échoue DANS ``send`` : l'échec est consigné sous le nom
    de son type, puis relancé."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with pytest.raises(requests.exceptions.InvalidSchema, match="No connection adapters"):
        with journaliser_requetes(lake) as entrees:
            requests.Session().get("ftp://exemple.test/x")
    assert [(e["statut_http"], e["erreur"]) for e in entrees] == [(None, "InvalidSchema")]


def test_sessions_creees_avant_ou_pendant_le_bloc_sont_toutes_journalisees(tmp_path):
    """Le remplacement se fait sur la classe : une session créée AVANT le bloc et une créée
    PENDANT sont journalisées, dans l'ordre des envois."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    ancienne, _ = _session()
    with journaliser_requetes(lake) as entrees:
        ancienne.get("https://exemple.test/ancienne")
        recente, _ = _session()
        recente.get("https://exemple.test/recente")
    assert [e["chemin"] for e in entrees] == ["/ancienne", "/recente"]
    assert [e["chemin"] for e in _lignes(lake)] == ["/ancienne", "/recente"]
