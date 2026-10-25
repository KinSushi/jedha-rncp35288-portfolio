# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de conversion_bloc3.py : chaque fonction a des tests directs ET des tests inverses.

Un test direct montre ce que la fonction calcule ; un test inverse montre ce qu'elle refuse
(pytest.raises) ou ce qu'elle laisse intact (entrée inchangée). Les données sont minuscules et
construites à la main : les valeurs attendues se vérifient à la calculatrice.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal, assert_series_equal
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import conversion_bloc3 as c

# --------------------------------------------------------------------------------------------
# Données et estimateurs de test
# --------------------------------------------------------------------------------------------


class Score(BaseEstimator, ClassifierMixin):
    """Classifieur factice : la probabilité de classe 1 est la colonne « score » de X.

    Les probabilités sont ainsi connues d'avance et les métriques se calculent à la main.
    """

    def fit(self, X, y):
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, X):
        p = X["score"].to_numpy()
        return np.column_stack([1 - p, p])

    def predict(self, X):
        return (X["score"].to_numpy() >= 0.5).astype(int)


class Memoire(Score):
    """Même classifieur que Score, qui retient en plus les données reçues par fit."""

    def fit(self, X, y):
        self.vu_x_, self.vu_y_ = X.copy(), y.copy()
        return super().fit(X, y)


class SansProba(BaseEstimator, ClassifierMixin):
    """Classifieur factice sans predict_proba : il prédit 1 quand « score » vaut au moins 0,5."""

    def fit(self, X, y):
        self.classes_ = np.array([0, 1])
        return self

    def predict(self, X):
        return (X["score"].to_numpy() >= 0.5).astype(int)


X_TRAIN = pd.DataFrame({"score": [0.2, 0.7, 0.9, 0.4]})
Y_TRAIN = pd.Series([0, 1, 1, 0])
X_VALID = pd.DataFrame({"score": [0.1, 0.6, 0.4, 0.8, 0.9]})
Y_VALID = pd.Series([0, 0, 1, 1, 1])


def segments() -> pd.DataFrame:
    """Six visiteurs : A convertit 2 fois sur 3, B 1 fois sur 2, C 0 fois sur 1."""
    return pd.DataFrame(
        {
            "country": ["A", "A", "A", "B", "B", "C"],
            "converted": [1, 1, 0, 1, 0, 0],
        }
    )


# --------------------------------------------------------------------------------------------
# trouver_racine
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notebook_dans", "depart_dans", "racine_attendue"),
    [
        (".", ".", "."),  # le notebook est dans le dossier de départ
        ("2_Conversion", ".", "2_Conversion"),  # le noyau démarre dans le dossier parent
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
    trouve = c.trouver_racine("nb.ipynb", "2_Conversion", depart=tmp_path / depart_dans)
    assert trouve == (tmp_path / racine_attendue).resolve()


def test_trouver_racine_normalise_le_chemin_de_depart(tmp_path):
    """Un départ écrit avec « .. » est ramené à un chemin absolu normalisé (resolve)."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "nb.ipynb").write_text("{}", encoding="utf-8")
    trouve = c.trouver_racine("nb.ipynb", "2_Conversion", depart=tmp_path / "a" / "b" / "..")
    assert trouve == (tmp_path / "a").resolve()


def test_trouver_racine_par_defaut_part_du_dossier_courant(tmp_path, monkeypatch):
    """Sans départ explicite, la recherche commence au dossier de travail courant."""
    (tmp_path / "nb.ipynb").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert c.trouver_racine("nb.ipynb", "2_Conversion") == tmp_path.resolve()


def test_trouver_racine_sans_notebook_garde_le_depart_et_ne_cree_rien_inchange(tmp_path):
    """Aucun notebook trouvé : on garde le départ, sans créer de fichier (dossier inchangé)."""
    depart = tmp_path / "vide"
    depart.mkdir()
    (depart / "x.txt").write_text("x", encoding="utf-8")
    avant = sorted(p.name for p in depart.iterdir())
    assert c.trouver_racine("introuvable.ipynb", "2_Conversion", depart=depart) == depart.resolve()
    assert sorted(p.name for p in depart.iterdir()) == avant


def test_trouver_racine_refuse_un_nom_de_notebook_vide(tmp_path):
    """Un nom vide désignerait n'importe quel dossier : ValueError."""
    with pytest.raises(ValueError) as erreur:
        c.trouver_racine("", "2_Conversion", depart=tmp_path)
    assert str(erreur.value) == "nom_notebook est vide : tout dossier « contiendrait » ce notebook"


# --------------------------------------------------------------------------------------------
# conversion_summary
# --------------------------------------------------------------------------------------------


def test_conversion_summary_compte_trie_et_ignore_les_categories_vides():
    """Volume et taux par pays, du plus fort taux au plus faible : A (2/3), B (1/2), C (0)."""
    resume = c.conversion_summary(segments(), "country")
    assert list(resume.index) == ["A", "B", "C"]
    assert list(resume["count"]) == [3, 2, 1]
    assert list(resume["conversion_rate"]) == pytest.approx([2 / 3, 0.5, 0.0])
    assert list(resume.columns) == ["count", "conversion_rate"]
    df = segments()
    df["country"] = pd.Categorical(df["country"], categories=["A", "B", "C", "Z"])
    # pandas 3 : groupby n'affiche que les modalités observées (observed=True par défaut)
    assert "Z" not in c.conversion_summary(df, "country").index
    assert list(c.conversion_summary(df, "country").index) == ["A", "B", "C"]


def test_conversion_summary_refuse_une_colonne_ou_une_cible_absente():
    """Sans la colonne demandée ou sans converted : KeyError."""
    with pytest.raises(KeyError):
        c.conversion_summary(segments(), "zzz")
    with pytest.raises(KeyError):
        c.conversion_summary(segments().drop(columns="converted"), "country")


def test_conversion_summary_refuse_une_cible_ecrite_en_texte():
    """Une cible « oui » / « non » n'a pas de moyenne : TypeError, pas un taux de conversion."""
    df = segments()
    df["converted"] = df["converted"].map({1: "oui", 0: "non"})
    with pytest.raises(TypeError):
        c.conversion_summary(df, "country")


def test_conversion_summary_laisse_le_tableau_inchange():
    """La synthèse ne modifie pas le tableau d'entrée."""
    df = segments()
    avant = df.copy()
    c.conversion_summary(df, "country")
    assert_frame_equal(df, avant)


# --------------------------------------------------------------------------------------------
# make_one_hot_encoder, make_model_pipeline
# --------------------------------------------------------------------------------------------


def test_make_one_hot_encoder_garde_ou_retire_une_modalite_et_ignore_l_inconnue():
    """drop=None : une colonne par modalité ; drop='first' : une de moins ; l'inconnue donne 0."""
    donnees = pd.DataFrame({"pays": ["A", "B", "C", "A"]})
    complet = c.make_one_hot_encoder().fit_transform(donnees)
    reduit = c.make_one_hot_encoder(drop="first").fit_transform(donnees)
    assert isinstance(complet, np.ndarray)  # sparse_output=False : tableau dense
    assert complet.shape == (4, 3)
    assert reduit.shape == (4, 2)
    assert reduit.tolist() == [[0, 0], [1, 0], [0, 1], [0, 0]]  # A est la référence
    encodeur = c.make_one_hot_encoder().fit(pd.DataFrame({"pays": ["A", "B"]}))
    assert encodeur.transform(pd.DataFrame({"pays": ["Z"]})).tolist() == [[0.0, 0.0]]


def test_make_one_hot_encoder_refuse_un_drop_inconnu():
    """drop='nimporte' n'existe pas : ValueError à l'ajustement."""
    with pytest.raises(ValueError):
        c.make_one_hot_encoder(drop="nimporte").fit(pd.DataFrame({"pays": ["A", "B"]}))


def test_make_model_pipeline_assemble_les_etapes_et_clone_le_preprocesseur():
    """Étapes preprocess puis model ; ajuster le pipeline laisse le préprocesseur d'origine."""
    pre = ColumnTransformer([("num", StandardScaler(), ["score"])])
    modele = LogisticRegression()
    pipeline = c.make_model_pipeline(pre, modele)
    assert [nom for nom, _ in pipeline.steps] == ["preprocess", "model"]
    assert pipeline.named_steps["model"] is modele
    pipeline.fit(X_VALID, Y_VALID)
    assert pipeline.named_steps["preprocess"] is not pre
    assert not hasattr(pre, "transformers_")  # l'original n'a jamais été ajusté


def test_make_model_pipeline_refuse_un_preprocesseur_qui_n_est_pas_un_estimateur():
    """None n'est pas clonable : TypeError, pas un pipeline cassé."""
    with pytest.raises(TypeError):
        c.make_model_pipeline(None, LogisticRegression())


# --------------------------------------------------------------------------------------------
# optimize_threshold
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("y", "proba", "grille", "seuil_attendu", "f1_attendu"),
    [
        # classes séparables : le F1 vaut 1 dès que le seuil dépasse 0,2 ; premier seuil = 0,205
        ([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9], None, 0.205, 1.0),
        # cas non séparable : meilleur F1 = 6/7, premier seuil à le donner = 0,105
        ([0, 0, 1, 1, 1], [0.1, 0.6, 0.4, 0.8, 0.9], None, 0.105, 6 / 7),
        # grille imposée ; à égalité de F1 (0,5 et 0,6 donnent 1), le premier de la grille
        ([0, 1], [0.3, 0.7], [0.9, 0.5, 0.6, 0.1], 0.5, 1.0),
        # une probabilité égale au seuil est prédite positive (>=)
        ([0, 1], [0.2, 0.5], [0.5], 0.5, 1.0),
        # aucun converti : F1 = 0 partout (zero_division=0), donc le premier seuil
        ([0, 0, 0], [0.1, 0.2, 0.3], None, 0.01, 0.0),
    ],
)
def test_optimize_threshold_rend_le_premier_seuil_qui_maximise_le_f1(
    y, proba, grille, seuil_attendu, f1_attendu
):
    """Le seuil rendu est le premier de la grille (0,01 à 0,99 par défaut) au meilleur F1."""
    seuil, f1 = c.optimize_threshold(np.array(y), np.array(proba), grid=grille)
    assert seuil == pytest.approx(seuil_attendu, abs=1e-9)
    assert f1 == pytest.approx(f1_attendu)


def test_optimize_threshold_ne_leve_aucun_avertissement_quand_le_f1_est_indefini():
    """Aucun converti : le F1 vaut 0 par choix (zero_division=0), sans avertissement."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        seuil, f1 = c.optimize_threshold(np.array([0, 0, 0]), np.array([0.1, 0.2, 0.3]))
    assert seuil == pytest.approx(0.01)
    assert f1 == 0.0


def test_optimize_threshold_refuse_une_grille_vide():
    """Sans seuil à essayer : ValueError, pas un seuil inventé."""
    with pytest.raises(ValueError) as erreur:
        c.optimize_threshold(np.array([0, 1]), np.array([0.2, 0.8]), grid=[])
    assert str(erreur.value) == "la grille de seuils est vide"


def test_optimize_threshold_refuse_des_longueurs_differentes():
    """Trois probabilités pour deux étiquettes : ValueError de scikit-learn."""
    with pytest.raises(ValueError):
        c.optimize_threshold(np.array([0, 1]), np.array([0.2, 0.8, 0.5]))


def test_optimize_threshold_refuse_une_etiquette_manquante():
    """Une étiquette NaN n'est ni 0 ni 1 : ValueError de scikit-learn, pas un seuil trompeur."""
    with pytest.raises(ValueError):
        c.optimize_threshold(np.array([0.0, math.nan]), np.array([0.2, 0.8]))


def test_optimize_threshold_refuse_des_probabilites_ecrites_en_texte():
    """Des probabilités en texte (« 0.8 ») ne se comparent pas à un seuil : TypeError."""
    with pytest.raises(TypeError):
        c.optimize_threshold(np.array([0, 1]), np.array(["0.2", "0.8"]))


def test_optimize_threshold_laisse_les_tableaux_inchanges():
    """Les étiquettes et les probabilités passées ne sont pas modifiées."""
    y, p = np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])
    y_avant, p_avant = y.copy(), p.copy()
    c.optimize_threshold(y, p)
    assert (y == y_avant).all()
    assert (p == p_avant).all()


# --------------------------------------------------------------------------------------------
# evaluate_classifier
# --------------------------------------------------------------------------------------------


def test_evaluate_classifier_avec_un_seuil_impose():
    """Seuil 0,5 : validation TP=2, FP=1, FN=1 → précision, rappel, F1 = 2/3 ; ROC-AUC 5/6."""
    pipe = Pipeline([("model", Score())])
    metriques, rendu = c.evaluate_classifier(
        "stub", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID, threshold=0.5
    )
    assert rendu is pipe
    assert metriques["model"] == "stub"
    assert metriques["threshold"] == 0.5
    assert metriques["train_f1"] == 1.0  # (0,2 ; 0,7 ; 0,9 ; 0,4) à 0,5 : toutes bien classées
    assert metriques["valid_f1"] == pytest.approx(2 / 3)
    assert metriques["valid_precision"] == pytest.approx(2 / 3)
    assert metriques["valid_recall"] == pytest.approx(2 / 3)
    assert metriques["valid_accuracy"] == pytest.approx(0.6)
    assert metriques["valid_roc_auc"] == pytest.approx(5 / 6)
    assert metriques["valid_pr_auc"] == pytest.approx((1 + 1 + 0.75) / 3)


def test_evaluate_classifier_sans_seuil_impose_ou_sans_probabilites():
    """Sans seuil imposé : celui de la validation (0,105 → F1 6/7). Sans predict_proba : NaN."""
    pipe = Pipeline([("model", Score())])
    metriques, _ = c.evaluate_classifier("stub", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID)
    assert metriques["threshold"] == pytest.approx(0.105, abs=1e-9)
    assert metriques["valid_f1"] == pytest.approx(6 / 7)
    assert metriques["valid_precision"] == pytest.approx(0.75)
    assert metriques["valid_recall"] == 1.0
    assert metriques["valid_accuracy"] == pytest.approx(0.8)
    # à ce seuil toute l'apprentissage est prédit positif : TP=2, FP=2 → F1 = 4/6
    assert metriques["train_f1"] == pytest.approx(2 / 3)

    dur, _ = c.evaluate_classifier(
        "dur", Pipeline([("model", SansProba())]), X_TRAIN, Y_TRAIN, X_VALID, Y_VALID
    )
    assert math.isnan(dur["threshold"])
    assert math.isnan(dur["valid_roc_auc"])
    assert math.isnan(dur["valid_pr_auc"])
    assert dur["valid_f1"] == pytest.approx(2 / 3)  # prédiction directe à 0,5
    assert dur["train_f1"] == 1.0

    x_zero = pd.DataFrame({"score": [0.0, 0.0, 0.0]})
    y_zero = pd.Series([1, 0, 1])
    zero, _ = c.evaluate_classifier(
        "zero", Pipeline([("model", Score())]), x_zero, y_zero, x_zero, y_zero, threshold=0.5
    )
    assert (zero["valid_precision"], zero["valid_recall"], zero["valid_f1"]) == (0.0, 0.0, 0.0)


def test_evaluate_classifier_ne_leve_aucun_avertissement_quand_les_metriques_sont_indefinies():
    """Ni positif réel ni positif prédit : F1, précision et rappel valent 0, sans avertissement."""
    x = pd.DataFrame({"score": [0.1, 0.2, 0.3]})
    y = pd.Series([0, 0, 0])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        metriques, _ = c.evaluate_classifier(
            "nul", Pipeline([("model", SansProba())]), x, y, x, y
        )
    assert metriques["train_f1"] == 0.0
    assert metriques["valid_f1"] == 0.0
    assert metriques["valid_precision"] == 0.0
    assert metriques["valid_recall"] == 0.0
    assert metriques["valid_accuracy"] == 1.0


@pytest.mark.parametrize("seuil", [-0.1, 1.1, math.nan])
def test_evaluate_classifier_refuse_un_seuil_hors_de_zero_un(seuil):
    """Un seuil de probabilité hors de [0, 1] (ou NaN) est refusé avant tout ajustement."""
    pipe = Pipeline([("model", Score())])
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        c.evaluate_classifier("stub", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID, threshold=seuil)


def test_evaluate_classifier_accepte_les_seuils_extremes_zero_et_un():
    """0 et 1 sont des seuils valides (bornes incluses) : 0 prédit tout positif, 1 presque rien.

    Au seuil 0, la validation a TP=3 et FP=2 : précision 0,6, rappel 1, F1 0,75. Au seuil 1,
    aucune probabilité n'atteint 1 : F1 0 et exactitude 2/5 ; une probabilité de 1 exactement,
    elle, est prédite positive.
    """
    pipe = Pipeline([("model", Score())])
    zero, _ = c.evaluate_classifier("s0", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID, threshold=0.0)
    assert (zero["threshold"], zero["valid_recall"]) == (0.0, 1.0)
    assert zero["valid_precision"] == pytest.approx(0.6)
    assert zero["valid_f1"] == pytest.approx(0.75)
    assert zero["train_f1"] == pytest.approx(2 / 3)
    un, _ = c.evaluate_classifier("s1", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID, threshold=1.0)
    assert (un["threshold"], un["valid_f1"], un["train_f1"]) == (1.0, 0.0, 0.0)
    assert un["valid_accuracy"] == pytest.approx(0.4)
    x_certain = pd.DataFrame({"score": [1.0, 0.3]})
    y_certain = pd.Series([1, 0])
    certain, _ = c.evaluate_classifier(
        "s1", pipe, x_certain, y_certain, x_certain, y_certain, threshold=1.0
    )
    assert certain["valid_f1"] == 1.0


def test_evaluate_classifier_ajuste_le_modele_sur_l_apprentissage_seulement():
    """Le modèle voit X_TRAIN et Y_TRAIN, jamais la validation : pas de fuite vers le score."""
    pipe = Pipeline([("model", Memoire())])
    c.evaluate_classifier("m", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID, threshold=0.5)
    modele = pipe.named_steps["model"]
    assert_frame_equal(modele.vu_x_, X_TRAIN)
    assert_series_equal(modele.vu_y_, Y_TRAIN)


def test_evaluate_classifier_refuse_une_validation_sans_les_colonnes_apprises():
    """La validation doit porter les colonnes vues à l'apprentissage : ValueError sinon."""
    pre = ColumnTransformer([("num", StandardScaler(), ["score"])])
    pipe = c.make_model_pipeline(pre, LogisticRegression())
    autre = X_VALID.rename(columns={"score": "autre"})
    with pytest.raises(ValueError, match="columns are missing"):
        c.evaluate_classifier("lr", pipe, X_TRAIN, Y_TRAIN, autre, Y_VALID)


def test_evaluate_classifier_refuse_un_seuil_ecrit_en_texte():
    """Un seuil « 0.5 » écrit en texte n'est pas comparé à des probabilités : TypeError."""
    pipe = Pipeline([("model", Score())])
    with pytest.raises(TypeError):
        c.evaluate_classifier("stub", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID, threshold="0.5")


#: Types de cible admis par evaluate_classifier : tous ne portent que 0 et 1 (ou False / True).
TYPES_DE_CIBLE_ZERO_UN = [
    "int64",
    "int8",
    "uint8",
    "float64",
    "float32",
    "bool",
    "Int64",
    "boolean",
    "object",
    "category",
]


@pytest.mark.parametrize("type_cible", TYPES_DE_CIBLE_ZERO_UN)
def test_premisse_les_metriques_ne_dependent_pas_du_type_des_predictions(type_cible):
    """Preuve de MUTANTS_EQUIVALENTS.md (astype(int) contre astype(None) sur les prédictions).

    1. « probabilité >= seuil » rend toujours des booléens, que les probabilités soient des
       flottants 64 ou 32 bits, des entiers ou des objets Python (tableau de type object) : les
       deux versions convertissent donc les mêmes booléens, en 0 / 1 ou en 0.0 / 1.0.
    2. Sur toute cible admise (0 / 1 de ce type ; deux classes, que des 0 ou que des 1), ces
       prédictions entières ou flottantes donnent les mêmes F1, précision, rappel et exactitude,
       ou le même refus de scikit-learn (même type, même message : cas d'une cible de type
       object, que scikit-learn ne sait pas classer, « unknown »).
    Une cible hors {0, 1}, seule capable de les distinguer (message d'erreur de scikit-learn),
    est refusée avant : test_evaluate_classifier_refuse_une_cible_autre_que_zero_ou_un.
    """

    def issue(metrique, vrai, predit):
        """Valeur rendue, ou type et message de l'exception levée."""
        try:
            return ("valeur", metrique(vrai, predit))
        except ValueError as exc:
            return ("refus", type(exc).__name__, str(exc))

    probabilites = [
        np.array([0.2, 0.9, 0.4, 0.1, 0.7]),
        np.array([0.2, 0.9, 0.4, 0.1, 0.7], dtype="float32"),
        np.array([0, 1, 0, 0, 1]),
        np.array([0.2, 0.9, 0.4, 0, 1], dtype=object),
    ]
    for proba in probabilites:
        assert (proba >= 0.5).dtype == np.bool_
    proba = probabilites[0]
    entieres = (proba >= 0.5).astype(int)
    flottantes = (proba >= 0.5).astype(None)
    assert entieres.dtype == np.int64
    assert flottantes.dtype == np.float64  # le mutant change bien le type
    metriques = (
        lambda vrai, predit: f1_score(vrai, predit, zero_division=0),
        lambda vrai, predit: precision_score(vrai, predit, zero_division=0),
        lambda vrai, predit: recall_score(vrai, predit, zero_division=0),
        accuracy_score,
    )
    for vrai in ([0, 1, 1, 0, 1], [0, 0, 0, 0, 0], [1, 1, 1, 1, 1]):
        cible = pd.Series(vrai).astype(type_cible)
        for metrique in metriques:
            assert issue(metrique, cible, entieres) == issue(metrique, cible, flottantes)


@pytest.mark.parametrize(
    ("argument", "cible", "refusees"),
    [
        # entrée du contrôle indépendant du 20/10/2026 : aucune classe 1, que des 2
        ("y_train", pd.Series([2, 2, 2, 2]), "2"),
        ("y_valid", pd.Series([2, 2, 2, 2, 2]), "2"),
        # convention -1 / 1 d'autres outils : -1 ne veut pas dire « non converti » ici
        ("y_train", pd.Series([-1, 1, 1, -1]), "-1"),
        # une probabilité passée à la place de la cible
        ("y_valid", pd.Series([0.0, 0.5, 1.0, 1.0, 0.0]), "0.5"),
        # étiquettes manquantes : NaN d'un flottant, pd.NA d'un entier nullable
        ("y_valid", pd.Series([0.0, math.nan, 1.0, 1.0, 1.0]), "nan"),
        ("y_train", pd.Series([0, None, 1, 0], dtype="Int64"), "<NA>"),
        # cible lue comme du texte : « 0 » n'est pas 0 ; les valeurs refusées sont triées
        ("y_train", pd.Series(["1", "0", "1", "0"]), "'0', '1'"),
    ],
)
def test_evaluate_classifier_refuse_une_cible_autre_que_zero_ou_un(argument, cible, refusees):
    """Une cible qui contient autre chose que 0 et 1 est refusée, en apprentissage comme en
    validation, avec un message qui nomme l'argument et les valeurs refusées.

    Avant ce refus (contrôle du 20/10/2026), la cible [2, 2, 2, 2] au seuil 1 échouait dans
    scikit-learn avec un message qui dépendait du type des prédictions (« [0 2] » ou « [0. 2.] »).
    """
    y_train = cible if argument == "y_train" else Y_TRAIN
    y_valid = cible if argument == "y_valid" else Y_VALID
    with pytest.raises(ValueError) as erreur:
        c.evaluate_classifier(
            "stub", Pipeline([("model", Score())]), X_TRAIN, y_train, X_VALID, y_valid,
            threshold=1.0,
        )
    assert str(erreur.value) == (
        f"{argument} ne doit contenir que 0 (non converti) et 1 (converti) ; "
        f"valeurs refusées : {refusees}"
    )


def test_evaluate_classifier_refuse_la_cible_avant_d_ajuster_le_modele():
    """Une cible refusée l'est AVANT l'ajustement : le modèle n'a rien vu (état inchangé)."""
    pipe = Pipeline([("model", Memoire())])
    with pytest.raises(ValueError, match="y_valid ne doit contenir que 0"):
        c.evaluate_classifier(
            "m", pipe, X_TRAIN, Y_TRAIN, X_VALID, pd.Series([0, 1, 2, 1, 0]), threshold=0.5
        )
    assert not hasattr(pipe.named_steps["model"], "vu_x_")


@pytest.mark.parametrize("type_cible", [t for t in TYPES_DE_CIBLE_ZERO_UN if t != "object"])
def test_evaluate_classifier_accepte_toute_cible_zero_un(type_cible):
    """Une cible 0 / 1 est admise quel que soit son type numérique (booléens, flottants, Int64,
    catégories...) : mêmes métriques que la cible entière. Une seule classe est admise aussi.
    (Une cible de type object passe ce refus, mais scikit-learn ne sait pas la classer.)

    Une seule classe : que des 1 en apprentissage ; au seuil 0,5, le stub prédit 0, 1, 1, 0 :
    TP = 2, FN = 2, aucun faux positif, donc F1 = 2 / 3.
    """
    reference, _ = c.evaluate_classifier(
        "ref", Pipeline([("model", Score())]), X_TRAIN, Y_TRAIN, X_VALID, Y_VALID
    )
    metriques, _ = c.evaluate_classifier(
        "ref",
        Pipeline([("model", Score())]),
        X_TRAIN,
        Y_TRAIN.astype(type_cible),
        X_VALID,
        Y_VALID.astype(type_cible),
    )
    assert metriques == reference
    une_classe, _ = c.evaluate_classifier(
        "un",
        Pipeline([("model", Score())]),
        X_TRAIN,
        pd.Series([1, 1, 1, 1]).astype(type_cible),
        X_VALID,
        Y_VALID.astype(type_cible),
        threshold=0.5,
    )
    assert une_classe["train_f1"] == pytest.approx(2 / 3)


def test_evaluate_classifier_predit_positif_quand_la_probabilite_egale_le_seuil():
    """Une probabilité exactement égale au seuil est classée positive (>=), pas négative."""
    x = pd.DataFrame({"score": [0.5, 0.2]})
    y = pd.Series([1, 0])
    metriques, _ = c.evaluate_classifier(
        "egal", Pipeline([("model", Score())]), x, y, x, y, threshold=0.5
    )
    assert metriques["valid_f1"] == 1.0
    assert metriques["train_f1"] == 1.0


def test_evaluate_classifier_refuse_le_seuil_avant_d_ajuster_le_modele():
    """Un seuil refusé l'est AVANT l'ajustement : le modèle n'a rien vu (état inchangé)."""
    pipe = Pipeline([("model", Memoire())])
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        c.evaluate_classifier("m", pipe, X_TRAIN, Y_TRAIN, X_VALID, Y_VALID, threshold=1.5)
    assert not hasattr(pipe.named_steps["model"], "vu_x_")


def test_evaluate_classifier_sans_aucun_converti_donne_zero_et_non_un():
    """Aucun converti et aucune prédiction positive : F1, précision et rappel valent 0, pas 1."""
    x = pd.DataFrame({"score": [0.0, 0.0, 0.0]})
    y = pd.Series([0, 0, 0])
    metriques, _ = c.evaluate_classifier(
        "vide", Pipeline([("model", SansProba())]), x, y, x, y
    )
    assert metriques["train_f1"] == 0.0
    assert (metriques["valid_f1"], metriques["valid_precision"], metriques["valid_recall"]) == (
        0.0,
        0.0,
        0.0,
    )
    assert metriques["valid_accuracy"] == 1.0


def test_evaluate_classifier_refuse_un_x_et_un_y_de_longueurs_differentes():
    """Quatre lignes de X pour trois étiquettes : ValueError de scikit-learn."""
    pipe = Pipeline([("model", LogisticRegression())])
    with pytest.raises(ValueError):
        c.evaluate_classifier("lr", pipe, X_TRAIN, Y_TRAIN.iloc[:3], X_VALID, Y_VALID)


def test_evaluate_classifier_laisse_les_donnees_inchangees():
    """L'évaluation lit X et y sans les modifier."""
    x_avant, y_avant = X_TRAIN.copy(), Y_TRAIN.copy()
    xv_avant, yv_avant = X_VALID.copy(), Y_VALID.copy()
    c.evaluate_classifier(
        "stub", Pipeline([("model", Score())]), X_TRAIN, Y_TRAIN, X_VALID, Y_VALID
    )
    assert_frame_equal(X_TRAIN, x_avant)
    assert_series_equal(Y_TRAIN, y_avant)
    assert_frame_equal(X_VALID, xv_avant)
    assert_series_equal(Y_VALID, yv_avant)


# --------------------------------------------------------------------------------------------
# resume_clusters
# --------------------------------------------------------------------------------------------


def visiteurs() -> tuple[pd.DataFrame, pd.Series]:
    """Quatre visiteurs : deux dans le cluster 0 (jeunes, convertis), deux dans le cluster 1."""
    x = pd.DataFrame(
        {
            "age": [20, 30, 40, 50],
            "new_user": [1, 0, 1, 1],
            "total_pages_visited": [10, 20, 2, 4],
            "country": ["China", "US", "US", "UK"],
            "source": ["Ads", "Direct", "Seo", "Seo"],
        }
    )
    return x, pd.Series([1, 1, 0, 0])


def test_resume_clusters_calcule_taille_conversion_parts_et_ordre():
    """Cluster 0 = lignes 0 et 1 ; cluster 1 = lignes 2 et 3 ; tri par conversion décroissante."""
    x, y = visiteurs()
    resume = c.resume_clusters(x, y, [0, 0, 1, 1])
    assert list(resume.index) == [0, 1]
    zero, un = resume.loc[0], resume.loc[1]
    assert (zero["n"], zero["conversion_rate"], zero["age_mean"]) == (2, 1.0, 25.0)
    assert (zero["new_user_rate"], zero["pages_mean"]) == (0.5, 15.0)
    assert (zero["china_share"], zero["us_share"]) == (0.5, 0.5)
    assert (zero["ads_share"], zero["direct_share"], zero["seo_share"]) == (0.5, 0.5, 0.0)
    assert (un["n"], un["conversion_rate"], un["age_mean"]) == (2, 0.0, 45.0)
    assert (un["china_share"], un["us_share"]) == (0.0, 0.5)
    assert (un["ads_share"], un["direct_share"], un["seo_share"]) == (0.0, 0.0, 1.0)
    # si les étiquettes sont permutées, le cluster le plus convertisseur reste en tête
    permute = c.resume_clusters(x, y, [1, 1, 0, 0])
    assert list(permute.index) == [1, 0]


def test_resume_clusters_parts_de_pays_et_de_sources_sur_des_clusters_desequilibres():
    """Cluster 0 : US, US, China ; cluster 1 : UK, US, UK. Chaque part est comptée par cluster."""
    x = pd.DataFrame(
        {
            "age": [20, 22, 24, 50, 52, 54],
            "new_user": [1, 1, 0, 0, 0, 1],
            "total_pages_visited": [9, 12, 15, 2, 3, 4],
            "country": ["US", "US", "China", "UK", "US", "UK"],
            "source": ["Ads", "Ads", "Direct", "Seo", "Seo", "Direct"],
        }
    )
    resume = c.resume_clusters(x, pd.Series([1, 1, 0, 0, 0, 1]), [0, 0, 0, 1, 1, 1])
    zero, un = resume.loc[0], resume.loc[1]
    assert list(resume.index) == [0, 1]  # 2/3 de conversion contre 1/3
    assert (zero["us_share"], zero["china_share"]) == pytest.approx((2 / 3, 1 / 3))
    assert (un["us_share"], un["china_share"]) == pytest.approx((1 / 3, 0.0))
    assert (zero["ads_share"], zero["direct_share"], zero["seo_share"]) == pytest.approx(
        (2 / 3, 1 / 3, 0.0)
    )
    assert (un["ads_share"], un["direct_share"], un["seo_share"]) == pytest.approx(
        (0.0, 1 / 3, 2 / 3)
    )
    assert (zero["new_user_rate"], un["new_user_rate"]) == pytest.approx((2 / 3, 1 / 3))


def test_resume_clusters_refuse_un_tableau_sans_une_colonne_decrite():
    """Sans la colonne age, l'âge moyen du cluster est impossible : KeyError qui nomme age."""
    x, y = visiteurs()
    with pytest.raises(KeyError, match="age"):
        c.resume_clusters(x.drop(columns="age"), y, [0, 0, 1, 1])


def test_resume_clusters_refuse_des_longueurs_differentes():
    """Trois étiquettes pour quatre visiteurs : ValueError."""
    x, y = visiteurs()
    message = "x, y et les étiquettes doivent avoir la même longueur"
    with pytest.raises(ValueError) as erreur:
        c.resume_clusters(x, y, [0, 0, 1])
    assert str(erreur.value) == message
    with pytest.raises(ValueError) as erreur:
        c.resume_clusters(x, y.iloc[:3], [0, 0, 1, 1])
    assert str(erreur.value) == message


def test_resume_clusters_laisse_x_et_y_inchanges():
    """La cible est ajoutée à une copie : X n'a pas de colonne converted ou cluster ensuite."""
    x, y = visiteurs()
    x_avant, y_avant = x.copy(), y.copy()
    c.resume_clusters(x, y, [0, 0, 1, 1])
    assert_frame_equal(x, x_avant)
    assert_series_equal(y, y_avant)


# --------------------------------------------------------------------------------------------
# statut_visiteurs, lecture_ecart_apprentissage, lecture_stabilite
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("taux", "attendu"),
    [
        (1.0, "uniquement des nouveaux visiteurs"),
        (0.991, "uniquement des nouveaux visiteurs"),
        (0.99, "99% de nouveaux visiteurs"),  # seuil strict
        (0.5, "50% de nouveaux visiteurs"),
        (0.01, "1% de nouveaux visiteurs"),  # seuil strict
        (0.009, "uniquement des visiteurs récurrents"),
        (0.0, "uniquement des visiteurs récurrents"),
    ],
)
def test_statut_visiteurs_selon_la_part_de_nouveaux(taux, attendu):
    """Au-delà de 99 % : uniquement nouveaux ; en deçà de 1 % : uniquement récurrents."""
    assert c.statut_visiteurs(taux) == attendu


@pytest.mark.parametrize("taux", [-0.1, 1.1, math.nan])
def test_statut_visiteurs_refuse_une_part_hors_de_zero_un(taux):
    """Une part de visiteurs n'est pas négative, ni supérieure à 1, ni NaN : ValueError."""
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        c.statut_visiteurs(taux)


def test_statut_visiteurs_refuse_une_part_ecrite_en_texte():
    """« 0,5 » écrit en texte n'est pas une part : TypeError, pas une phrase fabriquée."""
    with pytest.raises(TypeError):
        c.statut_visiteurs("0,5")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("ecart", "attendu"),
    [
        (0.0003, "négligeable"),
        (-0.019, "négligeable"),
        (0.02, "à surveiller"),  # seuil strict
        (-0.02, "à surveiller"),
        (0.2, "à surveiller"),
    ],
)
def test_lecture_ecart_apprentissage_selon_le_seuil(ecart, attendu):
    """L'écart apprentissage - validation est jugé en valeur absolue contre 0,02."""
    assert c.lecture_ecart_apprentissage(ecart) == attendu


@pytest.mark.parametrize(
    ("dispersion", "attendu"),
    [
        (0.0, "score stable, indépendant du découpage"),
        (0.0199, "score stable, indépendant du découpage"),
        (0.02, "score sensible au découpage"),  # seuil strict
        (0.3, "score sensible au découpage"),
    ],
)
def test_lecture_stabilite_selon_le_seuil(dispersion, attendu):
    """L'écart-type du F1 entre plis est comparé strictement à 0,02."""
    assert c.lecture_stabilite(dispersion) == attendu


def test_lectures_refusent_un_nan_et_un_ecart_type_negatif():
    """NaN et écart-type négatif ne sont pas des mesures : ValueError."""
    with pytest.raises(ValueError) as erreur:
        c.lecture_ecart_apprentissage(math.nan)
    assert str(erreur.value) == "écart non défini (NaN)"
    with pytest.raises(ValueError, match="invalide"):
        c.lecture_stabilite(math.nan)
    with pytest.raises(ValueError, match="invalide"):
        c.lecture_stabilite(-0.01)


# --------------------------------------------------------------------------------------------
# afficher, afficher_markdown
# --------------------------------------------------------------------------------------------


def test_afficher_transmet_les_objets_et_enveloppe_le_markdown(monkeypatch):
    """afficher passe les objets tels quels à display ; afficher_markdown envoie un Markdown."""
    recus = []
    monkeypatch.setattr(c, "display", lambda *objets: recus.append(objets))
    c.afficher(1, "deux")
    c.afficher_markdown("**gras**")
    assert recus[0] == (1, "deux")
    (objet,) = recus[1]
    assert isinstance(objet, c.Markdown)
    assert objet.data == "**gras**"


def test_afficher_refuse_les_arguments_nommes_inconnus():
    """afficher n'accepte que des objets positionnels : un argument nommé est refusé."""
    with pytest.raises(TypeError):
        c.afficher(1, inclure=2)


def test_afficher_markdown_refuse_un_texte_manquant():
    """Sans texte, rien à afficher : TypeError, pas un affichage vide."""
    with pytest.raises(TypeError):
        c.afficher_markdown()
