# ADR-0005 — Exécution locale conteneurisée plutôt qu'hébergement managé

**Date** : 20 juillet 2026 · **Statut** : acceptée

## Contexte

Le règlement de certification impose une démonstration **en direct**, sans
capture d'écran ni vidéo. Deux voies : héberger l'application sur une plateforme
accessible par lien, ou l'exécuter localement en partage d'écran.

## Options

**Hébergement managé gratuit.** Accessible par simple lien, aucune installation
côté examinateur. Mais les offres gratuites mettent les instances en veille après
inactivité, avec un réveil de trente secondes à deux minutes. Une base de données
persistante n'y est généralement pas incluse.

**Exécution locale conteneurisée.** Aucune dépendance réseau, démarrage maîtrisé,
base complète. Suppose un partage d'écran et une machine correctement préparée.

## Décision

Exécution locale par Docker Compose, démonstration en partage d'écran.

## Justification

Le facteur décisif est le **risque**. Une instance gratuite endormie qui met
quatre-vingt-dix secondes à répondre, sur une présentation de six minutes, est un
risque que rien ne compense. Une stack lancée soixante-quinze minutes avant et
laissée chaude n'en présente aucun.

S'ajoute un bénéfice de démonstration : le jury voit une vraie base PostgreSQL
démarrer, des contrôles SQL s'exécuter et des verdicts s'écrire. C'est plus
convaincant qu'une page servie depuis une plateforme tierce.

## Conséquences

**Positives** : aucune dépendance réseau, démarrage maîtrisé, architecture
complète visible.

**Négatives** : le jury ne peut pas manipuler l'application lui-même. Le dépôt de
livrables compense en pointant vers le code source.

**Contrainte induite** : le démarrage à froid doit rester sous deux minutes, ce
qui borne la volumétrie du jeu de démonstration.

---

## Précision — octobre 2026

La décision (exécution locale conteneurisée comme voie principale) est inchangée. Le contexte cité plus haut a évolué :

- **Règle de démonstration.** La démonstration en direct reste fortement recommandée, mais son absence n'est plus éliminatoire. Le candidat doit distinguer clairement ce qui est démontré en direct de ce qui ne l'est pas.
- **Hébergement managé en lecture seule.** Il n'est écarté que comme voie principale. Un Space Hugging Face en lecture seule a servi de secours de démonstration d'après l'historique du dépôt (une capture de son verdict est conservée dans le commit du 2 août 2026) ; son existence en ligne n'est pas vérifiable depuis l'archive de code. Il lit une base managée alimentée depuis une exécution locale et dépend donc d'un accès réseau.
