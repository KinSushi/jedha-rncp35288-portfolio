# ADR-0004 — Contraintes structurelles en base, règles métier en contrôle

**Date** : 11 juin 2026 · **Statut** : acceptée

## Contexte

Certains contrôles peuvent être appliqués à deux endroits : par le schéma de la
base, ou par la couche de contrôle applicative. Il fallait trancher la
répartition.

## Options

**Tout en base.** Contraintes déclaratives, clés, `CHECK`. Rejet immédiat, coût
nul à l'exécution. Mais un rejet à l'insertion ne produit ni trace, ni verdict,
ni incident.

**Tout en contrôle applicatif.** Traçabilité complète, mais on renonce aux
garanties structurelles que la base offre gratuitement.

**Répartition.** La base garantit la structure, la couche de contrôle traite le
sens.

## Décision

Répartition. Le schéma porte `NOT NULL`, la clé primaire et les clés étrangères.
La couche de contrôle porte les règles métier — bornes de montant, plage du score
de risque, référentiel de statuts, fraîcheur.

## Justification

Une donnée structurellement invalide ne doit jamais entrer : c'est le rôle du
schéma, et il l'assure sans code. Une donnée structurellement valide mais
métier-invalide doit entrer, être détectée, tracée et bloquer la publication :
c'est le rôle du contrôle.

Cette répartition a une conséquence remarquable, vérifiée lors de la conception
du scénario de démonstration : **CTRL-001, CTRL-002 et CTRL-004 ne peuvent pas
échouer** sur des lignes insérées, puisque `amount_chf` est `NOT NULL`,
`transaction_id` est clé primaire et `account_id` porte une clé étrangère.

Loin d'être une faiblesse, c'est la démonstration que la répartition fonctionne.
Les trois contrôles restants — montant, score, statut — portent des règles que
la base ne peut pas exprimer sans figer des seuils métier dans le schéma, ce qui
serait une erreur de conception.

## Conséquences

**Positives** : garanties fortes à coût nul, et une couche de contrôle qui ne
traite que ce qui a du sens métier.

**Négatives** : le scénario de démonstration ne peut illustrer que trois
contrôles sur sept.

**Effet inattendu** : cette limite est devenue l'argument central de la
présentation. Ce que la base ne peut pas voir est exactement ce qui justifie
l'existence de la couche de contrôle.
