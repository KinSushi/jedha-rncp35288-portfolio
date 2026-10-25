# Registre des risques

**Compétence visée** : C6.4, gérer un projet de données par des indicateurs et des dispositifs de maîtrise.

Cotation : probabilité et impact de 1 (faible) à 4 (critique). Criticité = produit des deux.

Le registre compte dix-sept risques : quatorze établis au cadrage (R-01 à R-14) et trois propres au modèle ajoutés en octobre 2026 (R-15 à R-17, § 4).

---

## 1. Risques projet

| Réf | Risque | P | I | Crit. | Traitement | État |
|---|---|:-:|:-:|:-:|---|---|
| R-01 | Échec du démarrage à froid le jour de la présentation | 3 | 4 | 12 | Pile lancée 75 minutes avant, répétition à froid obligatoire, gel du code à J-1 | Traité |
| R-02 | Dépendance réseau indisponible pendant la démonstration | 2 | 4 | 8 | Exécution entièrement locale, aucune ressource distante requise | Traité |
| R-03 | Dépassement des dix minutes de présentation, démonstration comprise | 3 | 2 | 6 | Déroulé minuté : 9 min 15 s prévues pour les huit diapositives et la démonstration, soit 45 s de marge sur les dix minutes ; non chronométré | Traité |
| R-04 | Modification de dernière minute cassant la démonstration | 3 | 4 | 12 | Gel du code, jalon J5 (2 août 2026) ; non tenu pour le support et la documentation le soir du 2 août (annexe 03, § 6.3) | Traité |
| R-05 | Périmètre non tenu faute de temps | 2 | 3 | 6 | Coupes décidées et documentées dès le cadrage | Traité |
| R-06 | Question d'évaluation sur une limite non anticipée | 3 | 2 | 6 | Limites recensées et déclarées dans le dossier de projet (§ 8) | Traité |

## 2. Risques produit

| Réf | Risque | P | I | Crit. | Traitement | État |
|---|---|:-:|:-:|:-:|---|---|
| R-07 | Faux positifs conduisant au débranchement des contrôles | 3 | 4 | 12 | Sévérités différenciées, mode avertissement sur CTRL-007, calibrage prévu à l'implantation | Partiel |
| R-08 | Contrôles insuffisants face à une anomalie non anticipée | 3 | 3 | 9 | Matrice de contrôles revue à chaque incident | Partiel |
| R-09 | Réconciliation limitée à un contrôle de cohérence interne | 4 | 2 | 8 | Limite assumée et documentée, extension en feuille de route | Accepté |
| R-10 | Volumétrie réelle supérieure à celle testée | 3 | 3 | 9 | Index posés sur les colonnes de filtrage, tests de charge en feuille de route | Partiel |
| R-11 | Dérive entre les contrôles SQL et leur documentation | 2 | 3 | 6 | Test automatisé vérifiant l'existence des fichiers SQL | Traité |

## 3. Risques de conformité

| Réf | Risque | P | I | Crit. | Traitement | État |
|---|---|:-:|:-:|:-:|---|---|
| R-12 | Introduction accidentelle de données réelles | 2 | 4 | 8 | Génération exclusivement synthétique, revue de sûreté publique à chaque publication | Traité |
| R-13 | Fuite d'identifiants dans le dépôt | 2 | 4 | 8 | Fichier d'exemple uniquement, balayage documenté dans la procédure de validation | Traité |
| R-14 | Surinterprétation du projet comme outil de production | 3 | 2 | 6 | Mention explicite en pied de tableau de bord et dans le README | Traité |

## 4. Risques du modèle

Le classifieur de priorisation (ADR-0008, 21 juillet 2026) apporte des risques que le registre du 20 juillet ne contenait pas. Ils sont cotés avec la même méthode, chaque cotation étant justifiée.

| Réf | Risque | P | I | Crit. | Traitement | État |
|---|---|:-:|:-:|:-:|---|---|
| R-15 | Dérive du modèle non surveillée : la distribution des transactions change et le score se dégrade sans alerte | 3 | 2 | 6 | Réentraînement volontaire, jamais automatique (ADR-0008) ; invariant : un modèle d'AUC inférieure à 0,70 n'est pas publié. La mesure de dérive n'est pas implantée (`docs/KPI_DEFINITIONS.md`, § 7) | Partiel |
| R-16 | Faux négatifs : des transactions suspectes restent en bas de file | 4 | 2 | 8 | Seuil choisi pour maximiser le F1 ; la bande *medium* (score d'au moins 0,511, soit 0,60 fois le seuil) élargit la file. Mesuré sur le jeu de test (dossier de projet, § 6.4) : 9 cas suspects manqués sur 30 au seuil retenu de 0,852, 3 sur 30 en lisant aussi la bande *medium*. Seuil non validé sur données réelles | Partiel |
| R-17 | Cible issue d'une règle du générateur : la performance mesurée (AUC 0,970) est lue comme un taux de détection réel | 4 | 2 | 8 | Limite assumée et documentée (ADR-0008, « Cadrage assumé » ; dossier § 6.4 et § 8). Sur données réelles, la cible viendrait d'investigations confirmées | Accepté |

Justification des cotations.

- *Probabilité.* R-15 : 3, parce qu'aucune mesure de dérive n'existe et que le réentraînement est manuel ; R-16 : 4, parce que l'événement est observé (9 faux négatifs sur 30 positifs du jeu de test, rappel 0,700) ; R-17 : 4, parce qu'il est vrai par construction.
- *Impact.* 2 pour les trois, dans le contexte actuel : le modèle ordonne une file d'instruction, il ne bloque rien et le verdict de publication n'en dépend pas ; un cas manqué ou une performance surestimée ne propage aucune donnée fausse. En production, l'impact de R-16 serait à requalifier (3 ou 4), ce qui porterait sa criticité à 12 ou 16.

## 5. Synthèse

Trois risques de criticité 12 : R-01, R-04 et R-07. Aucun risque ne dépasse 12. Deux de ces trois risques sont traités ; R-07 (faux positifs conduisant au débranchement des contrôles) ne l'est que partiellement. R-12 (introduction accidentelle de données réelles) vaut 8 (2 × 4) et n'appartient pas à cette liste.

Sur les dix-sept risques : dix traités, cinq partiels (R-07, R-08, R-10, R-15, R-16), deux acceptés (R-09, R-17).

La concentration porte sur le moment de la démonstration (démarrage à froid et modification tardive) et sur le calibrage des contrôles en exploitation réelle. R-04 (gel du code) est traité par une règle qui n'a pas été tenue pour le support et la documentation le soir du 2 août (annexe 03, § 6.3) : le traitement du risque est en place, son efficacité n'est pas totale.

Le risque R-09 est délibérément accepté plutôt que traité : corriger la réconciliation supposerait un second système source, ce qui sort du périmètre. Il est documenté.

## 6. Revue

Ce registre est revu à chaque jalon du rétroplanning.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
