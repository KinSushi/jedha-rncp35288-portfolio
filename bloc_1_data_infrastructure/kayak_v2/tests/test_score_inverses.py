# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests INVERSES du score météo (``kayak.etl.score``) : ce qu'il doit REFUSER ou ne pas toucher.

Le score classe les 35 destinations ; une erreur silencieuse y fausserait le « top 5 » des cartes.
Ce fichier prouve donc, avant tout : qu'une colonne obligatoire manquante est refusée avec un
message exact, qu'une valeur non finie fait échouer le calcul au lieu de produire un rang absurde,
que le tableau d'entrée n'est jamais modifié, et que les colonnes qui ne doivent pas compter
(probabilité de pluie quand l'ensoleillement existe, ensoleillement vide) restent sans effet.
Trois tests directs fixent ensuite les valeurs de référence calculées à la main (moyenne des trois
scores z, écart-type d'échantillon ``ddof=1``, égalités au rang minimum).
"""

import math
import warnings

import pandas as pd
import pytest

from kayak.etl.score import calculer_score_meteo

NAN = float("nan")
COLONNES_SCORE = ["score_meteo", "rang_meteo"]


def _tableau(temperatures, pluies, soleil=None, proba=None, index=None) -> pd.DataFrame:
    """Petit tableau d'agrégats ; les colonnes facultatives ne sont ajoutées que si on les donne."""
    colonnes = {
        "ville": [f"V{i}" for i in range(len(temperatures))],
        "temp_max_moy_c": temperatures,
        "pluie_totale_mm": pluies,
    }
    if soleil is not None:
        colonnes["ensoleillement_moy_h"] = soleil
    if proba is not None:
        colonnes["proba_pluie_moy_pct"] = proba
    return pd.DataFrame(colonnes, index=index)


def _reference() -> pd.DataFrame:
    """Trois villes dont les scores z valent, à la main, [0, -2/3, 2/3] (sans égalité)."""
    return _tableau([10.0, 20.0, 30.0], [0.0, 20.0, 10.0], soleil=[4.0, 2.0, 6.0])


# --------------------------------------------------------------------------------------------
# Refus : colonnes obligatoires et entrées invalides
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("manquante", ["temp_max_moy_c", "pluie_totale_mm"])
def test_colonne_obligatoire_manquante_est_refusee(manquante):
    """Sans température ou sans pluie le score n'a pas de sens : ValueError au message exact."""
    entree = _reference().drop(columns=[manquante])
    with pytest.raises(ValueError, match=rf"^Colonne manquante : {manquante}$"):
        calculer_score_meteo(entree)


def test_deux_colonnes_obligatoires_manquantes_signale_la_temperature_d_abord():
    """Quand les deux colonnes manquent, l'erreur nomme la première de la liste (température)."""
    entree = _reference().drop(columns=["temp_max_moy_c", "pluie_totale_mm"])
    with pytest.raises(ValueError, match=r"^Colonne manquante : temp_max_moy_c$"):
        calculer_score_meteo(entree)


def test_colonne_a_la_casse_differente_est_refusee():
    """Les noms de colonnes sont exacts : « Temp_max_moy_c » n'est pas « temp_max_moy_c »."""
    entree = _reference().rename(columns={"temp_max_moy_c": "Temp_max_moy_c"})
    with pytest.raises(ValueError, match=r"^Colonne manquante : temp_max_moy_c$"):
        calculer_score_meteo(entree)


@pytest.mark.parametrize(
    "tableau",
    [
        _tableau([10.0, NAN, 30.0], [0.0, 20.0, 10.0], soleil=[4.0, 2.0, 6.0]),
        _tableau([10.0, 20.0, 30.0], [0.0, NAN, 10.0], soleil=[4.0, 2.0, 6.0]),
        _tableau([10.0, 20.0, 30.0], [0.0, 20.0, 10.0], soleil=[4.0, NAN, 6.0]),
        _tableau([10.0, 20.0, 30.0], [0.0, 20.0, 10.0], proba=[5.0, NAN, 60.0]),
    ],
    ids=["temperature", "pluie", "ensoleillement", "probabilite_de_pluie"],
)
def test_valeur_manquante_dans_une_ville_fait_echouer_le_calcul(tableau):
    """Une ville sans mesure ferait un score NaN : le calcul échoue au lieu de lui donner un rang.

    Le rang entier ne peut pas représenter NaN ; mieux vaut une erreur franche qu'un classement
    qui placerait cette ville au hasard dans le top 5.
    """
    with pytest.raises(ValueError, match="non-finite"):
        calculer_score_meteo(tableau)


@pytest.mark.parametrize("entree", [None, 42, "texte"], ids=["None", "entier", "texte"])
def test_entree_qui_n_est_pas_un_tableau_est_refusee(entree):
    """Seul un DataFrame est accepté : toute autre valeur est refusée dès la copie."""
    with pytest.raises(AttributeError, match="copy"):
        calculer_score_meteo(entree)


# --------------------------------------------------------------------------------------------
# Entrée jamais modifiée
# --------------------------------------------------------------------------------------------


def test_colonne_manquante_laisse_l_entree_inchangee():
    """Un calcul refusé ne doit pas avoir abîmé le tableau de l'appelant."""
    entree = _reference().drop(columns=["pluie_totale_mm"])
    entree_avant = entree.copy()
    with pytest.raises(ValueError, match="pluie_totale_mm"):
        calculer_score_meteo(entree)
    pd.testing.assert_frame_equal(entree, entree_avant)


@pytest.mark.parametrize(
    "entree",
    [
        _reference(),
        _tableau([10.0, 20.0, 30.0], [0.0, 20.0, 10.0]),
        _tableau([10.0, 10.0, 10.0], [5.0, 5.0, 5.0], soleil=[3.0, 3.0, 3.0]),
    ],
    ids=["cas_normal", "branche_neutre", "ecart_type_nul"],
)
def test_calcul_reussi_laisse_l_entree_inchangee(entree):
    """Le score est calculé sur une copie : l'entrée garde ses colonnes, ses valeurs, son index."""
    entree_avant = entree.copy()
    resultat = calculer_score_meteo(entree)
    pd.testing.assert_frame_equal(entree, entree_avant)
    assert resultat is not entree
    assert not set(COLONNES_SCORE) & set(entree.columns)


def test_rejouer_le_calcul_sur_sa_propre_sortie_laisse_le_resultat_inchange():
    """Les colonnes de score déjà présentes sont remplacées, jamais dupliquées ni cumulées."""
    premier = calculer_score_meteo(_reference())
    second = calculer_score_meteo(premier)
    pd.testing.assert_frame_equal(second, premier)
    assert list(second.columns) == list(premier.columns)


def test_colonnes_et_ordre_des_lignes_de_l_entree_restent_inchanges_dans_le_resultat():
    """Le résultat n'est pas trié : mêmes lignes dans le même ordre, mêmes colonnes d'abord."""
    entree = _tableau([30.0, 10.0, 20.0], [10.0, 0.0, 20.0], soleil=[6.0, 4.0, 2.0])
    resultat = calculer_score_meteo(entree)
    assert list(resultat.columns) == [*entree.columns, *COLONNES_SCORE]
    assert resultat["ville"].tolist() == entree["ville"].tolist()
    pd.testing.assert_frame_equal(resultat[list(entree.columns)], entree)


# --------------------------------------------------------------------------------------------
# Colonnes sans effet et branche neutre
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proba",
    [None, [NAN, NAN, NAN], [90.0, 5.0, 40.0]],
    ids=["colonne_absente", "toute_nan", "autres_valeurs"],
)
def test_probabilite_de_pluie_est_sans_effet_quand_l_ensoleillement_existe(proba):
    """L'ensoleillement a priorité : la probabilité de pluie, quelle qu'elle soit, ne compte pas."""
    reference = calculer_score_meteo(_reference())
    avec_proba = _reference()
    if proba is not None:
        avec_proba["proba_pluie_moy_pct"] = proba
    resultat = calculer_score_meteo(avec_proba)
    pd.testing.assert_frame_equal(resultat[COLONNES_SCORE], reference[COLONNES_SCORE])


def test_ensoleillement_entierement_nan_est_sans_effet_sur_le_repli():
    """Une colonne d'ensoleillement vide est écartée : même résultat que si elle n'existait pas."""
    sans_colonne = _tableau([10.0, 20.0, 30.0], [0.0, 20.0, 10.0], proba=[10.0, 60.0, 30.0])
    avec_colonne_vide = sans_colonne.assign(ensoleillement_moy_h=[NAN, NAN, NAN])
    attendu = calculer_score_meteo(sans_colonne)
    resultat = calculer_score_meteo(avec_colonne_vide)
    pd.testing.assert_frame_equal(resultat[COLONNES_SCORE], attendu[COLONNES_SCORE])


@pytest.mark.parametrize(
    "temperatures, pluies",
    [([10.0, 20.0, 30.0], [0.0, 20.0, 10.0]), ([35.0, 5.0, 15.0], [90.0, 0.0, 30.0])],
    ids=["premiere_serie", "seconde_serie"],
)
def test_sans_ensoleillement_ni_probabilite_temperature_et_pluie_sont_sans_effet(
    temperatures, pluies
):
    """Faute de donnée de soleil, le score est neutre (0.0, rang 1) quelles que soient les valeurs.

    Aucune composante n'est comparée dans ce cas : deux séries très différentes donnent
    exactement le même résultat (comportement du code, signalé dans le rapport).
    """
    resultat = calculer_score_meteo(_tableau(temperatures, pluies))
    assert resultat["score_meteo"].tolist() == [0.0, 0.0, 0.0]
    assert resultat["rang_meteo"].tolist() == [1, 1, 1]


def test_ensoleillement_et_probabilite_vides_sont_sans_effet_sur_le_resultat_neutre():
    """Deux colonnes présentes mais vides valent « aucune donnée » : même résultat que sans elles
    (score 0.0, rang 1)."""
    sans_colonnes = _tableau([10.0, 20.0, 30.0], [0.0, 20.0, 10.0])
    avec_colonnes_vides = sans_colonnes.assign(
        ensoleillement_moy_h=[NAN] * 3, proba_pluie_moy_pct=[NAN] * 3
    )
    attendu = calculer_score_meteo(sans_colonnes)
    resultat = calculer_score_meteo(avec_colonnes_vides)
    pd.testing.assert_frame_equal(resultat[COLONNES_SCORE], attendu[COLONNES_SCORE])
    assert resultat["score_meteo"].tolist() == [0.0, 0.0, 0.0]
    assert resultat["rang_meteo"].tolist() == [1, 1, 1]


# --------------------------------------------------------------------------------------------
# Valeurs de référence (tests directs)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tableau, scores, rangs",
    [
        pytest.param(
            _reference(), [0.0, -2 / 3, 2 / 3], [2, 3, 1], id="trois_villes_sans_egalite"
        ),
        pytest.param(
            _tableau([20.0, 10.0, 30.0], [30.0, 10.0, 20.0], soleil=[2.0, 6.0, 4.0]),
            [-2 / 3, 1 / 3, 1 / 3],
            [3, 1, 1],
            id="egalite_au_premier_rang",
        ),
        pytest.param(
            _tableau([10.0, 20.0], [0.0, 0.0], soleil=[1.0, 1.0]),
            [-math.sqrt(2) / 6, math.sqrt(2) / 6],
            [2, 1],
            id="deux_villes_ecart_type_d_echantillon",
        ),
        pytest.param(_tableau([18.0], [3.0], soleil=[7.0]), [0.0], [1], id="une_seule_ville"),
        pytest.param(
            _tableau([10.0] * 3, [5.0] * 3, soleil=[3.0] * 3),
            [0.0, 0.0, 0.0],
            [1, 1, 1],
            id="ecart_type_nul_partout",
        ),
        pytest.param(
            _tableau([10.0, 20.0, 30.0], [0.0, 20.0, 10.0], proba=[30.0, 10.0, 20.0]),
            [-1 / 3, 0.0, 1 / 3],
            [3, 2, 1],
            id="repli_sur_la_probabilite_de_pluie",
        ),
    ],
)
def test_valeurs_de_reference_calculees_a_la_main(tableau, scores, rangs):
    """Score = moyenne des trois z (écart-type ``ddof=1``) ; rang 1 = meilleur, égalités au min.

    Aucun avertissement numérique ne doit être émis, y compris quand l'écart-type est nul.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resultat = calculer_score_meteo(tableau)
    assert resultat["score_meteo"].tolist() == pytest.approx(scores, abs=1e-12)
    assert resultat["rang_meteo"].tolist() == rangs
    assert pd.api.types.is_float_dtype(resultat["score_meteo"])
    assert pd.api.types.is_integer_dtype(resultat["rang_meteo"])


@pytest.mark.parametrize(
    "tableau, scores",
    [
        pytest.param(
            _tableau([10.0, 20.0, 30.0], [5.0] * 3, soleil=[3.0] * 3),
            [-1 / 3, 0.0, 1 / 3],
            id="chaleur_plus",
        ),
        pytest.param(
            _tableau([10.0] * 3, [10.0, 20.0, 30.0], soleil=[3.0] * 3),
            [1 / 3, 0.0, -1 / 3],
            id="pluie_moins",
        ),
        pytest.param(
            _tableau([10.0] * 3, [5.0] * 3, soleil=[1.0, 2.0, 3.0]),
            [-1 / 3, 0.0, 1 / 3],
            id="ensoleillement_plus",
        ),
        pytest.param(
            _tableau([10.0] * 3, [5.0] * 3, proba=[10.0, 20.0, 30.0]),
            [1 / 3, 0.0, -1 / 3],
            id="probabilite_de_pluie_moins",
        ),
        pytest.param(
            _tableau([10.0] * 3, [5.0] * 3, soleil=[NAN] * 3, proba=[10.0, 20.0, 30.0]),
            [1 / 3, 0.0, -1 / 3],
            id="probabilite_de_pluie_quand_le_soleil_est_vide",
        ),
    ],
)
def test_chaque_composante_pese_un_tiers_avec_son_signe(tableau, scores):
    """Une seule composante varie : elle compte pour 1/3 du score, avec son signe (chaleur +,
    soleil +, pluie -, probabilité de pluie -)."""
    resultat = calculer_score_meteo(tableau)
    assert resultat["score_meteo"].tolist() == pytest.approx(scores, abs=1e-12)


def test_index_de_l_entree_reste_inchange_et_chaque_ville_garde_son_rang():
    """L'index n'est pas remis à zéro : chaque score reste aligné sur la ligne de sa ville."""
    entree = _reference()
    entree.index = [10, 20, 30]
    resultat = calculer_score_meteo(entree)
    assert resultat.index.tolist() == [10, 20, 30]
    assert resultat.loc[10, "rang_meteo"] == 2
    assert resultat.loc[20, "rang_meteo"] == 3
    assert resultat.loc[30, "rang_meteo"] == 1
    neutre = calculer_score_meteo(_tableau([1.0, 2.0], [3.0, 4.0]))
    assert pd.api.types.is_integer_dtype(neutre["rang_meteo"])
