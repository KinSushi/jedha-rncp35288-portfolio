# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de walmart_bloc3.py : chaque fonction a des tests directs ET des tests inverses.

Un test direct montre ce que la fonction calcule ; un test inverse montre ce qu'elle refuse
(pytest.raises) ou ce qu'elle laisse intact (entrée inchangée). Les données sont minuscules et
construites à la main : les valeurs attendues se vérifient à la calculatrice.
"""

from __future__ import annotations

import math
import statistics
import warnings

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal, assert_series_equal
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.metrics import r2_score
from sklearn.model_selection import GridSearchCV, KFold, cross_validate, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

import walmart_bloc3 as w

# --------------------------------------------------------------------------------------------
# Données de test
# --------------------------------------------------------------------------------------------


def jeu_brut() -> pd.DataFrame:
    """20 lignes : une cible manquante (ligne 7), une date fausse (5) et une date absente (6).

    La ligne 3 est extrême en Temperature (100 pour 10 ailleurs) ET en Unemployment (70 pour 7
    ailleurs) : la règle la retire une seule fois. La ligne 11 est extrême en CPI (2000 pour 200).
    Les lignes 4 (CPI) et 9 (Temperature) ont une valeur manquante, qui n'est pas un extrême.
    """
    n = 20
    dates = ["07-01-2011"] * n
    dates[0] = "05-02-2010"  # un vendredi
    dates[1] = "01-03-2010"  # un lundi
    dates[5] = "pas une date"
    dates[6] = None
    brut = pd.DataFrame(
        {
            "Store": [float(1 + i % 4) for i in range(n)],
            "Date": dates,
            "Weekly_Sales": [1000.0 + 10 * i for i in range(n)],
            "Holiday_Flag": [0.0] * n,
            "Temperature": [10.0] * n,
            "Fuel_Price": [3.0] * n,
            "CPI": [200.0] * n,
            "Unemployment": [7.0] * n,
        }
    )
    brut.loc[7, "Weekly_Sales"] = np.nan
    brut.loc[3, "Temperature"] = 100.0
    brut.loc[3, "Unemployment"] = 70.0
    brut.loc[11, "CPI"] = 2000.0
    brut.loc[4, "CPI"] = np.nan
    brut.loc[9, "Temperature"] = np.nan
    return brut


REGLE = ["Temperature", "Fuel_Price", "CPI", "Unemployment"]
INDEX_ATTENDU = [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19]
MODERE = "sur-apprentissage modéré (petit jeu de données), pas de sous-apprentissage."
RIEN = "ni sous-apprentissage ni sur-apprentissage visible."
FAIBLE = "R² de validation faible : risque de sous-apprentissage."


def preprocesseur() -> ColumnTransformer:
    """Le même type de prétraitement que le notebook, sur deux colonnes numériques et Store."""
    numerique = Pipeline(
        [("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler())]
    )
    categoriel = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(drop="first", handle_unknown="ignore", sparse_output=False)),
        ]
    )
    return ColumnTransformer(
        [("num", numerique, ["Temperature", "CPI"]), ("cat", categoriel, ["Store"])]
    )


def jeu_modele(avec_magasin_rare: bool) -> tuple[pd.DataFrame, pd.Series]:
    """61 lignes (ou 60) : les ventes sont 1000 fois le magasin plus 10 fois la température.

    Le magasin 6, présent une seule fois, force l'avertissement « unknown categories » de
    scikit-learn quand il tombe dans le test d'un pli ou d'un découpage.
    """
    rng = np.random.default_rng(0)
    magasins = [1, 2, 3, 4, 5] * 12 + ([6] if avec_magasin_rare else [])
    n = len(magasins)
    x = pd.DataFrame(
        {
            "Store": [float(m) for m in magasins],
            "Temperature": rng.normal(60, 10, n),
            "CPI": rng.normal(180, 20, n),
        }
    )
    y = pd.Series(1000 * np.array(magasins) + 10 * x["Temperature"] + rng.normal(0, 5, n))
    return x, y


class RidgeBavard(Ridge):
    """Un Ridge qui émet un avertissement à chaque prédiction (pour tester la remontée)."""

    def predict(self, X):
        warnings.warn("avertissement maison", UserWarning, stacklevel=2)
        return super().predict(X)


# --------------------------------------------------------------------------------------------
# prepare_dataframe
# --------------------------------------------------------------------------------------------


def test_prepare_dataframe_retire_la_cible_manquante_et_les_extremes_seulement():
    """20 lignes → 19 (cible) → 17 (deux extrêmes) ; un manquant de X n'est pas un extrême."""
    model_df, _, _ = w.prepare_dataframe(jeu_brut(), REGLE)
    assert list(model_df.index) == INDEX_ATTENDU
    assert math.isnan(model_df.loc[4, "CPI"])  # manquant, conservé
    assert math.isnan(model_df.loc[9, "Temperature"])  # manquant, conservé
    # Sans colonne de règle, seule la cible manquante est retirée.
    sans_regle, _, _ = w.prepare_dataframe(jeu_brut(), [])
    assert list(sans_regle.index) == [i for i in range(20) if i != 7]


def test_prepare_dataframe_cree_les_variables_de_date_et_retire_les_colonnes_brutes():
    """05-02-2010 est un vendredi (4), 01-03-2010 un lundi (0) ; une date fausse devient NaN."""
    model_df, _, _ = w.prepare_dataframe(jeu_brut(), REGLE)
    attendu = pd.DataFrame(
        {
            "Year": [2010.0, 2010.0, 2011.0, 2011.0],
            "Month": [2.0, 3.0, 1.0, 1.0],
            "Day": [5.0, 1.0, 7.0, 7.0],
            "DayOfWeek": [4.0, 0.0, 4.0, 4.0],
        },
        index=[0, 1, 2, 4],
    )
    assert_frame_equal(
        model_df.loc[[0, 1, 2, 4], ["Year", "Month", "Day", "DayOfWeek"]].astype(float), attendu
    )
    for ligne in (5, 6):  # date fausse, date absente
        assert model_df.loc[ligne, ["Year", "Month", "Day", "DayOfWeek"]].isna().all()
    # Date et DateParsed sont remplacées : elles ne restent pas dans le jeu nettoyé.
    assert list(model_df.columns) == [
        "Store",
        "Weekly_Sales",
        "Holiday_Flag",
        "Temperature",
        "Fuel_Price",
        "CPI",
        "Unemployment",
        "Year",
        "Month",
        "Day",
        "DayOfWeek",
    ]


def test_prepare_dataframe_journal_et_seuils_a_trois_ecarts_types():
    """Journal étape par étape ; seuils = moyenne ± 3 écarts-types d'échantillon, NaN ignorés."""
    _, rapport, seuils = w.prepare_dataframe(jeu_brut(), REGLE)
    attendu = pd.DataFrame(
        {
            "step": [
                "drop_missing_target_Weekly_Sales",
                "create_calendar_features",
                "outlier_filter_Temperature",
                "outlier_filter_Fuel_Price",
                "outlier_filter_CPI",
                "outlier_filter_Unemployment",
                "after_all_outlier_filters",
            ],
            "rows_before": [20, 19, 19, 18, 18, 17, 19],
            "rows_after": [19, 19, 18, 18, 17, 17, 17],
            "rows_removed": [1, 0, 1, 0, 1, 0, 2],
            "reason": [
                "La cible y ne doit jamais être imputée.",
                "La date brute n'est pas exploitable directement par un modèle linéaire.",
                "Règle imposée par l'énoncé : moyenne ± 3 écarts-types.",
                "Règle imposée par l'énoncé : moyenne ± 3 écarts-types.",
                "Règle imposée par l'énoncé : moyenne ± 3 écarts-types.",
                "Règle imposée par l'énoncé : moyenne ± 3 écarts-types.",
                "Dataset prêt pour X/y et pipeline sklearn.",
            ],
        }
    )
    assert_frame_equal(rapport, attendu)
    assert list(seuils["variable"]) == REGLE
    lignes = seuils.set_index("variable")
    chomage = [7.0] * 18 + [70.0]  # les 19 lignes qui ont une cible
    temperature = [10.0] * 17 + [100.0]  # 18 valeurs : la 19e est manquante
    for nom, valeurs, retirees in [
        ("Unemployment", chomage, 0),  # la ligne 3 était déjà sortie par Temperature
        ("Temperature", temperature, 1),
    ]:
        moyenne, ecart = statistics.mean(valeurs), statistics.stdev(valeurs)
        assert lignes.loc[nom, "mean"] == pytest.approx(moyenne)
        assert lignes.loc[nom, "std"] == pytest.approx(ecart)
        assert lignes.loc[nom, "lower_bound"] == pytest.approx(moyenne - 3 * ecart)
        assert lignes.loc[nom, "upper_bound"] == pytest.approx(moyenne + 3 * ecart)
        assert lignes.loc[nom, "outliers_removed"] == retirees


def test_prepare_dataframe_suit_les_manipulations_du_jury():
    """5 écarts-types garde tout ; retirer Unemployment ou CPI de la règle suit le résultat."""
    large, rapport, seuils = w.prepare_dataframe(jeu_brut(), REGLE, n_sigma=5.0)
    assert list(large.index) == [i for i in range(20) if i != 7]  # z ≈ 4,0 : plus des extrêmes
    valeurs = [10.0] * 17 + [100.0]
    moyenne, ecart = statistics.mean(valeurs), statistics.stdev(valeurs)
    ligne = seuils.set_index("variable").loc["Temperature"]
    assert ligne["lower_bound"] == pytest.approx(moyenne - 5 * ecart)
    assert ligne["upper_bound"] == pytest.approx(moyenne + 5 * ecart)
    assert ligne["outliers_removed"] == 0
    assert rapport.loc[2, "reason"] == "Règle imposée par l'énoncé : moyenne ± 5 écarts-types."

    regle = ["Temperature", "Fuel_Price", "CPI"]  # Unemployment retiré : la ligne 3 reste extrême
    sans_chomage, _, seuils_sans = w.prepare_dataframe(jeu_brut(), regle)
    assert list(seuils_sans["variable"]) == regle
    assert 3 not in sans_chomage.index
    sans_cpi, _, _ = w.prepare_dataframe(jeu_brut(), ["Temperature"])
    assert 11 in sans_cpi.index  # la ligne 11 n'était extrême qu'en CPI


def test_prepare_dataframe_accepte_un_sigma_inferieur_a_un():
    """Un demi-écart-type est un intervalle valide (seuls 0, négatif et non fini sont refusés)."""
    model_df, rapport, _ = w.prepare_dataframe(jeu_brut(), REGLE, n_sigma=0.5)
    assert list(model_df.index) == INDEX_ATTENDU
    assert rapport.loc[2, "reason"] == "Règle imposée par l'énoncé : moyenne ± 0.5 écarts-types."


def test_prepare_dataframe_laisse_le_brut_inchange():
    """La fonction travaille sur une copie : le DataFrame brut est identique après l'appel."""
    brut = jeu_brut()
    avant = brut.copy()
    w.prepare_dataframe(brut, REGLE)
    assert_frame_equal(brut, avant)


def test_prepare_dataframe_refuse_une_colonne_inconnue():
    """Une faute de frappe dans outlier_cols est refusée avec le nom de la colonne."""
    with pytest.raises(ValueError, match="Unemploymnt"):
        w.prepare_dataframe(jeu_brut(), ["Temperature", "Unemploymnt"])


@pytest.mark.parametrize("sigma", [0, -3.0, math.nan, math.inf])
def test_prepare_dataframe_refuse_un_sigma_non_positif_ou_non_fini(sigma):
    """Un intervalle de largeur nulle, négative ou infinie n'a pas de sens : ValueError."""
    with pytest.raises(ValueError, match="strictement positif"):
        w.prepare_dataframe(jeu_brut(), REGLE, n_sigma=sigma)


@pytest.mark.parametrize("colonne", ["Weekly_Sales", "Date"])
def test_prepare_dataframe_refuse_un_brut_sans_la_cible_ou_la_date(colonne):
    """Sans Weekly_Sales ou sans Date, le jeu n'est pas celui de l'énoncé : KeyError."""
    with pytest.raises(KeyError):
        w.prepare_dataframe(jeu_brut().drop(columns=colonne), REGLE)


# --------------------------------------------------------------------------------------------
# consequences_pretraitement
# --------------------------------------------------------------------------------------------


def test_consequences_nomment_les_lignes_retirees_les_magasins_perdus_et_la_borne():
    """Un magasin dont toutes les lignes sont retirées disparaît EN ENTIER ; borne du chômage."""
    brut = pd.DataFrame(
        {
            "Store": [1.0, 1.0, 2.0, 2.0],
            "Weekly_Sales": [10.0, 20.0, 30.0, np.nan],
            "Unemployment": [7.0, 7.0, 7.0, 7.0],
        }
    )
    nettoye = brut.loc[[0, 1]]  # le magasin 2 a disparu ; la ligne 3 n'avait pas de cible
    seuils = pd.DataFrame({"variable": ["Unemployment"], "upper_bound": [12.5]})
    cons = w.consequences_pretraitement(brut, nettoye, seuils)
    assert list(cons.lignes_retirees.index) == [2]
    assert cons.magasins_avant == 2
    assert cons.magasins_apres == 1
    assert cons.magasins_perdus == [2]
    assert cons.borne_haute_chomage == 12.5


def test_consequences_rendent_les_magasins_perdus_en_entiers_croissants():
    """Magasins perdus : des int triés (un ensemble de flottants n'itère pas dans l'ordre)."""
    brut = pd.DataFrame(
        {"Store": [8.0, 1.0, 3.0, 3.0], "Weekly_Sales": [10.0, 20.0, 30.0, 40.0]}
    )
    seuils = pd.DataFrame({"variable": ["Unemployment"], "upper_bound": [12.5]})
    cons = w.consequences_pretraitement(brut, brut.loc[[2, 3]], seuils)
    assert cons.magasins_perdus == [1, 8]
    assert all(type(magasin) is int for magasin in cons.magasins_perdus)


def test_consequences_lisent_la_premiere_ligne_unemployment_du_rapport():
    """Si le rapport cite deux fois Unemployment, la première borne est celle qu'on rend."""
    brut = pd.DataFrame({"Store": [1.0, 1.0], "Weekly_Sales": [10.0, 20.0]})
    seuils = pd.DataFrame(
        {"variable": ["Unemployment", "CPI", "Unemployment"], "upper_bound": [12.5, 300.0, 99.0]}
    )
    assert w.consequences_pretraitement(brut, brut, seuils).borne_haute_chomage == 12.5


def test_consequences_chainees_avec_prepare_dataframe():
    """Sur le jeu de test : lignes 3 et 11 retirées, aucun magasin perdu, borne du rapport."""
    brut = jeu_brut()
    model_df, _, seuils = w.prepare_dataframe(brut, REGLE)
    cons = w.consequences_pretraitement(brut, model_df, seuils)
    assert list(cons.lignes_retirees.index) == [3, 11]
    assert cons.magasins_perdus == []
    valeurs = [7.0] * 18 + [70.0]
    assert cons.borne_haute_chomage == pytest.approx(
        statistics.mean(valeurs) + 3 * statistics.stdev(valeurs)
    )


@pytest.mark.parametrize("regle", [["Temperature", "CPI"], []])
def test_consequences_sans_unemployment_dans_la_regle_renvoient_none(regle):
    """Cas du jury : Unemployment hors de la règle (avant : IndexError) ; règle vide aussi."""
    brut = jeu_brut()
    model_df, _, seuils = w.prepare_dataframe(brut, regle)
    cons = w.consequences_pretraitement(brut, model_df, seuils)
    assert cons.borne_haute_chomage is None


def test_consequences_laissent_les_trois_tableaux_inchanges():
    """La comparaison ne modifie ni le brut, ni le jeu nettoyé, ni le rapport des seuils."""
    brut = jeu_brut()
    model_df, _, seuils = w.prepare_dataframe(brut, REGLE)
    brut_avant, model_avant, seuils_avant = brut.copy(), model_df.copy(), seuils.copy()
    w.consequences_pretraitement(brut, model_df, seuils)
    assert_frame_equal(brut, brut_avant)
    assert_frame_equal(model_df, model_avant)
    assert_frame_equal(seuils, seuils_avant)


def test_consequences_refusent_un_jeu_nettoye_qui_n_est_pas_un_sous_ensemble():
    """Un index absent du brut est une incohérence : KeyError plutôt qu'un résultat faux."""
    brut = jeu_brut()
    intrus = brut.loc[[0, 1]].copy()
    intrus.index = [100, 101]
    seuils = pd.DataFrame({"variable": ["Unemployment"], "upper_bound": [12.5]})
    with pytest.raises(KeyError):
        w.consequences_pretraitement(brut, intrus, seuils)


def test_consequences_refusent_un_brut_sans_la_colonne_store():
    """Sans Store, on ne peut pas dire quels magasins disparaissent : KeyError."""
    brut = jeu_brut()
    model_df, _, seuils = w.prepare_dataframe(brut, REGLE)
    with pytest.raises(KeyError):
        w.consequences_pretraitement(brut.drop(columns="Store"), model_df, seuils)


# --------------------------------------------------------------------------------------------
# trouver_racine
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notebook_dans", "depart_dans", "racine_attendue"),
    [
        (".", ".", "."),  # le notebook est dans le dossier de départ
        ("1_Walmart", ".", "1_Walmart"),  # le noyau démarre dans le dossier parent
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
    trouve = w.trouver_racine("nb.ipynb", "1_Walmart", depart=tmp_path / depart_dans)
    assert trouve == (tmp_path / racine_attendue).resolve()


def test_trouver_racine_normalise_le_chemin_de_depart(tmp_path):
    """Un départ écrit avec « .. » est ramené à un chemin absolu normalisé (resolve)."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "nb.ipynb").write_text("{}", encoding="utf-8")
    trouve = w.trouver_racine("nb.ipynb", "1_Walmart", depart=tmp_path / "a" / "b" / "..")
    assert trouve == (tmp_path / "a").resolve()


def test_trouver_racine_par_defaut_part_du_dossier_courant(tmp_path, monkeypatch):
    """Sans départ explicite, la recherche commence au dossier de travail courant."""
    (tmp_path / "nb.ipynb").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert w.trouver_racine("nb.ipynb", "1_Walmart") == tmp_path.resolve()


def test_trouver_racine_sans_notebook_garde_le_depart_et_ne_cree_rien_inchange(tmp_path):
    """Aucun notebook trouvé : on garde le départ, sans créer de fichier (dossier inchangé)."""
    depart = tmp_path / "vide"
    depart.mkdir()
    (depart / "x.txt").write_text("x", encoding="utf-8")
    avant = sorted(p.name for p in depart.iterdir())
    assert w.trouver_racine("introuvable.ipynb", "1_Walmart", depart=depart) == depart.resolve()
    assert sorted(p.name for p in depart.iterdir()) == avant


def test_trouver_racine_refuse_un_nom_de_notebook_vide(tmp_path):
    """Un nom vide désignerait n'importe quel dossier : ValueError."""
    with pytest.raises(ValueError) as erreur:
        w.trouver_racine("", "1_Walmart", depart=tmp_path)
    assert str(erreur.value) == "nom_notebook est vide : tout dossier « contiendrait » ce notebook"


# --------------------------------------------------------------------------------------------
# build_pipeline, rmse, RMSE_SCORER, evaluate_model
# --------------------------------------------------------------------------------------------


def test_build_pipeline_assemble_les_deux_etapes_et_clone_le_preprocesseur():
    """Étapes preprocess puis model ; ajuster le pipeline laisse le préprocesseur d'origine."""
    pre = preprocesseur()
    modele = LinearRegression()
    pipeline = w.build_pipeline(modele, pre)
    assert [nom for nom, _ in pipeline.steps] == ["preprocess", "model"]
    assert pipeline.named_steps["model"] is modele
    x, y = jeu_modele(avec_magasin_rare=False)
    pipeline.fit(x, y)
    assert pipeline.named_steps["preprocess"] is not pre
    assert not hasattr(pre, "transformers_")  # l'original n'a jamais été ajusté
    assert hasattr(pipeline.named_steps["preprocess"], "transformers_")


def test_build_pipeline_refuse_un_preprocesseur_qui_n_est_pas_un_estimateur():
    """None n'est pas clonable : TypeError, pas un pipeline cassé."""
    with pytest.raises(TypeError):
        w.build_pipeline(LinearRegression(), None)


def test_rmse_vaut_la_racine_de_l_erreur_quadratique_moyenne():
    """Erreurs 3 et 4 → racine de (9 + 16) / 2 ; zéro si parfait ; indifférent au signe."""
    assert w.rmse([0.0, 0.0], [3.0, 4.0]) == pytest.approx(math.sqrt(12.5))
    assert w.rmse([1.0, 2.0], [1.0, 2.0]) == 0.0
    assert w.rmse([1.0, 2.0], [0.0, 1.0]) == w.rmse([1.0, 2.0], [2.0, 3.0]) == 1.0


def test_rmse_refuse_des_longueurs_differentes():
    """Comparer 2 valeurs réelles à 3 prédictions est une erreur de l'appelant."""
    with pytest.raises(ValueError):
        w.rmse([1.0, 2.0], [1.0, 2.0, 3.0])


def test_rmse_refuse_une_valeur_manquante():
    """Un NaN dans les prédictions fausserait la moyenne : ValueError de scikit-learn."""
    with pytest.raises(ValueError):
        w.rmse([1.0, 2.0], [1.0, math.nan])


def test_rmse_scorer_est_un_rmse_negatif():
    """scikit-learn maximise les scores : le scorer rend l'opposé du RMSE."""
    x, y = jeu_modele(avec_magasin_rare=False)
    pipeline = w.build_pipeline(LinearRegression(), preprocesseur()).fit(x, y)
    score = w.RMSE_SCORER(pipeline, x, y)
    assert score < 0
    assert score == pytest.approx(-w.rmse(y, pipeline.predict(x)))


def test_evaluate_model_calcule_les_metriques_train_et_test():
    """Sur un modèle parfait en apprentissage, R² = 1 et RMSE = 0 ; le test est évalué à part."""
    x = pd.DataFrame({"Store": [1.0, 1.0, 2.0, 2.0], "Temperature": [1.0, 2.0, 3.0, 4.0]})
    y = pd.Series([10.0, 20.0, 30.0, 40.0])
    pre = ColumnTransformer([("num", "passthrough", ["Store", "Temperature"])])
    pipeline = w.build_pipeline(LinearRegression(), pre).fit(x, y)
    x_test = pd.DataFrame({"Store": [3.0, 3.0], "Temperature": [5.0, 6.0]})
    y_test = pd.Series([50.0, 61.0])  # le 61 est faux de 1 : erreur de test connue
    res = w.evaluate_model("modele", pipeline, x, x_test, y, y_test)
    assert res["model"] == "modele"
    assert res["train_R2"] == pytest.approx(1.0)
    assert res["train_RMSE"] == pytest.approx(0.0, abs=1e-9)
    assert res["train_MAE"] == pytest.approx(0.0, abs=1e-9)
    # prédictions 50 et 60 : erreurs 0 et 1
    assert res["test_RMSE"] == pytest.approx(math.sqrt(0.5))
    assert res["test_MAE"] == pytest.approx(0.5)
    assert res["test_R2"] == pytest.approx(1 - 1 / 60.5)  # 1 - somme des carrés / variance de y
    assert res["overfit_gap_RMSE"] == pytest.approx(res["test_RMSE"] - res["train_RMSE"])
    # avec une erreur d'apprentissage non nulle, l'écart est test - train (ni somme ni inverse)
    y_bruite = pd.Series([11.0, 19.0, 31.0, 39.0])
    bruite = w.evaluate_model("bruit", pipeline, x, x_test, y_bruite, y_test)
    assert bruite["train_RMSE"] == pytest.approx(1.0)
    assert bruite["overfit_gap_RMSE"] == pytest.approx(math.sqrt(0.5) - 1.0)


def test_evaluate_model_refuse_un_test_dont_x_et_y_different_en_longueur():
    """Trois lignes de X pour deux valeurs de y : ValueError de scikit-learn."""
    x = pd.DataFrame({"Store": [1.0, 2.0], "Temperature": [1.0, 2.0]})
    y = pd.Series([1.0, 2.0])
    pre = ColumnTransformer([("num", "passthrough", ["Store", "Temperature"])])
    pipeline = w.build_pipeline(LinearRegression(), pre).fit(x, y)
    x_long = pd.DataFrame({"Store": [1.0, 2.0, 3.0], "Temperature": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError):
        w.evaluate_model("m", pipeline, x, x_long, y, y)


def test_evaluate_model_ne_modifie_pas_les_donnees_inchangees():
    """L'évaluation lit les données : X et y restent inchangés."""
    x, y = jeu_modele(avec_magasin_rare=False)
    pipeline = w.build_pipeline(LinearRegression(), preprocesseur()).fit(x, y)
    x_avant, y_avant = x.copy(), y.copy()
    w.evaluate_model("m", pipeline, x, x, y, y)
    assert_frame_equal(x, x_avant)
    assert_series_equal(y, y_avant)


# --------------------------------------------------------------------------------------------
# chercher_alpha
# --------------------------------------------------------------------------------------------


def test_chercher_alpha_choisit_alpha_dans_la_grille_par_validation_croisee():
    """Le meilleur alpha vient de la grille ; best_estimator_ est un pipeline ajusté."""
    x, y = jeu_modele(avec_magasin_rare=False)
    alphas = np.logspace(-3, 3, 7)
    cv = KFold(n_splits=5, shuffle=True, random_state=0)
    recherche = w.chercher_alpha(Ridge(max_iter=10_000), alphas, cv, x, y, preprocesseur())
    assert recherche.best_params_["model__alpha"] in alphas
    assert list(recherche.cv_results_["param_model__alpha"]) == list(alphas)
    assert recherche.best_score_ < 0  # RMSE négatif
    assert "mean_train_score" in recherche.cv_results_  # return_train_score=True
    meilleur = recherche.best_estimator_
    assert meilleur.named_steps["model"].alpha == recherche.best_params_["model__alpha"]
    assert meilleur.predict(x).shape == (len(x),)
    assert recherche.n_jobs == 1


def test_chercher_alpha_donne_les_memes_scores_qu_un_gridsearchcv_direct():
    """Mêmes plis, même scorer, même grille : mêmes scores moyens, mêmes alphas, même meilleur."""
    x, y = jeu_modele(avec_magasin_rare=False)
    alphas = np.logspace(-3, 3, 7)
    cv = KFold(n_splits=5, shuffle=True, random_state=0)
    obtenu = w.chercher_alpha(Ridge(max_iter=10_000), alphas, cv, x, y, preprocesseur())
    direct = GridSearchCV(
        w.build_pipeline(Ridge(max_iter=10_000), preprocesseur()),
        param_grid={"model__alpha": alphas},
        scoring=w.RMSE_SCORER,
        cv=cv,
        n_jobs=1,
        return_train_score=True,
    ).fit(x, y)
    assert obtenu.best_score_ == direct.best_score_
    assert obtenu.best_params_ == direct.best_params_
    assert list(obtenu.cv_results_["mean_test_score"]) == list(
        direct.cv_results_["mean_test_score"]
    )
    assert list(obtenu.cv_results_["mean_train_score"]) == list(
        direct.cv_results_["mean_train_score"]
    )
    # sans les plis mélangés (cv par défaut), les scores seraient différents
    assert obtenu.best_score_ != GridSearchCV(
        w.build_pipeline(Ridge(max_iter=10_000), preprocesseur()),
        param_grid={"model__alpha": alphas},
        scoring=w.RMSE_SCORER,
    ).fit(x, y).best_score_


def test_chercher_alpha_refuse_une_grille_vide():
    """Pas d'alpha à essayer : ValueError, pas un modèle choisi au hasard."""
    x, y = jeu_modele(avec_magasin_rare=False)
    with pytest.raises(ValueError):
        w.chercher_alpha(Ridge(), [], 3, x, y, preprocesseur())


def test_chercher_alpha_refuse_un_estimateur_sans_alpha():
    """La régression linéaire n'a pas d'alpha : la recherche est refusée, pas ignorée."""
    x, y = jeu_modele(avec_magasin_rare=False)
    with pytest.raises(ValueError, match="alpha"):
        w.chercher_alpha(LinearRegression(), [0.1, 1.0], 3, x, y, preprocesseur())


def test_chercher_alpha_laisse_les_donnees_et_la_grille_inchangees():
    """La recherche ne modifie ni X, ni y, ni la grille d'alpha passée."""
    x, y = jeu_modele(avec_magasin_rare=False)
    x_avant, y_avant, alphas = x.copy(), y.copy(), np.array([0.1, 1.0, 10.0])
    w.chercher_alpha(Ridge(), alphas, 3, x, y, preprocesseur())
    assert_frame_equal(x, x_avant)
    assert_series_equal(y, y_avant)
    assert list(alphas) == [0.1, 1.0, 10.0]


# --------------------------------------------------------------------------------------------
# fr
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("valeur", "decimales", "attendu"),
    [
        (1234567.891, 0, "1 234 568"),
        (1234567.891, 2, "1 234 567.89"),
        (999.0, 0, "999"),
        (1000.0, 0, "1 000"),
        (-1234.5, 1, "-1 234.5"),
        (0.0, 0, "0"),
    ],
)
def test_fr_met_un_espace_entre_les_milliers(valeur, decimales, attendu):
    """Format français des montants : espace pour les milliers, décimales demandées."""
    assert w.fr(valeur, decimales) == attendu


def test_fr_sans_second_argument_n_a_aucune_decimale():
    """Sans second argument, aucune décimale : 2,6 devient 3."""
    assert w.fr(2.6) == "3"


def test_fr_refuse_un_texte():
    """Un texte n'est pas un nombre : ValueError au lieu d'un affichage trompeur."""
    with pytest.raises(ValueError):
        w.fr("mille")


def test_fr_refuse_un_nombre_de_decimales_negatif():
    """Une précision négative n'existe pas : ValueError."""
    with pytest.raises(ValueError):
        w.fr(1.5, -1)


# --------------------------------------------------------------------------------------------
# r2_regression, part_entre_magasins
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "attendu"),
    [
        ([1.0, 2.0, 3.0, 4.0], [3.0, 5.0, 7.0, 9.0], 1.0),  # b = 2a + 1 : relation exacte
        ([1.0, -1.0, 1.0, -1.0], [1.0, 1.0, -1.0, -1.0], 0.0),  # orthogonales et centrées
        ([1.0, -1.0, 1.0, -1.0], [2.0, 0.0, 0.0, -2.0], 0.5),  # b = a + e, e orthogonale à a
    ],
)
def test_r2_regression_vaut_un_zero_ou_un_demi(a, b, attendu):
    """R² de b expliqué par a : 1 si exact, 0 sans lien, 1/2 quand la moitié de la variance."""
    z = pd.DataFrame({"a": a, "b": b})
    assert w.r2_regression(z, "b", ["a"]) == pytest.approx(attendu, abs=1e-12)


def test_r2_regression_refuse_une_colonne_inconnue():
    """Une variable absente du tableau est une erreur de nom : KeyError."""
    z = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [1.0, 2.0, 4.0]})
    with pytest.raises(KeyError):
        w.r2_regression(z, "zzz", ["a"])
    with pytest.raises(KeyError):
        w.r2_regression(z, "b", ["zzz"])


def test_r2_regression_refuse_une_colonne_constante():
    """Une colonne constante n'a pas de variance à expliquer : ValueError, pas un NaN."""
    z = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [5.0, 5.0, 5.0]})
    with pytest.raises(ValueError, match="constante"):
        w.r2_regression(z, "b", ["a"])


def test_r2_regression_laisse_le_tableau_inchange():
    """Le calcul n'ajoute aucune colonne (constante comprise) au tableau passé."""
    z = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [1.0, 2.0, 4.0]})
    avant = z.copy()
    w.r2_regression(z, "b", ["a"])
    assert_frame_equal(z, avant)


@pytest.mark.parametrize(
    ("magasins", "valeurs", "attendu"),
    [
        ([1.0, 1.0, 2.0, 2.0], [10.0, 10.0, 30.0, 30.0], 1.0),  # tout tient au magasin
        ([1.0, 1.0, 2.0, 2.0], [10.0, 30.0, 10.0, 30.0], 0.0),  # mêmes moyennes de magasins
        # moyenne 25, moyennes de magasins 15 et 35 : 400 / 500 ; la valeur manquante est écartée
        ([1.0, 1.0, 2.0, 2.0, 2.0], [10.0, 20.0, 30.0, 40.0, math.nan], 0.8),
    ],
)
def test_part_entre_magasins_selon_la_repartition_des_valeurs(magasins, valeurs, attendu):
    """Variance des moyennes de magasins / variance totale, sur les valeurs observées."""
    x = pd.DataFrame({"Store": magasins, "CPI": valeurs})
    assert w.part_entre_magasins(x, "CPI") == pytest.approx(attendu)


def test_part_entre_magasins_refuse_une_variable_inconnue():
    """Variable absente : KeyError."""
    x = pd.DataFrame({"Store": [1.0, 2.0], "CPI": [1.0, 2.0]})
    with pytest.raises(KeyError):
        w.part_entre_magasins(x, "zzz")


def test_part_entre_magasins_refuse_une_variable_constante():
    """Une variable constante n'a pas de variance : ValueError, pas une division par zéro."""
    x = pd.DataFrame({"Store": [1.0, 2.0, 2.0], "CPI": [4.0, 4.0, 4.0]})
    with pytest.raises(ValueError, match="constante"):
        w.part_entre_magasins(x, "CPI")


def test_part_entre_magasins_laisse_le_tableau_inchange():
    """Les lignes à valeur manquante sont écartées dans une copie, pas dans le tableau passé."""
    x = pd.DataFrame({"Store": [1.0, 1.0, 2.0], "CPI": [10.0, math.nan, 30.0]})
    avant = x.copy()
    w.part_entre_magasins(x, "CPI")
    assert_frame_equal(x, avant)


# --------------------------------------------------------------------------------------------
# phrase_variable
# --------------------------------------------------------------------------------------------


def intervalles_cpi(signe_etabli: bool) -> pd.DataFrame:
    """Intervalles d'une variable fictive « CPI »."""
    return pd.DataFrame(
        {
            "coefficient": [43644.9],
            "ic95_bas": [-92509.9],
            "ic95_haut": [167321.1],
            "signe établi": [signe_etabli],
        },
        index=["num__CPI"],
    )


CHIFFRES = "Coefficient 43 645 $ par écart-type, intervalle à 95 % [-92 510 ; 167 321] : "
NON_ETABLI = "il contient 0 : ni le signe ni l'ordre de grandeur ne sont établis."
ETABLI = "il exclut 0 : le signe est établi, mais la valeur est très incertaine."


@pytest.mark.parametrize(
    ("etabli", "vif", "entre", "attendu"),
    [
        (  # VIF > 10 : colinéarité, quelle que soit la part entre magasins
            False,
            16.699,
            0.994,
            "- **CPI** : variable colinéaire avec les autres colonnes (VIF = 16.7, au-dessus de "
            f"10). {CHIFFRES}{NON_ETABLI}",
        ),
        (  # VIF ≤ 10 et plus de 50 % entre magasins
            True,
            4.945,
            0.806,
            "- **CPI** : variable fortement liée au magasin (81% de sa variance observée est "
            f"entre magasins, VIF = 4.9, sous le seuil de 10). {CHIFFRES}{ETABLI}",
        ),
        (  # VIF ≤ 10 et au plus 50 % entre magasins
            False,
            1.668,
            0.34,
            "- **CPI** : variable peu liée au magasin (34% de sa variance observée est entre "
            f"magasins, VIF = 1.7). {CHIFFRES}{NON_ETABLI}",
        ),
        (False, 10.0, 0.5, "variable peu liée au magasin (50%"),  # bornes exactes : strictes
        (False, 10.01, 0.1, "variable colinéaire avec les autres colonnes (VIF = 10.0,"),
        (False, 10.0, 0.51, "variable fortement liée au magasin (51%"),
    ],
)
def test_phrase_variable_selon_le_vif_et_la_part_entre_magasins(etabli, vif, entre, attendu):
    """Trois formulations selon le VIF (seuil 10) puis la part entre magasins (seuil 50 %)."""
    texte = w.phrase_variable(
        "CPI", intervalles_cpi(etabli), pd.Series({"CPI": vif}), pd.Series({"CPI": entre})
    )
    assert attendu in texte


def test_phrase_variable_refuse_une_variable_inconnue():
    """Un nom absent des intervalles ou des VIF : KeyError."""
    with pytest.raises(KeyError):
        w.phrase_variable(
            "Fuel_Price", intervalles_cpi(False), pd.Series({"CPI": 1.0}), pd.Series({"CPI": 1.0})
        )
    with pytest.raises(KeyError):
        w.phrase_variable(
            "CPI", intervalles_cpi(False), pd.Series({"Autre": 1.0}), pd.Series({"CPI": 1.0})
        )


def test_phrase_variable_laisse_les_tableaux_inchanges():
    """La lecture ne modifie pas les intervalles passés."""
    intervalles = intervalles_cpi(True)
    avant = intervalles.copy()
    w.phrase_variable("CPI", intervalles, pd.Series({"CPI": 2.0}), pd.Series({"CPI": 0.2}))
    assert_frame_equal(intervalles, avant)


# --------------------------------------------------------------------------------------------
# evaluer_un_decoupage
# --------------------------------------------------------------------------------------------

MODELES = {"LinearRegression": None, "Ridge": (Ridge(max_iter=10_000), np.logspace(-3, 3, 7))}


def test_evaluer_un_decoupage_rend_une_ligne_par_modele_reproductible():
    """Une ligne par modèle (graine, nom, scores, alpha) ; même graine, mêmes résultats."""
    x, y = jeu_modele(avec_magasin_rare=False)
    lignes, messages = w.evaluer_un_decoupage(3, x, y, preprocesseur(), MODELES)
    assert [ligne["modele"] for ligne in lignes] == ["LinearRegression", "Ridge"]
    assert {ligne["graine"] for ligne in lignes} == {3}
    assert math.isnan(lignes[0]["alpha"])
    assert lignes[1]["alpha"] in np.logspace(-3, 3, 7)
    for ligne in lignes:
        assert ligne["RMSE_CV"] > 0
        assert ligne["R2_test"] > 0.99  # ventes presque exactement linéaires
        assert ligne["RMSE_test"] > 0
    assert messages == []
    seconde, _ = w.evaluer_un_decoupage(3, x, y, preprocesseur(), MODELES)
    autre, _ = w.evaluer_un_decoupage(4, x, y, preprocesseur(), MODELES)
    assert lignes == seconde
    assert lignes[0]["RMSE_test"] != autre[0]["RMSE_test"]  # une autre graine change le découpage


@pytest.mark.parametrize(("magasin_rare", "inconnu"), [(True, True), (False, False)])
def test_evaluer_un_decoupage_compte_le_magasin_inconnu_sans_l_afficher(magasin_rare, inconnu):
    """Le magasin 6, vu une fois, est inconnu quelque part : compté, pas renvoyé en message."""
    x, y = jeu_modele(avec_magasin_rare=magasin_rare)
    lignes, messages = w.evaluer_un_decoupage(1, x, y, preprocesseur(), MODELES)
    assert all(ligne["magasin_inconnu_au_test"] is inconnu for ligne in lignes)
    assert messages == []


def test_evaluer_un_decoupage_suit_la_procedure_de_scikit_learn():
    """Découpage 80/20, 5 plis mélangés, même graine : mêmes scores qu'un calcul direct."""
    x, y = jeu_modele(avec_magasin_rare=False)
    graine = 7
    lignes, _ = w.evaluer_un_decoupage(graine, x, y, preprocesseur(), MODELES)
    x_app, x_val, y_app, y_val = train_test_split(x, y, test_size=0.20, random_state=graine)
    decoupage = KFold(n_splits=5, shuffle=True, random_state=graine)
    pipeline = w.build_pipeline(LinearRegression(), preprocesseur())
    scores = cross_validate(clone(pipeline), x_app, y_app, cv=decoupage, scoring=w.RMSE_SCORER)
    pipeline.fit(x_app, y_app)
    prediction = pipeline.predict(x_val)
    assert lignes[0]["RMSE_CV"] == pytest.approx(-scores["test_score"].mean())
    assert lignes[0]["R2_test"] == pytest.approx(r2_score(y_val, prediction))
    assert lignes[0]["RMSE_test"] == pytest.approx(w.rmse(y_val, prediction))
    recherche = w.chercher_alpha(
        Ridge(max_iter=10_000), np.logspace(-3, 3, 7), decoupage, x_app, y_app, preprocesseur()
    )
    assert lignes[1]["alpha"] == recherche.best_params_["model__alpha"]
    assert lignes[1]["RMSE_CV"] == pytest.approx(-recherche.best_score_)
    assert lignes[1]["R2_test"] == pytest.approx(r2_score(y_val, recherche.predict(x_val)))


def test_evaluer_un_decoupage_remonte_les_autres_avertissements():
    """Un avertissement qui n'est pas « unknown categories » est renvoyé au notebook."""
    x, y = jeu_modele(avec_magasin_rare=False)
    modeles = {"Ridge": (RidgeBavard(max_iter=10_000), np.array([0.1, 1.0]))}
    lignes, messages = w.evaluer_un_decoupage(1, x, y, preprocesseur(), modeles)
    assert messages
    assert set(messages) == {"avertissement maison"}
    assert lignes[0]["magasin_inconnu_au_test"] is False


def test_evaluer_un_decoupage_refuse_des_longueurs_differentes():
    """X de 60 lignes et y de 59 : ValueError du découpage."""
    x, y = jeu_modele(avec_magasin_rare=False)
    with pytest.raises(ValueError):
        w.evaluer_un_decoupage(1, x, y.iloc[:-1], preprocesseur(), MODELES)


def test_evaluer_un_decoupage_refuse_une_liste_de_modeles_vide():
    """Sans modèle à évaluer, il n'y a rien à rendre : ValueError plutôt qu'une liste muette."""
    x, y = jeu_modele(avec_magasin_rare=False)
    with pytest.raises(ValueError) as erreur:
        w.evaluer_un_decoupage(1, x, y, preprocesseur(), {})
    assert str(erreur.value) == "aucun modèle à évaluer : `modeles` est vide"


def test_evaluer_un_decoupage_laisse_x_et_y_inchanges():
    """Le découpage travaille sur des copies : X et y gardent leur contenu et leur ordre."""
    x, y = jeu_modele(avec_magasin_rare=False)
    x_avant, y_avant = x.copy(), y.copy()
    w.evaluer_un_decoupage(2, x, y, preprocesseur(), MODELES)
    assert_frame_equal(x, x_avant)
    assert_series_equal(y, y_avant)


# --------------------------------------------------------------------------------------------
# diagnostic_validation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("r2", "ecart", "attendu"),
    [
        (0.49, 0.0, FAIBLE),
        (0.5, 0.0, RIEN),
        (0.9, 0.101, "sur-apprentissage marqué."),
        (0.9, 0.10, MODERE),
        (0.9, 0.021, MODERE),
        (0.9, 0.02, RIEN),
        (0.9, -0.05, RIEN),
        (0.2, 0.5, FAIBLE),
    ],
)
def test_diagnostic_validation_selon_les_seuils(r2, ecart, attendu):
    """Seuils 0,5 / 0,10 / 0,02, tous stricts ; le sous-apprentissage passe avant le reste."""
    assert w.diagnostic_validation(r2, ecart) == attendu


@pytest.mark.parametrize(("r2", "ecart"), [(math.nan, 0.0), (0.9, math.nan)])
def test_diagnostic_validation_refuse_un_nan(r2, ecart):
    """Un NaN ferait échouer toutes les comparaisons et passerait pour « rien à signaler »."""
    with pytest.raises(ValueError) as erreur:
        w.diagnostic_validation(r2, ecart)
    assert str(erreur.value) == "diagnostic impossible : une des valeurs est NaN"


# --------------------------------------------------------------------------------------------
# afficher, afficher_markdown
# --------------------------------------------------------------------------------------------


def test_afficher_transmet_les_objets_et_enveloppe_le_markdown(monkeypatch):
    """afficher passe les objets tels quels à display ; afficher_markdown envoie un Markdown."""
    recus = []
    monkeypatch.setattr(w, "display", lambda *objets: recus.append(objets))
    w.afficher(1, "deux")
    w.afficher_markdown("**gras**")
    assert recus[0] == (1, "deux")
    (objet,) = recus[1]
    assert isinstance(objet, w.Markdown)
    assert objet.data == "**gras**"


def test_afficher_refuse_les_arguments_nommes_inconnus():
    """afficher n'accepte que des objets positionnels : un argument nommé est refusé."""
    with pytest.raises(TypeError):
        w.afficher(1, inclure=2)


def test_afficher_markdown_refuse_un_texte_manquant():
    """Sans texte, rien à afficher : TypeError, pas un affichage vide."""
    with pytest.raises(TypeError):
        w.afficher_markdown()
