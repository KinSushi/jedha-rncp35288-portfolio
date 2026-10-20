# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests de la configuration centralisée (``kayak.config``), surtout par leurs refus et leurs
invariants.

La configuration est lue dans l'environnement ; ces tests prouvent qu'elle reste prévisible :
valeurs par défaut exactes, constante figée qui refuse toute modification, variable inconnue
sans effet, surcharge d'une constante qui ne touche à aucune autre, chemins qui ne dépendent pas
du répertoire courant, date de collecte qui ne modifie jamais l'environnement.

Les tests qui rechargent le module (``importlib.reload``) passent par la fixture ``configurer`` :
elle remet l'environnement et le module dans leur état d'origine, même si le test échoue.
"""

import datetime
import importlib
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kayak import config

# Toutes les variables d'environnement que la configuration lit (ou pourrait être soupçonnée de
# lire) : la fixture les vide avant chaque rechargement et les restaure ensuite.
VARIABLES = (
    "KAYAK_USER_AGENT",
    "KAYAK_S3_BUCKET",
    "KAYAK_DB_URL",
    "OPENWEATHER_API_KEY",
    "KAYAK_DATE_COLLECTE",
    "KAYAK_S3_PREFIXE",
    "KAYAK_RAYON_AIRE_M",
    "KAYAK_RACINE",
)

USER_AGENT_PAR_DEFAUT = "KayakEtudeCertif/1.0 (projet pedagogique RNCP35288; robot identifie)"


@pytest.fixture
def configurer():
    """Recharge ``kayak.config`` dans un environnement donné, puis remet tout comme avant."""
    sauvegarde = {nom: os.environ.get(nom) for nom in VARIABLES}

    def appliquer(**variables: str) -> ModuleType:
        for nom in VARIABLES:
            os.environ.pop(nom, None)
        os.environ.update(variables)
        return importlib.reload(config)

    yield appliquer
    for nom, valeur in sauvegarde.items():
        if valeur is None:
            os.environ.pop(nom, None)
        else:
            os.environ[nom] = valeur
    importlib.reload(config)


def _constantes(module: ModuleType) -> dict[str, object]:
    """Photographie des constantes publiques (noms en majuscules) d'un module de configuration."""
    return {nom: getattr(module, nom) for nom in dir(module) if nom.isupper()}


def _fausse_date(jour: datetime.date) -> type:
    """Classe qui remplace ``config.date`` et dont ``today()`` rend toujours le jour donné."""

    class FausseDate:
        @staticmethod
        def today() -> datetime.date:
            return jour

    return FausseDate


# --------------------------------------------------------------------------------------------
# Tests directs : valeurs de référence
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nom, attendu",
    [
        ("USER_AGENT", USER_AGENT_PAR_DEFAUT),
        ("S3_BUCKET", None),
        ("S3_PREFIXE", "kayak"),
        ("RAYON_AIRE_M", 8000),
        ("OPENWEATHER_API_KEY", None),
    ],
)
def test_valeurs_par_defaut_sans_variable_d_environnement(configurer, nom, attendu):
    """Sans variable, le projet tourne en local : pas de bucket, pas de clé d'API, robot identifié.

    Le rayon de l'aire de destination est de 8 km (8000 m) et le préfixe S3 est « kayak ».
    """
    assert getattr(configurer(), nom) == attendu


@pytest.mark.parametrize(
    "variable, jour, attendu",
    [
        ("2026-10-10", datetime.date(2030, 1, 1), "2026-10-10"),
        (None, datetime.date(2026, 1, 5), "2026-01-05"),
    ],
    ids=["variable_prioritaire", "date_du_jour_au_format_iso"],
)
def test_date_collecte_variable_prioritaire_sinon_date_du_jour(
    monkeypatch, variable, jour, attendu
):
    """KAYAK_DATE_COLLECTE fixe la date ; sinon c'est le jour courant, au format ISO AAAA-MM-JJ."""
    if variable is None:
        monkeypatch.delenv("KAYAK_DATE_COLLECTE", raising=False)
    else:
        monkeypatch.setenv("KAYAK_DATE_COLLECTE", variable)
    monkeypatch.setattr(config, "date", _fausse_date(jour))
    assert config.date_collecte() == attendu


VILLES_DE_L_ENONCE = [
    "Mont Saint Michel",
    "St Malo",
    "Bayeux",
    "Le Havre",
    "Rouen",
    "Paris",
    "Amiens",
    "Lille",
    "Strasbourg",
    "Chateau du Haut Koenigsbourg",
    "Colmar",
    "Eguisheim",
    "Besancon",
    "Dijon",
    "Annecy",
    "Grenoble",
    "Lyon",
    "Gorges du Verdon",
    "Bormes les Mimosas",
    "Cassis",
    "Marseille",
    "Aix en Provence",
    "Avignon",
    "Uzes",
    "Nimes",
    "Aigues Mortes",
    "Saintes Maries de la mer",
    "Collioure",
    "Carcassonne",
    "Ariege",
    "Toulouse",
    "Montauban",
    "Biarritz",
    "Bayonne",
    "La Rochelle",
]


def test_villes_enonce_est_la_liste_exacte_sans_doublon_ni_blanc_parasite():
    """Les destinations de l'énoncé, dans l'ordre du sujet, chacune une seule fois, sans blanc."""
    villes = config.VILLES_ENONCE
    assert villes == VILLES_DE_L_ENONCE
    assert len(set(villes)) == len(villes)
    assert len({v.casefold() for v in villes}) == len(villes)
    assert all(v and v == v.strip() for v in villes)


# --------------------------------------------------------------------------------------------
# Tests inverses : refus, invariants, absence d'effet
# --------------------------------------------------------------------------------------------


def test_destinations_non_communales_refuse_toute_modification_et_reste_intacte():
    """Constante figée : un tuple. Toute modification est refusée et la valeur reste intacte.

    Elle est de plus incluse dans la liste de l'énoncé : on ne peut pas exclure du marquage
    « hors commune » une destination que le projet ne collecte pas.
    """
    attendu = ("Gorges du Verdon", "Ariege", "Chateau du Haut Koenigsbourg")
    destinations: Any = config.DESTINATIONS_NON_COMMUNALES
    with pytest.raises(TypeError, match="tuple"):
        destinations[0] = "Paris"
    with pytest.raises(AttributeError, match="append"):
        destinations.append("Paris")
    assert config.DESTINATIONS_NON_COMMUNALES == attendu
    assert set(attendu) <= set(config.VILLES_ENONCE)


def test_date_collecte_n_accepte_aucun_argument():
    """La date ne se passe pas en argument : seule la variable d'environnement la fixe."""
    date_collecte: Any = config.date_collecte
    with pytest.raises(TypeError, match="takes 0 positional arguments"):
        date_collecte("2026-10-10")


@pytest.mark.parametrize("variable", ["2026-10-10", None], ids=["definie", "absente"])
def test_date_collecte_laisse_l_environnement_inchange(monkeypatch, variable):
    """Lire la date ne crée, ne change et ne supprime aucune variable d'environnement."""
    if variable is None:
        monkeypatch.delenv("KAYAK_DATE_COLLECTE", raising=False)
    else:
        monkeypatch.setenv("KAYAK_DATE_COLLECTE", variable)
    environnement_avant = dict(os.environ)
    config.date_collecte()
    config.date_collecte()
    assert dict(os.environ) == environnement_avant


@pytest.mark.parametrize(
    "variable, constante, valeur",
    [
        ("KAYAK_USER_AGENT", "USER_AGENT", "Autre/2.0 (test)"),
        ("KAYAK_S3_BUCKET", "S3_BUCKET", "bucket-de-test"),
        ("KAYAK_DB_URL", "DB_URL", "sqlite:///autre.db"),
        ("OPENWEATHER_API_KEY", "OPENWEATHER_API_KEY", "cle-factice-de-test"),
    ],
)
def test_surcharge_d_une_variable_laisse_les_autres_constantes_inchangees(
    configurer, variable, constante, valeur
):
    """Chaque variable ne règle que sa constante : toutes les autres gardent leur valeur par défaut.

    Garde en particulier la clé d'API hors de l'agent utilisateur, qui part vers des tiers.
    """
    par_defaut = _constantes(configurer())
    surchargees = _constantes(configurer(**{variable: valeur}))
    assert surchargees[constante] == valeur
    assert par_defaut[constante] != valeur
    autres = [nom for nom in par_defaut if nom != constante]
    assert {nom: surchargees[nom] for nom in autres} == {nom: par_defaut[nom] for nom in autres}


@pytest.mark.parametrize(
    "variable",
    ["KAYAK_S3_PREFIXE", "KAYAK_RAYON_AIRE_M", "KAYAK_RACINE", "KAYAK_DATE_COLLECTE"],
)
def test_variable_non_prevue_est_sans_effet_sur_les_constantes(configurer, variable):
    """Préfixe S3, rayon et racine sont fixes ; la date est lue à l'appel, jamais à l'import."""
    par_defaut = _constantes(configurer())
    avec_variable = _constantes(configurer(**{variable: "valeur-inattendue"}))
    assert avec_variable == par_defaut


def test_recharger_la_configuration_laisse_l_environnement_inchange(configurer):
    """Recharger ``kayak.config`` ne crée ni ne modifie aucune variable d'environnement."""
    configurer(KAYAK_USER_AGENT="Autre/2.0", OPENWEATHER_API_KEY="cle-factice-de-test")
    environnement_avant = dict(os.environ)
    importlib.reload(config)
    assert dict(os.environ) == environnement_avant


def test_modifier_l_environnement_apres_l_import_laisse_les_constantes_inchangees(
    configurer, monkeypatch
):
    """Les constantes sont lues UNE fois, à l'import : changer l'environnement ensuite ne les
    change pas (seul un nouveau démarrage, ou un rechargement, les relit)."""
    constantes_avant = _constantes(configurer())
    monkeypatch.setenv("KAYAK_USER_AGENT", "Autre/2.0")
    monkeypatch.setenv("KAYAK_S3_BUCKET", "bucket-de-test")
    monkeypatch.setenv("KAYAK_DB_URL", "sqlite:///autre.db")
    monkeypatch.setenv("OPENWEATHER_API_KEY", "cle-factice-de-test")
    assert _constantes(config) == constantes_avant


def test_recharger_sans_variable_apres_une_surcharge_ramene_les_valeurs_initiales(configurer):
    """Aucune trace d'une surcharge précédente : le rechargement repart de l'environnement."""
    initiales = _constantes(configurer())
    surchargees = _constantes(
        configurer(
            KAYAK_USER_AGENT="Autre/2.0",
            KAYAK_S3_BUCKET="bucket-de-test",
            KAYAK_DB_URL="sqlite:///autre.db",
            OPENWEATHER_API_KEY="cle-factice-de-test",
        )
    )
    assert surchargees != initiales
    assert _constantes(configurer()) == initiales


def test_chemins_absolus_et_inchanges_quand_le_repertoire_courant_change(
    configurer, monkeypatch, tmp_path
):
    """La racine, le data lake local et l'entrepôt SQLite se calent sur le projet, pas sur le cwd.

    Lancer le pipeline depuis un autre dossier ne doit jamais écrire ailleurs que dans le projet.
    """
    noms = ("RACINE", "DATALAKE_LOCAL", "DB_URL")
    avant = _constantes(configurer())
    monkeypatch.chdir(tmp_path)
    apres = _constantes(configurer())
    assert {nom: apres[nom] for nom in noms} == {nom: avant[nom] for nom in noms}
    racine = apres["RACINE"]
    assert isinstance(racine, Path)
    assert racine.is_absolute()
    assert (racine / "kayak" / "config.py").is_file()
    assert apres["DATALAKE_LOCAL"] == racine / "datalake"
    url = str(apres["DB_URL"])
    assert url.startswith("sqlite:///")
    assert Path(url.removeprefix("sqlite:///")) == racine / "entrepot" / "kayak.db"
