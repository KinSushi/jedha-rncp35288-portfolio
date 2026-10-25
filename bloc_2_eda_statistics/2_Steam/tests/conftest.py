# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Réglages communs des tests : variables à poser avant l'import de jupyter, session Spark.

- jupyter_core 5.x émet un `DeprecationWarning` à l'import tant que `JUPYTER_PLATFORM_DIRS` n'est
  pas posée : on la pose, comme le demande l'avertissement lui-même, au lieu de le masquer. Ce
  fichier est lu avant les modules de test, donc avant `import nbclient`.
- Matplotlib écrit dans un fichier mémoire (backend Agg) : aucun test n'ouvre de fenêtre.
- La session Spark est créée par module de test et ARRÊTÉE avec sa passerelle Java à la fin du
  module. Pourquoi : mutmut exécute d'abord les tests dans son processus, puis crée un processus
  fils par mutant ; une passerelle Java laissée ouverte serait partagée par ces fils, qui
  s'enverraient des commandes les uns aux autres.
"""

import os
import sys
from collections.abc import Iterator

import matplotlib
import pytest
from pyspark import SparkContext
from pyspark.sql import SparkSession

import steam_analyse

os.environ.setdefault("JUPYTER_PLATFORM_DIRS", "1")
# Les tests créent de petits DataFrames depuis des listes Python : Spark lance alors un processus
# Python de travail, qu'il cherche sous le nom « python3 » dans le PATH. On lui désigne
# l'interpréteur des tests (le notebook, lui, n'en a pas besoin : il lit du JSON).
os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
matplotlib.use("Agg")


@pytest.fixture(scope="module")
def spark() -> Iterator[SparkSession]:
    """Session Spark locale créée exactement comme dans le notebook (`obtenir_spark(False)`).

    Le maître par défaut (`local[*]`, un fil par cœur) est donc celui que les tests vérifient : un
    maître réduit ici laisserait sans contrôle la valeur qu'utilise réellement le notebook.
    """
    session = steam_analyse.obtenir_spark(False)
    session.sparkContext.setLogLevel("ERROR")
    session.conf.set("spark.sql.legacy.timeParserPolicy", "CORRECTED")
    # Noms de colonnes sensibles à la casse : par défaut, Spark lirait « DATA.APPID » comme
    # « data.appid ». Les tests exigent l'écriture exacte des champs du fichier, pour qu'une
    # faute de casse dans le module ne passe pas inaperçue (elle échouerait sur un Spark réglé
    # autrement, et elle produirait une colonne au mauvais nom).
    session.conf.set("spark.sql.caseSensitive", "true")
    yield session
    session.stop()
    passerelle = SparkContext._gateway  # noqa: SLF001 - seul moyen d'arrêter la JVM de pyspark
    if passerelle is not None:
        passerelle.shutdown()
    SparkContext._gateway = None  # noqa: SLF001
    SparkContext._jvm = None  # noqa: SLF001
