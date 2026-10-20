# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Journaux d'audit structurés (une ligne JSON par événement) — critère 6.3.

Un incident de production se diagnostique avec des événements nommés et des champs
filtrables (``evenement``, ``trans_num``, ``lot``, ``duree_ms``...), pas avec des phrases libres.
Chaque événement est donc écrit en JSON, une ligne par événement, dans un fichier d'audit
(rotation incluse) et sur la console (JSON en production, texte lisible pour la démonstration).

CHOIX
-----
- Retenu : un événement nommé plus des champs (``journaliser(journal, niveau, evenement,
  **champs)``) plutôt qu'une phrase libre : le nom et les champs se filtrent avec ``grep`` ou un
  outil d'analyse.
- Retenu : le nettoyage est fait ici, au dernier moment, même si l'appelant a déjà masqué : défense
  en profondeur (critère 5.1). Les champs sensibles deviennent ``[MASQUE]``, toute suite isolée de
  13 à 19 chiffres devient ``[PAN_MASQUE]``.
- Retenu : un fichier tournant de 5 Mo sur 5 archives, pour que l'audit ne remplisse pas le disque.
- Retenu : deux formats, JSON (production, fichier) et texte lisible (console de démonstration).
- Écarté : un format libre ou un journal sans rotation. Alternative non documentée en détail
  (source : inconnue) ; le choix retenu est celui du code.

OÙ
---
- Place : brique transverse ; chaque module obtient son journal par ``obtenir_journal`` et écrit
  par ``info``, ``avertissement`` ou ``erreur``.
- Appelants : tous les modules du paquet ; ``main`` de chaque outil appelle ``configurer_journal``.
- Dépendances : bibliothèque standard seulement (``logging``, ``json``, ``re``).

COMMENT
-------
1. ``configurer_journal`` installe, sur le journal racine ``fraude``, un gestionnaire console
   (stderr) et, si un fichier est donné, un gestionnaire à rotation ; il est idempotent (il retire
   d'abord les gestionnaires précédents).
2. ``journaliser`` ne fait rien si le niveau est désactivé, sinon passe les champs à ``nettoyer``.
3. ``FormatJson`` écrit horodatage UTC à la milliseconde, niveau, composant, événement et champs ;
   une exception est ajoutée sous la clé ``exception`` après nettoyage.
Tests qui le prouvent : test_nettoyer_masque_les_champs_sensibles_et_les_numeros_de_carte
(tests/test_journal.py:18-23), test_une_ligne_json_par_evenement_avec_champs_normalises
(tests/test_journal.py:38-54), test_le_format_json_range_les_exceptions_sans_donnee_sensible
(tests/test_journal.py:57-68), test_vingt_chiffres_et_plus_restent_intacts
(tests/test_journal_garanties.py:235-239) et
test_chaque_cle_sensible_est_masquee_quelle_que_soit_la_valeur_et_l_entree_reste_inchangee
(tests/test_journal_garanties.py:155-164).

POURQUOI
--------
- Journaux d'audit exploitables, format et catalogue des événements :
  livrables/aia3/04_exploitation.md:92-133.
- Nettoyage automatique des champs sensibles : livrables/aia3/03_securite_conformite.md:21-73
  (section « Preuves exécutables », puce sur ``journal.py``).
- Exigence 10.2 de PCI DSS 4.0.1 (journaux d'audit sans PAN), transposée :
  livrables/aia3/03_securite_conformite.md:92-111 ; texte de la norme : source à joindre.
- Critère 6.3 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Ce module est le journal. Il écrit lui-même un événement par appel de ``info`` (niveau INFO),
``avertissement`` (WARNING) ou ``erreur`` (ERROR). Le fichier d'audit est ``audit.jsonl``, dans le
dossier ``donnees`` puis ``journaux`` par défaut (5 Mo, 5 archives) ; la console reçoit le même
événement en JSON (production) ou en texte lisible (démonstration). Le niveau minimal vient de la
configuration (``journal.niveau``, ``INFO`` par défaut).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Noms de champs de l'API source qui ne doivent jamais apparaître dans un journal.
CHAMPS_SENSIBLES = frozenset(
    {"cc_num", "first", "last", "street", "dob", "city", "zip", "lat", "long", "job", "gender"}
)
_MOTIF_PAN = re.compile(r"(?<![\w.])\d{13,19}(?![\w.])")
MASQUE = "[MASQUE]"


def nettoyer(valeur: Any, cle: str | None = None) -> Any:
    """Retire d'une valeur tout ce qui ressemble à une donnée sensible (récursif)."""
    if cle is not None and cle in CHAMPS_SENSIBLES:
        return MASQUE
    if isinstance(valeur, dict):
        return {str(k): nettoyer(v, str(k)) for k, v in valeur.items()}
    if isinstance(valeur, list | tuple):
        return [nettoyer(v) for v in valeur]
    if isinstance(valeur, str):
        return _MOTIF_PAN.sub("[PAN_MASQUE]", valeur)
    return valeur


class FormatJson(logging.Formatter):
    """Une ligne JSON par événement, horodatée en UTC."""

    def format(self, record: logging.LogRecord) -> str:
        ligne: dict[str, Any] = {
            "horodatage": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "niveau": record.levelname,
            "composant": record.name.removeprefix("fraude."),
            "evenement": record.getMessage(),
        }
        ligne.update(getattr(record, "champs", {}))
        if record.exc_info:
            ligne["exception"] = nettoyer(self.formatException(record.exc_info))
        return json.dumps(ligne, ensure_ascii=False, default=str)


class FormatLisible(logging.Formatter):
    """Format court pour la console pendant la démonstration."""

    def format(self, record: logging.LogRecord) -> str:
        heure = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        champs = getattr(record, "champs", {})
        details = " ".join(f"{k}={v}" for k, v in champs.items())
        texte = (
            f"{heure} {record.levelname:<7} {record.name.removeprefix('fraude.'):<12} "
            f"{record.getMessage()} {details}"
        )
        if record.exc_info:
            texte += "\n" + str(nettoyer(self.formatException(record.exc_info)))
        return texte.rstrip()


def console_utf8() -> None:
    """Force l'UTF-8 sur la console (accents corrects dans un terminal ou un tube Windows)."""
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(encoding="utf-8", errors="replace")


def configurer_journal(
    niveau: str = "INFO", console: str = "lisible", fichier: Path | None = None
) -> None:
    """Installe les gestionnaires sur le journal racine ``fraude`` (idempotent)."""
    console_utf8()
    racine = logging.getLogger("fraude")
    racine.setLevel(niveau.upper())
    racine.propagate = False
    for gestionnaire in list(racine.handlers):
        racine.removeHandler(gestionnaire)
        gestionnaire.close()
    sortie = logging.StreamHandler(sys.stderr)
    sortie.setFormatter(FormatJson() if console == "json" else FormatLisible())
    racine.addHandler(sortie)
    if fichier is not None:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        # 5 Mo par fichier, 5 fichiers conservés : l'audit ne remplit pas le disque.
        archive = RotatingFileHandler(fichier, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        archive.setFormatter(FormatJson())
        racine.addHandler(archive)


def obtenir_journal(composant: str) -> logging.Logger:
    return logging.getLogger(f"fraude.{composant}")


def journaliser(journal: logging.Logger, niveau: int, evenement: str, /, **champs: Any) -> None:
    """Écrit un événement nommé avec ses champs, après nettoyage."""
    if journal.isEnabledFor(niveau):
        journal.log(niveau, evenement, extra={"champs": nettoyer(champs)})


def info(journal: logging.Logger, evenement: str, /, **champs: Any) -> None:
    journaliser(journal, logging.INFO, evenement, **champs)


def avertissement(journal: logging.Logger, evenement: str, /, **champs: Any) -> None:
    journaliser(journal, logging.WARNING, evenement, **champs)


def erreur(journal: logging.Logger, evenement: str, /, **champs: Any) -> None:
    journaliser(journal, logging.ERROR, evenement, **champs)
