# Rejeu du modèle de priorisation — ancre du 27 juillet 2026

Ce fichier accompagne `evidence/rejeu_modele_2026-07-27.json`. Il dit ce que le rejeu est, comment il a été fait, et ce qu'il établit. Les chiffres du dossier de projet (§ 6.4) et de l'ADR-0008 qui ne figurent pas dans `evidence/pipeline_full.txt` viennent de ce rejeu.

## Pourquoi un rejeu

`evidence/pipeline_full.txt` journalise l'AUC des deux modèles (0,97 et 0,9573) mais ni la précision, le rappel, la matrice de confusion, ni la taille du jeu de test. Ces valeurs ne figuraient que sur des captures d'écran. Elles ont été recalculées sans PostgreSQL.

## Méthode

1. Le générateur du projet (`src/banking_dataops/generate_synthetic_data.py`) produit le jeu : 100 clients, 200 comptes, 1 500 transactions, graine 42, avec une ancre de date explicite : minuit UTC du jour indiqué.
2. La requête d'extraction `features._EXTRACT_SQL` (transactions, comptes, clients, hors lignes `demo_scenario`, triées par `transaction_id`) est reproduite en pandas. Le nettoyage Bronze vers Silver ne modifie aucune valeur sur ce jeu (journal : 1 500 lignes entrantes et sortantes, aucune quarantaine), la lecture directe des fichiers CSV est donc équivalente.
3. La fonction `train.train()` du dépôt est appelée telle quelle, avec des doublures neutres pour l'accès PostgreSQL et le journal. Le modèle est écrit dans un dossier temporaire.
4. Des mesures complémentaires sont calculées avec les fonctions du dépôt (`features.build`, `train._build_estimator`, `train._choose_threshold`) : règle simple de comparaison, ordre d'arrivée, seuil choisi hors du jeu de test, choix du modèle en validation croisée sur l'entraînement, rééchantillonnage du jeu de test (2 000 tirages, graine 0), variabilité selon le découpage (20 découpages), coefficients signés.

Le script de rejeu n'est pas joint au dépôt. La commande équivalente du dépôt est `python run_pipeline.py --seed 42 --transactions 1500 --customers 100 --train` (PostgreSQL requis), suivie de `python -m banking_dataops.ml.predict` pour la file de priorité.

## Pourquoi le 27 juillet 2026

`run_pipeline.py` ancre les horodatages à minuit UTC du jour d'exécution. L'empreinte du lot journalisée dans `evidence/pipeline_full.txt` est `89ac138fb9943f00`. Parmi les 911 dates d'ancre testées (du 1er janvier 2025 au 30 juin 2027), **une seule** reproduit cette empreinte : le **27 juillet 2026 à minuit UTC**. Le rejeu à cette date reproduit aussi les AUC du journal : 0,97 pour la régression logistique, 0,9573 pour la forêt aléatoire, et le choix du modèle.

## Résultats à l'ancre du 27 juillet 2026

| Mesure | Valeur |
|---|---|
| Transactions, positives | 1 500, 121 (8,07 %) |
| Entraînement, test | 1 125, 375 (30 positives) |
| AUC logistique, forêt, écart | 0,9700 ; 0,9573 ; −0,0128 (marge exigée : +0,01) |
| Modèle retenu | régression logistique |
| Seuil (maximise le F1 sur le jeu de test) | 0,8335 (0,833 arrondi) |
| Précision, rappel, F1 | 0,657 ; 0,767 ; 0,708 |
| Matrice de confusion (VP / FP / FN / VN) | 23 / 12 / 7 / 333 |
| Bandes sur les 1 500 transactions | 227 *medium*, 1 273 *low*, 0 *high* (frontière *high* : 1,0835 ; score maximal : 0,9994) |
| Règle simple (au moins 2 facteurs parmi nuit, transfrontalier, montant élevé) | AUC 0,811 ; précision 0,293 ; rappel 0,733 ; F1 0,419 |
| Ordre d'arrivée (horodatage ou identifiant) | AUC 0,45 à 0,49 sur le jeu de test |
| Seuil choisi hors du jeu de test | 0,852 ; précision 0,656 ; rappel 0,70 ; F1 0,677 |
| Choix du modèle en validation croisée sur l'entraînement | logistique 0,9748 ; meilleure forêt 0,9661 ; même décision |
| Rééchantillonnage du jeu de test (95 %) | AUC [0,951 ; 0,985] ; précision [0,50 ; 0,815] ; rappel [0,606 ; 0,909] |
| AUC sur 20 découpages | 0,957 à 0,989 (moyenne 0,973) |

## Dépendance à la date d'ancre

Les variables « jour de la semaine » et « week-end » sont dérivées des horodatages, donc de l'ancre. Seul le **jour de la semaine de l'ancre** change le jeu vu par le modèle : les rejeux du 20 et du 27 juillet, ainsi que du 3 août 2026 (trois lundis) donnent des résultats identiques. Les sept variantes possibles, une par jour de la semaine :

| Jour de l'ancre | AUC logistique | AUC forêt | Écart (valeurs arrondies) | Seuil | VP / FP / FN / VN | Modèle retenu |
|---|---|---|---|---|---|---|
| lundi (27/07/2026) | 0,9700 | 0,9573 | -0,0127 | 0,833 | 23 / 12 / 7 / 333 | logistic_regression |
| mardi (21/07/2026) | 0,9696 | 0,9558 | -0,0138 | 0,832 | 23 / 12 / 7 / 333 | logistic_regression |
| mercredi (22/07/2026) | 0,9698 | 0,9602 | -0,0096 | 0,836 | 23 / 12 / 7 / 333 | logistic_regression |
| jeudi (23/07/2026) | 0,9697 | 0,9561 | -0,0136 | 0,833 | 23 / 12 / 7 / 333 | logistic_regression |
| vendredi (24/07/2026) | 0,9696 | 0,9471 | -0,0225 | 0,834 | 23 / 12 / 7 / 333 | logistic_regression |
| samedi (25/07/2026) | 0,9699 | 0,9568 | -0,0131 | 0,836 | 23 / 12 / 7 / 333 | logistic_regression |
| dimanche (26/07/2026) | 0,9698 | 0,9574 | -0,0124 | 0,834 | 23 / 12 / 7 / 333 | logistic_regression |

La matrice de confusion, la précision, le rappel, le F1 et la répartition des bandes sont identiques pour les sept jours. Les 0,9698 et 0,9574 des captures du 2 août 2026 correspondent à l'ancre d'un dimanche (rejeu du 2 août : 0,9698 et 0,9574, seuil 0,834).

Les 0,9675 et 0,9576 de l'ADR-0008 (21 juillet 2026) ne correspondent à aucun des sept jours avec le code actuel. Leur cause n'est pas établie.

Dates supplémentaires rejouées (AUC logistique / AUC forêt) : 20/07/2026 (lundi) 0,9700 / 0,9573 ; 28/07/2026 (mardi) 0,9696 / 0,9558 ; 29/07/2026 (mercredi) 0,9698 / 0,9602 ; 30/07/2026 (jeudi) 0,9697 / 0,9561 ; 31/07/2026 (vendredi) 0,9696 / 0,9471 ; 01/08/2026 (samedi) 0,9699 / 0,9568 ; 02/08/2026 (dimanche) 0,9698 / 0,9574 ; 03/08/2026 (lundi) 0,9700 / 0,9573 ; 10/10/2026 (samedi) 0,9699 / 0,9568 ; 11/10/2026 (dimanche) 0,9698 / 0,9574.

## Ce que ce rejeu ne prouve pas

Les données sont synthétiques et la cible est une règle du générateur. Le seuil et le choix du modèle sont faits sur le jeu de test de 375 lignes (30 positives) : l'AUC de 0,970 n'est pas indépendante de la sélection. Voir le dossier de projet, § 6.4.
