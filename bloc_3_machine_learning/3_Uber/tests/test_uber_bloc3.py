# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests de uber_bloc3.py : chaque fonction a des tests directs ET des tests inverses.

Un test direct montre ce que la fonction calcule ; un test inverse montre ce qu'elle refuse
(pytest.raises) ou ce qu'elle laisse intact (entrée inchangée). Les données sont minuscules et
construites à la main : les valeurs attendues se vérifient à la calculatrice. Aucun test ne lit
les 100 Mo de données réelles : seul tests/test_notebook_uber.py les utilise.
"""

from __future__ import annotations

import hashlib
import math
import zipfile
from dataclasses import FrozenInstanceError

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score, silhouette_score

import uber_bloc3 as u

LAT0, LON0 = 40.75, -73.98
COS_LAT0 = math.cos(math.radians(LAT0))

# --------------------------------------------------------------------------------------------
# Données de test
# --------------------------------------------------------------------------------------------

REPERES = pd.DataFrame(
    {
        "zone_name": ["Alpha", "Beta", "Gamma"],
        "x_km": [0.5, 10.0, 0.0],
        "y_km": [0.0, 0.0, 14.5],
    }
)
REGLAGES = {
    "k": 3,
    "random_state": 0,
    "seuil_nom_km": 3.0,
    "taille_apprentissage": 50_000,
    "taille_qualite": 2_000,
    "batch_size": 5_000,
    "n_init": 3,
    "max_iter": 80,
}


def jeu_zones() -> pd.DataFrame:
    """600 courses en trois nuages bien séparés (300, 200 et 100) autour de trois centres en km.

    Les heures de pic sont 17 h, 8 h et 22 h ; Lat et Lon sont déduites de x_km et y_km.
    """
    rng = np.random.default_rng(0)
    centres = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
    tailles = [300, 200, 100]
    points = np.vstack(
        [rng.normal(c, 0.3, size=(n, 2)) for c, n in zip(centres, tailles, strict=True)]
    )
    heures = np.repeat([17, 8, 22], tailles)
    # un tiers des courses du dernier nuage à 3 h : l'heure de pic reste 22 h (majorité)
    heures[-30:] = 3
    return pd.DataFrame(
        {
            "x_km": points[:, 0],
            "y_km": points[:, 1],
            "Lat": points[:, 1] / u.KM_PAR_DEGRE_LATITUDE + LAT0,
            "Lon": points[:, 0] / (u.KM_PAR_DEGRE_LONGITUDE_EQUATEUR * COS_LAT0) + LON0,
            "hour": heures,
        }
    )


def ajuster(subset: pd.DataFrame, **modifs):
    """fit_hotzones avec les réglages du notebook, modifiables test par test."""
    reglages = {**REGLAGES, **modifs}
    return u.fit_hotzones(subset, landmarks=REPERES, lat0=LAT0, lon0=LON0, **reglages)


# --------------------------------------------------------------------------------------------
# trouver_racine
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notebook_dans", "depart_dans", "racine_attendue"),
    [
        (".", ".", "."),  # le notebook est dans le dossier de départ
        ("3_Uber", ".", "3_Uber"),  # le noyau démarre dans le dossier parent
        (".", "a/b", "."),  # le noyau démarre plus profond : on remonte
    ],
)
def test_trouver_racine_selon_l_endroit_ou_demarre_le_noyau(
    tmp_path, notebook_dans, depart_dans, racine_attendue
):
    """La racine est le dossier qui contient le notebook, quel que soit le départ du noyau."""
    (tmp_path / notebook_dans).mkdir(parents=True, exist_ok=True)
    (tmp_path / notebook_dans / "nb.ipynb").write_text("{}", encoding="utf-8")
    (tmp_path / depart_dans).mkdir(parents=True, exist_ok=True)
    trouve = u.trouver_racine("nb.ipynb", "3_Uber", depart=tmp_path / depart_dans)
    assert trouve == (tmp_path / racine_attendue).resolve()


def test_trouver_racine_normalise_le_chemin_de_depart(tmp_path):
    """Un départ écrit avec « .. » est ramené à un chemin absolu normalisé (resolve)."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "nb.ipynb").write_text("{}", encoding="utf-8")
    trouve = u.trouver_racine("nb.ipynb", "3_Uber", depart=tmp_path / "a" / "b" / "..")
    assert trouve == (tmp_path / "a").resolve()


def test_trouver_racine_par_defaut_part_du_dossier_courant(tmp_path, monkeypatch):
    """Sans départ explicite, la recherche commence au dossier de travail courant."""
    (tmp_path / "nb.ipynb").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert u.trouver_racine("nb.ipynb", "3_Uber") == tmp_path.resolve()


def test_trouver_racine_sans_notebook_garde_le_depart_et_ne_cree_rien_inchange(tmp_path):
    """Aucun notebook trouvé : on garde le départ, sans créer de fichier (dossier inchangé)."""
    depart = tmp_path / "vide"
    depart.mkdir()
    (depart / "x.txt").write_text("x", encoding="utf-8")
    avant = sorted(p.name for p in depart.iterdir())
    assert u.trouver_racine("introuvable.ipynb", "3_Uber", depart=depart) == depart.resolve()
    assert sorted(p.name for p in depart.iterdir()) == avant


def test_trouver_racine_refuse_un_nom_de_notebook_vide(tmp_path):
    """Un nom vide désignerait n'importe quel dossier : ValueError."""
    with pytest.raises(ValueError) as erreur:
        u.trouver_racine("", "3_Uber", depart=tmp_path)
    assert str(erreur.value) == "nom_notebook est vide : tout dossier « contiendrait » ce notebook"


# --------------------------------------------------------------------------------------------
# choisir_source, lire_octets, empreinte_conforme
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_present", "dossier", "archive_locale", "source_attendue", "zip_attendu"),
    [
        (True, True, True, "archive", "externe.zip"),  # 1. la variable passe avant tout
        (True, True, False, "archive", "externe.zip"),
        (False, True, True, "dossier", "uber-trip-data.zip"),  # 2. le dossier extrait
        (False, True, False, "dossier", "uber-trip-data.zip"),
        (False, False, True, "archive", "uber-trip-data.zip"),  # 3. l'archive locale
    ],
)
def test_choisir_source_respecte_l_ordre_de_priorite(
    tmp_path, env_present, dossier, archive_locale, source_attendue, zip_attendu
):
    """Variable d'environnement, puis dossier extrait, puis archive à côté du notebook."""
    externe = tmp_path / "externe.zip"
    externe.write_bytes(b"x")
    if dossier:
        (tmp_path / "uber-trip-data").mkdir()
    if archive_locale:
        (tmp_path / "uber-trip-data.zip").write_bytes(b"x")
    source, data_zip, data_dir = u.choisir_source(str(externe) if env_present else None, tmp_path)
    assert source == source_attendue
    assert data_zip == tmp_path / zip_attendu
    assert data_dir == tmp_path / "uber-trip-data"


def test_choisir_source_ignore_une_variable_qui_designe_un_fichier_absent(tmp_path):
    """Une variable d'environnement vers un fichier qui n'existe pas n'est pas retenue."""
    (tmp_path / "uber-trip-data").mkdir()
    source, data_zip, _ = u.choisir_source(str(tmp_path / "absent.zip"), tmp_path)
    assert source == "dossier"
    assert data_zip == tmp_path / "absent.zip"


def test_choisir_source_refuse_quand_rien_n_existe(tmp_path):
    """Sans variable, sans dossier ni archive : FileNotFoundError qui renvoie à LIRE_DONNEES.md."""
    message = (
        "Données introuvables : voir LIRE_DONNEES.md (source publique, archive "
        "« uber-trip-data.zip » à placer à côté du notebook ou variable UBER_DATA_ZIP)."
    )
    with pytest.raises(FileNotFoundError) as erreur:
        u.choisir_source(None, tmp_path)
    assert str(erreur.value) == message
    with pytest.raises(FileNotFoundError) as erreur:
        u.choisir_source(str(tmp_path / "absent.zip"), tmp_path)
    assert str(erreur.value) == message


def test_choisir_source_ne_cree_ni_dossier_ni_archive_inchange(tmp_path):
    """Choisir la source ne fait que regarder : le dossier du projet reste tel quel."""
    (tmp_path / "uber-trip-data.zip").write_bytes(b"x")
    avant = sorted(p.name for p in tmp_path.iterdir())
    u.choisir_source(None, tmp_path)
    with pytest.raises(FileNotFoundError):
        u.choisir_source(None, tmp_path / "vide")
    assert sorted(p.name for p in tmp_path.iterdir()) == avant


def fabriquer_archive(chemin, contenu: bytes) -> None:
    """Archive avec le fichier d'avril, une entrée __MACOSX piège et un autre mois."""
    with zipfile.ZipFile(chemin, "w") as z:
        z.writestr("__MACOSX/uber-trip-data/._uber-raw-data-apr14.csv", b"piege")
        z.writestr("uber-trip-data/uber-raw-data-apr14.csv", contenu)
        z.writestr("uber-trip-data/uber-raw-data-may14.csv", b"mai")


def test_lire_octets_depuis_l_archive_et_depuis_le_dossier(tmp_path):
    """Même contenu lu dans l'archive (entrée exacte, __MACOSX écarté) et dans le dossier."""
    fabriquer_archive(tmp_path / "a.zip", b"avril")
    assert u.lire_octets("apr", "archive", tmp_path, tmp_path / "a.zip") == b"avril"
    assert u.lire_octets("may", "archive", tmp_path, tmp_path / "a.zip") == b"mai"
    (tmp_path / "uber-raw-data-apr14.csv").write_bytes(b"avril du dossier")
    assert u.lire_octets("apr", "dossier", tmp_path, tmp_path / "a.zip") == b"avril du dossier"


def test_lire_octets_refuse_un_mois_absent_de_l_archive(tmp_path):
    """Le fichier de juin n'est pas dans l'archive : FileNotFoundError qui nomme le fichier."""
    fabriquer_archive(tmp_path / "a.zip", b"avril")
    with pytest.raises(FileNotFoundError, match="uber-raw-data-jun14.csv"):
        u.lire_octets("jun", "archive", tmp_path, tmp_path / "a.zip")


def test_lire_octets_refuse_une_source_inconnue(tmp_path):
    """Seules « dossier » et « archive » existent : ValueError sinon."""
    with pytest.raises(ValueError, match="source inconnue"):
        u.lire_octets("apr", "internet", tmp_path, tmp_path / "a.zip")


def test_lire_octets_refuse_un_dossier_sans_le_fichier_du_mois(tmp_path):
    """Dossier extrait sans le fichier de juin : FileNotFoundError, pas un mois lu à vide."""
    (tmp_path / "uber-raw-data-apr14.csv").write_bytes(b"avril")
    with pytest.raises(FileNotFoundError, match="uber-raw-data-jun14.csv"):
        u.lire_octets("jun", "dossier", tmp_path, tmp_path / "a.zip")


def test_lire_octets_ne_modifie_ni_l_archive_ni_le_dossier_inchange(tmp_path):
    """La lecture n'extrait rien : l'archive garde son contenu et le dossier ne reçoit rien."""
    fabriquer_archive(tmp_path / "a.zip", b"avril")
    avant = (tmp_path / "a.zip").read_bytes()
    u.lire_octets("apr", "archive", tmp_path, tmp_path / "a.zip")
    assert (tmp_path / "a.zip").read_bytes() == avant
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.zip"]


def test_lire_octets_refuse_une_archive_qui_n_en_est_pas_une(tmp_path):
    """Un fichier qui n'est pas une archive zip est refusé : BadZipFile, pas un contenu vide."""
    (tmp_path / "faux.zip").write_bytes(b"ceci n'est pas un zip")
    with pytest.raises(zipfile.BadZipFile):
        u.lire_octets("apr", "archive", tmp_path, tmp_path / "faux.zip")


def test_empreinte_conforme_compare_le_sha256_des_octets():
    """SHA-256 de « abc » connu : vrai avec la bonne empreinte, faux avec une autre."""
    abc = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert u.empreinte_conforme(b"abc", abc) is True
    assert u.empreinte_conforme(b"abd", abc) is False
    assert u.empreinte_conforme(b"abc", hashlib.sha256(b"abc").hexdigest()) is True


def test_empreinte_conforme_refuse_un_texte_a_la_place_d_octets():
    """Un str n'est pas des octets : TypeError, pas une empreinte calculée sur autre chose."""
    with pytest.raises(TypeError):
        u.empreinte_conforme("abc", "x")  # type: ignore[arg-type]


def test_empreinte_conforme_distingue_majuscules_et_minuscules():
    """L'empreinte attendue est comparée telle quelle : la même en majuscules est refusée."""
    abc = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert u.empreinte_conforme(b"abc", abc.upper()) is False


# --------------------------------------------------------------------------------------------
# to_xy, from_xy, projeter_en_km, nearest_zone_name
# --------------------------------------------------------------------------------------------


def test_to_xy_projette_en_kilometres_autour_du_centre():
    """Le centre donne (0, 0) ; un degré de latitude vaut 110,574 km ; la longitude se réduit."""
    assert u.to_xy(LAT0, LON0, LAT0, LON0) == (0.0, 0.0)
    x, y = u.to_xy(LAT0 + 1, LON0, LAT0, LON0)
    assert (x, y) == pytest.approx((0.0, 110.574), abs=1e-9)
    x, y = u.to_xy(LAT0, LON0 + 1, LAT0, LON0)
    assert (x, y) == pytest.approx((111.320 * COS_LAT0, 0.0), abs=1e-9)
    xs, ys = u.to_xy(np.array([LAT0, LAT0 + 0.5]), np.array([LON0, LON0]), LAT0, LON0)
    assert list(ys) == pytest.approx([0.0, 55.287])
    assert list(xs) == [0.0, 0.0]


def test_from_xy_est_l_inverse_de_to_xy():
    """Aller-retour kilomètres → latitude/longitude → kilomètres, et valeurs de référence."""
    lat, lon = u.from_xy(0.0, 110.574, LAT0, LON0)
    assert (lat, lon) == pytest.approx((LAT0 + 1, LON0))
    lat, lon = u.from_xy(111.320 * COS_LAT0, 0.0, LAT0, LON0)
    assert (lat, lon) == pytest.approx((LAT0, LON0 + 1))
    x, y = u.to_xy(*u.from_xy(3.0, -4.0, LAT0, LON0), LAT0, LON0)
    assert (x, y) == pytest.approx((3.0, -4.0))
    assert isinstance(u.from_xy(1, 2, LAT0, LON0)[0], float)


def test_projeter_en_km_projette_un_tableau_lat_lon():
    """Colonne 0 = latitude, colonne 1 = longitude ; sortie [[x_km, y_km], ...]."""
    sortie = u.projeter_en_km([[LAT0, LON0], [LAT0 + 1, LON0 + 1]], LAT0, LON0)
    assert sortie.shape == (2, 2)
    assert sortie.ravel().tolist() == pytest.approx([0.0, 0.0, 111.320 * COS_LAT0, 110.574])


def test_projeter_en_km_calcule_en_double_precision_meme_depuis_des_float32():
    """Des coordonnées float32 sont converties en float64 avant la projection."""
    coords32 = np.array([[LAT0 + 0.123, LON0 - 0.456], [LAT0, LON0]], dtype=np.float32)
    sortie = u.projeter_en_km(coords32, LAT0, LON0)
    assert sortie.dtype == np.float64
    reference = u.projeter_en_km(coords32.astype(np.float64), LAT0, LON0)
    assert sortie.tolist() == reference.tolist()


def test_projeter_en_km_refuse_un_tableau_a_une_dimension():
    """Une liste plate n'est pas un tableau de couples lat/lon : IndexError."""
    with pytest.raises(IndexError):
        u.projeter_en_km([LAT0, LON0], LAT0, LON0)


def test_projeter_en_km_refuse_un_tableau_sans_longitude():
    """Une seule colonne (latitudes seules) : il manque la longitude, IndexError."""
    with pytest.raises(IndexError):
        u.projeter_en_km(np.array([[LAT0], [LAT0 + 0.1]]), LAT0, LON0)


def test_projections_laissent_les_tableaux_inchanges():
    """Les tableaux passés à to_xy et projeter_en_km ne sont pas modifiés."""
    coords = np.array([[LAT0, LON0], [LAT0 + 1, LON0 + 1]])
    avant = coords.copy()
    u.projeter_en_km(coords, LAT0, LON0)
    u.to_xy(coords[:, 0], coords[:, 1], LAT0, LON0)
    assert (coords == avant).all()


def test_nearest_zone_name_rend_le_repere_le_plus_proche_et_la_distance():
    """Le point (9, 1) est à sqrt(2) km de Beta ; un point équidistant donne le premier repère."""
    lat, lon = u.from_xy(9.0, 1.0, LAT0, LON0)
    nom, distance = u.nearest_zone_name(lat, lon, REPERES, LAT0, LON0)
    assert nom == "Beta"
    assert distance == pytest.approx(math.sqrt(2.0))
    lat, lon = u.from_xy(5.25, 0.0, LAT0, LON0)  # à 4,75 km d'Alpha et de Beta
    assert u.nearest_zone_name(lat, lon, REPERES, LAT0, LON0)[0] == "Alpha"
    assert isinstance(u.nearest_zone_name(lat, lon, REPERES, LAT0, LON0)[1], float)


def test_nearest_zone_name_laisse_les_reperes_inchanges():
    """Chercher le repère le plus proche ne trie ni ne modifie le tableau des repères."""
    reperes = REPERES.copy()
    avant = reperes.copy()
    u.nearest_zone_name(LAT0, LON0, reperes, LAT0, LON0)
    assert_frame_equal(reperes, avant)


def test_nearest_zone_name_refuse_des_reperes_sans_coordonnees():
    """Des repères sans x_km ni y_km ne peuvent pas être comparés au point : KeyError."""
    with pytest.raises(KeyError):
        u.nearest_zone_name(LAT0, LON0, REPERES[["zone_name"]], LAT0, LON0)


def test_nearest_zone_name_refuse_des_reperes_vides_ou_sans_nom():
    """Sans repère, il n'y a pas de plus proche (ValueError) ; sans zone_name, KeyError."""
    vide = REPERES.iloc[0:0]
    with pytest.raises(ValueError):
        u.nearest_zone_name(LAT0, LON0, vide, LAT0, LON0)
    with pytest.raises(KeyError):
        u.nearest_zone_name(LAT0, LON0, REPERES.drop(columns="zone_name"), LAT0, LON0)


# --------------------------------------------------------------------------------------------
# fit_hotzones
# --------------------------------------------------------------------------------------------


def test_fit_hotzones_decrit_chaque_zone():
    """Trois nuages (300, 200, 100) : volumes, parts, rangs, pics, repères, centroïdes, rayons."""
    subset = jeu_zones()
    stats, model, labels, _ = ajuster(subset)
    assert list(stats["pickup_count"]) == [300, 200, 100]
    assert list(stats["share_pct"]) == pytest.approx([50.0, 100 / 3, 100 / 6])
    assert list(stats["rank"]) == [1, 2, 3]
    assert list(stats["peak_hour"]) == [17, 8, 22]
    assert list(stats["zone_name"]) == ["Alpha", "Beta", "Gamma"]
    assert stats["zone_distance_km"].tolist() == pytest.approx([0.5, 0.0, 4.5], abs=0.1)
    assert list(stats.columns) == [
        "cluster",
        "pickup_count",
        "share_pct",
        "centroid_lat",
        "centroid_lon",
        "radius_p90_km",
        "peak_hour",
        "zone_name",
        "zone_distance_km",
        "rank",
        "zone_label",
        "area_km2",
        "density_per_km2",
        "rank_density",
    ]
    for _, zone in stats.iterrows():
        x, y = model.cluster_centers_[int(zone["cluster"])]
        assert (zone["centroid_lat"], zone["centroid_lon"]) == u.from_xy(x, y, LAT0, LON0)
        assert zone["pickup_count"] == int((labels == zone["cluster"]).sum())
    assert labels.shape == (600,)
    # rayon = 90e centile des distances au centroïde ; surface = disque ; densité = volume/surface
    coords = subset[["x_km", "y_km"]].to_numpy(dtype=np.float32)
    distances = np.sqrt(((coords - model.cluster_centers_[labels]) ** 2).sum(axis=1))
    for _, zone in stats.iterrows():
        attendu = np.percentile(distances[labels == zone["cluster"]], 90)
        assert zone["radius_p90_km"] == pytest.approx(attendu, rel=1e-6)
        assert zone["area_km2"] == pytest.approx(math.pi * zone["radius_p90_km"] ** 2)
        assert zone["density_per_km2"] == pytest.approx(zone["pickup_count"] / zone["area_km2"])
    ordre_densite = stats["density_per_km2"].rank(ascending=False, method="first").astype(int)
    assert list(stats["rank_density"]) == list(ordre_densite)
    assert sorted(stats["rank_density"]) == [1, 2, 3]
    # les rangs sont des entiers : les exports CSV écrivent « 1 », pas « 1.0 »
    assert pd.api.types.is_integer_dtype(stats["rank_density"])
    assert pd.api.types.is_integer_dtype(stats["rank"])


def test_fit_hotzones_qualite_est_celle_de_scikit_learn_et_les_reglages_sont_transmis():
    """Qualité = scikit-learn sur les étiquettes rendues ; réglages transmis à MiniBatchKMeans."""
    subset = jeu_zones()
    _, model, labels, qualite = ajuster(subset)
    coords = subset[["x_km", "y_km"]].to_numpy(dtype=np.float32)
    assert qualite["n_rows"] == 600
    assert qualite["k"] == 3
    assert qualite["silhouette"] == silhouette_score(coords, labels)
    assert qualite["davies_bouldin"] == davies_bouldin_score(coords, labels)
    assert qualite["calinski_harabasz"] == calinski_harabasz_score(coords, labels)
    assert qualite["inertia"] == float(model.inertia_)
    assert qualite["silhouette"] > 0.9  # nuages très séparés
    assert list(qualite) == [
        "n_rows",
        "k",
        "silhouette",
        "davies_bouldin",
        "calinski_harabasz",
        "inertia",
    ]
    # batch_size, n_init et max_iter sont transmis à MiniBatchKMeans tels quels
    _, modele_regle, _, _ = ajuster(subset, batch_size=64, n_init=2, max_iter=7)
    assert (modele_regle.batch_size, modele_regle.n_init, modele_regle.max_iter) == (64, 2, 7)
    assert (modele_regle.n_clusters, modele_regle.random_state) == (3, 0)


def test_fit_hotzones_libelle_secteur_pour_un_repere_trop_loin():
    """Au-delà de seuil_nom_km : « secteur de ... (à X km) » ; en deçà, le nom seul."""
    stats, _, _, _ = ajuster(jeu_zones())
    distance_gamma = stats.loc[2, "zone_distance_km"]
    assert stats.loc[0, "zone_label"] == "Alpha"
    assert stats.loc[1, "zone_label"] == "Beta"
    assert stats.loc[2, "zone_label"] == f"secteur de Gamma (à {round(distance_gamma, 1)} km)"
    large, _, _, _ = ajuster(jeu_zones(), seuil_nom_km=5.0)
    assert large.loc[2, "zone_label"] == "Gamma"  # 4,5 km est sous le seuil de 5 km
    serre, _, _, _ = ajuster(jeu_zones(), seuil_nom_km=0.1)
    assert serre.loc[0, "zone_label"].startswith("secteur de Alpha")
    # le seuil est inclus : à distance égale au seuil, le nom seul est gardé
    egal, _, _, _ = ajuster(jeu_zones(), seuil_nom_km=stats.loc[0, "zone_distance_km"])
    assert egal.loc[0, "zone_label"] == "Alpha"


def test_fit_hotzones_echantillonne_sans_remise_pour_apprendre_et_pour_mesurer():
    """Apprentissage sur 200 points tirés, ou sur tout ; qualité sur 100 points tirés."""
    subset = jeu_zones()
    coords = subset[["x_km", "y_km"]].to_numpy(dtype=np.float32)

    def modele(donnees):
        return MiniBatchKMeans(
            n_clusters=3, random_state=0, batch_size=5_000, n_init=3, max_iter=80
        ).fit(donnees)

    _, partiel, _, _ = ajuster(subset, taille_apprentissage=200)
    echantillon = np.random.default_rng(0).choice(600, size=200, replace=False)
    attendu_partiel = modele(coords[echantillon]).cluster_centers_.tolist()
    assert partiel.cluster_centers_.tolist() == attendu_partiel
    # aucun tirage quand l'échantillon est au moins aussi grand que le jeu : l'ordre est conservé
    attendu = modele(coords).cluster_centers_.tolist()
    for taille in (600, 5_000):
        _, complet, _, _ = ajuster(subset, taille_apprentissage=taille)
        assert complet.cluster_centers_.tolist() == attendu
    # qualité sur exactement tous les points : aucun tirage, l'ordre des lignes est conservé
    _, _, etiquettes, exact = ajuster(subset, taille_qualite=600)
    assert exact["silhouette"] == silhouette_score(coords, etiquettes)
    assert exact["davies_bouldin"] == davies_bouldin_score(coords, etiquettes)
    assert exact["calinski_harabasz"] == calinski_harabasz_score(coords, etiquettes)
    # la qualité porte sur 100 points tirés avec la même graine (premier tirage de la graine)
    _, _, labels, qualite = ajuster(subset, taille_qualite=100)
    mesure = np.random.default_rng(0).choice(600, size=100, replace=False)
    assert qualite["silhouette"] == silhouette_score(coords[mesure], labels[mesure])
    assert qualite["davies_bouldin"] == davies_bouldin_score(coords[mesure], labels[mesure])
    assert qualite["calinski_harabasz"] == calinski_harabasz_score(coords[mesure], labels[mesure])
    assert qualite["n_rows"] == 600  # n_rows compte toutes les lignes, pas l'échantillon


def test_fit_hotzones_refuse_plus_de_clusters_que_de_lignes():
    """Trois lignes pour quatre clusters : ValueError."""
    petit = jeu_zones().iloc[:3]
    with pytest.raises(ValueError) as erreur:
        ajuster(petit, k=4)
    assert str(erreur.value) == "Pas assez de lignes pour le nombre de clusters demandé."


@pytest.mark.parametrize("k", [0, -2])
def test_fit_hotzones_refuse_un_nombre_de_clusters_non_positif(k):
    """k doit être au moins 1 : ValueError qui nomme k."""
    with pytest.raises(ValueError, match="k doit être"):
        ajuster(jeu_zones(), k=k)


def test_fit_hotzones_refuse_un_jeu_sans_les_colonnes_de_coordonnees():
    """Sans x_km, le clustering est impossible : KeyError."""
    with pytest.raises(KeyError):
        ajuster(jeu_zones().drop(columns="x_km"))


def test_fit_hotzones_un_jeu_de_k_lignes_echoue_a_la_qualite_pas_au_controle_de_taille():
    """Avec exactement k lignes, le contrôle de taille passe ; la silhouette exige moins de k."""
    trois = jeu_zones().iloc[[0, 300, 500]]
    with pytest.raises(ValueError) as erreur:
        ajuster(trois, k=3)
    assert "Pas assez de lignes" not in str(erreur.value)


def test_fit_hotzones_un_seul_cluster_echoue_a_la_qualite_pas_au_controle_de_k():
    """k = 1 passe le contrôle de k (au moins 1) ; la silhouette exige au moins 2 clusters."""
    with pytest.raises(ValueError) as erreur:
        ajuster(jeu_zones(), k=1)
    assert "k doit être" not in str(erreur.value)


def test_fit_hotzones_refuse_un_jeu_sans_colonne_hour():
    """Sans l'heure de chaque course, l'heure de pic est impossible : KeyError qui nomme hour."""
    with pytest.raises(KeyError, match="hour"):
        ajuster(jeu_zones().drop(columns="hour"))


def test_fit_hotzones_laisse_le_jeu_inchange():
    """Le jeu passé n'est pas modifié (le cluster est ajouté à une copie)."""
    subset = jeu_zones()
    avant = subset.copy()
    ajuster(subset)
    assert_frame_equal(subset, avant)


def test_numpy_traite_replace_none_comme_replace_false():
    """Preuve de MUTANTS_EQUIVALENTS.md : choice(..., replace=None) tire comme replace=False."""
    sans_remise = np.random.default_rng(0).choice(100, size=10, replace=False)
    avec_none = np.random.default_rng(0).choice(100, size=10, replace=None)
    assert avec_none.tolist() == sans_remise.tolist()


def test_numpy_refuse_un_tirage_sans_remise_plus_grand_que_la_population():
    """Tirer 11 points distincts parmi 10 est impossible : ValueError (d'où le min(taille, n))."""
    with pytest.raises(ValueError):
        np.random.default_rng(0).choice(10, size=11, replace=False)


# --------------------------------------------------------------------------------------------
# recommend_hotzones_from_history
# --------------------------------------------------------------------------------------------


def tableaux_zones():
    """Deux tableaux de zones : par jour (lundi, mardi ; 3 zones) et le créneau vendredi 18 h."""
    colonnes = ["rank", "zone_name", "pickup_count", "share_pct", "centroid_lat", "centroid_lon",
                "radius_p90_km"]
    lignes = []
    for jour in (0, 1):
        for rang in (3, 1, 2):  # volontairement dans le désordre
            lignes.append([jour, rang, f"zone{jour}{rang}", 100 * rang, 10.0 * rang, 40.0 + rang,
                           -74.0 + rang, 1.5 * rang])
    quotidien = pd.DataFrame(lignes, columns=["weekday", *colonnes])
    vendredi = pd.DataFrame(
        [[r, f"v{r}", 10 * r, 5.0 * r, 40.5 + r, -73.5 + r, 2.0 * r] for r in (1, 2, 3)],
        columns=colonnes,
    )
    return quotidien, vendredi


def test_recommend_hotzones_rend_les_premieres_zones_du_jour_ou_du_creneau():
    """Sans heure : les top_n premières zones du jour par rang ; vendredi 18 h : le créneau."""
    quotidien, vendredi = tableaux_zones()
    lundi = u.recommend_hotzones_from_history(0, None, 2, quotidien, vendredi)
    assert list(lundi["zone_name"]) == ["zone01", "zone02"]
    assert list(lundi.columns) == [
        "rank", "zone_name", "pickup_count", "share_pct", "centroid_lat", "centroid_lon",
        "radius_p90_km",
    ]
    mardi = u.recommend_hotzones_from_history(1, None, 5, quotidien, vendredi)
    assert list(mardi["zone_name"]) == ["zone11", "zone12", "zone13"]
    creneau = u.recommend_hotzones_from_history(4, 18, 2, quotidien, vendredi)
    assert list(creneau["zone_name"]) == ["v1", "v2"]


def test_recommend_hotzones_accepte_un_seul_resultat():
    """top_n = 1 est valide : la première zone seulement, pour un jour comme pour vendredi 18 h."""
    quotidien, vendredi = tableaux_zones()
    jour = u.recommend_hotzones_from_history(1, None, 1, quotidien, vendredi)
    creneau = u.recommend_hotzones_from_history(4, 18, 1, quotidien, vendredi)
    assert list(jour["zone_name"]) == ["zone11"]
    assert list(creneau["zone_name"]) == ["v1"]


def test_recommend_hotzones_refuse_un_tableau_quotidien_sans_colonne_weekday():
    """Sans la colonne weekday, on ne peut pas filtrer le jour demandé : KeyError."""
    quotidien, vendredi = tableaux_zones()
    with pytest.raises(KeyError):
        u.recommend_hotzones_from_history(0, None, 2, quotidien.drop(columns="weekday"), vendredi)


def test_recommend_hotzones_refuse_un_creneau_non_precalcule():
    """Seul vendredi 18 h est pré-calculé : tout autre créneau lève NotImplementedError."""
    quotidien, vendredi = tableaux_zones()
    with pytest.raises(NotImplementedError) as erreur:
        u.recommend_hotzones_from_history(0, 18, 3, quotidien, vendredi)
    assert str(erreur.value) == (
        "Ce notebook pré-calcule les modèles journaliers et le créneau vendredi 18h. "
        "Pour un autre créneau, utiliser fit_hotzones sur le filtre correspondant."
    )
    with pytest.raises(NotImplementedError):
        u.recommend_hotzones_from_history(4, 17, 3, quotidien, vendredi)


@pytest.mark.parametrize("top_n", [0, -1])
def test_recommend_hotzones_refuse_un_top_n_non_positif(top_n):
    """top_n doit être au moins 1 : ValueError."""
    quotidien, vendredi = tableaux_zones()
    with pytest.raises(ValueError, match="top_n"):
        u.recommend_hotzones_from_history(0, None, top_n, quotidien, vendredi)


def test_recommend_hotzones_laisse_les_tableaux_inchanges():
    """La recommandation trie une copie : les tableaux passés gardent leur ordre d'origine."""
    quotidien, vendredi = tableaux_zones()
    q_avant, v_avant = quotidien.copy(), vendredi.copy()
    u.recommend_hotzones_from_history(0, None, 2, quotidien, vendredi)
    u.recommend_hotzones_from_history(4, 18, 2, quotidien, vendredi)
    assert_frame_equal(quotidien, q_avant)
    assert_frame_equal(vendredi, v_avant)


# --------------------------------------------------------------------------------------------
# distance_au_segment, chercher_position, placer_etiquette
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "a", "b", "attendu"),
    [
        ((0.5, 0.0), (0.0, 0.0), (1.0, 0.0), 0.0),  # sur le segment
        ((0.5, 0.3), (0.0, 0.0), (1.0, 0.0), 0.3),  # à la perpendiculaire
        ((2.0, 0.0), (0.0, 0.0), (1.0, 0.0), 1.0),  # au-delà de b : distance à b
        ((-3.0, 4.0), (0.0, 0.0), (1.0, 0.0), 5.0),  # en deçà de a : distance à a
        ((3.0, 4.0), (0.0, 0.0), (0.0, 0.0), 5.0),  # segment réduit à un point
    ],
)
def test_distance_au_segment_selon_la_position_du_point(point, a, b, attendu):
    """Distance au segment [a, b], le point projeté étant ramené entre a et b."""
    assert u.distance_au_segment(point, a, b) == pytest.approx(attendu)


def test_distance_au_segment_refuse_des_dimensions_incompatibles():
    """Un point à trois coordonnées pour un segment à deux : ValueError de numpy."""
    with pytest.raises(ValueError):
        u.distance_au_segment((1.0, 2.0, 3.0), (0.0, 0.0), (1.0, 0.0))


GEO = u.GeometrieCarte(
    x_min=0.0, x_max=1.0, pas_x=0.25, y_min=0.0, y_max=1.0, pas_y=0.25,
    demi_hauteur=0.1, marge_x=0.0, marge_y=0.0, distance_trait=0.05,
)
CENTRE = (0.65, 0.5)
LARGEUR = 0.3


@pytest.mark.parametrize(
    ("occupes", "cercles", "attendu"),
    [
        ([], [], (0.5, 0.5)),  # sans obstacle : la case dont le milieu est au plus près du centre
        ([], [CENTRE], (0.25, 0.5)),  # son propre cercle : trois cases à 0,25, la première gardée
        ([(0.25, 0.4, 0.55, 0.6)], [CENTRE], (0.5, 0.25)),  # une étiquette déjà placée
        ([(0.2, 0.35, 0.6, 0.65)], [CENTRE], (0.5, 0.0)),  # deux boîtes qui se touchent
        ([(0.2, 0.36, 0.6, 0.65)], [CENTRE], (0.5, 0.25)),  # à 0,01 d'écart, elles sont séparées
    ],
)
def test_chercher_position_prend_la_position_libre_la_plus_proche_du_centre(
    occupes, cercles, attendu
):
    """Position libre la plus proche du centre (rien de recouvert) ; à égalité, la première."""
    assert u.chercher_position(CENTRE, LARGEUR, occupes, cercles, True, GEO) == attendu


# Bords de la boîte du meilleur candidat (0,5 ; 0,5), calculés comme le fait le module
BORD_GAUCHE = 0.5
BORD_DROIT = 0.5 + LARGEUR
BORD_BAS = 0.5 - 0.1
BORD_HAUT = 0.5 + 0.1
ECART = 0.01


@pytest.mark.parametrize(
    ("cercle", "bloque"),
    [
        ((BORD_DROIT, 0.5), True),  # sur le bord droit de la boîte : inclus
        ((BORD_GAUCHE, 0.5), True),  # sur le bord gauche
        ((0.65, BORD_HAUT), True),  # sur le bord haut
        ((0.65, BORD_BAS), True),  # sur le bord bas
        ((BORD_DROIT + ECART, 0.5), False),  # juste à droite
        ((BORD_GAUCHE - ECART, 0.5), False),  # juste à gauche
        ((0.65, BORD_HAUT + ECART), False),  # juste au-dessus
        ((0.65, BORD_BAS - ECART), False),  # juste au-dessous
    ],
)
def test_chercher_position_un_cercle_sur_le_bord_de_la_boite_la_recouvre(cercle, bloque):
    """Un cercle exactement sur le bord de l'étiquette la recouvre ; à 0,01 de là, non."""
    position = u.chercher_position(CENTRE, LARGEUR, [], [cercle], True, GEO)
    assert (position != (0.5, 0.5)) is bloque


@pytest.mark.parametrize(
    ("occupee", "bloque"),
    [
        ((BORD_DROIT, BORD_BAS, 0.9, BORD_HAUT), True),  # touche le côté droit
        ((0.4, BORD_BAS, BORD_GAUCHE, BORD_HAUT), True),  # touche le côté gauche
        ((0.5, BORD_HAUT, BORD_DROIT, 0.7), True),  # touche le haut
        ((0.5, 0.3, BORD_DROIT, BORD_BAS), True),  # touche le bas
        ((BORD_DROIT + ECART, BORD_BAS, 0.9, BORD_HAUT), False),
        ((0.4, BORD_BAS, BORD_GAUCHE - ECART, BORD_HAUT), False),
        ((0.5, BORD_HAUT + ECART, BORD_DROIT, 0.7), False),
        ((0.5, 0.3, BORD_DROIT, BORD_BAS - ECART), False),
    ],
)
def test_chercher_position_une_etiquette_qui_touche_la_boite_la_recouvre(occupee, bloque):
    """Deux boîtes qui se touchent par un côté se recouvrent ; à 0,01 d'écart, non."""
    position = u.chercher_position(CENTRE, LARGEUR, [occupee], [], True, GEO)
    assert (position != (0.5, 0.5)) is bloque


def test_chercher_position_un_obstacle_bas_laisse_la_case_libre_plus_haute_de_la_colonne():
    """Dans une colonne, des cases basses bloquées n'interdisent pas la case libre au-dessus."""
    centre = (0.65, 0.75)
    obstacle = [(0.45, 0.0, 0.85, 0.45)]
    assert u.chercher_position(centre, LARGEUR, obstacle, [], True, GEO) == (0.5, 0.75)


def test_chercher_position_applique_les_marges_autour_de_l_etiquette():
    """Avec une marge de 0,3 autour de l'étiquette, un cercle voisin la rend impossible."""
    geo_marge = u.GeometrieCarte(
        x_min=0.0, x_max=1.0, pas_x=0.25, y_min=0.0, y_max=1.0, pas_y=0.25,
        demi_hauteur=0.1, marge_x=0.3, marge_y=0.3, distance_trait=0.05,
    )
    cercle = [(0.65, 0.5)]
    sans_marge = u.chercher_position(CENTRE, LARGEUR, [], cercle, True, GEO)
    avec_marge = u.chercher_position(CENTRE, LARGEUR, [], cercle, True, geo_marge)
    assert sans_marge == (0.25, 0.5)
    assert avec_marge == (0.0, 0.5)  # l'étiquette s'écarte du cercle d'au moins la marge


def test_chercher_position_exige_un_trait_de_rappel_libre_quand_on_le_demande():
    """Le trait le plus court frôle un autre cercle : refusé en mode strict, accepté sinon."""
    cercles = [CENTRE, (0.62, 0.38)]
    occupes = [(0.2, 0.35, 0.6, 0.65), (0.45, 0.65, 0.85, 0.85)]
    strict = u.chercher_position(CENTRE, LARGEUR, occupes, cercles, True, GEO)
    souple = u.chercher_position(CENTRE, LARGEUR, occupes, cercles, False, GEO)
    assert strict == (0.0, 0.0)
    assert souple == (0.5, 0.0)
    # le trait de la position souple passe à 0,03 du cercle voisin, sous la distance de 0,05
    assert u.distance_au_segment(cercles[1], (0.65, 0.0), CENTRE) == pytest.approx(0.03)


def test_chercher_position_un_trait_a_la_distance_exacte_du_seuil_n_est_pas_libre():
    """Le trait doit passer STRICTEMENT plus loin que distance_trait d'un autre cercle."""
    geo = u.GeometrieCarte(
        x_min=0.0, x_max=1.0, pas_x=0.5, y_min=0.0, y_max=1.0, pas_y=0.5,
        demi_hauteur=0.1, marge_x=0.0, marge_y=0.0, distance_trait=0.25,
    )
    centre, voisin = (0.75, 0.5), (0.5, 0.75)
    # le trait horizontal de (0,25 ; 0,5) à (0,75 ; 0,5) passe exactement à 0,25 du voisin
    assert u.distance_au_segment(voisin, (0.25, 0.5), centre) == 0.25
    assert u.chercher_position(centre, 0.5, [], [centre, voisin], True, geo) == (0.0, 0.0)
    assert u.chercher_position(centre, 0.5, [], [centre, voisin], False, geo) == (0.0, 0.5)


def test_chercher_position_le_trait_de_rappel_part_du_milieu_de_l_etiquette():
    """Le trait va du MILIEU de l'étiquette au centre de la zone, pas d'un autre point du bord.

    Étiquette de largeur 0,5 en (0 ; 0,5) : son trait part de (0,25 ; 0,5). Le voisin (0,2 ; 0,62)
    est à 0,13 de ce trait, au-delà du seuil 0,125 : la position est libre. Un trait parti du
    tiers de l'étiquette passerait à 0,12 du voisin, et la position serait refusée.
    """
    geo = u.GeometrieCarte(
        x_min=0.0, x_max=1.0, pas_x=0.5, y_min=0.0, y_max=1.0, pas_y=0.5,
        demi_hauteur=0.1, marge_x=0.0, marge_y=0.0, distance_trait=0.125,
    )
    centre, voisin = (0.75, 0.5), (0.2, 0.62)
    assert u.distance_au_segment(voisin, (0.25, 0.5), centre) == pytest.approx(0.13)
    assert u.distance_au_segment(voisin, (0.5 / 3, 0.5), centre) == pytest.approx(0.12)
    assert u.chercher_position(centre, 0.5, [], [centre, voisin], True, geo) == (0.0, 0.5)


def test_chercher_position_un_cercle_confondu_avec_le_centre_n_est_pas_un_autre_cercle():
    """Un cercle à 1e-9 du centre est le même cercle : il ne bloque pas le trait de rappel."""
    centre = (0.0, 0.5)
    seul = u.chercher_position(centre, LARGEUR, [], [centre], True, GEO)
    assert seul is not None
    assert u.chercher_position(centre, LARGEUR, [], [centre, (1e-9, 0.5)], True, GEO) == seul


def test_chercher_position_rend_none_quand_tout_est_occupe():
    """Une étiquette qui couvre toute la carte ne laisse aucune position."""
    assert u.chercher_position(CENTRE, LARGEUR, [(0, 0, 1, 1)], [CENTRE], True, GEO) is None


def test_chercher_position_laisse_les_listes_inchangees():
    """Les listes d'obstacles passées ne sont pas modifiées."""
    occupes, cercles = [(0.2, 0.35, 0.6, 0.65)], [CENTRE, (0.62, 0.38)]
    occupes_avant, cercles_avant = list(occupes), list(cercles)
    u.chercher_position(CENTRE, LARGEUR, occupes, cercles, True, GEO)
    assert occupes == occupes_avant
    assert cercles == cercles_avant


def test_placer_etiquette_prefere_un_trait_libre_puis_accepte_un_trait_qui_croise():
    """Strict d'abord ; à défaut de position stricte, la position souple ; sinon None."""
    cercles = [CENTRE, (0.62, 0.38)]
    occupes = [(0.2, 0.35, 0.6, 0.65), (0.45, 0.65, 0.85, 0.85)]
    assert u.placer_etiquette(CENTRE, LARGEUR, occupes, cercles, GEO) == (0.0, 0.0)
    bloques = [*occupes, (0.0, 0.0, 0.45, 0.2), (0.0, 0.6, 0.45, 1.0)]
    assert u.chercher_position(CENTRE, LARGEUR, bloques, cercles, True, GEO) is None
    assert u.placer_etiquette(CENTRE, LARGEUR, bloques, cercles, GEO) == (0.5, 0.0)
    assert u.placer_etiquette(CENTRE, LARGEUR, [(0, 0, 1, 1)], cercles, GEO) is None


def test_placer_etiquette_refuse_une_geometrie_absente():
    """Sans géométrie, la recherche est impossible : TypeError, pas de valeur cachée."""
    with pytest.raises(TypeError):
        u.placer_etiquette(CENTRE, LARGEUR, [], [CENTRE])  # type: ignore[call-arg]


# --------------------------------------------------------------------------------------------
# indicateurs, evaluer, qualifier_gain
# --------------------------------------------------------------------------------------------


def test_indicateurs_calculent_mae_wape_et_biais():
    """Réel (10, 20), prévu (12, 15) : erreurs +2 et −5 ; MAE 3,5 ; WAPE 7/30 ; biais −3/30."""
    ind = u.indicateurs([10, 20], [12, 15])
    assert ind == pytest.approx({"MAE": 3.5, "WAPE": 100 * 7 / 30, "biais": -10.0})
    assert list(ind) == ["MAE", "WAPE", "biais"]
    assert all(isinstance(v, float) for v in ind.values())
    assert u.indicateurs([5, 6, 7], [5, 6, 7]) == {"MAE": 0.0, "WAPE": 0.0, "biais": 0.0}


def test_indicateurs_calculent_en_double_precision_meme_depuis_des_float32():
    """Des séries float32 sont converties en float64 : mêmes résultats qu'en double précision."""
    reel32 = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    prevu32 = np.array([0.15, 0.25, 0.1], dtype=np.float32)
    reel, prevu = reel32.astype(np.float64), prevu32.astype(np.float64)
    ind = u.indicateurs(reel32, prevu32)
    assert ind["MAE"] == pytest.approx(np.abs(prevu - reel).mean(), rel=1e-13)
    assert ind["WAPE"] == pytest.approx(100 * np.abs(prevu - reel).sum() / reel.sum(), rel=1e-13)
    assert ind["biais"] == pytest.approx(100 * (prevu - reel).sum() / reel.sum(), rel=1e-13)


def test_indicateurs_refusent_des_longueurs_differentes_ou_une_demande_nulle():
    """Longueurs différentes : ValueError ; demande totale nulle : ValueError (pas d'infini)."""
    message = "y_reel et y_prevu doivent avoir la même longueur"
    with pytest.raises(ValueError) as erreur:
        u.indicateurs([1, 2, 3], [1, 2])
    assert str(erreur.value) == message
    with pytest.raises(ValueError) as erreur:
        u.indicateurs([1, 2, 3], [1])
    assert str(erreur.value) == message
    with pytest.raises(ValueError) as erreur:
        u.indicateurs([0, 0], [1, 2])
    assert str(erreur.value) == "la demande réelle totale est nulle : WAPE et biais indéfinis"


def test_indicateurs_refusent_une_prevision_illisible():
    """Une prévision qui n'est pas un nombre (« a ») est refusée : ValueError de conversion."""
    with pytest.raises(ValueError, match="could not convert"):
        u.indicateurs([10, 20], ["a", "b"])
    with pytest.raises(ValueError, match="could not convert"):
        u.indicateurs(["a", "b"], [10, 20])


def test_indicateurs_laissent_les_series_inchangees():
    """Les séries passées ne sont pas modifiées."""
    reel, prevu = pd.Series([10, 20]), pd.Series([12, 15])
    u.indicateurs(reel, prevu)
    assert list(reel) == [10, 20]
    assert list(prevu) == [12, 15]


def test_evaluer_rend_une_ligne_de_tableau_avec_ou_sans_apprentissage():
    """R², MAE, WAPE, biais du test ; le R² d'apprentissage seulement si on le demande."""
    ligne = u.evaluer("m", [10, 20, 30], [10, 20, 30])
    assert ligne == {
        "modèle": "m",
        "R² test": 1.0,
        "MAE test (courses/heure)": 0.0,
        "WAPE test (%)": 0.0,
        "biais test (%)": 0.0,
    }
    avec = u.evaluer("m", [10, 20, 30], [12, 18, 33], [1, 2, 3], [1, 2, 3])
    assert avec["R² apprentissage"] == 1.0
    assert avec["R² test"] == pytest.approx(1 - 17 / 200)  # erreurs 2, −2, 3 : SCR 17, SCT 200
    assert avec["MAE test (courses/heure)"] == pytest.approx(7 / 3)
    assert avec["WAPE test (%)"] == pytest.approx(100 * 7 / 60)
    assert avec["biais test (%)"] == pytest.approx(100 * 3 / 60)


def test_evaluer_laisse_les_series_inchangees():
    """Évaluer une prévision ne modifie ni le réel ni le prévu (test et apprentissage)."""
    reel, prevu = pd.Series([10, 20, 30]), pd.Series([12, 18, 33])
    app_reel, app_prevu = pd.Series([1, 2, 3]), pd.Series([1.5, 2.0, 2.5])
    copies_avant = [s.copy() for s in (reel, prevu, app_reel, app_prevu)]
    u.evaluer("m", reel, prevu, app_reel, app_prevu)
    for serie, copie_avant in zip((reel, prevu, app_reel, app_prevu), copies_avant, strict=True):
        assert serie.equals(copie_avant)


def test_evaluer_refuse_un_seul_des_deux_jeux_d_apprentissage():
    """Donner y_app_reel sans y_app_prevu (ou l'inverse) est une erreur d'appel : ValueError."""
    message = "y_app_reel et y_app_prevu vont ensemble : donner les deux ou aucun"
    with pytest.raises(ValueError) as erreur:
        u.evaluer("m", [1, 2], [1, 2], y_app_reel=[1, 2])
    assert str(erreur.value) == message
    with pytest.raises(ValueError) as erreur:
        u.evaluer("m", [1, 2], [1, 2], y_app_prevu=[1, 2])
    assert str(erreur.value) == message


@pytest.mark.parametrize(
    ("gain", "mae_ref", "attendu"),
    [
        (2.0, 36.0, "modeste"),
        (5.5, 36.0, "net"),
        (-3.0, 36.0, "modeste"),
        (15.0, 100.0, "net"),  # le seuil est strict : 15 % pile est déjà « net »
        (14.999, 100.0, "modeste"),
        (0.05, 0.5, "modeste"),  # une référence positive sous 1 est acceptée
        (0.1, 0.5, "net"),
    ],
)
def test_qualifier_gain_selon_le_seuil_de_quinze_pour_cent(gain, mae_ref, attendu):
    """Un gain sous 15 % de la MAE de référence est « modeste », sinon « net »."""
    assert u.qualifier_gain(gain, mae_ref) == attendu


@pytest.mark.parametrize(
    ("gain", "ref"), [(1.0, 0.0), (1.0, -5.0), (math.nan, 10.0), (1.0, math.nan)]
)
def test_qualifier_gain_refuse_une_reference_nulle_negative_ou_nan(gain, ref):
    """Rapporter un gain à une référence nulle, négative ou NaN n'a pas de sens : ValueError."""
    with pytest.raises(ValueError, match="invalide"):
        u.qualifier_gain(gain, ref)


def test_to_xy_et_from_xy_refusent_un_texte():
    """Une latitude ou une abscisse en texte n'est pas projetable : TypeError."""
    with pytest.raises(TypeError):
        u.to_xy("40.7", LON0, LAT0, LON0)
    with pytest.raises(TypeError):
        u.from_xy("3.0", 4.0, LAT0, LON0)


def test_chercher_position_refuse_un_centre_incomplet():
    """Un centre à une seule coordonnée n'est pas un point du plan : IndexError."""
    with pytest.raises(IndexError):
        u.chercher_position((0.5,), LARGEUR, [], [], True, GEO)


def test_geometrie_carte_est_figee():
    """Les réglages d'une carte ne se modifient pas après coup : FrozenInstanceError."""
    with pytest.raises(FrozenInstanceError):
        GEO.pas_x = 0.5  # type: ignore[misc]


def test_evaluer_refuse_des_longueurs_differentes():
    """Réel et prévu de longueurs différentes : ValueError, pas un R² calculé de travers."""
    with pytest.raises(ValueError):
        u.evaluer("m", [1, 2, 3], [1, 2])


# --------------------------------------------------------------------------------------------
# afficher, afficher_markdown
# --------------------------------------------------------------------------------------------


def test_afficher_transmet_les_objets_et_enveloppe_le_markdown(monkeypatch):
    """afficher passe les objets tels quels à display ; afficher_markdown envoie un Markdown."""
    recus = []
    monkeypatch.setattr(u, "display", lambda *objets: recus.append(objets))
    u.afficher(1, "deux")
    u.afficher_markdown("**gras**")
    assert recus[0] == (1, "deux")
    (objet,) = recus[1]
    assert isinstance(objet, u.Markdown)
    assert objet.data == "**gras**"


def test_afficher_refuse_les_arguments_nommes_inconnus():
    """afficher n'accepte que des objets positionnels : un argument nommé est refusé."""
    with pytest.raises(TypeError):
        u.afficher(1, inclure=2)


def test_afficher_markdown_refuse_un_texte_manquant():
    """Sans texte, rien à afficher : TypeError, pas un affichage vide."""
    with pytest.raises(TypeError):
        u.afficher_markdown()
