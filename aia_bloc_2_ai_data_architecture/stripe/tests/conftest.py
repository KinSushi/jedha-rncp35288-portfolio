# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Fixtures communes : une mini-démonstration (OLTP + entrepôt) rapide et isolée.

Pourquoi : les tests doivent tourner en quelques secondes et sans dépendre de la démonstration
complète (48 000 paiements). On rejoue donc les MÊMES fonctions que `pipeline.py`, avec quelques
centaines de paiements, dans un dossier temporaire propre à chaque test (aucun état partagé).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import mongomock
import pytest
from aide_demo import espionner_ouvertures_utf8
from sqlalchemy import text
from sqlalchemy.engine import Engine

from stripe_demo import olap, oltp, validation_mongo

DEBUT = datetime(2026, 8, 1, tzinfo=UTC)
JOURS = 4
PAR_JOUR = 120


def maintenant_utc_naif() -> datetime:
    """Instant présent en UTC, sans fuseau : exactement ce que renvoie `datetime.utcnow()`."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def horloge_mongomock_sans_utcnow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remplace l'horloge de mongomock par un équivalent non déprécié.

    Pourquoi : mongomock (4.3.0, dernière version publiée) lit l'heure par `datetime.utcnow()`,
    déprécié depuis Python 3.12, dès qu'une collection porte un index à durée de vie (TTL, ici
    90 jours sur `evenements`). La bibliothèque documente son point d'extension :
    `mongomock.utcnow` est faite pour être remplacée en test (helpers.py, docstring de `utcnow`).
    On la remplace donc, au lieu de filtrer l'avertissement : `-W error::DeprecationWarning`
    reste actif et signalerait tout autre appel déprécié. Le remplaçant rend le même instant
    (UTC, naïf) : l'expiration des documents se comporte comme avant.
    """
    monkeypatch.setattr(mongomock, "utcnow", maintenant_utc_naif)


@pytest.fixture(autouse=True)
def schemas_mongo_sans_memoire() -> Iterator[None]:
    """Vide la mémoire de `validation_mongo.schema` avant et après chaque test.

    Pourquoi : `schema()` garde son résultat (`functools.cache`). Un test qui la lit après un
    autre ne traduirait plus rien : il verrait le résultat gardé, et la traduction
    (`_vers_json_schema`) ne serait vérifiée que par le premier test du processus. Avec un
    cache vidé, chaque test refait la traduction qu'il prétend vérifier, quel que soit l'ordre
    d'exécution ; sans cela, une erreur introduite dans la traduction passerait inaperçue
    (mesuré par mutation : 8 mutants de `_vers_json_schema` survivaient).
    """
    validation_mongo.schema.cache_clear()
    yield
    validation_mongo.schema.cache_clear()


@pytest.fixture
def lecture_ecriture_en_utf8(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Fait échouer toute ouverture de fichier texte qui ne déclare pas l'encodage « utf-8 ».

    Pourquoi : sur Windows, `Path.read_text()` sans `encoding` lit en cp1252 ; sous Linux, en
    UTF-8. Un fichier lu sans encodage explicite donne donc deux résultats selon la machine, et
    un test lancé sur une seule des deux ne le voit pas. `pathlib` ouvre ses fichiers par
    `io.open` : on le remplace, le temps du test, par une version qui refuse l'absence
    d'encodage (ou un encodage autre que « utf-8 », écrit exactement ainsi, comme partout dans le
    projet). Les fichiers binaires ne sont pas concernés.

    La fixture rend la liste des ouvertures (chemin, mode, encodage, fin de ligne demandée) :
    un test peut ainsi vérifier aussi que les fichiers générés sont écrits avec des fins de
    ligne LF (argument `newline` égal à un saut de ligne seul), quel que soit le système.
    """
    return espionner_ouvertures_utf8(monkeypatch)


@dataclass
class MiniDemo:
    """Tout ce qu'un test peut vouloir manipuler après le chargement batch."""

    chemin_oltp: Path
    chemin_olap: Path
    moteur: Engine
    entrepot: duckdb.DuckDBPyConnection
    dernier_id: int
    position: int

    def ajouter_paiements(self, nombre: int, graine: int, jour: int = 10) -> int:
        """Ajoute des paiements à l'OLTP (le journal des changements suit)."""
        self.dernier_id = oltp.paiements_synthetiques(
            self.moteur, DEBUT + timedelta(days=jour), nombre, graine=graine,
            premier_id=self.dernier_id + 1)
        return self.dernier_id


@pytest.fixture
def mini_demo(tmp_path: Path):
    """OLTP synthétique + entrepôt en étoile chargé en batch, dans un dossier temporaire."""
    chemin_oltp, chemin_olap = tmp_path / "oltp.sqlite", tmp_path / "olap.duckdb"
    moteur = oltp.moteur(str(chemin_oltp))
    oltp.creer(moteur)
    # jours + 12 : les taux de change couvrent aussi les paiements ajoutés par les tests.
    oltp.referentiels(moteur, jours=JOURS + 12, debut=date(2026, 8, 1))
    dernier = 0
    for j in range(JOURS):
        dernier = oltp.paiements_synthetiques(moteur, DEBUT + timedelta(days=j), PAR_JOUR,
                                              graine=1000 + j, premier_id=dernier + 1)
    entrepot = olap.connecter(str(chemin_olap))
    try:
        olap.charger_batch(entrepot, str(chemin_oltp))
    except duckdb.Error as erreur:
        entrepot.close()
        moteur.dispose()
        # Seule l'extension « sqlite » de DuckDB, absente et impossible à télécharger hors ligne,
        # justifie d'ignorer les tests. Toute autre erreur du chargement (une table mal écrite,
        # une colonne manquante) est un vrai défaut : elle doit faire ÉCHOUER, pas ignorer.
        if "extension" not in str(erreur).lower():
            raise
        pytest.skip(f"extension DuckDB « sqlite » indisponible : {erreur}")
    with moteur.connect() as cx:
        position = cx.execute(text("SELECT max(position) FROM journal_changements")).scalar_one()
    demo = MiniDemo(chemin_oltp, chemin_olap, moteur, entrepot, dernier, position)
    yield demo
    entrepot.close()
    moteur.dispose()
