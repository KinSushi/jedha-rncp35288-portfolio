---
title: banking-dataops-monitoring - Live
emoji: 🏦
colorFrom: indigo
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
short_description: Verdict de publication en direct, pipeline médaillon bancaire, contrôles déterministes, ADR-0002.
tags:
  - streamlit
  - postgresql
  - data-quality
  - banking
  - dataops
  - monitoring
---

# banking-dataops-monitoring : tableau de bord live

Ce lot de données bancaires est-il publiable ? Ce Space répond en direct,
en lisant, jamais en écrivant, la base PostgreSQL alimentée par le pipeline
`banking-dataops-monitoring`.

## Ce qui s'affiche

- Verdict de publication : dérive uniquement de 7 contrôles déterministes,
  jamais d'un score de modèle (ADR-0002, vérifié par test).
- Le détail des 7 contrôles : identifiant, sévérité, statut, lignes en échec.
- La volumétrie des couches silver et gold.
- La priorisation par apprentissage : le modèle ordonne la file des
  analystes, il ne décide jamais de la publication.

## Données

Entièrement synthétiques, générées de façon déterministe (`seed=42`).
Aucune donnée personnelle réelle.

## Lecture seule, par conception

Ce Space n'a, techniquement, aucun moyen d'écrire dans la base : aucune
requête de modification n'existe dans son code. Cohérent avec l'API du projet,
également en lecture seule.

## Reproduire

Le code complet, la suite de tests (chiffres dans `evidence/`), le pipeline et la démonstration
(injection d'anomalies → détection → blocage → rétablissement) sont dans l'archive de code du dépôt de
certification, à la racine de laquelle se trouve `README.md`.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
