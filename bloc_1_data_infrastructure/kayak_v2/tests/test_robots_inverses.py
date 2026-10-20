# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests INVERSES de ``kayak.collecte.robots`` : ce que l'interpréteur doit refuser ou ignorer.

Le fichier prouve qu'une ligne de robots.txt mal formée, un commentaire, une directive inconnue,
un motif qui ne correspond pas ou un User-Agent vide n'ont AUCUN effet sur les règles retenues
(la lecture reste identique à celle du fichier propre), que seule une réponse HTTP 200 est
archivée comme robots.txt (404 et 410 valent « pas de fichier », tout le reste lève
``PermissionError`` avec un message précis), et que l'archive reste intacte en cas de refus ou
de coupure réseau. Aucun réseau : les sessions HTTP sont factices.
"""

import hashlib
import json
import re

import pytest
import requests

from kayak import config
from kayak.collecte.robots import (
    _motif_vers_regex,
    archiver_robots,
    jeton_produit,
    lire_groupes,
    regles_applicables,
    verdict,
)
from kayak.stockage.data_lake import DataLake

UA = "KayakEtudeCertif/1.0 (test)"
SHA256_VIDE = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
DATE_SERVEUR = "Sun, 11 Oct 2026 05:15:11 GMT"
URL_ROBOTS = "https://www.exemple.test/robots.txt"


def _libre(groupe="*"):
    """Verdict attendu quand aucune règle ne correspond : autorisé, sans règle décisive."""
    return {"autorise": True, "regle": None, "correspondantes": [], "groupe": groupe}


class _Reponse:
    """Réponse HTTP minimale, comme celle de requests."""

    def __init__(self, statut, contenu=b"", entetes=None, texte=None):
        self.status_code = statut
        self.content = contenu
        self.text = contenu.decode("utf-8", "replace") if texte is None else texte
        self.headers = {"Date": DATE_SERVEUR} if entetes is None else entetes


class _Session:
    """Session factice : mémorise chaque appel, rend une réponse ou lève une erreur."""

    def __init__(self, reponse=None, erreur=None):
        self.reponse = reponse
        self.erreur = erreur
        self.appels = []

    def get(self, url, **kwargs):
        self.appels.append((url, kwargs))
        if self.erreur is not None:
            raise self.erreur
        return self.reponse


# --------------------------------------------------------------------------------------------
# jeton_produit
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "attendu"),
    [
        ("KayakEtudeCertif/1.0 (projet; robot)", "kayaketudecertif"),
        ("Bot (https://exemple.test/bot)", "bot"),
        ("Bot(x)", "bot"),
        ("Bot\tNom/2", "bot"),
        ("  Bot/1.0  ", "bot"),
        ("  BOT", "bot"),
        ("Bot", "bot"),
        ("", ""),
        ("   ", ""),
        ("/1.0", ""),
    ],
)
def test_jeton_produit_est_le_premier_mot_en_minuscules(agent, attendu):
    """Le jeton produit s'arrête au premier « / », espace ou « ( » et se compare en minuscules.

    Les espaces de tête sont retirés d'abord, sinon le premier morceau serait vide.
    """
    assert jeton_produit(agent) == attendu


# --------------------------------------------------------------------------------------------
# lire_groupes : ce qui est ignoré
# --------------------------------------------------------------------------------------------


def test_commentaire_en_fin_de_ligne_est_sans_effet():
    """Tout ce qui suit « # » est un commentaire : le fichier se lit comme s'il était absent.

    Le commentaire peut contenir « : » ou un mot-clé (« Allow ») sans créer la moindre règle.
    """
    propre = "User-agent: *\nDisallow: /a/\n"
    commente = (
        "User-agent: * # tous les robots\n"
        "# Disallow: /fantome/\n"
        "Disallow: /a/ # privé : ne pas indexer\n"
        "   # Allow: /a/\n"
    )
    assert lire_groupes(commente) == lire_groupes(propre)
    assert lire_groupes(commente) == [{"agents": ["*"], "regles": [("Disallow", "/a/")]}]


def test_ligne_sans_deux_points_est_sans_effet():
    """Une ligne sans « : » n'est pas une directive : elle est sautée sans lever d'erreur.

    Sans cette garde, le découpage « directive : valeur » échouerait sur la première ligne
    de bruit d'un fichier réel.
    """
    propre = "User-agent: *\nDisallow: /a/\n"
    bruite = "User-agent: *\nDisallow /oublie\nrobots sans separateur\n   \nDisallow: /a/\n"
    assert lire_groupes(bruite) == lire_groupes(propre)


def test_directives_inconnues_sont_sans_effet():
    """Sitemap, Crawl-delay et les directives inventées ne créent aucune règle d'accès.

    « Disallowed » ou « Allowance » ne sont pas « Disallow » / « Allow » : l'égalité est stricte.
    """
    propre = "User-agent: *\nDisallow: /a/\n"
    bruite = propre + (
        "Sitemap: https://exemple.test/plan.xml\n"
        "Crawl-delay: 10\n"
        "Host: exemple.test\n"
        "Noindex: /b/\n"
        "Disallowed: /c/\n"
        "Allowance: /d/\n"
    )
    assert lire_groupes(bruite) == lire_groupes(propre)


def test_regle_avant_tout_user_agent_est_sans_effet():
    """Une règle écrite avant le premier « User-agent » n'appartient à aucun groupe : ignorée.

    L'accepter lui donnerait une portée que le fichier ne déclare pas.
    """
    texte = "Disallow: /orpheline/\nAllow: /autre/\nUser-agent: *\nDisallow: /z/\n"
    assert lire_groupes(texte) == [{"agents": ["*"], "regles": [("Disallow", "/z/")]}]


def test_motif_vide_est_sans_effet_mais_le_groupe_reste():
    """« Disallow: » et « Allow: » à motif vide ne limitent rien : écartés, groupe conservé.

    Le groupe existe donc (il peut être choisi pour un robot), mais sans aucune règle.
    """
    texte = "User-agent: *\nDisallow:\nAllow:   \nDisallow:   # rien\n"
    assert lire_groupes(texte) == [{"agents": ["*"], "regles": []}]


@pytest.mark.parametrize("texte", ["", "\n\n  \n", "# seulement\n# un: commentaire\n"])
def test_texte_sans_directive_est_sans_effet_aucun_groupe(texte):
    """Un fichier vide ou fait de commentaires ne produit aucun groupe."""
    assert lire_groupes(texte) == []


# --------------------------------------------------------------------------------------------
# lire_groupes : la lecture d'une ligne et la frontière des groupes
# --------------------------------------------------------------------------------------------


def test_directives_sans_casse_ni_espaces_et_valeur_avec_deux_points():
    """Directive insensible à la casse et aux espaces ; motif gardé tel quel, « : » compris.

    Seul le premier « : » sépare la directive de sa valeur ; la casse du motif est conservée
    (les chemins sont sensibles à la casse), celle des noms d'agents est abaissée.
    """
    texte = "  USER-AGENT :  KayakBot \n  DISALLOW  :  /Prive:a:b  \nallow:/Prive/ok\n"
    assert lire_groupes(texte) == [
        {
            "agents": ["kayakbot"],
            "regles": [("Disallow", "/Prive:a:b"), ("Allow", "/Prive/ok")],
        }
    ]


def test_frontieres_des_groupes_lignes_vides_et_nouvel_agent_apres_une_regle():
    """Les lignes vides ou commentées entre deux « User-agent » ne coupent pas le groupe ;
    un « User-agent » placé après une règle en ouvre un nouveau (RFC 9309, § 2.2.1)."""
    texte = (
        "User-agent: a\n\n# pause\nUser-agent: b\nDisallow: /x\n"
        "User-agent: c\nDisallow: /y\n"
    )
    assert lire_groupes(texte) == [
        {"agents": ["a", "b"], "regles": [("Disallow", "/x")]},
        {"agents": ["c"], "regles": [("Disallow", "/y")]},
    ]


# --------------------------------------------------------------------------------------------
# regles_applicables : quel groupe retenir
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["", "   "])
def test_user_agent_vide_ne_retient_pas_le_groupe_a_agent_vide_resultat_inchange(agent):
    """Un User-Agent vide a un jeton vide : il ne doit pas « tomber » sur un groupe dont la
    ligne « User-agent: » est vide. Le résultat est le même qu'en l'absence de ce groupe."""
    avec_groupe_vide = "User-agent:\nDisallow: /vide/\n\nUser-agent: *\nDisallow: /b/\n"
    sans_groupe_vide = "User-agent: *\nDisallow: /b/\n"
    assert regles_applicables(avec_groupe_vide, agent) == ("*", [("Disallow", "/b/")])
    assert regles_applicables(avec_groupe_vide, agent) == regles_applicables(
        sans_groupe_vide, agent
    )


def test_nom_d_agent_partiel_est_sans_effet_sur_le_choix_du_groupe():
    """Le nom doit être EXACTEMENT le jeton produit : ni préfixe (« kayak »), ni suffixe."""
    texte = (
        "User-agent: Kayak\nDisallow: /a/\n\n"
        "User-agent: kayaketudecertif2\nDisallow: /b/\n\n"
        "User-agent: *\nDisallow: /c/\n"
    )
    assert regles_applicables(texte, UA) == ("*", [("Disallow", "/c/")])


def test_groupes_d_autres_robots_laissent_le_groupe_generique_inchange():
    """Les groupes nommés pour d'autres robots n'altèrent pas ce que reçoit un robot inconnu."""
    generique = "User-agent: *\nDisallow: /c/\n"
    avec_autres = (
        "User-agent: MauvaisRobot\nDisallow: /\n\n"
        + generique
        + "\nUser-agent: AutreBot\nDisallow: /d/\n"
    )
    assert regles_applicables(avec_autres, UA) == regles_applicables(generique, UA)
    assert regles_applicables(avec_autres, UA) == ("*", [("Disallow", "/c/")])


def test_groupe_nomme_sans_regle_rend_les_regles_generiques_sans_effet_pour_ce_robot():
    """Un groupe nommé existe même vide : il neutralise « * » pour ce robot (RFC 9309, § 2.2.1).

    Les règles génériques (« Disallow: / ») restent sans effet sur lui, mais valent toujours
    pour un autre robot : on ne fusionne pas le groupe nommé avec le groupe générique.
    """
    texte = "User-agent: KayakEtudeCertif\nDisallow:\n\nUser-agent: *\nDisallow: /\n"
    assert regles_applicables(texte, UA) == ("kayaketudecertif", [])
    assert verdict(texte, UA, "/page") == _libre("kayaketudecertif")
    assert not verdict(texte, "AutreRobot/1.0", "/page")["autorise"]


@pytest.mark.parametrize(
    "texte",
    ["", "Disallow: /x\n", "User-agent: Autre\nDisallow: /\n", "# User-agent: *\nDisallow: /\n"],
)
def test_fichier_sans_groupe_applicable_n_impose_aucune_regle_sans_effet(texte):
    """Sans groupe nommé ni « * » (fichier vide, règle orpheline, groupe d'un autre robot,
    ligne commentée), aucune règle ne s'applique : l'agent retenu est « * » et rien n'est interdit.
    """
    assert regles_applicables(texte, UA) == ("*", [])
    assert verdict(texte, UA, "/n-importe-quoi") == _libre()


# --------------------------------------------------------------------------------------------
# verdict : correspondance des motifs
# --------------------------------------------------------------------------------------------


def test_dollar_seul_est_sans_effet_car_aucun_chemin_n_est_vide():
    """Un motif réduit à « $ » exige un chemin vide ; or le chemin vaut au moins « / ».

    Il ne peut donc rien interdire, alors que « /$ » interdit la racine et elle seule.
    """
    texte = "User-agent: *\nDisallow: $\n"
    for chemin in ("/", "/page", "https://exemple.test", ""):
        assert verdict(texte, UA, chemin) == _libre(), chemin
    racine = "User-agent: *\nDisallow: /$\n"
    assert not verdict(racine, UA, "/")["autorise"]
    assert verdict(racine, UA, "/page") == _libre()


def test_ancre_de_fin_est_sans_effet_sur_les_chemins_plus_longs():
    """« /fin$ » n'interdit que « /fin » : la requête, une suite ou un suffixe y échappent."""
    texte = "User-agent: *\nDisallow: /fin$\n"
    refuse = verdict(texte, UA, "/fin")
    assert refuse["autorise"] is False and refuse["regle"] == ("Disallow", "/fin$")
    for chemin in ("/fin?x=1", "/fin/", "/fin2", "/finale", "/fin/suite"):
        assert verdict(texte, UA, chemin) == _libre(), chemin


def test_motif_est_un_prefixe_pas_une_sous_chaine_sans_effet_ailleurs():
    """« /b » interdit « /b », « /bb » et « /b/c », mais n'a aucun effet sur « /a/b » ni « /ab »."""
    texte = "User-agent: *\nDisallow: /b\n"
    for chemin in ("/b", "/bb", "/b/c"):
        assert not verdict(texte, UA, chemin)["autorise"], chemin
    for chemin in ("/a/b", "/ab", "/", "/a"):
        assert verdict(texte, UA, chemin) == _libre(), chemin


def test_chemin_sensible_a_la_casse_la_regle_est_sans_effet_sur_une_autre_casse():
    """Les chemins sont sensibles à la casse : « /Prive/ » n'interdit pas « /prive/ »."""
    texte = "User-agent: *\nDisallow: /Prive/\n"
    assert not verdict(texte, UA, "/Prive/x")["autorise"]
    assert verdict(texte, UA, "/prive/x") == _libre()
    assert verdict(texte, UA, "/PRIVE/x") == _libre()


@pytest.mark.parametrize(
    ("motif", "libre", "bloque"),
    [
        ("/a.html", "/aXhtml", "/a.html"),
        ("/s?q=1", "/sq=1", "/s?q=1"),
        ("/a+b", "/aab", "/a+b"),
        ("/a(b)", "/ab", "/a(b)"),
        ("/a[1]", "/a1", "/a[1]"),
        ("/a|b", "/a", "/a|b"),
        ("/a\\b", "/a", "/a\\b"),
    ],
)
def test_caracteres_speciaux_d_expression_reguliere_sont_litteraux_sans_effet_ailleurs(
    motif, libre, bloque
):
    """Seuls « * » et « $ » final sont spéciaux : « . ? + ( [ | \\ » se comparent littéralement.

    Interprétés comme une expression régulière, ils interdiraient d'autres pages (ou feraient
    échouer la compilation) ; ici la page voisine reste libre et la page exacte interdite.
    """
    texte = f"User-agent: *\nDisallow: {motif}\n"
    assert verdict(texte, UA, libre) == _libre(), libre
    assert verdict(texte, UA, bloque)["regle"] == ("Disallow", motif)


def test_dollar_non_final_est_litteral_et_sans_effet_sur_le_prefixe():
    """Un « $ » qui n'est pas le dernier caractère est un caractère ordinaire, pas une ancre."""
    texte = "User-agent: *\nDisallow: /a$b\n"
    assert verdict(texte, UA, "/a$b")["regle"] == ("Disallow", "/a$b")
    for chemin in ("/a", "/ab", "/a$"):
        assert verdict(texte, UA, chemin) == _libre(), chemin
    double = "User-agent: *\nDisallow: /a$$\n"
    assert not verdict(double, UA, "/a$")["autorise"]  # « $ » littéral puis ancre de fin
    assert verdict(double, UA, "/a$x") == _libre()


def test_joker_du_milieu_prend_zero_ou_plusieurs_caracteres_sans_effet_ailleurs():
    """« * » remplace une suite quelconque (même vide) ; sans « $ » c'est un préfixe, avec « $ »
    la fin du chemin est ancrée. Les chemins qui ne suivent pas le motif ne sont pas touchés."""
    texte = "User-agent: *\nDisallow: /a*b\nDisallow: /*.pdf$\n"
    for chemin in ("/ab", "/aXb", "/a/x/y/b", "/abc", "/doc/x.pdf", "/.pdf"):
        assert not verdict(texte, UA, chemin)["autorise"], chemin
    for chemin in ("/aXc", "/a", "/doc/x.pdfs", "/doc/x.pdf?dl=1", "/b", "/doc/pdf"):
        assert verdict(texte, UA, chemin) == _libre(), chemin


def test_motif_vers_regex_dollar_final_ancre_et_etoile_seule_prend_tout():
    """Appel direct : « $ » final ancre la fin, « * » seul correspond à tout (même vide)."""
    ancre = _motif_vers_regex("/a$")
    assert isinstance(ancre, re.Pattern)
    assert ancre.match("/a") is not None
    assert ancre.match("/a/") is None
    assert ancre.match("x/a") is None  # ancré au DÉBUT : un motif n'est pas cherché plus loin
    for chemin in ("", "/", "/n/importe/quoi?x=1"):
        assert _motif_vers_regex("*").match(chemin) is not None, chemin


# --------------------------------------------------------------------------------------------
# verdict : choix de la règle décisive
# --------------------------------------------------------------------------------------------


def test_allow_plus_court_est_sans_effet_sur_une_disallow_plus_specifique():
    """La règle la plus longue décide : un « Allow » plus court ne ré-autorise rien.

    Il reste cité dans les règles correspondantes (preuve), quel que soit l'ordre du fichier.
    """
    for texte in (
        "User-agent: *\nDisallow: /docs/prive/\nAllow: /docs/\n",
        "User-agent: *\nAllow: /docs/\nDisallow: /docs/prive/\n",
    ):
        decision = verdict(texte, UA, "/docs/prive/x")
        assert decision["autorise"] is False
        assert decision["regle"] == ("Disallow", "/docs/prive/")
        assert ("Allow", "/docs/") in decision["correspondantes"]
        assert verdict(texte, UA, "/docs/public")["regle"] == ("Allow", "/docs/")
    joker = "User-agent: *\nAllow: *\nDisallow: /x\n"
    assert not verdict(joker, UA, "/x/y")["autorise"]  # « * » (1 caractère) < « /x » (2)
    assert verdict(joker, UA, "/y")["regle"] == ("Allow", "*")
    assert not verdict("User-agent: *\nDisallow: *\n", UA, "/n/importe/quoi?x=1")["autorise"]


def test_a_longueur_egale_disallow_est_sans_effet_allow_l_emporte():
    """Deux motifs de même longueur (« /ab » et « /a* ») : « Allow » décide, dans les deux ordres.

    La « Disallow » ex æquo ne pèse rien ; seule la longueur la plus grande la ferait gagner.
    """
    for texte in (
        "User-agent: *\nDisallow: /ab\nAllow: /a*\n",
        "User-agent: *\nAllow: /a*\nDisallow: /ab\n",
    ):
        decision = verdict(texte, UA, "/abc")
        assert decision["autorise"] is True
        assert decision["regle"] == ("Allow", "/a*")
        assert ("Disallow", "/ab") in decision["correspondantes"]


def test_hote_schema_et_fragment_sont_sans_effet_sur_le_verdict():
    """Seuls le chemin et la requête comptent : l'hôte, le schéma, le port et le « #fragment »
    ne changent rien (l'appelant fournit le robots.txt de la bonne origine)."""
    texte = "User-agent: *\nDisallow: /prive/\n"
    reference = verdict(texte, UA, "/prive/x?a=1")
    assert reference["autorise"] is False
    assert verdict(texte, UA, "https://autre.exemple:8443/prive/x?a=1#ancre") == reference
    assert verdict(texte, UA, "http://x.test/prive/x?a=1") == reference
    ancre = "User-agent: *\nDisallow: /prive/x$\n"
    assert not verdict(ancre, UA, "/prive/x#fragment")["autorise"]


def test_url_sans_chemin_est_la_racine_et_la_requete_fait_partie_du_chemin():
    """Sans chemin, l'URL vaut « / » ; la requête est comparée avec le chemin (« ?a=1 » compris).

    Une règle sur « /s?id=7 » n'atteint donc que cette requête-là, pas « /s » ni « /s?id=8 ».
    """
    texte = "User-agent: *\nDisallow: /$\nDisallow: /?secret\nDisallow: /s?id=7\n"
    for url in ("https://exemple.test", "https://exemple.test/", "/", ""):
        assert verdict(texte, UA, url)["regle"] == ("Disallow", "/$"), url
    for url in ("?secret", "https://exemple.test?secret=1"):
        assert verdict(texte, UA, url)["regle"] == ("Disallow", "/?secret"), url
    assert verdict(texte, UA, "/s?id=7&x=1")["regle"] == ("Disallow", "/s?id=7")
    for url in ("/s", "/s?id=8", "/s?"):
        assert verdict(texte, UA, url) == _libre(), url


# --------------------------------------------------------------------------------------------
# archiver_robots
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("statut", [403, 429, 500, 503])
def test_statut_illisible_leve_permission_error_avec_statut_et_url(tmp_path, statut):
    """Un 403 (contrôle anti-robot) ou un 5xx (fichier injoignable) arrête la collecte.

    Le message cite le statut reçu et l'URL du robots.txt, pour que l'erreur soit explicite.
    """
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session = _Session(_Reponse(statut, b"refus"))
    message = f"robots.txt illisible ({statut}) : {URL_ROBOTS}"
    with pytest.raises(PermissionError, match="^" + re.escape(message) + "$"):
        archiver_robots(lake, "https://www.exemple.test/page.html", "booking", session)


@pytest.mark.parametrize("statut", [201, 204, 206, 301, 302, 304, 308])
def test_seul_le_statut_200_est_une_lecture_valide(tmp_path, statut):
    """Un succès « voisin » (201, 204...) ou une redirection n'est pas un robots.txt lu.

    Accepter un 204 (corps vide) ferait croire à un fichier sans règle : tout serait permis.
    """
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session = _Session(_Reponse(statut, b"User-agent: *\nDisallow: /\n"))
    with pytest.raises(PermissionError, match=rf"robots\.txt illisible \({statut}\) : "):
        archiver_robots(lake, "https://www.exemple.test/", "booking", session)


@pytest.mark.parametrize("statut", [400, 401, 403, 405, 409, 411, 429, 451])
def test_seuls_404_et_410_sont_des_absences_les_autres_4xx_refusent(tmp_path, statut):
    """Les autres erreurs 4xx ne valent PAS « pas de robots.txt » : elles arrêtent la collecte.

    Un 403 ou un 451 peut cacher une interdiction ; seuls 404 et 410 disent « fichier absent ».
    """
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session = _Session(_Reponse(statut, b""))
    with pytest.raises(PermissionError, match=rf"illisible \({statut}\)"):
        archiver_robots(lake, "https://www.exemple.test/", "booking", session)


def test_refus_laisse_le_lake_intact_y_compris_l_archive_du_jour(tmp_path, monkeypatch):
    """Un statut illisible n'écrit rien et n'écrase pas le robots.txt déjà archivé ce jour-là."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    chemin = "raw/booking/date=2026-10-10/robots.txt"
    lake.ecrire(chemin, b"User-agent: *\nDisallow: /ancien/\n")
    avant = sorted(lake.lister(""))
    contenu_avant = lake.lire(chemin)
    session = _Session(_Reponse(503, b"indisponible"))
    with pytest.raises(PermissionError, match=r"\(503\)"):
        archiver_robots(lake, "https://www.exemple.test/", "booking", session)
    assert sorted(lake.lister("")) == avant
    assert lake.lire(chemin) == contenu_avant


def test_erreur_reseau_est_propagee_telle_quelle_et_rien_n_est_archive(tmp_path):
    """Une coupure pendant la lecture n'est pas avalée : la même exception remonte, lake vide."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    coupure = requests.ConnectionError("coupure réseau simulée")
    session = _Session(erreur=coupure)
    with pytest.raises(requests.ConnectionError, match="coupure réseau simulée") as info:
        archiver_robots(lake, "https://www.exemple.test/", "booking", session)
    assert info.value is coupure
    assert lake.lister("") == []


@pytest.mark.parametrize("statut", [404, 410])
def test_fichier_absent_n_archive_que_le_statut_et_laisse_les_autres_archives_intactes(
    tmp_path, monkeypatch, statut
):
    """404/410 : aucun robots.txt n'est inventé, seul robots_meta.json (preuve) est écrit.

    Le texte rendu est vide (donc tout est permis), la date du serveur absente vaut None, la
    taille est 0 et l'empreinte est celle du contenu vide ; une autre archive du jour reste
    intacte.
    """
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")
    monkeypatch.setattr(config, "USER_AGENT", "RobotTest/1.0")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    autre = "raw/destinations/date=2026-10-10/page.html"
    lake.ecrire(autre, "<h2>1. Paris</h2>")
    session = _Session(_Reponse(statut, b"introuvable", entetes={}))
    resultat = archiver_robots(
        lake, "https://www.exemple.test/a/b?c=1", "destinations", session
    )
    attendu = {
        "url": URL_ROBOTS,
        "statut_http": statut,
        "date_reponse_serveur": None,
        "date_archive": "2026-10-10",
        "taille_octets": 0,
        "sha256": SHA256_VIDE,
        "user_agent": "RobotTest/1.0",
        "restriction": "aucune (fichier absent)",
    }
    assert resultat["texte"] == ""
    assert resultat["meta"] == attendu
    dossier = "raw/destinations/date=2026-10-10"
    assert json.loads(lake.lire(f"{dossier}/robots_meta.json")) == attendu
    assert sorted(lake.lister("raw")) == [autre, f"{dossier}/robots_meta.json"]
    assert lake.lire(autre) == b"<h2>1. Paris</h2>"
    assert verdict(resultat["texte"], "RobotTest/1.0", "/n-importe-quoi")["autorise"]


def test_contenu_archive_non_modifie_octet_pour_octet(tmp_path, monkeypatch):
    """Le fichier reçu est archivé tel quel (CRLF, accent, pas de saut de ligne final).

    La preuve vaut ce que vaut sa fidélité : octets, taille en OCTETS (pas en caractères) et
    empreinte SHA-256 portent sur ``content`` ; le texte rendu vient de ``text``.
    """
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    contenu = "User-agent: *\r\nDisallow: /café/\r\n\r\n# fin sans saut".encode()
    session = _Session(_Reponse(200, contenu, texte="texte décodé par requests"))
    resultat = archiver_robots(lake, "https://www.exemple.test/", "booking", session)
    dossier = "raw/booking/date=2026-10-10"
    assert lake.lire(f"{dossier}/robots.txt") == contenu
    assert resultat["texte"] == "texte décodé par requests"
    meta = json.loads(lake.lire(f"{dossier}/robots_meta.json"))
    assert meta["taille_octets"] == len(contenu)
    assert len(contenu) != len(contenu.decode("utf-8"))  # l'accent pèse 2 octets
    assert meta["sha256"] == hashlib.sha256(contenu).hexdigest()


def test_session_fournie_laisse_le_constructeur_de_sessions_inchange(tmp_path, monkeypatch):
    """Avec une session fournie, aucune session n'est créée : le constructeur n'est pas appelé."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")
    creations = []

    def fabrique():
        creations.append("session créée")
        return _Session(_Reponse(500, b""))

    monkeypatch.setattr(requests, "Session", fabrique)
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    fournie = _Session(_Reponse(200, b"User-agent: *\nDisallow:\n"))
    archiver_robots(lake, "https://www.exemple.test/x", "booking", fournie)
    assert creations == []
    assert [url for url, _ in fournie.appels] == [URL_ROBOTS]


# --------------------------------------------------------------------------------------------
# archiver_robots : les cas nominaux qui complètent les refus
# --------------------------------------------------------------------------------------------


def test_robots_txt_demande_a_la_racine_du_site_avec_user_agent_et_delai(tmp_path, monkeypatch):
    """L'URL est celle de /robots.txt à la racine (port conservé), avec le User-Agent du projet
    et un délai de 30 s ; sans session fournie, une session par défaut est créée et utilisée."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")
    monkeypatch.setattr(config, "USER_AGENT", "RobotTest/1.0")
    par_defaut = _Session(_Reponse(200, b"User-agent: *\nDisallow:\n"))
    monkeypatch.setattr(requests, "Session", lambda: par_defaut)
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    archiver_robots(lake, "https://www.exemple.test:8443/a/b/page.html?x=1#f", "booking")
    assert par_defaut.appels == [
        (
            "https://www.exemple.test:8443/robots.txt",
            {"headers": {"User-Agent": "RobotTest/1.0"}, "timeout": 30},
        )
    ]


def test_archive_200_ecrit_le_fichier_et_des_metadonnees_exactes(tmp_path, monkeypatch):
    """Un 200 écrit robots.txt et robots_meta.json sous raw/<source>/date=<jour>/, rien d'autre.

    Les métadonnées ont exactement les huit clés attendues ; le JSON est lisible (indenté) et
    garde les caractères non ASCII tels quels.
    """
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")
    monkeypatch.setattr(config, "USER_AGENT", "Robot é/1.0")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    contenu = b"User-agent: *\nDisallow: /prive/\n"
    session = _Session(_Reponse(200, contenu))
    resultat = archiver_robots(lake, "https://www.exemple.test/", "mon_site", session)
    dossier = "raw/mon_site/date=2026-10-10"
    attendu = {
        "url": URL_ROBOTS,
        "statut_http": 200,
        "date_reponse_serveur": DATE_SERVEUR,
        "date_archive": "2026-10-10",
        "taille_octets": len(contenu),
        "sha256": hashlib.sha256(contenu).hexdigest(),
        "user_agent": "Robot é/1.0",
        "restriction": "voir robots.txt",
    }
    assert resultat["meta"] == attendu
    assert resultat["texte"] == contenu.decode("utf-8")
    assert sorted(lake.lister("raw")) == [f"{dossier}/robots.txt", f"{dossier}/robots_meta.json"]
    brut = lake.lire(f"{dossier}/robots_meta.json").decode("utf-8")
    assert json.loads(brut) == attendu
    assert brut.startswith('{\n  "url": "https://www.exemple.test/robots.txt",\n')
    assert "Robot é/1.0" in brut and "\\u00e9" not in brut


# --------------------------------------------------------------------------------------------
# verdict : le dictionnaire rendu, clé par clé
# --------------------------------------------------------------------------------------------


def test_verdict_decisif_rend_quatre_cles_et_l_allow_court_est_sans_effet():
    """Quand une règle décide, le verdict porte exactement quatre clés : le booléen, la règle
    décisive, TOUTES les règles correspondantes dans l'ordre du fichier, et le groupe retenu.

    Un « Allow » plus court reste cité en preuve mais n'emporte pas la décision (sans effet
    sur le résultat), et le groupe vaut « * » ou le jeton du robot selon le cas.
    """
    disallow, allow = ("Disallow", "/docs/prive/"), ("Allow", "/docs/")
    for texte, ordre in (
        ("User-agent: *\nDisallow: /docs/prive/\nAllow: /docs/\n", [disallow, allow]),
        ("User-agent: *\nAllow: /docs/\nDisallow: /docs/prive/\n", [allow, disallow]),
    ):
        refuse = {"autorise": False, "regle": disallow, "correspondantes": ordre, "groupe": "*"}
        assert verdict(texte, UA, "/docs/prive/x") == refuse
        permis = {"autorise": True, "regle": allow, "correspondantes": [allow], "groupe": "*"}
        assert verdict(texte, UA, "/docs/public") == permis
    nomme = "User-agent: KayakEtudeCertif\nAllow: /a*\nDisallow: /ab\n"
    assert verdict(nomme, UA, "/abc") == {
        "autorise": True,
        "regle": ("Allow", "/a*"),
        "correspondantes": [("Allow", "/a*"), ("Disallow", "/ab")],
        "groupe": "kayaketudecertif",
    }


def test_un_commentaire_qui_contient_lui_meme_un_diese_est_retire_en_entier():
    """Tout ce qui suit le PREMIER « # » d'une ligne est un commentaire, même s'il en contient
    d'autres : la règle garde son seul motif."""
    groupes = lire_groupes("User-agent: *\nDisallow: /prive/ # interne # ne pas indexer\n")
    assert groupes == [{"agents": ["*"], "regles": [("Disallow", "/prive/")]}]
