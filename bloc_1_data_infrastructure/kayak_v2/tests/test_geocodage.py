# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests de ``kayak.collecte.geocodage`` : géocodage Nominatim, archives brutes et refus.

Le fichier prouve qu'une réponse mal formée (latitude absente ou non numérique, objet au lieu
d'une liste) est REFUSÉE par une exception et non convertie en coordonnées inventées ; qu'un
statut HTTP d'erreur, un JSON illisible ou une coupure remontent sans rien archiver ; qu'une
archive du jour est réutilisée SANS appel ni pause (et reste intacte), qu'une archive d'un autre
jour ne l'est pas ; qu'une panne en cours de route garde les villes déjà archivées et n'écrit
pas coordonnees.csv ; et que les appels respectent la politique de Nominatim (paramètres exacts,
User-Agent du projet, pause de 1,1 s après chaque appel réseau). Aucun réseau : la session
HTTP est factice.
"""

import io
import json
import os
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from kayak import config
from kayak.collecte import geocodage
from kayak.collecte.geocodage import (
    collecter_coordonnees,
    geocoder,
    interpreter,
    interroger,
)
from kayak.stockage.data_lake import DataLake

URL_NOMINATIM = "https://nominatim.openstreetmap.org/search"
DOSSIER = "raw/geocodage/date=2026-10-10"
PARIS = [{"lat": "48.8588897", "lon": "2.3200410", "display_name": "Paris, Île-de-France"}]
LYON = [{"lat": "45.7578137", "lon": "4.8320114", "display_name": "Lyon"}]
NICE = [{"lat": "43.7009358", "lon": "7.2683912", "display_name": "Nice"}]
REPONSES = {"Paris, France": PARIS, "Lyon, France": LYON, "Nice, France": NICE}


@pytest.fixture(autouse=True)
def _date(monkeypatch):
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")


def _reponse(statut=200, contenu=b"[]", raison=""):
    """Vraie ``requests.Response`` (donc vrais ``raise_for_status`` et ``json``), sans réseau."""
    reponse = requests.Response()
    reponse.status_code = statut
    reponse.reason = raison
    reponse.url = URL_NOMINATIM
    reponse.raw = io.BytesIO(contenu)
    reponse.encoding = "utf-8"
    return reponse


def _json(donnees):
    """Corps JSON d'une réponse Nominatim."""
    return json.dumps(donnees, ensure_ascii=False).encode("utf-8")


class _SessionNominatim(requests.Session):
    """Vraie session ``requests`` (de vrais en-têtes) dont ``get`` est factice.

    Chaque appel est mémorisé avec l'URL, les paramètres, le délai et le User-Agent présent
    dans les en-têtes de la session AU MOMENT de l'appel, ainsi que le NOM exact sous lequel
    l'en-tête serait envoyé (requête préparée par la vraie session, sans réseau).
    """

    def __init__(self, reponses=None, statuts=None, erreur=None, corps_brut=None):
        super().__init__()
        self.reponses = REPONSES if reponses is None else reponses
        self.statuts = statuts or {}
        self.erreur = erreur
        self.corps_brut = corps_brut
        self.appels = []

    def get(self, url, **kwargs):
        params = kwargs["params"]
        self.appels.append(
            {
                "url": url,
                "params": params,
                "timeout": kwargs["timeout"],
                "user_agent": self.headers.get("User-Agent"),
                "noms_user_agent": [
                    cle
                    for cle in self.prepare_request(requests.Request("GET", url)).headers
                    if cle.lower() == "user-agent"
                ],
            }
        )
        if self.erreur is not None:
            raise self.erreur
        if self.corps_brut is not None:
            return _reponse(200, self.corps_brut)
        if params["q"] in self.statuts:
            statut, raison = self.statuts[params["q"]]
            return _reponse(statut, b"<html>erreur</html>", raison)
        return _reponse(200, _json(self.reponses.get(params["q"], [])))

    def villes_demandees(self):
        """Valeur de ``q`` de chaque appel, dans l'ordre."""
        return [appel["params"]["q"] for appel in self.appels]


class _Banc:
    """Remplace ``requests.Session`` et ``time.sleep`` dans le module géocodage.

    Il compte les sessions créées et note, pour chaque pause, sa durée et les archives déjà
    présentes à ce moment-là.
    """

    def __init__(self, lake, session):
        self.lake = lake
        self.session = session
        self.creations = []
        self.pauses = []
        self.etats = []

    def fabrique(self):
        self.creations.append("session créée")
        return self.session

    def sleep(self, secondes):
        self.pauses.append(secondes)
        self.etats.append(sorted(self.lake.lister("raw")))

    def installer(self, monkeypatch):
        monkeypatch.setattr(geocodage, "requests", SimpleNamespace(Session=self.fabrique))
        monkeypatch.setattr(geocodage, "time", SimpleNamespace(sleep=self.sleep))
        return self


def _destinations(*couples):
    """DataFrame (id_ville, ville) à partir de couples (identifiant, nom)."""
    return pd.DataFrame(
        {"id_ville": [i for i, _ in couples], "ville": [nom for _, nom in couples]}
    )


def _banc(tmp_path, monkeypatch, **options):
    """Lake local, session factice et banc branchés : renvoie (lake, session, banc)."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session = _SessionNominatim(**options)
    return lake, session, _Banc(lake, session).installer(monkeypatch)


# --------------------------------------------------------------------------------------------
# interpreter
# --------------------------------------------------------------------------------------------


def test_interpreter_convertit_en_float_sans_inverser_et_garde_le_nom_complet():
    """Latitude et longitude deviennent des ``float`` (dans cet ordre, signe et entiers compris),
    le nom complet est repris tel quel (absent : None, vide : vide), le reste est ignoré."""
    complet = interpreter(
        [{"lat": "43.2965", "lon": "5.3698", "display_name": "Marseille, France", "x": 1}]
    )
    assert complet == {"latitude": 43.2965, "longitude": 5.3698, "nom_complet": "Marseille, France"}
    assert isinstance(complet["latitude"], float) and isinstance(complet["longitude"], float)
    assert interpreter([{"lat": "-1.5", "lon": "45", "display_name": ""}]) == {
        "latitude": -1.5,
        "longitude": 45.0,
        "nom_complet": "",
    }
    assert interpreter([{"lat": 43.5, "lon": 4}]) == {
        "latitude": 43.5,
        "longitude": 4.0,
        "nom_complet": None,
    }


def test_reponse_vide_est_sans_effet_trois_none_et_pas_de_coordonnee_par_defaut():
    """Aucun résultat (ville inconnue) : trois None, jamais (0, 0) ni une exception."""
    assert interpreter([]) == {"latitude": None, "longitude": None, "nom_complet": None}


def test_resultats_suivants_sont_sans_effet_seul_le_premier_compte():
    """Nominatim classe par pertinence : on garde le premier, les autres n'y changent rien."""
    premier = {"lat": "1.5", "lon": "2.5", "display_name": "Premier"}
    second = {"lat": "9.5", "lon": "8.5", "display_name": "Second"}
    assert interpreter([premier, second]) == interpreter([premier])
    assert interpreter([premier, second])["nom_complet"] == "Premier"


def test_reponse_sans_latitude_est_refusee():
    """Un résultat sans « lat » lève ``KeyError`` : on n'invente pas la coordonnée manquante."""
    with pytest.raises(KeyError, match="lat"):
        interpreter([{"lon": "2.5", "display_name": "Sans latitude"}])


def test_reponse_sans_longitude_est_refusee():
    """Un résultat sans « lon » lève ``KeyError`` : un demi-point n'est pas un point."""
    with pytest.raises(KeyError, match="lon"):
        interpreter([{"lat": "1.5", "display_name": "Sans longitude"}])


@pytest.mark.parametrize(
    ("valeur", "erreur", "motif"),
    [
        ("abc", ValueError, "could not convert string to float"),
        ("", ValueError, "could not convert string to float"),
        (None, TypeError, "must be a string or a real number"),
    ],
)
def test_latitude_non_numerique_est_refusee(valeur, erreur, motif):
    """Une latitude illisible (texte, vide, nulle) lève une erreur au lieu de devenir NaN."""
    with pytest.raises(erreur, match=motif):
        interpreter([{"lat": valeur, "lon": "2.5"}])


@pytest.mark.parametrize(
    ("valeur", "erreur", "motif"),
    [
        ("abc", ValueError, "could not convert string to float"),
        ("", ValueError, "could not convert string to float"),
        (None, TypeError, "must be a string or a real number"),
    ],
)
def test_longitude_non_numerique_est_refusee(valeur, erreur, motif):
    """Une longitude illisible (texte, vide, nulle) lève une erreur au lieu de devenir NaN."""
    with pytest.raises(erreur, match=motif):
        interpreter([{"lat": "1.5", "lon": valeur}])


def test_objet_json_a_la_place_d_une_liste_est_refuse():
    """Un message d'erreur JSON (objet) n'est pas une liste de résultats : refusé, pas lu."""
    with pytest.raises(KeyError, match="0"):
        interpreter({"error": "Unable to geocode"})


# --------------------------------------------------------------------------------------------
# interroger et geocoder
# --------------------------------------------------------------------------------------------


def test_interroger_envoie_les_bons_parametres_et_rend_le_json_brut():
    """Un seul appel GET sur l'URL de Nominatim, avec q « <ville>, France », format jsonv2,
    limit 1 (entier), countrycodes fr et un délai de 30 s ; le JSON est rendu tel quel."""
    brut = [{"lat": "1.5", "lon": "2.5", "display_name": "Ville", "importance": 0.5}]
    session = _SessionNominatim(reponses={"Saintes Maries de la mer, France": brut})
    assert interroger("Saintes Maries de la mer", session) == brut
    assert [(a["url"], a["params"], a["timeout"]) for a in session.appels] == [
        (
            "https://nominatim.openstreetmap.org/search",
            {
                "q": "Saintes Maries de la mer, France",
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "fr",
            },
            30,
        )
    ]


@pytest.mark.parametrize(
    ("statut", "raison"),
    [(403, "Forbidden"), (429, "Too Many Requests"), (503, "Service Unavailable")],
)
def test_statut_http_d_erreur_est_propage_et_le_corps_n_est_pas_lu_comme_json(statut, raison):
    """Un refus (ban 403, limite 429, panne 503) lève ``HTTPError`` AVANT toute lecture du JSON :
    le corps HTML d'erreur n'est jamais pris pour une réponse."""
    session = _SessionNominatim(statuts={"Paris, France": (statut, raison)})
    with pytest.raises(requests.HTTPError, match=rf"{statut} (Client|Server) Error: {raison}"):
        interroger("Paris", session)


def test_json_illisible_leve_une_erreur_de_decodage():
    """Un 200 dont le corps n'est pas du JSON lève ``JSONDecodeError``, pas une liste vide."""
    session = _SessionNominatim(corps_brut=b"<html>maintenance</html>")
    with pytest.raises(requests.exceptions.JSONDecodeError, match="Expecting value"):
        interroger("Paris", session)


def test_coupure_reseau_est_propagee_par_interroger():
    """Un délai dépassé remonte tel quel : ni nouvelle tentative ni réponse vide."""
    delai = requests.Timeout("délai dépassé")
    with pytest.raises(requests.Timeout, match="délai dépassé") as info:
        interroger("Paris", _SessionNominatim(erreur=delai))
    assert info.value is delai


def test_geocoder_enchaine_interrogation_et_interpretation():
    """``geocoder`` rend les coordonnées de la ville ; une ville inconnue donne trois None."""
    session = _SessionNominatim()
    assert geocoder("Lyon", session) == {
        "latitude": 45.7578137,
        "longitude": 4.8320114,
        "nom_complet": "Lyon",
    }
    assert geocoder("Atlantide", session) == {
        "latitude": None,
        "longitude": None,
        "nom_complet": None,
    }
    assert session.villes_demandees() == ["Lyon, France", "Atlantide, France"]


def test_geocoder_propage_l_erreur_http_sans_rendre_de_coordonnees():
    """Un refus du service n'est pas converti en « ville inconnue » : l'erreur remonte."""
    session = _SessionNominatim(statuts={"Lyon, France": (429, "Too Many Requests")})
    with pytest.raises(requests.HTTPError, match="429 Client Error: Too Many Requests"):
        geocoder("Lyon", session)


# --------------------------------------------------------------------------------------------
# collecter_coordonnees : refus et archives
# --------------------------------------------------------------------------------------------


def test_archive_reutilisee_aucun_appel_ni_pause_archive_intacte(tmp_path, monkeypatch):
    """Une archive du jour est lue en UTF-8 : aucun appel, aucune pause, archive intacte."""
    lake, session, banc = _banc(tmp_path, monkeypatch)
    archive = [{"lat": "1.5", "lon": "2.5", "display_name": "Île d'Archive"}]
    chemin = f"{DOSSIER}/ville_01.json"
    lake.ecrire(chemin, json.dumps(archive, ensure_ascii=False))
    avant = lake.lire(chemin)
    coordonnees = collecter_coordonnees(_destinations((1, "Paris")), lake)
    assert session.appels == [] and banc.pauses == []
    assert lake.lire(chemin) == avant
    assert coordonnees["nom_complet"].tolist() == ["Île d'Archive"]
    assert coordonnees["latitude"].tolist() == [1.5]


def test_archive_d_une_autre_date_n_est_pas_reutilisee_et_reste_intacte(tmp_path, monkeypatch):
    """Seule l'archive de la date de collecte compte : celle de la veille déclenche un appel
    et n'est ni lue ni modifiée."""
    lake, session, banc = _banc(tmp_path, monkeypatch)
    veille = "raw/geocodage/date=2026-10-09/ville_01.json"
    lake.ecrire(veille, json.dumps([{"lat": "9.0", "lon": "9.0", "display_name": "Hier"}]))
    veille_avant = lake.lire(veille)
    coordonnees = collecter_coordonnees(_destinations((1, "Paris")), lake)
    assert session.villes_demandees() == ["Paris, France"]
    assert lake.lire(veille) == veille_avant
    assert coordonnees["latitude"].tolist() == [48.8588897]
    assert lake.existe(f"{DOSSIER}/ville_01.json")


def test_archive_illisible_leve_une_erreur_sans_appel_ni_ecriture(tmp_path, monkeypatch):
    """Une archive corrompue n'est pas écrasée en silence : ``JSONDecodeError``, aucun appel,
    archive intacte, pas de coordonnees.csv."""
    lake, session, _ = _banc(tmp_path, monkeypatch)
    chemin = f"{DOSSIER}/ville_01.json"
    lake.ecrire(chemin, "{pas du json")
    avant = lake.lire(chemin)
    with pytest.raises(json.JSONDecodeError, match="Expecting property name"):
        collecter_coordonnees(_destinations((1, "Paris")), lake)
    assert session.appels == []
    assert lake.lire(chemin) == avant
    assert not lake.existe(f"{DOSSIER}/coordonnees.csv")


def test_panne_en_cours_de_route_garde_les_archives_faites_et_la_relance_reprend(
    tmp_path, monkeypatch
):
    """Si la 2e ville échoue (503) : ``HTTPError``, la 1re reste archivée, ni la 2e ni la 3e ne
    le sont, coordonnees.csv n'est pas écrit. La relance ne redemande que les villes manquantes."""
    lake, session, banc = _banc(
        tmp_path, monkeypatch, statuts={"Lyon, France": (503, "Service Unavailable")}
    )
    villes = _destinations((1, "Paris"), (2, "Lyon"), (3, "Nice"))
    with pytest.raises(requests.HTTPError, match="503 Server Error: Service Unavailable"):
        collecter_coordonnees(villes, lake)
    assert sorted(lake.lister("raw")) == [f"{DOSSIER}/ville_01.json"]
    assert banc.pauses == [1.1]
    relance = _SessionNominatim()
    banc.session = relance
    collecter_coordonnees(villes, lake)
    assert relance.villes_demandees() == ["Lyon, France", "Nice, France"]
    assert lake.existe(f"{DOSSIER}/coordonnees.csv")


def test_coupure_sur_la_premiere_ville_n_archive_rien(tmp_path, monkeypatch):
    """Une coupure dès le premier appel remonte telle quelle : ni archive, ni CSV, ni pause."""
    coupure = requests.ConnectionError("réseau coupé")
    lake, _, banc = _banc(tmp_path, monkeypatch, erreur=coupure)
    with pytest.raises(requests.ConnectionError, match="réseau coupé") as info:
        collecter_coordonnees(_destinations((1, "Paris")), lake)
    assert info.value is coupure
    assert lake.lister("") == []
    assert banc.pauses == []


@pytest.mark.parametrize(
    ("archive", "erreur", "motif"),
    [
        ('{"error": "Unable to geocode"}', KeyError, "0"),
        ('[{"lon": "2.5", "display_name": "Sans latitude"}]', KeyError, "lat"),
        ('[{"lat": "abc", "lon": "2.5"}]', ValueError, "could not convert string to float"),
    ],
)
def test_archive_valide_mais_mal_formee_est_refusee_sans_appel_et_intacte(
    tmp_path, monkeypatch, archive, erreur, motif
):
    """Une archive qui est du JSON mais pas une réponse Nominatim (objet, latitude absente ou
    illisible) lève une erreur : pas d'appel de remplacement, archive intacte, pas de CSV."""
    lake, session, _ = _banc(tmp_path, monkeypatch)
    chemin = f"{DOSSIER}/ville_01.json"
    lake.ecrire(chemin, archive)
    avant = lake.lire(chemin)
    with pytest.raises(erreur, match=motif):
        collecter_coordonnees(_destinations((1, "Paris")), lake)
    assert session.appels == []
    assert lake.lire(chemin) == avant
    assert not lake.existe(f"{DOSSIER}/coordonnees.csv")


@pytest.mark.parametrize(
    ("colonnes", "manquante"),
    [({"id_ville": [1]}, "ville"), ({"ville": ["Paris"]}, "id_ville")],
)
def test_destinations_sans_colonne_obligatoire_sont_refusees(
    tmp_path, monkeypatch, colonnes, manquante
):
    """Il faut les colonnes id_ville et ville : sinon ``KeyError``, aucun appel, rien d'écrit."""
    lake, session, _ = _banc(tmp_path, monkeypatch)
    with pytest.raises(KeyError, match=manquante):
        collecter_coordonnees(pd.DataFrame(colonnes), lake)
    assert session.appels == []
    assert lake.lister("") == []


def test_destinations_vides_sans_effet_aucun_appel_aucune_archive_de_ville(tmp_path, monkeypatch):
    """Sans destination : aucun appel, aucune pause, aucune archive de ville ; seul le tableau
    (vide) est écrit."""
    lake, session, banc = _banc(tmp_path, monkeypatch)
    collecter_coordonnees(_destinations(), lake)
    assert session.appels == [] and banc.pauses == []
    assert lake.lister("raw") == [f"{DOSSIER}/coordonnees.csv"]


def test_collecte_ne_modifie_pas_le_dataframe_d_entree_et_ignore_ses_autres_colonnes(
    tmp_path, monkeypatch
):
    """Le DataFrame fourni reste inchangé ; ses colonnes en plus (statut...) ne passent pas
    dans le résultat, qui a exactement cinq colonnes."""
    lake, _, _ = _banc(tmp_path, monkeypatch)
    villes = _destinations((1, "Paris"), (2, "Lyon"))
    villes["statut"] = ["identique", "alias"]
    villes_avant = villes.copy()
    coordonnees = collecter_coordonnees(villes, lake)
    assert villes.equals(villes_avant)
    assert list(coordonnees.columns) == [
        "id_ville",
        "ville",
        "latitude",
        "longitude",
        "nom_complet",
    ]


def test_ville_inconnue_archivee_vide_et_la_relance_laisse_les_archives_inchangees(
    tmp_path, monkeypatch
):
    """Une ville inconnue donne une réponse « [] » archivée et des champs vides ; rejouer la
    collecte n'émet aucun appel et laisse toutes les archives inchangées."""
    lake, session, banc = _banc(tmp_path, monkeypatch)
    villes = _destinations((1, "Paris"), (2, "Atlantide"))
    premiere = collecter_coordonnees(villes, lake)
    assert lake.lire(f"{DOSSIER}/ville_02.json") == b"[]"
    assert premiere["latitude"].isna().tolist() == [False, True]
    assert premiere["nom_complet"].isna().tolist() == [False, True]
    lignes = lake.lire(f"{DOSSIER}/coordonnees.csv").decode("utf-8").split("\n")
    assert lignes[2] == "2,Atlantide,,,"
    avant = {c: lake.lire(c) for c in lake.lister("raw")}
    appels_avant = list(session.appels)
    pauses_avant = list(banc.pauses)
    collecter_coordonnees(villes, lake)
    assert {c: lake.lire(c) for c in lake.lister("raw")} == avant
    assert session.appels == appels_avant and banc.pauses == pauses_avant


def test_seul_l_appel_reseau_attend_une_archive_reutilisee_n_ajoute_aucune_pause(
    tmp_path, monkeypatch
):
    """Ville 1 archivée, ville 2 non : un seul appel (la ville 2), une seule pause de 1,1 s ;
    l'archive de la ville 1 reste intacte."""
    lake, session, banc = _banc(tmp_path, monkeypatch)
    chemin = f"{DOSSIER}/ville_01.json"
    lake.ecrire(chemin, _json(PARIS))
    avant = lake.lire(chemin)
    collecter_coordonnees(_destinations((1, "Paris"), (2, "Lyon")), lake)
    assert session.villes_demandees() == ["Lyon, France"]
    assert banc.pauses == [1.1]
    assert lake.lire(chemin) == avant


# --------------------------------------------------------------------------------------------
# collecter_coordonnees : le cas nominal
# --------------------------------------------------------------------------------------------


def test_deux_villes_une_session_user_agent_archives_et_pause_apres_chaque_appel(
    tmp_path, monkeypatch
):
    """Une seule session, avec le User-Agent du projet, interroge chaque ville ; chaque réponse
    est archivée (JSON indenté, accents non échappés, identifiant sur deux chiffres, flottant
    converti en entier) AVANT une pause de 1,1 s."""
    monkeypatch.setattr(config, "USER_AGENT", "RobotTest/1.0")
    lake, session, banc = _banc(tmp_path, monkeypatch)
    collecter_coordonnees(_destinations((3.0, "Paris"), (12.0, "Lyon")), lake)
    assert banc.creations == ["session créée"]
    assert [(a["url"], a["user_agent"]) for a in session.appels] == [
        (URL_NOMINATIM, "RobotTest/1.0"),
        (URL_NOMINATIM, "RobotTest/1.0"),
    ]
    assert session.villes_demandees() == ["Paris, France", "Lyon, France"]
    assert banc.pauses == [1.1, 1.1]
    assert banc.etats == [
        [f"{DOSSIER}/ville_03.json"],
        [f"{DOSSIER}/ville_03.json", f"{DOSSIER}/ville_12.json"],
    ]
    brut = lake.lire(f"{DOSSIER}/ville_03.json").decode("utf-8")
    assert brut.startswith('[\n  {\n    "lat": "48.8588897",')
    assert "Île-de-France" in brut and "\\u00ce" not in brut
    assert json.loads(brut) == PARIS
    assert json.loads(lake.lire(f"{DOSSIER}/ville_12.json")) == LYON


def test_user_agent_envoye_sous_son_nom_canonique(tmp_path, monkeypatch):
    """L'en-tête part sous le nom « User-Agent », écrit ainsi, et une seule fois.

    ``requests`` garde la casse du nom qu'on lui donne et l'envoie telle quelle (le nom par
    défaut « User-Agent » de la session est alors remplacé). Un serveur HTTP lit ce nom sans
    tenir compte de la casse, mais une trace réseau, ou le journal d'un serveur, montre le nom
    écrit : la politique d'usage de Nominatim et la norme HTTP l'écrivent « User-Agent ».
    """
    monkeypatch.setattr(config, "USER_AGENT", "RobotTest/1.0")
    lake, session, _ = _banc(tmp_path, monkeypatch)
    collecter_coordonnees(_destinations((3.0, "Paris")), lake)
    assert [appel["noms_user_agent"] for appel in session.appels] == [["User-Agent"]]


def test_csv_des_coordonnees_exact_fins_de_ligne_lf_et_dataframe_rendu(tmp_path, monkeypatch):
    """coordonnees.csv a un en-tête exact, des coordonnées converties, un nom entre guillemets
    quand il contient une virgule, des fins de ligne « \\n » (jamais « \\r\\n ») et le contenu
    du DataFrame rendu. La fin de ligne native du système est forcée à « \\r\\n » (celle de
    Windows) : le CSV doit rester en « \\n »."""
    monkeypatch.setattr(os, "linesep", "\r\n")
    lake, _, _ = _banc(tmp_path, monkeypatch)
    coordonnees = collecter_coordonnees(_destinations((3.0, "Paris"), (12.0, "Lyon")), lake)
    csv = lake.lire(f"{DOSSIER}/coordonnees.csv").decode("utf-8")
    assert csv == (
        "id_ville,ville,latitude,longitude,nom_complet\n"
        '3,Paris,48.8588897,2.320041,"Paris, Île-de-France"\n'
        "12,Lyon,45.7578137,4.8320114,Lyon\n"
    )
    assert coordonnees["id_ville"].tolist() == [3, 12]
    assert coordonnees["ville"].tolist() == ["Paris", "Lyon"]
    assert coordonnees["longitude"].tolist() == [2.320041, 4.8320114]
