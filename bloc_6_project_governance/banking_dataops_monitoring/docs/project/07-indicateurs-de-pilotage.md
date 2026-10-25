# Indicateurs de pilotage

**Compétences visées** : C6.4, piloter par indicateurs et tableaux de bord ; C6.5, restituer aux directions métier.

Deux familles distinctes : les indicateurs du produit, qui mesurent la qualité des données, et les indicateurs du projet, qui mesurent sa conduite.

---

## 1. Indicateurs produit

Affichés en tête du tableau de bord, lisibles en moins de trente secondes. L'interface est en anglais : le libellé affiché est donné entre parenthèses.

| Indicateur | Définition | Source | Seuil |
|---|---|---|---|
| Contrôles réussis (`Controls passed`) | Nombre de contrôles au statut PASS sur le total | `quality_check_results` | 7/7 attendu |
| Lignes en violation (`Rows in violation`) | Somme des `failed_rows` des contrôles en échec | `quality_check_results` | 0 attendu |
| Transactions sous contrôle (`Transactions under control`) | Volume du lot évalué | `transactions` | Informatif |
| Dernière exécution (`Last control run`) | Horodatage de la dernière passe | `quality_check_results` | Moins de 24 h |
| Verdict de publication | PASS, WARN ou FAIL | Agrégat | PASS pour publier |

### Indicateurs du modèle de priorisation

Mesurés sur le jeu de test de 375 transactions (30 positives), évalué une seule fois, ancre de date du 27 juillet 2026 ; source : `evidence/model_metrics.json` et `evidence/bands_gold_predictions.txt`. Le choix du modèle et du seuil se font sur l'apprentissage seul, par validation croisée (ADR-0009 ; dossier de projet, § 6.4).

| Indicateur | Valeur | Seuil ou référence |
|---|---|---|
| AUC ROC de la régression logistique retenue | 0,970 | Plancher de publication du modèle : 0,70 (un invariant lève sous ce seuil) |
| Précision au seuil de 0,852 | 0,656 | - |
| Rappel au seuil de 0,852 | 0,700 | - |
| F1 | 0,677 | Règle simple de comparaison : 0,419 |
| Transactions par niveau de priorité (1 500 notées) | 126 *high*, 99 *medium*, 1 275 *low* | Règle de l'ADR-0009 : *high* au seuil de décision, *medium* à partir de 0,60 fois le seuil |

### Règle de décision

| Verdict | Condition | Action |
|---|---|---|
| PASS | Aucun contrôle en échec | Publication autorisée |
| WARN | Aucun échec, au moins un avertissement | Publication sous réserve, revue requise |
| FAIL | Au moins un contrôle en échec | Publication bloquée, procédure d'incident |

## 2. Indicateurs de conduite du projet

| Indicateur | Mesure | Valeur au 20 juillet 2026 | Mise à jour (octobre 2026) |
|---|---|---|---|
| Avancement des jalons | Jalons franchis sur total | 2 sur 8 | Bilan complet en annexe 03, § 6 : J1, J2 et J4 datés par des pièces, J3 non établi, J5 respecté pour le code seulement, J8 sorti de la fenêtre prévue et replanifié : dépôt au plus tard le 1er novembre 2026 (annexe 03, § 6.6) |
| Charge consommée | Heures passées sur estimées | 47 h sur 81 h | Aucune mesure postérieure : non établi |
| Couverture des exigences | Exigences fonctionnelles livrées | 10 sur 10 | Non remesurée |
| Risques de criticité haute traités | Risques cotés 12 avec traitement en place | 3 risques cotés 12 (R-01, R-04, R-07), dont 2 traités et 1 partiel (R-07) | 17 risques cotés au total (annexe 05) ; R-12 vaut 8 |
| Dette documentaire | Limites connues non documentées | 0 déclarée | Non remesurée |
| Santé de la CI | Vérification de style et tests au vert | Vert | `ruff` et `pytest` réussis dans le rapport généré par GitHub Actions le 3 août 2026, qui n'est pas reproduit dans l'archive ; la CI n'a pas de PostgreSQL |

## 3. Indicateurs de qualité du code

| Indicateur | Outil | État |
|---|---|---|
| Conformité au style | `ruff check .` | Non conservée dans les preuves d'octobre ; sans avertissement au rapport CI du 3 août 2026, non reproduit |
| Tests | `pytest` | <!--tests-fr-->547 réussis, 3 ignorés, 1 échec attendu (xfail), soit 551 cas<!--/tests--> dans <!--tests-fichiers-->48<!--/tests--> fichiers, preuves du 20 octobre 2026 (`evidence/01_pytest_summary.txt`). Les ignorés sautent sous condition (`tests/test_sql_splitter.py`) ; le *xfail* est un écart connu et déclaré du script de démarrage (`docker/entrypoint.sh`) ; la CI n'exécute pas les contrôles SQL sur une base |
| Contrôles documentés | `docs/controls_matrix.md` | 10 contrôles décrits |
| Exécution vérifiable | `evidence/` | Sorties réelles des commandes, avec leur date et leur commande (`evidence/README.md`) |

## 4. Restitution aux directions métier

Le tableau de bord est conçu pour être lu par une personne qui n'écrit pas de SQL. Trois niveaux de lecture :

Premier niveau, trois secondes : le bandeau de verdict. Vert, orange ou rouge, avec une phrase énonçant la décision.

Deuxième niveau, trente secondes : les quatre indicateurs et la liste des contrôles en échec, avec leur sévérité et le nombre de lignes concernées.

Troisième niveau, quelques minutes : la descente au détail par contrôle, la procédure de triage associée, et les volumes par date, statut et canal.

Cette gradation est délibérée : une direction métier doit obtenir la décision sans traverser la technique, tout en pouvant y descendre si elle le souhaite.

## 5. Ce qui n'est pas mesuré aujourd'hui

| Indicateur manquant | Pourquoi il compterait en production |
|---|---|
| Dérive du modèle | Un modèle se dégrade sans alerte quand la distribution change |
| Lisibilité du verdict en moins de 30 secondes (OBJ-05) | Aucun test utilisateur n'a mesuré la cible |
| Taux de faux positifs | Un contrôle trop strict finit débranché |
| Délai moyen de résolution d'incident | Mesure l'efficacité réelle du dispositif |
| Couverture des contrôles par domaine de risque | Révèle les angles morts |
| Durée d'exécution du pipeline | Anticipe la montée en volumétrie |

Les indicateurs ci-dessus figurent dans la feuille de route. Leur absence est assumée : ils supposent un historique d'exploitation que le projet n'a pas encore.

<style>
:not(pre) > code { white-space: nowrap; }
</style>

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
