# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de `kayak.outils_notebook` : les fonctions d'aide sorties du notebook.

Ce fichier prouve deux choses. D'abord, que chaque fonction rend le bon résultat sur un petit jeu
construit pour l'occasion (nombre formaté, taille cumulée, bilan Overpass par ville, hôtels relus,
image allégée). Ensuite, et surtout, qu'elle REFUSE ce qui est invalide (fichier absent, JSON
cassé, colonne manquante, largeur impossible...) ou qu'elle reste SANS EFFET sur ce qu'elle ne doit
pas toucher (fichiers non JSON, autres jours, objet affiché, lake, DataFrame de coordonnées).

Aucun réseau : les data lakes sont locaux (dossier temporaire) ou factices, les images sont
fabriquées avec Pillow, et `display` d'IPython est remplacé par un enregistreur.
"""

import functools
import io
import json
import random
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from IPython.display import Image
from PIL import Image as ImagePIL
from PIL import UnidentifiedImageError

from kayak import outils_notebook
from kayak.outils_notebook import (
    afficher,
    bilan_overpass,
    hotels_de,
    image_allegee,
    milliers,
    octets,
)
from kayak.stockage.data_lake import DataLake

JOUR = "2026-10-10"
SIGNATURE_PNG = b"\x89PNG\r\n\x1a\n"


# --- Outils de fabrication --------------------------------------------------------------------


def _lake(dossier: Path) -> DataLake:
    """Data lake local dans un dossier temporaire."""
    return DataLake(bucket=None, racine_locale=dossier / "lake")


class LakeFactice:
    """Lake minimal (un dictionnaire chemin -> octets) qui note ce qu'on lui demande.

    Il n'offre que `lister` et `lire`, les deux méthodes qu'emploie `bilan_overpass` : toute
    autre opération (écriture, suppression) lèverait une AttributeError et ferait échouer le
    test. `lister` rend les chemins dans l'ordre d'insertion, donc volontairement DÉSORDONNÉ.
    """

    def __init__(self, objets: dict[str, bytes]) -> None:
        self.objets = dict(objets)
        self.prefixes: list[str] = []
        self.lectures: list[str] = []

    def lister(self, prefixe: str) -> list[str]:
        self.prefixes.append(prefixe)
        return [chemin for chemin in self.objets if chemin.startswith(prefixe)]

    def lire(self, chemin: str) -> bytes:
        self.lectures.append(chemin)
        return self.objets[chemin]


def _chemin(ident: int, jour: str = JOUR) -> str:
    """Chemin d'une archive Overpass, nommée comme le fait la collecte (deux chiffres)."""
    return f"raw/osm/date={jour}/ville_{ident:02d}.json"


def _archive(elements: list[dict[str, Any]], **autres: Any) -> bytes:
    """Réponse Overpass sérialisée : la liste des éléments et d'éventuelles clés en plus."""
    return json.dumps({"elements": elements, **autres}).encode("utf-8")


def _element(type_: str, ident: int, nom: str | None = None) -> dict[str, Any]:
    """Un élément OpenStreetMap ; sans nom, il n'a pas de `tags` du tout."""
    element: dict[str, Any] = {"type": type_, "id": ident}
    if nom is not None:
        element["tags"] = {"name": nom}
    return element


def _coordonnees() -> pd.DataFrame:
    """Table des villes telle que la lit le notebook (avec des colonnes en plus de `ville`)."""
    return pd.DataFrame(
        {
            "id_ville": [3, 7, 12],
            "ville": ["Nice", "Lille", "Brest"],
            "latitude": [43.70, 50.63, 48.39],
            "longitude": [7.27, 3.06, -4.49],
        }
    )


@functools.cache
def _octets_png_bruite(taille: tuple[int, int], mode: str) -> bytes:
    """Octets d'un PNG de bruit (plus de 256 couleurs), reproductible grâce à la graine fixe ;
    calculés une fois par taille et par mode, pour que la suite reste rapide."""
    hasard = random.Random(2026)
    pixels: list[tuple[int, ...]] = []
    for _ in range(taille[0] * taille[1]):
        rvb = (hasard.randrange(256), hasard.randrange(256), hasard.randrange(256))
        pixels.append((*rvb, 255) if mode == "RGBA" else rvb)
    image = ImagePIL.new(mode, taille)
    image.putdata(pixels)
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    return tampon.getvalue()


def _png_bruite(chemin: Path, taille: tuple[int, int] = (400, 200), mode: str = "RGB") -> Path:
    """Écrit dans `chemin` un PNG de bruit de la taille et du mode demandés."""
    chemin.write_bytes(_octets_png_bruite(taille, mode))
    return chemin


def _damier(chemin: Path, taille: tuple[int, int] = (400, 200)) -> Path:
    """Écrit un damier noir et blanc d'un pixel (2 couleurs : la quantification est sans perte)."""
    image = ImagePIL.new("RGB", taille)
    image.putdata(
        [
            (255, 255, 255) if (x + y) % 2 else (0, 0, 0)
            for y in range(taille[1])
            for x in range(taille[0])
        ]
    )
    image.save(chemin)
    return chemin


def _contenu_du_dossier(dossier: Path) -> dict[str, bytes]:
    """Photographie d'un dossier : chaque fichier (chemin relatif) et ses octets."""
    return {
        fichier.relative_to(dossier).as_posix(): fichier.read_bytes()
        for fichier in sorted(dossier.rglob("*"))
        if fichier.is_file()
    }


# --- milliers -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nombre", "attendu"),
    [
        (0, "0"),
        (1000, "1 000"),
        (12345, "12 345"),
        (1234567, "1 234 567"),
        (-1000, "-1 000"),
        (-1234567, "-1 234 567"),
    ],
)
def test_milliers_groupe_par_trois_chiffres_avec_une_espace(nombre, attendu):
    """L'usage français sépare les milliers par une espace ordinaire (ni virgule, ni insécable)."""
    assert milliers(nombre) == attendu


@pytest.mark.parametrize("nombre", [0, 1, 99, 999, -1, -999])
def test_milliers_sous_mille_laisse_le_nombre_inchange(nombre):
    """Sous mille (en valeur absolue), aucune espace n'est insérée : le texte reste inchangé."""
    assert milliers(nombre) == str(nombre)


def test_milliers_refuse_un_texte():
    """Une chaîne n'est pas un entier : le formatage échoue au lieu de renvoyer un faux résultat."""
    with pytest.raises(ValueError, match=re.escape("Cannot specify ',' with 's'")):
        milliers("1234567")


def test_milliers_refuse_l_absence_de_valeur():
    """`None` (valeur manquante) n'est pas formaté en « None » : l'erreur est signalée."""
    with pytest.raises(TypeError, match="unsupported format string passed to NoneType"):
        milliers(None)


# --- octets -------------------------------------------------------------------------------------


def test_octets_somme_les_tailles_reelles_des_fichiers(tmp_path):
    """La taille est celle du fichier sur disque, en octets (« é » pèse 2 octets, pas 1)."""
    lake = _lake(tmp_path)
    lake.ecrire("raw/a.txt", "é")
    lake.ecrire("raw/b.bin", b"\x00\x01\x02")
    lake.ecrire("curated/c.bin", b"x" * 10)
    assert octets(lake, ["raw/a.txt", "raw/b.bin"]) == 5
    assert octets(lake, ["curated/c.bin"]) == 10
    assert octets(lake, ("raw/a.txt", "raw/b.bin", "curated/c.bin")) == 15
    assert octets(lake, (c for c in ["raw/b.bin", "curated/c.bin"])) == 13


def test_octets_liste_vide_donne_zero_et_laisse_le_lake_intact():
    """Sans fichier à mesurer, le total est 0 et aucun appel n'est émis vers le lake."""

    class LakeEspion:
        def __init__(self) -> None:
            self.appels: list[str] = []

        def chemin_local(self, chemin: str) -> Path:
            self.appels.append(chemin)
            raise AssertionError("le lake ne doit pas être interrogé")

    espion = LakeEspion()
    total = octets(espion, [])
    assert total == 0
    assert isinstance(total, int)
    assert espion.appels == []


@pytest.mark.parametrize(
    "chemins",
    [
        ["absent.bin"],
        ["present.bin", "absent.bin"],
        ["absent.bin", "present.bin"],
    ],
)
def test_octets_refuse_un_fichier_absent_sans_le_creer(tmp_path, chemins):
    """Un fichier introuvable lève FileNotFoundError, où qu'il soit dans la liste, sans total
    partiel, et la mesure ne crée jamais le fichier manquant."""
    lake = _lake(tmp_path)
    lake.ecrire("present.bin", b"123")
    with pytest.raises(FileNotFoundError, match=re.escape("absent.bin")):
        octets(lake, chemins)
    assert not lake.existe("absent.bin")


def test_octets_laisse_les_fichiers_intacts(tmp_path):
    """Mesurer ne modifie rien : mêmes fichiers, mêmes octets avant et après."""
    lake = _lake(tmp_path)
    lake.ecrire("raw/a.txt", "contenu A")
    lake.ecrire("raw/b.txt", b"contenu B")
    avant = {c: lake.lire(c) for c in lake.lister("")}
    octets(lake, ["raw/a.txt", "raw/b.txt"])
    apres = {c: lake.lire(c) for c in lake.lister("")}
    assert apres == avant


# --- bilan_overpass -----------------------------------------------------------------------------


def test_bilan_reprend_de_chaque_archive_ses_comptes_sa_remarque_et_sa_marque():
    """Une ligne par archive : éléments, nommés, points, contours, remarque d'Overpass et
    marque `kayak_requete` (sa seule PRÉSENCE compte, même avec une valeur nulle)."""
    lake = LakeFactice(
        {
            _chemin(3): _archive(
                [
                    _element("node", 1, "Hôtel A"),
                    _element("node", 2),
                    _element("way", 3, "Hôtel B"),
                    _element("relation", 4, "Quartier"),
                ],
                remark="Dépassement de délai",
                kayak_requete="[out:json];",
            ),
            _chemin(7): _archive([]),
            _chemin(12): _archive([_element("way", 5)], kayak_requete=None),
        }
    )
    bilan = bilan_overpass(lake, JOUR, _coordonnees())
    attendu = {
        "id_ville": [3, 7, 12],
        "elements": [4, 0, 1],
        "nommes": [3, 0, 0],
        "points": [2, 0, 0],
        "contours": [1, 0, 1],
        "requete_actuelle": [True, False, True],
    }
    assert {colonne: bilan[colonne].tolist() for colonne in attendu} == attendu
    assert bilan["remarque"].iloc[0] == "Dépassement de délai"
    assert pd.isna(bilan["remarque"].iloc[1])
    assert pd.isna(bilan["remarque"].iloc[2])


def test_bilan_joint_les_noms_de_villes_et_trie_les_archives():
    """Le bilan est trié par archive (donc par numéro de ville), avec les colonnes dans l'ordre
    annoncé puis le nom de la ville ; l'archive est cherchée sous `raw/osm/date=<jour>/`."""
    lake = LakeFactice(
        {
            _chemin(12): _archive([_element("node", 1)]),
            _chemin(3): _archive([_element("node", 2)]),
            _chemin(7): _archive([_element("node", 3)]),
        }
    )
    bilan = bilan_overpass(lake, JOUR, _coordonnees())
    assert bilan.columns.tolist() == [
        "id_ville",
        "elements",
        "nommes",
        "points",
        "contours",
        "remarque",
        "requete_actuelle",
        "ville",
    ]
    assert bilan["id_ville"].tolist() == [3, 7, 12]
    assert bilan["ville"].tolist() == ["Nice", "Lille", "Brest"]
    assert lake.prefixes == [f"raw/osm/date={JOUR}/"]
    assert lake.lectures == [_chemin(3), _chemin(7), _chemin(12)]


def test_elements_sans_nom_sans_effet_sur_le_compte_des_nommes():
    """Pas de `tags`, des `tags` sans `name`, un `name` vide : aucun ne compte comme nommé ;
    ils augmentent seulement le nombre d'éléments."""
    base = [_element("node", 1, "Hôtel A")]
    sans_nom = [
        _element("node", 2),
        {"type": "node", "id": 3, "tags": {"tourism": "hotel"}},
        {"type": "node", "id": 4, "tags": {"name": ""}},
    ]
    seul = bilan_overpass(LakeFactice({_chemin(3): _archive(base)}), JOUR, _coordonnees())
    avec = bilan_overpass(
        LakeFactice({_chemin(3): _archive(base + sans_nom)}), JOUR, _coordonnees()
    )
    assert seul["nommes"].tolist() == [1]
    assert avec["nommes"].tolist() == seul["nommes"].tolist()
    assert avec["elements"].tolist() == [seul["elements"].iloc[0] + 3]


def test_relations_sans_effet_sur_les_points_et_les_contours():
    """Une relation n'est ni un point (`node`) ni un contour (`way`) : elle ne grossit que le
    nombre d'éléments."""
    base = [_element("node", 1), _element("way", 2)]
    relations = [_element("relation", 3), _element("relation", 4)]
    seul = bilan_overpass(LakeFactice({_chemin(7): _archive(base)}), JOUR, _coordonnees())
    avec = bilan_overpass(
        LakeFactice({_chemin(7): _archive(base + relations)}), JOUR, _coordonnees()
    )
    assert avec["points"].tolist() == seul["points"].tolist() == [1]
    assert avec["contours"].tolist() == seul["contours"].tolist() == [1]
    assert avec["elements"].tolist() == [seul["elements"].iloc[0] + 2]


def test_fichiers_non_json_ignores_sans_effet_sur_le_bilan():
    """`hotels.jsonl`, un `.json.tmp` ou un CSV du même dossier ne sont ni lus ni comptés ; leur
    contenu est du texte quelconque, qui ferait échouer l'analyse s'il était lu."""
    archives = {_chemin(7): _archive([_element("node", 1, "Hôtel")])}
    parasites = {
        f"raw/osm/date={JOUR}/hotels.jsonl": b"pas du json",
        f"{_chemin(7)}.tmp": b"pas du json",
        f"raw/osm/date={JOUR}/ville_08.csv": b"pas du json",
    }
    propre = LakeFactice(archives)
    sale = LakeFactice({**parasites, **archives})
    attendu = bilan_overpass(propre, JOUR, _coordonnees())
    obtenu = bilan_overpass(sale, JOUR, _coordonnees())
    pd.testing.assert_frame_equal(obtenu, attendu)
    assert sale.lectures == [_chemin(7)]


def test_archives_des_autres_jours_sans_effet_sur_le_bilan():
    """Seul le dossier du jour demandé est lu, y compris quand un autre jour commence par les
    mêmes caractères (`2026-10-100` ne se confond pas avec `2026-10-10`)."""
    lake = LakeFactice(
        {
            _chemin(7): _archive([_element("node", 1)]),
            _chemin(3, "2026-10-09"): _archive([_element("node", 2), _element("node", 3)]),
            _chemin(12, "2026-10-100"): _archive([_element("way", 4)]),
        }
    )
    bilan = bilan_overpass(lake, JOUR, _coordonnees())
    assert bilan["id_ville"].tolist() == [7]
    assert bilan["elements"].tolist() == [1]
    assert lake.lectures == [_chemin(7)]


def test_ville_absente_des_coordonnees_exclue_sans_effet_sur_les_autres_lignes():
    """La jointure est interne : une archive dont la ville n'est pas dans les coordonnées
    disparaît du bilan, et les lignes des villes connues restent inchangées."""
    objets = {
        _chemin(3): _archive([_element("node", 1)]),
        _chemin(7): _archive([_element("way", 2), _element("way", 3)]),
        _chemin(12): _archive([_element("node", 4, "Hôtel")]),
    }
    complet = bilan_overpass(LakeFactice(objets), JOUR, _coordonnees())
    coordonnees = _coordonnees()
    reduit = bilan_overpass(LakeFactice(objets), JOUR, coordonnees[coordonnees["id_ville"] != 7])
    assert reduit["id_ville"].tolist() == [3, 12]
    attendu = complet[complet["id_ville"] != 7].reset_index(drop=True)
    pd.testing.assert_frame_equal(reduit, attendu)


def test_bilan_ne_modifie_pas_la_table_des_coordonnees():
    """La table passée par le notebook est lue, jamais modifiée (colonnes et valeurs identiques)."""
    coordonnees = _coordonnees()
    avant = coordonnees.copy(deep=True)
    lake = LakeFactice({_chemin(7): _archive([_element("node", 1)])})
    bilan_overpass(lake, JOUR, coordonnees)
    pd.testing.assert_frame_equal(coordonnees, avant)


def test_bilan_laisse_le_lake_intact(tmp_path):
    """Sur un vrai lake local, le bilan ne fait que lire : mêmes fichiers, mêmes octets après."""
    lake = _lake(tmp_path)
    lake.ecrire(_chemin(3), _archive([_element("node", 1, "Hôtel")]))
    lake.ecrire(_chemin(7), _archive([_element("way", 2)], remark="Délai"))
    lake.ecrire(f"raw/osm/date={JOUR}/hotels.jsonl", '{"hotel_id": 1}\n')
    avant = {c: lake.lire(c) for c in lake.lister("")}
    bilan = bilan_overpass(lake, JOUR, _coordonnees())
    apres = {c: lake.lire(c) for c in lake.lister("")}
    assert apres == avant
    assert sorted(bilan["id_ville"].tolist()) == [3, 7]


@pytest.mark.parametrize(
    "objets",
    [
        {},
        {f"raw/osm/date={JOUR}/hotels.jsonl": b"{}"},
        {_chemin(3, "2026-10-09"): _archive([_element("node", 1)])},
    ],
    ids=["lake_vide", "aucun_json", "autre_jour"],
)
def test_jour_sans_archive_leve_une_erreur_de_cle(objets):
    """Comportement actuel : sans archive `.json` pour le jour, le tableau vide n'a pas de colonne
    `id_ville` et la jointure lève KeyError (au lieu d'un bilan vide)."""
    with pytest.raises(KeyError, match="id_ville"):
        bilan_overpass(LakeFactice(objets), JOUR, _coordonnees())


def test_archive_sans_cle_elements_est_refusee():
    """Une réponse sans `elements` (réponse d'erreur d'Overpass, par exemple) n'est pas comptée
    comme vide : l'erreur est signalée."""
    lake = LakeFactice({_chemin(7): json.dumps({"remark": "timeout"}).encode("utf-8")})
    with pytest.raises(KeyError, match="elements"):
        bilan_overpass(lake, JOUR, _coordonnees())


def test_element_sans_type_est_refuse():
    """Un élément qui n'est ni `node` ni `way` mais n'a pas de `type` du tout est refusé."""
    lake = LakeFactice({_chemin(7): _archive([{"id": 1}])})
    with pytest.raises(KeyError, match="type"):
        bilan_overpass(lake, JOUR, _coordonnees())


def test_archive_qui_n_est_pas_du_json_est_refusee():
    """Un fichier `.json` corrompu lève l'erreur d'analyse JSON, il n'est pas sauté en silence."""
    lake = LakeFactice({_chemin(7): b"pas du json"})
    with pytest.raises(json.JSONDecodeError, match="Expecting value"):
        bilan_overpass(lake, JOUR, _coordonnees())


def test_archive_qui_n_est_pas_de_l_utf8_est_refusee():
    """Les archives sont décodées en UTF-8 : des octets invalides lèvent UnicodeDecodeError."""
    lake = LakeFactice({_chemin(7): b"\xff\xfe{}"})
    with pytest.raises(UnicodeDecodeError, match="utf-8"):
        bilan_overpass(lake, JOUR, _coordonnees())


@pytest.mark.parametrize("nom", ["ville_ab.json", "ville_.json", "ville.json", "autre.json"])
def test_archive_sans_numero_de_ville_est_refusee(nom):
    """Le numéro de ville est le nombre qui suit `ville_` : un nom sans nombre est refusé avec
    son nom complet, il ne reçoit pas un numéro inventé."""
    lake = LakeFactice({f"raw/osm/date={JOUR}/{nom}": _archive([_element("node", 1)])})
    with pytest.raises(ValueError, match=f"nom d'archive Overpass inattendu : .*{nom}"):
        bilan_overpass(lake, JOUR, _coordonnees())


@pytest.mark.parametrize("colonne", ["id_ville", "ville"])
def test_coordonnees_sans_colonne_requise_sont_refusees(colonne):
    """La jointure exige `id_ville` et `ville` ; si l'une manque, l'erreur nomme la colonne."""
    lake = LakeFactice({_chemin(7): _archive([_element("node", 1)])})
    with pytest.raises(KeyError, match=colonne):
        bilan_overpass(lake, JOUR, _coordonnees().drop(columns=[colonne]))


def test_archive_dont_les_elements_sont_du_texte_est_refusee():
    """`elements` doit être une liste d'objets : un texte à la place est refusé, il n'est pas
    compté lettre par lettre."""
    lake = LakeFactice({_chemin(7): json.dumps({"elements": "oups"}).encode("utf-8")})
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        bilan_overpass(lake, JOUR, _coordonnees())


def test_archive_dont_les_elements_sont_nuls_est_refusee():
    """`"elements": null` n'est pas une liste vide : l'archive est refusée, pas comptée à zéro."""
    lake = LakeFactice({_chemin(7): json.dumps({"elements": None}).encode("utf-8")})
    with pytest.raises(TypeError, match="has no len"):
        bilan_overpass(lake, JOUR, _coordonnees())


def test_element_qui_n_est_pas_un_objet_json_est_refuse():
    """Chaque élément doit être un objet JSON : un nombre à sa place est refusé."""
    lake = LakeFactice({_chemin(7): _archive([1])})
    with pytest.raises(AttributeError, match="'int' object has no attribute 'get'"):
        bilan_overpass(lake, JOUR, _coordonnees())


def test_archive_qui_est_une_liste_json_est_refusee():
    """Une archive doit être un objet JSON ; une liste n'a pas de clé `elements` à lire."""
    lake = LakeFactice({_chemin(7): json.dumps([1, 2]).encode("utf-8")})
    with pytest.raises(TypeError, match="list indices must be integers"):
        bilan_overpass(lake, JOUR, _coordonnees())


# --- hotels_de ----------------------------------------------------------------------------------


def test_hotels_de_relit_les_hotels_osm_du_jour(tmp_path):
    """Le fichier JSON lines `raw/osm/date=<jour>/hotels.jsonl` devient un tableau, une ligne par
    hôtel, avec ses colonnes et ses types (entier, texte, décimal)."""
    lake = _lake(tmp_path)
    lignes = [
        {"hotel_id": 12345678, "nom": "Hôtel Océan", "note": 8.5},
        {"hotel_id": 2, "nom": "Chez Zoé", "note": 7.0},
    ]
    lake.ecrire(
        f"raw/osm/date={JOUR}/hotels.jsonl",
        "".join(json.dumps(ligne, ensure_ascii=False) + "\n" for ligne in lignes),
    )
    hotels = hotels_de(lake, JOUR)
    assert hotels.columns.tolist() == ["hotel_id", "nom", "note"]
    assert hotels.to_dict("records") == lignes


def test_hotels_de_refuse_un_jour_sans_fichier(tmp_path):
    """Pas de `hotels.jsonl` pour le jour : FileNotFoundError, pas de tableau vide."""
    lake = _lake(tmp_path)
    with pytest.raises(FileNotFoundError, match=r"hotels\.jsonl"):
        hotels_de(lake, JOUR)


def test_hotels_de_ne_lit_ni_booking_ni_un_autre_jour(tmp_path):
    """Seul le dossier OpenStreetMap du jour demandé est lu : des hôtels Booking du même jour ou
    OpenStreetMap d'un autre jour ne le remplacent pas."""
    lake = _lake(tmp_path)
    lake.ecrire(f"raw/booking/date={JOUR}/hotels.jsonl", '{"hotel_id": 1}\n')
    lake.ecrire("raw/osm/date=2026-10-09/hotels.jsonl", '{"hotel_id": 2}\n')
    with pytest.raises(FileNotFoundError, match=r"osm.date=2026-10-10.hotels\.jsonl"):
        hotels_de(lake, JOUR)


def test_hotels_de_refuse_un_fichier_qui_n_est_pas_du_json_lines(tmp_path):
    """Une ligne qui n'est pas du JSON fait échouer la lecture : pas de tableau partiel."""
    lake = _lake(tmp_path)
    lake.ecrire(f"raw/osm/date={JOUR}/hotels.jsonl", "{pas du json}\n")
    with pytest.raises(ValueError, match="Expected object or value"):
        hotels_de(lake, JOUR)


def test_hotels_de_laisse_le_fichier_intact(tmp_path):
    """La lecture ne réécrit pas le fichier : mêmes octets avant et après."""
    lake = _lake(tmp_path)
    chemin = f"raw/osm/date={JOUR}/hotels.jsonl"
    lake.ecrire(chemin, '{"hotel_id": 1, "nom": "A"}\n')
    avant = lake.lire(chemin)
    hotels_de(lake, JOUR)
    assert lake.lire(chemin) == avant


# --- image_allegee ------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_image_allegee_rend_un_png_de_256_couleurs_embarquable(tmp_path, mode):
    """Le résultat est une image IPython (PNG, affichée à 1 000 pixels) dont les données sont un
    vrai PNG en palette de 256 couleurs, même depuis une source qui en compte des milliers ou
    qui porte un canal de transparence."""
    chemin = _png_bruite(tmp_path / "source.png", mode=mode)
    source = ImagePIL.open(chemin).convert("RGB")
    assert source.getcolors(maxcolors=256) is None  # plus de 256 couleurs dans la source

    resultat = image_allegee(chemin, largeur=600)

    assert isinstance(resultat, Image)
    assert resultat.width == 1000
    assert resultat.format == "png"
    assert resultat.data.startswith(SIGNATURE_PNG)
    sortie = ImagePIL.open(io.BytesIO(resultat.data))
    sortie.load()
    assert sortie.format == "PNG"
    assert sortie.mode == "P"
    assert sortie.getcolors(maxcolors=256) is not None
    assert max(sortie.tobytes()) == 255


@pytest.mark.parametrize(
    ("taille", "largeur", "taille_attendue"),
    [
        ((400, 200), None, (1600, 800)),
        ((400, 200), 300, (300, 150)),
        ((300, 100), 200, (200, 67)),
        ((300, 101), 200, (200, 67)),
        ((300, 102), 200, (200, 68)),
        ((300, 99), 200, (200, 66)),
        ((400, 200), 2, (2, 1)),
    ],
)
def test_image_allegee_a_la_largeur_demandee_et_garde_les_proportions(
    tmp_path, taille, largeur, taille_attendue
):
    """La largeur par défaut est 1 600 pixels ; la hauteur est la hauteur d'origine multipliée
    par largeur/largeur d'origine, ARRONDIE au plus proche (66,67 -> 67 ; 67,33 -> 67)."""
    chemin = _damier(tmp_path / "source.png", taille)
    options = {} if largeur is None else {"largeur": largeur}
    resultat = image_allegee(chemin, **options)
    assert ImagePIL.open(io.BytesIO(resultat.data)).size == taille_attendue


def test_image_allegee_refuse_un_fichier_absent(tmp_path):
    """Un PNG introuvable lève FileNotFoundError, avec le nom du fichier."""
    with pytest.raises(FileNotFoundError, match=re.escape("absent.png")):
        image_allegee(tmp_path / "absent.png")


def test_image_allegee_refuse_un_fichier_qui_n_est_pas_une_image(tmp_path):
    """Un fichier de texte nommé `.png` n'est pas une image : Pillow le refuse."""
    faux = tmp_path / "faux.png"
    faux.write_text("ceci n'est pas une image", encoding="utf-8")
    with pytest.raises(UnidentifiedImageError, match="cannot identify image file"):
        image_allegee(faux)


@pytest.mark.parametrize("largeur", [0, -5, 1])
def test_image_allegee_refuse_une_largeur_qui_donne_une_taille_nulle(tmp_path, largeur):
    """Largeur nulle, négative, ou si étroite que la hauteur proportionnelle s'arrondit à 0
    (400x200 réduit à 1 pixel de large : 0,5 -> 0) : l'image impossible est refusée."""
    chemin = _png_bruite(tmp_path / "source.png")
    with pytest.raises(ValueError, match="height and width must be > 0"):
        image_allegee(chemin, largeur=largeur)


def test_image_allegee_refuse_une_largeur_non_entiere(tmp_path):
    """Une largeur en pixels est un entier : 300,5 est refusé, il n'est pas arrondi en silence."""
    chemin = _png_bruite(tmp_path / "source.png")
    with pytest.raises(TypeError, match="cannot be interpreted as an integer"):
        image_allegee(chemin, largeur=300.5)


def test_image_allegee_refuse_une_largeur_en_texte(tmp_path):
    """Une largeur écrite en texte (« 800 ») est refusée, elle n'est pas convertie."""
    chemin = _png_bruite(tmp_path / "source.png")
    with pytest.raises(TypeError, match="unsupported operand"):
        image_allegee(chemin, largeur="800")


def test_image_allegee_reduit_avec_le_filtre_lanczos(tmp_path):
    """Sur un damier d'un pixel, l'image rendue est, pixel pour pixel, celle que Pillow produit
    avec le filtre Lanczos (la quantification est sans perte) et non celle du filtre bicubique."""
    chemin = _damier(tmp_path / "damier.png")
    sortie = ImagePIL.open(io.BytesIO(image_allegee(chemin, largeur=300).data)).convert("RGB")
    with ImagePIL.open(chemin) as source:
        lanczos = source.convert("RGB").resize(sortie.size, ImagePIL.Resampling.LANCZOS)
        bicubique = source.convert("RGB").resize(sortie.size, ImagePIL.Resampling.BICUBIC)
    assert sortie.tobytes() == lanczos.tobytes()
    assert sortie.tobytes() != bicubique.tobytes()


def test_image_allegee_enregistre_un_png_optimise(tmp_path):
    """Le PNG rendu est plus petit que le même PNG en palette enregistré sans optimisation."""
    chemin = _damier(tmp_path / "damier.png")
    resultat = image_allegee(chemin, largeur=1600)
    with ImagePIL.open(chemin) as source:
        palette = (
            source.convert("RGB")
            .resize((1600, 800), ImagePIL.Resampling.LANCZOS)
            .quantize(colors=256, method=ImagePIL.Quantize.MEDIANCUT)
        )
    sans_optimisation = io.BytesIO()
    palette.save(sans_optimisation, format="PNG")
    assert len(resultat.data) < len(sans_optimisation.getvalue())


def test_image_allegee_laisse_la_source_intacte(tmp_path):
    """L'allègement n'écrit que dans la mémoire : le PNG d'origine garde ses octets et aucun
    fichier n'apparaît dans son dossier."""
    chemin = _png_bruite(tmp_path / "source.png")
    avant = _contenu_du_dossier(tmp_path)
    image_allegee(chemin, largeur=300)
    assert _contenu_du_dossier(tmp_path) == avant


# --- afficher -----------------------------------------------------------------------------------


def test_afficher_ne_modifie_pas_l_objet_transmis_une_seule_fois(monkeypatch):
    """L'objet arrive TEL QUEL (même objet, pas une copie) à `display`, appelé une seule fois,
    sans argument en plus, et il n'est pas modifié."""
    recus: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        outils_notebook, "display", lambda *args, **options: recus.append((args, options))
    )
    objet = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    avant = objet.copy(deep=True)

    assert afficher(objet) is None

    assert [options for _, options in recus] == [{}]
    assert [args[0] is objet for args, _ in recus] == [True]
    assert [args[1:] for args, _ in recus] == [()]
    pd.testing.assert_frame_equal(objet, avant)


@pytest.mark.parametrize("arguments", [(), (1, 2)])
def test_afficher_refuse_un_nombre_d_arguments_incorrect_sans_rien_afficher(monkeypatch, arguments):
    """`afficher` prend exactement un objet : aucun ou deux arguments lèvent TypeError et rien
    n'est envoyé à `display`."""
    recus: list[Any] = []
    monkeypatch.setattr(outils_notebook, "display", recus.append)
    # Le message cite le nombre d'arguments, pas seulement le nom de la fonction : sous mutmut,
    # la fonction appelée porte un autre nom, et le refus doit rester vérifiable.
    with pytest.raises(TypeError, match=r"positional argument"):
        afficher(*arguments)
    assert recus == []
