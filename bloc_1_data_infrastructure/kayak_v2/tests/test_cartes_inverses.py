# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests inverses des cartes (``kayak.cartes``) : refus, entrées intactes, valeurs calculées.

Ce fichier complète ``test_cartes.py`` en prouvant ce que le module REFUSE ou NE FAIT PAS :
- une entrée interdite (colonne absente, latitude hors des pôles, surface de carte nulle, tableau
  absent, argument d'un mauvais type) est rejetée par une exception précise ;
- une entrée valide n'est jamais modifiée (tableaux, cadre, figure) ;
- dans les cas où il n'y a rien à faire (pastilles déjà éloignées, diamètre nul, aucune itération,
  ordre des lignes, destination hors du top 5), le résultat est strictement le même ;
- les valeurs exactes (zoom, centre, déplacements en pixels, ordre de tri, textes, couleurs) sont
  vérifiées sur des cas résolus à la main, avec une formule indépendante de celle du module.

Aucun réseau, aucun navigateur, aucun Kaleido : l'export d'image ``Figure.write_image`` est
remplacé par une fonction factice, et le dossier ``sorties`` est créé sous ``tmp_path``.
"""

import copy
import logging
import math
import os
from types import SimpleNamespace
from typing import Any, Literal

import numpy as np
import pandas as pd
import pytest
from plotly import graph_objects as go

from kayak import cartes as module_cartes
from kayak import config
from kayak.cartes import (
    _court,
    _mercator_y,
    _priorite_commune,
    cadrage,
    carte_destinations,
    carte_hotels,
    ecarter_marqueurs,
    enregistrer,
    selectionner_top_hotels,
)

MENTION = "Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC"
COULEURS_ATTENDUES = ["#1b6ca8", "#b03a2e", "#1e8449", "#7d3c98", "#9c640c"]
ROUGE, NEUTRE = "#b03a2e", "#333333"

# Les destinations du palmarès (vraies coordonnées arrondies), comme dans ``test_cartes.py``.
VILLES = pd.DataFrame(
    {
        "id_ville": [26, 19, 31, 25, 20, 28],
        "ville": [
            "Aigues Mortes",
            "Bormes les Mimosas",
            "Toulouse",
            "Nimes",
            "Cassis",
            "Collioure",
        ],
        "latitude": [43.566, 43.151, 43.604, 43.837, 43.214, 42.525],
        "longitude": [4.192, 6.342, 1.444, 4.360, 5.540, 3.083],
        "score_meteo": [0.92, 0.915, 0.91, 0.895, 0.865, 0.816],
        "rang_meteo": [1, 2, 3, 4, 5, 6],
        "temp_max_moy_c": [23.1, 23.1, 23.2, 22.8, 22.6, 22.7],
        "pluie_totale_mm": [0.0, 1.2, 0.0, 0.0, 0.3, 1.2],
        "ensoleillement_moy_h": [10.1, 10.3, 9.9, 10.2, 10.2, 10.1],
    }
)

# Position d'une vue de référence pour les tests d'écartement : tout se mesure en pixels autour
# de son centre (43,5° N ; 4° E), au niveau de zoom 10.
CADRE = {"center": {"lat": 43.5, "lon": 4.0}, "zoom": 10.0}
ECHELLE = 512 * 2**10 / (2 * math.pi)  # pixels par radian de Mercator au zoom 10


@pytest.fixture(autouse=True)
def _date(monkeypatch):
    """Date de collecte fixée : aucun test ne dépend de l'horloge réelle."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")


def _hotels() -> pd.DataFrame:
    """25 hôtels, 5 par destination : étoiles 5, 4, 4, 3 et une valeur manquante."""
    lignes = []
    ident = 1000
    for _, ville in VILLES.head(5).iterrows():
        for k in range(5):
            ident += 1
            lignes.append(
                {
                    "hotel_id": ident,
                    "id_ville": ville["id_ville"],
                    "ville": ville["ville"],
                    "nom": f"Hôtel {ident}",
                    "latitude": ville["latitude"] + 0.01 * k,
                    "longitude": ville["longitude"] + 0.01 * k,
                    "note": np.nan,
                    "nb_avis": np.nan,
                    "etoiles": float([5, 4, 4, 3, np.nan][k]),
                    "distance_km": 1.0 + k,
                    "commune": np.nan if k % 2 else ville["ville"],
                    "hors_commune": None if k % 2 else False,
                }
            )
    return pd.DataFrame(lignes)


def _villes_avec_destination_sans_rang() -> pd.DataFrame:
    """Le palmarès, plus une destination « Fantôme » dont le rang météo est manquant."""
    fantome = {
        "id_ville": 99,
        "ville": "Fantôme",
        "latitude": 44.0,
        "longitude": 3.0,
        "score_meteo": np.nan,
        "rang_meteo": np.nan,
        "temp_max_moy_c": np.nan,
        "pluie_totale_mm": np.nan,
        "ensoleillement_moy_h": np.nan,
    }
    return pd.concat([VILLES, pd.DataFrame([fantome])], ignore_index=True)


def _hotel(ident: int, id_ville: int = 26, **champs: Any) -> dict[str, Any]:
    """Un hôtel de la destination n° 1 (Aigues Mortes), les champs fournis écrasant les défauts."""
    base = {
        "hotel_id": ident,
        "id_ville": id_ville,
        "ville": "Aigues Mortes",
        "nom": f"Hôtel {ident}",
        "latitude": 43.566,
        "longitude": 4.192,
    }
    return {**base, **champs}


def _y(latitude: float) -> float:
    """Ordonnée de Mercator par l'identité y = argsinh(tan φ), indépendante du module."""
    return math.asinh(math.tan(math.radians(latitude)))


def _points(*couples: tuple[float, float]) -> pd.DataFrame:
    """Tableau de points (latitude, longitude)."""
    return pd.DataFrame(
        {"latitude": [c[0] for c in couples], "longitude": [c[1] for c in couples]}
    )


def _zoom_largeur(utile_px: float, etendue_deg: float) -> float:
    """Zoom qui fait tenir ``etendue_deg`` degrés dans ``utile_px`` pixels (monde = 512 × 2^z)."""
    return math.log2(utile_px * 360 / (512 * etendue_deg))


def _zoom_hauteur(utile_px: float, etendue_y: float) -> float:
    """Même calcul pour une étendue exprimée en ordonnée de Mercator (radians)."""
    return math.log2(utile_px * 2 * math.pi / (512 * etendue_y))


def _pixels(
    points: pd.DataFrame, cadre: dict, lat: str = "latitude", lon: str = "longitude"
) -> list[tuple[float, float]]:
    """Position en pixels de chaque point par rapport au centre de la vue (Mercator)."""
    echelle = 512 * 2 ** cadre["zoom"] / (2 * math.pi)
    lon0 = math.radians(cadre["center"]["lon"])
    y0 = _y(cadre["center"]["lat"])
    return [
        ((math.radians(b) - lon0) * echelle, (_y(a) - y0) * echelle)
        for a, b in zip(points[lat], points[lon], strict=True)
    ]


def _a_plat(positions: list[tuple[float, float]]) -> list[float]:
    """Aplatit [(x, y), ...] en [x, y, ...] pour comparer avec ``pytest.approx``."""
    return [coordonnee for xy in positions for coordonnee in xy]


def _points_a_pixels(*positions: tuple[float, float]) -> pd.DataFrame:
    """Points placés à des écarts donnés (x, y), en pixels, du centre de ``CADRE``."""
    lon0, y0 = math.radians(4.0), _y(43.5)
    return pd.DataFrame(
        {
            "latitude": [
                math.degrees(math.atan(math.sinh(y0 + y / ECHELLE))) for _, y in positions
            ],
            "longitude": [math.degrees(lon0 + x / ECHELLE) for x, _ in positions],
        }
    )


def _affichees(resultat: pd.DataFrame) -> list[tuple[float, float]]:
    """Positions affichées (en pixels autour du centre de ``CADRE``) d'un résultat d'écartement."""
    return _pixels(resultat, CADRE, "lat_affichee", "lon_affichee")


def _tableaux(fig: go.Figure) -> list[Any]:
    """Les deux tableaux (dans l'ordre) d'une carte des hôtels."""
    return [t for t in fig.data if t.type == "table"]


def _valeurs(tableau: Any) -> list[list[str]]:
    """Contenu des cellules d'un tableau, colonne par colonne."""
    return [list(colonne) for colonne in tableau.cells.values]


def _couleurs_commune(fig: go.Figure) -> list[str]:
    """Couleurs de police de la colonne « Commune OSM », tous tableaux confondus."""
    return [c for t in _tableaux(fig) for c in t.cells.font.color[5]]


def _portee(fixture_name: str, config: pytest.Config) -> Literal["module", "function"]:
    """Portée « module » d'ordinaire ; « function » pendant le relevé de l'outil de mutation.

    mutmut commence par un relevé (``MUTANT_UNDER_TEST`` vaut « stats ») : il relie chaque test
    aux fonctions du projet qu'il appelle, en les observant pendant le test. Une fixture de module
    ne s'exécute que pendant le PREMIER test du module : seul ce test serait relié au code qu'elle
    appelle, et les autres ne seraient jamais lancés contre ses mutants. Pendant ce relevé, la
    fixture est donc refaite pour chaque test. Ensuite, pour chaque mutant, elle redevient de
    module : faite une fois, avec le mutant actif, pour tous les tests choisis. Le contenu des
    tests ne change pas.
    """
    return "function" if os.environ.get("MUTANT_UNDER_TEST") == "stats" else "module"


@pytest.fixture(scope=_portee)
def figure_hotels() -> go.Figure:
    """La carte des hôtels construite sur les 25 hôtels du jeu de données (lecture seule)."""
    return carte_hotels(_hotels(), VILLES)


@pytest.fixture
def banc(tmp_path, monkeypatch):
    """Racine du projet détournée vers ``tmp_path`` et export d'image remplacé par un espion."""
    appels: list[tuple[Any, dict]] = []

    def faux_export(self, chemin, **options):
        appels.append((chemin, options))

    monkeypatch.setattr(config, "RACINE", tmp_path)
    monkeypatch.setattr(go.Figure, "write_image", faux_export)
    return SimpleNamespace(racine=tmp_path, dossier=tmp_path / "sorties", appels=appels)


# ------------------------------------------------------------------------------------------
# _mercator_y
# ------------------------------------------------------------------------------------------


def test_mercator_valeurs_connues_et_symetrie():
    """Mercator vaut 0 à l'équateur, argsinh(tan φ) ailleurs, et change de signe avec φ."""
    assert _mercator_y(0.0) == pytest.approx(0.0, abs=1e-12)
    assert _mercator_y(45.0) == pytest.approx(0.881373587019543)  # argsinh(1)
    assert _mercator_y(60.0) == pytest.approx(1.3169578969248166)  # argsinh(√3)
    assert _mercator_y(-60.0) == pytest.approx(-1.3169578969248166)
    assert _mercator_y(43.5) == pytest.approx(_y(43.5))


@pytest.mark.parametrize("latitude", [-90.0, -91.0, 91.0, 180.0])
def test_mercator_refuse_les_latitudes_hors_domaine(latitude):
    """Au pôle Sud et au-delà des pôles, le logarithme n'existe pas : on refuse, sans inventer."""
    with pytest.raises(ValueError, match="math domain error"):
        _mercator_y(latitude)


def test_mercator_refuse_une_latitude_textuelle():
    """Une latitude lue comme du texte (« 45 ») est refusée au lieu d'être convertie en silence."""
    with pytest.raises(TypeError, match="must be real number"):
        _mercator_y("45")


# ------------------------------------------------------------------------------------------
# cadrage
# ------------------------------------------------------------------------------------------


def test_cadrage_valeurs_calculees_a_la_main():
    """Centre et zoom exacts sur des cas résolus à la main (monde = 360° = 512 × 2^z pixels).

    Le zoom qui fait tenir E degrés dans U pixels vaut log2(U × 360 / (512 × E)) ; U est la taille
    de la carte moins 12 % de marge de chaque côté (1100 → 836 px, 750 → 570 px par défaut).
    Le centre en latitude est le milieu en Mercator, pas en degrés.
    """
    y10 = _y(10.0)
    lat10 = math.degrees(math.atan(math.sinh(y10 / 2)))  # ≈ 5,019° et non 5°
    cas = [
        ("largeur limitante", _points((0, 0), (0, 10)), {}, (0.0, 5.0), _zoom_largeur(836, 10)),
        (
            "hauteur limitante, centre de Mercator",
            _points((0, 0), (10, 0)),
            {},
            (lat10, 0.0),
            _zoom_hauteur(570, y10),
        ),
        (
            "marge de 30 %",
            _points((0, 0), (0, 10)),
            {"marge": 0.3},
            (0.0, 5.0),
            _zoom_largeur(440, 10),
        ),
        (
            "largeur de 600 px",
            _points((0, 0), (0, 10)),
            {"largeur_px": 600},
            (0.0, 5.0),
            _zoom_largeur(456, 10),
        ),
        (
            "hauteur de 400 px",
            _points((0, 0), (10, 0)),
            {"hauteur_px": 400},
            (lat10, 0.0),
            _zoom_hauteur(304, y10),
        ),
        (
            "hémisphère sud",
            _points((-10, 0), (0, 0)),
            {},
            (-lat10, 0.0),
            _zoom_hauteur(570, y10),
        ),
        (
            "zoom_max de 5 plafonne",
            _points((0, 0), (0, 10)),
            {"zoom_max": 5.0},
            (0.0, 5.0),
            5.0,
        ),
        ("point unique, plafond 15 par défaut", _points((43, 2)), {}, (43.0, 2.0), 15.0),
        (
            "point unique, plancher d'étendue de 0,004°",
            _points((43, 2)),
            {"zoom_max": 20.0},
            (43.0, 2.0),
            _zoom_largeur(570, 0.004),
        ),
        (
            "73° d'étendue : juste au-dessus du plancher de zoom",
            _points((0, -36.5), (0, 36.5)),
            {},
            (0.0, 0.0),
            _zoom_largeur(836, 73),
        ),
        (
            "74° d'étendue : plancher de zoom à 3",
            _points((0, -37), (0, 37)),
            {},
            (0.0, 0.0),
            3.0,
        ),
    ]
    for nom, points, options, (lat, lon), zoom in cas:
        cadre = cadrage(points, **options)
        assert set(cadre) == {"center", "zoom"}, nom
        assert set(cadre["center"]) == {"lat", "lon"}, nom
        assert cadre["center"]["lat"] == pytest.approx(lat, abs=1e-9), nom
        assert cadre["center"]["lon"] == pytest.approx(lon, abs=1e-9), nom
        assert cadre["zoom"] == pytest.approx(zoom, rel=1e-9), nom
        assert isinstance(cadre["zoom"], float), nom


@pytest.mark.parametrize("colonne", ["latitude", "longitude"])
def test_cadrage_refuse_une_colonne_absente(colonne):
    """Sans la colonne de coordonnées, le cadrage échoue au lieu de deviner une vue."""
    points = _points((0, 0), (1, 1)).drop(columns=colonne)
    with pytest.raises(KeyError, match=colonne):
        cadrage(points)


@pytest.mark.parametrize(
    ("largeur", "hauteur", "marge"),
    [
        (0, 750, 0.12),
        (-5, 750, 0.12),
        (1100, 0, 0.12),
        (1100, -5, 0.12),
        (1100, 750, 0.5),
        (1100, 750, 0.75),
    ],
)
def test_cadrage_refuse_une_surface_utile_nulle_ou_negative(largeur, hauteur, marge):
    """Taille nulle ou marges de 50 % et plus : aucune surface utile, donc aucun zoom calculable."""
    with pytest.raises(ValueError, match="math domain error"):
        cadrage(_points((0, 0), (0, 10)), largeur, hauteur, marge)


@pytest.mark.parametrize("latitude", [95.0, -90.0])
def test_cadrage_refuse_une_latitude_hors_domaine(latitude):
    """Un point au-delà des pôles fait échouer le cadrage au lieu de produire un zoom absurde."""
    with pytest.raises(ValueError, match="math domain error"):
        cadrage(_points((latitude, 0), (10, 0)))


def test_cadrage_refuse_un_tableau_absent():
    """Sans tableau de points (``None``), le cadrage est refusé."""
    with pytest.raises(TypeError, match="not subscriptable"):
        cadrage(None)


def test_cadrage_refuse_une_largeur_textuelle():
    """Une largeur donnée comme texte (« 1100 ») est refusée : le calcul est en pixels."""
    with pytest.raises(TypeError, match="can't multiply sequence"):
        cadrage(_points((0, 0), (0, 10)), largeur_px="1100")


def test_cadrage_ne_modifie_pas_le_tableau_de_points():
    """Le cadrage ne fait que lire les points : le tableau est identique avant et après."""
    points = VILLES.head(5).copy()
    avant = points.copy(deep=True)
    cadrage(points)
    pd.testing.assert_frame_equal(points, avant)


def test_cadrage_ordre_des_points_sans_effet():
    """Min et max ne dépendent pas de l'ordre : points inversés ou mélangés, même cadrage."""
    points = VILLES.head(5)
    reference = cadrage(points)
    assert cadrage(points.iloc[::-1]) == reference
    assert cadrage(points.iloc[[2, 0, 4, 1, 3]]) == reference


def test_cadrage_point_interieur_sans_effet():
    """Un point à l'intérieur de l'enveloppe des autres ne change ni le centre ni le zoom."""
    sans = _points((0, 0), (10, 20))
    avec = _points((0, 0), (10, 20), (5, 10), (1, 19))
    assert cadrage(avec) == cadrage(sans)


def test_cadrage_coordonnees_manquantes_sans_effet():
    """Un point sans coordonnées (NaN) est ignoré : le cadrage est celui des autres points."""
    sans = _points((0, 0), (10, 20))
    avec = _points((0, 0), (10, 20), (np.nan, np.nan))
    assert cadrage(avec) == cadrage(sans)


def test_cadrage_zoom_max_sans_effet_quand_l_etendue_est_large():
    """Un plafond de zoom au-dessus du zoom calculé ne change rien au résultat."""
    points = _points((0, 0), (0, 10))
    assert cadrage(points, zoom_max=100.0) == cadrage(points)


# ------------------------------------------------------------------------------------------
# carte_destinations
# ------------------------------------------------------------------------------------------


def test_carte_destinations_contenu_exact_par_defaut():
    """Titre, taille, marges, légende, fond et première pastille de la carte des destinations."""
    fig = carte_destinations(VILLES)
    disposition = fig.layout
    assert disposition.title.text == "Top 5 destinations selon le score météo"
    assert disposition.title.x == 0.5
    assert (disposition.width, disposition.height) == (1100, 750)
    marge = disposition.margin
    assert (marge.r, marge.t, marge.l, marge.b) == (0, 50, 0, 135)
    legende = disposition.legend
    assert (legende.x, legende.y, legende.yanchor) == (0.01, 0, "top")
    assert (legende.bgcolor, legende.font.size) == ("rgba(255,255,255,0)", 14)
    assert disposition.map.style == "open-street-map"
    attendu = cadrage(VILLES.head(5), 1100, 565)  # 750 - 50 (titre) - 135 (légende)
    assert disposition.map.center.lat == pytest.approx(attendu["center"]["lat"])
    assert disposition.map.center.lon == pytest.approx(attendu["center"]["lon"])
    assert disposition.map.zoom == pytest.approx(attendu["zoom"])

    premiere = fig.data[0]
    assert premiere.type == "scattermap"
    assert premiere.mode == "markers+text"
    assert (premiere.marker.size, premiere.marker.color) == (38, "#1b6ca8")
    assert list(premiere.text) == ["1"]
    assert (premiere.textfont.size, premiere.textfont.color) == (22, "white")
    assert premiere.textposition == "middle center"
    assert (list(premiere.lat), list(premiere.lon)) == ([43.566], [4.192])
    assert premiere.name == (
        "1. Aigues Mortes - score 0.92 (23.1 °C · 0.0 mm de pluie · 10.1 h de soleil/jour)"
    )
    assert premiere.hovertemplate == (
        "<b>1. Aigues Mortes</b><br>score 0.92<br>"
        "23.1 °C · 0.0 mm de pluie · 10.1 h de soleil/jour<extra></extra>"
    )


def test_carte_destinations_arrondis_des_textes_et_rang_decimal():
    """Score à 2 décimales, mesures à 1 décimale ; un rang décimal (2.0) s'écrit « 2 »."""
    villes = pd.DataFrame(
        {
            "ville": ["Alpha", "Beta"],
            "latitude": [43.0, 44.0],
            "longitude": [3.0, 4.0],
            "score_meteo": [0.9266, 0.8234],
            "rang_meteo": [1.0, 2.0],
            "temp_max_moy_c": [23.16, 20.04],
            "pluie_totale_mm": [1.26, 0.04],
            "ensoleillement_moy_h": [10.04, 9.96],
        }
    )
    fig = carte_destinations(villes)
    assert [list(t.text) for t in fig.data] == [["1"], ["2"]]
    assert fig.data[0].name == (
        "1. Alpha - score 0.93 (23.2 °C · 1.3 mm de pluie · 10.0 h de soleil/jour)"
    )
    assert fig.data[1].hovertemplate == (
        "<b>2. Beta</b><br>score 0.82<br>20.0 °C · 0.0 mm de pluie · 10.0 h de soleil/jour"
        "<extra></extra>"
    )


def test_carte_destinations_options_top_taille_et_couleurs_cycliques():
    """``top`` borne les pastilles, la taille cadre la vue, et les couleurs reviennent après 5."""
    cas = [
        ({}, 5, 1100, 750, 565),
        ({"top": 3}, 3, 1100, 750, 565),
        ({"top": 6}, 6, 1100, 750, 565),
        ({"top": 10}, 6, 1100, 750, 565),
        ({"largeur": 900, "hauteur": 600}, 5, 900, 600, 415),
    ]
    for options, nombre, largeur, hauteur, hauteur_carte in cas:
        fig = carte_destinations(VILLES, **options)
        top = options.get("top", 5)  # le titre annonce le ``top`` demandé, même s'il y a moins
        assert fig.layout.title.text == f"Top {top} destinations selon le score météo", options
        assert (fig.layout.width, fig.layout.height) == (largeur, hauteur), options
        assert [t.text[0] for t in fig.data] == [str(r) for r in range(1, nombre + 1)], options
        assert [t.marker.color for t in fig.data] == (COULEURS_ATTENDUES * 2)[:nombre], options
        attendu = cadrage(VILLES.head(nombre), largeur, hauteur_carte)
        assert fig.layout.map.zoom == pytest.approx(attendu["zoom"]), options
        assert fig.layout.map.center.lat == pytest.approx(attendu["center"]["lat"]), options
    # Villes alignées nord-sud : la hauteur de la carte (image - 50 de titre - 135 de légende)
    # limite alors le zoom.
    alignees = VILLES.head(3).assign(latitude=[42.0, 45.5, 49.0], longitude=2.0)
    for options, hauteur_carte in [({}, 565), ({"largeur": 900, "hauteur": 600}, 415)]:
        fig = carte_destinations(alignees, **options)
        attendu = cadrage(alignees, options.get("largeur", 1100), hauteur_carte)
        assert fig.layout.map.zoom == pytest.approx(attendu["zoom"], rel=1e-9), options
        assert fig.layout.map.center.lat == pytest.approx(attendu["center"]["lat"]), options


@pytest.mark.parametrize(
    "colonne",
    [
        "ville",
        "latitude",
        "longitude",
        "score_meteo",
        "rang_meteo",
        "temp_max_moy_c",
        "pluie_totale_mm",
        "ensoleillement_moy_h",
    ],
)
def test_carte_destinations_refuse_une_colonne_absente(colonne):
    """Chaque colonne documentée est indispensable : sans elle, la carte n'est pas dessinée."""
    with pytest.raises(KeyError, match=colonne):
        carte_destinations(VILLES.drop(columns=colonne))


def test_carte_destinations_refuse_un_tableau_absent():
    """Sans tableau de villes (``None``), la carte est refusée."""
    with pytest.raises(AttributeError, match="nsmallest"):
        carte_destinations(None)


@pytest.mark.parametrize(("largeur", "hauteur"), [(0, 750), (1100, 185), (1100, 100)])
def test_carte_destinations_refuse_une_image_sans_surface_pour_la_carte(largeur, hauteur):
    """Une image dont la carte n'a plus de pixels (hauteur ≤ 50 + 135 de marges) est refusée."""
    with pytest.raises(ValueError, match="math domain error"):
        carte_destinations(VILLES, largeur=largeur, hauteur=hauteur)


def test_carte_destinations_ne_modifie_pas_le_tableau_des_villes():
    """La carte lit le palmarès sans le trier ni y ajouter de colonne."""
    villes = VILLES.copy()
    avant = villes.copy(deep=True)
    carte_destinations(villes)
    pd.testing.assert_frame_equal(villes, avant)


def test_carte_destinations_ordre_des_lignes_sans_effet():
    """Le classement vient de ``rang_meteo`` : un tableau désordonné donne la même figure."""
    reference = carte_destinations(VILLES)
    melangee = carte_destinations(VILLES.iloc[[3, 5, 0, 4, 1, 2]].reset_index(drop=True))
    assert [t.text[0] for t in melangee.data] == ["1", "2", "3", "4", "5"]
    assert melangee.to_json() == reference.to_json()


def test_carte_destinations_sixieme_destination_sans_effet():
    """La 6e destination du palmarès n'apparaît pas : la figure est celle des cinq premières."""
    sans_sixieme = carte_destinations(VILLES.head(5))
    avec_sixieme = carte_destinations(VILLES)
    assert avec_sixieme.to_json() == sans_sixieme.to_json()
    assert "Collioure" not in avec_sixieme.to_json()


def test_carte_destinations_destination_sans_rang_sans_effet():
    """Une destination sans rang météo (NaN) n'entre dans aucun palmarès : même figure."""
    avec = carte_destinations(_villes_avec_destination_sans_rang())
    assert avec.to_json() == carte_destinations(VILLES).to_json()
    assert "Fantôme" not in avec.to_json()


def test_carte_destinations_taille_sans_effet_sur_les_pastilles():
    """Changer la taille de l'image recadre la vue mais ne touche pas aux pastilles."""
    petite = carte_destinations(VILLES, largeur=900, hauteur=600)
    grande = carte_destinations(VILLES)
    assert [t.to_plotly_json() for t in petite.data] == [t.to_plotly_json() for t in grande.data]


def test_carte_destinations_reduire_top_laisse_inchangees_les_premieres_pastilles():
    """Avec top=3, les 3 pastilles sont exactement les 3 premières du top 5."""
    trois = carte_destinations(VILLES, top=3)
    cinq = carte_destinations(VILLES)
    assert [t.to_plotly_json() for t in trois.data] == [
        t.to_plotly_json() for t in cinq.data[:3]
    ]


# ------------------------------------------------------------------------------------------
# ecarter_marqueurs
# ------------------------------------------------------------------------------------------


def test_ecarter_deplacements_calcules_a_la_main():
    """Déplacements en pixels résolus à la main pour deux pastilles (diamètre 28 par défaut).

    Deux pastilles confondues partent dans la direction 120° (2π × 1/3) : chaque itération les
    repousse de la moitié du recouvrement (27 px après la 1re, 28 px après la 2e). Au-dessus du
    seuil de 1e-6 px, la droite réelle qui les relie est conservée ; en dessous, elles comptent
    pour confondues. Trois pastilles confondues finissent à 28 px au moins les unes des autres.
    """
    s3 = math.sqrt(3)
    cas = [
        (
            "confondues, une itération",
            0.0,
            {"iterations": 1},
            [(6.75, -6.75 * s3), (-6.75, 6.75 * s3)],
        ),
        ("confondues, défaut", 0.0, {}, [(7.0, -7 * s3), (-7.0, 7 * s3)]),
        (
            "confondues, diamètre 40",
            0.0,
            {"diametre_px": 40.0},
            [(10.0, -10 * s3), (-10.0, 10 * s3)],
        ),
        ("écart 5e-7 px : confondues", 5e-7, {}, [(7.0, -7 * s3), (-7.0, 7 * s3)]),
        ("écart 2e-6 px : droite réelle", 2e-6, {}, [(-14.0, 0.0), (14.0, 0.0)]),
        ("écart 0,5 px", 0.5, {}, [(-13.75, 0.0), (14.25, 0.0)]),
        ("écart 20 px", 20.0, {}, [(-4.0, 0.0), (24.0, 0.0)]),
        ("écart 27,9 px", 27.9, {}, [(-0.05, 0.0), (27.95, 0.0)]),
    ]
    for nom, ecart, options, attendu in cas:
        points = _points_a_pixels((0.0, 0.0), (ecart, 0.0))
        resultat = ecarter_marqueurs(points, CADRE, **options)
        assert _a_plat(_affichees(resultat)) == pytest.approx(_a_plat(attendu), abs=1e-4), nom
    # Une pastille lointaine, puis un couple confondu en position 1 et 2 : l'angle vaut
    # 2π × 2/4 = 180°, donc le couple s'écarte à l'horizontale, de part et d'autre de son point.
    couple = _points_a_pixels((300.0, 300.0), (0.0, 0.0), (0.0, 0.0))
    positions = _affichees(ecarter_marqueurs(couple, CADRE))
    attendu = [(300.0, 300.0), (14.0, 0.0), (-14.0, 0.0)]
    assert _a_plat(positions) == pytest.approx(_a_plat(attendu), abs=1e-4)
    # Huit pastilles confondues ne se stabilisent pas : l'arrêt à 80 itérations (défaut) se voit.
    huit = _points_a_pixels(*[(0.0, 0.0)] * 8)
    par_defaut = ecarter_marqueurs(huit, CADRE)
    pd.testing.assert_frame_equal(par_defaut, ecarter_marqueurs(huit, CADRE, iterations=80))
    assert not par_defaut.equals(ecarter_marqueurs(huit, CADRE, iterations=79))
    assert not par_defaut.equals(ecarter_marqueurs(huit, CADRE, iterations=81))
    trois = _points_a_pixels((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))
    positions = _affichees(ecarter_marqueurs(trois, CADRE))
    for i in range(3):
        for j in range(i + 1, 3):
            ecart = math.hypot(positions[i][0] - positions[j][0], positions[i][1] - positions[j][1])
            assert ecart >= 28 - 1e-6, ("trois confondues", i, j, ecart)


def test_ecarter_pastilles_deja_eloignees_positions_inchangees():
    """Des pastilles à plus d'un diamètre les unes des autres restent à leur vraie position."""
    points = _points_a_pixels((0.0, 0.0), (28.001, 0.0), (0.0, 60.0), (-80.0, -80.0))
    resultat = ecarter_marqueurs(points, CADRE)
    assert list(resultat["lat_affichee"]) == pytest.approx(list(points["latitude"]), abs=1e-9)
    assert list(resultat["lon_affichee"]) == pytest.approx(list(points["longitude"]), abs=1e-9)


def test_ecarter_une_seule_pastille_position_inchangee():
    """Une pastille seule n'a personne à éviter : elle reste où elle est, même hors du centre."""
    points = _points_a_pixels((120.0, -75.0))
    resultat = ecarter_marqueurs(points, CADRE)
    assert list(resultat["lat_affichee"]) == pytest.approx(list(points["latitude"]), abs=1e-9)
    assert list(resultat["lon_affichee"]) == pytest.approx(list(points["longitude"]), abs=1e-9)


@pytest.mark.parametrize("diametre", [0.0, -5.0])
def test_ecarter_diametre_nul_ou_negatif_sans_effet_meme_confondues(diametre):
    """Sans diamètre, deux pastilles confondues ne se recouvrent pas : rien ne bouge."""
    points = _points_a_pixels((0.0, 0.0), (0.0, 0.0))
    resultat = ecarter_marqueurs(points, CADRE, diametre_px=diametre)
    assert list(resultat["lat_affichee"]) == pytest.approx(list(points["latitude"]), abs=1e-9)
    assert list(resultat["lon_affichee"]) == pytest.approx(list(points["longitude"]), abs=1e-9)


@pytest.mark.parametrize("iterations", [0, -3])
def test_ecarter_zero_iteration_sans_effet_meme_confondues(iterations):
    """Sans aucune itération, les pastilles confondues restent confondues."""
    points = _points_a_pixels((0.0, 0.0), (0.0, 0.0))
    resultat = ecarter_marqueurs(points, CADRE, iterations=iterations)
    assert list(resultat["lat_affichee"]) == pytest.approx(list(points["latitude"]), abs=1e-9)
    assert list(resultat["lon_affichee"]) == pytest.approx(list(points["longitude"]), abs=1e-9)


def test_ecarter_ne_modifie_pas_le_tableau_d_entree():
    """L'écartement travaille sur une copie : le tableau d'entrée garde ses colonnes et valeurs."""
    points = _points_a_pixels((0.0, 0.0), (0.0, 0.0), (3.0, 4.0))
    avant = points.copy(deep=True)
    ecarter_marqueurs(points, CADRE)
    pd.testing.assert_frame_equal(points, avant)
    assert list(points.columns) == ["latitude", "longitude"]


def test_ecarter_cadre_non_modifie():
    """Le cadre (centre et zoom) est seulement lu : le dictionnaire reste identique."""
    cadre = copy.deepcopy(CADRE)
    ecarter_marqueurs(_points_a_pixels((0.0, 0.0), (0.0, 0.0)), cadre)
    assert cadre == CADRE


@pytest.mark.parametrize("colonne", ["latitude", "longitude"])
def test_ecarter_refuse_une_colonne_absente(colonne):
    """Sans coordonnées, on ne peut pas placer les pastilles : la colonne manquante est signalée."""
    points = _points_a_pixels((0.0, 0.0), (5.0, 0.0)).drop(columns=colonne)
    with pytest.raises(KeyError, match=colonne):
        ecarter_marqueurs(points, CADRE)


@pytest.mark.parametrize(
    ("cadre", "manquant"),
    [
        ({"center": {"lat": 43.5, "lon": 4.0}}, "zoom"),
        ({"zoom": 10.0}, "center"),
        ({"center": {"lat": 43.5}, "zoom": 10.0}, "lon"),
        ({"center": {"lon": 4.0}, "zoom": 10.0}, "lat"),
    ],
)
def test_ecarter_refuse_un_cadre_incomplet(cadre, manquant):
    """Sans centre (lat, lon) ni zoom, la projection en pixels est impossible : clé réclamée."""
    with pytest.raises(KeyError, match=manquant):
        ecarter_marqueurs(_points_a_pixels((0.0, 0.0)), cadre)


@pytest.mark.parametrize("iterations", ["3", 2.0, None])
def test_ecarter_refuse_un_nombre_d_iterations_non_entier(iterations):
    """Le nombre d'itérations doit être un entier : texte, décimal ou ``None`` sont refusés."""
    with pytest.raises(TypeError, match="cannot be interpreted as an integer"):
        ecarter_marqueurs(_points_a_pixels((0.0, 0.0)), CADRE, iterations=iterations)


def test_ecarter_refuse_un_diametre_textuel():
    """Un diamètre donné comme texte est comparé à des pixels : la comparaison est refusée."""
    points = _points_a_pixels((0.0, 0.0), (5.0, 0.0))
    with pytest.raises(TypeError, match="not supported between instances"):
        ecarter_marqueurs(points, CADRE, diametre_px="28")


def test_ecarter_refuse_un_tableau_absent():
    """Sans tableau de pastilles (``None``), l'écartement est refusé."""
    with pytest.raises(TypeError, match="not subscriptable"):
        ecarter_marqueurs(None, CADRE)


def test_ecarter_centre_de_gravite_du_couple_inchange():
    """Les deux pastilles d'un couple s'éloignent l'une de l'autre sans que leur milieu bouge."""
    points = _points_a_pixels((0.0, 0.0), (3.0, 4.0))  # à 5 px l'une de l'autre
    positions = _affichees(ecarter_marqueurs(points, CADRE))
    milieu = (
        (positions[0][0] + positions[1][0]) / 2,
        (positions[0][1] + positions[1][1]) / 2,
    )
    assert milieu == pytest.approx((1.5, 2.0), abs=1e-6)
    ecart = math.hypot(positions[0][0] - positions[1][0], positions[0][1] - positions[1][1])
    assert ecart == pytest.approx(28.0, abs=1e-6)


def test_ecarter_pastille_isolee_inchangee_pendant_que_les_voisines_s_ecartent():
    """Seules les pastilles qui se recouvrent bougent : la pastille isolée reste en place."""
    points = _points_a_pixels((0.0, 0.0), (5.0, 0.0), (300.0, 200.0))
    resultat = ecarter_marqueurs(points, CADRE)
    positions = _affichees(resultat)
    assert positions[2] == pytest.approx((300.0, 200.0), abs=1e-6)
    assert resultat.loc[2, "lat_affichee"] == pytest.approx(points.loc[2, "latitude"], abs=1e-9)
    ecart = math.hypot(positions[0][0] - positions[1][0], positions[0][1] - positions[1][1])
    assert ecart == pytest.approx(28.0, abs=1e-6)


def test_ecarter_tableau_vide_sans_effet():
    """Aucune pastille, aucun déplacement : le résultat est vide mais porte les deux colonnes."""
    resultat = ecarter_marqueurs(_points(), CADRE)
    assert resultat.empty
    assert list(resultat.columns) == ["latitude", "longitude", "lon_affichee", "lat_affichee"]


def test_ecarter_vraies_positions_conservees_inchangees():
    """Les vraies positions, les autres colonnes et l'index restent ceux de l'entrée.

    Seules deux colonnes sont ajoutées, ``lon_affichee`` puis ``lat_affichee``.
    """
    points = _points_a_pixels((0.0, 0.0), (0.0, 0.0))
    points["nom"] = ["a", "b"]
    points.index = [10, 20]
    resultat = ecarter_marqueurs(points, CADRE)
    colonnes = ["latitude", "longitude", "nom", "lon_affichee", "lat_affichee"]
    assert list(resultat.columns) == colonnes
    assert list(resultat.index) == [10, 20]
    pd.testing.assert_frame_equal(resultat[["latitude", "longitude", "nom"]], points)
    assert resultat["lat_affichee"].iloc[0] != resultat["latitude"].iloc[0]  # elles ont bougé


# ------------------------------------------------------------------------------------------
# _priorite_commune et selectionner_top_hotels
# ------------------------------------------------------------------------------------------


def test_priorite_commune_valeurs_inconnues_ou_fausses_sans_effet_sur_le_rang():
    """Inconnu (None, NaN, NA) ou faux : priorité 0, aucun recul ; vrai (Python ou NumPy) : 1."""
    valeurs = pd.Series(
        [None, np.nan, pd.NA, False, np.False_, True, np.True_],
        dtype=object,
        index=list("abcdefg"),
    )
    priorites = _priorite_commune(valeurs)
    assert priorites.tolist() == [0, 0, 0, 0, 0, 1, 1]
    assert list(priorites.index) == list("abcdefg")
    assert pd.api.types.is_integer_dtype(priorites)


CAS_TRI = [
    (
        "la note l'emporte sur les étoiles",
        [
            _hotel(1, note=7.0, etoiles=5.0, nb_avis=500, distance_km=1.0, nom="A"),
            _hotel(9, note=8.0, etoiles=3.0, nb_avis=5, distance_km=9.0, nom="Z"),
        ],
        [9, 1],
    ),
    (
        "les étoiles l'emportent sur le nombre d'avis",
        [
            _hotel(1, etoiles=3.0, nb_avis=500, distance_km=1.0, nom="A"),
            _hotel(9, etoiles=4.0, nb_avis=10, distance_km=9.0, nom="Z"),
        ],
        [9, 1],
    ),
    (
        "le nombre d'avis l'emporte sur la distance",
        [
            _hotel(1, nb_avis=50, distance_km=1.0, nom="A"),
            _hotel(9, nb_avis=100, distance_km=9.0, nom="Z"),
        ],
        [9, 1],
    ),
    (
        "la distance (croissante) l'emporte sur le nom",
        [_hotel(1, distance_km=2.0, nom="A"), _hotel(9, distance_km=1.0, nom="Z")],
        [9, 1],
    ),
    (
        "le nom (croissant) l'emporte sur l'identifiant",
        [_hotel(1, nom="B"), _hotel(9, nom="A")],
        [9, 1],
    ),
    (
        "l'identifiant (croissant) départage deux homonymes",
        [_hotel(9, nom="A"), _hotel(1, nom="A")],
        [1, 9],
    ),
    (
        "une note manquante passe après une note basse",
        [_hotel(1, note=np.nan, etoiles=5.0), _hotel(2, note=3.0, etoiles=1.0)],
        [2, 1],
    ),
    (
        "des étoiles manquantes passent après des étoiles basses",
        [_hotel(1, etoiles=np.nan, nb_avis=900), _hotel(2, etoiles=1.0, nb_avis=1)],
        [2, 1],
    ),
    (
        "un nombre d'avis manquant passe après un nombre bas",
        [_hotel(1, nb_avis=np.nan, distance_km=0.1), _hotel(2, nb_avis=1, distance_km=50.0)],
        [2, 1],
    ),
    (
        "une distance manquante passe après une distance grande",
        [_hotel(1, distance_km=np.nan, nom="A"), _hotel(2, distance_km=99.0, nom="Z")],
        [2, 1],
    ),
]


def test_selection_cles_de_tri_dans_l_ordre_et_manquants_en_dernier():
    """Ordre des clés : note, étoiles, avis (décroissants), distance, nom, identifiant (croissants).

    Chaque cas oppose deux hôtels dont seule la clé testée donne le bon ordre ; les valeurs
    manquantes passent toujours après une valeur renseignée, même mauvaise.
    """
    for nom, lignes, attendu in CAS_TRI:
        resultat = selectionner_top_hotels(pd.DataFrame(lignes), VILLES)
        assert resultat["hotel_id"].tolist() == attendu, nom


def test_selection_top_borne_les_hotels_et_numerote_de_1_a_n():
    """``top`` borne le résultat (20 par défaut) ; les rangs vont de 1 à n et l'index de 0 à n-1."""
    hotels = _hotels()
    ordre_complet = selectionner_top_hotels(hotels, VILLES, top=25)["hotel_id"].tolist()
    attendus = [(None, 20), (1, 1), (3, 3), (20, 20), (25, 25), (30, 25)]
    for top, nombre in attendus:
        options = {} if top is None else {"top": top}
        resultat = selectionner_top_hotels(hotels, VILLES, **options)
        assert resultat["rang_hotel"].tolist() == list(range(1, nombre + 1)), top
        assert resultat.index.tolist() == list(range(nombre)), top
        assert resultat["hotel_id"].tolist() == ordre_complet[:nombre], top
        assert list(resultat.columns) == [*hotels.columns, "rang_hotel", "indice_qualite"], top
    # Par défaut, les cinq hôtels sans étoiles (un par destination) sortent du top 20.
    par_defaut = selectionner_top_hotels(hotels, VILLES)
    assert set(hotels["hotel_id"]) - set(par_defaut["hotel_id"]) == {1005, 1010, 1015, 1020, 1025}


def test_selection_indice_qualite_note_puis_etoiles_fois_deux_puis_cinq():
    """Indice sur 10 : la note si elle existe (même 0), sinon étoiles × 2, sinon 5."""
    hotels = pd.DataFrame(
        [
            _hotel(1, note=8.4, etoiles=3.0),
            _hotel(2, note=0.0, etoiles=5.0),
            _hotel(3, note=np.nan, etoiles=4.0),
            _hotel(4, note=np.nan, etoiles=np.nan),
            _hotel(5, note=np.nan, etoiles=0.0),
        ]
    )
    resultat = selectionner_top_hotels(hotels, VILLES)
    assert resultat.set_index("hotel_id")["indice_qualite"].to_dict() == {
        1: 8.4,
        2: 0.0,
        3: 8.0,
        4: 5.0,
        5: 0.0,
    }
    assert resultat["indice_qualite"].dtype == float
    # Une colonne de notes entièrement vide, lue comme « objet » (None), donne tout de même des
    # flottants.
    sans_notes = pd.DataFrame([_hotel(1, note=None, etoiles=3.0), _hotel(2, note=None)])
    assert sans_notes["note"].dtype == object
    indices = selectionner_top_hotels(sans_notes, VILLES)["indice_qualite"]
    assert indices.tolist() == [6.0, 5.0]
    assert indices.dtype == float


@pytest.mark.parametrize("colonne", ["rang_meteo", "id_ville"])
def test_selection_refuse_une_colonne_absente_des_villes(colonne):
    """Sans rang ni identifiant de destination, on ne sait pas quelles sont les 5 meilleures."""
    with pytest.raises(KeyError, match=colonne):
        selectionner_top_hotels(_hotels(), VILLES.drop(columns=colonne))


@pytest.mark.parametrize("colonne", ["hotel_id", "id_ville", "nom"])
def test_selection_refuse_une_colonne_absente_des_hotels(colonne):
    """Identifiant, destination et nom sont indispensables (filtre et départage du tri)."""
    with pytest.raises(KeyError, match=colonne):
        selectionner_top_hotels(_hotels().drop(columns=colonne), VILLES)


def test_selection_refuse_des_hotels_absents():
    """Sans tableau d'hôtels (``None``), la sélection est refusée."""
    with pytest.raises(TypeError, match="not subscriptable"):
        selectionner_top_hotels(None, VILLES)


def test_selection_refuse_des_villes_absentes():
    """Sans tableau de destinations (``None``), la sélection est refusée."""
    with pytest.raises(AttributeError, match="nsmallest"):
        selectionner_top_hotels(_hotels(), None)


def test_selection_ne_modifie_pas_les_tableaux_d_entree():
    """Ni les hôtels ni les villes ne sont triés, complétés ou annotés par la sélection."""
    hotels, villes = _hotels(), VILLES.copy()
    hotels_avant, villes_avant = hotels.copy(deep=True), villes.copy(deep=True)
    selectionner_top_hotels(hotels, villes, privilegier_commune=True)
    pd.testing.assert_frame_equal(hotels, hotels_avant)
    pd.testing.assert_frame_equal(villes, villes_avant)
    assert "priorite_commune" not in hotels.columns
    assert "rang_hotel" not in hotels.columns


@pytest.mark.parametrize("cas", ["sixieme_destination", "destination_inconnue"])
def test_selection_hotels_hors_top5_sans_effet(cas):
    """Les hôtels de la 6e destination ou d'une destination inconnue n'entrent jamais au top."""
    hotels = _hotels()
    etrangers = hotels.head(5).assign(
        id_ville=28 if cas == "sixieme_destination" else 999,
        hotel_id=hotels.head(5)["hotel_id"] + 5000,
    )
    villes = VILLES.iloc[[5, 0, 1, 2, 3, 4]].reset_index(drop=True)  # la 6e en première ligne
    avec = selectionner_top_hotels(pd.concat([hotels, etrangers], ignore_index=True), villes)
    sans = selectionner_top_hotels(hotels, VILLES)
    pd.testing.assert_frame_equal(avec, sans)
    assert set(avec["id_ville"]) == {26, 19, 31, 25, 20}


def test_selection_destination_sans_rang_sans_effet():
    """Les hôtels d'une destination sans rang météo (NaN) ne sont jamais retenus."""
    hotels = _hotels()
    fantomes = hotels.head(5).assign(id_ville=99, hotel_id=hotels.head(5)["hotel_id"] + 5000)
    avec = selectionner_top_hotels(
        pd.concat([hotels, fantomes], ignore_index=True), _villes_avec_destination_sans_rang()
    )
    pd.testing.assert_frame_equal(avec, selectionner_top_hotels(hotels, VILLES))


def test_selection_ordre_des_lignes_d_entree_sans_effet():
    """Hôtels et villes mélangés : même classement, car le tri finit par l'identifiant."""
    hotels = _hotels()
    melange = hotels.iloc[np.random.default_rng(2026).permutation(len(hotels))]
    villes = VILLES.iloc[[4, 1, 5, 0, 3, 2]].reset_index(drop=True)
    pd.testing.assert_frame_equal(
        selectionner_top_hotels(melange, villes), selectionner_top_hotels(hotels, VILLES)
    )


def test_selection_valeurs_des_colonnes_d_origine_intactes():
    """Les colonnes d'origine des hôtels retenus gardent exactement leurs valeurs d'entrée."""
    hotels = _hotels()
    resultat = selectionner_top_hotels(hotels, VILLES).set_index("hotel_id")
    origine = hotels.set_index("hotel_id").loc[resultat.index]
    for colonne in origine.columns:
        pd.testing.assert_series_equal(resultat[colonne], origine[colonne], obj=colonne)


def test_selection_hors_commune_sans_effet_tant_que_privilegier_commune_est_faux():
    """Par défaut, être d'une autre commune ne change pas le rang et n'ajoute aucune colonne."""
    valeurs = [True, True, False, None, True]
    confirmes = pd.DataFrame(
        [_hotel(i, etoiles=float(6 - i), hors_commune=v) for i, v in enumerate(valeurs, start=1)]
    )
    inconnus = confirmes.assign(hors_commune=np.nan)
    resultat = selectionner_top_hotels(confirmes, VILLES)
    assert resultat["hotel_id"].tolist() == [1, 2, 3, 4, 5]
    assert selectionner_top_hotels(inconnus, VILLES)["hotel_id"].tolist() == [1, 2, 3, 4, 5]
    assert "priorite_commune" not in resultat.columns


def test_selection_privilegier_commune_ordre_relatif_inchange_dans_chaque_groupe():
    """Seuls les hôtels CONFIRMÉS d'une autre commune reculent, l'ordre de chaque groupe restant.

    None, NaN et False restent devant ; True et ``numpy.True_`` passent derrière. Aucun hôtel
    n'est écarté : seul l'ordre change.
    """
    valeurs = [True, None, np.True_, np.nan, False, np.False_]
    hotels = pd.DataFrame(
        [
            _hotel(i, etoiles=float(7 - i), hors_commune=v)
            for i, v in enumerate(valeurs, start=1)
        ]
    )
    par_defaut = selectionner_top_hotels(hotels, VILLES)
    privilegie = selectionner_top_hotels(hotels, VILLES, privilegier_commune=True)
    assert par_defaut["hotel_id"].tolist() == [1, 2, 3, 4, 5, 6]
    assert privilegie["hotel_id"].tolist() == [2, 4, 5, 6, 1, 3]
    assert privilegie["priorite_commune"].tolist() == [0, 0, 0, 0, 1, 1]
    assert sorted(privilegie["hotel_id"]) == sorted(par_defaut["hotel_id"])


def test_selection_privilegier_commune_sans_hotel_confirme_classement_inchange():
    """Si aucun hôtel n'est confirmé d'une autre commune, l'option laisse le classement tel quel."""
    hotels = pd.DataFrame(
        [
            _hotel(i, etoiles=float(7 - i), hors_commune=v)
            for i, v in enumerate([None, np.nan, False, np.False_], start=1)
        ]
    )
    privilegie = selectionner_top_hotels(hotels, VILLES, privilegier_commune=True)
    pd.testing.assert_frame_equal(
        privilegie.drop(columns="priorite_commune"), selectionner_top_hotels(hotels, VILLES)
    )
    assert privilegie["priorite_commune"].tolist() == [0, 0, 0, 0]


def test_selection_privilegier_commune_sans_colonne_hors_commune_classement_inchange():
    """Sans colonne ``hors_commune`` (source sans cette information), aucun hôtel ne recule."""
    hotels = pd.DataFrame([_hotel(i, etoiles=float(7 - i)) for i in (1, 2, 3)])
    privilegie = selectionner_top_hotels(hotels, VILLES, privilegier_commune=True)
    assert privilegie["hotel_id"].tolist() == [1, 2, 3]
    assert privilegie["priorite_commune"].tolist() == [0, 0, 0]


def test_selection_colonnes_optionnelles_absentes_sans_effet_par_rapport_a_des_colonnes_vides():
    """Une source sans note, étoiles, avis, distance ni commune se classe comme si ces colonnes
    existaient mais étaient vides ; elles sont ajoutées, remplies de valeurs manquantes."""
    minimal = pd.DataFrame(
        [_hotel(3, nom="C"), _hotel(1, nom="B"), _hotel(2, nom="A")]
    ).drop(columns=["ville", "latitude", "longitude"])
    vides = minimal.assign(
        note=np.nan, etoiles=np.nan, nb_avis=np.nan, distance_km=np.nan, hors_commune=np.nan
    )
    resultat = selectionner_top_hotels(minimal, VILLES)
    pd.testing.assert_frame_equal(resultat, selectionner_top_hotels(vides, VILLES), check_like=True)
    assert resultat["hotel_id"].tolist() == [2, 1, 3]
    for colonne in ("note", "etoiles", "nb_avis", "distance_km", "hors_commune"):
        assert resultat[colonne].isna().all(), colonne
    assert resultat["indice_qualite"].tolist() == [5.0, 5.0, 5.0]


# ------------------------------------------------------------------------------------------
# _court
# ------------------------------------------------------------------------------------------


def test_court_coupe_a_trente_caracteres_avec_ellipse_et_retire_l_espace_final():
    """Au-delà de 30 caractères : 29 caractères, espace final retiré, puis « … » (30 au plus)."""
    cas = [
        ("a" * 31, 30, "a" * 29 + "…"),
        ("abcdefghijklmnopqrstuvwxyz0123456789", 30, "abcdefghijklmnopqrstuvwxyz012…"),
        ("a" * 28 + " bc", 30, "a" * 28 + "…"),
        ("Boulevard des Alpes-Maritimes", 18, "Boulevard des Alp…"),
        ("abcdef", 5, "abcd…"),
    ]
    for texte, longueur, attendu in cas:
        assert _court(texte, longueur) == attendu, (texte, longueur)
    assert _court("a" * 31) == "a" * 29 + "…"  # la longueur par défaut est 30


def test_court_texte_de_longueur_exacte_ou_inferieure_inchange():
    """Un texte qui tient (30 caractères ou moins, ou la limite donnée) est rendu tel quel."""
    for texte, longueur in [("a" * 30, 30), ("é" * 30, 30), ("court", 30), ("", 30), ("abcde", 5)]:
        assert _court(texte, longueur) == texte, (texte, longueur)
    assert _court("a" * 30) == "a" * 30  # limite par défaut : 30 caractères passent


@pytest.mark.parametrize("texte", [None, 12, 3.5])
def test_court_refuse_un_texte_absent_ou_non_textuel(texte):
    """Une valeur sans longueur (None, nombre) n'est pas un nom : on refuse de la couper."""
    with pytest.raises(TypeError, match="has no len"):
        _court(texte)


# ------------------------------------------------------------------------------------------
# carte_hotels
# ------------------------------------------------------------------------------------------


def _hotels_du_tableau() -> pd.DataFrame:
    """Cinq hôtels de la destination n° 1 aux valeurs choisies pour exercer chaque format."""
    return pd.DataFrame(
        [
            _hotel(
                1,
                nom="Hôtel Méditerranée",
                etoiles=5.0,
                distance_km=1.04,
                commune="Aigues Mortes",
                hors_commune=False,
            ),
            _hotel(
                2,
                nom="A" * 31,
                etoiles=4.0,
                distance_km=2.26,
                commune="La Grande-Motte-sur-Mer",
                hors_commune=True,
            ),
            _hotel(3, etoiles=3.0, distance_km=np.nan, commune=np.nan, hors_commune=np.nan),
            _hotel(4, etoiles=2.0, distance_km=4.0, commune="Nîmes", hors_commune=None),
            _hotel(5, etoiles=np.nan, distance_km=5.04, commune=np.nan, hors_commune=np.True_),
        ]
    )


def test_carte_hotels_contenu_exact_des_tableaux():
    """En-têtes, largeurs, styles, textes (étoiles, km, commune) et couleurs des deux tableaux."""
    fig = carte_hotels(_hotels_du_tableau(), VILLES)
    premier, second = _tableaux(fig)
    for tableau in (premier, second):
        assert list(tableau.header.values) == [
            "N°",
            "Hôtel",
            "Destination",
            "★",
            "km",
            "Commune OSM",
        ]
        assert list(tableau.columnwidth) == [0.06, 0.34, 0.2, 0.07, 0.1, 0.23]
        assert tableau.header.fill.color == "#1f4e79"
        assert (tableau.header.font.color, tableau.header.font.size) == ("white", 13)
        assert tableau.header.align == "left"
        assert (tableau.cells.align, tableau.cells.height) == ("left", 28)
        assert tableau.cells.font.size == 13
    # Première moitié (3 hôtels) : nom entier, nom coupé à 30, distance manquante « - ».
    assert _valeurs(premier) == [
        ["1", "2", "3"],
        ["Hôtel Méditerranée", "A" * 29 + "…", "Hôtel 3"],
        ["Aigues Mortes"] * 3,
        ["5", "4", "3"],
        ["1.0", "2.3", "-"],
        ["Aigues Mortes", "La Grande-Motte-s…", "?"],
    ]
    # Seconde moitié (2 hôtels) : étoiles manquantes « - », commune manquante « ? ».
    assert _valeurs(second) == [
        ["4", "5"],
        ["Hôtel 4", "Hôtel 5"],
        ["Aigues Mortes"] * 2,
        ["2", "-"],
        ["4.0", "5.0"],
        ["Nîmes", "?"],
    ]
    # Numéro en blanc sur la couleur de la destination ; commune en rouge si elle est ailleurs.
    assert [list(c) for c in premier.cells.font.color] == [
        ["white"] * 3,
        ["#222222"] * 3,
        ["#222222"] * 3,
        ["#222222"] * 3,
        ["#222222"] * 3,
        [NEUTRE, ROUGE, NEUTRE],
    ]
    assert [list(c) for c in second.cells.font.color][5] == [NEUTRE, ROUGE]
    assert [list(c) for c in premier.cells.fill.color] == [
        ["#1b6ca8"] * 3,
        ["#f5f7fa"] * 3,
        ["#f5f7fa"] * 3,
        ["#f5f7fa"] * 3,
        ["#f5f7fa"] * 3,
        ["#f5f7fa"] * 3,
    ]
    # Avec ``privilegier_commune``, les hôtels confirmés d'une autre commune passent derrière.
    privilegie = carte_hotels(_hotels_du_tableau(), VILLES, privilegier_commune=True)
    noms = [_valeurs(t)[1] for t in _tableaux(privilegie)]
    assert noms == [["Hôtel Méditerranée", "Hôtel 3", "Hôtel 4"], ["A" * 29 + "…", "Hôtel 5"]]
    # Sans colonnes de commune dans la source : « ? » partout et aucune commune en rouge.
    sans_commune = _hotels_du_tableau().drop(columns=["commune", "hors_commune"])
    figure = carte_hotels(sans_commune, VILLES)
    assert [_valeurs(t)[5] for t in _tableaux(figure)] == [["?"] * 3, ["?"] * 2]
    assert _couleurs_commune(figure) == [NEUTRE] * 5


def test_carte_hotels_moitie_des_hotels_dans_chaque_tableau():
    """La moitié (arrondie au-dessus) va dans le premier tableau ; pas de tableau vide."""
    cas = {
        1: [["1"]],
        2: [["1"], ["2"]],
        3: [["1", "2"], ["3"]],
        4: [["1", "2"], ["3", "4"]],
        5: [["1", "2", "3"], ["4", "5"]],
        7: [["1", "2", "3", "4"], ["5", "6", "7"]],
    }
    for top, attendu in cas.items():
        tableaux = _tableaux(carte_hotels(_hotels(), VILLES, top=top))
        assert [_valeurs(t)[0] for t in tableaux] == attendu, top
        if len(tableaux) > 1:
            assert tableaux[0].domain.x[1] <= tableaux[1].domain.x[0], top  # côte à côte


def test_carte_hotels_une_carte_par_destination_ayant_des_hotels(figure_hotels):
    """Une carte, un titre « rang. ville - n hôtel(s) » et une couleur par destination du top.

    La grille est celle de ``make_subplots`` : cartes en haut (60 % de la hauteur), deux tableaux
    en bas (40 %), 0,8 % d'écart entre colonnes et 6 % entre les deux rangées.
    """
    hotels = _hotels()
    couleur_par_ville = dict(zip([26, 19, 31, 25, 20], COULEURS_ATTENDUES, strict=True))
    cas = [
        ([26, 31], ["1. Aigues Mortes - 5 hôtel(s)", "3. Toulouse - 5 hôtel(s)"]),
        (
            [26, 19, 31, 25, 20],
            [
                "1. Aigues Mortes - 4 hôtel(s)",
                "2. Bormes les Mimosas - 4 hôtel(s)",
                "3. Toulouse - 4 hôtel(s)",
                "4. Nimes - 4 hôtel(s)",
                "5. Cassis - 4 hôtel(s)",
            ],
        ),
        ([], []),
        ([28], []),  # la 6e destination n'a pas de carte
    ]
    for ids, titres in cas:
        fig = carte_hotels(hotels[hotels["id_ville"].isin(ids)], VILLES)
        cartes = sorted(cle for cle in fig.layout if cle.startswith("map"))
        assert cartes == ["map"] + [f"map{i}" for i in range(2, len(titres) + 1)], ids
        assert [a.text for a in fig.layout.annotations if a.text.endswith("hôtel(s)")] == titres
        lignes = [t for t in fig.data if t.type == "scattermap" and t.mode == "lines"]
        traits = {t.subplot: t.line.color for t in lignes}
        attendu = [couleur_par_ville[i] for i in (26, 19, 31, 25, 20) if i in ids]
        assert list(traits.values()) == attendu, ids
        if not titres:
            assert fig.data == ()  # ni carte, ni tableau quand aucun hôtel n'est retenu
        classement = selectionner_top_hotels(hotels[hotels["id_ville"].isin(ids)], VILLES)
        pastilles = [couleur_par_ville[i] for i in classement["id_ville"]]
        assert [c for t in _tableaux(fig) for c in t.cells.fill.color[0]] == pastilles, ids
    cartes = figure_hotels.layout
    assert cartes["map"].domain.x == pytest.approx((0.0, 0.1936))
    assert cartes["map2"].domain.x == pytest.approx((0.2016, 0.3952))
    assert cartes["map"].domain.y == pytest.approx((0.436, 1.0))
    premier, second = _tableaux(figure_hotels)
    assert premier.domain.x == pytest.approx((0.0, 0.496))
    assert second.domain.x == pytest.approx((0.504, 1.0))
    assert premier.domain.y == pytest.approx((0.0, 0.376))


def test_carte_hotels_titre_sous_titre_et_dimensions():
    """Titre, taille, marges et sous-titre exacts, avec ou sans mention d'échantillon."""
    sous_titre = (
        "aire de destination (8 km autour du centre) · classement : étoiles, puis proximité du "
        "centre<br>point noir = centre · petit point = vraie position (pastilles écartées) · "
        "rouge = commune différente de la destination · ? = commune non renseignée"
    )
    fig = carte_hotels(_hotels(), VILLES)
    disposition = fig.layout
    assert disposition.title.text == "Top 20 hôtels des 5 meilleures destinations"
    titre = disposition.title
    assert (titre.x, titre.y, titre.yanchor) == (0.5, 0.985, "top")
    assert (disposition.width, disposition.height) == (1600, 1050)
    marge = disposition.margin
    assert (marge.t, marge.r, marge.b, marge.l) == (140, 10, 10, 10)
    assert disposition.showlegend is False
    sous = fig.layout.annotations[-1]
    assert sous.text == sous_titre
    assert (sous.x, sous.xref, sous.yref, sous.yanchor) == (0.5, "paper", "paper", "bottom")
    assert sous.y == pytest.approx(1 + 40 / 900)  # 1050 - 140 - 10 pixels utiles
    assert (sous.showarrow, sous.align, sous.font.size, sous.font.color) == (
        False,
        "center",
        13,
        NEUTRE,
    )

    note = "20 meilleurs parmi au plus 40 hôtels OSM par ville"
    personnalisee = carte_hotels(
        _hotels(), VILLES, top=7, note_echantillon=note, largeur=1200, hauteur=800
    )
    assert personnalisee.layout.title.text == "Top 7 hôtels des 5 meilleures destinations"
    assert (personnalisee.layout.width, personnalisee.layout.height) == (1200, 800)
    assert personnalisee.layout.annotations[-1].text == f"<b>{note}</b><br>{sous_titre}"
    assert personnalisee.layout.annotations[-1].y == pytest.approx(1 + 40 / 650)


def test_carte_hotels_traces_et_survol_de_la_premiere_carte():
    """Quatre traces par carte (traits, vrais points, centre noir, pastilles) et leurs textes.

    Le numéro d'une pastille est le rang de l'hôtel dans ``selectionner_top_hotels``.
    """
    hotels = pd.DataFrame(
        [
            _hotel(1, etoiles=5.0, distance_km=1.0, latitude=43.566, longitude=4.192),
            _hotel(2, etoiles=4.0, distance_km=2.26, latitude=43.586, longitude=4.212),
            _hotel(3, etoiles=np.nan, distance_km=3.0, latitude=43.606, longitude=4.232),
        ]
    )
    fig = carte_hotels(hotels, VILLES)
    traces = [t for t in fig.data if t.type == "scattermap"]
    assert [t.mode for t in traces] == ["lines", "markers", "markers", "markers+text"]
    traits, vrais, centre, pastilles = traces
    assert all(t.subplot == "map" for t in traces)
    assert [t.showlegend for t in traces] == [False] * 4
    assert (traits.line.width, traits.line.color, traits.hoverinfo) == (2, "#1b6ca8", "skip")
    assert list(traits.lat[2::3]) == [None] * 3 and list(traits.lon[2::3]) == [None] * 3
    assert list(traits.lat[0::3]) == pytest.approx([43.566, 43.586, 43.606])
    assert list(traits.lon[0::3]) == pytest.approx([4.192, 4.212, 4.232])
    assert (vrais.marker.size, vrais.marker.color, vrais.hoverinfo) == (6, "#1b6ca8", "skip")
    assert list(vrais.lat) == pytest.approx([43.566, 43.586, 43.606])
    assert (centre.marker.size, centre.marker.color) == (11, "black")
    assert (list(centre.lat), list(centre.lon)) == ([43.566], [4.192])
    assert centre.hovertemplate == "Centre de la destination : Aigues Mortes<extra></extra>"
    assert (pastilles.marker.size, pastilles.marker.color) == (26, "#1b6ca8")
    assert list(pastilles.text) == ["1", "2", "3"]
    assert (pastilles.textfont.size, pastilles.textfont.color) == (14, "white")
    assert pastilles.textposition == "middle center"
    assert pastilles.hovertemplate == (
        "<b>%{text}. %{customdata[0]}</b><br>étoiles : %{customdata[2]}"
        "<br>à %{customdata[1]} km du centre<extra></extra>"
    )
    assert [list(ligne) for ligne in pastilles.customdata] == [
        ["Hôtel 1", "1.0", "5"],
        ["Hôtel 2", "2.3", "4"],
        ["Hôtel 3", "3.0", "?"],
    ]
    # Sur le jeu complet, chaque numéro de pastille désigne l'hôtel de ce rang dans le classement.
    tous = _hotels()
    classement = selectionner_top_hotels(tous, VILLES)
    nom_par_rang = {}
    for trace in carte_hotels(tous, VILLES).data:
        if trace.type == "scattermap" and trace.mode == "markers+text":
            noms = [ligne[0] for ligne in trace.customdata]
            nom_par_rang.update(zip(trace.text, noms, strict=True))
    rangs = classement["rang_hotel"].astype(str)
    assert nom_par_rang == dict(zip(rangs, classement["nom"], strict=True))


def test_carte_hotels_cadrage_de_chaque_carte_avec_16_pour_cent_de_marge():
    """Les hôtels ET le centre tiennent dans la carte, la marge de 16 % étant atteinte.

    La taille de chaque panneau est lue dans son domaine ; le zoom est plafonné à 15 par
    ``cadrage``, ce qui n'est pas atteint ici (hôtels répartis sur plusieurs kilomètres). Deux
    jeux : hôtels en diagonale (la largeur du panneau limite le zoom) et hôtels alignés
    nord-sud (la hauteur du panneau le limite).
    """
    en_diagonale = _hotels()
    en_diagonale[["latitude", "longitude"]] += 0.05  # le centre est hors de l'enveloppe des hôtels
    en_colonne = _hotels()
    rang_dans_la_ville = (en_colonne["hotel_id"] - 1001) % 5  # 0 à 4 dans chaque destination
    en_colonne["longitude"] -= 0.01 * rang_dans_la_ville  # même longitude pour tous
    en_colonne["latitude"] += 0.05
    for nom, hotels in (("diagonale", en_diagonale), ("colonne", en_colonne)):
        classement = selectionner_top_hotels(hotels, VILLES)
        fig = carte_hotels(hotels, VILLES)
        for i, (_, ville) in enumerate(VILLES.head(5).iterrows()):
            cle = "map" if i == 0 else f"map{i + 1}"
            vue = fig.layout[cle]
            cadre = {"center": {"lat": vue.center.lat, "lon": vue.center.lon}, "zoom": vue.zoom}
            de_la_ville = classement[classement["id_ville"] == ville["id_ville"]]
            points = pd.concat(
                [
                    de_la_ville[["latitude", "longitude"]],
                    _points((ville["latitude"], ville["longitude"])),
                ]
            )
            largeur = (1600 - 20) * (vue.domain.x[1] - vue.domain.x[0])
            hauteur = (1050 - 150) * (vue.domain.y[1] - vue.domain.y[0])
            rapports = [
                max(abs(x) / (0.34 * largeur), abs(y) / (0.34 * hauteur))
                for x, y in _pixels(points, cadre)
            ]
            assert max(rapports) == pytest.approx(1.0, rel=1e-6), (nom, cle)  # le plus excentré
            assert vue.style == "open-street-map", (nom, cle)
            assert vue.zoom < 15, (nom, cle)


@pytest.mark.parametrize(
    "colonne", ["hotel_id", "id_ville", "ville", "nom", "latitude", "longitude"]
)
def test_carte_hotels_refuse_une_colonne_absente_des_hotels(colonne):
    """Sans ces colonnes, les hôtels ne peuvent être ni classés, ni placés, ni listés."""
    with pytest.raises(KeyError, match=colonne):
        carte_hotels(_hotels().drop(columns=colonne), VILLES)


@pytest.mark.parametrize("colonne", ["id_ville", "ville", "latitude", "longitude", "rang_meteo"])
def test_carte_hotels_refuse_une_colonne_absente_des_villes(colonne):
    """Sans ces colonnes, on ne sait ni retenir les destinations, ni les nommer, ni les centrer."""
    with pytest.raises(KeyError, match=colonne):
        carte_hotels(_hotels(), VILLES.drop(columns=colonne))


def test_carte_hotels_refuse_des_hotels_absents():
    """Sans tableau d'hôtels (``None``), la carte est refusée."""
    with pytest.raises(TypeError, match="not subscriptable"):
        carte_hotels(None, VILLES)


def test_carte_hotels_refuse_des_villes_absentes():
    """Sans tableau de destinations (``None``), la carte est refusée."""
    with pytest.raises(AttributeError, match="nsmallest"):
        carte_hotels(_hotels(), None)


@pytest.mark.parametrize(("largeur", "hauteur"), [(20, 1050), (1600, 150), (10, 10)])
def test_carte_hotels_refuse_une_image_sans_surface_pour_les_cartes(largeur, hauteur):
    """Marges comprises (20 px en largeur, 150 en hauteur), il ne reste aucun pixel : refusé."""
    with pytest.raises(ValueError, match="math domain error"):
        carte_hotels(_hotels(), VILLES, largeur=largeur, hauteur=hauteur)


def test_carte_hotels_ne_modifie_pas_les_tableaux_d_entree():
    """Ni les hôtels ni les villes ne sont modifiés par la construction de la carte."""
    hotels, villes = _hotels(), VILLES.copy()
    hotels_avant, villes_avant = hotels.copy(deep=True), villes.copy(deep=True)
    carte_hotels(hotels, villes, privilegier_commune=True, note_echantillon="échantillon")
    pd.testing.assert_frame_equal(hotels, hotels_avant)
    pd.testing.assert_frame_equal(villes, villes_avant)


def test_carte_hotels_sixieme_destination_sans_effet(figure_hotels):
    """Les hôtels de la 6e destination n'ajoutent ni carte, ni pastille, ni ligne de tableau."""
    hotels = _hotels()
    sixieme = hotels.head(5).assign(
        id_ville=28, ville="Collioure", hotel_id=hotels.head(5)["hotel_id"] + 5000
    )
    avec = carte_hotels(pd.concat([hotels, sixieme], ignore_index=True), VILLES)
    assert avec.to_json() == figure_hotels.to_json()


def test_carte_hotels_destination_sans_rang_sans_effet(figure_hotels):
    """Une destination sans rang météo (NaN) n'ajoute ni carte, ni pastille, ni ligne de tableau."""
    hotels = _hotels()
    fantomes = hotels.head(5).assign(
        id_ville=99, ville="Fantôme", hotel_id=hotels.head(5)["hotel_id"] + 5000
    )
    avec = carte_hotels(
        pd.concat([hotels, fantomes], ignore_index=True), _villes_avec_destination_sans_rang()
    )
    assert avec.to_json() == figure_hotels.to_json()


def test_carte_hotels_privilegier_commune_faux_explicite_sans_effet_par_rapport_au_defaut():
    """Passer ``privilegier_commune=False`` revient au défaut, même avec des hôtels d'ailleurs."""
    hotels = _hotels_du_tableau()
    explicite = carte_hotels(hotels, VILLES, privilegier_commune=False)
    assert explicite.to_json() == carte_hotels(hotels, VILLES).to_json()
    noms = [_valeurs(t)[1] for t in _tableaux(explicite)]
    assert noms == [["Hôtel Méditerranée", "A" * 29 + "…", "Hôtel 3"], ["Hôtel 4", "Hôtel 5"]]


def test_carte_hotels_ordre_des_lignes_d_entree_sans_effet(figure_hotels):
    """Hôtels et villes mélangés : la carte est exactement la même."""
    hotels = _hotels()
    melange = hotels.iloc[np.random.default_rng(7).permutation(len(hotels))]
    villes = VILLES.iloc[[4, 1, 5, 0, 3, 2]].reset_index(drop=True)
    assert carte_hotels(melange, villes).to_json() == figure_hotels.to_json()


@pytest.mark.parametrize("note", [None, ""])
def test_carte_hotels_note_d_echantillon_vide_sans_effet(note, figure_hotels):
    """Une mention absente ou vide n'ajoute aucune ligne en gras au sous-titre."""
    fig = carte_hotels(_hotels(), VILLES, note_echantillon=note)
    assert fig.to_json() == figure_hotels.to_json()
    assert "<b>" not in fig.layout.annotations[-1].text


def test_carte_hotels_pastilles_eloignees_positions_affichees_inchangees(figure_hotels):
    """Des hôtels éloignés d'un diamètre de pastille ne bougent pas : traits de longueur nulle."""
    traces = [t for t in figure_hotels.data if t.type == "scattermap"]
    for debut in range(0, len(traces), 4):
        traits, vrais, _centre, pastilles = traces[debut : debut + 4]
        assert [t.mode for t in traces[debut : debut + 4]] == [
            "lines",
            "markers",
            "markers",
            "markers+text",
        ]
        assert list(pastilles.lat) == pytest.approx(list(vrais.lat), abs=1e-9)
        assert list(pastilles.lon) == pytest.approx(list(vrais.lon), abs=1e-9)
        assert list(traits.lat[1::3]) == pytest.approx(list(traits.lat[0::3]), abs=1e-9)
        assert list(traits.lon[1::3]) == pytest.approx(list(traits.lon[0::3]), abs=1e-9)


def test_carte_hotels_centre_et_vraies_positions_intacts_malgre_l_ecartement():
    """Trois hôtels au même point : les pastilles s'écartent, mais le centre et les vrais points
    restent aux coordonnées d'origine, et chaque trait relie la vraie position à la pastille."""
    hotels = pd.DataFrame([_hotel(i, etoiles=float(5 - i)) for i in (1, 2, 3)])
    fig = carte_hotels(hotels, VILLES)
    traits, vrais, centre, pastilles = [t for t in fig.data if t.type == "scattermap"]
    assert (list(centre.lat), list(centre.lon)) == ([43.566], [4.192])
    assert (list(vrais.lat), list(vrais.lon)) == ([43.566] * 3, [4.192] * 3)
    assert list(traits.lat[0::3]) == [43.566] * 3
    assert list(traits.lon[0::3]) == [4.192] * 3
    positions = set(zip(pastilles.lat, pastilles.lon, strict=True))
    assert len(positions) == len(pastilles.lat)  # trois pastilles, trois positions distinctes
    assert list(traits.lat[1::3]) == list(pastilles.lat)
    assert list(traits.lon[1::3]) == list(pastilles.lon)


def test_carte_hotels_couleur_neutre_inchangee_quand_la_commune_n_est_pas_confirmee():
    """False, None, NaN et NA gardent la couleur neutre, comme sans aucune information."""
    valeurs = [False, np.False_, None, np.nan, pd.NA]
    hotels = pd.DataFrame(
        [_hotel(i, etoiles=float(9 - i), hors_commune=v) for i, v in enumerate(valeurs, start=1)]
    )
    sans_information = hotels.drop(columns="hors_commune")
    avec = _couleurs_commune(carte_hotels(hotels, VILLES))
    assert avec == _couleurs_commune(carte_hotels(sans_information, VILLES))
    assert avec == [NEUTRE] * 5


# ------------------------------------------------------------------------------------------
# enregistrer
# ------------------------------------------------------------------------------------------


def test_enregistrer_chemins_appels_et_contenu_du_fichier_html(banc, monkeypatch):
    """HTML UTF-8 en fins de ligne « \\n », mention en tête, PNG demandé à l'échelle 2."""
    appels_html = []

    def faux_to_html(self, **options):
        appels_html.append(options)
        return "<html>\r\n★ héhé 日本\r\n</html>"

    monkeypatch.setattr(go.Figure, "to_html", faux_to_html)
    chemins = enregistrer(go.Figure(), "essai")
    assert chemins == {"html": banc.dossier / "essai.html", "png": banc.dossier / "essai.png"}
    assert appels_html == [{"include_plotlyjs": "cdn"}]
    assert banc.appels == [(banc.dossier / "essai.png", {"scale": 2})]
    attendu = f"<!-- {MENTION} -->\n<html>\n★ héhé 日本\n</html>".encode()
    assert (banc.dossier / "essai.html").read_bytes() == attendu


def test_enregistrer_refuse_une_racine_inexistante(banc, monkeypatch, tmp_path):
    """Le dossier « sorties » n'est créé que dans une racine existante : pas de parents créés."""
    absente = tmp_path / "absente"
    monkeypatch.setattr(config, "RACINE", absente)
    with pytest.raises(FileNotFoundError, match="sorties"):
        enregistrer(go.Figure(), "essai")
    assert not absente.exists()
    assert banc.appels == []


def test_enregistrer_refuse_une_figure_absente(banc):
    """Sans figure (``None``), rien n'est écrit : ni HTML, ni demande de PNG."""
    with pytest.raises(AttributeError, match="to_html"):
        enregistrer(None, "essai")
    assert not (banc.dossier / "essai.html").exists()
    assert banc.appels == []


def test_enregistrer_refuse_un_nom_dans_un_sous_dossier_absent(banc):
    """Un nom qui vise un sous-dossier inexistant est refusé : le dossier reste vide."""
    with pytest.raises(FileNotFoundError, match="essai.html"):
        enregistrer(go.Figure(), "sous/essai")
    assert list(banc.dossier.iterdir()) == []
    assert banc.appels == []


def test_enregistrer_dans_un_dossier_existant_laisse_intacts_les_autres_fichiers(banc):
    """Un dossier « sorties » déjà présent n'est ni vidé ni refusé : ses fichiers sont intacts."""
    banc.dossier.mkdir()
    (banc.dossier / "ancien.txt").write_text("à garder", encoding="utf-8")
    (banc.dossier / "autre.html").write_text("<p>autre</p>", encoding="utf-8")
    chemins = enregistrer(go.Figure(), "essai")
    assert (banc.dossier / "ancien.txt").read_text(encoding="utf-8") == "à garder"
    assert (banc.dossier / "autre.html").read_text(encoding="utf-8") == "<p>autre</p>"
    assert chemins["html"].read_text(encoding="utf-8").startswith(f"<!-- {MENTION} -->\n")


@pytest.mark.parametrize(
    "erreur",
    [RuntimeError("Kaleido est absent"), OSError("disque plein"), ValueError("format inconnu")],
)
def test_enregistrer_export_png_en_echec_laisse_le_html_intact_et_journalise(
    erreur, banc, monkeypatch, caplog
):
    """Un échec d'export PNG n'est pas bloquant : HTML complet, pas de clé png, avertissement."""

    def export_en_echec(self, chemin, **options):
        raise erreur

    monkeypatch.setattr(go.Figure, "write_image", export_en_echec)
    with caplog.at_level(logging.WARNING, logger="kayak.cartes"):
        chemins = enregistrer(carte_destinations(VILLES), "essai")
    assert chemins == {"html": banc.dossier / "essai.html"}
    assert not (banc.dossier / "essai.png").exists()
    contenu = (banc.dossier / "essai.html").read_text(encoding="utf-8")
    assert contenu.startswith(f"<!-- {MENTION} -->\n") and "<html" in contenu
    assert contenu.rstrip().endswith("</html>")
    journal = [
        (r.name, r.levelno, r.getMessage()) for r in caplog.records if r.name == "kayak.cartes"
    ]
    assert journal == [
        (
            "kayak.cartes",
            logging.WARNING,
            f"Export PNG impossible pour essai ({type(erreur).__name__}) : seul le HTML est écrit",
        )
    ]


def test_enregistrer_export_png_en_echec_garde_le_texte_de_l_erreur_hors_de_l_avertissement(
    banc, monkeypatch, caplog
):
    """Le texte de l'erreur n'entre pas dans l'avertissement, affiché dans le notebook.

    Celui de Kaleido cite l'emplacement du navigateur sur le poste : seul le type de l'erreur
    est donné en WARNING. Le texte complet n'est pas perdu : il est journalisé en DEBUG.
    """
    texte_erreur = "Le navigateur s'est fermé ; emplacement : /opt/navigateur/chrome"

    def export_en_echec(self, chemin, **options):
        raise RuntimeError(texte_erreur)

    monkeypatch.setattr(go.Figure, "write_image", export_en_echec)
    with caplog.at_level(logging.DEBUG, logger="kayak.cartes"):
        enregistrer(go.Figure(), "essai")
    journal = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "kayak.cartes"]
    assert journal == [
        (
            logging.WARNING,
            "Export PNG impossible pour essai (RuntimeError) : seul le HTML est écrit",
        ),
        (logging.DEBUG, f"Détail de l'échec de l'export PNG : {texte_erreur}"),
    ]
    assert not [m for niveau, m in journal if niveau >= logging.INFO and "/opt/" in m]


@pytest.mark.parametrize("export_reussi", [True, False], ids=["export_reussi", "export_en_echec"])
@pytest.mark.parametrize("niveau_avant", [logging.NOTSET, logging.INFO])
def test_enregistrer_tait_le_pilote_du_navigateur_sous_error_puis_rend_son_niveau(
    export_reussi, niveau_avant, banc, monkeypatch, caplog
):
    """Pendant l'export, le journal du pilote du navigateur (Choreographer) ne laisse passer que
    ERROR et au-delà : son avertissement en anglais (« Wait expired... ») doublerait celui du
    projet. Après l'export, réussi ou non, il retrouve exactement son niveau d'avant."""
    pilote = logging.getLogger("choreographer")
    niveaux_pendant = []

    def export(self, chemin, **options):
        niveaux_pendant.append(pilote.level)
        journal_tiers = logging.getLogger("choreographer.browser_async")
        journal_tiers.warning("Wait expired, Browser is being closed by watchdog.")
        journal_tiers.error("Erreur grave du navigateur")
        if not export_reussi:
            raise RuntimeError("navigateur absent")

    monkeypatch.setattr(go.Figure, "write_image", export)
    origine = pilote.level
    pilote.setLevel(niveau_avant)
    try:
        with caplog.at_level(logging.DEBUG):
            chemins = enregistrer(go.Figure(), "essai")
        assert pilote.level == niveau_avant
    finally:
        pilote.setLevel(origine)
    assert niveaux_pendant == [logging.ERROR]
    assert ("png" in chemins) is export_reussi
    messages_tiers = [
        (r.levelno, r.getMessage()) for r in caplog.records if r.name.startswith("choreographer")
    ]
    assert messages_tiers == [(logging.ERROR, "Erreur grave du navigateur")]


def test_enregistrer_deux_fois_la_meme_figure_laisse_le_fichier_inchange(banc, monkeypatch):
    """Enregistrer deux fois sous le même nom écrase le fichier sans le dupliquer ni le changer."""
    monkeypatch.setattr(go.Figure, "to_html", lambda self, **options: "<html>x</html>")
    premiers = enregistrer(go.Figure(), "essai")
    octets = premiers["html"].read_bytes()
    seconds = enregistrer(go.Figure(), "essai")
    assert seconds == premiers
    assert seconds["html"].read_bytes() == octets
    assert sorted(p.name for p in banc.dossier.iterdir()) == ["essai.html"]


def test_enregistrer_ne_modifie_pas_la_figure(banc):
    """L'enregistrement lit la figure : son contenu est identique avant et après."""
    fig = carte_destinations(VILLES)
    avant = fig.to_json()
    enregistrer(fig, "essai")
    assert fig.to_json() == avant


# ------------------------------------------------------------------------------------------
# Cas limites relevés par la mutation (17/10/2026)
# ------------------------------------------------------------------------------------------


def test_enregistrer_ouvre_le_html_en_utf8_et_en_lf_sur_tous_les_systemes(banc, espion_open):
    """Le HTML est ouvert en écriture texte, avec UTF-8 et fins de ligne « \\n » demandés.

    Sous Linux, l'oubli de ces réglages ne changerait pas le fichier ; sous Windows, il écrirait
    en cp1252 et en « \\r\\n » : on vérifie donc ce qui est demandé à ``open``.
    """
    ouvertures = espion_open(module_cartes)
    enregistrer(go.Figure(), "essai")
    assert [(o.fichier, o.mode, o.en_utf8, o.newline) for o in ouvertures] == [
        (str(banc.dossier / "essai.html"), "w", True, "\n")
    ]


def test_ecarter_au_seuil_exact_de_confusion_garde_la_droite_qui_relie_les_pastilles(monkeypatch):
    """À 1e-6 pixel exactement, deux pastilles ne sont pas « confondues ».

    Elles s'écartent le long de la droite qui les relie (ici est-ouest : la latitude affichée ne
    bouge pas) ; seules des pastilles à MOINS de 1e-6 pixel prennent une direction propre. La
    distance est forcée à 1e-6 (``math.hypot`` remplacé) : aucune paire de coordonnées réelles
    ne tombe exactement sur ce seuil.
    """
    points = pd.DataFrame({"latitude": [45.0, 45.0], "longitude": [5.0, 5.0 + 7e-10]})
    cadre = {"zoom": 10, "center": {"lat": 45.0, "lon": 5.0}}
    monkeypatch.setattr(module_cartes.math, "hypot", lambda _dx, _dy: 1e-6)
    resultat = ecarter_marqueurs(points, cadre, iterations=1)
    assert resultat["lat_affichee"].tolist() == pytest.approx([45.0, 45.0], abs=1e-9)
    assert resultat["lon_affichee"].iloc[0] < 5.0 < resultat["lon_affichee"].iloc[1]
