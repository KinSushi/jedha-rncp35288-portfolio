# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Téléchargement vérifié du jeu public ``fraudTest.csv`` (jamais versionné).

Un fichier de 150 Mo n'a pas sa place dans le dépôt : il est téléchargé à la demande. Pour que
l'entraînement reste reproductible, la taille et l'empreinte SHA-256 attendues sont consignées
dans ``config/base.yaml`` ; un fichier qui ne correspond pas est refusé et supprimé.

Usage : ``python -m detection_fraude.telechargement``

CHOIX
-----
- Retenu : télécharger à la demande et vérifier, plutôt que versionner le fichier (150 Mo).
- Retenu : deux vérifications, sur le flux reçu puis sur le fichier relu depuis le disque : ce
  qu'on a écrit est ce qu'on lira à l'entraînement.
- Retenu : écrire dans un fichier ``.partiel``, forcer l'écriture sur disque (``fsync``), vérifier,
  puis renommer : un entraînement lancé aussitôt ne lit jamais un fichier à moitié visible.
- Retenu : en cas de désaccord, supprimer le fichier partiel et lever ``RuntimeError``.
- Écarté : accepter un fichier sans vérifier son empreinte, qui rendrait l'entraînement non
  reproductible ; alternative non documentée en détail (source : inconnue).

OÙ
---
- Place : amont de l'entraînement ; première tâche du DAG de réentraînement.
- Appelants : preparer (detection_fraude/local.py:117-124), verifier_jeu_de_donnees
  (detection_fraude/taches_airflow.py:220-224), ``main``.
- Dépendances : ``requests`` ; ``configuration`` (URL, taille, empreinte attendues),
  ``modele.empreinte_fichier`` et ``journal``.

COMMENT
-------
1. Si le fichier existe et que ``forcer`` est faux, la fonction rend le chemin sans réseau
   (événement ``jeu_deja_present``).
2. Sinon elle télécharge par blocs de 1 Mio avec les délais (10 s de connexion, 120 s de lecture),
   calcule la taille et le SHA-256 au fil du flux, vérifie la taille et l'empreinte attendues
   (quand elles sont configurées), puis relit le fichier pour confirmer l'empreinte.
3. Tout écart supprime le fichier partiel et lève ``RuntimeError`` ; sinon le fichier est
   renommé à son nom définitif.
Tests qui le prouvent : test_telechargement_verifie_taille_et_empreinte
(tests/test_divers.py:66-78), test_telechargement_refuse_un_fichier_altere
(tests/test_divers.py:81-94),
test_taille_et_empreinte_fausses_sont_toutes_deux_citees_dans_cet_ordre
(tests/test_telechargement_garanties.py:284-299) et
test_un_refus_du_serveur_sans_jeu_en_place_cree_le_dossier_mais_aucun_fichier
(tests/test_telechargement_garanties.py:392-403).

POURQUOI
--------
- Jeu téléchargé : taille et SHA-256 vérifiés sur le flux et sur le fichier relu :
  livrables/aia3/02_pipeline_et_qualite.md:210-230 (ligne « Jeu téléchargé »).
- Reproductibilité (empreinte du jeu consignée dans les métadonnées du modèle) :
  livrables/aia3/02_pipeline_et_qualite.md:38-107.
- Taille et empreinte attendues : config/base.yaml:23.
- Indicateurs 3.3 et 4.1 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.telechargement``, INFO) : ``jeu_deja_present`` (``chemin``,
``octets``) et ``jeu_telecharge`` (``chemin``, ``octets``, ``sha256``). Un refus n'est pas
journalisé ici : il remonte par ``RuntimeError`` dont le message cite les écarts constatés.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import requests

from detection_fraude.configuration import Configuration, charger_configuration
from detection_fraude.journal import configurer_journal, info, obtenir_journal
from detection_fraude.modele import empreinte_fichier

_journal = obtenir_journal("telechargement")


def telecharger_jeu(config: Configuration, *, forcer: bool = False) -> Path:
    """Télécharge le jeu si besoin et en vérifie la taille puis l'empreinte."""
    source = config.source
    chemin = source.jeu_chemin
    if chemin.exists() and not forcer:
        info(_journal, "jeu_deja_present", chemin=str(chemin), octets=chemin.stat().st_size)
        return chemin
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(".partiel")
    condense = hashlib.sha256()
    taille = 0
    with requests.get(source.jeu_url, stream=True, timeout=(10, 120)) as reponse:
        reponse.raise_for_status()
        with temporaire.open("wb") as flux:
            for bloc in reponse.iter_content(chunk_size=1 << 20):
                flux.write(bloc)
                condense.update(bloc)
                taille += len(bloc)
            # Écriture garantie sur le disque avant la vérification et le renommage : un
            # entraînement lancé aussitôt ne doit jamais lire un fichier à moitié visible.
            flux.flush()
            os.fsync(flux.fileno())
    erreurs = []
    if source.jeu_taille_octets and taille != source.jeu_taille_octets:
        erreurs.append(f"taille {taille} au lieu de {source.jeu_taille_octets}")
    if source.jeu_sha256 and condense.hexdigest() != source.jeu_sha256:
        erreurs.append("empreinte SHA-256 différente de celle attendue")
    # Seconde vérification, cette fois sur le fichier relu depuis le disque : ce qu'on a écrit
    # est ce qu'on lira à l'entraînement.
    if not erreurs and source.jeu_sha256 and empreinte_fichier(temporaire) != source.jeu_sha256:
        erreurs.append("le fichier relu depuis le disque diffère du flux reçu")
    if erreurs:
        temporaire.unlink(missing_ok=True)
        raise RuntimeError("Téléchargement refusé : " + " ; ".join(erreurs))
    temporaire.replace(chemin)
    info(
        _journal, "jeu_telecharge", chemin=str(chemin), octets=taille, sha256=condense.hexdigest()
    )
    return chemin


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Télécharge le jeu public fraudTest.csv.")
    analyseur.add_argument("--forcer", action="store_true", help="retélécharge même si présent")
    arguments = analyseur.parse_args(argv)
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console)
    telecharger_jeu(config, forcer=arguments.forcer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
