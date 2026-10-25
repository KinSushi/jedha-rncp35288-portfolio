# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests INVERSES de ``kayak.collecte.destinations`` : ce que la collecte doit refuser ou ignorer.

Le fichier prouve qu'un titre autre que h2, un commentaire HTML ou un script, un nom voisin ou
l'alias d'une AUTRE ville n'ont aucun effet sur la liste extraite et sur la réconciliation ;
qu'une entrée non textuelle est refusée ; que la collecte s'arrête (``PermissionError``) quand
robots.txt interdit la page, quel que soit le motif (groupe nommé, ancre, joker) sans émettre la
moindre requête ni archiver quoi que ce soit ; qu'un statut HTTP d'erreur ou une coupure lève
l'exception sans rien archiver ni attendre ; et qu'une collecte rejouée ne modifie pas les
archives. Aucun réseau : robots.txt, ``requests.get`` et ``time.sleep`` sont factices.
"""

import io
import re
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from kayak import config
from kayak.collecte import destinations
from kayak.collecte.destinations import (
    ALIAS,
    collecter_destinations,
    extraire_destinations,
    normaliser,
    reconcilier,
)
from kayak.stockage.data_lake import DataLake

URL = "https://one-week-in.com/35-cities-to-visit-in-france/"
DOSSIER = "raw/destinations/date=2026-10-10"
PAGE = (
    "<html><body><h2>1. Paris</h2><h2>2. Verdon Gorge</h2>"
    "<h2>3. Nîmes (belle ville)</h2><h2>4. Besançon</h2></body></html>"
)
ABSENTE = "absente_de_la_source"
ROBOTS_PERMIS = "User-agent: *\nDisallow: /prive/\n"


@pytest.fixture(autouse=True)
def _date(monkeypatch):
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")


def _synthese(df):
    """Lignes (ville, ville_source, statut) d'une réconciliation, les valeurs manquantes en None."""
    return [
        (ville, None if pd.isna(source) else source, statut)
        for ville, source, statut in zip(df["ville"], df["ville_source"], df["statut"], strict=True)
    ]


def _reponse_http(statut, texte=PAGE, raison=""):
    """Vraie ``requests.Response`` (donc vrai ``raise_for_status``) construite sans réseau."""
    reponse = requests.Response()
    reponse.status_code = statut
    reponse.reason = raison
    reponse.url = URL
    reponse.raw = io.BytesIO(texte.encode("utf-8"))
    reponse.encoding = "utf-8"
    return reponse


class _ReponseRobots:
    """Réponse HTTP minimale de /robots.txt."""

    def __init__(self, statut, contenu):
        self.status_code = statut
        self.content = contenu
        self.text = contenu.decode("utf-8")
        self.headers = {"Date": "Sun, 11 Oct 2026 05:15:11 GMT"}


class _SessionRobots:
    """Session factice que ``archiver_robots`` crée lui-même : sert toujours le même robots.txt."""

    def __init__(self, statut, contenu=b""):
        self.statut = statut
        self.contenu = contenu
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _ReponseRobots(self.statut, self.contenu)


class _Scenario:
    """Remplace robots.txt, ``requests.get`` et ``time.sleep`` dans le module destinations.

    Il note l'ordre des événements, les arguments reçus et les pauses demandées (avec l'état des
    archives AU MOMENT de la pause).
    """

    def __init__(self, lake, robots=ROBOTS_PERMIS, reponse=None, erreur=None):
        self.lake = lake
        self.robots = robots
        self.reponse = reponse
        self.erreur = erreur
        self.evenements = []
        self.args_robots = []
        self.appels_get = []
        self.pauses = []
        self.fichiers_a_la_pause = None

    def archiver_robots(self, lake, url_site, source, session=None):
        self.evenements.append("robots")
        self.args_robots.append((lake, url_site, source))
        return {"texte": self.robots, "meta": {}}

    def get(self, url, **kwargs):
        self.evenements.append("get")
        self.appels_get.append((url, kwargs))
        if self.erreur is not None:
            raise self.erreur
        return self.reponse

    def sleep(self, secondes):
        self.evenements.append("pause")
        self.pauses.append(secondes)
        self.fichiers_a_la_pause = sorted(self.lake.lister("raw"))

    def installer(self, monkeypatch, robots_reel=False):
        """Branche le scénario ; ``robots_reel`` garde le vrai ``archiver_robots``."""
        if not robots_reel:
            monkeypatch.setattr(destinations, "archiver_robots", self.archiver_robots)
        monkeypatch.setattr(destinations, "requests", SimpleNamespace(get=self.get))
        monkeypatch.setattr(destinations, "time", SimpleNamespace(sleep=self.sleep))
        return self


def _archives(lake):
    """Contenu de toutes les archives du lake, par chemin (pour comparer avant / après)."""
    return {chemin: lake.lire(chemin) for chemin in lake.lister("")}


# --------------------------------------------------------------------------------------------
# extraire_destinations
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("titre", "attendu"),
    [
        ("1. Paris", "Paris"),
        ("10. Lyon", "Lyon"),
        ("35. Biarritz", "Biarritz"),
        ("  1.   Paris  ", "Paris"),
        ("1. Paris", "Paris"),
        ("<a href='#'>2. Nîmes</a>", "Nîmes"),
        ("<span>3.</span> <a>Colmar</a>", "Colmar"),
        ("4. Avignon (my favorite!)", "Avignon"),
        ("5. Nîmes (a) (b)", "Nîmes"),
        ("6. Colmar (très (joli))", "Colmar"),
        ("7. Lyon   (centre)", "Lyon"),
        ("8. Annecy(lac)", "Annecy"),
    ],
)
def test_titre_h2_numerote_ou_commente_devient_un_nom_propre(titre, attendu):
    """La numérotation « N. » et la parenthèse finale (même multiple ou imbriquée) sont retirées ;
    les balises internes, espaces et espaces insécables ne gênent pas."""
    assert extraire_destinations(f"<html><body><h2>{titre}</h2></body></html>") == [attendu]


def test_autres_niveaux_de_titre_sans_effet_ordre_et_doublons_conserves():
    """Seuls les h2 comptent (h1, h3, h4, paragraphes sont sans effet) ; l'ordre du document est
    gardé et un doublon n'est pas retiré."""
    html = (
        "<h1>0. Titre</h1><h2>1. Paris</h2><h3>2. Fantôme</h3><p>3. Autre</p>"
        "<h2>4. Lyon</h2><h4>x</h4><H2>5. Paris</H2>"
    )
    assert extraire_destinations(html) == ["Paris", "Lyon", "Paris"]


@pytest.mark.parametrize("html", ["", "<p>rien</p>", "<h1>1. Paris</h1>", "pas du html"])
def test_html_sans_h2_est_sans_effet_liste_vide(html):
    """Sans titre h2, rien n'est extrait : aucune destination n'est inventée."""
    assert extraire_destinations(html) == []


def test_commentaire_et_script_sont_sans_effet_sur_l_extraction():
    """Un h2 mis en commentaire ou écrit dans un script n'est pas un titre de la page."""
    html = (
        "<!-- <h2>1. Fantôme</h2> -->"
        '<script>document.write("<h2>2. Script</h2>")</script>'
        "<h2>3. Vrai</h2>"
    )
    assert extraire_destinations(html) == ["Vrai"]


@pytest.mark.parametrize(
    "titre",
    ["Paris", "Aix-en-Provence", "1 Paris", "Paris 75. Centre", "1) Paris", ".Paris", "a1. Paris"],
)
def test_titre_sans_numero_ou_au_numero_mal_forme_reste_intact(titre):
    """Seul « <chiffres>. » en TÊTE de titre est une numérotation : le titre reste inchangé."""
    assert extraire_destinations(f"<h2>{titre}</h2>") == [titre]


@pytest.mark.parametrize("html", [None, 42])
def test_extraction_refuse_un_html_non_textuel(html):
    """Une valeur qui n'est ni texte ni octets est refusée, pas transformée en page vide."""
    with pytest.raises(TypeError, match="Incoming markup is of an invalid type"):
        extraire_destinations(html)


# --------------------------------------------------------------------------------------------
# normaliser
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nom", "attendu"),
    [
        ("Uzès", "uzes"),
        ("ÉTRETAT", "etretat"),
        ("Besançon", "besancon"),
        ("Nîmes", "nimes"),
        ("étretat", "etretat"),
        ("ﬁnistère", "finistere"),
        ("Ａｒｌｅｓ", "arles"),
        ("Avignon (my favorite!)", "avignon my favorite"),
        ("St. Malo", "st malo"),
        ("Côte d'Azur", "cote dazur"),
        ("Paris 75", "paris"),
        ("A_B", "ab"),
        ("  Mont \t Saint\nMichel  ", "mont saint michel"),
        ("Gorges du Verdon", "gorges du verdon"),
        ("", ""),
        ("   ", ""),
        ("123", ""),
        ("!!!", ""),
        ("東京", ""),
    ],
)
def test_normaliser_forme_canonique_sans_accent_ni_ponctuation(nom, attendu):
    """Minuscules, accents retirés (y compris décomposés ou de compatibilité), seules les lettres
    latines et les espaces restent, les espaces multiples deviennent une seule espace."""
    assert normaliser(nom) == attendu


@pytest.mark.parametrize(
    "nom",
    ["Uzès", "Aix en Provence", "Côte d'Azur (nord)", "  Mont \t Saint  Michel ", "", "uzes"],
)
def test_normaliser_est_idempotent_le_deuxieme_passage_est_sans_effet(nom):
    """Normaliser un nom déjà normalisé le laisse inchangé : la clé de comparaison est stable."""
    une_fois = normaliser(nom)
    assert normaliser(une_fois) == une_fois


@pytest.mark.parametrize(
    ("valeur", "motif"),
    [(None, "NoneType"), (42, "int"), (b"Paris", "must be str")],
)
def test_normaliser_refuse_un_nom_non_textuel(valeur, motif):
    """Un nom absent, numérique ou en octets lève une erreur : il n'est pas converti en texte
    (« None » deviendrait la ville « none »)."""
    with pytest.raises((AttributeError, TypeError), match=motif):
        normaliser(valeur)


# --------------------------------------------------------------------------------------------
# reconcilier
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extraites", "reference", "attendu"),
    [
        pytest.param(["Paris"], ["Paris"], [("Paris", "Paris", "identique")], id="identique"),
        pytest.param(
            ["Besançon"], ["Besancon"], [("Besancon", "Besançon", "identique")], id="normalisee"
        ),
        pytest.param(
            ["Verdon Gorge"],
            ["Gorges du Verdon"],
            [("Gorges du Verdon", "Verdon Gorge", "alias")],
            id="alias_inverse",
        ),
        pytest.param(
            ["Gorges du Verdon"],
            ["Verdon Gorge"],
            [("Verdon Gorge", "Gorges du Verdon", "alias")],
            id="alias_direct",
        ),
        pytest.param(["Nimmes"], ["Nimes"], [("Nimes", "Nimmes", "alias")], id="alias_inverse_2"),
        pytest.param(["Nimes"], ["Nimmes"], [("Nimmes", "Nimes", "alias")], id="alias_direct_2"),
        pytest.param(["Paris"], ["Lyon"], [("Lyon", None, ABSENTE)], id="absente"),
    ],
)
def test_reconcilier_statut_et_ville_source_de_chaque_cas(extraites, reference, attendu):
    """Chaque cas de réconciliation donne son statut ET l'orthographe de la ville dans la source
    (celle de la source, pas celle de la référence ; None quand la ville est absente)."""
    assert _synthese(reconcilier(extraites, reference)) == attendu


def test_ids_colonnes_et_ordre_suivent_la_reference_doublons_et_defaut_compris():
    """id_ville vaut 1, 2, 3... dans l'ordre de la référence (doublons conservés), les colonnes
    sont exactement quatre ; sans référence fournie, c'est la liste de l'énoncé."""
    df = reconcilier(["B"], ["B", "A", "B"])
    assert df["id_ville"].tolist() == [1, 2, 3]
    assert list(df.columns) == ["id_ville", "ville", "ville_source", "statut"]
    assert _synthese(df) == [("B", "B", "identique"), ("A", None, ABSENTE), ("B", "B", "identique")]
    par_defaut = reconcilier(["Paris"])
    assert par_defaut["ville"].tolist() == config.VILLES_ENONCE
    assert par_defaut["id_ville"].tolist() == list(range(1, len(config.VILLES_ENONCE) + 1))
    paris = par_defaut["ville"] == "Paris"
    assert par_defaut.loc[paris, "statut"].tolist() == ["identique"]
    assert set(par_defaut.loc[~paris, "statut"]) == {ABSENTE}


def test_reconcilier_ne_modifie_ni_extraites_ni_reference_ni_alias():
    """La réconciliation n'altère aucune de ses entrées (listes, table d'alias) et, rejouée,
    rend un résultat identique."""
    extraites = ["Paris", "Verdon Gorge", "Nîmes"]
    reference = ["Paris", "Gorges du Verdon", "Nimes", "Lyon"]
    extraites_avant = list(extraites)
    reference_avant = list(reference)
    alias_avant = dict(ALIAS)
    reference_enonce_avant = list(config.VILLES_ENONCE)
    premier = reconcilier(extraites, reference)
    reconcilier(extraites)
    assert extraites == extraites_avant
    assert reference == reference_avant
    assert ALIAS == alias_avant
    assert config.VILLES_ENONCE == reference_enonce_avant
    pd.testing.assert_frame_equal(premier, reconcilier(extraites, reference))


def test_alias_sans_cible_dans_la_source_est_sans_effet():
    """Un alias n'est retenu que si son pendant est PRÉSENT dans la source : sinon la ville est
    déclarée absente, avec une ville_source vide."""
    for extraites, reference in (
        ([], ["Verdon Gorge"]),
        (["Autre"], ["Nimmes"]),
        (["Nimes"], ["Gorges du Verdon"]),
    ):
        assert _synthese(reconcilier(extraites, reference)) == [(reference[0], None, ABSENTE)]


def test_alias_d_une_autre_destination_est_sans_effet_sur_la_reference():
    """« Verdon Gorge » ou « Nimmes » dans la source ne rattachent ni « Lyon » ni « Nimes »
    (pour « Verdon Gorge ») : un alias n'agit que sur la destination qu'il désigne."""
    assert _synthese(reconcilier(["Verdon Gorge"], ["Nimes", "Lyon"])) == [
        ("Nimes", None, ABSENTE),
        ("Lyon", None, ABSENTE),
    ]
    assert _synthese(reconcilier(["Nimmes", "Verdon Gorge"], ["Lyon"])) == [
        ("Lyon", None, ABSENTE)
    ]


def test_ville_source_hors_reference_est_sans_effet_resultat_inchange():
    """Une ville de la source absente de la référence (« Lourdes ») n'ajoute aucune ligne : le
    résultat est inchangé par rapport à une source qui ne la contient pas."""
    avec = reconcilier(["Lourdes", "Paris", "Pau"], ["Paris", "Lyon"])
    sans = reconcilier(["Paris"], ["Paris", "Lyon"])
    pd.testing.assert_frame_equal(avec, sans)
    assert avec["ville"].tolist() == ["Paris", "Lyon"]


def test_nom_voisin_est_sans_effet_le_resultat_reste_celui_d_une_source_vide():
    """Il faut l'égalité des noms normalisés, pas une inclusion : « Parisien », « Paris Nord » ou
    « Saint Malo » ne valent ni « Paris » ni « St Malo » ; le résultat est inchangé par rapport
    à une source vide."""
    extraites = ["Parisien", "Paris Nord", "Saint Malo", "Lyonnais"]
    reference = ["Paris", "St Malo", "Lyon"]
    pd.testing.assert_frame_equal(reconcilier(extraites, reference), reconcilier([], reference))
    assert _synthese(reconcilier(extraites, reference)) == [
        ("Paris", None, ABSENTE),
        ("St Malo", None, ABSENTE),
        ("Lyon", None, ABSENTE),
    ]


def test_nom_present_rend_l_alias_sans_effet_et_l_exact_prime_sur_le_normalise():
    """Si le nom lui-même figure dans la source, l'alias ne sert pas ; entre « Nimes » exact et
    « Nîmes » accentué, l'exact gagne (quel que soit l'ordre dans la source)."""
    assert _synthese(reconcilier(["Nimes", "Nîmes", "Nimmes"], ["Nimes"])) == [
        ("Nimes", "Nimes", "identique")
    ]
    assert _synthese(reconcilier(["Nîmes", "Nimes"], ["Nimes"])) == [
        ("Nimes", "Nimes", "identique")
    ]
    assert _synthese(reconcilier(["Nîmes", "Nimmes"], ["Nimes"])) == [
        ("Nimes", "Nîmes", "identique")
    ]


def test_de_deux_alias_vers_la_meme_cible_le_second_est_sans_effet(monkeypatch):
    """La table d'alias donne l'ordre de priorité : le premier alias présent l'emporte et le
    suivant est sans effet, quel que soit l'ordre des villes dans la source."""
    assert ALIAS == {"verdon gorge": "Gorges du Verdon", "nimmes": "Nimes"}
    monkeypatch.setattr(destinations, "ALIAS", {"alpha": "Cible", "beta": "Cible"})
    for extraites in (["Alpha", "Beta"], ["Beta", "Alpha"]):
        assert _synthese(reconcilier(extraites, ["Cible"])) == [("Cible", "Alpha", "alias")]


@pytest.mark.parametrize(
    ("extraites", "reference", "motif"),
    [
        (None, ["Paris"], "not iterable"),
        (["Paris"], None, "not iterable"),
        ([None], ["Paris"], "NoneType"),
        (["Paris"], [None], "NoneType"),
    ],
)
def test_reconcilier_refuse_des_entrees_non_valides(extraites, reference, motif):
    """Une liste absente ou un nom absent sont refusés avec une erreur, pas réconciliés."""
    with pytest.raises((TypeError, AttributeError), match=motif):
        reconcilier(extraites, reference)


# --------------------------------------------------------------------------------------------
# collecter_destinations : refus de robots.txt
# --------------------------------------------------------------------------------------------


def test_robots_interdit_leve_permission_error_sans_requete_ni_archive_ni_pause(
    tmp_path, monkeypatch
):
    """Si robots.txt interdit la page : ``PermissionError`` citant l'URL, aucune requête vers
    la page, rien d'archivé, aucune pause. robots.txt est bien consulté pour ce site."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    scenario = _Scenario(lake, robots="User-agent: *\nDisallow: /\n").installer(monkeypatch)
    message = "robots.txt interdit la collecte sur " + URL
    with pytest.raises(PermissionError, match="^" + re.escape(message) + "$"):
        collecter_destinations(lake)
    assert scenario.args_robots == [(lake, URL, "destinations")]
    assert scenario.evenements == ["robots"]
    assert scenario.appels_get == [] and scenario.pauses == []
    assert lake.lister("") == []


@pytest.mark.parametrize(
    "robots",
    [
        "User-agent: RobotTest\nDisallow: /35-cities\n\nUser-agent: *\nDisallow:\n",
        "User-agent: *\nDisallow: /35-cities-to-visit-in-france/$\n",
        "User-agent: *\nDisallow: /*-to-visit-*\n",
        "User-agent: *\nDisallow: /35\nAllow: /3\n",
        "User-agent: *\nDisallow: /35-cities-to-visit-in-france/\nAllow: /3\n",
    ],
)
def test_motif_qui_vise_la_page_arrete_la_collecte(tmp_path, monkeypatch, robots):
    """Groupe nommé d'après le robot, ancre « $ » sur la page exacte, joker, ou « Disallow »
    plus long qu'un « Allow » : chacun suffit à interdire, donc à lever ``PermissionError``."""
    monkeypatch.setattr(config, "USER_AGENT", "RobotTest/1.0")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    scenario = _Scenario(lake, robots=robots).installer(monkeypatch)
    with pytest.raises(PermissionError, match="robots.txt interdit la collecte"):
        collecter_destinations(lake)
    assert scenario.appels_get == []
    assert lake.lister("") == []


@pytest.mark.parametrize(
    "robots",
    [
        "",
        "User-agent: *\nDisallow:\n",
        "User-agent: *\nDisallow: /autre/\n",
        "User-agent: *\nDisallow: /35-cities-to-visit-in-france/suite\n",
        "User-agent: *\nDisallow: /35\nAllow: /35-cities\n",
        "User-agent: Autre\nDisallow: /\n",
    ],
)
def test_regles_ne_visant_pas_la_page_sont_sans_effet_sur_la_collecte(
    tmp_path, monkeypatch, robots
):
    """Des règles qui ne visent pas cette page (autre chemin, sous-page, groupe d'un autre robot,
    « Allow » plus précis) sont sans effet : la page est demandée puis archivée."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    scenario = _Scenario(lake, robots=robots, reponse=_reponse_http(200)).installer(monkeypatch)
    collecter_destinations(lake)
    assert [url for url, _ in scenario.appels_get] == [URL]
    assert lake.existe(f"{DOSSIER}/page.html")


def test_robots_reel_interdit_robots_archive_page_et_csv_non_crees(tmp_path, monkeypatch):
    """Avec le vrai module robots : le robots.txt reçu est archivé (preuve) puis la collecte
    s'arrête ; ni page.html ni destinations.csv ne sont créés, aucune requête vers la page."""
    robots = _SessionRobots(200, b"User-agent: *\nDisallow: /35-cities\n")
    monkeypatch.setattr(requests, "Session", lambda: robots)
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    scenario = _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch, robots_reel=True)
    with pytest.raises(PermissionError, match="robots.txt interdit la collecte"):
        collecter_destinations(lake)
    assert robots.urls == ["https://one-week-in.com/robots.txt"]
    assert sorted(lake.lister("raw")) == [f"{DOSSIER}/robots.txt", f"{DOSSIER}/robots_meta.json"]
    assert scenario.appels_get == [] and scenario.pauses == []


@pytest.mark.parametrize("statut", [403, 500])
def test_robots_illisible_arrete_la_collecte_sans_requete_ni_archive(
    tmp_path, monkeypatch, statut
):
    """Un robots.txt illisible (403, 5xx) lève ``PermissionError`` : on ne collecte pas sur la
    foi d'un fichier qu'on n'a pas pu lire. Aucune page demandée, rien d'archivé."""
    monkeypatch.setattr(requests, "Session", lambda: _SessionRobots(statut))
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    scenario = _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch, robots_reel=True)
    with pytest.raises(PermissionError, match=rf"robots\.txt illisible \({statut}\)"):
        collecter_destinations(lake)
    assert scenario.appels_get == [] and scenario.pauses == []
    assert lake.lister("") == []


def test_robots_txt_absent_404_est_sans_effet_restrictif_la_collecte_se_poursuit(
    tmp_path, monkeypatch
):
    """Pas de robots.txt (404) : aucune restriction. Seul le statut est archivé ; la page et le
    CSV le sont aussi, et aucun fichier robots.txt n'est inventé."""
    monkeypatch.setattr(requests, "Session", lambda: _SessionRobots(404, b"introuvable"))
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch, robots_reel=True)
    collecter_destinations(lake)
    assert sorted(lake.lister("raw")) == [
        f"{DOSSIER}/destinations.csv",
        f"{DOSSIER}/page.html",
        f"{DOSSIER}/robots_meta.json",
    ]


# --------------------------------------------------------------------------------------------
# collecter_destinations : échecs de la requête
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statut", "raison"),
    [
        (404, "Not Found"),
        (429, "Too Many Requests"),
        (500, "Internal Server Error"),
        (503, "Service Unavailable"),
    ],
)
def test_statut_http_d_erreur_leve_et_rien_n_est_archive_ni_pause(
    tmp_path, monkeypatch, statut, raison
):
    """Une page en erreur (4xx, 5xx) lève ``HTTPError`` : ni page.html ni CSV ne sont écrits,
    pas de pause, et le HTML d'erreur n'est pas réconcilié comme s'il s'agissait de la page."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    reponse = _reponse_http(statut, texte="<h2>1. Paris</h2>", raison=raison)
    scenario = _Scenario(lake, reponse=reponse).installer(monkeypatch)
    with pytest.raises(requests.HTTPError, match=rf"{statut} (Client|Server) Error: {raison}"):
        collecter_destinations(lake)
    assert scenario.evenements == ["robots", "get"]
    assert scenario.pauses == []
    assert lake.lister("") == []


def test_coupure_reseau_est_propagee_rien_archive_pas_de_pause(tmp_path, monkeypatch):
    """Une coupure pendant le téléchargement remonte telle quelle ; rien n'est archivé."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    coupure = requests.ConnectionError("coupure réseau")
    scenario = _Scenario(lake, erreur=coupure).installer(monkeypatch)
    with pytest.raises(requests.ConnectionError, match="coupure réseau") as info:
        collecter_destinations(lake)
    assert info.value is coupure
    assert scenario.pauses == []
    assert lake.lister("") == []


# --------------------------------------------------------------------------------------------
# collecter_destinations : le cas nominal et la relance
# --------------------------------------------------------------------------------------------


def test_robots_puis_page_puis_pause_dans_l_ordre_avec_user_agent_et_delai(tmp_path, monkeypatch):
    """robots.txt d'abord, puis la page (User-Agent du projet, délai de 30 s), puis UNE pause
    de 1 s, une fois la page et le CSV déjà archivés."""
    monkeypatch.setattr(config, "USER_AGENT", "RobotTest/1.0")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    scenario = _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch)
    collecter_destinations(lake)
    assert destinations.URL_SOURCE == URL
    assert scenario.evenements == ["robots", "get", "pause"]
    assert scenario.args_robots == [(lake, URL, "destinations")]
    assert scenario.appels_get == [
        (URL, {"headers": {"User-Agent": "RobotTest/1.0"}, "timeout": 30})
    ]
    assert scenario.pauses == [1.0]
    assert scenario.fichiers_a_la_pause == [f"{DOSSIER}/destinations.csv", f"{DOSSIER}/page.html"]


def test_page_brute_et_csv_archives_exactement_et_dataframe_rendu(tmp_path, monkeypatch):
    """La page est archivée telle que reçue (accents compris) ; le CSV réconcilié a un en-tête
    exact, une ligne par ville de l'énoncé, des fins de ligne « \\n » et le même contenu que le
    DataFrame rendu."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch)
    df = collecter_destinations(lake)
    assert lake.lire(f"{DOSSIER}/page.html") == PAGE.encode("utf-8")
    csv = lake.lire(f"{DOSSIER}/destinations.csv").decode("utf-8")
    assert "\r" not in csv and csv.endswith("\n")
    lignes = csv.split("\n")
    assert lignes[0] == "id_ville,ville,ville_source,statut"
    assert lignes[1] == "1,Mont Saint Michel,,absente_de_la_source"
    assert lignes[6] == "6,Paris,Paris,identique"
    assert lignes[13] == "13,Besancon,Besançon,identique"
    assert lignes[18] == "18,Gorges du Verdon,Verdon Gorge,alias"
    assert lignes[25] == "25,Nimes,Nîmes,identique"
    relu = pd.read_csv(io.StringIO(csv))
    assert relu["ville"].tolist() == df["ville"].tolist() == config.VILLES_ENONCE
    assert relu["statut"].tolist() == df["statut"].tolist()
    trouvees = [ligne for ligne in _synthese(df) if ligne[2] != ABSENTE]
    assert trouvees == [
        ("Paris", "Paris", "identique"),
        ("Besancon", "Besançon", "identique"),
        ("Gorges du Verdon", "Verdon Gorge", "alias"),
        ("Nimes", "Nîmes", "identique"),
    ]


def test_deuxieme_collecte_identique_laisse_les_archives_inchangees(tmp_path, monkeypatch):
    """Rejouer la collecte le même jour, sur la même page, réécrit les mêmes fichiers : les
    archives sont identiques octet pour octet (idempotence)."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch)
    collecter_destinations(lake)
    avant = _archives(lake)
    _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch)
    collecter_destinations(lake)
    assert _archives(lake) == avant


def test_collecte_d_un_autre_jour_laisse_les_archives_de_la_veille_intactes(tmp_path, monkeypatch):
    """Chaque jour a son dossier : collecter le lendemain n'altère pas les archives de la veille."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    _Scenario(lake, reponse=_reponse_http(200)).installer(monkeypatch)
    collecter_destinations(lake)
    veille_avant = _archives(lake)
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-11")
    _Scenario(lake, reponse=_reponse_http(200, texte="<h2>1. Lyon</h2>")).installer(monkeypatch)
    collecter_destinations(lake)
    assert {c: lake.lire(c) for c in veille_avant} == veille_avant
    assert lake.lire("raw/destinations/date=2026-10-11/page.html") == b"<h2>1. Lyon</h2>"
