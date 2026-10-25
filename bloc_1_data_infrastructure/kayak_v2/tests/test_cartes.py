# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests des cartes : cadrage, classement du top 20, pastilles numérotées, écartement.

Les figures sont construites mais PAS exportées en image (kaleido et le fond de carte demandent
un navigateur et du réseau) : on vérifie leur contenu. La lisibilité réelle des PNG a été
contrôlée à l'œil après génération.
"""

import math

import numpy as np
import pandas as pd
import pytest

from kayak.cartes import (
    FOND_DE_CARTE,
    cadrage,
    carte_destinations,
    carte_hotels,
    ecarter_marqueurs,
    selectionner_top_hotels,
)

# Les cinq destinations du palmarès, avec leurs vraies coordonnées (arrondies).
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


def _y(latitude: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2))


def _pixels(points: pd.DataFrame, cadre: dict) -> list[tuple[float, float]]:
    """Position en pixels de chaque point par rapport au centre de la vue (Mercator)."""
    echelle = 512 * 2 ** cadre["zoom"] / (2 * math.pi)
    lon0 = math.radians(cadre["center"]["lon"])
    y0 = _y(cadre["center"]["lat"])
    return [
        ((math.radians(lon) - lon0) * echelle, (_y(lat) - y0) * echelle)
        for lat, lon in zip(points["latitude"], points["longitude"], strict=True)
    ]


def test_cadrage_englobe_les_points_avec_une_marge():
    """Tous les points tiennent dans la vue, sans la déborder, et la remplissent."""
    top5 = VILLES.head(5)
    cadre = cadrage(top5, 1100, 700, marge=0.12)
    positions = _pixels(top5, cadre)
    assert all(abs(x) <= 1100 / 2 and abs(y) <= 700 / 2 for x, y in positions)
    # Le point le plus excentré touche la marge utile (12 % de chaque côté), pas moins.
    assert max(abs(x) for x, _ in positions) == pytest.approx(0.5 * 1100 * (1 - 2 * 0.12))
    # L'ancien calcul (log2(360 / étendue) - 1) donnait ≈ 5,2 ici : deux niveaux de trop.
    assert cadre["zoom"] > 6.5


def test_cadrage_zoom_croit_quand_la_marge_diminue_et_est_borne():
    """Moins de marge, plus de zoom ; un point isolé n'a pas un zoom infini."""
    top5 = VILLES.head(5)
    serre = cadrage(top5, 1100, 700, marge=0.02)["zoom"]
    large = cadrage(top5, 1100, 700, marge=0.3)["zoom"]
    assert serre > large
    isole = VILLES.head(1)
    assert cadrage(isole, 500, 500, zoom_max=15.0)["zoom"] == 15.0


def test_carte_destinations_pastilles_numerotees_et_legende():
    """Chaque destination a sa pastille, numérotée DANS le disque, et son entrée de légende."""
    fig = carte_destinations(VILLES)
    pastilles = [t for t in fig.data if t.mode == "markers+text"]
    assert [t.text[0] for t in pastilles] == ["1", "2", "3", "4", "5"]  # jamais d'étiquette perdue
    assert all(t.textposition == "middle center" for t in pastilles)
    legende = [t.name for t in pastilles]
    assert legende[0].startswith("1. Aigues Mortes") and legende[4].startswith("5. Cassis")
    assert fig.layout.map.style == FOND_DE_CARTE
    # Pas de trace de texte seul (elle fait échouer l'export), ni de texte collé aux pastilles.
    assert not [t for t in fig.data if t.mode == "text"]
    assert list(fig.data) == pastilles  # aucune autre trace que les cinq pastilles


def test_carte_destinations_place_chaque_pastille_sur_sa_ville():
    """Les pastilles sont aux coordonnées des villes (et non à celles d'un hôtel)."""
    fig = carte_destinations(VILLES)
    for trace, (_, ville) in zip(fig.data, VILLES.head(5).iterrows(), strict=True):
        assert trace.lat[0] == ville["latitude"] and trace.lon[0] == ville["longitude"]


def _hotels() -> pd.DataFrame:
    """Hôtels des 5 destinations : 24 hôtels, des étoiles, des distances, des communes."""
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


def test_selection_classe_par_etoiles_puis_proximite_et_numerote():
    """Étoiles décroissantes, puis distance croissante ; rangs 1..20 ; manquants en dernier."""
    top = selectionner_top_hotels(_hotels(), VILLES)
    assert top["rang_hotel"].tolist() == list(range(1, 21))
    etoiles = top["etoiles"].tolist()
    assert etoiles[:5] == [5.0] * 5  # les cinq 5 étoiles (un par destination) en tête
    # Les cinq 5 étoiles sont à égalité de distance : le nom les départage, toujours pareil.
    assert top["distance_km"][:5].tolist() == [1.0] * 5
    assert top["hotel_id"][:5].is_monotonic_increasing
    # À étoiles égales, le plus proche du centre passe devant.
    quatre = top[top["etoiles"] == 4.0]
    assert quatre["distance_km"].is_monotonic_increasing
    # Résultat déterministe : deux appels donnent exactement le même ordre.
    encore = selectionner_top_hotels(_hotels(), VILLES)
    assert encore["hotel_id"].tolist() == top["hotel_id"].tolist()


def test_selection_privilegier_la_commune_fait_reculer_les_hotels_d_une_autre_commune():
    """L'option recule les hôtels CONFIRMÉS d'une autre commune, pas ceux de commune inconnue."""
    hotels = _hotels()
    # Le meilleur hôtel (5 étoiles) de la destination 1 est en réalité dans une autre commune.
    meilleur = hotels.index[(hotels["id_ville"] == 26) & (hotels["etoiles"] == 5.0)][0]
    hotels.loc[meilleur, ["commune", "hors_commune"]] = ["La Grande-Motte", True]
    identifiant = hotels.loc[meilleur, "hotel_id"]
    par_defaut = selectionner_top_hotels(hotels, VILLES)
    privilegie = selectionner_top_hotels(hotels, VILLES, privilegier_commune=True)
    assert identifiant in par_defaut["hotel_id"].tolist()[:5]  # par défaut : l'aire entière
    assert identifiant not in privilegie["hotel_id"].tolist()  # option : il recule hors du top
    # Les hôtels de commune inconnue ne sont pas pénalisés : ils restent dans le classement.
    inconnus = privilegie[privilegie["hors_commune"].isna()]
    assert len(inconnus) > 0


def test_carte_hotels_numeros_tableaux_et_mention_de_l_echantillon():
    """La carte du top 20 : une carte par destination, 20 pastilles numérotées, 2 tableaux."""
    mention = "20 meilleurs parmi au plus 40 hôtels OSM par ville"
    fig = carte_hotels(_hotels(), VILLES, note_echantillon=mention)
    numeros = []
    for trace in fig.data:
        if trace.type == "scattermap" and trace.mode == "markers+text":
            numeros += list(trace.text)
    assert sorted(numeros, key=int) == [str(i) for i in range(1, 21)]
    premier, second = (t for t in fig.data if t.type == "table")  # exactement deux tableaux
    assert premier.header["values"][:3] == ("N°", "Hôtel", "Destination")
    noms = [*premier.cells["values"][1], *second.cells["values"][1]]
    assert len(set(noms)) == len(noms)  # chaque hôtel est écrit une seule fois
    numeros = [*premier.cells["values"][0], *second.cells["values"][0]]
    assert numeros == [str(i) for i in range(1, 21)]
    # Une carte (« map ») par destination ayant au moins un hôtel dans le top, chacune cadrée.
    cartes = [cle for cle in fig.layout if cle.startswith("map")]
    assert sorted(cartes) == ["map", "map2", "map3", "map4", "map5"]
    assert all(fig.layout[cle].zoom is not None and fig.layout[cle].zoom > 8 for cle in cartes)
    # La mention de l'échantillon figure en toutes lettres sur la carte, avec l'aire de 8 km.
    textes = " ".join(a.text for a in fig.layout.annotations)
    assert mention in textes and "8 km" in textes


def test_carte_hotels_sans_mention_quand_l_echantillon_n_est_pas_limite():
    """Sans plafond d'échantillon à signaler, aucune mention n'est écrite."""
    fig = carte_hotels(_hotels(), VILLES)
    textes = " ".join(a.text for a in fig.layout.annotations)
    assert "meilleurs parmi" not in textes


def test_ecarter_marqueurs_separe_des_pastilles_confondues():
    """Des pastilles quasi confondues sont écartées d'au moins leur diamètre ; les autres non."""
    points = pd.DataFrame(
        {
            "latitude": [43.5000, 43.5000, 43.5001, 43.6000],
            "longitude": [4.0000, 4.0000, 4.0001, 4.2000],
        }
    )
    cadre = {"center": {"lat": 43.55, "lon": 4.1}, "zoom": 10.0}
    resultat = ecarter_marqueurs(points, cadre, diametre_px=28)
    affichees = pd.DataFrame(
        {"latitude": resultat["lat_affichee"], "longitude": resultat["lon_affichee"]}
    )
    positions = _pixels(affichees, cadre)
    for i in range(3):
        for j in range(i + 1, 3):
            dx = positions[i][0] - positions[j][0]
            dy = positions[i][1] - positions[j][1]
            ecart = math.hypot(dx, dy)
            assert ecart >= 28 - 1.0, (i, j, ecart)
    # Le point isolé n'a pas bougé ; les vraies positions sont conservées à part.
    assert resultat.loc[3, "lat_affichee"] == pytest.approx(43.6, abs=1e-9)
    assert resultat.loc[3, "lon_affichee"] == pytest.approx(4.2, abs=1e-9)
    assert resultat["latitude"].tolist() == points["latitude"].tolist()


def test_enregistrer_ecrit_le_html_avec_la_mention_de_droits(tmp_path, monkeypatch):
    """Le HTML interactif commence par la mention de droits ; le PNG est tenté à l'échelle 2.

    L'export d'image (kaleido, fond de carte en ligne) est remplacé par une fonction factice.
    """
    from plotly import graph_objects as go

    from kayak import cartes, config

    appels = []

    def faux_export(self, chemin, **options):
        appels.append((str(chemin), options))
        open(chemin, "wb").close()

    monkeypatch.setattr(config, "RACINE", tmp_path)
    monkeypatch.setattr(go.Figure, "write_image", faux_export)
    chemins = cartes.enregistrer(carte_destinations(VILLES), "essai")
    contenu = chemins["html"].read_text(encoding="utf-8")
    assert contenu.startswith("<!-- Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC")
    assert "<html" in contenu and chemins["png"].name == "essai.png"
    assert appels and appels[0][1] == {"scale": 2}
