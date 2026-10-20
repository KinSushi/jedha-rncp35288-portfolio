# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests des fonctions du notebook Tinder (`speed_dating.py`).

Chaque test calcule le résultat attendu À LA MAIN (petit tableau, formule écrite en clair) :
on compare le code à l'arithmétique, pas à lui-même. Les tests dont le nom dit « refuse »,
« leve » ou « inchange » vérifient qu'une entrée interdite est refusée, ou que l'entrée n'a pas
été modifiée : pour chaque comportement attendu, il y a un comportement interdit.
"""

from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
import pytest
import statsmodels.formula.api as smf
from patsy import PatsyError
from scipy import stats

import speed_dating as sd


def complet(message: str) -> str:
    """Motif qui n'accepte que ce message, en entier (pas un morceau de message)."""
    return "^" + re.escape(message) + "$"


PROTEGEES = "colonne(s) protégée(s) par le RGPD, non reprises : "
# ------------------------------------------------------------------------------------------
# Nettoyage
# ------------------------------------------------------------------------------------------


def test_convertir_en_nombres_retire_milliers_et_decimales_fixes_et_rend_nan_si_illisible() -> None:
    entree = pd.Series(
        ["12,345.00", "7", "0.50", "1,000,000.00", "2.005", "abc", "", "12 000", None]
    )
    resultat = sd.convertir_en_nombres(entree)
    # « 2.005 » reste 2,005 : retirer « .00 » du texte en aurait fait 25.
    assert resultat.iloc[:5].tolist() == [12345.0, 7.0, 0.5, 1000000.0, 2.005]
    assert resultat.iloc[5:].isna().all()  # ce qui n'est pas un nombre devient NaN, sans exception


def test_convertir_en_nombres_laisse_la_serie_d_entree_inchangee() -> None:
    entree = pd.Series(["1,000.00", "x"])
    avant = entree.copy()
    sd.convertir_en_nombres(entree)
    pd.testing.assert_series_equal(entree, avant)


def test_normaliser_a_100_ramene_chaque_ligne_a_cent_points_et_ajoute_ses_colonnes() -> None:
    donnees = pd.DataFrame(
        {"x": [5.0, 6.0, 7.0], "a": [10.0, 1.0, 0.1], "b": [20.0, 2.0, 0.2], "c": [70.0, 2.0, 0.2]}
    )
    resultat = sd.normaliser_a_100(donnees, ["a", "b", "c"], suffixe="_pct")
    assert list(resultat.columns) == ["x", "a", "b", "c", "a_pct", "b_pct", "c_pct"]
    assert resultat["a_pct"].tolist() == pytest.approx([10.0, 20.0, 20.0])
    assert resultat["b_pct"].tolist() == pytest.approx([20.0, 40.0, 40.0])
    assert resultat["c_pct"].tolist() == pytest.approx([70.0, 40.0, 40.0])
    somme = resultat[["a_pct", "b_pct", "c_pct"]].sum(axis=1)
    assert somme.tolist() == pytest.approx([100.0, 100.0, 100.0])


def test_normaliser_a_100_donne_nan_si_une_preference_manque_ou_si_la_somme_est_nulle() -> None:
    donnees = pd.DataFrame({"a": [np.nan, 1.0, 0.0, 0.2], "b": [5.0, -1.0, 0.0, 0.3]})
    resultat = sd.normaliser_a_100(donnees, ["a", "b"])
    # ligne 0 : une préférence manque ; ligne 1 : somme nulle (1 - 1) ; ligne 2 : somme nulle.
    assert resultat.loc[[0, 1, 2], ["a_norm", "b_norm"]].isna().all().all()
    # ligne 3 : somme 0,5 (positive, mais inférieure à 1) : normalisée quand même.
    assert resultat.loc[3, "a_norm"] == pytest.approx(40.0)
    assert resultat.loc[3, "b_norm"] == pytest.approx(60.0)


def test_normaliser_a_100_aligne_chaque_ligne_sur_son_index_et_rend_des_flottants() -> None:
    # Un tableau filtré n'a plus l'index 0, 1, 2... : chaque ligne normalisée doit rester en face
    # de sa ligne d'origine, et une valeur absente doit être un NaN flottant (pas un objet).
    donnees = pd.DataFrame({"a": [1.0, 3.0, np.nan], "b": [3.0, 1.0, 1.0]}, index=[10, 20, 30])
    resultat = sd.normaliser_a_100(donnees, ["a", "b"])
    assert resultat.index.tolist() == [10, 20, 30]
    assert resultat.loc[10, "a_norm"] == pytest.approx(25.0)
    assert resultat.loc[20, "a_norm"] == pytest.approx(75.0)
    assert math.isnan(resultat.loc[30, "a_norm"])
    assert str(resultat["a_norm"].dtype) == "float64"


def test_normaliser_a_100_leve_une_erreur_si_une_colonne_est_absente() -> None:
    donnees = pd.DataFrame({"a": [1.0]})
    with pytest.raises(KeyError):
        sd.normaliser_a_100(donnees, ["a", "absente"])


def test_normaliser_a_100_refuse_une_liste_de_colonnes_vide() -> None:
    # Sans colonne, il n'y aurait rien à normaliser : le tableau reviendrait tel quel, sans alerte.
    with pytest.raises(ValueError, match=complet("aucune colonne à ramener à 100 points")):
        sd.normaliser_a_100(pd.DataFrame({"a": [1.0]}), [])


def test_normaliser_a_100_refuse_un_suffixe_vide() -> None:
    # Avec un suffixe vide, « a » normalisée s'appellerait « a » : deux colonnes du même nom.
    with pytest.raises(
        ValueError,
        match=complet("suffixe vide : les colonnes normalisées doubleraient celles d'origine"),
    ):
        sd.normaliser_a_100(pd.DataFrame({"a": [1.0], "b": [3.0]}), ["a", "b"], suffixe="")


def test_normaliser_a_100_laisse_le_tableau_d_entree_inchange() -> None:
    donnees = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]})
    avant = donnees.copy()
    sd.normaliser_a_100(donnees, ["a", "b"])
    pd.testing.assert_frame_equal(donnees, avant)


def test_invalider_hors_bornes_garde_les_bornes_et_invalide_ce_qui_depasse() -> None:
    serie = pd.Series([0.99, 1.0, 5.0, 10.0, 10.01, np.nan])
    resultat, nombre = sd.invalider_hors_bornes(serie, 1, 10)
    assert resultat.isna().tolist() == [True, False, False, False, True, True]
    assert resultat.dropna().tolist() == [1.0, 5.0, 10.0]
    assert nombre == 2  # le NaN d'origine n'est pas compté


def test_invalider_hors_bornes_laisse_la_serie_d_entree_inchangee() -> None:
    serie = pd.Series([0.0, 5.0, 99.0])
    avant = serie.copy()
    sd.invalider_hors_bornes(serie, 1, 10)
    pd.testing.assert_series_equal(serie, avant)


def test_invalider_hors_bornes_leve_une_erreur_si_les_bornes_sont_inversees() -> None:
    with pytest.raises(ValueError, match=complet("bornes inversées : 10 > 1")):
        sd.invalider_hors_bornes(pd.Series([5.0]), 10, 1)
    resultat, nombre = sd.invalider_hors_bornes(pd.Series([5.0]), 5, 5)  # bornes égales : permis
    assert (resultat.tolist(), nombre) == ([5.0], 0)


@pytest.mark.parametrize(
    ("bas", "haut", "message"),
    [(math.nan, 10, "borne indéfinie : [nan, 10]"), (1, math.nan, "borne indéfinie : [1, nan]")],
)
def test_invalider_hors_bornes_refuse_une_borne_indefinie(
    bas: float, haut: float, message: str
) -> None:
    # Sans ce refus, aucune valeur n'étant « entre » NaN et 10, les trois notes seraient effacées.
    with pytest.raises(ValueError, match=complet(message)):
        sd.invalider_hors_bornes(pd.Series([2.0, 5.0, 8.0]), bas, haut)


def test_minimiser_colonnes_ne_garde_que_les_colonnes_demandees_dans_une_copie() -> None:
    donnees = pd.DataFrame({"iid": [1, 2], "dec": [0, 1], "race": [2.0, 4.0]})
    resultat = sd.minimiser_colonnes(donnees, ["iid", "dec"])
    assert list(resultat.columns) == ["iid", "dec"]
    resultat.loc[0, "iid"] = 99
    assert donnees.loc[0, "iid"] == 1  # une copie : le tableau d'origine n'a pas bougé
    attendues = {"race", "race_o", "imprelig", "income", "tuition", "mn_sat", "zipcode", "undergra"}
    assert attendues <= sd.COLONNES_ECARTEES  # race, religion, revenu, études, code postal
    assert "samerace" not in sd.COLONNES_ECARTEES  # l'indicateur dérivé, lui, sert au test (c)


@pytest.mark.parametrize("protegee", sorted(sd.COLONNES_ECARTEES))
def test_minimiser_colonnes_refuse_une_colonne_protegee_par_le_rgpd(protegee: str) -> None:
    donnees = pd.DataFrame({"iid": [1], protegee: [1.0]})
    with pytest.raises(ValueError, match=complet(PROTEGEES + protegee)):
        sd.minimiser_colonnes(donnees, ["iid", protegee])


def test_minimiser_colonnes_refuse_en_nommant_toutes_les_colonnes_protegees() -> None:
    donnees = pd.DataFrame({"iid": [1], "race": [1.0], "imprelig": [2.0]})
    with pytest.raises(ValueError, match=complet(PROTEGEES + "imprelig, race")):
        sd.minimiser_colonnes(donnees, ["race", "iid", "imprelig"])


def test_minimiser_colonnes_refuse_une_colonne_demandee_deux_fois() -> None:
    # pandas copierait « dec » deux fois ; les deux doublons sont nommés, dans l'ordre alphabétique.
    donnees = pd.DataFrame({"iid": [1], "dec": [0], "match": [1]})
    with pytest.raises(
        ValueError, match=complet("colonne(s) demandée(s) plusieurs fois : dec, iid")
    ):
        sd.minimiser_colonnes(donnees, ["iid", "dec", "match", "dec", "iid"])


def test_minimiser_colonnes_leve_une_erreur_si_une_colonne_demandee_est_absente() -> None:
    with pytest.raises(KeyError):
        sd.minimiser_colonnes(pd.DataFrame({"iid": [1]}), ["iid", "absente"])


# ------------------------------------------------------------------------------------------
# Structure : décisions, rendez-vous, personnes
# ------------------------------------------------------------------------------------------


@pytest.fixture
def decisions() -> pd.DataFrame:
    """Trois rendez-vous vus des deux côtés (1-2, 1-3, 2-3), plus deux décisions sans miroir."""
    lignes = [
        # iid, pid, match, dec, dec_o
        (1, 2, 1, 1, 1),
        (2, 1, 1, 1, 1),
        (1, 3, 0, 1, 0),
        (3, 1, 0, 0, 1),
        (2, 3, 0, 0, 0),
        (3, 2, 0, 0, 0),
        (4, np.nan, 0, 1, 0),  # partenaire inconnu
        (5, 6, 0, 1, 0),  # aucune ligne miroir (6, 5)
    ]
    return pd.DataFrame(lignes, columns=["iid", "pid", "match", "dec", "dec_o"])


def test_appariement_miroir_relie_chaque_decision_a_celle_du_partenaire(
    decisions: pd.DataFrame,
) -> None:
    miroir = sd.appariement_miroir(decisions)
    # 3 rendez-vous x 2 lignes ; la décision sans partenaire et celle sans miroir disparaissent.
    couples = sorted(zip(miroir["iid"], miroir["pid"], strict=True))
    assert couples == [(1, 2), (1, 3), (2, 1), (2, 3), (3, 1), (3, 2)]
    ligne = miroir[(miroir["iid"] == 1) & (miroir["pid"] == 3)].iloc[0]
    assert ligne["dec"] == 1
    assert ligne["dec_miroir"] == 0  # ce que 3 a décidé sur 1
    assert ligne["dec_o"] == ligne["dec_miroir"]  # la décision miroir est le « dec_o » de la ligne


def test_appariement_miroir_laisse_les_decisions_d_entree_inchangees(
    decisions: pd.DataFrame,
) -> None:
    avant = decisions.copy()
    sd.appariement_miroir(decisions)
    pd.testing.assert_frame_equal(decisions, avant)


def test_part_identique_compte_une_egalite_de_nan_comme_identique_et_vaut_un_si_coherent(
    decisions: pd.DataFrame,
) -> None:
    miroir = pd.DataFrame({"x": [1.0, 2.0, np.nan, np.nan], "x_miroir": [1.0, 3.0, np.nan, 5.0]})
    # lignes identiques : 0 (1 = 1) et 2 (NaN = NaN) ; différentes : 1 et 3 -> 2 sur 4.
    assert sd.part_identique(miroir, "x") == pytest.approx(0.5)
    complet = sd.appariement_miroir(decisions)
    assert sd.part_identique(complet, "match") == pytest.approx(1.0)
    assert sd.part_identique(complet, "dec") < 1.0  # une décision n'est pas celle du miroir


def test_part_identique_leve_une_erreur_si_la_variable_n_existe_pas(
    decisions: pd.DataFrame,
) -> None:
    with pytest.raises(KeyError):
        sd.part_identique(sd.appariement_miroir(decisions), "absente")


def test_part_identique_refuse_un_appariement_sans_aucune_ligne() -> None:
    # Des décisions sans aucun miroir (rendez-vous à sens unique) : pandas rendrait NaN.
    sans_miroir = pd.DataFrame({"iid": [1, 3], "pid": [2, 4], "match": [0, 1]})
    miroir = sd.appariement_miroir(sans_miroir)
    with pytest.raises(ValueError, match=complet("aucune ligne appariée : part indéfinie")):
        sd.part_identique(miroir, "match")


def test_rendez_vous_garde_une_ligne_par_rendez_vous_celle_de_l_identifiant_le_plus_bas(
    decisions: pd.DataFrame,
) -> None:
    resultat = sd.rendez_vous(decisions)
    paires = sorted(zip(resultat["iid"], resultat["pid"], strict=True))
    # 1-2, 1-3, 2-3 et la décision (5, 6) dont le miroir manque ; (4, NaN) est écartée.
    assert paires == [(1, 2), (1, 3), (2, 3), (5, 6)]
    # Une décision d'un participant sur lui-même (iid = pid) n'est pas un rendez-vous.
    soi_meme = pd.DataFrame({"iid": [1, 2, 3], "pid": [1, 1, 4]})
    assert sd.rendez_vous(soi_meme)[["iid", "pid"]].values.tolist() == [[3, 4]]


def test_rendez_vous_ecarte_un_partenaire_inconnu_et_laisse_l_entree_inchangee(
    decisions: pd.DataFrame,
) -> None:
    avant = decisions.copy()
    resultat = sd.rendez_vous(decisions)
    assert 4 not in set(resultat["iid"])
    pd.testing.assert_frame_equal(decisions, avant)


def test_rendez_vous_leve_une_erreur_sans_colonne_pid() -> None:
    with pytest.raises(KeyError):
        sd.rendez_vous(pd.DataFrame({"iid": [1, 2]}))


def test_agreger_personnes_une_ligne_par_participant_avec_les_noms_du_notebook() -> None:
    decisions = pd.DataFrame(
        {
            "iid": [1, 1, 1, 2],
            "gender": [0, 0, 0, 1],
            "attr_o": [4.0, 6.0, np.nan, 7.0],
            "order": [1, 2, 3, 1],
            "dec": [1, 0, 1, 1],
        }
    )
    agregation = {"gender": "first", "attr_o": "mean", "order": "count", "dec": "mean"}
    personnes = sd.agreger_personnes(decisions, agregation)
    assert personnes["iid"].tolist() == [1, 2]
    assert personnes["nb_rendez_vous"].tolist() == [3, 1]  # « order » renommé
    assert personnes["attr_o_moyen"].tolist() == pytest.approx([5.0, 7.0])  # NaN ignoré
    assert personnes["dec"].tolist() == pytest.approx([2 / 3, 1.0])
    assert "order" not in personnes.columns
    assert "attr_o" not in personnes.columns


def test_agreger_personnes_leve_une_erreur_pour_une_colonne_absente() -> None:
    decisions = pd.DataFrame({"iid": [1, 2], "dec": [1, 0]})
    with pytest.raises(KeyError):
        sd.agreger_personnes(decisions, {"dec": "mean", "absente": "first"})


def test_agreger_personnes_laisse_les_decisions_d_entree_inchangees() -> None:
    decisions = pd.DataFrame({"iid": [1, 1], "order": [1, 2], "attr_o": [4.0, 6.0]})
    avant = decisions.copy()
    sd.agreger_personnes(decisions, {"order": "count", "attr_o": "mean"})
    pd.testing.assert_frame_equal(decisions, avant)


# ------------------------------------------------------------------------------------------
# Statistiques
# ------------------------------------------------------------------------------------------


def test_bornes_iqr_compte_les_valeurs_hors_de_q1_moins_1_5_iqr_et_q3_plus_1_5_iqr() -> None:
    # Q1 = 2, Q3 = 4, IQR = 2 : bornes -1 et 7 ; seul 100 est dehors.
    assert sd.bornes_iqr(pd.Series([1.0, 2.0, 3.0, 4.0, 100.0])) == (-1.0, 7.0, 1)
    # Interpolation linéaire des quartiles : Q1 = 1 + 0,75 x (2 - 1) = 1,75 ;
    # Q3 = 5 + 0,25 x (6 - 5) = 5,25 ; IQR = 3,5 ; bornes 1,75 - 5,25 = -3,5 et 5,25 + 5,25 = 10,5.
    serie = pd.Series([-100.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 100.0])
    inf, sup, nombre = sd.bornes_iqr(serie)
    assert (inf, sup) == (-3.5, 10.5)
    assert nombre == 2  # un de chaque côté
    # Une valeur exactement sur la borne n'est pas une anomalie (7 = borne haute, -1 = borne basse).
    assert sd.bornes_iqr(pd.Series([1.0, 2.0, 3.0, 4.0, 7.0])) == (-1.0, 7.0, 0)
    assert sd.bornes_iqr(pd.Series([-1.0, 2.0, 3.0, 4.0, 5.0]))[2] == 0
    # Les manquants ne comptent pas.
    assert sd.bornes_iqr(pd.Series([1.0, 2.0, np.nan, 3.0, 4.0, 100.0])) == (-1.0, 7.0, 1)


@pytest.mark.parametrize("valeurs", [[], [np.nan, np.nan]])
def test_bornes_iqr_leve_une_erreur_sans_valeur_renseignee(valeurs: list[float]) -> None:
    with pytest.raises(
        ValueError, match=complet("série vide ou sans valeur renseignée : quartiles indéfinis")
    ):
        sd.bornes_iqr(pd.Series(valeurs, dtype=float))


def test_bornes_iqr_laisse_la_serie_d_entree_inchangee() -> None:
    serie = pd.Series([3.0, np.nan, 1.0, 2.0])
    avant = serie.copy()
    sd.bornes_iqr(serie)
    pd.testing.assert_series_equal(serie, avant)


def test_ajouter_z_standardise_moyenne_zero_ecart_type_un_en_ignorant_les_manquants() -> None:
    donnees = pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [10.0, 30.0, 20.0]})
    resultat = sd.ajouter_z(donnees, ["x", "y"])
    assert resultat["x_z"].tolist() == pytest.approx([-1.0, 0.0, 1.0])
    assert resultat["y_z"].tolist() == pytest.approx([-1.0, 1.0, 0.0])
    assert resultat["y_z"].std() == pytest.approx(1.0)  # écart-type d'échantillon (ddof = 1)
    manquant = sd.ajouter_z(pd.DataFrame({"x": [1.0, np.nan, 3.0]}), ["x"], suffixe="_std")
    # moyenne 2, écart-type d'échantillon sqrt(2) : (1 - 2) / sqrt(2) = -0,7071...
    assert manquant.loc[0, "x_std"] == pytest.approx(-1 / math.sqrt(2))
    assert math.isnan(manquant.loc[1, "x_std"])


def test_ajouter_z_laisse_les_donnees_d_entree_inchangees() -> None:
    donnees = pd.DataFrame({"x": [1.0, 2.0, 3.0]})
    avant = donnees.copy()
    sd.ajouter_z(donnees, ["x"])
    pd.testing.assert_frame_equal(donnees, avant)


@pytest.mark.parametrize("valeurs", [[2.0, 2.0, 2.0], [np.nan, np.nan, np.nan], [4.0]])
def test_ajouter_z_refuse_une_colonne_constante_vide_ou_reduite_a_une_valeur(
    valeurs: list[float],
) -> None:
    with pytest.raises(
        ValueError, match=complet("colonne constante ou vide, standardisation impossible : x")
    ):
        sd.ajouter_z(pd.DataFrame({"x": valeurs}), ["x"])


def test_ajouter_z_leve_une_erreur_pour_une_colonne_absente() -> None:
    with pytest.raises(KeyError):
        sd.ajouter_z(pd.DataFrame({"x": [1.0, 2.0]}), ["absente"])


def test_ajouter_z_refuse_un_suffixe_vide() -> None:
    # Avec un suffixe vide, « x » standardisée écraserait « x » : la note d'origine serait perdue.
    with pytest.raises(
        ValueError,
        match=complet("suffixe vide : la colonne standardisée remplacerait celle d'origine"),
    ):
        sd.ajouter_z(pd.DataFrame({"x": [1.0, 2.0, 3.0]}), ["x"], suffixe="")


def donnees_logit(graine: int = 7, n: int = 600) -> pd.DataFrame:
    """Décisions simulées : 30 participants x 20 partenaires, effet réel de `x` sur `dec`."""
    hasard = np.random.default_rng(graine)
    x = hasard.normal(size=n)
    proba = 1 / (1 + np.exp(-(-0.3 + 0.9 * x)))
    return pd.DataFrame(
        {
            "dec": (hasard.uniform(size=n) < proba).astype(int),
            "x": x,
            "iid": hasard.integers(0, 30, size=n),
            "pid": hasard.integers(100, 120, size=n),
        }
    )


def test_logit_grappes_garde_les_coefficients_et_change_les_erreurs_standard() -> None:
    donnees = donnees_logit()
    deux = sd.logit_grappes("dec ~ x", donnees, ["iid", "pid"])
    une = sd.logit_grappes("dec ~ x", donnees, ["iid"])
    autre = sd.logit_grappes("dec ~ x", donnees, ["pid"])
    simple = smf.logit("dec ~ x", data=donnees).fit(disp=False)
    # Les coefficients ne changent pas : seule l'incertitude est corrigée par les grappes.
    assert deux.params.to_numpy() == pytest.approx(simple.params.to_numpy())
    assert une.params["x"] == pytest.approx(deux.params["x"])
    assert deux.pvalues["x"] != pytest.approx(simple.pvalues["x"], rel=1e-6)
    assert deux.params["x"] > 0  # le sens de l'effet simulé est retrouvé
    assert deux.tvalues["x"] == pytest.approx(deux.params["x"] / deux.bse["x"])
    assert deux.cov_type == "cluster"  # le résultat dit bien quelle covariance a été employée
    # Une ou deux dimensions de grappes : des p-valeurs différentes.
    p_une, p_deux, p_autre = (m.pvalues["x"] for m in (une, deux, autre))
    assert p_une != pytest.approx(p_deux, rel=1e-6)
    assert p_une != pytest.approx(p_autre, rel=1e-6)
    assert p_deux != pytest.approx(p_autre, rel=1e-6)


def test_logit_grappes_retire_les_lignes_incompletes_et_n_affiche_rien(
    capsys: pytest.CaptureFixture[str],
) -> None:
    donnees = donnees_logit(n=300)
    donnees.loc[0:9, "x"] = np.nan  # variable de la formule manquante
    donnees.loc[10:14, "iid"] = np.nan  # grappe manquante
    donnees["commentaire"] = np.nan  # colonne étrangère à la formule : ne retire aucune ligne
    attendu = int(donnees[["dec", "x", "iid", "pid"]].dropna().shape[0])
    modele = sd.logit_grappes("dec ~ x", donnees, ["iid", "pid"])
    assert int(modele.nobs) == attendu
    assert attendu == 300 - 15  # 10 + 5 lignes écartées, sans recouvrement
    # `disp=False` : statsmodels n'écrit pas son journal d'optimisation dans la sortie du notebook.
    sortie = capsys.readouterr()
    assert (sortie.out, sortie.err) == ("", "")
    # Le résultat rendu garde aussi le réglage demandé (`mle_settings`, attribut public des
    # résultats statsmodels) : ajustement explicitement sans affichage, `False`.
    assert modele.mle_settings["disp"] is False


@pytest.mark.parametrize("grappes", [[], ["iid", "pid", "wave"]])
def test_logit_grappes_leve_une_erreur_hors_d_une_ou_deux_grappes(grappes: list[str]) -> None:
    donnees = donnees_logit(n=100)
    donnees["wave"] = 1
    with pytest.raises(
        ValueError, match=complet(f"une ou deux grappes attendues, reçu {len(grappes)} : {grappes}")
    ):
        sd.logit_grappes("dec ~ x", donnees, grappes)


def test_logit_grappes_refuse_la_meme_grappe_donnee_deux_fois() -> None:
    # « iid » deux fois n'est pas un groupement à deux voies (participant et partenaire).
    with pytest.raises(
        ValueError, match=complet("la même grappe est donnée deux fois : ['iid', 'iid']")
    ):
        sd.logit_grappes("dec ~ x", donnees_logit(n=100), ["iid", "iid"])


def test_logit_grappes_donne_les_memes_resultats_avec_des_identifiants_entiers_ou_flottants() -> (
    None
):
    # Les identifiants de grappes ne servent qu'à regrouper : leur type est sans effet.
    entiers = donnees_logit()
    flottants = entiers.assign(iid=entiers["iid"].astype(float), pid=entiers["pid"].astype(float))
    a = sd.logit_grappes("dec ~ x", entiers, ["iid", "pid"])
    b = sd.logit_grappes("dec ~ x", flottants, ["iid", "pid"])
    assert b.pvalues["x"] == pytest.approx(a.pvalues["x"], rel=1e-12)
    assert b.params["x"] == pytest.approx(a.params["x"], rel=1e-12)


def test_logit_grappes_ne_fusionne_pas_des_identifiants_non_entiers() -> None:
    # Diviser tous les identifiants par deux ne change pas les groupes (0, 0,5, 1, 1,5...) : le
    # résultat doit être le même. Une conversion en entiers fusionnerait 0 et 0,5, 1 et 1,5...
    entiers = donnees_logit()
    moities = entiers.assign(iid=entiers["iid"] / 2, pid=entiers["pid"] / 2)
    a = sd.logit_grappes("dec ~ x", entiers, ["iid", "pid"])
    b = sd.logit_grappes("dec ~ x", moities, ["iid", "pid"])
    assert b.pvalues["x"] == pytest.approx(a.pvalues["x"], rel=1e-12)


def test_logit_grappes_leve_une_erreur_pour_une_variable_absente_de_la_formule() -> None:
    with pytest.raises(PatsyError):
        sd.logit_grappes("dec ~ inconnue", donnees_logit(n=100), ["iid"])


def test_logit_grappes_leve_une_erreur_pour_une_grappe_absente() -> None:
    with pytest.raises(KeyError):
        sd.logit_grappes("dec ~ x", donnees_logit(n=100), ["inconnue"])


def test_logit_grappes_laisse_les_donnees_d_entree_inchangees() -> None:
    donnees = donnees_logit(n=200)
    donnees.loc[0:4, "x"] = np.nan
    avant = donnees.copy()
    sd.logit_grappes("dec ~ x", donnees, ["iid", "pid"])
    pd.testing.assert_frame_equal(donnees, avant)


def test_rapports_de_cotes_exponentielle_garde_les_noms_et_vaut_un_si_coefficient_nul() -> None:
    coefficients = pd.Series({"attr_z": math.log(2), "sinc_z": 0.0, "amb_z": -math.log(2)})
    rapports = sd.rapports_de_cotes(coefficients)
    assert rapports.index.tolist() == ["attr_z", "sinc_z", "amb_z"]
    # +1 écart-type multiplie les chances par 2, ne les change pas, ou les divise par 2.
    assert rapports.tolist() == pytest.approx([2.0, 1.0, 0.5])


def test_rapports_de_cotes_laisse_la_serie_d_entree_inchangee() -> None:
    coefficients = pd.Series([0.5, -0.5])
    avant = coefficients.copy()
    sd.rapports_de_cotes(coefficients)
    pd.testing.assert_series_equal(coefficients, avant)


@pytest.mark.parametrize(
    ("p", "attendu"),
    [
        (0.05, "5.00e-02"),
        (1.51e-17, "1.51e-17"),
        (1e-300, "1.00e-300"),  # exactement le seuil : encore affichée
        (9.9e-301, "< 1e-300"),  # juste dessous : sous la précision des flottants
        (0.0, "< 1e-300"),
        (1.0, "1.00e+00"),  # borne haute comprise
    ],
)
def test_formater_p_notation_scientifique_et_plancher_a_1e_300(p: float, attendu: str) -> None:
    assert sd.formater_p(p) == attendu
    assert sd.formater_p(np.float64(0.0123)) == "1.23e-02"  # les flottants numpy sont acceptés


@pytest.mark.parametrize("p", [float("nan"), -0.01, 1.0000001, 12.0])
def test_formater_p_refuse_une_p_valeur_indefinie_ou_hors_de_zero_un(p: float) -> None:
    with pytest.raises(
        ValueError, match=complet(f"p-valeur hors de [0, 1] ou indéfinie : {float(p)}")
    ):
        sd.formater_p(p)


def test_consigner_test_decide_au_seuil_de_bonferroni_strictement_avec_sept_champs() -> None:
    resultats: list[dict[str, str]] = []
    seuil = 0.05 / 6
    sd.consigner_test(resultats, seuil, "t1", "h0", "personne", "t = 8.85", 1e-5, "d = 0.76")
    sd.consigner_test(resultats, seuil, "t2", "h0", "rendez-vous", "z = 0.60", 0.547, "V = 0.012")
    sd.consigner_test(resultats, seuil, "t3", "h0", "décision", "z = 1.9", seuil, "RC = 0.96")
    decisions = [ligne["décision (Bonferroni)"] for ligne in resultats]
    assert decisions == ["H0 rejetée", "H0 conservée", "H0 conservée"]  # p = seuil : conservée
    assert [ligne["test"] for ligne in resultats] == ["t1", "t2", "t3"]  # ordre d'ajout
    # Le seuil 1 est le plus grand permis : tout p < 1 rejette H0.
    sd.consigner_test(resultats, 1.0, "t4", "h0", "personne", "t = 0", 0.5, "d = 0")
    assert resultats[-1]["décision (Bonferroni)"] == "H0 rejetée"
    assert resultats[0] == {
        "test": "t1",
        "H0": "h0",
        "unité": "personne",
        "statistique": "t = 8.85",
        "p-valeur": "1.00e-05",
        "taille d'effet": "d = 0.76",
        "décision (Bonferroni)": "H0 rejetée",
    }


@pytest.mark.parametrize("p", [float("nan"), -1.0, 2.0])
def test_consigner_test_refuse_une_p_valeur_invalide_et_laisse_le_tableau_inchange(
    p: float,
) -> None:
    resultats: list[dict[str, str]] = [{"test": "déjà là"}]
    with pytest.raises(
        ValueError, match=complet(f"p-valeur hors de [0, 1] ou indéfinie : {float(p)}")
    ):
        sd.consigner_test(resultats, 0.05, "t", "h0", "personne", "t = 1", p, "d = 0")
    assert resultats == [{"test": "déjà là"}]


@pytest.mark.parametrize("seuil", [0.0, -0.05, 1.5])
def test_consigner_test_refuse_un_seuil_hors_de_zero_un_et_laisse_le_tableau_inchange(
    seuil: float,
) -> None:
    resultats: list[dict[str, str]] = []
    with pytest.raises(ValueError, match=complet(f"seuil hors de ]0, 1] : {seuil}")):
        sd.consigner_test(resultats, seuil, "t", "h0", "personne", "t = 1", 0.01, "d = 0")
    assert resultats == []


def test_d_cohen_utilise_l_ecart_type_moyen_des_deux_groupes() -> None:
    a = pd.Series([1.0, 2.0, 3.0])  # moyenne 2, variance 1
    b = pd.Series([3.0, 5.0, 7.0])  # moyenne 5, variance 4
    # (2 - 5) / sqrt((1 + 4) / 2) = -3 / 1,5811...
    assert sd.d_cohen(a, b) == pytest.approx(-3 / math.sqrt(2.5))
    assert sd.d_cohen(b, a) == pytest.approx(3 / math.sqrt(2.5))  # signe inversé
    # Sans unité : les mêmes notes divisées par 10 (variances 0,01 et 0,04) donnent le même d.
    assert sd.d_cohen(a / 10, b / 10) == pytest.approx(-3 / math.sqrt(2.5))


@pytest.mark.parametrize("valeurs", [[2.0, 2.0], [], [np.nan, np.nan]])
def test_d_cohen_leve_une_erreur_si_les_deux_groupes_sont_sans_variation(
    valeurs: list[float],
) -> None:
    groupe = pd.Series(valeurs, dtype=float)
    with pytest.raises(
        ValueError, match=complet("variances nulles dans les deux groupes : d de Cohen indéfini")
    ):
        sd.d_cohen(groupe, groupe)


def test_d_cohen_apparie_divise_la_moyenne_des_differences_par_leur_ecart_type() -> None:
    ecarts = pd.Series([1.0, 2.0, 3.0])  # moyenne 2, écart-type 1
    assert sd.d_cohen_apparie(ecarts) == pytest.approx(2.0)
    assert sd.d_cohen_apparie(-ecarts) == pytest.approx(-2.0)
    # Écarts doublés (moyenne 4, écart-type 2) : même d, car il est sans unité.
    assert sd.d_cohen_apparie(ecarts * 2) == pytest.approx(2.0)


@pytest.mark.parametrize("valeurs", [[2.0, 2.0, 2.0], [3.0]])
def test_d_cohen_apparie_leve_une_erreur_sans_variation(valeurs: list[float]) -> None:
    with pytest.raises(
        ValueError,
        match=complet("différences constantes ou en nombre insuffisant : d apparié indéfini"),
    ):
        sd.d_cohen_apparie(pd.Series(valeurs))


def test_rang_bisserial_apparie_pondere_par_les_rangs_et_ignore_les_ecarts_nuls() -> None:
    assert sd.rang_bisserial_apparie(pd.Series([0.5, 1.0, 4.0])) == pytest.approx(1.0)
    assert sd.rang_bisserial_apparie(pd.Series([-0.5, -1.0, -4.0])) == pytest.approx(-1.0)
    # |écarts| = 1, 2, 3 -> rangs 1, 2, 3 ; positifs (1 et 3) : 4 ; négatif (2) : 2 ;
    # (4 - 2) / 6 = 1/3. Les zéros ne comptent ni dans les rangs ni dans la somme.
    attendu = 1 / 3
    assert sd.rang_bisserial_apparie(pd.Series([1.0, -2.0, 3.0])) == pytest.approx(attendu)
    avec_zeros = pd.Series([0.0, 1.0, -2.0, 0.0, 3.0])
    assert sd.rang_bisserial_apparie(avec_zeros) == pytest.approx(attendu)


@pytest.mark.parametrize("valeurs", [[0.0, 0.0], []])
def test_rang_bisserial_apparie_leve_une_erreur_sans_ecart_non_nul(valeurs: list[float]) -> None:
    with pytest.raises(
        ValueError, match=complet("aucun écart non nul : corrélation de rang bisériale indéfinie")
    ):
        sd.rang_bisserial_apparie(pd.Series(valeurs, dtype=float))


def test_v_cramer_vaut_un_si_l_association_est_parfaite_et_un_tiers_sur_l_exemple() -> None:
    parfait = pd.DataFrame([[10, 0], [0, 10]])
    chi2_parfait = stats.chi2_contingency(parfait, correction=False)[0]
    assert sd.v_cramer(parfait, chi2_parfait) == pytest.approx(1.0)
    # [[10, 20], [20, 10]] : effectifs attendus 15, chi-deux = 4 x 25 / 15, n = 60 :
    # V = sqrt((100 / 15) / 60) = sqrt(1 / 9) = 1/3.
    exemple = pd.DataFrame([[10, 20], [20, 10]])
    chi2 = stats.chi2_contingency(exemple, correction=False)[0]
    assert chi2 == pytest.approx(100 / 15)
    assert sd.v_cramer(exemple, chi2) == pytest.approx(1 / 3)
    # 2 lignes, 3 colonnes : min(2, 3) - 1 = 1. Effectifs attendus 5 partout (n = 30) :
    # chi-deux = 2 x ((10 - 5)^2 / 5 + (0 - 5)^2 / 5 + 0) = 20 ; V = sqrt(20 / 30).
    large = pd.DataFrame([[10, 0, 5], [0, 10, 5]])
    chi2_large = stats.chi2_contingency(large, correction=False)[0]
    assert chi2_large == pytest.approx(20.0)
    assert sd.v_cramer(large, chi2_large) == pytest.approx(math.sqrt(20 / 30))
    # 3 x 3 à association parfaite : chi-deux = n x (3 - 1) = 60 pour n = 30, donc V = 1.
    # C'est le seul cas des tests où min(lignes, colonnes) - 1 ne vaut pas 1.
    diagonal = pd.DataFrame([[10, 0, 0], [0, 10, 0], [0, 0, 10]])
    chi2_diagonal = stats.chi2_contingency(diagonal, correction=False)[0]
    assert chi2_diagonal == pytest.approx(60.0)
    assert sd.v_cramer(diagonal, chi2_diagonal) == pytest.approx(1.0)


@pytest.mark.parametrize("tableau", [[[3, 4]], [[3], [4]]])
def test_v_cramer_leve_une_erreur_pour_un_tableau_sans_deux_lignes_et_deux_colonnes(
    tableau: list[list[int]],
) -> None:
    with pytest.raises(
        ValueError,
        match=complet("un tableau de contingence a au moins deux lignes et deux colonnes"),
    ):
        sd.v_cramer(pd.DataFrame(tableau), 0.0)


@pytest.mark.parametrize(
    ("tableau", "chi2", "message"),
    [
        ([[10, 20], [20, 10]], -1.0, "χ² négatif ou indéfini : -1.0"),
        ([[10, 20], [20, 10]], math.nan, "χ² négatif ou indéfini : nan"),
        ([[0, 0], [0, 0]], 0.0, "tableau de contingence sans aucun effectif"),
    ],
)
def test_v_cramer_refuse_un_chi2_negatif_ou_indefini_et_un_tableau_sans_effectif(
    tableau: list[list[int]], chi2: float, message: str
) -> None:
    # Sans ces refus : racine d'un nombre négatif (NaN) ou division par un effectif nul.
    with pytest.raises(ValueError, match=complet(message)):
        sd.v_cramer(pd.DataFrame(tableau), chi2)
    # Les bornes permises : χ² nul (aucune association) et un tableau d'une seule observation.
    assert sd.v_cramer(pd.DataFrame([[1, 0], [0, 0]]), 0.0) == 0.0
