# ADR-0003 : Données synthétiques déterministes

**Date** : 10 juin 2026 · **Statut** : acceptée · Complétée le 11 octobre 2026 (section finale)

## Contexte

Le dépôt est une preuve technique publique. Il doit démontrer un dispositif
destiné à de la donnée bancaire réelle, sans en exposer.

## Options

Jeu de données public réel. Crédibilité immédiate, mais aucun jeu public ne
présente la structure d'un flux transactionnel bancaire avec systèmes sources
multiples et scores de risque.

Extrait réel anonymisé. Impossible : aucun accès légitime, et
l'anonymisation d'un flux transactionnel est un problème ouvert.

Génération synthétique déterministe. Contrôle total sur la structure, sur les
volumes et sur l'injection d'anomalies. Reproductible à la graine.

## Décision

Génération synthétique, graine fixe, aucune donnée réelle.

## Justification

La reproductibilité est ici supérieure à ce qu'offrirait un jeu réel : avec
la même graine, n'importe qui reproduit exactement les mêmes lignes et donc les
mêmes verdicts. C'est un argument de rigueur, pas un pis-aller.

La génération permet également d'injecter des anomalies choisies, ce qui rend le
dispositif de contrôle testable : un jeu réel propre ne permettrait pas de
démontrer la détection.

Le dépôt ne contient aucune donnée réelle : l'absence de personne identifiable se vérifie
par lecture du générateur (`src/banking_dataops/generate_synthetic_data.py`), et la note de
conformité en tire la conséquence ([06-conformite-rgpd.md](../project/06-conformite-rgpd.md)).

## Conséquences

Positives : reproductibilité totale, aucune exposition, testabilité des
contrôles, publication sans restriction.

Négatives : les distributions ne reflètent pas la réalité d'un flux bancaire.
Le calibrage des seuils devra être refait sur données réelles.

Limite assumée : le projet démontre un motif d'architecture, pas des
seuils prêts à l'emploi.

## Précision d'octobre 2026

« Avec la même graine, n'importe qui reproduit exactement les mêmes lignes » n'était vrai
qu'avec une ancre de date identique. Les horodatages étaient dérivés du jour d'exécution,
donc le jour de la semaine, donc deux variables du modèle : la graine seule ne suffisait pas.
La reproductibilité est désormais garantie par le couple **graine 42 et ancre
`2026-07-27T00:00:00Z`**, valeur par défaut de `run_pipeline.py` et du générateur
(option `--generated-at`, variable `BANKING_DATAOPS_GENERATED_AT`). Voir l'ADR-0009,
décision 3.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
