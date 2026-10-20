# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Modèle de fraude : découpage dans le temps, variables, notation d'un paiement, PSI.

Un test sur le modèle lui-même ne peut pas se contenter de « ça tourne » : il doit montrer que
le découpage apprend sur le passé et évalue sur le futur, que les variables de la requête sont
celles du modèle, que `noter` construit la ligne attendue (testée avec un modèle-espion qui
enregistre ce qu'on lui donne) et que le PSI suit sa définition, y compris pour les scores qui
sortent de l'intervalle de référence. Chaque fonction est testée avec son contraire : une entrée
qu'elle doit refuser (une seule classe, un paiement incomplet, une série vide).
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.exceptions import NotFittedError
from sklearn.metrics import average_precision_score, roc_auc_score

from stripe_demo import fraude, olap

MESSAGE_DEUX_CLASSES = ("l'apprentissage et le test doivent chacun contenir des fraudes et "
                        "des paiements normaux")
MESSAGE_SERIE_VIDE = "le PSI exige au moins un score de référence et un de production"

# ---------------------------------------------------------------- entrepôt de test


def _entrepot(paiements: list[dict]) -> duckdb.DuckDBPyConnection:
    """Entrepôt en mémoire : mêmes tables que la démonstration, remplies avec `paiements`."""
    cx = duckdb.connect(":memory:")
    cx.execute(olap.DDL_ETOILE)
    jours = sorted({p["jour"] for p in paiements})
    for jour in jours:
        cx.execute("INSERT INTO dim_date VALUES (?, ?, 2026, 8, ?, 1, FALSE)",
                   [20260800 + jour, date(2026, 8, jour), jour])
    for p in paiements:
        cx.execute("INSERT OR REPLACE INTO dim_client VALUES (?, ?, ?, 'regulier')",
                   [p["client"], p["pays_client"], p["anciennete"]])
        cx.execute("INSERT INTO faits_paiements VALUES (?, ?, 1, ?, 1, ?, 'EUR', ?, ?, ?, "
                   "TRUE, FALSE, ?, NULL)",
                   [p["id"], 20260800 + p["jour"], p["client"], p["pays_ip"], p["appareil"],
                    p["heure"], p["montant"], bool(p["fraude"])])
    return cx


def _paiements_de_test(nombre: int = 301, jours: int = 15, graine: int = 12345) -> list[dict]:
    """Paiements déterministes : une fraude toutes les 7 lignes, dont certaines ressemblent à
    des paiements normaux (et inversement), pour que le modèle ne soit pas parfait."""
    alea = np.random.default_rng(graine)
    par_jour = math.ceil(nombre / jours)
    paiements = []
    for i in range(nombre):
        fraude = int(i % 7 == 0)
        suspect = alea.random() < (0.75 if fraude else 0.08)
        paiements.append({
            "id": i + 1, "jour": 1 + i // par_jour, "client": 1 + i % 40,
            "pays_client": "FR", "anciennete": 30 + (i % 40) * 5,
            "pays_ip": "US" if suspect else "FR",
            "appareil": "mobile" if alea.random() < 0.5 else "ordinateur",
            "heure": int(alea.integers(0, 6)) if suspect else int(alea.integers(6, 24)),
            "montant": float(alea.uniform(300, 900) if suspect else alea.uniform(5, 120)),
            "fraude": fraude})
    return paiements


@pytest.fixture(scope="module")
def entrepot_de_test():
    cx = _entrepot(_paiements_de_test())
    yield cx
    cx.close()


@pytest.fixture
def entrainement(entrepot_de_test):
    """L'entraînement est refait pour CHAQUE test qui le demande (environ une seconde).

    Pourquoi pas une fois pour tout le fichier : une fixture de module s'exécute au moment où le
    premier test la demande, et les outils de mutation (`mutmut`) rattachent l'appel de
    `fraude.entrainer` à ce seul test ; les autres, qui relisent le résultat, ne seraient jamais
    lancés contre une version altérée de la fonction. Ici chacun l'appelle donc réellement.
    """
    return fraude.entrainer(entrepot_de_test)


# ---------------------------------------------------------------- requête d'entraînement


def test_la_requete_calcule_les_variables_du_modele():
    """Pays différent, nuit (avant 6 h), mobile et fraude, dans l'ordre date puis identifiant."""
    cx = _entrepot([
        {"id": 3, "jour": 2, "client": 1, "pays_client": "FR", "anciennete": 40, "pays_ip": "FR",
         "appareil": "mobile", "heure": 5, "montant": 10.0, "fraude": 0},
        {"id": 1, "jour": 2, "client": 2, "pays_client": "FR", "anciennete": 90, "pays_ip": "US",
         "appareil": "ordinateur", "heure": 6, "montant": 20.0, "fraude": 1},
        {"id": 2, "jour": 1, "client": 1, "pays_client": "FR", "anciennete": 40, "pays_ip": "DE",
         "appareil": "tablette", "heure": 0, "montant": 30.0, "fraude": 0},
    ])

    tableau = cx.execute(fraude.REQUETE_ENTRAINEMENT).df()

    assert fraude.VARIABLES == ["montant_usd", "heure", "pays_different", "nuit",
                                "anciennete_jours", "mobile"]
    assert fraude.VERSION_MODELE == "fraude-hgb-1.0"
    assert list(tableau.columns) == [*fraude.VARIABLES, "fraude", "date"]
    # jour 1 d'abord ; le 2 e jour est ordonné par identifiant croissant (1 puis 3)
    assert tableau["montant_usd"].tolist() == [30.0, 20.0, 10.0]
    assert tableau["pays_different"].tolist() == [1, 1, 0]
    assert tableau["nuit"].tolist() == [1, 0, 1]  # 6 h n'est pas la nuit, 5 h oui, 0 h oui
    assert tableau["mobile"].tolist() == [0, 0, 1]
    assert tableau["anciennete_jours"].tolist() == [40, 90, 40]
    assert tableau["fraude"].tolist() == [0, 1, 0]


# ---------------------------------------------------------------- entraînement


def test_le_decoupage_apprend_sur_le_passe_et_evalue_sur_le_futur(entrepot_de_test,
                                                                   entrainement):
    """Le 80e centile des dates tombe sur un jour précis : tout ce jour-là appartient à
    l'apprentissage, et seules les dates STRICTEMENT postérieures servent à évaluer. Les mesures
    de qualité sont calculées sur cette seule période de test."""
    modele, mesures, scores = entrainement
    paiements = _paiements_de_test()
    jours = sorted(p["jour"] for p in paiements)
    coupure = jours[int(0.8 * (len(jours) - 1))]  # 80 % des lignes, rang entier : 240 sur 301
    attendu_apprentissage = sum(1 for j in jours if j <= coupure)
    attendu_test = sum(1 for j in jours if j > coupure)

    assert mesures["lignes_apprentissage"] == attendu_apprentissage
    assert mesures["lignes_test"] == attendu_test
    assert attendu_apprentissage + attendu_test == len(paiements)
    # La coupure tombe sur un jour de plusieurs lignes : `<` ou `>=` donneraient d'autres comptes.
    assert jours.count(coupure) > 1
    assert mesures["taux_fraude"] == round(sum(p["fraude"] for p in paiements) / 301, 4)
    assert mesures["taux_fraude"] == 0.1429  # 43 fraudes sur 301

    tableau = entrepot_de_test.execute(fraude.REQUETE_ENTRAINEMENT).df().sort_values("date")
    seuil = tableau["date"].quantile(0.8)
    test, app = tableau[tableau["date"] > seuil], tableau[tableau["date"] <= seuil]
    proba = modele.predict_proba(test[fraude.VARIABLES])[:, 1]
    assert mesures["pr_auc_test"] == round(average_precision_score(test["fraude"], proba), 3)
    assert mesures["roc_auc_test"] == round(roc_auc_score(test["fraude"], proba), 3)
    assert 0.5 < mesures["roc_auc_test"] < 1.0  # mieux que le hasard, sans être parfait
    # Les scores de référence du PSI sont ceux du modèle sur l'apprentissage.
    assert len(scores) == mesures["lignes_apprentissage"]
    assert scores == pytest.approx(modele.predict_proba(app[fraude.VARIABLES])[:, 1])
    assert 0.0 <= scores.min() <= scores.max() <= 1.0


def test_le_modele_porte_les_parametres_documentes_et_l_entrainement_est_reproductible(
        entrepot_de_test, entrainement):
    """200 itérations, taux d'apprentissage 0,05, classes équilibrées, graine 42 (note 07 § 4)."""
    modele, mesures, scores = entrainement
    parametres = modele.get_params()
    note = (Path(__file__).resolve().parents[2] / "livrables" / "aia2"
            / "07_integration_ml.md").read_text(encoding="utf-8")

    assert parametres["max_iter"] == 200
    assert parametres["learning_rate"] == 0.05
    assert parametres["class_weight"] == "balanced"
    assert parametres["random_state"] == 42
    assert modele.n_iter_ == 200  # pas d'arrêt précoce : les 200 itérations sont faites
    assert ("200 itérations, taux d'apprentissage 0,05, poids de classes équilibrés, "
            "graine 42") in note
    assert f"`{fraude.VERSION_MODELE}`" in note
    _autre_modele, autres_mesures, autres_scores = fraude.entrainer(entrepot_de_test)
    assert autres_mesures == mesures
    assert autres_scores == pytest.approx(scores)


def test_l_entrainement_refuse_un_entrepot_sans_les_deux_classes_des_deux_cotes():
    """Sans fraude, sans paiement normal ou sans table, aucun modèle de fraude n'a de sens."""
    sans_fraude = _entrepot([{**p, "fraude": 0} for p in _paiements_de_test()])
    fraude_au_debut = _entrepot([{**p, "fraude": int(p["fraude"] and p["jour"] <= 5)}
                                 for p in _paiements_de_test()])
    fraude_a_la_fin = _entrepot([{**p, "fraude": int(p["fraude"] and p["jour"] >= 14)}
                                 for p in _paiements_de_test()])
    vide = duckdb.connect(":memory:")
    vide.execute(olap.DDL_ETOILE)

    # Sans fraude, ou seulement d'un côté : le ROC-AUC n'existe pas (sklearn rendrait NaN).
    for entrepot in (sans_fraude, fraude_au_debut, fraude_a_la_fin, vide):
        with pytest.raises(ValueError) as erreur:
            fraude.entrainer(entrepot)
        assert str(erreur.value) == MESSAGE_DEUX_CLASSES

    with pytest.raises(duckdb.CatalogException):  # un entrepôt sans les tables
        fraude.entrainer(duckdb.connect(":memory:"))


# ---------------------------------------------------------------- notation d'un paiement


class ModeleEspion:
    """Faux modèle : garde la ligne reçue et rend une probabilité connue (0,3 / 0,7)."""

    def __init__(self):
        self.recu = None

    def predict_proba(self, ligne):
        self.recu = ligne
        return np.array([[0.3, 0.7]])


def _paiement(**champs):
    base = {"id_client": 9, "montant_usd": 42.5, "heure": 14, "pays_ip": "FR",
            "appareil": "ordinateur"}
    base.update(champs)
    return base


def test_noter_construit_la_ligne_de_variables_et_refuse_un_paiement_incomplet():
    espion = ModeleEspion()
    donnees = _paiement()
    avant = dict(donnees)

    score = fraude.noter(espion, donnees, {}, 100, "FR")

    # La probabilité de la classe « fraude », en flottant ; la ligne dans l'ordre des variables.
    assert score == 0.7 and isinstance(score, float)
    assert list(espion.recu.columns) == fraude.VARIABLES
    assert espion.recu.iloc[0].tolist() == [42.5, 14, 0, 0, 100, 0]
    assert len(espion.recu) == 1
    assert donnees == avant  # le paiement donné n'est pas modifié

    # Pays différent, nuit (avant 6 h exactement), appareil mobile, ancienneté donnée.
    for pays_ip, pays_client, attendu in (("US", "FR", 1), ("FR", "FR", 0)):
        fraude.noter(espion, _paiement(pays_ip=pays_ip), {}, 100, pays_client)
        assert espion.recu["pays_different"].iloc[0] == attendu
    for heure, attendu in ((0, 1), (5, 1), (6, 0), (23, 0)):
        fraude.noter(espion, _paiement(heure=heure), {}, 100, "FR")
        assert espion.recu["nuit"].iloc[0] == attendu
        assert espion.recu["heure"].iloc[0] == heure
    for appareil, attendu in (("mobile", 1), ("ordinateur", 0), ("MOBILE", 0), ("api", 0)):
        fraude.noter(espion, _paiement(appareil=appareil), {}, 100, "FR")
        assert espion.recu["mobile"].iloc[0] == attendu
    fraude.noter(espion, _paiement(), {"nb_paiements": 99}, 321, "FR")
    assert espion.recu["anciennete_jours"].iloc[0] == 321  # pas celle des caractéristiques

    # Un paiement sans heure, ou un modèle non entraîné, ne peut pas être noté.
    with pytest.raises(KeyError):
        fraude.noter(ModeleEspion(), {"montant_usd": 1.0, "pays_ip": "FR", "appareil": "api"},
                     {}, 100, "FR")
    with pytest.raises(NotFittedError):
        fraude.noter(HistGradientBoostingClassifier(), _paiement(), {}, 100, "FR")


def test_noter_avec_un_vrai_modele_donne_une_probabilite_plus_haute_a_un_profil_de_fraude(
        entrainement):
    modele, _mesures, _proba = entrainement
    suspect = _paiement(montant_usd=700.0, heure=2, pays_ip="US", appareil="mobile")
    normal = _paiement(montant_usd=40.0, heure=15, pays_ip="FR", appareil="ordinateur")

    score_suspect = fraude.noter(modele, suspect, {}, 100, "FR")
    score_normal = fraude.noter(modele, normal, {}, 100, "FR")

    assert 0.0 <= score_normal <= 1.0 and 0.0 <= score_suspect <= 1.0
    assert score_suspect > score_normal  # le profil de fraude connu est mieux noté


# ---------------------------------------------------------------- indice de stabilité (PSI)


def _psi_attendu(reference, production, classes):
    """Définition du PSI, recalculée à part : bornes = quantiles de la référence, classes
    ouvertes aux deux bouts, proportions décalées de 1e-6, somme de (p - r) * ln(p / r)."""
    reference, production = np.asarray(reference), np.asarray(production)
    bornes = [float(q) for q in np.quantile(reference, [k / classes for k in range(classes + 1)])]
    bornes[0], bornes[-1] = -math.inf, math.inf

    def proportions(valeurs):
        comptes = [0] * classes
        for v in valeurs:
            for k in range(classes):
                dernier = k == classes - 1
                if bornes[k] <= v and (v < bornes[k + 1] or (dernier and v <= bornes[k + 1])):
                    comptes[k] += 1
                    break
        return [c / len(valeurs) + 1e-6 for c in comptes]

    ref, prod = proportions(reference), proportions(production)
    return sum((p - r) * math.log(p / r) for p, r in zip(prod, ref, strict=True))


def test_psi_suit_sa_definition_pour_tout_nombre_de_classes_et_refuse_les_entrees_vides():
    alea = np.random.default_rng(7)
    reference = alea.normal(0.0, 1.0, 2000)
    production = alea.normal(0.4, 1.2, 800)
    reference_avant = reference.copy()

    # Au dixième de milliardième près, avec dix classes (défaut) ou un autre nombre.
    assert fraude.psi(reference, production) == pytest.approx(
        _psi_attendu(reference, production, 10), rel=1e-9)
    for classes in (2, 4, 5, 20):
        uniformes = np.random.default_rng(classes).uniform(0.0, 1.0, 1000)
        beta = np.random.default_rng(classes).beta(2.0, 3.0, 500)
        assert fraude.psi(uniformes, beta, classes=classes) == pytest.approx(
            _psi_attendu(uniformes, beta, classes), rel=1e-9)
    assert np.array_equal(reference, reference_avant)  # les scores donnés ne sont pas modifiés

    # Une seule valeur de production suffit au calcul.
    une_valeur = fraude.psi(np.arange(100, dtype=float), np.array([42.0]), classes=2)
    assert math.isfinite(une_valeur)
    assert une_valeur == pytest.approx(_psi_attendu(np.arange(100.0), [42.0], 2), rel=1e-9)

    # Une série vide donnait NaN, et « NaN > 0,2 » est faux : le modèle paraissait stable.
    for vide_ref, vide_prod in ((np.array([], dtype=float), np.array([1.0, 2.0])),
                                (np.arange(10, dtype=float), np.array([], dtype=float))):
        with pytest.raises(ValueError) as erreur:
            fraude.psi(vide_ref, vide_prod)
        assert str(erreur.value) == MESSAGE_SERIE_VIDE
    for classes in (1, 0, -3):
        with pytest.raises(ValueError) as erreur:
            fraude.psi(np.arange(10, dtype=float), np.arange(10, dtype=float), classes=classes)
        assert str(erreur.value) == f"le PSI exige au moins 2 classes (reçu : {classes})"


def test_les_scores_hors_de_l_intervalle_de_reference_sont_comptes_dans_les_classes_extremes():
    """Production entièrement hors de la plage de référence : répartie 10/10 dans les deux
    classes extrêmes, donc aucune dérive entre deux classes de même poids ; et concentrée dans
    une seule classe, la dérive est extrême."""
    reference = np.arange(100, dtype=float)
    hors_plage = np.concatenate([np.full(10, -50.0), np.full(10, 500.0)])
    concentree = np.full(50, 5.0)  # tout dans la première classe

    assert fraude.psi(reference, hors_plage, classes=2) == pytest.approx(0.0, abs=1e-12)
    attendu = _psi_attendu(reference, concentree, 2)
    assert attendu > 5.0  # presque toute la masse d'un côté : dérive extrême
    assert fraude.psi(reference, concentree, classes=2) == pytest.approx(attendu, rel=1e-9)
