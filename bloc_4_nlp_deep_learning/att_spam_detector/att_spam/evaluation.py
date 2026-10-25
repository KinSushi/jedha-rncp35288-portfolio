# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Évaluation : référence TF-IDF, seuil, bootstrap, Wilson, McNemar, ablation, erreurs.

CHOIX
-----
- Retenu : choisir le seuil de décision sur la VALIDATION (grille de 0,05 à 0,95 par pas de 0,01,
  maximum du F1) et n'évaluer chaque modèle qu'UNE fois sur le test. Le test mesure, il ne règle
  rien.
- Retenu : des indicateurs adaptés aux classes déséquilibrées (précision, rappel, F1, ROC-AUC,
  PR-AUC). L'exactitude seule serait trompeuse : un modèle qui ne détecte aucun spam a déjà 87 %.
- Retenu : un intervalle de confiance du F1 par bootstrap (2 000 tirages avec remise des messages
  de test, les mêmes tirages pour tous les modèles) et un intervalle de Wilson pour une proportion
  proche de 100 %, où l'approximation normale échoue.
- Retenu : le test exact de McNemar (loi binomiale de paramètre 0,5 sur les messages où un seul des
  deux modèles a raison) pour comparer deux modèles sur les mêmes messages.
- Retenu : une référence non profonde, TF-IDF et régression logistique à classes équilibrées, pour
  juger si un réseau apporte quelque chose. Son analyseur est une fonction identité : les mots
  sont déjà découpés par `att_spam/donnees.py`.
- Écarté : un test de comparaison non apparié (deux proportions indépendantes). Les deux modèles
  sont évalués sur les MÊMES messages ; McNemar exploite cet appariement.
- Écarté : relancer DistilBERT sur les sept plis (9 minutes par ajustement). Seule la référence
  TF-IDF est réentraînée sur chaque pli, pour mesurer la variabilité du chiffre selon le pli.

OÙ
---
- Place : sections 3, 7 et 8 du notebook `ATT_Spam_Bloc4_CDSD.ipynb` (référence, évaluation
  finale, signaux). Appelants : le notebook. Dépendances : numpy, pandas, scipy
  (`binomtest`), scikit-learn, et `att_spam/reseaux.py` pour le type `Historique`.

COMMENT
-------
1. `entrainer_reference` ajuste le vectoriseur TF-IDF (mots vus au moins deux fois) et la
   régression logistique ; `probabilites_reference` donne les probabilités de spam.
2. `evaluer` choisit le seuil (`meilleur_seuil`), prédit sur le test, calcule la matrice de
   confusion, l'intervalle bootstrap (`intervalle_bootstrap`), précision, rappel, F1, erreurs,
   ROC-AUC, PR-AUC, et rend ces valeurs dans une ligne du tableau final.
3. `mcnemar` compte les messages où un seul modèle a raison puis applique `binomtest`.
4. `performance` rend rappel, précision, faux positifs et leurs intervalles de Wilson.
5. `f1_reference_par_pli` réentraîne la référence en prenant tour à tour chaque pli pour test.
6. `retirer_signal` supprime un jeton ou un mot des textes qui le contiennent (délimiteurs sûrs :
   `<tel>` n'est pas confondu avec `<telephone>`), `ablation` mesure la baisse de probabilité et le
   nombre de spams qui repassent sous le seuil.
7. `erreurs_du_modele` isole faux positifs et faux négatifs ; `meilleure_epoque` lit les courbes.
- Tests qui le prouvent : `tests/test_evaluation.py` (valeurs exactes du seuil, de Wilson et de
  McNemar sur des cas calculés à la main, bornes de l'intervalle bootstrap ; entrées refusées :
  test vide, effectif nul, longueurs incohérentes, pli hors du jeu ; entrées laissées
  inchangées) ; `tests/test_notebook.py` (tableau final du notebook).

POURQUOI
--------
- Les critères d'évaluation (indicateurs adaptés, intervalle de confiance, généralisation sur le
  test) viennent de la compétence C4.5 du bloc 4. Source : référentiel de compétences
  RNCP35288, bloc 4.
- La formule de Wilson et le test exact de McNemar sont des méthodes classiques de statistique ;
  elles sont vérifiées ici par des valeurs calculées à la main dans `tests/test_evaluation.py`.
- Le niveau de confiance (95 %), la grille de seuils et les 2 000 tirages du bootstrap sont des
  conventions usuelles, non établies par une étude de ce projet : source : inconnue.

JOURNALISATION
--------------
- Aucune écriture dans `logging` et aucun `print` : ce module calcule et rend des valeurs ; le
  notebook les affiche. Aucune exception n'est interceptée : un test vide, un effectif nul ou une
  absence de spam lève `ValueError`, jamais un résultat silencieux.
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import binomtest
from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

from att_spam.reseaux import Historique

#: Vecteur de réels (probabilités ou cibles 0/1), en simple ou en double précision.
type Reels = npt.NDArray[np.floating[Any]]


def analyseur_identite(mots: list[str]) -> list[str]:
    """Analyseur de TF-IDF : les mots sont déjà découpés, on les rend tels quels."""
    return mots


def entrainer_reference(
    mots_par_sms: Iterable[list[str]], cible: Iterable[int], graine: int
) -> tuple[TfidfVectorizer, LogisticRegression]:
    """TF-IDF (mots vus au moins deux fois) puis régression logistique à classes équilibrées."""
    vectoriseur = TfidfVectorizer(analyzer=analyseur_identite, min_df=2)
    matrice = vectoriseur.fit_transform(mots_par_sms)
    modele = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=graine)
    modele.fit(matrice, cible)
    return vectoriseur, modele


def probabilites_reference(
    vectoriseur: TfidfVectorizer, modele: LogisticRegression, mots_par_sms: Iterable[list[str]]
) -> npt.NDArray[np.float64]:
    """Probabilités de spam de la référence."""
    return np.asarray(modele.predict_proba(vectoriseur.transform(mots_par_sms))[:, 1])


def f1_reference_par_pli(
    donnees: pd.DataFrame, plis: Sequence[npt.NDArray[np.int64]], graine: int, seuil: float = 0.5
) -> list[float]:
    """F1 de la référence en prenant tour à tour chaque pli pour test, les autres pour apprendre."""
    resultats: list[float] = []
    for indices_pli in plis:
        test_pli = donnees.iloc[indices_pli]
        app_pli = donnees.drop(index=test_pli.index)
        vectoriseur, modele = entrainer_reference(app_pli["mots"], app_pli["cible"], graine)
        proba = probabilites_reference(vectoriseur, modele, test_pli["mots"])
        resultats.append(float(f1_score(test_pli["cible"], proba >= seuil)))
    return resultats


def f1(y_vrai: npt.ArrayLike, predictions: npt.ArrayLike) -> float:
    """F1 (moyenne harmonique de la précision et du rappel) de la classe spam."""
    return float(f1_score(y_vrai, predictions))


def meilleur_seuil(y_vrai: Reels, proba: Reels) -> float:
    """Seuil qui maximise le F1 (grille de 0,05 à 0,95)."""
    grille = np.arange(0.05, 0.96, 0.01)
    return float(grille[np.argmax([f1_score(y_vrai, proba >= s) for s in grille])])


def wilson(succes: int, effectif: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance de Wilson à 95 % d'une proportion."""
    if effectif <= 0:
        raise ValueError("l'effectif d'une proportion doit être positif")
    p = succes / effectif
    centre = p + z**2 / (2 * effectif)
    rayon = z * np.sqrt(p * (1 - p) / effectif + z**2 / (4 * effectif**2))
    return (
        float((centre - rayon) / (1 + z**2 / effectif)),
        float((centre + rayon) / (1 + z**2 / effectif)),
    )


def tirages_bootstrap(graine: int, taille: int, nombre: int = 2000) -> npt.NDArray[np.int64]:
    """Indices de messages tirés avec remise : `nombre` tirages de `taille` messages."""
    # `integers(taille)` tire dans [0, taille) : même flux que `integers(0, taille)`, sans le « 0 »
    # écrit en dur, qu'aucun test ne pourrait distinguer d'un « 0 » retiré.
    return np.random.default_rng(graine).integers(taille, size=(nombre, taille))


def intervalle_bootstrap(
    y_vrai: Reels,
    predictions: npt.NDArray[np.bool_],
    tirages: npt.NDArray[np.int64],
) -> tuple[float, float]:
    """Intervalle de confiance à 95 % du F1 : percentiles 2,5 et 97,5 des F1 des tirages."""
    f1_tirages = [f1_score(y_vrai[t], predictions[t]) for t in tirages]
    bas, haut = np.percentile(f1_tirages, [2.5, 97.5])
    return float(bas), float(haut)


@dataclass(frozen=True)
class Evaluation:
    """Évaluation d'un modèle sur le test, au seuil choisi sur la validation."""

    seuil: float
    predictions: npt.NDArray[np.bool_]
    matrice: npt.NDArray[np.int64]
    ligne: dict[str, object]


def evaluer(
    nom: str,
    y_val: Reels,
    p_val: Reels,
    y_test: Reels,
    p_test: Reels,
    tirages: npt.NDArray[np.int64],
) -> Evaluation:
    """Seuil choisi sur la validation, puis indicateurs sur le test (évalué une seule fois)."""
    if len(y_test) == 0:
        raise ValueError("le jeu de test est vide : rien à évaluer")
    seuil = meilleur_seuil(y_val, p_val)
    predictions = p_test >= seuil
    ic_bas, ic_haut = intervalle_bootstrap(y_test, predictions, tirages)
    ligne: dict[str, object] = {
        "modèle": nom,
        "seuil (validation)": round(seuil, 2),
        "F1 validation": round(f1_score(y_val, p_val >= seuil), 3),
        "précision test": round(precision_score(y_test, predictions), 3),
        "rappel test": round(recall_score(y_test, predictions), 3),
        "F1 test": round(f1_score(y_test, predictions), 3),
        "F1 test IC 95 %": f"[{ic_bas:.3f} ; {ic_haut:.3f}]",
        "erreurs test": int((predictions != y_test).sum()),
        "ROC-AUC test": round(roc_auc_score(y_test, p_test), 4),
        "PR-AUC test": round(average_precision_score(y_test, p_test), 4),
    }
    return Evaluation(seuil, predictions, confusion_matrix(y_test, predictions), ligne)


def mcnemar(
    juste_a: npt.NDArray[np.bool_], juste_b: npt.NDArray[np.bool_]
) -> tuple[int, int, float]:
    """Test exact de McNemar : (seul A a raison, seul B a raison, p).

    Parmi les messages où un seul des deux modèles se trompe, la répartition doit être 50/50 si
    les deux modèles se valent. Sans message discordant, p vaut 1.
    """
    seul_a = int((juste_a & ~juste_b).sum())
    seul_b = int((~juste_a & juste_b).sum())
    discordants = seul_a + seul_b
    p = float(binomtest(seul_a, discordants, 0.5).pvalue) if discordants else 1.0
    return seul_a, seul_b, p


@dataclass(frozen=True)
class Performance:
    """Rappel, précision et faux positifs d'un modèle sur le test, avec intervalles de Wilson."""

    vrais_positifs: int
    nb_spams: int
    nb_alertes: int
    faux_positifs: int
    nb_legitimes: int
    ic_rappel: tuple[float, float]
    ic_precision: tuple[float, float]


def performance(y_vrai: Reels, predictions: npt.NDArray[np.bool_]) -> Performance:
    """Comptes du modèle retenu et intervalles de Wilson de son rappel et de sa précision."""
    vrais_positifs = int((predictions & (y_vrai == 1)).sum())
    nb_spams, nb_alertes = int(y_vrai.sum()), int(predictions.sum())
    return Performance(
        vrais_positifs,
        nb_spams,
        nb_alertes,
        nb_alertes - vrais_positifs,
        len(y_vrai) - nb_spams,
        wilson(vrais_positifs, nb_spams),
        wilson(vrais_positifs, nb_alertes),
    )


def meilleure_epoque(historique: Historique) -> int:
    """Indice (à partir de 0) de l'époque au meilleur F1 de validation (la première si égalité)."""
    return int(np.argmax(historique["f1_val"]))


def retirer_signal(textes: Sequence[str], signal: str) -> tuple[list[int], list[str]]:
    """Textes qui contiennent le signal (jeton ou mot entier) et ces textes privés du signal."""
    motif = re.compile(rf"(?<![\w<]){re.escape(signal)}(?![\w>])")
    concernes = [i for i, t in enumerate(textes) if motif.search(t)]
    return concernes, [motif.sub(" ", textes[i]) for i in concernes]


def ablation(
    textes: Sequence[str],
    noter: Callable[[Sequence[str]], Reels],
    seuil: float,
    signaux: Sequence[str],
) -> list[dict[str, object]]:
    """Pour chaque signal : combien de textes le contiennent, baisse moyenne de probabilité, et
    combien repassent sous le seuil une fois le signal retiré."""
    proba_initiale = noter(textes)
    lignes: list[dict[str, object]] = []
    for signal in signaux:
        concernes, sans_signal = retirer_signal(textes, signal)
        if not concernes:
            continue
        proba_sans = noter(sans_signal)
        lignes.append(
            {
                "signal retiré": signal,
                "spams détectés qui le contiennent": len(concernes),
                "baisse moyenne de probabilité": float(
                    np.mean(proba_initiale[concernes] - proba_sans)
                ),
                "repassent sous le seuil": int((proba_sans < seuil).sum()),
            }
        )
    return lignes


def erreurs_du_modele(
    test: pd.DataFrame, proba: Reels, seuil: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(faux positifs, faux négatifs) du modèle sur le test, avec sa probabilité de spam."""
    erreurs = test.assign(proba=proba)
    faux_positifs = erreurs[(erreurs["cible"] == 0) & (erreurs["proba"] >= seuil)]
    faux_negatifs = erreurs[(erreurs["cible"] == 1) & (erreurs["proba"] < seuil)]
    return faux_positifs, faux_negatifs
