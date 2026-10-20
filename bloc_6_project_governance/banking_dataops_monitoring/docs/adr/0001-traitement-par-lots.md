# ADR-0001 — Traitement par lots plutôt qu'en flux continu

**Date** : 8 juin 2026 · **Statut** : acceptée

## Contexte

Le dispositif contrôle la qualité d'un flux de transactions bancaires avant
qu'il n'alimente les systèmes aval. Deux cadences sont possibles : évaluer
chaque transaction à son arrivée, ou évaluer le lot constitué.

## Options

**Flux continu.** Détection à la transaction près, latence minimale. Suppose un
courtier de messages, une gestion d'état, une reprise sur incident, et une
sémantique de fenêtrage pour les contrôles d'agrégat.

**Traitement par lots.** Évaluation d'un lot constitué, à cadence fixe. Simple à
raisonner, à tester et à rejouer. Latence égale à la période du lot.

## Décision

Traitement par lots, à cadence journalière.

## Justification

La décision outillée est **journalière** : elle correspond à l'arrêté quotidien,
qui est la maille réelle du contrôle réglementaire. Une détection à la seconde
n'avance pas une décision prise une fois par jour.

De plus, plusieurs contrôles sont par nature des agrégats — unicité sur
l'ensemble du lot, réconciliation de volumes par système source. Les exprimer en
flux exigerait un fenêtrage dont la sémantique est plus difficile à défendre
devant un auditeur qu'un simple « sur le lot du jour ».

## Conséquences

**Positives** : architecture réduite à quatre étapes séquentielles, rejouable à
l'identique, testable sans infrastructure de messagerie.

**Négatives** : une anomalie survenue en début de journée n'est détectée qu'à la
passe suivante. Acceptable puisque la publication n'a lieu qu'après cette passe.

**Réversibilité** : moyenne. Passer en flux supposerait de repenser les contrôles
d'agrégat, pas seulement de changer d'ordonnanceur.
