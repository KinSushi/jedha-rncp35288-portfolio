# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Intégration du machine learning : modèle de fraude entraîné sur l'OLAP, servi en temps réel.

- **Entraînement** (batch) : sur l'historique de l'entrepôt (`faits_paiements` + dimensions),
  avec un découpage TEMPOREL (on apprend sur le passé, on évalue sur la période suivante),
  comme en production.
- **Service** (temps réel) : chaque paiement capturé par la CDC est enrichi par les
  caractéristiques en ligne du client (base NoSQL), puis noté ; le score et la version du modèle
  sont écrits dans `signaux_fraude`.
- **Surveillance** : indice de stabilité de population (PSI) entre les scores d'entraînement et
  les scores en production : au-delà de 0,2, la distribution a dérivé et un réentraînement est
  déclenché.

CHOIX
-----
- Retenu : un `HistGradientBoostingClassifier` (200 itérations, taux d'apprentissage 0,05, classes
  pondérées, graine 42), bon sur des données tabulaires et rapide ; six variables simples
  (`VARIABLES`), aucune n'est un identifiant direct.
- Retenu : un découpage TEMPOREL à 80 % des dates : on apprend sur le passé, on évalue sur les
  dates strictement postérieures. Écarté : un tirage aléatoire, qui ferait fuir de l'information
  du futur vers l'apprentissage.
- Retenu : refuser d'entraîner quand l'apprentissage ou le test n'a qu'une classe. Écarté :
  laisser sklearn rendre un ROC-AUC NaN avec un simple avertissement, qui publierait un modèle
  sans valeur.
- Retenu : un PSI par déciles de la référence, avec classes ouvertes aux deux bouts et
  proportions décalées de 1e-6 (pas de logarithme de zéro). Refus des séries vides et de moins
  de deux classes : un NaN n'est jamais supérieur à 0,2, donc il aurait fait conclure à tort que
  le modèle est stable.
- Limite connue, non corrigée : `noter` reçoit les caractéristiques en ligne mais ne lit que les
  champs du paiement (voir POURQUOI).

OÙ
---
- Place : couche « intégration du machine learning ». Appelants : `stripe_demo/pipeline.py`
  (`entrainer`, `noter`, `psi`, `VERSION_MODELE`), `stripe_demo/mesures.py` (`entrainer`,
  `REQUETE_ENTRAINEMENT`, `VARIABLES`) et les tests `tests/test_fraude.py`, `tests/test_psi.py`.
- Dépendances : DuckDB (la requête d'entraînement lit `faits_paiements`, `dim_client`,
  `dim_date` de `stripe_demo/olap.py`), pandas, numpy, scikit-learn.

COMMENT
-------
- `entrainer` exécute `REQUETE_ENTRAINEMENT` (ordre date puis identifiant : l'ordre fixe rend les
  mesures reproductibles), coupe au 80e centile des dates, entraîne, et rend le modèle, les mesures
  (taux de fraude, PR-AUC et ROC-AUC sur le test, tailles) et les scores de la période
  d'apprentissage (référence du PSI). `noter` construit UNE ligne de variables et rend la
  probabilité de la classe « fraude ». `psi` compare deux séries de scores.
- Ce qui le prouve (`tests/test_fraude.py`) : la requête calcule les six variables
  (`test_la_requete_calcule_les_variables_du_modele`), le découpage tombe sur la bonne date
  (`test_le_decoupage_apprend_sur_le_passe_et_evalue_sur_le_futur`), les paramètres sont ceux de
  la note 07, `noter` est testé avec un modèle-espion, le PSI suit sa définition recalculée à part,
  et les refus (une seule classe, entrepôt vide, série vide, moins de deux classes).

POURQUOI
--------
- Variables, paramètres, découpage temporel et reproductibilité :
  `livrables/aia2/07_integration_ml.md:33-46` et `livrables/aia2/07_integration_ml.md:52-73`.
- Surveillance par PSI, seuils 0,1 et 0,2 : `livrables/aia2/07_integration_ml.md:99-114`.
- Le modèle 1.0 n'utilise pas encore les caractéristiques en ligne :
  `livrables/aia2/07_integration_ml.md:48-50` et `livrables/aia2/07_integration_ml.md:140-144`.

JOURNALISATION
--------------
- Rien n'est journalisé (pas de module `logging`). Les erreurs de données (une seule classe,
  séries vides, nombre de classes) lèvent `ValueError` avec un message explicite ; les mesures
  sont rendues à l'appelant, qui les affiche (`pipeline.py`) ou les écrit (`mesures.py`).
"""

from __future__ import annotations

from typing import Any

import duckdb
import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

VERSION_MODELE = "fraude-hgb-1.0"
VARIABLES = ["montant_usd", "heure", "pays_different", "nuit", "anciennete_jours", "mobile"]

REQUETE_ENTRAINEMENT = """
SELECT f.montant_usd, f.heure, (f.pays_ip <> c.code_pays)::INTEGER AS pays_different,
       (f.heure < 6)::INTEGER AS nuit, c.anciennete_jours,
       (f.appareil = 'mobile')::INTEGER AS mobile,
       f.est_conteste::INTEGER AS fraude, d.date
FROM faits_paiements f JOIN dim_client c USING (cle_client) JOIN dim_date d USING (cle_date)
ORDER BY d.date, f.id_paiement
"""


def entrainer(
    cx_olap: duckdb.DuckDBPyConnection,
) -> tuple[HistGradientBoostingClassifier, dict[str, float], NDArray[np.float64]]:
    """Entraîne sur les premières semaines, évalue sur les dernières ; renvoie les mesures."""
    donnees = cx_olap.execute(REQUETE_ENTRAINEMENT).df().sort_values("date")
    coupure = donnees["date"].quantile(0.8)
    app, test = donnees[donnees["date"] <= coupure], donnees[donnees["date"] > coupure]
    if app["fraude"].nunique() < 2 or test["fraude"].nunique() < 2:
        # Sans les deux classes des deux côtés, le modèle ou ses mesures (ROC-AUC) n'ont pas de
        # sens : sklearn rendrait NaN avec un simple avertissement. On refuse plutôt que de
        # publier un modèle sans valeur.
        raise ValueError("l'apprentissage et le test doivent chacun contenir des fraudes et "
                         "des paiements normaux")
    modele = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05,
                                            class_weight="balanced", random_state=42)
    modele.fit(app[VARIABLES], app["fraude"])
    proba = modele.predict_proba(test[VARIABLES])[:, 1]
    mesures = {
        "taux_fraude": round(float(donnees["fraude"].mean()), 4),
        "pr_auc_test": round(float(average_precision_score(test["fraude"], proba)), 3),
        "roc_auc_test": round(float(roc_auc_score(test["fraude"], proba)), 3),
        "lignes_apprentissage": len(app), "lignes_test": len(test),
    }
    return modele, mesures, modele.predict_proba(app[VARIABLES])[:, 1]


def noter(modele: HistGradientBoostingClassifier, paiement: dict[str, Any],
          caracteristiques: dict[str, Any], anciennete_jours: int, pays_client: str) -> float:
    """Score de fraude d'un paiement à partir de ses champs et des caractéristiques en ligne."""
    ligne = pd.DataFrame([{
        "montant_usd": paiement["montant_usd"], "heure": paiement["heure"],
        "pays_different": int(paiement["pays_ip"] != pays_client),
        "nuit": int(paiement["heure"] < 6), "anciennete_jours": anciennete_jours,
        "mobile": int(paiement["appareil"] == "mobile"),
    }])
    return float(modele.predict_proba(ligne[VARIABLES])[:, 1][0])


def psi(reference: NDArray[np.float64], production: NDArray[np.float64],
        classes: int = 10) -> float:
    """Indice de stabilité de population entre deux distributions de scores (déciles).

    Refuse moins de deux classes et les séries vides : sans cela, le calcul rendrait 0,0 ou NaN,
    et la décision « PSI > 0,2 » de `pipeline.py` concluerait à tort que le modèle est stable
    (un NaN n'est jamais supérieur à 0,2).
    """
    if classes < 2:
        raise ValueError(f"le PSI exige au moins 2 classes (reçu : {classes})")
    if len(reference) == 0 or len(production) == 0:
        raise ValueError("le PSI exige au moins un score de référence et un de production")
    bornes = np.quantile(reference, np.linspace(0, 1, classes + 1))
    bornes[0], bornes[-1] = -np.inf, np.inf
    ref = np.histogram(reference, bornes)[0] / len(reference) + 1e-6
    prod = np.histogram(production, bornes)[0] / len(production) + 1e-6
    return float(np.sum((prod - ref) * np.log(prod / ref)))
