# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Fonctions du notebook Steam (PySpark), extraites pour être testées une à une.

CHOIX
-----
- Retenu : des fonctions qui prennent un DataFrame Spark (ou pandas) et en rendent un autre. Le
  notebook garde le raisonnement (cellules Markdown), les agrégations de l'analyse et les
  affichages ; le nettoyage (aplatissement, langues, dates, propriétaires, éditeurs) et les outils
  de l'expérience de parallélisme vivent ici et sont contrôlés par `tests/test_steam_analyse.py`
  sur de petits enregistrements qui reproduisent les défauts réels du fichier.
- Retenu : une entrée interdite est refusée par une exception qui dit ce qui manque (DataFrame
  vide, totaux de votes différents entre Spark et pandas, tag demandé deux fois, quantile z nul
  ou négatif, chemin de machine resté dans un plan).
  Jamais de valeur inventée en silence.
- Écarté : tester le notebook seul. Il met de deux à un peu plus de trois minutes à s'exécuter
  (135,5 s : réexécution enregistrée du 17/10/2026 ; 133 à 188 s : huit rejeux de
  `tests/test_notebook_steam.py`, marqué `notebook`, du 17/10 au 19/10 au matin), 512 s sur une
  machine saturée (19/10, midi), et un échec n'y dirait pas quelle règle de nettoyage est en cause.
- Écarté : appeler `display()` depuis ce module. Sur Databricks, `display` est une fonction du
  notebook, invisible d'un module importé : le notebook la passe en paramètre (`afficher`).
- Écarté : `toPandas()` dans ce module. Spark 3.5 y importe `distutils`, retiré de Python 3.12 et
  fourni seulement par une copie obsolète de setuptools : les tests, lancés avec
  `-W error::DeprecationWarning`, échoueraient sur cet avertissement. `vers_pandas` collecte les
  lignes à la place (mesuré le 17/10 : `LooseVersion` lève `DeprecationWarning`). Le notebook,
  lui, garde `toPandas()` : `setuptools` est donc épinglé dans `requirements.txt`.
- Écarté : une session Spark créée à l'import du module. Elle serait lancée par les tests et
  par n'importe quel outil qui lit le fichier ; `obtenir_spark` la crée à la demande.

OÙ
---
- Place : à côté du notebook, dans `2_Steam/`. Appelant : `Steam_Bloc2_CDSD_PySpark.ipynb`
  (cellule 2 pour l'import ; les fonctions servent aux sections 1, 2, 3 et 7). Aucun autre
  appelant.
- Dépendances : pyspark 3.5.9 (session `local[*]`, Java fourni par `jdk4py`), pandas, matplotlib ;
  versions épinglées dans `requirements.txt`. Sur Databricks, la session et `display()` existent
  déjà ; ce fichier doit être importé avec le notebook, dans le même dossier de l'espace de
  travail.

COMMENT
-------
1. Session et chemins : `sur_databricks`, `chemin_donnees`, `obtenir_spark`.
2. Nettoyage, une fonction par défaut réel du fichier : `espaces_normalises` et `texte_ou_nul`
   (texte vide ou à tabulations), `aplatir_jeux` (prix en cents vers dollars, champs de `data`),
   `liste_langues` et `nombre_de_langues` (balises de mise en forme), `borne_proprietaires`
   (tranches « 20,000 .. 50,000 »), `part_positive` et `score_wilson` (classement honnête des
   jeux notés, NULL sans avis),
   `ajouter_champs_derives` (les assemble, âges au-delà de 21 ans mis à NULL),
   `nom_editeur_normalise` et `editeurs_par_jeu` (un éditeur, plusieurs écritures).
3. Analyse : `wilson` (intervalle de confiance d'une proportion), `montrer_graphique` (graphique
   local ou `display` sur Databricks), `vers_pandas` (petit résultat agrégé vers pandas, sans
   `toPandas`), `premiere_ligne` et `champs_struct` (lecture sûre).
4. Parallélisme : `votes_par_tag` (déplie les 441 tags ; `refuser_tags_en_double` écarte une
   liste qui compterait deux fois le même tag), `chronometrer`, `reglages_partitions`,
   `tableau_acceleration`, `comparer_votes` (Spark et pandas doivent donner les mêmes totaux),
   `plan_sans_chemin_de_machine`.
5. Tests qui le prouvent : `tests/test_steam_analyse.py` (cas calculés à la main sur de petits
   DataFrames, refus des entrées interdites) et `tests/test_notebook_steam.py` (notebook entier).

POURQUOI
--------
- Borne de Wilson : intervalle de confiance d'une proportion, qui pénalise les petits effectifs
  (un jeu à 100 % sur 50 avis ne passe pas devant un jeu à 98 % sur 100 000). Source : inconnue.
  Aucun manuel de statistique n'est joint au dossier ; la formule est recalculée à la main dans
  `tests/test_steam_analyse.py`.
- `local[*]` et 8 partitions de brassage au lieu de 200 : réglages décrits dans le notebook
  (section 1) pour un volume de quelques dizaines de mégaoctets ; contrôlés par
  `tests/test_steam_analyse.py`.
- Les tranches de propriétaires viennent de SteamSpy (format « 20,000 .. 50,000 ») ; le fichier ne
  contient aucun chiffre de ventes : `donnees/LIRE_DONNEES.md` décrit le fichier fourni.
- Les écritures d'un même éditeur (« Ubisoft », « Ubisoft Entertainment », « Ubisoft® »,
  « Ubisoft - San Francisco ») sont celles relevées dans le fichier : le notebook les cite en
  section 2 et le test de `editeurs_par_jeu` les reproduit.

JOURNALISATION
--------------
Aucune : fonctions sans entrée-sortie propre (hors la session Spark) et sans module `logging`.
Une entrée inexploitable lève une exception dont le message dit ce qui manque ; le notebook
affiche les comptes (âges aberrants, jeux retenus, éditeurs regroupés). Le niveau de journal de
Spark est réglé par le notebook (`setLogLevel("ERROR")`).
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
from pyspark.sql import Column, DataFrame, Row, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import DataType, DoubleType, IntegerType, StructField, StructType

#: Quantile de la loi normale pour un niveau de confiance de 95 %.
Z_95 = 1.96

#: Types de graphique acceptés par `montrer_graphique`.
TYPES_GRAPHIQUE = ("barh", "bar", "line")

#: Couleur unique des graphiques locaux : une seule couleur, sans légende, pour un public non
#: spécialiste (le titre porte le message).
COULEUR = "steelblue"

VARIABLE_DATABRICKS = "DATABRICKS_RUNTIME_VERSION"
#: Variable d'environnement qui donne, sur Databricks, l'adresse S3 du fichier JSON (cette
#: adresse n'est pas publiée dans ce dépôt : voir `donnees/LIRE_DONNEES.md`).
VARIABLE_CHEMIN_S3 = "STEAM_CHEMIN_S3"
CHEMIN_LOCAL = "donnees/steam_game_output.json"

#: Séparateur des éditeurs multiples : la virgule, sauf devant une forme sociale
#: (« Foo, Inc. » reste un seul éditeur ; « Cyan Worlds Inc, Ubisoft » en donne deux).
SEPARATEUR_EDITEURS = r",(?!\s*(?i:inc|llc|ltd|co|corp|s\.a|gmbh)\b)\s*"
#: Forme sociale ou mot « Entertainment » en fin de nom (insensible à la casse).
FORME_SOCIALE = (
    r"(?i)[\s,]+(inc\.?|llc\.?|ltd\.?|co\.?,?\s*ltd\.?|gmbh|s\.a\.?|corp\.?|corporation"
    r"|entertainment)$"
)


# ------------------------------------------------------------------------------------------
# Session et chemins
# ------------------------------------------------------------------------------------------


def sur_databricks(environnement: Mapping[str, str] | None = None) -> bool:
    """Vrai sur Databricks, où cette variable d'environnement existe toujours.

    C'est le moyen fiable de savoir où l'on s'exécute. `environnement` vaut `os.environ` par défaut.
    """
    env = os.environ if environnement is None else environnement
    return VARIABLE_DATABRICKS in env


def chemin_donnees(databricks: bool, environnement: Mapping[str, str] | None = None) -> str:
    """Chemin du JSON : le dossier `donnees/` en local ; sur Databricks, l'adresse S3 lue dans
    la variable d'environnement `STEAM_CHEMIN_S3`.

    `environnement` vaut `os.environ` par défaut. Sur Databricks, une variable absente ou vide
    lève une erreur : mieux vaut s'arrêter que lire un autre fichier.
    """
    if not databricks:
        return CHEMIN_LOCAL
    env = os.environ if environnement is None else environnement
    chemin = env.get(VARIABLE_CHEMIN_S3, "").strip()
    if not chemin:
        raise ValueError(
            f"sur Databricks, définir la variable d'environnement {VARIABLE_CHEMIN_S3} "
            "(adresse S3 du fichier JSON)"
        )
    return chemin


def obtenir_spark(databricks: bool, maitre: str = "local[*]") -> SparkSession:
    """Renvoie la session Spark : celle de Databricks, ou une session locale.

    En local : Java est fourni par jdk4py (pas d'installation système) ; l'interface web de Spark
    est désactivée (inutile dans un notebook) ; 8 partitions de brassage (« shuffle ») au lieu de
    200 par défaut, valeur adaptée à un volume de quelques dizaines de mégaoctets. `maitre` vaut
    `local[*]` (un fil par cœur logique) : c'est la valeur du notebook, et celle des tests.
    """
    if databricks:
        return SparkSession.builder.getOrCreate()
    import jdk4py

    os.environ["JAVA_HOME"] = str(jdk4py.JAVA_HOME)  # os.environ n'accepte que des chaînes
    return (
        SparkSession.builder.master(maitre)
        .appName("Steam_Bloc2_CDSD")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "8")
        .getOrCreate()
    )


def champs_struct(structure: StructType, *chemin: str) -> list[StructField]:
    """Champs de la sous-structure désignée par `chemin` (« data », puis « tags »...).

    Chaque nom du chemin doit désigner une structure : sinon `TypeError`, qui nomme le champ
    fautif et son type.
    """
    courant: DataType = structure
    for nom in chemin:
        if not isinstance(courant, StructType):
            type_texte = courant.simpleString()
            raise TypeError(f"avant « {nom} », ce n'est pas une structure : {type_texte}")
        courant = courant[nom].dataType
    if not isinstance(courant, StructType):
        type_texte = courant.simpleString()
        raise TypeError(f"« {'.'.join(chemin)} » n'est pas une structure : {type_texte}")
    return list(courant.fields)


def premiere_ligne(donnees: DataFrame) -> Row:
    """Première ligne d'un DataFrame ; refuse un DataFrame vide (`first()` rendrait None)."""
    ligne = donnees.first()
    if ligne is None:
        raise ValueError("DataFrame vide : aucune première ligne")
    return ligne


# ------------------------------------------------------------------------------------------
# Nettoyage
# ------------------------------------------------------------------------------------------


def espaces_normalises(colonne: Column) -> Column:
    """Tabulations, espaces insécables et suites d'espaces → une espace ; bords retirés.

    F.trim seul ne retire que l'espace ordinaire : « Ubisoft Entertainment » suivi de quatre
    tabulations, présent dans les données, resterait un éditeur à part.
    """
    return F.trim(F.regexp_replace(colonne, r"[\s\p{Z}]+", " "))


def texte_ou_nul(colonne: str) -> Column:
    """Texte nettoyé (espaces normalisés) ; NULL si la chaîne est vide."""
    propre = espaces_normalises(F.col(colonne))
    return F.when(propre == "", None).otherwise(propre)


def aplatir_jeux(brut: DataFrame) -> DataFrame:
    """Extrait les champs utiles de l'objet imbriqué `data` et convertit les types.

    Les prix arrivent en cents de dollar US (format SteamSpy) : ils sont divisés par 100. Un
    éditeur ou un développeur vide devient NULL (sinon « vide » serait compté comme un éditeur).
    """
    return brut.select(
        F.col("data.appid").alias("appid"),
        F.col("data.name").alias("nom"),
        texte_ou_nul("data.developer").alias("developpeur"),
        texte_ou_nul("data.publisher").alias("editeur"),
        F.col("data.genre").alias("genres_texte"),
        F.col("data.languages").alias("langues_texte"),
        F.col("data.categories").alias("categories"),
        F.col("data.platforms.windows").alias("windows"),
        F.col("data.platforms.mac").alias("mac"),
        F.col("data.platforms.linux").alias("linux"),
        F.col("data.positive").alias("avis_positifs"),
        F.col("data.negative").alias("avis_negatifs"),
        F.col("data.ccu").alias("joueurs_simultanes"),
        (F.col("data.price").cast(DoubleType()) / 100).alias("prix_usd"),
        (F.col("data.initialprice").cast(DoubleType()) / 100).alias("prix_initial_usd"),
        F.col("data.discount").cast(IntegerType()).alias("remise_pct"),
        F.col("data.required_age").cast(IntegerType()).alias("age_requis_brut"),
        F.col("data.release_date").alias("date_texte"),
        F.col("data.owners").alias("proprietaires_texte"),
        F.col("data.type").alias("type"),
    )


def liste_langues(colonne: str) -> Column:
    """Liste des langues d'un texte « French, German[b]*[/b], English ».

    On retire les balises de mise en forme ([b], [/b], *) et les retours à la ligne trouvés dans
    quatre fiches, puis on découpe la liste et on retire les espaces de chaque nom ; les noms
    vides sont écartés.
    """
    return F.filter(
        F.transform(
            F.split(F.regexp_replace(colonne, r"\[/?b\]|\*|[\r\n]+", ""), ","),
            lambda langue: F.trim(langue),
        ),
        lambda langue: langue != "",
    )


def nombre_de_langues(colonne: str) -> Column:
    """Nombre de langues d'une colonne de listes ; une liste vide ou absente compte pour 0.

    `F.size` d'une liste absente vaut -1 dans Spark : on la remplace explicitement par 0 plutôt
    que de compter -1 langue.
    """
    return F.when(F.col(colonne).isNull(), F.lit(0)).otherwise(F.size(colonne))


def borne_proprietaires(position: int) -> Column:
    """Borne basse (1) ou haute (2) de « 20,000 .. 50,000 », sans les virgules.

    Les virgules doivent être retirées AVANT la conversion : « 1,000,000 » converti tel quel
    donnerait NULL. Toute autre position est refusée (`ValueError`) : la tranche n'a que deux
    bornes.
    """
    if position not in (1, 2):
        raise ValueError(f"position 1 (basse) ou 2 (haute) attendue, reçu {position}")
    nombre = F.regexp_extract("proprietaires_texte", r"([\d,]+)\s*\.\.\s*([\d,]+)", position)
    return F.regexp_replace(nombre, ",", "").cast(DoubleType())


def part_positive(positifs: Column, total: Column) -> Column:
    """Part d'avis positifs ; NULL quand il n'y a aucun avis (jamais une division par zéro).

    Le garde `total > 0` compte aussi hors de Spark en mode ANSI (Databricks SQL, par exemple),
    où une division par zéro lève une erreur au lieu de rendre NULL.
    """
    return F.when(total > 0, positifs / total)


def score_wilson(total: Column, part: Column, z: float = Z_95) -> Column:
    """Borne basse de l'intervalle de Wilson (95 %) de la part positive `part` sur `total` avis.

    Pourquoi : un jeu à 100 % d'avis positifs sur 50 avis n'est pas « meilleur » qu'un jeu à 98 %
    sur 100 000 avis ; la borne de Wilson pénalise les petits effectifs et sert à classer les jeux
    de façon honnête. NULL quand il n'y a aucun avis (garde `total > 0`, valable aussi en mode
    ANSI). Un quantile `z` nul ou négatif est refusé (`ValueError`), comme dans `wilson` : la
    « borne basse » deviendrait la part brute, ou passerait au-dessus d'elle.
    """
    if z <= 0:
        raise ValueError(f"quantile z strictement positif attendu, reçu {z}")
    n, p = total, part
    return F.when(
        n > 0,
        (p + z**2 / (2 * n) - z * F.sqrt(p * (1 - p) / n + z**2 / (4 * n * n))) / (1 + z**2 / n),
    )


def ajouter_champs_derives(jeux: DataFrame) -> DataFrame:
    """Ajoute langues, date, propriétaires, avis, score de Wilson et âge requis à la table aplatie.

    - date : « yyyy/M/d » accepte un ou deux chiffres pour le mois et le jour ; « yyyy/M » rattrape
      les dates sans jour (fixées au 1er du mois) ;
    - âge requis : les valeurs au-delà de 21 ans (180, 35 : vues dans l'audit) sont des erreurs
      de saisie, aucune classification n'allant au-delà de 18 ou 21 ans ; elles deviennent NULL.
      La colonne brute `age_requis_brut` est conservée pour que l'appelant compte les aberrants.
    """
    jeux = jeux.withColumn("langues", liste_langues("langues_texte")).withColumn(
        "nb_langues", nombre_de_langues("langues")
    )
    jeux = jeux.withColumn(
        "date_sortie",
        F.coalesce(F.to_date("date_texte", "yyyy/M/d"), F.to_date("date_texte", "yyyy/M")),
    )
    jeux = (
        jeux.withColumn("annee", F.year("date_sortie"))
        .withColumn("mois", F.month("date_sortie"))
        .withColumn("proprietaires_min", borne_proprietaires(1))
        .withColumn("proprietaires_max", borne_proprietaires(2))
        .withColumn(
            "proprietaires_milieu", (F.col("proprietaires_min") + F.col("proprietaires_max")) / 2
        )
        .withColumn("total_avis", F.col("avis_positifs") + F.col("avis_negatifs"))
        .withColumn("part_positive", part_positive(F.col("avis_positifs"), F.col("total_avis")))
    )
    jeux = jeux.withColumn(
        "score_wilson", score_wilson(F.col("total_avis"), F.col("part_positive"))
    )
    return jeux.withColumn(
        "age_requis",
        F.when(F.col("age_requis_brut").between(0, 21), F.col("age_requis_brut")),
    )


def nom_editeur_normalise(colonne: Column) -> Column:
    """Nom d'éditeur sans symboles ®™©, sans studio après un tiret, sans forme sociale.

    - « Ubisoft® » → « Ubisoft » ; « Ubisoft - San Francisco » → « Ubisoft » ;
    - « Ubisoft Entertainment », « Strategy First Inc. » perdent leur suffixe ; deux passages,
      car « X Entertainment Inc. » porte deux suffixes.
    """
    nom = F.regexp_replace(espaces_normalises(colonne), "[®™©]", "")
    nom = F.regexp_replace(nom, r"\s+-\s+.*$", "")
    for _ in range(2):
        nom = F.trim(F.regexp_replace(nom, FORME_SOCIALE, ""))
    return nom


def editeurs_par_jeu(jeux: DataFrame) -> DataFrame:
    """Table (jeu, éditeur) aux noms normalisés : `appid`, `cle`, `editeur_brut`, `editeur`.

    Trois étapes, chacune motivée par un cas réel : découper les éditeurs multiples sur la
    virgule (sauf devant une forme sociale) ; normaliser le nom (`nom_editeur_normalise`) ; comparer
    sans tenir compte de la casse (« 8floor » et « 8Floor » sont le même éditeur). Le nom affiché
    pour un groupe est son écriture la plus fréquente (égalités : ordre alphabétique, pour un
    résultat reproductible). Un nom sans lettre ni chiffre (« - ») n'est pas un éditeur.
    """
    editeurs_bruts = (
        jeux.filter(F.col("editeur").isNotNull())
        .select("appid", F.explode(F.split("editeur", SEPARATEUR_EDITEURS)).alias("editeur_brut"))
        .withColumn("editeur_nom", nom_editeur_normalise(F.col("editeur_brut")))
        .withColumn("cle", F.lower("editeur_nom"))
        .filter(F.col("cle").rlike(r"[\p{L}\p{N}]"))
    )
    fenetre_nom = Window.partitionBy("cle").orderBy(F.desc("n"), "editeur_nom")
    noms_affiches = (
        editeurs_bruts.groupBy("cle", "editeur_nom")
        .agg(F.count("*").alias("n"))
        .withColumn("rang", F.row_number().over(fenetre_nom))
        .filter(F.col("rang") == 1)
        .select("cle", F.col("editeur_nom").alias("editeur"))
    )
    return editeurs_bruts.select("appid", "cle", "editeur_brut").join(noms_affiches, "cle").cache()


# ------------------------------------------------------------------------------------------
# Analyse
# ------------------------------------------------------------------------------------------


def wilson(
    succes: pd.Series[Any], effectif: pd.Series[Any], z: float = Z_95
) -> tuple[pd.Series[float], pd.Series[float]]:
    """Intervalle de confiance de Wilson (bornes basse et haute) d'une proportion.

    Refuse (`ValueError`) un quantile `z` nul ou négatif (bornes confondues ou inversées), une
    valeur manquante (la comparaison à zéro la laisserait passer et rendrait une borne NaN sans
    prévenir), un effectif nul ou négatif, et un nombre de succès négatif ou supérieur à
    l'effectif : ce ne seraient pas des proportions.
    """
    if z <= 0:
        raise ValueError(f"quantile z strictement positif attendu, reçu {z}")
    if succes.isna().any() or effectif.isna().any():
        raise ValueError("nombre de succès ou effectif manquant : la proportion n'existe pas")
    if (effectif <= 0).any():
        raise ValueError("effectif nul ou négatif : la proportion n'existe pas")
    if (succes < 0).any() or (succes > effectif).any():
        raise ValueError("nombre de succès négatif ou supérieur à l'effectif")
    p_chap = succes / effectif
    centre = p_chap + z**2 / (2 * effectif)
    rayon = z * ((p_chap * (1 - p_chap) / effectif + z**2 / (4 * effectif**2)) ** 0.5)
    return (centre - rayon) / (1 + z**2 / effectif), (centre + rayon) / (1 + z**2 / effectif)


def vers_pandas(donnees: DataFrame) -> pd.DataFrame:
    """Petit DataFrame Spark (résultat d'une agrégation) converti en pandas, ligne par ligne.

    `toPandas()` de Spark 3.5 importe `distutils`, retiré de Python 3.12 : ce module n'existe plus
    que par une copie de compatibilité de setuptools, déclarée obsolète (`DeprecationWarning` à
    chaque conversion). Pour quelques dizaines de lignes déjà agrégées, collecter les lignes et
    les donner à pandas suffit, sans cette dépendance. Noms et ordre des colonnes sont gardés,
    même quand le DataFrame est vide.
    """
    return pd.DataFrame.from_records(donnees.collect(), columns=donnees.columns)


def montrer_graphique(
    df_spark: DataFrame,
    titre: str,
    x: str,
    y: str,
    type_graphique: str = "barh",
    etiquette_x: str = "",
    etiquette_y: str = "",
    *,
    databricks: bool,
    afficher: Callable[[DataFrame], Any] | None = None,
) -> None:
    """Affiche un petit DataFrame Spark agrégé sous forme de graphique.

    Sur Databricks : `afficher` (le `display()` du notebook, qui ouvre l'outil de visualisation
    demandé par l'énoncé). En local : conversion en pandas (le résultat d'une agrégation est
    petit) et matplotlib. Un type de graphique inconnu est refusé (`ValueError`) au lieu de
    devenir une courbe sans prévenir.
    """
    if type_graphique not in TYPES_GRAPHIQUE:
        raise ValueError(f"type de graphique inconnu : {type_graphique} ({TYPES_GRAPHIQUE})")
    if databricks:
        if afficher is None:
            raise ValueError("sur Databricks, la fonction d'affichage display() est requise")
        afficher(df_spark)
        return
    donnees = vers_pandas(df_spark)
    _, ax = plt.subplots(figsize=(10, 6))
    if type_graphique == "barh":
        donnees.iloc[::-1].plot.barh(x=x, y=y, ax=ax, legend=False, color=COULEUR)
    elif type_graphique == "bar":
        donnees.plot.bar(x=x, y=y, ax=ax, legend=False, color=COULEUR)
    else:  # "line", seul type restant après la vérification ci-dessus
        donnees.plot.line(x=x, y=y, ax=ax, legend=False, marker="o", color=COULEUR)
    ax.set_title(titre)
    # Étiquettes telles que demandées, même vides : jamais le nom technique d'une colonne
    # (« nb_jeux ») sur un graphique destiné à un public non spécialiste.
    ax.set_xlabel(etiquette_x)
    ax.set_ylabel(etiquette_y)
    plt.tight_layout()
    plt.show()


# ------------------------------------------------------------------------------------------
# Efficacité du traitement parallélisé
# ------------------------------------------------------------------------------------------


def refuser_tags_en_double(noms_tags: Sequence[str]) -> None:
    """`ValueError` si un nom de tag figure plusieurs fois dans la liste.

    Un tag demandé deux fois serait déplié deux fois : ses votes et son nombre de jeux
    doubleraient sans prévenir, et la comparaison avec pandas porterait sur des totaux faux.
    """
    doublons = sorted(nom for nom, fois in Counter(noms_tags).items() if fois > 1)
    if doublons:
        raise ValueError(f"tag(s) demandé(s) plusieurs fois : {', '.join(doublons)}")


def votes_par_tag(source: DataFrame, noms_tags: Sequence[str]) -> DataFrame:
    """Déplie la structure des tags en lignes (jeu, tag, votes) et somme les votes par tag.

    `source` est `brut.select("data.tags")`, qui produit une colonne nommée « tags ». Un tag sans
    vote (NULL) est écarté avant la somme. Colonnes rendues : `tag`, `votes`, `nb_jeux`. Une liste
    de noms vide est refusée (`ValueError`) : le calcul rendrait un résultat vide sans prévenir ;
    un nom en double aussi (`refuser_tags_en_double`).
    """
    if not noms_tags:
        raise ValueError("aucun nom de tag à sommer")
    refuser_tags_en_double(noms_tags)
    paires = F.array(
        *[
            F.struct(F.lit(nom).alias("tag"), F.col("tags").getField(nom).alias("votes"))
            for nom in noms_tags
        ]
    )
    return (
        source.select(F.explode(paires).alias("paire"))
        .select("paire.tag", "paire.votes")
        .filter(F.col("votes").isNotNull())
        .groupBy("tag")
        .agg(F.sum("votes").alias("votes"), F.count("*").alias("nb_jeux"))
    )


def chronometrer(
    brut: DataFrame, noms_tags: Sequence[str], partitions: int, essais: int = 5
) -> list[float]:
    """Durées (s) de `essais` exécutions du calcul complet, après un essai de chauffe.

    La source est mise en cache et matérialisée avant le chronomètre : on ne mesure pas la lecture
    du fichier. L'essai de chauffe (chargement des classes Java) n'est pas compté. Moins d'une
    partition, moins d'un essai, aucun nom de tag ou un nom en double est refusé (`ValueError`),
    avant tout calcul Spark : sans ce contrôle, une liste vide n'échouerait qu'après la mise en
    cache, qui resterait occupée.
    """
    if partitions < 1:
        raise ValueError(f"au moins une partition attendue, reçu {partitions}")
    if essais < 1:
        raise ValueError(f"au moins un essai attendu, reçu {essais}")
    if not noms_tags:
        raise ValueError("aucun nom de tag à chronométrer")
    refuser_tags_en_double(noms_tags)
    source = brut.select("data.tags").repartition(partitions).cache()
    source.count()  # matérialise le cache : on ne mesure pas la lecture du fichier
    votes_par_tag(source, noms_tags).collect()  # essai de chauffe, non compté
    durees: list[float] = []
    for _ in range(essais):
        debut = time.perf_counter()
        votes_par_tag(source, noms_tags).collect()
        durees.append(time.perf_counter() - debut)
    source.unpersist()
    return durees


def reglages_partitions(coeurs: int) -> list[int]:
    """Partitions essayées : 1, 2, 4, 8, 16 et le nombre de cœurs, sans dépasser celui-ci.

    Une machine a au moins un cœur : `ValueError` sinon.
    """
    if coeurs < 1:
        raise ValueError(f"au moins un cœur attendu, reçu {coeurs}")
    return sorted(reglage for reglage in {1, 2, 4, 8, 16, coeurs} if reglage <= coeurs)


def tableau_acceleration(essais_par_reglage: Mapping[int, Sequence[float]]) -> pd.DataFrame:
    """Un essai par colonne, la médiane, et l'accélération par rapport à une seule partition.

    L'accélération d'un réglage est la médiane à 1 partition divisée par sa propre médiane ; le
    réglage à 1 partition est donc obligatoire (sinon `KeyError`). Aucune mesure, ou une durée
    nulle, négative ou manquante, est refusée (`ValueError`) : une durée nulle donnerait une
    accélération infinie.
    """
    if not essais_par_reglage:
        raise ValueError("aucun réglage mesuré")
    mesures = pd.DataFrame(dict(essais_par_reglage)).T
    if not (mesures > 0).all().all():
        raise ValueError("durée nulle, négative ou manquante dans les essais")
    mesures.columns = pd.Index([f"essai {i + 1} (s)" for i in range(mesures.shape[1])])
    mesures["médiane (s)"] = mesures.median(axis=1)
    medianes = mesures["médiane (s)"]
    mesures["accélération"] = medianes.loc[1] / medianes
    mesures.index.name = "partitions"
    return mesures


def comparer_votes(
    resultat_spark: pd.Series[Any], votes_pandas: pd.Series[Any], noms_tags: Sequence[str]
) -> pd.DataFrame:
    """Aligne les totaux de votes des deux moteurs et refuse la moindre différence.

    Les tags absents d'un côté comptent 0. Il faut autant de lignes que de tags attendus, les mêmes
    noms de tags que ceux attendus (deux moteurs d'accord sur d'autres tags ne prouvent rien) et un
    écart maximal nul ; sinon `ValueError` : deux moteurs qui ne rendent pas les mêmes totaux ne
    se comparent pas en temps.
    """
    comparaison = pd.DataFrame({"spark": resultat_spark, "pandas": votes_pandas}).fillna(0)
    ecarts = (comparaison["spark"] - comparaison["pandas"]).abs()
    if len(comparaison) != len(noms_tags):
        raise ValueError(f"{len(comparaison)} tags comparés au lieu de {len(noms_tags)} attendus")
    if set(comparaison.index) != set(noms_tags):
        raise ValueError("tags comparés différents des tags attendus")
    if ecarts.max() != 0:
        raise ValueError(f"totaux de votes différents entre Spark et pandas (écart {ecarts.max()})")
    return comparaison


def plan_sans_chemin_de_machine(plan: str) -> str:
    """Remplace le chemin absolu du fichier, dans le plan d'exécution, par le chemin du projet.

    Le plan cite le chemin absolu du fichier sur la machine qui l'exécute, parfois tronqué par
    Spark (« ... ») : on remplace tout, de « file: » jusqu'au nom du fichier ou jusqu'à la
    troncature (le chemin peut contenir des espaces). Si un chemin de machine subsiste malgré tout,
    `ValueError` : aucun chemin de machine ne doit rester dans les sorties.
    """
    propre = re.sub(
        r"file:[^\]\n]*?(?:steam_game_output\.json|\.\.\.)",
        CHEMIN_LOCAL,
        plan,
    )
    if "file:" in propre or "Users" in propre:
        raise ValueError("un chemin de machine subsiste dans le plan d'exécution")
    return propre
