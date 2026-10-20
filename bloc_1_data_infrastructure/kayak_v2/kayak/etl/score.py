# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Calcul du score météo agrégé par ville.

La règle de score est la suivante :
- Plus la température maximale moyenne est élevée, meilleur est le score (chaleur +).
- Plus l'ensoleillement moyen est élevé, meilleur est le score (ensoleillement +).
- Plus la pluie totale est élevée, moins bon est le score (pluie -).
- Si l'ensoleillement n'est pas disponible, on utilise la probabilité de pluie moyenne
  avec un signe négatif (plus elle est élevée, moins bon est le score).

Le score final est la moyenne des scores z de ces trois composantes.
Le rang 1 correspond au score le plus élevé (meilleure destination).

CHOIX
-----
- **Retenu : la moyenne de trois scores z** (chaleur, ensoleillement, pluie), à poids égaux. Les
  trois grandeurs n'ont ni la même unité ni la même échelle : le score z (écart à la moyenne des
  villes, en écarts-types) les rend comparables sans fixer de pondération arbitraire. *Écarté* :
  une somme pondérée des valeurs brutes, dont les poids seraient un choix non justifié par les
  données.
- **Retenu : repli sur la probabilité de pluie (signe négatif)** quand l'ensoleillement manque,
  car la source OpenWeather ne fournit pas d'ensoleillement (kayak/collecte/meteo.py:178).
- **Retenu : sans ensoleillement ni probabilité de pluie, score neutre 0.0 et rang 1 pour toutes
  les villes** (ex æquo). *Écarté* : lever une exception, qui arrêterait toute l'ETL pour une
  donnée absente ; c'est un jugement de conception, pas une mesure.
- **Retenu : rang « min »** (deux villes ex æquo partagent le meilleur rang), et un écart-type nul
  ou indéfini donne 0.0 à la composante (aucune division par zéro).

OÙ
---
- **Place** : calcul pur sur la table des villes (une ligne par destination, 35 lignes), appelé
  par ``kayak/etl/spark_etl.py`` (kayak/etl/spark_etl.py:335) après l'agrégation distribuée de la
  météo.
- **Dépendances** : pandas seulement ; aucune lecture de fichier ni de réseau.

COMMENT
-------
- ``calculer_score_meteo`` copie le tableau d'entrée, vérifie les colonnes obligatoires, choisit
  la composante de soleil, calcule trois scores z, leur moyenne, puis le rang décroissant.
- **Preuves directes** : la meilleure ville a le rang 1 (tests/test_score.py:9) ; valeurs
  calculées à la main (tests/test_score_inverses.py:246) ; chaque composante pèse un tiers avec
  son signe (tests/test_score_inverses.py:290).
- **Refus** (tests inverses) : une colonne obligatoire absente est refusée avec le nom de la
  colonne (tests/test_score_inverses.py:50) ; l'entrée n'est jamais modifiée
  (tests/test_score_inverses.py:121) ; sans soleil ni probabilité, le résultat est neutre
  (tests/test_score_inverses.py:181).

POURQUOI
--------
- Le score z est la définition usuelle (valeur moins moyenne, divisée par l'écart-type) ; pandas
  calcule l'écart-type d'échantillon (``ddof=1``), vérifié à la main dans
  tests/test_score_inverses.py:246.
- L'énoncé demande de classer les destinations selon la météo ; le choix de l'équipondération des
  trois composantes est une décision du projet, sans source externe : source : inconnue.
- Usage du résultat dans les cartes : kayak/cartes.py:173.

JOURNALISATION
--------------
- Aucune : fonction pure, sans ``logging``. Les erreurs sont signalées par une exception
  (``ValueError`` pour une colonne obligatoire absente), que l'ETL laisse remonter.
"""

import pandas as pd


def calculer_score_meteo(agregats: pd.DataFrame) -> pd.DataFrame:
    """Calcule le score météo et le rang pour chaque ville.

    Paramètres
    ----------
    agregats : pandas.DataFrame
        Doit contenir les colonnes suivantes :
        - temp_max_moy_c : température maximale moyenne (float)
        - pluie_totale_mm : pluie totale (float)
        - ensoleillement_moy_h : ensoleillement moyen en heures (float, optionnel)
        - proba_pluie_moy_pct : probabilité de pluie moyenne en pourcentage (float, optionnel)
        Si ensoleillement_moy_h est absent ou entièrement nul, on utilise proba_pluie_moy_pct.

    Retourne
    --------
    pandas.DataFrame
        Le DataFrame d'entrée avec deux colonnes supplémentaires :
        - score_meteo : moyenne des scores z (float)
        - rang_meteo : rang de 1 (meilleur) à N (moins bon) (int)
    """
    # Copie pour ne pas modifier l'original
    df = agregats.copy()

    # Vérification de la présence des colonnes obligatoires
    colonnes_requises = ["temp_max_moy_c", "pluie_totale_mm"]
    for col in colonnes_requises:
        if col not in df.columns:
            raise ValueError(f"Colonne manquante : {col}")

    # Déterminer la colonne d'ensoleillement ou de probabilité de pluie
    if "ensoleillement_moy_h" in df.columns and df["ensoleillement_moy_h"].notna().any():
        # Utiliser l'ensoleillement (positif)
        col_soleil = "ensoleillement_moy_h"
        signe_soleil = 1.0
    elif "proba_pluie_moy_pct" in df.columns and df["proba_pluie_moy_pct"].notna().any():
        # Utiliser la probabilité de pluie (négatif)
        col_soleil = "proba_pluie_moy_pct"
        signe_soleil = -1.0
    else:
        # Aucune donnée d'ensoleillement ni de probabilité de pluie : on met un score neutre
        # (toutes les villes ont le même score pour cette composante)
        df["score_meteo"] = 0.0
        df["rang_meteo"] = 1  # Toutes ex aequo
        return df

    # Fonction pour calculer le z-score d'une série, en gérant l'écart-type nul
    def z_score(serie: pd.Series) -> pd.Series:
        std = serie.std()
        if std == 0 or pd.isna(std):
            return pd.Series(0.0, index=serie.index)
        return (serie - serie.mean()) / std

    # Calcul des scores z pour chaque composante
    z_chaleur = z_score(df["temp_max_moy_c"])  # positif
    z_soleil = (
        z_score(df[col_soleil]) * signe_soleil
    )  # positif si ensoleillement, négatif si proba pluie
    z_pluie = -z_score(df["pluie_totale_mm"])  # négatif

    # Score final = moyenne des trois scores z
    df["score_meteo"] = (z_chaleur + z_soleil + z_pluie) / 3.0

    # Rang : 1 pour le score le plus élevé (meilleur)
    df["rang_meteo"] = df["score_meteo"].rank(ascending=False, method="min").astype(int)

    return df
