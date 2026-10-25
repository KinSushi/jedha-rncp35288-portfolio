# ADR-0006 : API de décision en lecture seule

**Date** : 20 juillet 2026 · **Statut** : acceptée

## Contexte

Le verdict de publication n'était consultable que par un humain, dans le tableau
de bord. Or la décision « ce lot est-il publiable » concerne d'abord les systèmes
aval : c'est le pipeline qui doit s'arrêter, pas seulement l'analyste qui doit le
savoir.

## Options

Ne rien exposer. L'analyste consulte le tableau de bord et arrête
manuellement les traitements. Simple, mais réintroduit exactement l'intervention
humaine que le dispositif cherche à outiller.

Exposer une API complète. Lecture et écriture : déclencher une passe,
acquitter un incident, forcer une publication. Puissant, mais impose
authentification, autorisation, journalisation des accès et gestion de rôles.

Exposer une API en lecture seule. Le verdict, les contrôles, les incidents.
Aucune écriture.

## Décision

API en lecture seule, quatre points d'entrée, servie par un conteneur distinct.

## Justification

La responsabilité doit rester où elle est : **les contrôles décident, l'API
rapporte**. Permettre de forcer une publication par appel HTTP créerait un
chemin de contournement du dispositif : exactement ce qu'un auditeur chercherait.

En lecture seule, il n'y a pas de surface d'écriture à protéger. Le service peut
tomber sans conséquence sur la décision elle-même : le tableau de bord et le
pipeline de contrôle continuent de fonctionner.

Le conteneur est séparé de celui du tableau de bord pour la même raison : une
défaillance de l'un ne doit pas emporter l'autre, ce qui compte le jour d'une
démonstration.

## Conséquences

Positives : le verdict devient consommable par une machine, ce qui fait
passer le dispositif du tableau de bord au système. Aucune surface
d'authentification à traiter.

Négatives : un service de plus à démarrer et à surveiller. Aucun moyen de
déclencher une passe de contrôle à distance.

Point de vigilance : un verdict `WARN` renvoie `can_publish: true`. Un
consommateur qui brancherait sur `status == "PASS"` plutôt que sur `can_publish`
bloquerait à tort sur un simple avertissement. La documentation de l'API le
signale explicitement.

---

## Précision (octobre 2026)

La décision (API en lecture seule) est inchangée. L'implémentation diffère de ce qui est écrit plus haut sur deux points :

- Un seul conteneur pour l'API et le tableau de bord. `docker-compose.yml` déclare deux services, `postgres` et `app`. Le conteneur `app` lance l'API (port 8000) et le tableau de bord Streamlit (port 8501) dans le même conteneur, comme deux processus lancés par `docker/entrypoint.sh`. La séparation en deux conteneurs, décrite dans la justification, n'a pas été réalisée.
- Cinq points d'entrée, pas quatre : `/health`, `/ready`, `/verdict`, `/controls` et `/incidents`.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
