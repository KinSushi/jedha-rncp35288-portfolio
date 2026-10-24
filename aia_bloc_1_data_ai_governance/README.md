# AIA Bloc 1 - Gouvernance des données et des systèmes d'IA

Plan de gouvernance des données et des systèmes d'intelligence artificielle pour une plateforme mondiale de streaming musical, réalisé pour le bloc 1 de la certification Architecte en intelligence artificielle (RNCP38777, renouvelée sous le RNCP41993) : « Concevoir et piloter la gouvernance des données et des systèmes d'IA ».

> **Nature du travail.** Il s'agit d'une étude de cas. Le plan ne décrit pas la gouvernance réelle de l'entreprise étudiée et ne s'appuie sur aucune donnée interne de cette entreprise. Les éléments de contexte viennent de l'étude de cas ; tout ce qui n'y figure pas est posé comme hypothèse numérotée (H1 à H13) et signalé comme tel.

## En bref

| | |
|---|---|
| Problème | Une entreprise qui vit de ses données les gère en silos, par département, sur plusieurs juridictions et sans processus commun. |
| Livrable | Un plan de gouvernance en quatre pièces (rapport de maturité, politique, organigramme des rôles, plan de mise en œuvre avec pilote), des annexes de preuve, et une présentation de synthèse. |
| Point de départ mesuré | Maturité moyenne de 2,6 sur 5 (somme 23 sur neuf dimensions). |
| Objectif | Niveau 4 sur 5 en 18 mois, suivi par neuf indicateurs. |
| Cadre réglementaire | RGPD (socle), CCPA, PDPA de Singapour, PCI-DSS 4.0.1, AI Act, DSA. |

## 1. Problématique

L'étude de cas décrit une entreprise dont le produit repose sur la recommandation, et dont les données sont gérées département par département. Trois questions structurent le travail :

1. Où en est l'organisation, dimension par dimension, et quels chantiers traiter en premier ?
2. Quelles règles, quels rôles et quels outils permettent de corriger les silos sans retirer leur autonomie aux équipes produit ?
3. Comment démontrer la conformité (protection des données, paiement, IA) et mesurer le progrès ?

## 2. Données

Le projet ne traite aucun jeu de données. Ses entrées sont :

- **l'étude de cas**, fournie avec l'énoncé du projet ;
- **une liste de conformité** fournie avec l'étude de cas, qui couvre le RGPD, le CCPA et PCI-DSS ;
- **les textes officiels** cités dans le plan (RGPD, AI Act, DSA, CCPA, lignes directrices et normes de sécurité des cartes de paiement).

L'étude de cas et la liste de conformité ne sont **pas redistribuées** ici. Le fichier [`OBTENIR_LES_DONNEES.md`](OBTENIR_LES_DONNEES.md) explique comment y accéder. Aucune donnée personnelle n'est utilisée.

## 3. Méthode

1. **Diagnostic.** Grille de maturité en neuf dimensions, notées de 1 à 5. Recensement de 8 catégories de données et de 5 flux, avec leur sensibilité. Inventaire de 6 systèmes d'IA classés au regard de l'AI Act. Registre de 10 risques notés en gravité et en probabilité, de façon qualitative. Priorisation de 8 chantiers.
2. **Politique.** Neuf principes traduits en règles vérifiables : chaque règle a un responsable et une preuve attendue. Classification en quatre niveaux (public, interne, confidentiel, restreint), qui commande les contrôles. Contrôle d'accès par rôles (RBAC) pour les outils métiers et par attributs (ABAC) pour la plateforme de données. Pseudonymisation par défaut pour l'analytique. Contrats de données au format YAML.
3. **Organisation.** Modèle hybride : un Conseil de gouvernance présidé par le directeur des données ; un centre d'excellence de cinq personnes qui fixe les normes, forme et audite sans exploiter le catalogue ; un Data Owner et un ou deux Data Stewards par domaine ; un délégué à la protection des données (DPO) indépendant ; une équipe Privacy distincte du DPO. Matrice RACI de 10 activités, avec un seul responsable du résultat (« A ») par ligne.
4. **Mise en œuvre.** Calendrier relatif, de M0 (mois de la décision de la direction) à M18. Pilote de 12 semaines, en six quinzaines, sur le domaine « Utilisateurs & consentements », dans deux marchés de l'Union européenne, avec une équipe de sept personnes. Décision de passage à l'échelle à M5 selon une règle chiffrée. Chiffrage en hypothèses explicites.
5. **Pilotage.** Neuf indicateurs, dont deux sur l'IA (inscription au registre, tests de biais), et un indicateur complémentaire de disponibilité. Programme d'audit annuel fondé sur les risques. Veille réglementaire et technologique.
6. **Inclusion.** Plan de formation par public, formats accessibles (référence WCAG 2.1 AA), référent handicap.

Outils retenus dans le plan : Collibra (catalogue, glossaire, lignage), Great Expectations (qualité dans les pipelines), OneTrust (consentements et demandes des personnes), Apache Ranger (ABAC). Le lignage suit le standard ouvert OpenLineage.

## 4. Résultats chiffrés

Chaque valeur renvoie à la section du plan de gouvernance qui la porte.

| Résultat | Valeur | Section du plan |
|---|---|---|
| Maturité moyenne | 23 ÷ 9 = 2,56, arrondi à 2,6 sur 5 | Pièce 1, § 1.2 |
| Répartition des notes | Une dimension à 4, trois à 3, cinq à 2 | Pièce 1, § 1.1 |
| Données recensées | 8 catégories, 5 flux | Annexe B.1 |
| Systèmes d'IA classés | 6, dont aucun à haut risque (analyse préliminaire, H12) | Annexe B.2 |
| Risques | 10 notés, dont 5 prioritaires | Annexe B.3 |
| Chantiers priorisés | 8 | Annexe B.4 |
| Indicateurs de pilotage | 9, plus 1 indicateur complémentaire de disponibilité | Annexe G.1 |
| Cibles de départ (H9) | Contrôles de qualité réussis ≥ 98 % ; personnel formé ≥ 90 % ; demandes des personnes traitées en 15 jours au plus | Annexe G.1 ; § 4.2 |
| Règle de décision à M5 | Au moins 8 critères sur 9, dont obligatoirement la base légale et les incidents | § 4.5 |
| Pilote (hypothèses H5, H6) | 12 semaines ; 5 ETP pendant 3 mois, soit 15 ETP-mois ; équipe de 7 personnes | § 4.2 ; § 4.3 |
| Coût du pilote (hypothèse H6) | 15 ETP-mois × 8 000 € = 120 000 € | § 4.3 |
| Personnel identifié de M0 à M18 (plancher, hypothèses H5 à H7) | 138,5 à 159,5 ETP-mois, soit 1 108 000 à 1 276 000 € | § 4.3 |
| Licences | Sur devis, sous un plafond de décision de 50 000 € pour M0 à M5 (H7) | § 4.3 |
| Couverture du référentiel | 28 indicateurs du RNCP41993 et 5 compétences du RNCP38777, chacun relié à une section du plan | Annexe J |

Le coût mensuel chargé de 8 000 € par ETP est un paramètre de calcul, sans source : il est à remplacer par la donnée des ressources humaines (H6). Le total de 1,1 à 1,3 M€ est un plancher. Il exclut l'équipe Privacy, l'ingénierie d'extension, les licences, la formation, l'audit externe et la journalisation.

## 5. Figures

![Organigramme de la gouvernance : direction générale ; directeur des données, qui préside le Conseil ; sous lui, le centre d'excellence, l'équipe Privacy, le Conseil de gouvernance et le comité d'éthique de l'IA ; DPO indépendant ; ingénierie et référent sécurité ; marketing, juridique et chefs de produit ; cinq domaines de données, chacun avec un Data Owner et un ou deux Data Stewards.](figures/organigramme.png)

![Pilote en six quinzaines : lancement, état initial et flux de données ; catalogue, contrats et registre des IA ; qualité, test de biais et atelier PCI-DSS ; consentements, droits et accès par attributs ; formation, incident simulé et audit à blanc ; mesure finale, rapport et décision.](figures/pilote.png)

## 6. Comment reproduire

Le travail est un raisonnement documenté ; ce qui se reproduit, ce sont ses calculs et ses contrôles de cohérence.

1. **Obtenir les entrées** : voir [`OBTENIR_LES_DONNEES.md`](OBTENIR_LES_DONNEES.md).
2. **Refaire le diagnostic** : noter les neuf dimensions de 1 à 5 à partir de l'étude de cas, puis calculer la moyenne. Avec les notes du plan (4, 3, 3, 3, 2, 2, 2, 2, 2), la somme vaut 23 et la moyenne 2,56.
3. **Recalculer le chiffrage** (§ 4.3), en ETP-mois, puis multiplier par le coût mensuel retenu :

   | Poste | Calcul | ETP-mois |
   |---|---|---|
   | M0, acquisition | 1 + 0,5 | 1,5 |
   | M1, lancement | 1 + 2 + 0,5 + 0,5 | 4 |
   | Pilote, M2 à M4 | 5 ETP × 3 mois | 15 |
   | Centre d'excellence, M1 à M18 | 5 ETP × 18 mois | 90 |
   | Data Owners | 1 × 0,1 × 5 mois + 5 × 0,1 × 13 mois | 7 |
   | Data Stewards hors pilote, M5 à M18 | 5 à 10 × 0,3 × 14 mois | 21 à 42 |
   | **Total (plancher)** | | **138,5 à 159,5** |

4. **Contrôler la cohérence** : un seul « A » par ligne de la matrice RACI ; neuf indicateurs annoncés et listés ; règle de décision appliquée aux neuf critères.
5. **Figures** : diagrammes produits avec Matplotlib à partir des valeurs du plan.

## 7. Limites

- **Étude de cas, pas de terrain.** Les attentes des parties prenantes sont déduites de l'étude de cas, sans consultation réelle (H4). Les profils et les effectifs sont proposés (H2, H3, H5).
- **Chiffrage en hypothèses.** Le coût par ETP n'a pas de source. Les licences ne sont pas chiffrées. Les bénéfices ne sont pas valorisés en euros, faute de données de coût.
- **Classifications préliminaires.** Le classement des systèmes d'IA au regard de l'AI Act, le statut de plateforme en ligne au sens du DSA, la présence de mineurs et le périmètre PCI-DSS restent à confirmer, par le service juridique ou avec l'acquéreur et le prestataire de paiement (H10 à H12).
- **Références juridiques datées.** Elles ont été relevées en octobre 2026. La numérotation consolidée de l'AI Act après sa modification de 2026, le texte de la loi PDPA et les numéros de normes ISO sont à reconfirmer sur les textes d'origine. Les échéances de l'article 50 de l'AI Act (2 août 2026 et 2 décembre 2026) doivent être relues à la date de lecture.
- **Benchmarks.** Les enseignements tirés des pratiques d'autres entreprises sont des hypothèses de transposition, pas des constats.

## 8. Livrables

| Livrable | Disponibilité |
|---|---|
| Ce résumé et ses deux figures | Dans ce dossier |
| Plan de gouvernance complet et présentation de synthèse | Non publiés : ils reprennent des éléments de l'étude de cas et d'autres documents de l'énoncé, qui ne sont pas redistribuables |

## Compétences démontrées

- cartographie des données, des flux et des systèmes d'IA ; analyse et priorisation des risques ;
- rôles et responsabilités : Data Owners, Data Stewards, matrice RACI, coordination entre métiers et équipes techniques ;
- standards de qualité et de traçabilité : contrats de données, catalogue, lignage, interopérabilité ;
- politique d'accès et de protection : RBAC, ABAC, pseudonymisation, chiffrement ;
- conformité et IA responsable : RGPD, CCPA, PCI-DSS, AI Act, DSA, biais et transparence de la recommandation ;
- formation inclusive, y compris pour les personnes en situation de handicap ;
- indicateurs, audits, amélioration continue et veille.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
