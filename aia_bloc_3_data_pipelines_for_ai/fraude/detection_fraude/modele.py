# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Export, intégrité et chargement du modèle de détection — critères 2.3 et 5.2.

Entre l'entraînement (tâche hebdomadaire Airflow) et le scoring (consommateur en continu), il
faut un *contrat d'échange* : un dossier versionné contenant le pipeline scikit-learn complet
(prétraitements compris, donc aucune dérive possible entre les deux), le seuil de décision et
des métadonnées. Un pointeur ``ACTUEL`` désigne la version en service ; le consommateur le
surveille et recharge le modèle sans redémarrage quand une nouvelle version est promue.

CHOIX
-----
- Retenu : un dossier par version (``modele.pkl.gz`` et ``metadonnees.json``) et un pointeur
  ``ACTUEL`` écrit de façon atomique (écriture dans un fichier temporaire puis renommage) : un
  lecteur ne voit jamais un fichier à moitié écrit.
- Retenu : vérifier l'empreinte SHA-256 consignée dans les métadonnées **avant** de charger le
  fichier, car un ``pickle`` est exécutable au chargement.
- Retenu : sérialiser avec le ``pickle`` standard en C, compressé en gzip, et non avec ``joblib``.
  Un modèle écrit sous Airflow contenait des références à ``dill`` (modifié globalement par
  Airflow) que le consommateur, sans ``dill``, ne pouvait pas charger.
- Retenu : un rechargement raté ne doit pas arrêter le flux : on garde l'ancien modèle, on
  journalise une fois par version refusée, la supervision le signale. Le premier chargement,
  lui, échoue franchement.
- Écarté : ``joblib`` pour l'artefact (cause constatée ci-dessus) ; un chargement sans contrôle
  d'empreinte.

OÙ
---
- Place : contrat d'échange entre l'entraînement et le scoring.
- Appelants : entrainer (detection_fraude/entrainement.py:351-526) (export),
  construire_consommateur (detection_fraude/consommateur.py:472-485) (``ChargeurModele``),
  retraiter (detection_fraude/retraitement.py:94-198) (``charger_modele``), controler
  (detection_fraude/supervision.py:99-189) (pointeur) et telecharger_jeu
  (detection_fraude/telechargement.py:79-117) (``empreinte_fichier``).
- Dépendances : NumPy, pandas, ``fonctionnalites.COLONNES_MODELE`` et ``journal``.

COMMENT
-------
1. ``exporter_modele`` écrit l'artefact, calcule son SHA-256 et écrit les métadonnées avec le
   seuil, sans changer le modèle en service.
2. ``promouvoir`` refuse une version inconnue puis écrit le pointeur ``ACTUEL``.
3. ``charger_modele`` lit les métadonnées, compare l'empreinte et ne charge qu'en cas d'accord.
4. ``ChargeurModele.obtenir`` garde le modèle en mémoire et le recharge quand le pointeur change.
5. ``ModeleFraude.noter`` refuse un tableau auquel il manque une caractéristique.
Tests qui le prouvent : test_chargement_refuse_un_fichier_modifie
(tests/test_entrainement.py:163-173), test_le_chargeur_recharge_apres_une_promotion
(tests/test_entrainement.py:183-198),
test_le_rechargement_rate_garde_l_ancien_modele_sans_arreter_le_flux
(tests/test_entrainement.py:326-352),
test_l_export_reste_independant_de_dill_meme_importe_apres_joblib
(tests/test_entrainement.py:360-394),
test_le_modele_exporte_ne_depend_que_des_bibliotheques_de_scoring
(tests/test_entrainement.py:397-416),
test_noter_liste_les_manquantes_dans_l_ordre_du_modele_et_non_celui_du_tableau
(tests/test_modele_garanties.py:206-212) et
test_ecriture_atomique_ne_masque_pas_l_erreur_d_origine_si_le_temporaire_a_disparu
(tests/test_modele_garanties.py:411-425).

POURQUOI
--------
- Empreinte vérifiée avant le chargement du pickle, modèle indépendant du canal :
  livrables/aia3/03_securite_conformite.md:113-127 et
  livrables/aia3/03_securite_conformite.md:158-168.
- Incident constaté sous Airflow (référence à ``dill``), conservation de l'ancien modèle et
  constat ``modele_non_applique`` : livrables/aia3/04_exploitation.md:78-90.
- Comportement du chargeur : ChargeurModele.obtenir (detection_fraude/modele.py:239-261).
- Indicateurs 2.3 et 5.2 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Événement écrit (journal ``fraude.modele``) : ``rechargement_modele_impossible`` (ERROR), une
seule fois par version refusée, avec les champs ``version``, ``gardee`` (version conservée) et
``cause`` (type et message, tronqué à 300 caractères). Aucun autre événement : la promotion est
journalisée par ``entrainement.promouvoir_version`` (``modele_promu``).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pickle
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from detection_fraude.fonctionnalites import COLONNES_MODELE
from detection_fraude.journal import erreur, obtenir_journal

_journal = obtenir_journal("modele")

NOM_FICHIER_MODELE = "modele.pkl.gz"
NOM_FICHIER_METADONNEES = "metadonnees.json"
NOM_POINTEUR = "ACTUEL"


class ErreurModele(Exception):
    """Modèle absent, corrompu ou dont l'empreinte ne correspond pas."""


@dataclass
class ModeleFraude:
    """Pipeline entraîné + seuil + métadonnées, prêt à noter un tableau de caractéristiques."""

    pipeline: Any
    seuil: float
    version: str
    metadonnees: dict[str, Any] = field(default_factory=dict)

    def noter(self, caracteristiques: pd.DataFrame) -> np.ndarray:
        """Probabilité de fraude pour chaque ligne."""
        manquantes = [c for c in COLONNES_MODELE if c not in caracteristiques.columns]
        if manquantes:
            raise ErreurModele(f"Caractéristiques manquantes : {', '.join(manquantes)}")
        return np.asarray(
            self.pipeline.predict_proba(caracteristiques[list(COLONNES_MODELE)])[:, 1]
        )

    def decider(self, scores: np.ndarray) -> np.ndarray:
        return (np.asarray(scores) >= self.seuil).astype(int)


def empreinte_fichier(chemin: Path) -> str:
    condense = hashlib.sha256()
    with chemin.open("rb") as flux:
        # Blocs de 1 Mio jusqu'au premier bloc vide (fin du fichier), sans valeur sentinelle :
        # une sentinelle mal écrite ferait tourner la lecture sans fin.
        while bloc := flux.read(1 << 20):
            condense.update(bloc)
    return condense.hexdigest()


def nouvelle_version() -> str:
    """Identifiant lisible et triable : date UTC de l'entraînement."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _ecrire_atomiquement(chemin: Path, contenu: str) -> None:
    """Écrit puis renomme : un lecteur ne voit jamais un fichier à moitié écrit."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    descripteur, temporaire = tempfile.mkstemp(dir=chemin.parent, prefix=".tmp_")
    try:
        with os.fdopen(descripteur, "w", encoding="utf-8") as flux:
            flux.write(contenu)
        os.replace(temporaire, chemin)
    except BaseException:
        Path(temporaire).unlink(missing_ok=True)
        raise


def ecrire_artefact(pipeline: Any, fichier: Path) -> None:
    """Sérialise le pipeline avec le ``pickle`` **standard en C**, compressé en gzip.

    Choix délibéré, constaté en production locale : ``joblib`` s'appuie sur la version Python
    pure de ``pickle``, que ``dill`` (importé par Airflow) modifie globalement. Un modèle
    entraîné par le DAG contenait alors des références à ``dill`` (types ``slice`` et
    ``functools.partial``) et le consommateur, qui n'a pas ``dill``, ne pouvait plus le charger.
    Le pickler C n'est pas affecté : l'artefact ne dépend que de scikit-learn, numpy, scipy et
    pandas, jamais du processus qui l'a écrit.
    """
    with gzip.open(fichier, "wb", compresslevel=3) as flux:
        pickle.dump(pipeline, flux, protocol=pickle.HIGHEST_PROTOCOL)


def lire_artefact(fichier: Path) -> Any:
    """Relit un artefact (à n'appeler qu'après vérification de son empreinte SHA-256)."""
    with gzip.open(fichier, "rb") as flux:
        return pickle.load(flux)  # noqa: S301 - empreinte vérifiée juste avant par l'appelant


def exporter_modele(
    dossier_modeles: Path, pipeline: Any, seuil: float, metadonnees: dict[str, Any]
) -> Path:
    """Écrit ``<dossier>/<version>/`` (pipeline, métadonnées) sans changer le modèle en service."""
    version: str = metadonnees["version"]
    cible = dossier_modeles / version
    cible.mkdir(parents=True, exist_ok=True)
    fichier = cible / NOM_FICHIER_MODELE
    ecrire_artefact(pipeline, fichier)
    contenu = {**metadonnees, "seuil": seuil, "sha256_modele": empreinte_fichier(fichier)}
    _ecrire_atomiquement(
        cible / NOM_FICHIER_METADONNEES,
        json.dumps(contenu, ensure_ascii=False, indent=2, default=str),
    )
    return cible


def promouvoir(dossier_modeles: Path, version: str) -> None:
    """Fait pointer ``ACTUEL`` vers ``version`` (écriture atomique)."""
    if not (dossier_modeles / version / NOM_FICHIER_MODELE).exists():
        raise ErreurModele(f"Version inconnue : {version}")
    _ecrire_atomiquement(dossier_modeles / NOM_POINTEUR, version + "\n")


def version_en_service(dossier_modeles: Path) -> str | None:
    pointeur = dossier_modeles / NOM_POINTEUR
    if not pointeur.exists():
        return None
    return pointeur.read_text(encoding="utf-8").strip() or None


def lire_metadonnees(dossier_modeles: Path, version: str) -> dict[str, Any]:
    chemin = dossier_modeles / version / NOM_FICHIER_METADONNEES
    if not chemin.exists():
        raise ErreurModele(f"Métadonnées absentes pour la version {version}")
    metadonnees: dict[str, Any] = json.loads(chemin.read_text(encoding="utf-8"))
    return metadonnees


def charger_modele(dossier_modeles: Path, version: str | None = None) -> ModeleFraude:
    """Charge une version (par défaut celle en service) après vérification de son empreinte."""
    version = version or version_en_service(dossier_modeles)
    if not version:
        raise ErreurModele(
            f"Aucun modèle en service dans {dossier_modeles}. "
            "Lancer l'entraînement : python -m detection_fraude.entrainement"
        )
    metadonnees = lire_metadonnees(dossier_modeles, version)
    fichier = dossier_modeles / version / NOM_FICHIER_MODELE
    if empreinte_fichier(fichier) != metadonnees.get("sha256_modele"):
        raise ErreurModele(f"Empreinte SHA-256 incorrecte pour {fichier} : chargement refusé.")
    pipeline = lire_artefact(fichier)
    return ModeleFraude(pipeline, float(metadonnees["seuil"]), version, metadonnees)


class ChargeurModele:
    """Garde le modèle en mémoire et le recharge quand le pointeur ``ACTUEL`` change."""

    def __init__(self, dossier_modeles: Path) -> None:
        self._dossier = dossier_modeles
        self._modele: ModeleFraude | None = None
        self._refusee: str | None = None  # dernière version dont le chargement a échoué

    def obtenir(self) -> ModeleFraude:
        """Renvoie le modèle en service ; recharge si une nouvelle version a été promue.

        Le **premier** chargement échoue franchement (sans modèle, rien ne peut être noté). Un
        **rechargement** raté ne doit pas arrêter le flux : on garde le modèle qui fonctionne, on
        journalise l'erreur une fois par version refusée, et la supervision le signale.
        """
        courante = version_en_service(self._dossier)
        if self._modele is None:
            self._modele = charger_modele(self._dossier, courante)
        elif courante and courante != self._modele.version and courante != self._refusee:
            try:
                self._modele = charger_modele(self._dossier, courante)
            except Exception as exc:  # noqa: BLE001 - tout échec de chargement garde l'ancien modèle
                self._refusee = courante
                erreur(
                    _journal,
                    "rechargement_modele_impossible",
                    version=courante,
                    gardee=self._modele.version,
                    cause=f"{type(exc).__name__}: {exc}"[:300],
                )
        return self._modele
