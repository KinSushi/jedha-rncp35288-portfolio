# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de `att_spam.evaluation` : seuil, intervalles, McNemar, référence, ablation, erreurs."""

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import f1_score

from att_spam import evaluation
from att_spam.evaluation import (
    ablation,
    analyseur_identite,
    entrainer_reference,
    erreurs_du_modele,
    evaluer,
    f1,
    f1_reference_par_pli,
    intervalle_bootstrap,
    mcnemar,
    meilleur_seuil,
    meilleure_epoque,
    performance,
    probabilites_reference,
    retirer_signal,
    tirages_bootstrap,
    wilson,
)


def f32(valeurs):
    return np.array(valeurs, dtype=np.float32)


# --------------------------------------------------------------------------- F1


def test_f1_est_la_moyenne_harmonique_de_la_precision_et_du_rappel():
    y = f32([1, 1, 1, 0, 0])
    predictions = np.array([1, 1, 0, 1, 0], dtype=bool)  # 2 vrais positifs, 1 faux, 1 manqué
    assert f1(y, predictions) == pytest.approx(2 / 3)
    assert type(f1(y, predictions)) is float
    assert f1(y, y == 1) == 1.0


def test_f1_refuse_des_longueurs_differentes():
    with pytest.raises(ValueError):
        f1([1, 0, 1], [1, 0])


# --------------------------------------------------------------------------- seuil


def test_meilleur_seuil_grille_de_0_05_a_0_95_par_pas_de_0_01():
    """Premier seuil au F1 maximal ; grille de 0,05 à 0,95 par pas de 0,01 ; flottant Python."""
    y = f32([0, 0, 1, 1])
    proba = f32([0.1, 0.4, 0.6, 0.9])
    seuil = meilleur_seuil(y, proba)
    assert 0.41 - 1e-9 <= seuil <= 0.6 + 1e-9
    assert f1_score(y, proba >= seuil) == 1.0
    assert f1_score(y, proba >= seuil - 0.01) < 1.0  # un cran plus bas, un faux positif apparaît

    seuil = meilleur_seuil(f32([0, 1]), f32([0.0, 1.0]))
    assert seuil == pytest.approx(0.05)

    seuil = meilleur_seuil(f32([0, 0, 1]), f32([0.949, 0.94, 0.96]))
    assert seuil == pytest.approx(0.95)

    seuil = meilleur_seuil(f32([0, 1]), f32([0.415, 0.9]))
    assert seuil == pytest.approx(0.42)

    assert type(meilleur_seuil(f32([0, 1]), f32([0.2, 0.8]))) is float


def test_meilleur_seuil_ne_propose_jamais_un_seuil_au_dessus_de_0_95():
    """Quand seul un seuil au-delà de la grille séparerait les classes, le premier cran reste."""
    # Les deux probabilités dépassent 0,95 : tous les seuils de la grille déclarent les deux
    # messages spams (F1 de 2/3 partout, donc le premier cran) ; un seuil de 0,98, hors grille,
    # donnerait un F1 de 1 en écartant le légitime.
    seuil = meilleur_seuil(f32([0, 1]), f32([0.97, 0.99]))
    assert seuil == pytest.approx(0.05)


def test_meilleur_seuil_un_seuil_egal_a_la_probabilite_d_un_legitime_le_declare_spam():
    """Une probabilité égale au seuil est déclarée spam : la grille passe au cran suivant."""
    # En double précision, 0,2 est une valeur EXACTE de la grille (np.arange) : le seuil 0,20
    # déclare le légitime spam (F1 de 2/3), le seuil 0,21 l'écarte (F1 de 1).
    seuil = meilleur_seuil(np.array([0.0, 1.0]), np.array([0.2, 0.9]))
    assert seuil == pytest.approx(0.21)


# --------------------------------------------------------------------------- Wilson


def test_wilson_valeurs_de_reference_bornes_et_symetrie():
    """Valeurs de référence, bornes 0 et 1, z nul, effectif croissant, symétrie."""
    bas, haut = wilson(50, 100)
    assert (bas, haut) == pytest.approx((0.4038, 0.5962), abs=5e-5)
    bas, haut = wilson(92, 93)  # rappel de DistilBERT dans le notebook
    assert (bas, haut) == pytest.approx((0.942, 0.998), abs=5e-4)

    assert wilson(0, 20)[0] == pytest.approx(0.0, abs=1e-12)
    assert wilson(1, 1) == pytest.approx((0.2065, 1.0), abs=5e-5)  # un seul message : permis
    assert wilson(20, 20)[1] == pytest.approx(1.0, abs=1e-12)
    bas, haut = wilson(0, 20)
    assert 0.0 < haut < 0.2
    bas, haut = wilson(20, 20)
    assert 0.8 < bas < 1.0

    bas, haut = wilson(3, 10, z=0.0)
    assert (bas, haut) == pytest.approx((0.3, 0.3))

    petit, grand = wilson(5, 10), wilson(500, 1000)
    assert (grand[1] - grand[0]) < (petit[1] - petit[0])

    bas, haut = wilson(30, 100)
    bas2, haut2 = wilson(70, 100)
    assert (bas, haut) == pytest.approx((1 - haut2, 1 - bas2))


@pytest.mark.parametrize("effectif", [0, -1])
def test_wilson_refuse_un_effectif_nul_ou_negatif(effectif):
    with pytest.raises(ValueError) as erreur:
        wilson(0, effectif)
    assert str(erreur.value) == "l'effectif d'une proportion doit être positif"


# --------------------------------------------------------------------------- bootstrap


def test_tirages_bootstrap_forme_bornes_et_graine():
    """Forme, bornes, reproductible, dépend de la graine."""
    tirages = tirages_bootstrap(graine=1, taille=5)
    assert tirages.shape == (2000, 5)
    assert tirages.min() == 0
    assert tirages.max() == 4  # indices de 0 à taille - 1, tous atteints sur 2000 tirages
    assert tirages_bootstrap(1, 7, nombre=30).shape == (30, 7)

    assert np.array_equal(tirages_bootstrap(3, 10, 50), tirages_bootstrap(3, 10, 50))
    assert not np.array_equal(tirages_bootstrap(3, 10, 50), tirages_bootstrap(4, 10, 50))


def test_intervalle_bootstrap_percentiles():
    """Prédictions parfaites : un point ; sinon percentiles 2,5 et 97,5 des F1 des tirages."""
    y = f32([0, 1, 0, 1, 1, 0])
    bas, haut = intervalle_bootstrap(y, y == 1, tirages_bootstrap(0, len(y), 50))
    assert (bas, haut) == (1.0, 1.0)

    y = f32([1, 1, 1, 0, 0, 0, 1, 0])
    predictions = np.array([1, 1, 0, 0, 1, 0, 1, 0], dtype=bool)
    tirages = tirages_bootstrap(2, len(y), 60)
    f1_tirages = [f1_score(y[t], predictions[t]) for t in tirages]
    bas, haut = intervalle_bootstrap(y, predictions, tirages)
    assert bas == pytest.approx(np.percentile(f1_tirages, 2.5))
    assert haut == pytest.approx(np.percentile(f1_tirages, 97.5))
    assert bas < haut

    # Sur 40 messages, presque chaque tirage a son propre F1 : la borne haute est bien le
    # percentile 97,5, et pas un voisin comme 98,5, qui donnerait ici une autre valeur.
    hasard = np.random.default_rng(1)
    y = f32(hasard.integers(0, 2, 40))
    predictions = np.where(hasard.random(40) < 0.25, y == 0, y == 1)  # un quart d'erreurs environ
    tirages = tirages_bootstrap(3, len(y), 400)
    f1_tirages = [f1_score(y[t], predictions[t]) for t in tirages]
    assert np.percentile(f1_tirages, 97.5) < np.percentile(f1_tirages, 98.5)
    _, haut = intervalle_bootstrap(y, predictions, tirages)
    assert haut == pytest.approx(np.percentile(f1_tirages, 97.5))


# --------------------------------------------------------------------------- évaluation d'un modèle

Y_VAL = f32([0, 1])
P_VAL = f32([0.2, 0.8])  # le premier seuil de la grille au-dessus de 0,2 est 0,21
Y_TEST = f32([0, 0, 1, 1, 1, 0])
P_TEST = f32([0.1, 0.3, 0.2, 0.9, 0.95, 0.05])  # prédit [non, oui, non, oui, oui, non] à 0,21


def test_evaluer_mesures_seuil_de_validation_et_erreurs():
    """Mesures arrondies comme annoncé, seuil choisi sur la validation, erreurs comptées."""
    tirages = tirages_bootstrap(0, len(Y_TEST), 40)
    resultat = evaluer("Jouet", Y_VAL, P_VAL, Y_TEST, P_TEST, tirages)
    assert resultat.seuil == pytest.approx(0.21)
    assert resultat.predictions.tolist() == [False, True, False, True, True, False]
    assert resultat.matrice.tolist() == [[2, 1], [1, 2]]  # [[vrais négatifs, faux positifs], ...]
    ligne = resultat.ligne
    assert list(ligne) == [
        "modèle", "seuil (validation)", "F1 validation", "précision test", "rappel test",
        "F1 test", "F1 test IC 95 %", "erreurs test", "ROC-AUC test", "PR-AUC test",
    ]  # fmt: skip
    assert ligne["modèle"] == "Jouet"
    assert ligne["seuil (validation)"] == 0.21
    assert ligne["F1 validation"] == 1.0
    assert ligne["précision test"] == 0.667  # 2 / 3, arrondi à trois décimales
    assert ligne["rappel test"] == 0.667
    assert ligne["F1 test"] == 0.667
    assert ligne["erreurs test"] == 2
    assert ligne["ROC-AUC test"] == 0.8889  # 8 / 9, arrondi à quatre décimales
    assert ligne["PR-AUC test"] == 0.9167  # (1 + 1 + 3/4) / 3
    bas, haut = intervalle_bootstrap(Y_TEST, resultat.predictions, tirages)
    assert ligne["F1 test IC 95 %"] == f"[{bas:.3f} ; {haut:.3f}]"

    tirages = tirages_bootstrap(0, len(Y_TEST), 20)
    resultat = evaluer("Jouet", Y_VAL, P_VAL, Y_TEST, P_TEST, tirages)
    assert resultat.seuil == meilleur_seuil(Y_VAL, P_VAL)
    assert resultat.seuil != meilleur_seuil(Y_TEST, P_TEST)

    tirages = tirages_bootstrap(0, len(Y_TEST), 20)
    parfait = evaluer("Parfait", Y_TEST, f32([0.1, 0.1, 0.9, 0.9, 0.9, 0.1]), Y_TEST,
                      f32([0.1, 0.1, 0.9, 0.9, 0.9, 0.1]), tirages)  # fmt: skip
    assert parfait.ligne["erreurs test"] == 0
    assert parfait.ligne["F1 test"] == 1.0
    assert parfait.ligne["F1 test IC 95 %"] == "[1.000 ; 1.000]"


def test_evaluer_refuse_un_test_vide():
    vide = np.array([], dtype=np.float32)
    with pytest.raises(ValueError) as erreur:
        evaluer("Jouet", Y_VAL, P_VAL, vide, vide, tirages_bootstrap(0, 1, 3))
    assert str(erreur.value) == "le jeu de test est vide : rien à évaluer"


# --------------------------------------------------------------------------- McNemar


def test_mcnemar_comptes_et_valeur_p():
    """Messages discordants, valeurs du notebook, échange des modèles, absence de discordance,
    différence nette."""
    a = np.array([1, 1, 1, 1, 1, 1, 1, 0, 0, 1], dtype=bool)
    b = np.array([0, 0, 0, 0, 0, 0, 1, 1, 0, 1], dtype=bool)
    seul_a, seul_b, p = mcnemar(a, b)
    assert (seul_a, seul_b) == (6, 1)
    assert p == pytest.approx(0.125)  # 2 x (7 + 1) / 128, comme dans le notebook

    a = np.array([1] * 12 + [0] + [1] * 3, dtype=bool)
    b = np.array([0] * 12 + [1] + [1] * 3, dtype=bool)
    seul_a, seul_b, p = mcnemar(a, b)
    assert (seul_a, seul_b) == (12, 1)
    assert p == pytest.approx(2 * 14 / 8192)

    a = np.array([1, 1, 1, 1, 0], dtype=bool)
    b = np.zeros(5, dtype=bool)
    assert mcnemar(a, b)[:2] == (4, 0)
    assert mcnemar(b, a)[:2] == (0, 4)
    assert mcnemar(a, b)[2] == pytest.approx(0.125)  # 2 / 16
    assert mcnemar(b, a)[2] == pytest.approx(0.125)

    a = np.array([1, 0, 1], dtype=bool)
    assert mcnemar(a, a.copy()) == (0, 0, 1.0)

    a = np.ones(30, dtype=bool)
    b = np.zeros(30, dtype=bool)
    seul_a, seul_b, p = mcnemar(a, b)
    assert (seul_a, seul_b) == (30, 0)
    assert p < 1e-6


def test_mcnemar_laisse_les_entrees_inchangees():
    a = np.array([1, 0, 1, 1], dtype=bool)
    b = np.array([0, 0, 1, 0], dtype=bool)
    a_avant, b_avant = a.copy(), b.copy()
    mcnemar(a, b)
    assert np.array_equal(a, a_avant) and np.array_equal(b, b_avant)


# --------------------------------------------------------------------------- performance


def test_performance_comptes_et_intervalles():
    """Comptes du modèle retenu et intervalles de Wilson de son rappel et de sa précision."""
    y = f32([1, 1, 1, 0, 0, 0, 0])
    predictions = np.array([1, 1, 0, 1, 0, 0, 0], dtype=bool)
    resultat = performance(y, predictions)
    assert resultat.vrais_positifs == 2
    assert resultat.nb_spams == 3
    assert resultat.nb_alertes == 3
    assert resultat.faux_positifs == 1
    assert resultat.nb_legitimes == 4
    assert resultat.ic_rappel == pytest.approx(wilson(2, 3))
    assert resultat.ic_precision == pytest.approx(wilson(2, 3))

    y = f32([1, 1, 1, 1, 0, 0])
    predictions = np.array([1, 1, 1, 0, 0, 0], dtype=bool)  # 3 trouvés sur 4 spams, 3 alertes
    resultat = performance(y, predictions)
    assert resultat.ic_rappel == pytest.approx(wilson(3, 4))
    assert resultat.ic_precision == pytest.approx(wilson(3, 3))
    assert resultat.faux_positifs == 0


def test_performance_refuse_un_modele_qui_ne_donne_aucune_alerte():
    with pytest.raises(ValueError):
        performance(f32([1, 0]), np.array([False, False]))


def test_performance_refuse_un_jeu_sans_spam():
    with pytest.raises(ValueError):
        performance(f32([0, 0]), np.array([True, False]))


# --------------------------------------------------------------------------- historique


def test_meilleure_epoque_est_la_premiere_au_meilleur_f1_de_validation():
    assert meilleure_epoque({"f1_val": [0.5, 0.9, 0.7, 0.9]}) == 1
    assert meilleure_epoque({"f1_val": [0.9]}) == 0
    assert isinstance(meilleure_epoque({"f1_val": [0.1, 0.2]}), int)


# --------------------------------------------------------------------------- référence TF-IDF

SMS_SPAM = ["free prize call now", "win free prize now", "call now free win", "free call prize"]
SMS_HAM = ["see you at home", "call me at home later", "home later see you", "ok see you later"]


def corpus():
    mots = [s.split() for s in SMS_SPAM + SMS_HAM]
    cible = [1] * len(SMS_SPAM) + [0] * len(SMS_HAM)
    return mots, cible


def test_reference_separe_les_classes():
    """Analyseur identité ; la référence sépare spams et légitimes ; probabilité du spam."""
    mots = ["a", "b"]
    assert analyseur_identite(mots) is mots

    mots, cible = corpus()
    vectoriseur, modele = entrainer_reference(mots, cible, graine=0)
    probas = probabilites_reference(vectoriseur, modele, mots)
    assert probas.shape == (len(mots),)
    assert probas[:4].min() > 0.5 > probas[4:].max()

    mots, cible = corpus()
    vectoriseur, modele = entrainer_reference(mots, cible, graine=0)
    attendu = modele.predict_proba(vectoriseur.transform(mots))
    assert probabilites_reference(vectoriseur, modele, mots).tolist() == attendu[:, 1].tolist()


def test_reference_reglages_et_classe_rare():
    """Mots vus une fois ignorés, réglages du modèle, classe rare reconnue."""
    mots, cible = corpus()
    mots[0] = [*mots[0], "unique"]
    vectoriseur, _ = entrainer_reference(mots, cible, graine=0)
    assert "unique" not in vectoriseur.vocabulary_
    assert "free" in vectoriseur.vocabulary_
    assert "win" in vectoriseur.vocabulary_  # vu exactement deux fois : retenu

    mots, cible = corpus()
    vectoriseur, modele = entrainer_reference(mots, cible, graine=7)
    assert modele.class_weight == "balanced"
    assert modele.max_iter == 2000
    assert modele.random_state == 7
    assert vectoriseur.min_df == 2
    assert vectoriseur.analyzer is analyseur_identite

    mots = [["spam", "x"]] * 2 + [["ham", "y"]] * 18
    cible = [1] * 2 + [0] * 18
    vectoriseur, modele = entrainer_reference(mots, cible, graine=0)
    p = probabilites_reference(vectoriseur, modele, [["spam", "x"], ["ham", "y"]])
    assert p[0] > 0.5 > p[1]


def test_f1_reference_par_pli(monkeypatch):
    """Chaque pli sert une fois de test, le reste entraîne ; le seuil demandé s'applique."""
    mots, cible = corpus()
    donnees = pd.DataFrame({"mots": mots, "cible": cible})
    plis = [np.array([0, 4]), np.array([1, 5]), np.array([2, 6]), np.array([3, 7])]
    entraines = []
    reelle = evaluation.entrainer_reference

    def espion(mots_app, cible_app, graine):
        entraines.append((list(mots_app.index), graine))
        return reelle(mots_app, cible_app, graine)

    monkeypatch.setattr(evaluation, "entrainer_reference", espion)
    f1 = f1_reference_par_pli(donnees, plis, graine=3)
    assert f1 == [1.0, 1.0, 1.0, 1.0]
    assert entraines == [
        ([1, 2, 3, 5, 6, 7], 3), ([0, 2, 3, 4, 6, 7], 3),
        ([0, 1, 3, 4, 5, 7], 3), ([0, 1, 2, 4, 5, 6], 3),
    ]  # fmt: skip

    mots, cible = corpus()
    donnees = pd.DataFrame({"mots": mots, "cible": cible})
    plis = [np.array([0, 4])]
    assert f1_reference_par_pli(donnees, plis, graine=0, seuil=0.5) == [1.0]
    zero = f1_reference_par_pli(donnees, plis, graine=0, seuil=0.999999)
    assert zero[0] < 1.0


@pytest.mark.filterwarnings("ignore::sklearn.exceptions.UndefinedMetricWarning")
def test_f1_reference_par_pli_une_probabilite_egale_au_seuil_est_un_spam(monkeypatch):
    mots, cible = corpus()
    donnees = pd.DataFrame({"mots": mots, "cible": cible})
    monkeypatch.setattr(
        evaluation, "probabilites_reference", lambda *_: np.full(2, 0.5)
    )  # exactement le seuil par défaut : « >= » déclare spam, « > » ne déclarerait rien
    pli_une_classe = [np.array([0, 1])]  # deux spams : tout déclaré spam donne un F1 de 1
    assert f1_reference_par_pli(donnees, pli_une_classe, graine=0) == [1.0]
    assert f1_reference_par_pli(donnees, pli_une_classe, graine=0, seuil=0.5000001) == [0.0]


def test_mcnemar_la_loi_binomiale_est_celle_d_une_piece_equilibree(monkeypatch):
    appels = []
    reel = evaluation.binomtest

    def espion(*args, **options):
        appels.append((args, options))
        return reel(*args, **options)

    monkeypatch.setattr(evaluation, "binomtest", espion)
    mcnemar(np.array([True, True, False]), np.array([False, False, True]))
    assert appels == [((2, 3, 0.5), {})]


def test_f1_reference_par_pli_laisse_le_jeu_inchange():
    mots, cible = corpus()
    donnees = pd.DataFrame({"mots": mots, "cible": cible})
    avant = donnees.copy(deep=True)
    f1_reference_par_pli(donnees, [np.array([0, 4])], graine=0)
    pd.testing.assert_frame_equal(donnees, avant)


# --------------------------------------------------------------------------- ablation


def test_retirer_signal_mot_entier_jeton_entier_et_metacaracteres():
    """Retire un mot ou un jeton entier, jamais une partie ; les métacaractères sont échappés."""
    textes = ["call me now", "recall me", "calls me", "call", "<call> me", "a call-b"]
    concernes, sans = retirer_signal(textes, "call")
    assert concernes == [0, 3, 5]
    assert sans == ["  me now", " ", "a  -b"]

    textes = ["call <tel> now", "call <telephone> now", "x<tel>y", "<tel><tel>"]
    concernes, sans = retirer_signal(textes, "<tel>")
    assert concernes == [0, 3]  # deux jetons collés sont deux jetons entiers
    assert sans == ["call   now", "  "]

    concernes, sans = retirer_signal(["abc a.c a.c"], "a.c")
    assert concernes == [0]
    assert sans == ["abc    "]  # « abc » n'est pas touché : le point ne vaut pas « n'importe quoi »

    assert retirer_signal(["hello"], "call") == ([], [])


def test_retirer_signal_laisse_la_liste_d_entree_inchangee():
    textes = ["call me now"]
    retirer_signal(textes, "call")
    assert textes == ["call me now"]


def noter_free(textes):
    """Faux modèle : 0,9 si « free » figure dans le texte, sinon 0,2."""
    return np.array([0.9 if "free" in t.split() else 0.2 for t in textes], dtype=np.float32)


def test_ablation_baisse_seuil_strict_et_signaux_absents():
    """Baisse de probabilité, passages sous le seuil (strict), signaux absents sautés."""
    textes = ["free call", "free prize", "call now", "free"]
    lignes = ablation(textes, noter_free, seuil=0.5, signaux=["free"])
    assert len(lignes) == 1
    ligne = lignes[0]
    assert list(ligne) == [
        "signal retiré", "spams détectés qui le contiennent", "baisse moyenne de probabilité",
        "repassent sous le seuil",
    ]  # fmt: skip
    assert ligne["signal retiré"] == "free"
    assert ligne["spams détectés qui le contiennent"] == 3
    assert ligne["baisse moyenne de probabilité"] == pytest.approx(0.7)
    assert ligne["repassent sous le seuil"] == 3

    appels = []

    def noter(textes):
        appels.append(list(textes))
        return noter_free(textes)

    lignes = ablation(["free call", "call now"], noter, seuil=0.5, signaux=["absent", "call"])
    assert [ligne["signal retiré"] for ligne in lignes] == ["call"]
    assert appels == [["free call", "call now"], ["free  ", "  now"]]

    def noter(textes):
        return np.array([0.9 if "x" in t.split() else 0.5 for t in textes], dtype=np.float32)

    lignes = ablation(["x y"], noter, seuil=0.5, signaux=["x"])
    assert lignes[0]["repassent sous le seuil"] == 0  # 0,5 n'est pas strictement sous 0,5
    lignes = ablation(["x y"], noter, seuil=0.5001, signaux=["x"])
    assert lignes[0]["repassent sous le seuil"] == 1

    def noter(textes):
        return np.array([0.9 if "x" in t.split() else 0.1 for t in textes], dtype=np.float32)

    lignes = ablation(["x a", "b", "x c"], noter, seuil=0.5, signaux=["x"])
    assert lignes[0]["spams détectés qui le contiennent"] == 2
    assert lignes[0]["baisse moyenne de probabilité"] == pytest.approx(0.8)


# --------------------------------------------------------------------------- erreurs


def test_erreurs_du_modele_faux_positifs_et_faux_negatifs():
    """Faux positifs et faux négatifs, bornes comprises ; deux tables vides sans erreur."""
    test = pd.DataFrame({"cible": [0, 0, 1, 1, 0, 1], "texte": list("abcdef")})
    proba = f32([0.9, 0.5, 0.5, 0.1, 0.2, 0.8])
    faux_positifs, faux_negatifs = erreurs_du_modele(test, proba, seuil=0.5)
    assert faux_positifs["texte"].tolist() == ["a", "b"]  # à 0,5 pile : déclaré spam
    assert faux_negatifs["texte"].tolist() == ["d"]  # 0,5 pile n'est pas « sous le seuil »
    assert faux_positifs["proba"].tolist() == pytest.approx([0.9, 0.5])
    assert faux_negatifs["proba"].tolist() == pytest.approx([0.1])

    test = pd.DataFrame({"cible": [0, 1], "texte": ["a", "b"]})
    faux_positifs, faux_negatifs = erreurs_du_modele(test, f32([0.1, 0.9]), seuil=0.5)
    assert faux_positifs.empty and faux_negatifs.empty


def test_erreurs_du_modele_laisse_le_jeu_de_test_inchange():
    test = pd.DataFrame({"cible": [0, 1], "texte": ["a", "b"]})
    avant = test.copy(deep=True)
    erreurs_du_modele(test, f32([0.9, 0.1]), seuil=0.5)
    pd.testing.assert_frame_equal(test, avant)
    assert "proba" not in test.columns




# --------------------------------------------------------------------------- entrées refusées
# Chaque test ci-dessous porte sur une entrée interdite ou absurde : elle doit être refusée par
# une exception, ou laisser les données de l'appelant exactement comme elles étaient.


def test_meilleur_seuil_refuse_des_cibles_et_des_probabilites_de_longueurs_differentes():
    with pytest.raises(ValueError):
        meilleur_seuil(f32([0, 1, 1]), f32([0.2, 0.8]))


def test_meilleur_seuil_laisse_les_probabilites_inchangees():
    y, proba = f32([0, 1]), f32([0.2, 0.8])
    y_avant, proba_avant = y.copy(), proba.copy()
    meilleur_seuil(y, proba)
    assert np.array_equal(y, y_avant) and np.array_equal(proba, proba_avant)


def test_tirages_bootstrap_refuse_un_nombre_de_tirages_negatif():
    with pytest.raises(ValueError):
        tirages_bootstrap(graine=0, taille=5, nombre=-1)


def test_intervalle_bootstrap_refuse_un_tirage_hors_du_test():
    y = f32([0, 1])
    with pytest.raises(IndexError):
        intervalle_bootstrap(y, y == 1, np.array([[0, 5]]))


def test_evaluer_refuse_des_probabilites_de_test_de_longueur_differente():
    with pytest.raises((ValueError, IndexError)):
        evaluer("Jouet", Y_VAL, P_VAL, Y_TEST, P_TEST[:4], tirages_bootstrap(0, 6, 5))


def test_evaluer_laisse_les_vecteurs_d_entree_inchanges():
    entrees = [Y_VAL.copy(), P_VAL.copy(), Y_TEST.copy(), P_TEST.copy()]
    evaluer("Jouet", *entrees, tirages_bootstrap(0, len(Y_TEST), 5))
    for obtenu, attendu in zip(entrees, (Y_VAL, P_VAL, Y_TEST, P_TEST), strict=True):
        assert np.array_equal(obtenu, attendu)


def test_mcnemar_refuse_des_reponses_de_longueurs_differentes():
    with pytest.raises(ValueError):
        mcnemar(np.array([True, False, True]), np.array([True, False]))


def test_performance_laisse_les_vecteurs_d_entree_inchanges():
    y = f32([1, 1, 0, 0])
    predictions = np.array([True, False, True, False])
    y_avant, predictions_avant = y.copy(), predictions.copy()
    performance(y, predictions)
    assert np.array_equal(y, y_avant) and np.array_equal(predictions, predictions_avant)


def test_meilleure_epoque_refuse_un_historique_sans_epoque():
    with pytest.raises(ValueError):
        meilleure_epoque({"f1_val": []})


def test_entrainer_reference_refuse_une_cible_de_longueur_differente_des_messages():
    mots, cible = corpus()
    with pytest.raises(ValueError):
        entrainer_reference(mots, cible[:-1], graine=0)


def test_entrainer_reference_refuse_un_corpus_ou_aucun_mot_n_est_assez_frequent():
    with pytest.raises(ValueError):
        entrainer_reference([["a"], ["b"], ["c"], ["d"]], [0, 1, 0, 1], graine=0)


def test_probabilites_reference_laisse_les_messages_inchanges():
    mots, cible = corpus()
    vectoriseur, modele = entrainer_reference(mots, cible, graine=0)
    avant = [list(sms) for sms in mots]
    probabilites_reference(vectoriseur, modele, mots)
    assert mots == avant


def test_f1_reference_par_pli_refuse_un_pli_hors_du_jeu():
    mots, cible = corpus()
    donnees = pd.DataFrame({"mots": mots, "cible": cible})
    with pytest.raises(IndexError):
        f1_reference_par_pli(donnees, [np.array([0, 99])], graine=0)


def test_ablation_refuse_un_modele_qui_ne_note_pas_tous_les_textes():
    def noter_un_seul(textes):
        return np.array([0.9], dtype=np.float32)

    with pytest.raises(IndexError):
        ablation(["free a", "free b"], noter_un_seul, seuil=0.5, signaux=["free"])


def test_ablation_laisse_les_textes_inchanges():
    textes = ["free call", "call now"]
    avant = list(textes)
    ablation(textes, noter_free, seuil=0.5, signaux=["free", "call"])
    assert textes == avant


def test_erreurs_du_modele_refuse_des_probabilites_de_longueur_differente():
    test = pd.DataFrame({"cible": [0, 1, 1], "texte": ["a", "b", "c"]})
    with pytest.raises(ValueError):
        erreurs_du_modele(test, f32([0.9, 0.1]), seuil=0.5)


def test_evaluer_arrondit_le_f1_de_validation_a_trois_decimales():
    # validation : le meilleur F1 (6/7) s'obtient en déclarant tout spam, donc au plus bas seuil
    y_val = f32([1, 1, 1, 0])
    p_val = f32([0.9, 0.8, 0.3, 0.6])
    resultat = evaluer("Jouet", y_val, p_val, Y_TEST, P_TEST, tirages_bootstrap(0, 6, 5))
    assert resultat.seuil == pytest.approx(0.05)
    assert resultat.ligne["seuil (validation)"] == 0.05
    # le test contient une probabilité de 0,05 pile : égale au seuil, elle est déclarée spam
    assert resultat.predictions.tolist() == [True] * 6
    assert resultat.ligne["F1 validation"] == 0.857  # 6/7 = 0.857142... à trois décimales


def test_intervalle_bootstrap_laisse_les_vecteurs_inchanges():
    y = f32([1, 0, 1, 1])
    predictions = np.array([True, False, False, True])
    tirages = tirages_bootstrap(0, 4, 20)
    copies = (y.copy(), predictions.copy(), tirages.copy())
    intervalle_bootstrap(y, predictions, tirages)
    for obtenu, attendu in zip((y, predictions, tirages), copies, strict=True):
        assert np.array_equal(obtenu, attendu)


def test_erreurs_du_modele_refuse_un_jeu_de_test_sans_colonne_cible():
    with pytest.raises(KeyError):
        erreurs_du_modele(pd.DataFrame({"texte": ["a", "b"]}), f32([0.9, 0.1]), seuil=0.5)


def test_evaluer_affiche_le_seuil_arrondi_a_deux_decimales(monkeypatch):
    monkeypatch.setattr(evaluation, "meilleur_seuil", lambda *_: 0.2149)
    resultat = evaluer("Jouet", Y_VAL, P_VAL, Y_TEST, P_TEST, tirages_bootstrap(0, 6, 5))
    assert resultat.seuil == 0.2149  # le seuil utilisé reste exact
    assert resultat.ligne["seuil (validation)"] == 0.21  # le tableau l'arrondit à 2 décimales


def test_evaluer_une_probabilite_de_validation_egale_au_seuil_est_un_spam(monkeypatch):
    # Seuil imposé à 0,5 : le légitime vaut 0,1 et le spam exactement 0,5. Avec « >= » le spam est
    # trouvé (F1 de 1) ; avec « > » il ne le serait pas (F1 de 0).
    monkeypatch.setattr(evaluation, "meilleur_seuil", lambda *_: 0.5)
    resultat = evaluer(
        "Égalité", f32([0, 1]), f32([0.1, 0.5]), Y_TEST, P_TEST, tirages_bootstrap(0, 6, 5)
    )
    assert resultat.ligne["F1 validation"] == 1.0
