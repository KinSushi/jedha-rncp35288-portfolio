# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Jeu de données : lecture économe, conversion au format de l'API, préparation (2.1, 4.1).

Les tests inverses prouvent qu'un fichier absent, une colonne manquante, une date illisible ou un
jeu sans ligne valide sont refusés avec des messages exacts, que chaque rejet est compté par
``champ:code`` et que ni le fichier lu ni le tableau reçu ne sont modifiés par la lecture.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

import detection_fraude.jeu_donnees as jeu_module
from detection_fraude.jeu_donnees import (
    COLONNES_API,
    lignes_depuis_jeu,
    lire_jeu_brut,
    preparer_jeu_entrainement,
)
from detection_fraude.masquage import HacheurCarte
from detection_fraude.schema import valider_brut
from tests.helpers import ligne_brute

COLONNE_DATE = "trans_date_trans_time"
JOUR_A = "2026-10-09"
JOUR_B = "2026-10-10"
JOUR_C = "2026-10-11"
CHAMPS_SENSIBLES = {
    "cc_num",
    "first",
    "last",
    "street",
    "dob",
    "lat",
    "long",
    "zip",
    "city",
    "job",
    "gender",
}
COLONNES_SORTIE = {
    "version_schema",
    "trans_num",
    "horodatage_s",
    "carte_hash",
    "carte_fin",
    "age",
    "distance_km",
    "source",
    "commercant",
    "categorie",
    "montant",
    "etat",
    "population_ville",
    "lat_commercant",
    "long_commercant",
    "etiquette_source",
    "heure",
    "jour_semaine",
    "nb_tx_24h",
    "montant_relatif",
}


# --------------------------------------------------------------------------------------------
# Fabriques
# --------------------------------------------------------------------------------------------
def ecrire_csv(chemin, jours: list[str]):
    """CSV minimal : la colonne des dates et un numéro de ligne (pour reconnaître chaque ligne)."""
    lignes = [f"{COLONNE_DATE},numero", *[f"{jour} 10:00:00,{n}" for n, jour in enumerate(jours)]]
    chemin.write_bytes(("\n".join(lignes) + "\n").encode("utf-8"))
    return chemin


def enregistrement(n: int, jour: str) -> dict[str, Any]:
    return {COLONNE_DATE: f"{jour} 10:00:00", "numero": n}


def espionner_lecture_csv(monkeypatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Enregistre chaque appel à ``pandas.read_csv`` (arguments exacts) sans le modifier."""
    appels: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    lecteur = pd.read_csv

    def espion(*args, **options):
        appels.append((args, options))
        return lecteur(*args, **options)

    monkeypatch.setattr(pd, "read_csv", espion)
    return appels


def tableau(
    nombre: int,
    surcharges: dict[int, dict[str, Any]] | None = None,
    dates: dict[int, str] | None = None,
) -> pd.DataFrame:
    """Tableau au format de ``fraudTest.csv`` : une ligne valide par rang, avec surcharges."""
    if nombre == 0:
        return tableau(1).iloc[0:0]
    enregistrements = []
    for rang in range(nombre):
        ligne = ligne_brute(rang, **(surcharges or {}).get(rang, {}))
        ligne_csv: dict[str, Any] = {
            COLONNE_DATE: (dates or {}).get(rang, f"2026-10-09 10:00:{rang:02d}")
        }
        ligne_csv.update({colonne: ligne[colonne] for colonne in COLONNES_API})
        enregistrements.append(ligne_csv)
    return pd.DataFrame(enregistrements)


def epoch(texte: str) -> int:
    return int(datetime.fromisoformat(texte).replace(tzinfo=UTC).timestamp())


# --------------------------------------------------------------------------------------------
# lire_jeu_brut
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("jour", "limite"), [(None, None), (JOUR_A, None), (JOUR_A, 3), ("", 2)])
def test_un_jeu_absent_est_refuse_avec_la_commande_de_telechargement(tmp_path, jour, limite):
    """Sans fichier, l'erreur dit lequel manquait et comment l'obtenir ; rien n'est créé."""
    chemin = tmp_path / "absent.csv"
    with pytest.raises(FileNotFoundError) as exc:
        lire_jeu_brut(chemin, jour=jour, limite=limite)
    assert str(exc.value) == (
        f"Jeu de données introuvable : {chemin}. "
        "Le télécharger avec : python -m detection_fraude.telechargement"
    )
    assert not chemin.exists()


@pytest.mark.parametrize("jour", [None, ""])
def test_sans_jour_le_fichier_est_lu_en_entier_ou_jusqu_a_la_limite_et_reste_inchange(
    tmp_path, monkeypatch, jour
):
    """Pas de jour (ou jour vide) : lecture simple, bornée par ``limite`` ; fichier intact."""
    chemin = ecrire_csv(tmp_path / "jeu.csv", [JOUR_A, JOUR_A, JOUR_B, JOUR_C])
    octets_avant = chemin.read_bytes()
    appels = espionner_lecture_csv(monkeypatch)
    tout = lire_jeu_brut(chemin, jour=jour)
    limite = lire_jeu_brut(chemin, jour=jour, limite=3)
    assert tout.to_dict("records") == [
        enregistrement(0, JOUR_A),
        enregistrement(1, JOUR_A),
        enregistrement(2, JOUR_B),
        enregistrement(3, JOUR_C),
    ]
    assert limite.to_dict("records") == tout.to_dict("records")[:3]
    assert appels == [((chemin,), {"nrows": None}), ((chemin,), {"nrows": 3})]
    assert chemin.read_bytes() == octets_avant


@pytest.mark.parametrize(
    ("jours", "jour", "positions"),
    [
        ([JOUR_A, JOUR_A, JOUR_B, JOUR_B, JOUR_B, JOUR_C], JOUR_A, (0, 2)),
        ([JOUR_A, JOUR_A, JOUR_B, JOUR_B, JOUR_B, JOUR_C], JOUR_B, (2, 5)),
        ([JOUR_A, JOUR_A, JOUR_B, JOUR_B, JOUR_B, JOUR_C], JOUR_C, (5, 6)),
        ([JOUR_A, JOUR_B, JOUR_C], JOUR_B, (1, 2)),
        ([JOUR_B], JOUR_B, (0, 1)),
    ],
)
def test_un_jour_contigu_est_lu_par_saut_de_lignes_sans_lire_le_reste_et_le_fichier_reste_inchange(
    tmp_path, monkeypatch, jours, jour, positions
):
    """Deux lectures économes : la colonne des dates, puis seulement les lignes du jour."""
    chemin = ecrire_csv(tmp_path / "jeu.csv", jours)
    octets_avant = chemin.read_bytes()
    appels = espionner_lecture_csv(monkeypatch)
    df = lire_jeu_brut(chemin, jour=jour)
    debut, fin = positions
    assert df.to_dict("records") == [enregistrement(n, jours[n]) for n in range(debut, fin)]
    assert list(df.index) == list(range(fin - debut))
    assert appels == [
        ((chemin,), {"usecols": [COLONNE_DATE]}),
        ((chemin,), {"skiprows": range(1, debut + 1), "nrows": fin - debut}),
    ]
    assert chemin.read_bytes() == octets_avant


@pytest.mark.parametrize(
    ("jours", "attendus"),
    [
        ([JOUR_A, JOUR_B, JOUR_A], [0, 2]),
        ([JOUR_B, JOUR_A, JOUR_B, JOUR_A, JOUR_B], [1, 3]),
        ([JOUR_A, JOUR_A, JOUR_B, JOUR_A], [0, 1, 3]),
    ],
)
def test_un_jour_non_contigu_se_lit_par_tranches_sans_rien_perdre_ni_melanger(
    tmp_path, monkeypatch, jours, attendus
):
    """Fichier mal trié : repli sur une lecture par tranches de 100 000 lignes, filtrées."""
    chemin = ecrire_csv(tmp_path / "jeu.csv", jours)
    octets_avant = chemin.read_bytes()
    appels = espionner_lecture_csv(monkeypatch)
    df = lire_jeu_brut(chemin, jour=JOUR_A)
    assert df.to_dict("records") == [enregistrement(n, jours[n]) for n in attendus]
    assert list(df.index) == list(range(len(attendus)))
    assert appels == [
        ((chemin,), {"usecols": [COLONNE_DATE]}),
        ((chemin,), {"chunksize": 100_000}),
    ]
    assert chemin.read_bytes() == octets_avant


@pytest.mark.parametrize(
    ("jours", "limite", "attendus"),
    [
        ([JOUR_A, JOUR_A, JOUR_A, JOUR_B], 2, [0, 1]),
        ([JOUR_A, JOUR_A, JOUR_A, JOUR_B], 1, [0]),
        ([JOUR_A, JOUR_A, JOUR_A, JOUR_B], 3, [0, 1, 2]),
        ([JOUR_A, JOUR_A, JOUR_A, JOUR_B], 10, [0, 1, 2]),
        ([JOUR_A, JOUR_A, JOUR_A, JOUR_B], None, [0, 1, 2]),
        ([JOUR_A, JOUR_B, JOUR_A, JOUR_A], 2, [0, 2]),
        ([JOUR_A, JOUR_B, JOUR_A, JOUR_A], None, [0, 2, 3]),
        ([JOUR_A, JOUR_A, JOUR_A, JOUR_B], 0, [0, 1, 2]),  # zéro = pas de limite (contigu)
        ([JOUR_A, JOUR_B, JOUR_A, JOUR_A], 0, [0, 2, 3]),  # zéro = pas de limite (par tranches)
    ],
)
def test_la_limite_s_applique_apres_le_filtre_du_jour_dans_les_deux_modes_de_lecture(
    tmp_path, jours, limite, attendus
):
    """``limite`` borne les lignes du jour (pas du fichier) ; plus grande, elle est sans effet."""
    chemin = ecrire_csv(tmp_path / "jeu.csv", jours)
    octets_avant = chemin.read_bytes()
    df = lire_jeu_brut(chemin, jour=JOUR_A, limite=limite)
    assert df.to_dict("records") == [enregistrement(n, jours[n]) for n in attendus]
    assert list(df.index) == list(range(len(attendus)))
    assert chemin.read_bytes() == octets_avant


@pytest.mark.parametrize("contenu", [[], [JOUR_B, JOUR_B], [JOUR_A[:-1] + "8"]])
def test_un_jour_absent_donne_un_tableau_vide_qui_garde_les_colonnes(
    tmp_path, monkeypatch, contenu
):
    """Aucune ligne ce jour-là : tableau vide, mêmes colonnes, lecture à zéro ligne."""
    chemin = ecrire_csv(tmp_path / "jeu.csv", contenu)
    octets_avant = chemin.read_bytes()
    appels = espionner_lecture_csv(monkeypatch)
    df = lire_jeu_brut(chemin, jour=JOUR_A)
    assert list(df.columns) == [COLONNE_DATE, "numero"]
    assert df.to_dict("records") == []
    assert appels[1:] == [((chemin,), {"nrows": 0})]
    assert chemin.read_bytes() == octets_avant


def test_un_fichier_sans_colonne_de_date_est_refuse_quand_on_filtre_par_jour(tmp_path):
    """Filtrer par jour exige la colonne des dates : ValueError de pandas, fichier intact."""
    chemin = tmp_path / "jeu.csv"
    chemin.write_bytes(b"autre,numero\nx,1\n")
    octets_avant = chemin.read_bytes()
    with pytest.raises(ValueError) as exc:
        lire_jeu_brut(chemin, jour=JOUR_A)
    assert COLONNE_DATE in str(exc.value)
    assert chemin.read_bytes() == octets_avant
    assert lire_jeu_brut(chemin).to_dict("records") == [{"autre": "x", "numero": 1}]


# --------------------------------------------------------------------------------------------
# lignes_depuis_jeu et conversion des dates
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("texte", "millisecondes"),
    [
        ("1970-01-01 00:00:00", 0),
        ("1970-01-01 00:00:01", 1000),
        ("1969-12-31 23:59:59", -1000),
        ("2000-01-01 00:00:00", 946_684_800_000),
        ("2020-06-21 12:34:56", 1_592_742_896_000),
        ("2020-06-21 12:34:56.789", 1_592_742_896_789),
        ("2020-06-21 12:34:56.7899", 1_592_742_896_789),
        ("1969-12-31 23:59:59.9999", -1),
        ("2026-10-09 12:00:00", 1_791_547_200_000),
        ("2026-10-09 23:59:59", 1_791_590_399_000),
        ("pas une date", None),
        ("2026-13-45 00:00:00", None),
        ("", None),
    ],
)
def test_les_dates_sont_lues_comme_utc_en_millisecondes_exactes_et_une_date_illisible_est_refusee(
    texte, millisecondes
):
    """``current_time`` est l'époque UTC en millisecondes ; une date impossible lève ValueError."""
    df = tableau(1, dates={0: texte})
    df_avant = df.copy()
    if millisecondes is None:
        with pytest.raises(ValueError):
            list(lignes_depuis_jeu(df))
    else:
        (ligne,) = list(lignes_depuis_jeu(df))
        assert ligne["current_time"] == millisecondes
        assert type(ligne["current_time"]) is int
    assert df.equals(df_avant)


def test_une_ligne_est_au_format_de_l_api_en_types_natifs_et_le_tableau_reste_inchange():
    """Clés de l'API plus ``current_time`` ; colonnes parasites écartées ; types natifs."""
    df = tableau(3)
    df.insert(0, "Unnamed: 0", range(3))
    df["unix_time"] = 1_371_816_865
    df_avant = df.copy()
    colonnes_avant = list(df.columns)
    lignes = list(lignes_depuis_jeu(df))
    assert [list(ligne) for ligne in lignes] == [[*COLONNES_API, "current_time"]] * 3
    assert lignes[1]["trans_num"] == ligne_brute(1)["trans_num"]
    assert lignes[1]["cc_num"] == 4_929_000_000_000_001
    assert lignes[1]["current_time"] == epoch("2026-10-09 10:00:01") * 1000
    assert {type(lignes[1][c]) for c in ("cc_num", "zip", "city_pop", "is_fraud")} == {int}
    assert {type(lignes[1][c]) for c in ("amt", "lat", "merch_long")} == {float}
    assert {type(lignes[1][c]) for c in ("merchant", "dob", "trans_num")} == {str}
    assert all(valider_brut(ligne) == [] for ligne in lignes)
    assert list(df.columns) == colonnes_avant
    assert df.equals(df_avant)


@pytest.mark.parametrize(
    ("taille", "nombre"), [(1, 4), (3, 8), (3, 6), (3, 3), (3, 2), (5, 0), (2, 1)]
)
def test_les_lignes_sortent_dans_l_ordre_par_tranches_sans_perte_ni_doublon_tableau_intact(
    monkeypatch, taille, nombre
):
    """Quelle que soit la taille de tranche, toutes les lignes sortent une fois, dans l'ordre."""
    monkeypatch.setattr(jeu_module, "TAILLE_TRANCHE", taille)
    df = tableau(nombre)
    df_avant = df.copy()
    lignes = list(lignes_depuis_jeu(df))
    assert [ligne["trans_num"] for ligne in lignes] == [
        ligne_brute(n)["trans_num"] for n in range(nombre)
    ]
    assert df.equals(df_avant)


def test_une_colonne_manquante_est_refusee_des_la_premiere_ligne_et_le_tableau_reste_inchange():
    """Sans la date ou sans un champ de l'API, le flux échoue (KeyError) au lieu de combler."""
    sans_date = tableau(2).drop(columns=[COLONNE_DATE])
    sans_carte = tableau(2).drop(columns=["cc_num"])
    avant_date, avant_carte = sans_date.copy(), sans_carte.copy()
    with pytest.raises(KeyError) as exc_date:
        list(lignes_depuis_jeu(sans_date))
    assert str(exc_date.value) == f"'{COLONNE_DATE}'"
    with pytest.raises(KeyError) as exc_carte:
        list(lignes_depuis_jeu(sans_carte))
    assert "cc_num" in str(exc_carte.value)
    assert sans_date.equals(avant_date)
    assert sans_carte.equals(avant_carte)


# --------------------------------------------------------------------------------------------
# preparer_jeu_entrainement
# --------------------------------------------------------------------------------------------
SURCHARGES_REJETS = {
    1: {"amt": "abc"},
    2: {"amt": -5.0},
    3: {"gender": "X"},
    4: {"amt": "abc", "gender": "X"},
    9: {"cc_num": "carte-invalide"},
}
DATES_REJETS = {6: "1999-12-31 23:59:59", 7: "2000-01-01 00:00:00", 8: "2999-01-01 00:00:00"}


def test_chaque_rejet_est_compte_par_champ_et_code_et_les_lignes_valides_sortent_masquees(hacheur):
    """Décompte « champ:code » exact ; seules les lignes valides passent, sans identifiant."""
    df = tableau(10, SURCHARGES_REJETS, DATES_REJETS)
    df_avant = df.copy()
    sortie, rejets = preparer_jeu_entrainement(df, hacheur)
    assert isinstance(rejets, Counter)
    assert rejets == Counter(
        {
            "amt:type_invalide": 2,
            "amt:hors_bornes": 1,
            "gender:valeur_inconnue": 2,
            "current_time:hors_bornes": 2,
            "cc_num:type_invalide": 1,
        }
    )
    assert df.equals(df_avant)
    assert list(sortie["trans_num"]) == [ligne_brute(n)["trans_num"] for n in (0, 5, 7)]
    assert list(sortie.index) == [0, 1, 2]
    assert set(sortie.columns) == COLONNES_SORTIE
    assert not CHAMPS_SENSIBLES & set(sortie.columns)
    assert list(sortie["source"]) == ["jeu", "jeu", "jeu"]
    assert list(sortie["etiquette_source"]) == [0, 0, 0]
    assert list(sortie["montant"]) == [20.0, 37.5, 44.5]
    assert list(sortie["carte_fin"]) == ["0000", "0005", "0007"]
    assert list(sortie["carte_hash"]) == [
        hacheur.empreinte(4_929_000_000_000_000 + n) for n in (0, 5, 7)
    ]
    assert list(sortie["horodatage_s"]) == [
        epoch("2026-10-09 10:00:00"),
        epoch("2026-10-09 10:00:05"),
        946_684_800,
    ]
    assert list(sortie["heure"]) == [10, 10, 0]
    assert list(sortie["jour_semaine"]) == [4, 4, 5]


@pytest.mark.parametrize(
    ("nombre", "invalides", "tranches"),
    [(10, (2, 5, 8), [3, 3, 1]), (8, (2, 5), [3, 3]), (5, (1, 3), [3]), (4, (0, 3), [2])],
)
def test_les_lignes_valides_sont_regroupees_par_tranches_de_la_taille_prevue_sans_tranche_vide(
    hacheur, monkeypatch, nombre, invalides, tranches
):
    """Seules les lignes valides remplissent une tranche ; pas de tranche vide en fin de lot."""
    monkeypatch.setattr(jeu_module, "TAILLE_TRANCHE", 3)
    surcharges = {rang: {"amt": "abc"} for rang in invalides}
    appels: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    concatener = pd.concat

    def espion(*args, **options):
        appels.append((args, options))
        return concatener(*args, **options)

    monkeypatch.setattr(pd, "concat", espion)
    df = tableau(nombre, surcharges)
    df_avant = df.copy()
    sortie, rejets = preparer_jeu_entrainement(df, hacheur)
    assert [len(t) for t in appels[0][0][0]] == tranches
    assert appels[0][1] == {"ignore_index": True}
    assert rejets == Counter({"amt:type_invalide": len(invalides)})
    valides = [n for n in range(nombre) if n not in invalides]
    assert list(sortie.index) == list(range(len(valides)))
    assert list(sortie["trans_num"]) == [ligne_brute(n)["trans_num"] for n in valides]
    assert df.equals(df_avant)


@pytest.mark.parametrize(
    ("surcharges", "rejets"),
    [
        ({0: {"amt": "abc"}, 1: {"gender": "X"}, 2: {"dob": "n/a"}}, 3),
        ({0: {"amt": -1.0}}, 1),
    ],
)
def test_un_jeu_sans_aucune_ligne_valide_est_refuse_avec_le_nombre_de_rejets(
    hacheur, surcharges, rejets
):
    """Entraîner sur rien est une erreur franche : le message donne le nombre de rejets."""
    df = tableau(len(surcharges), surcharges)
    df_avant = df.copy()
    with pytest.raises(ValueError) as exc:
        preparer_jeu_entrainement(df, hacheur)
    assert str(exc.value) == f"Aucune ligne valide dans le jeu ({rejets} rejet(s))."
    assert df.equals(df_avant)


def test_un_tableau_vide_est_refuse_comme_un_jeu_sans_ligne_valide(hacheur):
    """Zéro ligne, zéro rejet : même refus, message à zéro."""
    df = tableau(2).iloc[0:0]
    with pytest.raises(ValueError) as exc:
        preparer_jeu_entrainement(df, hacheur)
    assert str(exc.value) == "Aucune ligne valide dans le jeu (0 rejet(s))."


def test_la_cle_de_hachage_change_l_empreinte_mais_pas_le_reste_et_le_tableau_reste_inchange(
    hacheur,
):
    """Deux clés donnent deux empreintes de carte distinctes, tout le reste est identique."""
    df = tableau(4)
    df_avant = df.copy()
    autre = HacheurCarte(b"une-autre-cle-de-test-0123456789", 32)
    sortie, rejets = preparer_jeu_entrainement(df, hacheur)
    sortie_autre, rejets_autre = preparer_jeu_entrainement(df, autre)
    assert rejets == rejets_autre == Counter()
    assert list(sortie["carte_hash"]) != list(sortie_autre["carte_hash"])
    assert sortie.drop(columns=["carte_hash"]).equals(sortie_autre.drop(columns=["carte_hash"]))
    assert list(sortie["carte_hash"]) == [
        hacheur.empreinte(4_929_000_000_000_000 + n) for n in range(4)
    ]
    assert df.equals(df_avant)
