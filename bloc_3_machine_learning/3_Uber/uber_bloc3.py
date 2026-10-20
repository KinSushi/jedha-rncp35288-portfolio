# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Fonctions du notebook Uber Pickups (bloc 3 CDSD), sorties du notebook pour être testées.

CHOIX
-----
- Sortir du notebook les fonctions qui calculent (lecture des données, projection en kilomètres,
  nom d'une zone, hot-zones par MiniBatchKMeans, placement des étiquettes de carte, indicateurs
  de prévision) et les mettre dans ce module, importé par la cellule d'imports. Les tests
  (dossier tests/) les appellent sans exécuter le notebook, donc sans les 100 Mo de données.
- Laisser dans le notebook ce qui se lit et se modifie à l'oral : RANDOM_STATE, K_DAY, la liste
  des repères, le seuil de nom de zone, les réglages de MiniBatchKMeans, la géométrie des cartes,
  la liste des variables de la prévision et toute la mise en page. Un jury peut changer une
  valeur dans la cellule et relancer, sans ouvrir ce fichier.
- Écarté : tout mettre dans le module. Le raisonnement vit dans les cellules Markdown.
- Écarté : garder des variables globales (lat0, lon0, landmarks, SOURCE) lues par les fonctions.
  Chaque fonction reçoit ses données en paramètres : elle se teste seule et ne dépend pas de
  l'ordre d'exécution des cellules.
- Écarté : des valeurs par défaut cachées dans les signatures pour les réglages de
  l'algorithme : fit_hotzones exige batch_size, n_init, max_iter et les tailles d'échantillon,
  qui sont écrits en clair dans la cellule 12 du notebook.
- Écarté : avaler les entrées invalides. Une source inconnue, un fichier absent de l'archive,
  un k trop grand ou une géométrie incohérente lèvent une erreur qui nomme la faute.

OÙ
---
- Place : à côté du notebook, dans le dossier 3_Uber. Appelant unique : le notebook
  Uber_Pickups_Bloc3_CDSD_v2.ipynb (cellules 2 à 32) ; les tests de tests/test_uber_bloc3.py et le
  test d'intégration tests/test_notebook_uber.py l'appellent aussi.
- Dépendances : numpy, pandas, scikit-learn et IPython (affichage), versions épinglées dans
  requirements.txt ; zipfile et hashlib de la bibliothèque standard pour lire les données. Le
  module lit les fichiers de données que le notebook lui désigne ; il n'écrit rien sur disque
  et n'ouvre aucune connexion réseau.
- Les fonctions sont pures : mêmes entrées, mêmes sorties (les graines sont des paramètres).

COMMENT
-------
- choisir_source, lire_octets et empreinte_conforme retrouvent et vérifient les six fichiers
  mensuels, dans l'archive ou dans le dossier extrait. Preuves : tests/test_uber_bloc3.py
  (test_choisir_source_*, test_lire_octets_*, test_empreinte_conforme_*).
- to_xy, from_xy et projeter_en_km convertissent latitude et longitude en kilomètres autour du
  centre de New York ; nearest_zone_name donne le repère le plus proche. Preuves :
  test_to_xy_*, test_from_xy_*, test_projeter_en_km_*, test_nearest_zone_name_*.
- fit_hotzones ajuste MiniBatchKMeans sur un sous-ensemble et rend le tableau des zones (volume,
  part, centroïde, rayon p90, heure de pic, repère, surface, densité), le modèle, les étiquettes
  et la qualité. Preuves : test_fit_hotzones_*.
- distance_au_segment, chercher_position et placer_etiquette placent les étiquettes des cartes sans
  recouvrir un cercle ni une autre étiquette. Preuves : test_distance_au_segment_*,
  test_chercher_position_*, test_placer_etiquette_*.
- recommend_hotzones_from_history, indicateurs, evaluer et qualifier_gain servent la
  recommandation et la comparaison à la règle « comme la semaine précédente ». Preuves : tests du
  même nom.
- Intégration : tests/test_notebook_uber.py exécute le notebook sur les données réelles et
  vérifie ses chiffres clés (4 513 100 lignes conservées, MAE 34,164 contre 36,202).

POURQUOI
--------
- Règle de documentation et de test : une fonction sans test direct ni test inverse n'est pas
  livrée (tests/test_uber_bloc3.py : chaque fonction a les deux).
- Les six empreintes SHA-256 des fichiers de 2014 viennent du dépôt public FiveThirtyEight :
  https://github.com/fivethirtyeight/uber-tlc-foil-response ; la comparaison est faite par le
  notebook (voir LIRE_DONNEES.md pour la vérification de taille et d'empreinte Git).
- Projection locale en kilomètres : 111,320 km par degré de longitude à l'équateur et 110,574 km
  par degré de latitude, valeurs d'usage courant pour une carte à l'échelle d'une ville ;
  source : inconnue, non rejouée sur une référence géodésique dans ce dossier.
- Le seuil de 3 km au-delà duquel un nom de zone est signalé comme approximatif est un choix du
  notebook, sans source externe : source : inconnue.
- Référentiel RNCP35288, bloc 3 : https://www.francecompetences.fr/recherche/rncp/35288/

JOURNALISATION
--------------
- Ce module ne journalise rien : ce sont des fonctions pures, appelées dans un notebook dont les
  affichages (print et tableaux) font office de journal.
- Les données sont des géolocalisations horodatées : une donnée personnelle potentielle (section
  11 bis du notebook). Le module ne conserve ni n'écrit aucune coordonnée individuelle ; il ne
  rend que des agrégats (zones, volumes, indicateurs).
"""

from __future__ import annotations

import hashlib
import math
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from IPython.display import Markdown, display
from sklearn.cluster import MiniBatchKMeans  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    calinski_harabasz_score,
    davies_bouldin_score,
    r2_score,
    silhouette_score,
)

__all__ = [
    "KM_PAR_DEGRE_LATITUDE",
    "KM_PAR_DEGRE_LONGITUDE_EQUATEUR",
    "GeometrieCarte",
    "afficher",
    "afficher_markdown",
    "choisir_source",
    "chercher_position",
    "distance_au_segment",
    "empreinte_conforme",
    "evaluer",
    "fit_hotzones",
    "from_xy",
    "indicateurs",
    "lire_octets",
    "nearest_zone_name",
    "placer_etiquette",
    "projeter_en_km",
    "qualifier_gain",
    "recommend_hotzones_from_history",
    "to_xy",
    "trouver_racine",
]

#: Kilomètres par degré de latitude, et par degré de longitude à l'équateur (projection locale).
KM_PAR_DEGRE_LATITUDE = 110.574
KM_PAR_DEGRE_LONGITUDE_EQUATEUR = 111.320


# --------------------------------------------------------------------------------------------
# Affichage dans le notebook
# --------------------------------------------------------------------------------------------


def afficher(*objets: object) -> None:
    """Affiche des objets dans le notebook (enveloppe typée de IPython.display.display).

    Pourquoi : `display` n'a pas d'annotations, et mypy --strict refuse de l'appeler depuis du
    code typé. Cette enveloppe n'ajoute aucun comportement.
    """
    display(*objets)  # type: ignore[no-untyped-call]


def afficher_markdown(texte: str) -> None:
    """Affiche un texte Markdown calculé (enveloppe typée de display(Markdown(texte)))."""
    display(Markdown(texte))  # type: ignore[no-untyped-call]


# --------------------------------------------------------------------------------------------
# Dossier du projet, source des données
# --------------------------------------------------------------------------------------------


def trouver_racine(
    nom_notebook: str, sous_dossier: str, depart: Path | None = None
) -> Path:
    """Dossier du projet : celui qui contient le notebook (ou son sous-dossier `sous_dossier`).

    On remonte depuis `depart` (par défaut le dossier courant) jusqu'au premier dossier qui
    contient le notebook, directement ou dans `sous_dossier`. À défaut, on garde `depart`.
    Le noyau Jupyter ne démarre pas toujours dans le dossier du notebook, d'où cette recherche.
    Lève ValueError si `nom_notebook` est vide.
    """
    if not nom_notebook:
        raise ValueError("nom_notebook est vide : tout dossier « contiendrait » ce notebook")
    point_depart = (depart if depart is not None else Path.cwd()).resolve()
    for dossier in (point_depart, *point_depart.parents):
        for candidat in (dossier, dossier / sous_dossier):
            if (candidat / nom_notebook).exists():
                return candidat
    return point_depart


def choisir_source(archive_env: str | None, racine: Path) -> tuple[str, Path, Path]:
    """Choisit la source des données ; rend (source, chemin de l'archive, dossier extrait).

    Trois sources possibles, par ordre de priorité (voir LIRE_DONNEES.md) :
    1. l'archive désignée par `archive_env` (la variable UBER_DATA_ZIP), où qu'elle se trouve ;
    2. un dossier « uber-trip-data/ » déjà extrait, à côté du notebook ;
    3. l'archive « uber-trip-data.zip » à côté du notebook.
    `source` vaut « archive » ou « dossier ». Lève FileNotFoundError si aucune n'existe.
    """
    data_zip = Path(archive_env) if archive_env else racine / "uber-trip-data.zip"
    data_dir = racine / "uber-trip-data"
    if archive_env and data_zip.exists():
        return "archive", data_zip, data_dir
    if data_dir.exists():
        return "dossier", data_zip, data_dir
    if data_zip.exists():
        return "archive", data_zip, data_dir
    raise FileNotFoundError(
        "Données introuvables : voir LIRE_DONNEES.md (source publique, archive "
        "« uber-trip-data.zip » à placer à côté du notebook ou variable UBER_DATA_ZIP)."
    )


def lire_octets(mois: str, source: str, data_dir: Path, data_zip: Path) -> bytes:
    """Contenu brut du fichier d'un mois (« apr », « may », ...), depuis l'archive ou le dossier.

    Dans l'archive, on écarte les entrées « __MACOSX » (métadonnées ajoutées par certains outils
    de compression) : seule l'entrée exacte « .../nom » est lue. Rien n'est extrait sur disque.
    Lève ValueError si `source` n'est pas « dossier » ou « archive », et FileNotFoundError si
    l'archive ne contient pas le fichier.
    """
    nom = f"uber-raw-data-{mois}14.csv"
    if source == "dossier":
        return (data_dir / nom).read_bytes()
    if source != "archive":
        raise ValueError(f"source inconnue : {source!r} (attendu « dossier » ou « archive »)")
    with zipfile.ZipFile(data_zip) as archive:
        entree = next((n for n in archive.namelist() if n.endswith("/" + nom)), None)
        if entree is None:
            raise FileNotFoundError(f"{nom} est absent de l'archive {data_zip.name}")
        return archive.read(entree)


def empreinte_conforme(octets: bytes, attendue: str) -> bool:
    """Vrai si l'empreinte SHA-256 des octets est celle attendue (64 caractères hexadécimaux)."""
    return hashlib.sha256(octets).hexdigest() == attendue


# --------------------------------------------------------------------------------------------
# Projection locale en kilomètres, nom de zone
# --------------------------------------------------------------------------------------------


def to_xy(lat: Any, lon: Any, lat0: float, lon0: float) -> tuple[Any, Any]:
    """Projection locale (kilomètres) d'un point latitude/longitude autour de (lat0, lon0)."""
    x = (lon - lon0) * KM_PAR_DEGRE_LONGITUDE_EQUATEUR * math.cos(math.radians(lat0))
    y = (lat - lat0) * KM_PAR_DEGRE_LATITUDE
    return x, y


def from_xy(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Opération inverse de to_xy : kilomètres → (latitude, longitude)."""
    lat = y / KM_PAR_DEGRE_LATITUDE + lat0
    lon = x / (KM_PAR_DEGRE_LONGITUDE_EQUATEUR * math.cos(math.radians(lat0))) + lon0
    return float(lat), float(lon)


def projeter_en_km(coords: Any, lat0: float, lon0: float) -> np.ndarray[Any, Any]:
    """Convertit un tableau [[lat, lon], ...] en [[x_km, y_km], ...].

    Même formule que to_xy, écrite pour fonctionner dans un Pipeline scikit-learn
    (FunctionTransformer) : lat0 et lon0 arrivent par kw_args.
    """
    tableau = np.asarray(coords, dtype="float64")
    x_km, y_km = to_xy(tableau[:, 0], tableau[:, 1], lat0, lon0)
    return np.column_stack([x_km, y_km])


def nearest_zone_name(
    lat: float, lon: float, landmarks: pd.DataFrame, lat0: float, lon0: float
) -> tuple[str, float]:
    """Nom du repère le plus proche d'un point, et distance en kilomètres.

    `landmarks` porte zone_name, x_km et y_km (positions déjà projetées autour de lat0, lon0).
    """
    x, y = to_xy(lat, lon, lat0, lon0)
    dist = np.sqrt(
        (landmarks["x_km"].to_numpy() - x) ** 2 + (landmarks["y_km"].to_numpy() - y) ** 2
    )
    idx = int(np.argmin(dist))
    return str(landmarks.iloc[idx]["zone_name"]), float(dist[idx])


# --------------------------------------------------------------------------------------------
# Hot-zones
# --------------------------------------------------------------------------------------------


def fit_hotzones(
    subset: pd.DataFrame,
    *,
    landmarks: pd.DataFrame,
    lat0: float,
    lon0: float,
    k: int,
    random_state: int,
    seuil_nom_km: float,
    taille_apprentissage: int,
    taille_qualite: int,
    batch_size: int,
    n_init: int,
    max_iter: int,
) -> tuple[pd.DataFrame, MiniBatchKMeans, np.ndarray[Any, Any], dict[str, Any]]:
    """Ajuste MiniBatchKMeans sur un sous-ensemble ; rend (stats, modèle, étiquettes, qualité).

    `subset` porte x_km, y_km (coordonnées projetées, qui seules servent au clustering), Lat,
    Lon et hour. Le modèle est ajusté sur au plus `taille_apprentissage` points tirés sans
    remise, puis prédit sur tous. La qualité (silhouette, Davies-Bouldin, Calinski-Harabasz) est
    mesurée sur au plus `taille_qualite` points. Les noms viennent du repère le plus proche du
    centroïde ; au-delà de `seuil_nom_km`, le libellé le dit (« secteur de ... »).

    Lève ValueError si k n'est pas strictement positif ou si le sous-ensemble a moins de k lignes.
    """
    if k < 1:
        raise ValueError(f"k doit être au moins 1, reçu {k}")
    coords = subset[["x_km", "y_km"]].to_numpy(dtype=np.float32)
    if len(coords) < k:
        raise ValueError("Pas assez de lignes pour le nombre de clusters demandé.")

    rng = np.random.default_rng(random_state)
    fit_n = min(taille_apprentissage, len(coords))
    fit_idx = (
        rng.choice(len(coords), size=fit_n, replace=False)
        if len(coords) > fit_n
        else np.arange(len(coords))
    )

    model = MiniBatchKMeans(
        n_clusters=k,
        random_state=random_state,
        batch_size=batch_size,
        n_init=n_init,
        max_iter=max_iter,
    )
    model.fit(coords[fit_idx])
    labels = model.predict(coords)
    centers = model.cluster_centers_

    work = subset[["Lat", "Lon", "hour"]].copy()
    work["cluster"] = labels
    dist = np.sqrt(((coords - centers[labels]) ** 2).sum(axis=1))
    work["distance_km"] = dist

    rows = []
    for cid, grp in work.groupby("cluster"):
        x, y = centers[int(cid)]
        lat, lon = from_xy(x, y, lat0, lon0)
        zone, zone_dist = nearest_zone_name(lat, lon, landmarks, lat0, lon0)
        peak_hour = int(grp["hour"].value_counts().idxmax())
        rows.append(
            {
                "cluster": int(cid),
                "pickup_count": len(grp),
                "share_pct": 100 * len(grp) / len(work),
                "centroid_lat": lat,
                "centroid_lon": lon,
                "radius_p90_km": float(np.percentile(grp["distance_km"], 90)),
                "peak_hour": peak_hour,
                "zone_name": zone,
                "zone_distance_km": zone_dist,
            }
        )

    stats = pd.DataFrame(rows).sort_values("pickup_count", ascending=False).reset_index(drop=True)
    stats["rank"] = np.arange(1, len(stats) + 1)

    # Libellé honnête : si le repère le plus proche est loin du centroïde, on le dit.
    stats["zone_label"] = np.where(
        stats["zone_distance_km"] <= seuil_nom_km,
        stats["zone_name"],
        "secteur de "
        + stats["zone_name"]
        + " (à "
        + stats["zone_distance_km"].round(1).astype(str)
        + " km)",
    )

    # Surface (disque de rayon p90) et densité : le volume seul mélange taille et intensité.
    stats["area_km2"] = np.pi * stats["radius_p90_km"] ** 2
    stats["density_per_km2"] = stats["pickup_count"] / stats["area_km2"]
    # Rang de densité décroissante, à égalité le premier : on classe l'opposé par ordre croissant.
    stats["rank_density"] = (-stats["density_per_km2"]).rank(method="first").astype(int)

    q_n = min(taille_qualite, len(coords))
    q_idx = (
        rng.choice(len(coords), size=q_n, replace=False)
        if len(coords) > q_n
        else np.arange(len(coords))
    )
    q_coords = coords[q_idx]
    q_labels = labels[q_idx]
    quality = {
        "n_rows": len(coords),
        "k": k,
        "silhouette": float(silhouette_score(q_coords, q_labels)),
        "davies_bouldin": float(davies_bouldin_score(q_coords, q_labels)),
        "calinski_harabasz": float(calinski_harabasz_score(q_coords, q_labels)),
        "inertia": float(model.inertia_),
    }
    return stats, model, labels, quality


def recommend_hotzones_from_history(
    weekday: int,
    hour: int | None,
    top_n: int,
    daily_hotzones: pd.DataFrame,
    fri18_stats: pd.DataFrame,
) -> pd.DataFrame:
    """Retourne les hot-zones déjà calculées, sans réentraîner.

    - Si hour est None : modèle journalier de `daily_hotzones`, adapté au livrable « hot-zones
      par jour » (les `top_n` premières zones par rang).
    - Si weekday vaut 4 et hour 18 : le créneau vendredi 18 h de `fri18_stats`.
    - Pour tout autre créneau, il faut appeler fit_hotzones sur le filtre correspondant : une
      NotImplementedError le dit.
    Lève ValueError si top_n n'est pas strictement positif.
    """
    if top_n < 1:
        raise ValueError(f"top_n doit être au moins 1, reçu {top_n}")
    cols = [
        "rank",
        "zone_name",
        "pickup_count",
        "share_pct",
        "centroid_lat",
        "centroid_lon",
        "radius_p90_km",
    ]
    if hour is None:
        return (
            daily_hotzones[daily_hotzones["weekday"] == weekday]
            .sort_values("rank")[cols]
            .head(top_n)
        )
    if weekday == 4 and hour == 18:
        return fri18_stats[cols].head(top_n)
    raise NotImplementedError(
        "Ce notebook pré-calcule les modèles journaliers et le créneau vendredi 18h. "
        "Pour un autre créneau, utiliser fit_hotzones sur le filtre correspondant."
    )


# --------------------------------------------------------------------------------------------
# Placement des étiquettes des cartes
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GeometrieCarte:
    """Réglages du placement des étiquettes, en fraction de la largeur ou de la hauteur d'une case.

    x_min, x_max, pas_x : zone de recherche horizontale (le bord droit est x_max - largeur).
    y_min, y_max, pas_y : zone de recherche verticale.
    demi_hauteur : demi-hauteur d'une étiquette.
    marge_x, marge_y : marge autour d'une étiquette où aucun cercle ne doit tomber.
    distance_trait : distance minimale entre le trait de rappel et un autre cercle.
    """

    x_min: float
    x_max: float
    pas_x: float
    y_min: float
    y_max: float
    pas_y: float
    demi_hauteur: float
    marge_x: float
    marge_y: float
    distance_trait: float


def distance_au_segment(point: Any, a: Any, b: Any) -> float:
    """Distance d'un point au segment [a, b] (coordonnées en fraction des axes)."""
    a, b, point = np.asarray(a), np.asarray(b), np.asarray(point)
    ab = b - a
    t = 0.0 if not ab.any() else float(np.clip(np.dot(point - a, ab) / np.dot(ab, ab), 0, 1))
    return float(np.linalg.norm(point - (a + t * ab)))


def chercher_position(
    centre: Sequence[float],
    largeur: float,
    occupes: Sequence[Sequence[float]],
    cercles: Sequence[Sequence[float]],
    exiger_trait_libre: bool,
    geo: GeometrieCarte,
) -> tuple[float, float] | None:
    """Position (fraction des axes) de l'étiquette la plus proche de `centre` qui ne recouvre
    aucun cercle de `cercles` ni aucune boîte de `occupes`, et dont le trait de rappel ne
    traverse aucun autre cercle si `exiger_trait_libre` ; None si rien n'est libre."""
    autres = [c for c in cercles if np.hypot(c[0] - centre[0], c[1] - centre[1]) > 1e-9]
    meilleur: tuple[float, float] | None = None
    distance_min = np.inf
    for x in np.arange(geo.x_min, geo.x_max - largeur, geo.pas_x):
        for y in np.arange(geo.y_min, geo.y_max, geo.pas_y):
            boite = (x, y - geo.demi_hauteur, x + largeur, y + geo.demi_hauteur)
            sur_un_cercle = any(
                boite[0] - geo.marge_x <= cx <= boite[2] + geo.marge_x
                and boite[1] - geo.marge_y <= cy <= boite[3] + geo.marge_y
                for cx, cy in cercles
            )
            sur_une_etiquette = any(
                not (boite[2] < b[0] or boite[0] > b[2] or boite[3] < b[1] or boite[1] > b[3])
                for b in occupes
            )
            if sur_un_cercle or sur_une_etiquette:
                continue
            trait_libre = all(
                distance_au_segment(c, (x + largeur / 2, y), centre) > geo.distance_trait
                for c in autres
            )
            if exiger_trait_libre and not trait_libre:
                continue
            distance = np.hypot(x + largeur / 2 - centre[0], y - centre[1])
            if distance < distance_min:
                meilleur, distance_min = (float(x), float(y)), distance
    return meilleur


def placer_etiquette(
    centre: Sequence[float],
    largeur: float,
    occupes: Sequence[Sequence[float]],
    cercles: Sequence[Sequence[float]],
    geo: GeometrieCarte,
) -> tuple[float, float] | None:
    """Cherche d'abord une position dont le trait de rappel ne traverse aucun autre cercle ;
    à défaut, accepte un trait qui en traverse un (l'étiquette ne recouvre jamais un cercle)."""
    for exiger in (True, False):
        position = chercher_position(centre, largeur, occupes, cercles, exiger, geo)
        if position is not None:
            return position
    return None


# --------------------------------------------------------------------------------------------
# Indicateurs de prévision
# --------------------------------------------------------------------------------------------


def indicateurs(y_reel: Any, y_prevu: Any) -> dict[str, float]:
    """MAE (courses/heure), WAPE et biais (en % de la demande réelle totale).

    WAPE = somme des erreurs absolues / somme de la demande. Biais = somme des erreurs signées
    (prévu − réel) / somme de la demande : négatif = sous-estimation. Lève ValueError si les
    longueurs diffèrent ou si la demande totale est nulle (les pourcentages seraient infinis).
    """
    # Une seule règle de conversion pour les deux séries : tout est calculé en double précision.
    reel, prevu = (np.asarray(serie, dtype="float64") for serie in (y_reel, y_prevu))
    if reel.shape != prevu.shape:
        raise ValueError("y_reel et y_prevu doivent avoir la même longueur")
    erreur = prevu - reel
    if reel.sum() == 0:
        raise ValueError("la demande réelle totale est nulle : WAPE et biais indéfinis")
    return {
        "MAE": float(np.abs(erreur).mean()),
        "WAPE": 100 * float(np.abs(erreur).sum() / reel.sum()),
        "biais": 100 * float(erreur.sum() / reel.sum()),
    }


def evaluer(
    nom: str,
    y_reel: Any,
    y_prevu: Any,
    y_app_reel: Any = None,
    y_app_prevu: Any = None,
) -> dict[str, Any]:
    """Une ligne de tableau : R² (part de la variance expliquée, 1 = parfait), MAE, WAPE, biais.

    Si les valeurs d'apprentissage sont données, le R² d'apprentissage est ajouté. Lève
    ValueError si y_app_reel est donné sans y_app_prevu, ou l'inverse.
    """
    if (y_app_reel is None) != (y_app_prevu is None):
        raise ValueError("y_app_reel et y_app_prevu vont ensemble : donner les deux ou aucun")
    ligne: dict[str, Any] = {"modèle": nom, "R² test": r2_score(y_reel, y_prevu)}
    ind = indicateurs(y_reel, y_prevu)
    ligne["MAE test (courses/heure)"] = ind["MAE"]
    ligne["WAPE test (%)"] = ind["WAPE"]
    ligne["biais test (%)"] = ind["biais"]
    if y_app_reel is not None:
        ligne["R² apprentissage"] = r2_score(y_app_reel, y_app_prevu)
    return ligne


def qualifier_gain(gain: float, mae_ref: float) -> str:
    """« modeste » si le gain de MAE est inférieur à 15 % de la MAE de référence, « net » sinon.

    Convention de lecture du notebook (seuil indicatif). Lève ValueError si la référence n'est
    pas strictement positive ou si l'un des nombres est NaN.
    """
    if math.isnan(gain) or math.isnan(mae_ref) or mae_ref <= 0:
        raise ValueError(f"gain ou référence invalide : gain={gain}, mae_ref={mae_ref}")
    return "modeste" if 100 * gain / mae_ref < 15 else "net"
