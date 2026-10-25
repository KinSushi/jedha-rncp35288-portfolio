# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Fonctions du notebook « Speed Dating » (Tinder), extraites pour être testées une à une.

CHOIX
-----
- Retenu : des fonctions qui prennent des tableaux pandas et rendent des tableaux ou des
  nombres, sans lire de fichier ni afficher quoi que ce soit. Le notebook garde le
  raisonnement (cellules Markdown), les affichages et les graphiques ; chaque chiffre qu'il
  affiche passe par une fonction que `tests/test_speed_dating.py` peut contrôler.
- Retenu : une entrée interdite est refusée par une exception `ValueError` qui dit ce qui
  manque (colonne protégée par le RGPD ou demandée deux fois, groupe de grappes vide ou donné
  deux fois, borne indéfinie, suffixe vide, écart-type nul, tableau sans écart, aucune ligne
  appariée, χ² négatif). Jamais de valeur inventée en silence.
- Écarté : tout mettre dans un seul fichier de test qui exécute le notebook. Le notebook
  s'exécute en quelques secondes (`tests/test_notebook_tinder.py` le fait aussi), mais un
  échec n'y dirait pas quelle règle de nettoyage ou quel calcul est en cause.
- Écarté : déplacer les graphiques dans ce module. Ils sont du raisonnement montré au jury
  (titres qui énoncent le message) ; les tester n'apporterait rien de vérifiable.
- Écarté : modifier les tableaux reçus en place. Les fonctions rendent un nouveau tableau,
  ce qui évite l'avertissement de fragmentation de pandas et rend chaque étape rejouable.

OÙ
---
- Place : à côté du notebook, dans `1_Tinder/`. Appelant : `Speed_Dating_Bloc2_CDSD.ipynb`
  (cellule 2 pour l'import ; les fonctions servent aux sections 2, 4 et 5). Aucun autre
  appelant.
- Dépendances : numpy, pandas, scipy (rangs) et statsmodels (régression logistique), versions
  épinglées dans `requirements.txt`. statsmodels ne publie ni types ni fichier `py.typed` : son
  import porte un `type: ignore[import-untyped]` justifié plus bas.

COMMENT
-------
1. Nettoyage : `convertir_en_nombres` (chaînes à séparateurs de milliers), `normaliser_a_100`
   (préférences ramenées à 100 points), `invalider_hors_bornes` (notes et âges hors échelle
   deviennent manquants), `minimiser_colonnes` (refuse les colonnes protégées par le RGPD).
2. Structure : `appariement_miroir` relie chaque décision à celle du partenaire,
   `part_identique` vérifie que les variables du rendez-vous y sont les mêmes, `rendez_vous`
   garde une ligne par rendez-vous, `agreger_personnes` une ligne par participant.
3. Statistiques : `bornes_iqr`, `ajouter_z`, `logit_grappes` (erreurs standard groupées sur un
   ou deux identifiants), `rapports_de_cotes`, `formater_p`, `consigner_test` (décision au
   seuil de Bonferroni), `d_cohen`, `d_cohen_apparie`, `rang_bisserial_apparie`, `v_cramer`.
4. Tests qui le prouvent : `tests/test_speed_dating.py` (cas calculés à la main, cas limites
   aux bornes, refus des entrées interdites) et `tests/test_notebook_tinder.py` (le notebook
   exécuté de bout en bout redonne les chiffres du jury).

POURQUOI
--------
- Erreurs standard groupées à deux voies : estimateur de Cameron, Gelbach et Miller (2011),
  que statsmodels calcule quand on lui passe deux colonnes de groupes. L'article
  n'est pas reproduit ici ; le comportement est vérifié par
  `tests/test_speed_dating.py` (mêmes coefficients, erreurs différentes d'un groupement à
  l'autre).
- Minimisation des données (RGPD, article 5.1.c) et catégories particulières (article 9) :
  le notebook les applique et les cite en section 8, `Speed_Dating_Bloc2_CDSD.ipynb`.
  Le refus des colonnes protégées est vérifié par `tests/test_speed_dating.py`.
- Les chiffres du jury (42,0 % de oui, 16,5 % de matchs, p = 0,18 pour les centres d'intérêt)
  sont ceux du notebook exécuté, contrôlés par `tests/test_notebook_tinder.py`.

JOURNALISATION
--------------
Aucune : fonctions pures, sans entrée-sortie ni module `logging`. Une entrée inexploitable
lève `ValueError` (le message dit ce qui manque) ; les comptes (valeurs invalidées, lignes
retirées) sont rendus à l'appelant, et le notebook les affiche.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, SupportsFloat

import numpy as np
import pandas as pd

# statsmodels ne fournit ni stubs ni py.typed (mesuré : « module is installed, but missing
# library stubs or py.typed marker »). L'ignorance est limitée à cette ligne et au code d'erreur
# `import-untyped` ; les résultats d'ajustement sont typés par `ResultatLogit` ci-dessous.
import statsmodels.formula.api as smf  # type: ignore[import-untyped]
from scipy import stats

#: Colonnes que la base d'analyse ne doit jamais reprendre. `race`, `race_o` et `imprelig` relèvent
#: de l'article 9 du RGPD (origine, religion) ; le revenu, les études et le code postal sont
#: écartés par minimisation (article 5.1.c) ; `age_o` (âge du partenaire) est sans usage.
COLONNES_ECARTEES: frozenset[str] = frozenset(
    {"race", "race_o", "imprelig", "income", "tuition", "mn_sat", "zipcode", "undergra", "age_o"}
)


class ResultatLogit(Protocol):
    """Ce que le notebook (et, pour `mle_settings`, les tests) lit d'une régression ajustée."""

    params: pd.Series[float]
    pvalues: pd.Series[float]
    tvalues: pd.Series[float]
    nobs: float
    mle_settings: dict[str, Any]  # réglages de l'ajustement, dont `disp` (affichage)

    def summary(self) -> Any:
        """Tableau récapitulatif de statsmodels (le notebook en affiche `tables[1]`)."""


# ------------------------------------------------------------------------------------------
# Nettoyage
# ------------------------------------------------------------------------------------------


def convertir_en_nombres(serie: pd.Series[Any]) -> pd.Series[float]:
    """Convertit des chaînes à séparateurs de milliers (« 12,345.00 ») en nombres.

    La virgule des milliers est retirée AVANT la conversion ; ce qui n'est pas un nombre devient
    NaN (jamais d'exception). Les décimales « .00 » se lisent telles quelles : les retirer du
    texte, comme le faisait la première version, était inutile et pouvait abîmer un nombre
    (« 2.005 » devenait « 25 ») ; sur ce fichier, les conversions sont identiques (recompté le
    17/10 sur `income`, `tuition` et `mn_sat`).
    """
    sans_milliers = serie.astype(str).str.replace(",", "")  # texte littéral (pandas >= 2)
    return pd.to_numeric(sans_milliers, errors="coerce")


def normaliser_a_100(
    donnees: pd.DataFrame, colonnes: Sequence[str], suffixe: str = "_norm"
) -> pd.DataFrame:
    """Ajoute, pour chaque colonne, sa version ramenée à une somme de 100 points par ligne.

    Une ligne dont les préférences ne sont pas toutes renseignées, ou dont la somme n'est pas
    strictement positive, reçoit NaN : on ne répartit pas 100 points sur une somme inconnue.
    Refus (`ValueError`) : aucune colonne (rien ne serait normalisé, sans le dire) ; un suffixe
    vide (chaque colonne normalisée porterait le nom de sa colonne d'origine, en double).
    """
    if not colonnes:
        raise ValueError("aucune colonne à ramener à 100 points")
    if not suffixe:
        raise ValueError("suffixe vide : les colonnes normalisées doubleraient celles d'origine")
    somme = donnees[list(colonnes)].sum(axis=1, min_count=len(colonnes))
    normalisees = pd.DataFrame(
        {
            colonne + suffixe: np.where(somme > 0, donnees[colonne] / somme * 100, np.nan)
            for colonne in colonnes
        },
        index=donnees.index,
    )
    return pd.concat([donnees, normalisees], axis=1)


def invalider_hors_bornes(
    serie: pd.Series[float], bas: float, haut: float
) -> tuple[pd.Series[float], int]:
    """Remplace par NaN les valeurs hors de [bas, haut] (bornes comprises) ; rend aussi leur nombre.

    Une valeur hors échelle est une erreur de saisie : on la rend manquante plutôt que de
    l'imputer (imputer inventerait un jugement). Des bornes inversées (`bas` > `haut`)
    invalideraient tout ou rien selon l'écriture : `ValueError`. Une borne indéfinie (NaN) aussi :
    aucune valeur n'étant « entre » NaN et une autre borne, toute la série serait invalidée.
    """
    if np.isnan(bas) or np.isnan(haut):
        raise ValueError(f"borne indéfinie : [{bas}, {haut}]")
    if bas > haut:
        raise ValueError(f"bornes inversées : {bas} > {haut}")
    hors = serie.notna() & ~serie.between(bas, haut)
    return serie.mask(hors), int(hors.sum())


def minimiser_colonnes(
    donnees: pd.DataFrame,
    colonnes: Sequence[str],
    interdites: frozenset[str] = COLONNES_ECARTEES,
) -> pd.DataFrame:
    """Copie des seules `colonnes` demandées ; refuse toute colonne protégée (RGPD 5.1.c et 9).

    Une colonne demandée deux fois est aussi refusée : pandas la copierait deux fois, et la
    base aurait deux colonnes du même nom (une lecture rendrait alors un tableau, pas une série).
    """
    reprises = sorted(set(colonnes) & interdites)
    if reprises:
        noms = ", ".join(reprises)
        raise ValueError(f"colonne(s) protégée(s) par le RGPD, non reprises : {noms}")
    doublons = sorted({c for c in colonnes if list(colonnes).count(c) > 1})
    if doublons:
        raise ValueError(f"colonne(s) demandée(s) plusieurs fois : {', '.join(doublons)}")
    return donnees[list(colonnes)].copy()


# ------------------------------------------------------------------------------------------
# Structure : décisions, rendez-vous, personnes
# ------------------------------------------------------------------------------------------


def appariement_miroir(decisions: pd.DataFrame) -> pd.DataFrame:
    """Apparie chaque décision (iid, pid) à la décision miroir du même rendez-vous (pid, iid).

    Les colonnes de la ligne miroir portent le suffixe `_miroir`. Une décision sans miroir
    (partenaire inconnu, rendez-vous à sens unique) n'apparaît pas dans le résultat.
    """
    return decisions.merge(
        decisions,
        left_on=["iid", "pid"],
        right_on=["pid", "iid"],
        suffixes=("", "_miroir"),
    )


def part_identique(miroir: pd.DataFrame, variable: str) -> float:
    """Part des lignes appariées où `variable` vaut la même chose des deux côtés (NaN = NaN).

    Sans aucune ligne appariée, la part n'existe pas : `ValueError` (pandas rendrait NaN, qu'un
    contrôle « part == 1 » lirait comme un simple écart).
    """
    if miroir.empty:
        raise ValueError("aucune ligne appariée : part indéfinie")
    gauche = miroir[variable]
    droite = miroir[variable + "_miroir"]
    return float(((gauche == droite) | (gauche.isna() & droite.isna())).mean())


def rendez_vous(decisions: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par rendez-vous : celle où iid < pid (le partenaire inconnu est écarté)."""
    return decisions[decisions["iid"] < decisions["pid"]].copy()


def agreger_personnes(decisions: pd.DataFrame, agregation: dict[str, str]) -> pd.DataFrame:
    """Une ligne par participant (`iid`), selon `agregation` {colonne: fonction pandas}.

    `order` devient `nb_rendez_vous` (nombre de rendez-vous) et `attr_o` devient
    `attr_o_moyen` (note d'attirance moyenne reçue), quand ces colonnes sont agrégées.
    """
    personnes = decisions.groupby("iid").agg(agregation).reset_index()
    return personnes.rename(columns={"order": "nb_rendez_vous", "attr_o": "attr_o_moyen"})


# ------------------------------------------------------------------------------------------
# Statistiques
# ------------------------------------------------------------------------------------------


def bornes_iqr(serie: pd.Series[float]) -> tuple[float, float, int]:
    """Bornes de la règle de l'IQR [Q1 - 1,5 × IQR ; Q3 + 1,5 × IQR] et nombre de valeurs dehors.

    Une série sans aucune valeur renseignée n'a pas de quartiles : `ValueError`.
    """
    if serie.dropna().empty:
        raise ValueError("série vide ou sans valeur renseignée : quartiles indéfinis")
    q1 = float(serie.quantile(0.25))
    q3 = float(serie.quantile(0.75))
    ecart = q3 - q1
    borne_inf = q1 - 1.5 * ecart
    borne_sup = q3 + 1.5 * ecart
    return borne_inf, borne_sup, int(((serie < borne_inf) | (serie > borne_sup)).sum())


def ajouter_z(donnees: pd.DataFrame, colonnes: Sequence[str], suffixe: str = "_z") -> pd.DataFrame:
    """Copie de `donnees` avec, par colonne, sa version standardisée (moyenne 0, écart-type 1).

    L'écart-type est celui de l'échantillon (ddof = 1, défaut de pandas). Une colonne constante
    ne peut pas être standardisée : refus explicite plutôt qu'un NaN muet. Un suffixe vide est
    refusé aussi : la version standardisée remplacerait la colonne d'origine, sans le dire.
    """
    if not suffixe:
        raise ValueError("suffixe vide : la colonne standardisée remplacerait celle d'origine")
    resultat = donnees.copy()
    for colonne in colonnes:
        ecart = donnees[colonne].std()
        if not ecart > 0:
            raise ValueError(f"colonne constante ou vide, standardisation impossible : {colonne}")
        resultat[colonne + suffixe] = (donnees[colonne] - donnees[colonne].mean()) / ecart
    return resultat


def logit_grappes(
    formule: str, donnees: pd.DataFrame, grappes: list[str]
) -> ResultatLogit:
    """Ajuste une régression logistique à erreurs standard groupées sur une ou deux dimensions.

    Les lignes incomplètes (variables de la formule ou des grappes) sont retirées AVANT
    l'ajustement : le modèle et les groupes portent exactement sur les mêmes lignes.
    L'effectif réellement utilisé est disponible dans `modele.nobs`.
    """
    if not 1 <= len(grappes) <= 2:
        raise ValueError(f"une ou deux grappes attendues, reçu {len(grappes)} : {grappes}")
    if len(set(grappes)) != len(grappes):
        # Deux fois la même dimension n'est pas un groupement à deux voies : la correction de
        # Cameron, Gelbach et Miller compterait deux fois la même corrélation intra-groupe.
        raise ValueError(f"la même grappe est donnée deux fois : {grappes}")
    propres = donnees.dropna(subset=grappes)
    modele = smf.logit(formule, data=propres)  # patsy retire les lignes à valeur manquante
    lignes_utilisees = modele.data.row_labels  # index des lignes gardées par patsy
    # Les identifiants ne servent qu'à regrouper : ils sont pris tels quels. Une conversion en
    # entiers (première version) fusionnerait deux identifiants non entiers voisins (1,2 et 1,7).
    groupes = propres.loc[lignes_utilisees, grappes].to_numpy()
    if len(grappes) == 1:
        groupes = groupes[:, 0]  # une seule dimension : vecteur, et non matrice à une colonne
    # disp=False : statsmodels n'affiche pas son journal d'optimisation dans la sortie du
    # notebook ; `tests/test_speed_dating.py` vérifie que rien n'est affiché.
    ajuste: ResultatLogit = modele.fit(cov_type="cluster", cov_kwds={"groups": groupes}, disp=False)
    return ajuste


def rapports_de_cotes(coefficients: pd.Series[float]) -> pd.Series[float]:
    """exp(coefficient) : rapport de cotes pour +1 de la variable (1 = aucun effet).

    Un coefficient logistique est un logarithme de rapport de cotes ; l'exponentielle le
    ramène à « les chances de oui sont multipliées par X ». Le calcul porte sur les valeurs ;
    le nom de chaque variable est reporté sur son rapport.
    """
    return pd.Series(np.exp(coefficients.to_numpy()), index=coefficients.index)


def formater_p(p_valeur: SupportsFloat) -> str:
    """Rend une p-valeur lisible ; sous 1e-300, on atteint la précision des nombres flottants.

    Une probabilité est comprise entre 0 et 1 : NaN ou une valeur hors de [0, 1] est une erreur
    de calcul en amont, refusée (`ValueError`) plutôt que mise en forme.
    """
    valeur = float(p_valeur)
    if not 0.0 <= valeur <= 1.0:
        raise ValueError(f"p-valeur hors de [0, 1] ou indéfinie : {valeur}")
    return "< 1e-300" if valeur < 1e-300 else f"{valeur:.2e}"


def consigner_test(
    resultats: list[dict[str, str]],
    seuil: float,
    nom: str,
    h0: str,
    unite: str,
    statistique: str,
    p_valeur: SupportsFloat,
    effet: str,
) -> None:
    """Ajoute un test au tableau récapitulatif, avec sa décision au seuil de Bonferroni.

    La décision est stricte : une p-valeur égale au seuil conserve H0. Un seuil hors de ]0, 1]
    ou une p-valeur invalide est refusé (`ValueError`) et le tableau reste inchangé.
    """
    if not 0.0 < seuil <= 1.0:
        raise ValueError(f"seuil hors de ]0, 1] : {seuil}")
    resultats.append(
        {
            "test": nom,
            "H0": h0,
            "unité": unite,
            "statistique": statistique,
            "p-valeur": formater_p(p_valeur),
            "taille d'effet": effet,
            "décision (Bonferroni)": (
                "H0 rejetée" if float(p_valeur) < seuil else "H0 conservée"
            ),
        }
    )


def d_cohen(groupe_a: pd.Series[float], groupe_b: pd.Series[float]) -> float:
    """d de Cohen : (moyenne de A - moyenne de B) / écart-type moyen des deux groupes (Welch)."""
    ecart_moyen = np.sqrt((groupe_a.var() + groupe_b.var()) / 2)
    if not ecart_moyen > 0:
        raise ValueError("variances nulles dans les deux groupes : d de Cohen indéfini")
    return float((groupe_a.mean() - groupe_b.mean()) / ecart_moyen)


def d_cohen_apparie(ecarts: pd.Series[float]) -> float:
    """d de Cohen apparié : moyenne des différences / écart-type des différences."""
    ecart_type = ecarts.std()
    if not ecart_type > 0:
        raise ValueError("différences constantes ou en nombre insuffisant : d apparié indéfini")
    return float(ecarts.mean() / ecart_type)


def rang_bisserial_apparie(ecarts: pd.Series[float]) -> float:
    """Corrélation de rang bisériale appariée, entre -1 et 1 (taille d'effet du Wilcoxon signé).

    (somme des rangs des écarts positifs - somme des rangs des écarts négatifs) / somme des rangs,
    les rangs étant ceux des valeurs absolues ; les écarts nuls sont ignorés. Écrit comme la
    somme des rangs signés (chaque rang multiplié par le signe +1 ou -1 de son écart), divisée
    par la somme des rangs : c'est la même quantité, en une seule opération.
    """
    non_nuls = ecarts[ecarts != 0].to_numpy()
    if non_nuls.size == 0:
        raise ValueError("aucun écart non nul : corrélation de rang bisériale indéfinie")
    rangs = stats.rankdata(np.abs(non_nuls))
    return float(np.dot(np.sign(non_nuls), rangs) / rangs.sum())


def v_cramer(tableau: pd.DataFrame, chi2: float) -> float:
    """V de Cramér : racine de χ² / (n × (min(lignes, colonnes) - 1)), pour un tableau croisé.

    Refus (`ValueError`) : moins de deux lignes ou de deux colonnes ; un χ² négatif ou indéfini
    (la racine rendrait NaN sans prévenir) ; un tableau sans aucun effectif (division par zéro).
    """
    if min(tableau.shape) < 2:
        raise ValueError("un tableau de contingence a au moins deux lignes et deux colonnes")
    if not chi2 >= 0:
        raise ValueError(f"χ² négatif ou indéfini : {chi2}")
    effectif = tableau.to_numpy().sum()
    if not effectif > 0:
        raise ValueError("tableau de contingence sans aucun effectif")
    return float(np.sqrt(chi2 / (effectif * (min(tableau.shape) - 1))))
