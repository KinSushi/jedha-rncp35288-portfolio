# Budget

**Compétence visée** : C6.3, définir un budget et le défendre devant les directions métier.

Les montants sont des ordres de grandeur, établis sur des hypothèses explicites. Un budget approximatif mais argumenté vaut mieux qu'une absence de budget.

L'estimation de 81 heures date du cadrage du 20 juillet 2026 : aucun de ses six postes ne nomme le classifieur de priorisation (ADR-0008, 21 juillet 2026) ni le rapport Power BI. Ils ne sont pas chiffrés (§ 6).

---

## 1. Hypothèses

| Hypothèse | Valeur retenue | Source |
|---|---|---|
| Coût journalier d'un ingénieur data | 450 € | Fourchette basse du marché européen pour un profil junior à intermédiaire |
| Journée de travail | 7 heures | Convention |
| Instance PostgreSQL hébergée | 50 €/mois | Offre d'entrée de gamme, un fournisseur cloud européen |
| Exécution du traitement par lots | 30 €/mois | Une exécution quotidienne, quelques minutes de calcul |
| Maintenance en régime établi | 2 jours/mois | Calibrage des règles et traitement des incidents |

## 2. Coût de construction

| Poste | Charge | Coût |
|---|---|---|
| Modélisation et schéma | 12 h | 770 € |
| Générateur et ingestion | 14 h | 900 € |
| Contrôles qualité et réconciliation | 16 h | 1 030 € |
| Tableau de bord | 10 h | 640 € |
| Conteneurisation et industrialisation | 8 h | 515 € |
| Documentation et gestion de projet | 21 h | 1 350 € |
| Total construction | 81 h | ≈ 5 200 € |

Les 81 h sont la charge totale estimée le 20 juillet 2026 pour concevoir, réaliser, industrialiser et documenter le dispositif : le total est la somme exacte des six postes (12 + 14 + 16 + 10 + 8 + 21), y compris les 21 h de « Documentation et gestion de projet ». Le rétroplanning reprend ces six postes et ce total (annexe 03, § 3). La charge consommée (47 h au 20 juillet 2026) y est relevée par phase : elle n'est pas rapprochée poste par poste, et le coût consommé n'est pas établi.

## 3. Coût d'exploitation annuel

| Poste | Base | Coût annuel |
|---|---|---|
| Base de données hébergée | 50 €/mois | 600 € |
| Exécution du traitement | 30 €/mois | 360 € |
| Maintenance des règles | 24 jours/an | 10 800 € |
| Total annuel | | ≈ 11 800 € (600 + 360 + 10 800 = 11 760 €, arrondi) |

## 4. Coût total sur trois ans

| Année | Construction | Exploitation | Cumul |
|---|---|---|---|
| 1 | 5 200 € | 11 800 € | 17 000 € |
| 2 | - | 11 800 € | 28 800 € |
| 3 | - | 11 800 € | 40 600 € |

## 5. Justification économique

Le poste dominant n'est pas l'infrastructure (960 € par an : 600 € de base et 360 € d'exécution) mais la maintenance des règles métier : 24 jours par an, soit 10 800 €. Elle représente 63,5 % du coût de la première année (10 800 € sur 17 000 €) et plus de 90 % de l'exploitation seule (10 800 € sur 11 800 €, soit 91,5 %). C'est cohérent avec la nature du système : sa valeur réside dans la pertinence des contrôles, qui doit être entretenue avec les directions métier.

### Point de comparaison, recalculé avec les hypothèses du § 1

Le projet ne dispose d'aucune source chiffrant la fréquence ou le coût d'un incident de qualité de données réel. La question est donc posée à l'envers : quel coût d'incidents évités justifie le dispositif ? Pour fixer l'ordre de grandeur : quinze jours-homme coûtent 6 750 € (15 × 450 €), moins qu'une seule année d'exploitation (11 800 €). Le calcul suivant donne les seuils, avec les hypothèses du § 1.

| Horizon | Coût du dispositif | Équivalent en jours-homme à 450 € |
|---|---|---|
| Exploitation d'une année | 11 800 € | 11 800 ÷ 450 = 26,2 jours |
| Première année (construction et exploitation) | 17 000 € | 17 000 ÷ 450 = 37,8 jours |
| Trois ans | 40 600 € | 40 600 ÷ 450 = 90,2 jours |

Le dispositif est rentable sur trois ans si les incidents qu'il évite coûtent, ensemble, plus de 40 600 €, soit un peu plus de 90 jours-homme. Un incident découvert à l'audit implique typiquement l'identification de la population affectée, la correction en cascade sur les systèmes aval, le retraitement du reporting concerné et la production d'une justification auprès du régulateur : savoir si un tel incident dépasse ce seuil est une hypothèse à renseigner par la direction métier ; ce projet ne l'établit pas. Il ne conclut donc pas à la rentabilité : il donne le coût d'incidents évités à partir duquel le dispositif devient rentable.

## 6. Ce que ce budget ne couvre pas

- Le classifieur de priorisation : développement, entraînement, suivi de la dérive et réentraînement. Il date du 21 juillet 2026, après l'estimation de 81 heures, et aucun poste du § 2 ne le nomme
- Les licences Power BI : aucun poste du § 2 ni du § 3 ne les nomme
- L'hébergement du Space Hugging Face de secours et de la base hébergée qui l'alimente
- Le branchement sur des sources de données réelles, qui dépend du système d'information cible
- La formation des utilisateurs métier au calibrage des règles
- Toute exigence d'hébergement souverain ou de certification d'infrastructure
- L'extension aux blocs de fonctionnalités listés en feuille de route

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
