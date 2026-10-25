# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
r"""Réglages communs des tests : arrêt de Spark après chaque module, espion des ouvertures.

1. Spark et sa machine virtuelle Java arrêtés à la fin de chaque module.

Pourquoi : sans arrêt, la session Spark et le processus Java démarrés par un module de test
survivent jusqu'à la fin de la suite. C'est sans effet pour un lancement ordinaire de pytest, mais
l'outil de mutation (mutmut, sous Linux) lance d'abord toute la suite dans son propre processus,
puis se duplique (``fork``) pour tester chaque mutant : chaque copie hériterait de la MÊME
connexion vers le même processus Java, et plusieurs copies écriraient en même temps sur cette
connexion. Un test pourrait alors échouer pour une raison qui n'a rien à voir avec le mutant, et
la mutation compterait à tort ce mutant comme « tué ». Avec cet arrêt, chaque copie démarre son
propre processus Java, avec son propre environnement (dont la variable qui désigne le mutant).

Écarté : une seule session pour toute la suite (plus rapide de quelques secondes), pour la raison
ci-dessus.

2. ``espion_open`` : relève les arguments de chaque ``open()`` d'un module (fichier, mode,
encodage, fins de ligne) et laisse l'ouverture se faire. Pourquoi : sous Linux, oublier
``encoding="utf-8"`` ou ``newline="\n"`` ne change rien au fichier écrit (l'encodage par
défaut y est UTF-8, la fin de ligne « \n ») ; sous Windows, le même oubli écrirait en cp1252
et en « \r\n ». Le projet tourne sous Windows : ces réglages se vérifient donc par les
arguments.
"""

import builtins
import codecs
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture(scope="module", autouse=True)
def arreter_spark_a_la_fin_du_module() -> Iterator[None]:
    """Après les tests du module : arrête la session Spark, puis la passerelle et le processus Java.

    Rien à faire si PySpark n'a pas été importé, ou si aucune passerelle n'a été lancée (tests
    qui utilisent de faux objets Spark).
    """
    yield
    if "pyspark" not in sys.modules:
        return
    mutant = os.environ.get("MUTANT_UNDER_TEST")
    if mutant and mutant != "stats":
        # Copie de mutmut qui teste UN mutant : elle ne se duplique plus, et son processus Java
        # s'arrête avec elle (entrée standard fermée). Garder Spark d'un module à l'autre évite
        # de relancer Java pour chaque module de test de ce mutant.
        return
    from pyspark import SparkContext
    from pyspark.sql import SparkSession

    session = SparkSession.getActiveSession()
    if session is not None:
        session.stop()
    # PySpark garde la passerelle dans un attribut de classe et ne l'expose par aucune API
    # publique : on la lit et on la remet à None, pour que le prochain module en démarre une neuve.
    passerelle = SparkContext._gateway  # noqa: SLF001
    if passerelle is None:
        return
    processus = getattr(passerelle, "proc", None)  # processus Java lancé par PySpark
    passerelle.shutdown()
    SparkContext._gateway = None  # noqa: SLF001
    SparkContext._jvm = None  # noqa: SLF001
    if processus is not None and processus.stdin is not None:
        # Le processus Java de PySpark s'arrête quand son entrée standard se ferme (Linux, macOS).
        # Sous Windows, PySpark l'arrête lui-même à la fin du programme (taskkill).
        processus.stdin.close()
        if os.name != "nt":
            processus.wait(timeout=120)


@dataclass(frozen=True)
class Ouverture:
    """Arguments d'un appel à ``open()`` relevés par l'espion."""

    fichier: str
    mode: str
    encoding: str | None
    newline: str | None

    @property
    def en_utf8(self) -> bool:
        """Vrai si l'encodage demandé est UTF-8, quelle que soit son écriture (utf-8, UTF8...)."""
        return self.encoding is not None and codecs.lookup(self.encoding).name == "utf-8"


@pytest.fixture
def espion_open(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[ModuleType], list[Ouverture]]:
    """Installe un espion d'``open()`` dans un module ; rend la liste (remplie au fil des appels).

    Le nom ``open`` est cherché dans le module avant les fonctions intégrées : le poser comme
    attribut du module suffit, et ``monkeypatch`` le retire à la fin du test.
    """

    def installer(module: ModuleType) -> list[Ouverture]:
        appels: list[Ouverture] = []
        vrai_open = builtins.open

        def open_espion(
            fichier: Any,
            mode: str = "r",
            buffering: int = -1,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
            closefd: bool = True,
            opener: Any = None,
        ) -> Any:
            appels.append(Ouverture(str(fichier), mode, encoding, newline))
            return vrai_open(fichier, mode, buffering, encoding, errors, newline, closefd, opener)

        monkeypatch.setattr(module, "open", open_espion, raising=False)
        return appels

    return installer
