# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Génération des cartes interactives avec Plotly.

Deux cartes sont produites :
- le **top 5 des destinations** : une seule carte, des pastilles numérotées de 1 à 5 (le numéro
  est dessiné DANS la pastille, donc toujours lisible, alors qu'une étiquette posée à côté d'un
  point disparaît dès qu'elle en chevauche une autre) et une légende qui donne le nom et le
  détail de chaque destination ;
- le **top 20 des hôtels** : une carte resserrée par destination (une vue France entière
  noyait les hôtels dans quelques pixels), des pastilles numérotées de 1 à 20 et un tableau
  qui nomme chaque hôtel, sa distance au centre et sa commune.

CHOIX
-----
- **Retenu : le numéro DANS la pastille** et le nom dans la légende. *Écarté* : une étiquette
  posée à côté du point, que la carte masque dès qu'elle en chevauche une autre ; et deux textes
  collés, mis en concurrence par la carte, qui en cachait un.
- **Retenu : une carte resserrée par destination pour le top 20.** *Écarté* : une vue France
  entière, où les hôtels tenaient dans quelques pixels.
- **Retenu : calculer le zoom en pixels** (le monde mesure 512 × 2^z pixels, projection de
  Mercator, marge de chaque côté). *Écarté* : l'ancien calcul ``log2(360 / étendue) − 1``, qui
  laissait deux niveaux de zoom de trop (la moitié de l'Europe pour cinq villes du Sud).
- **Retenu : écarter les pastilles qui se chevauchent** (``ecarter_marqueurs``) et relier chacune
  à la vraie position de l'hôtel par un trait. *Écarté* : laisser les pastilles se recouvrir, où
  seul le dernier numéro se lit.
- **Retenu : classer les hôtels par note, étoiles, avis, proximité du centre, nom, identifiant**,
  les valeurs manquantes en dernier, pour un résultat toujours identique. *Écarté* : laisser les
  nombreux 4 étoiles ex æquo au hasard. L'option ``privilegier_commune`` existe mais n'est pas le
  défaut : la définition retenue des hôtels d'une destination est l'aire de 8 km.
- **Retenu : l'export PNG est facultatif** (Kaleido exige un navigateur) : un échec est
  journalisé, pas bloquant.
- **Retenu : un avertissement court** (nom de la carte, type de l'erreur) et le pilote du
  navigateur tu sous ERROR pendant l'essai. *Écarté* : le texte complet de l'erreur, qui
  affichait dans le notebook l'emplacement du navigateur sur le poste et un message en anglais ;
  et ``logging.disable``, qui tairait aussi les journaux du projet.

OÙ
---
- **Place** : dernière étape du pipeline (kayak/pipeline.py:199) et section 6 du notebook.
  **Appelants** : ``kayak/pipeline.py``, ``Kayak_Bloc1_CDSD_source.py``.
- **Lit** les tables Parquet de la zone curated, déjà chargées en pandas par l'appelant ;
  **écrit** dans ``sorties/`` (HTML interactif et PNG), dossier fixé par rapport à
  ``kayak.config.RACINE``.
- **Dépendances** : Plotly (fond « open-street-map »), pandas, NumPy, Kaleido et un navigateur
  Chromium pour le PNG, ``kayak.config``.

COMMENT
-------
- ``cadrage`` rend centre et zoom qui englobent les points ; ``carte_destinations`` dessine une
  pastille par destination ; ``selectionner_top_hotels`` classe et numérote ; ``carte_hotels``
  assemble cartes et tableaux ; ``enregistrer`` écrit le HTML (avec la mention de droits en
  commentaire) et tente le PNG.
- **Preuves directes** : le cadrage englobe les points (tests/test_cartes.py:62), des valeurs
  calculées à la main (tests/test_cartes_inverses.py:274) ; les pastilles numérotées
  (tests/test_cartes.py:84) ; le classement (tests/test_cartes.py:131) ; la carte du top 20
  (tests/test_cartes.py:164).
- **Refus** (tests inverses) : une latitude hors domaine est refusée
  (tests/test_cartes_inverses.py:386) ; des hôtels absents aussi
  (tests/test_cartes_inverses.py:963) ; le tableau d'entrée n'est jamais modifié
  (tests/test_cartes_inverses.py:404) ; deux enregistrements successifs laissent le fichier
  inchangé (tests/test_cartes_inverses.py:1717) ; l'avertissement d'un export PNG en échec ne
  reprend pas le texte de l'erreur (tests/test_cartes_inverses.py:1654) ;
  le pilote du navigateur est tu sous ERROR pendant l'export, puis retrouve son niveau
  (tests/test_cartes_inverses.py:1683).

POURQUOI
--------
- Mercator et taille du monde en pixels d'une carte web (512 × 2^zoom) :
  https://en.wikipedia.org/wiki/Web_Mercator_projection (lecture non consignée, à
  revérifier) ; code : kayak/cartes.py:133.
- L'attribution « © contributeurs OpenStreetMap » est affichée par le fond de carte ; la licence
  ODbL des données est rappelée dans le README (section « Conformité de la collecte »).
- Les défauts corrigés (zoom trop large, numéros masqués, légende qui cache les pastilles) ont été
  constatés à l'œil sur les images générées, comme le dit l'en-tête de tests/test_cartes.py ; ils
  ne sont donc prouvés que par les tests de structure de la figure, pas par un test d'image.

JOURNALISATION
--------------
- Logger ``kayak.cartes``, quand Kaleido échoue : « Export PNG impossible pour <nom> (<type de
  l'erreur>) : seul le HTML est écrit », au niveau WARNING (kayak/cartes.py:629), puis le texte
  complet de l'erreur au niveau DEBUG (kayak/cartes.py:634) ; le chemin du PNG est retiré du
  résultat. Le WARNING ne reprend pas ce texte : celui de Kaleido cite l'emplacement du
  navigateur sur le poste, et le notebook l'afficherait.
- Logger ``choreographer`` (pilote du navigateur de Kaleido) : pendant l'essai d'export, seuls
  ses messages ERROR et au-delà passent ; son niveau d'avant lui est rendu ensuite, même après
  une erreur (kayak/cartes.py:636).
- Aucun message INFO.
"""

import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go  # type: ignore[import-untyped]
from plotly.subplots import make_subplots  # type: ignore[import-untyped]

from kayak import config

logger = logging.getLogger(__name__)

# Journal du pilote de navigateur de Kaleido (Choreographer). Quand le navigateur ne démarre pas,
# il écrit en anglais « Wait expired, Browser is being closed by watchdog. », qui double
# l'avertissement d'``enregistrer`` : ses messages sous le niveau ERROR sont tus pendant l'essai.
JOURNAL_NAVIGATEUR = "choreographer"

MENTION_DROITS = "Copyright © 2026 · voir le fichier LICENSE du dépôt"

# Tailles des images exportées (pixels) : le cadrage de chaque carte en dépend, car le niveau
# de zoom qui « tient » dans une carte dépend de sa taille en pixels.
LARGEUR_DESTINATIONS, HAUTEUR_DESTINATIONS = 1100, 750
LARGEUR_HOTELS, HAUTEUR_HOTELS = 1600, 1050

# Une couleur par destination (de la 1re à la 5e), assez sombres pour qu'un chiffre blanc y reste
# lisible, et distinguables entre elles (bleu, rouge, vert, violet, ocre).
COULEURS = ["#1b6ca8", "#b03a2e", "#1e8449", "#7d3c98", "#9c640c"]

# Fond OpenStreetMap : l'attribution « © contributeurs OpenStreetMap » est affichée par la carte.
FOND_DE_CARTE = "open-street-map"
MARGE_LEGENDE = 135  # hauteur (pixels) réservée sous la carte des destinations pour sa légende
# Nombre de destinations retenues par l'énoncé (« top 5 ») : une seule valeur pour la sélection
# des hôtels et pour la carte des hôtels, qui doivent porter sur les mêmes destinations.
NB_DESTINATIONS = 5


def _mercator_y(latitude: float) -> float:
    """Ordonnée de projection de Mercator (en radians) : les cartes web sont en Mercator."""
    return math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2))


def cadrage(
    points: pd.DataFrame,
    largeur_px: float = LARGEUR_DESTINATIONS,
    hauteur_px: float = HAUTEUR_DESTINATIONS,
    marge: float = 0.12,
    zoom_max: float = 15.0,
) -> dict[str, Any]:
    """Centre et niveau de zoom qui englobent exactement les points, avec une marge.

    Sans centre explicite, l'export statique place la vue en (0, 0), au large de l'Afrique.
    Le zoom est calculé en pixels : sur une carte web, à un zoom z le monde entier (2π radians de
    longitude) mesure 512 × 2^z pixels ; on prend le plus grand zoom pour lequel l'étendue des
    points, en projection de Mercator, tient dans la surface utile (taille de la carte moins la
    marge de chaque côté). L'ancien calcul « log2(360 / étendue) − 1 » laissait deux niveaux de
    zoom de trop : la vue montrait la moitié de l'Europe pour cinq villes du Sud.

    ``marge`` : part de chaque côté laissée vide (0,12 = 12 %). ``zoom_max`` empêche un zoom
    absurde quand tous les points sont confondus.
    """
    lat_min, lat_max = float(points["latitude"].min()), float(points["latitude"].max())
    lon_min, lon_max = float(points["longitude"].min()), float(points["longitude"].max())
    y_min, y_max = _mercator_y(lat_min), _mercator_y(lat_max)
    # Étendue minimale (≈ 450 m) : un point isolé ne doit pas donner un zoom infini.
    plancher = math.radians(0.004)
    etendue_x = max(math.radians(lon_max - lon_min), plancher)
    etendue_y = max(y_max - y_min, plancher)
    utile_x = largeur_px * (1 - 2 * marge)
    utile_y = hauteur_px * (1 - 2 * marge)
    zoom = min(
        math.log2(utile_x * 2 * math.pi / (512 * etendue_x)),
        math.log2(utile_y * 2 * math.pi / (512 * etendue_y)),
    )
    # Latitude du milieu en Mercator, reconvertie en degrés.
    lat_centre = math.degrees(2 * math.atan(math.exp((y_min + y_max) / 2)) - math.pi / 2)
    return {
        "center": {"lat": lat_centre, "lon": (lon_min + lon_max) / 2},
        "zoom": float(np.clip(zoom, 3, zoom_max)),
    }


def carte_destinations(
    villes_df: pd.DataFrame,
    top: int = 5,
    largeur: int = LARGEUR_DESTINATIONS,
    hauteur: int = HAUTEUR_DESTINATIONS,
) -> go.Figure:
    """Crée une carte des meilleures destinations selon le score météo.

    Paramètres
    ----------
    villes_df : pandas.DataFrame
        Doit contenir les colonnes : ville, latitude, longitude, score_meteo, rang_meteo,
        temp_max_moy_c, pluie_totale_mm, ensoleillement_moy_h.
    top : int
        Nombre de destinations à afficher (par défaut 5).
    largeur, hauteur : int
        Taille de l'image en pixels (sert aussi à cadrer la vue).

    Retourne
    --------
    plotly.graph_objects.Figure
    """
    # Les destinations du palmarès, de la 1re à la dernière : l'ordre fixe aussi la couleur.
    top_villes = villes_df.nsmallest(top, "rang_meteo").sort_values("rang_meteo")

    fig = go.Figure()
    for position, (_, v) in enumerate(top_villes.iterrows()):
        rang = int(v["rang_meteo"])
        couleur = COULEURS[position % len(COULEURS)]
        detail = (
            f"{v['temp_max_moy_c']:.1f} °C · {v['pluie_totale_mm']:.1f} mm de pluie · "
            f"{v['ensoleillement_moy_h']:.1f} h de soleil/jour"
        )
        # Pastille : le rang est écrit AU CENTRE du disque, en blanc. Sa lisibilité ne dépend
        # d'aucune place libre alentour, contrairement à une étiquette posée à côté du point.
        # On n'ajoute pas, collé à la pastille, un second texte (le nom de la ville) : deux
        # textes qui se chevauchent sont mis en concurrence par la carte, qui en masque un, et
        # c'était le chiffre. Le nom figure dans la légende, toujours affichée.
        fig.add_trace(
            go.Scattermap(
                lat=[v["latitude"]],
                lon=[v["longitude"]],
                mode="markers+text",
                marker={"size": 38, "color": couleur},
                text=[str(rang)],
                textfont={"size": 22, "color": "white"},
                textposition="middle center",
                name=f"{rang}. {v['ville']} — score {v['score_meteo']:.2f} ({detail})",
                hovertemplate=f"<b>{rang}. {v['ville']}</b><br>score {v['score_meteo']:.2f}"
                f"<br>{detail}<extra></extra>",
            )
        )
    fig.update_layout(
        title={"text": f"Top {top} destinations selon le score météo", "x": 0.5},
        width=largeur,
        height=hauteur,
        margin={"r": 0, "t": 50, "l": 0, "b": MARGE_LEGENDE},
        map={
            "style": FOND_DE_CARTE,
            **cadrage(top_villes, largeur, hauteur - 50 - MARGE_LEGENDE),
        },
        # Légende SOUS la carte, dans la marge : posée sur la carte, elle masquait des
        # pastilles (les destinations du nord) dès que le palmarès changeait de région.
        legend={
            "x": 0.01,
            "y": 0,
            "yanchor": "top",
            "bgcolor": "rgba(255,255,255,0)",
            "font": {"size": 14},
        },
    )
    return fig


def ecarter_marqueurs(
    points: pd.DataFrame,
    cadre: dict[str, Any],
    diametre_px: float = 28.0,
    iterations: int = 80,
) -> pd.DataFrame:
    """Éloigne les pastilles qui se chevauchent, pour que chaque numéro reste lisible.

    Plusieurs hôtels d'un même quartier tombent à quelques dizaines de mètres les uns des autres
    (4 hôtels de La Grande-Motte, 2 pastilles de Toulouse...) : à l'échelle de la carte leurs
    pastilles se recouvrent et seul le dernier numéro se lit. On travaille en pixels, dans la
    projection de la carte : tant que deux pastilles sont plus proches que leur diamètre, on les
    repousse de moitié du recouvrement chacune, le long de la droite qui les relie (dans une
    direction propre à chacune si elles sont exactement confondues). Les positions décalées sont
    ajoutées dans ``lat_affichee`` et ``lon_affichee`` ; les vraies restent dans ``latitude`` et
    ``longitude``, et la carte relie chaque pastille déplacée à sa vraie position par un trait.
    """
    echelle = 512 * 2 ** cadre["zoom"] / (2 * math.pi)  # pixels par radian de Mercator
    lon0 = math.radians(cadre["center"]["lon"])
    y0 = _mercator_y(cadre["center"]["lat"])
    xs = [(math.radians(lon) - lon0) * echelle for lon in points["longitude"]]
    ys = [(_mercator_y(lat) - y0) * echelle for lat in points["latitude"]]
    for _ in range(iterations):
        deplace = False
        for i in range(len(xs)):
            for j in range(i + 1, len(xs)):
                dx, dy = xs[j] - xs[i], ys[j] - ys[i]
                distance = math.hypot(dx, dy)
                if distance >= diametre_px:
                    continue
                if distance < 1e-6:  # pastilles confondues : une direction propre à chacune
                    angle = 2 * math.pi * (i + 1) / (len(xs) + 1)
                    dx, dy, distance = math.cos(angle), math.sin(angle), 1.0
                recul = (diametre_px - distance) / 2 / distance
                xs[i], ys[i] = xs[i] - dx * recul, ys[i] - dy * recul
                xs[j], ys[j] = xs[j] + dx * recul, ys[j] + dy * recul
                deplace = True
        if not deplace:
            break
    resultat = points.copy()
    resultat["lon_affichee"] = [math.degrees(lon0 + x / echelle) for x in xs]
    resultat["lat_affichee"] = [
        math.degrees(2 * math.atan(math.exp(y0 + y / echelle)) - math.pi / 2) for y in ys
    ]
    return resultat


def _priorite_commune(hors_commune: pd.Series) -> pd.Series:
    """0 : hôtel de la commune de la destination OU de commune inconnue, 1 : autre commune.

    Seuls les hôtels dont OpenStreetMap indique CLAIREMENT une autre commune reculent : la
    commune n'est renseignée que pour environ un hôtel sur cinq, et pénaliser les autres
    reviendrait à classer d'après la qualité de la saisie plutôt que celle des hôtels.
    """
    return hors_commune.map(lambda v: 0 if pd.isna(v) else int(bool(v))).astype(int)


def selectionner_top_hotels(
    hotels_df: pd.DataFrame,
    villes_df: pd.DataFrame,
    top: int = 20,
    privilegier_commune: bool = False,
) -> pd.DataFrame:
    """Les ``top`` meilleurs hôtels des 5 meilleures destinations, classés.

    Ordre de classement : note des clients (Booking), puis nombre d'étoiles (seul critère de
    qualité disponible avec OpenStreetMap), puis nombre d'avis, puis PROXIMITÉ DU CENTRE de la
    destination (à égalité, l'hôtel le plus proche passe devant ; sans ce critère, les nombreux
    4 étoiles ex æquo seraient départagés au hasard), puis nom et identifiant pour que le
    résultat soit toujours le même. Les valeurs manquantes passent en dernier.

    ``privilegier_commune=True`` fait reculer, derrière tous les autres, les hôtels dont
    OpenStreetMap indique une autre commune que celle de la destination (La Grande-Motte et Le
    Grau-du-Roi pour Aigues-Mortes, par exemple) ; les hôtels de la commune et ceux dont la
    commune est inconnue restent classés entre eux selon les critères ci-dessus. Par défaut, le
    classement porte sur l'aire entière de 8 km : c'est la définition de « hôtels d'une
    destination » retenue par le projet (voir le README).
    """
    ids_top5 = villes_df.nsmallest(NB_DESTINATIONS, "rang_meteo")["id_ville"].tolist()
    hotels = hotels_df[hotels_df["id_ville"].isin(ids_top5)].copy()
    for colonne in ("note", "etoiles", "nb_avis", "distance_km", "hors_commune"):
        if colonne not in hotels:
            hotels[colonne] = np.nan  # source sans cette information (ex. Booking sans distance)

    cles = ["note", "etoiles", "nb_avis", "distance_km", "nom", "hotel_id"]
    sens = [False, False, False, True, True, True]  # False = décroissant, True = croissant
    if privilegier_commune:
        hotels["priorite_commune"] = _priorite_commune(hotels["hors_commune"])
        cles, sens = ["priorite_commune", *cles], [True, *sens]
    hotels = hotels.sort_values(by=cles, ascending=sens, na_position="last").head(top).copy()
    hotels["rang_hotel"] = range(1, len(hotels) + 1)
    # Indice de qualité sur 10 : la note des clients quand elle existe (Booking), sinon les
    # étoiles × 2 (OpenStreetMap n'a pas de note), sinon une valeur neutre de 5.
    hotels["indice_qualite"] = hotels["note"].fillna(hotels["etoiles"] * 2).fillna(5).astype(float)
    return hotels.reset_index(drop=True)


def _court(texte: str, longueur: int = 30) -> str:
    """Nom raccourci avec « … » pour qu'il tienne dans une cellule de tableau."""
    return texte if len(texte) <= longueur else texte[: longueur - 1].rstrip() + "…"


def carte_hotels(
    hotels_df: pd.DataFrame,
    villes_df: pd.DataFrame,
    top: int = 20,
    privilegier_commune: bool = False,
    note_echantillon: str | None = None,
    largeur: int = LARGEUR_HOTELS,
    hauteur: int = HAUTEUR_HOTELS,
) -> go.Figure:
    """Crée la carte des meilleurs hôtels des 5 meilleures destinations.

    Une carte resserrée par destination (rangée du haut) et, dessous, deux tableaux qui listent
    les hôtels numérotés avec leur étoiles, leur distance au centre et leur commune. Le centre de
    chaque destination est marqué d'un point noir : on voit la distance et les hôtels qui
    sortent de la commune.

    Paramètres
    ----------
    hotels_df : pandas.DataFrame
        Doit contenir : hotel_id, id_ville, nom, latitude, longitude, ville, et si possible
        note, nb_avis, etoiles, distance_km, commune, hors_commune.
    villes_df : pandas.DataFrame
        Doit contenir : id_ville, rang_meteo, ville, latitude, longitude.
    top : int
        Nombre d'hôtels à afficher (par défaut 20).
    privilegier_commune : bool
        Voir ``selectionner_top_hotels``.
    note_echantillon : str | None
        Mention écrite sous le titre quand l'échantillon d'hôtels est limité (ex. « 20 meilleurs
        parmi au plus 40 hôtels OSM par ville »).

    Retourne
    --------
    plotly.graph_objects.Figure
    """
    hotels_top = selectionner_top_hotels(hotels_df, villes_df, top, privilegier_commune)
    # Destinations qui ont au moins un hôtel dans le top, dans l'ordre du palmarès météo.
    classement = villes_df.nsmallest(NB_DESTINATIONS, "rang_meteo")
    destinations = [
        (position, v)
        for position, (_, v) in enumerate(classement.iterrows())
        if (hotels_top["id_ville"] == v["id_ville"]).any()
    ]
    n = max(len(destinations), 1)
    # Grille : une colonne double par carte (2 × n colonnes), puis deux tableaux qui se
    # partagent la rangée du bas (n colonnes chacun).
    colonnes = 2 * n
    specs = [
        [{"type": "map", "colspan": 2}, None] * n,
        [{"type": "table", "colspan": n}, *([None] * (n - 1)), {"type": "table", "colspan": n}]
        + [None] * (n - 1),
    ]
    titres = [
        f"{v['rang_meteo']:.0f}. {v['ville']} — {(hotels_top['id_ville'] == v['id_ville']).sum()} "
        "hôtel(s)"
        for _, v in destinations
    ]
    fig = make_subplots(
        rows=2,
        cols=colonnes,
        specs=specs,
        subplot_titles=titres,
        row_heights=[0.6, 0.4],
        vertical_spacing=0.06,
        horizontal_spacing=0.008,
    )
    marge = {"t": 140, "r": 10, "b": 10, "l": 10}
    hauteur_utile = hauteur - marge["t"] - marge["b"]
    largeur_utile = largeur - marge["l"] - marge["r"]

    for colonne, (position, v) in enumerate(destinations):
        couleur = COULEURS[position % len(COULEURS)]
        hotels_ville = hotels_top[hotels_top["id_ville"] == v["id_ville"]]
        nom_carte = "map" if colonne == 0 else f"map{colonne + 1}"
        # Largeur et hauteur réelles de ce panneau, lues dans le domaine que Plotly lui a donné.
        domaine = fig.layout[nom_carte].domain
        largeur_panneau = largeur_utile * (domaine.x[1] - domaine.x[0])
        hauteur_panneau = hauteur_utile * (domaine.y[1] - domaine.y[0])
        # Les points à cadrer : les hôtels ET le centre de la destination.
        points = pd.concat(
            [
                hotels_ville[["latitude", "longitude"]],
                pd.DataFrame({"latitude": [v["latitude"]], "longitude": [v["longitude"]]}),
            ]
        )
        cadre = cadrage(points, largeur_panneau, hauteur_panneau, marge=0.16)
        hotels_ville = ecarter_marqueurs(hotels_ville, cadre)
        col = 2 * colonne + 1

        # Traits fins entre chaque pastille déplacée et la vraie position de l'hôtel.
        segments_lat, segments_lon = [], []
        for _, h in hotels_ville.iterrows():
            segments_lat += [h["latitude"], h["lat_affichee"], None]
            segments_lon += [h["longitude"], h["lon_affichee"], None]
        fig.add_trace(
            go.Scattermap(
                lat=segments_lat,
                lon=segments_lon,
                mode="lines",
                line={"width": 2, "color": couleur},
                hoverinfo="skip",
                showlegend=False,
            ),
            row=1,
            col=col,
        )
        # Vraie position de chaque hôtel : un petit point de la couleur de la destination.
        fig.add_trace(
            go.Scattermap(
                lat=hotels_ville["latitude"],
                lon=hotels_ville["longitude"],
                mode="markers",
                marker={"size": 6, "color": couleur},
                hoverinfo="skip",
                showlegend=False,
            ),
            row=1,
            col=col,
        )
        # Centre de la destination : un point noir.
        fig.add_trace(
            go.Scattermap(
                lat=[v["latitude"]],
                lon=[v["longitude"]],
                mode="markers",
                marker={"size": 11, "color": "black"},
                hovertemplate=f"Centre de la destination : {v['ville']}<extra></extra>",
                showlegend=False,
            ),
            row=1,
            col=col,
        )
        # Pastilles numérotées (rang de l'hôtel dans le top), à leur position affichée.
        fig.add_trace(
            go.Scattermap(
                lat=hotels_ville["lat_affichee"],
                lon=hotels_ville["lon_affichee"],
                mode="markers+text",
                marker={"size": 26, "color": couleur},
                text=hotels_ville["rang_hotel"].astype(str),
                textfont={"size": 14, "color": "white"},
                textposition="middle center",
                customdata=np.stack(
                    [
                        hotels_ville["nom"],
                        hotels_ville["distance_km"].round(1).astype(str),
                        hotels_ville["etoiles"].map(lambda e: "?" if pd.isna(e) else f"{e:.0f}"),
                    ],
                    axis=-1,
                ),
                hovertemplate="<b>%{text}. %{customdata[0]}</b><br>étoiles : %{customdata[2]}"
                "<br>à %{customdata[1]} km du centre<extra></extra>",
                showlegend=False,
            ),
            row=1,
            col=col,
        )
        fig.update_layout({nom_carte: {"style": FOND_DE_CARTE, **cadre}})

    # Deux tableaux : les hôtels 1 à 10, puis 11 à 20 (la moitié du top s'il est plus court).
    moitie = math.ceil(len(hotels_top) / 2)
    couleurs_destinations = {
        v["id_ville"]: COULEURS[position % len(COULEURS)] for position, v in destinations
    }
    for numero, bloc in enumerate((hotels_top.iloc[:moitie], hotels_top.iloc[moitie:])):
        if bloc.empty:
            continue
        communes = [
            "?" if pd.isna(c) else _court(str(c), 18)
            for c in bloc.get("commune", [None] * len(bloc))
        ]
        # Texte rouge quand la commune de l'hôtel n'est pas celle de la destination (la valeur
        # peut être un booléen Python ou NumPy, ou une valeur manquante).
        couleur_commune = [
            "#b03a2e" if (not pd.isna(h) and bool(h)) else "#333333" for h in bloc["hors_commune"]
        ]
        fig.add_trace(
            go.Table(
                columnwidth=[0.06, 0.34, 0.2, 0.07, 0.1, 0.23],
                header={
                    "values": ["N°", "Hôtel", "Destination", "★", "km", "Commune OSM"],
                    "fill_color": "#1f4e79",
                    "font": {"color": "white", "size": 13},
                    "align": "left",
                },
                cells={
                    "values": [
                        bloc["rang_hotel"].astype(str),
                        bloc["nom"].map(_court),
                        bloc["ville"],
                        bloc["etoiles"].map(lambda e: "—" if pd.isna(e) else f"{e:.0f}"),
                        bloc["distance_km"].map(lambda d: "—" if pd.isna(d) else f"{d:.1f}"),
                        communes,
                    ],
                    "fill_color": [
                        [couleurs_destinations[i] for i in bloc["id_ville"]],
                        *[["#f5f7fa"] * len(bloc)] * 5,
                    ],
                    "font": {
                        "size": 13,
                        "color": [
                            ["white"] * len(bloc),
                            *[["#222222"] * len(bloc)] * 4,
                            couleur_commune,
                        ],
                    },
                    "align": "left",
                    "height": 28,
                },
            ),
            row=2,
            col=1 if numero == 0 else n + 1,
        )

    # Sous-titre sur deux lignes : l'échantillon (en gras, car il conditionne la lecture du
    # classement), puis la légende des symboles.
    lignes = [
        "aire de destination (8 km autour du centre) · classement : étoiles, puis proximité "
        "du centre",
        "point noir = centre · petit point = vraie position (pastilles écartées) · "
        "rouge = commune différente de la destination · ? = commune non renseignée",
    ]
    if note_echantillon:
        lignes.insert(0, f"<b>{note_echantillon}</b>")
    fig.update_layout(
        title={
            "text": f"Top {top} hôtels des 5 meilleures destinations",
            "x": 0.5,
            "y": 0.985,
            "yanchor": "top",
        },
        width=largeur,
        height=hauteur,
        margin=marge,
        showlegend=False,
    )
    # Le sous-titre est une annotation posée au-dessus des titres de panneaux : le balisage
    # « <sup> » d'un titre multiligne se chevauche avec eux.
    fig.add_annotation(
        x=0.5,
        y=1 + 40 / hauteur_utile,
        xref="paper",
        yref="paper",
        yanchor="bottom",
        text="<br>".join(lignes),
        showarrow=False,
        font={"size": 13, "color": "#333333"},
        align="center",
    )
    return fig


def enregistrer(fig: go.Figure, nom: str) -> dict[str, Path]:
    """Enregistre la carte en HTML interactif et tente un export PNG ; renvoie les chemins.

    Le dossier est fixé par rapport à la racine du projet (et non au dossier courant), pour que
    les cartes arrivent au même endroit depuis le notebook comme depuis la ligne de commande.
    L'export PNG (kaleido) peut échouer sans navigateur disponible : ce n'est pas bloquant. Sa
    taille est celle de la figure (largeur et hauteur fixées à sa création), pour que le cadrage
    calculé corresponde à l'image.
    """
    dossier = config.RACINE / "sorties"
    dossier.mkdir(exist_ok=True)
    chemins = {"html": dossier / f"{nom}.html"}
    # Le HTML interactif porte la mention de droits en commentaire, avant la balise <html>.
    page = fig.to_html(include_plotlyjs="cdn")
    contenu = f"<!-- {MENTION_DROITS} -->\n{page}".replace("\r\n", "\n")
    with open(chemins["html"], "w", encoding="utf-8", newline="\n") as fichier:
        fichier.write(contenu)  # fins de ligne « \n » sur tous les systèmes
    navigateur = logging.getLogger(JOURNAL_NAVIGATEUR)
    niveau = navigateur.level
    navigateur.setLevel(logging.ERROR)
    try:
        chemins["png"] = dossier / f"{nom}.png"
        fig.write_image(chemins["png"], scale=2)
    except Exception as erreur:  # export d'image facultatif
        chemins.pop("png")
        # Le message de Kaleido cite l'emplacement du navigateur sur le poste, en anglais : seul
        # le type de l'erreur est donné en WARNING (il apparaît dans le notebook) ; le message
        # complet reste en DEBUG, pour le diagnostic.
        logger.warning(
            "Export PNG impossible pour %s (%s) : seul le HTML est écrit",
            nom,
            type(erreur).__name__,
        )
        logger.debug("Détail de l'échec de l'export PNG : %s", erreur)
    finally:
        navigateur.setLevel(niveau)
    return chemins
