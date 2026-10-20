# ADR-0009 — Sélection hors du jeu de test, bandes ancrées sur le seuil, ancre de date fixe

**Date** : 11 octobre 2026 · **Statut** : acceptée · **Complète** l'ADR-0008 (section « Précisions d'octobre 2026 ») et l'ADR-0003

## Contexte

Une relecture indépendante du dossier de projet (11 octobre 2026) a rejoué le code à
l'ancre du 27 juillet 2026 (`evidence/rejeu_modele_2026-07-27.md`) et mis trois défauts
en évidence. L'ADR-0008 les avait décrits sans les corriger. Ils sont corrigés ici.

1. **Le jeu de test servait à choisir.** Dans la version du 3 août 2026 (commit
   `09c4f2d`), `train.py` calculait l'AUC des deux modèles sur `y_test`, appliquait la
   marge de 0,01 à ces deux AUC, puis choisissait le seuil sur les scores du même jeu. Les
   mêmes 375 transactions (dont 30 positives) choisissaient donc le modèle, choisissaient
   le seuil et annonçaient la performance : l'AUC de 0,970, la précision, le rappel et le F1
   n'étaient pas des mesures indépendantes de la sélection.
2. **La bande « high » était inatteignable.** `predict.py` la plaçait à 1,30 fois le seuil.
   Avec un seuil de 0,8335, la borne valait 1,0835, au-dessus du maximum d'une probabilité
   (score maximal observé : 0,9994). Sur les 1 500 transactions : 0 *high*, 227 *medium*,
   1 273 *low*.
3. **Les données dépendaient du jour d'exécution.** `run_pipeline.py` ancrait les
   horodatages à minuit UTC du jour d'exécution. Le jour de la semaine, donc `weekday_name`
   et `is_weekend`, deux variables du modèle, changeait avec lui : l'AUC et le seuil variaient
   selon la date (sept variantes mesurées). L'ADR-0003 affirmait que « la même graine
   reproduit exactement les mêmes lignes » : c'était faux sans ancre fixe.

## Décision

**1. Sélection du modèle et du seuil sur l'apprentissage seul.**

- Le découpage, la graine (42) et la grille de la forêt ne changent pas : 75 % / 25 %
  stratifié, 1 125 lignes d'apprentissage, 375 de test.
- Les deux candidats sont comparés par **validation croisée stratifiée à 4 plis sur
  l'apprentissage**, avec les mêmes plis. La forêt (optimisée dans ces plis par la
  recherche sur grille) n'est retenue que si son AUC de validation croisée dépasse celui
  de la régression logistique d'au moins 0,01, la marge de l'ADR-0008.
- Le **seuil** est celui qui maximise le **F1** sur les **prédictions hors-pli** de
  l'apprentissage (chaque ligne est notée par un modèle qui ne l'a pas vue). Le critère est
  inchangé, mais il est désormais écrit : `threshold_criterion = "max_f1"` et
  `threshold_source` figurent dans le rapport. Le seuil est arrondi à trois décimales, comme
  la colonne `gold.predictions.decision_threshold`, et utilisé sous cette forme partout :
  le seuil lu à côté d'un score est exactement celui qui l'a classé.
- Le **jeu de test est évalué une seule fois**, une fois le modèle et le seuil figés. Les
  AUC de test des deux candidats sont publiés pour information ; aucune décision ne les lit.
- `select_model` et `out_of_fold_threshold` ne reçoivent aucune donnée de test : la
  signature est la garantie, et un test vérifie qu'altérer les variables des lignes de test
  ne change ni le modèle retenu, ni les AUC de validation croisée, ni le seuil.

**2. Bandes de priorité ancrées sur le seuil de décision.**

| Bande | Condition | Sens |
|---|---|---|
| *high* | score ≥ seuil | transaction prédite suspecte, à investiguer en premier |
| *medium* | 0,60 × seuil ≤ score < seuil | zone grise, à voir après les *high* |
| *low* | score < 0,60 × seuil | pas de priorité |

*high* égale désormais `predicted_suspicious`. Le coefficient 0,60 est conservé de la
version précédente (il bornait déjà *medium*). `priority_band` refuse un score hors de
[0, 1], NaN compris, et un seuil hors de ]0, 1] : un NaN, faux contre toutes les bornes,
serait sinon rangé en *low*, la plus discrète des erreurs.

**3. Ancre de date explicite et fixe.**

- Ordre de priorité : option `--generated-at` de `run_pipeline.py` et de
  `generate_synthetic_data`, puis variable d'environnement `BANKING_DATAOPS_GENERATED_AT`,
  puis valeur par défaut **`2026-07-27T00:00:00Z`**. C'est la seule date, parmi 911 testées,
  qui reproduit l'empreinte de lot `89ac138fb9943f00` de `evidence/pipeline_full.txt`
  (un test le vérifie).
- Dans le conteneur, `docker-compose.yml` transmet `BANKING_DATAOPS_GENERATED_AT` (prise dans
  l'environnement de l'hôte ou le fichier `.env`) au générateur et à `run_pipeline.py`. Le point
  d'entrée `docker/entrypoint.sh` accepte aussi `--generated-at VALEUR`, qui l'emporte sur la
  variable, et refuse toute autre option ; `.\run.ps1 start -GeneratedAt today` renseigne la
  variable pour Compose. `tests/test_entrypoint_anchor.py` exécute le script réel avec des
  doublures de `python`, `uvicorn` et `streamlit`.
- Le mot-clé `today` rétablit l'ancien comportement (minuit UTC du jour), non reproductible
  d'un jour à l'autre.
- Une ancre qui n'est pas une date est refusée : `run_pipeline.py` s'arrête avec le code 2
  avant toute étape, le générateur lève sans rien écrire. Elle n'est jamais remplacée en
  silence par l'horloge.
- Le défaut de `SyntheticConfig.generated_at` reste `None` (horloge) pour les appelants de
  la bibliothèque, car un test verrouille ce comportement et la défaillance qu'il provoque ;
  les deux points d'entrée en ligne de commande ne l'utilisent jamais.

## Alternatives écartées

**Pour la sélection.**

- *Conserver le choix sur le jeu de test et le déclarer* (état documenté par l'ADR-0008) :
  honnête mais laisse une performance annoncée non indépendante.
- *Un jeu de validation distinct, fixe* : l'apprentissage compte environ 91 positives
  (121 au total, 30 en test) ; un jeu de validation de 20 % en contiendrait moins de 20, trop
  peu. La validation croisée réutilise toutes les lignes d'apprentissage.
- *Validation croisée imbriquée* : la meilleure forêt est la meilleure de 12 combinaisons,
  donc légèrement optimiste. Le biais joue en faveur de la forêt, si bien que la victoire de
  la régression logistique reste solide ; l'imbriquée a été jugée disproportionnée pour
  1 125 lignes. L'ampleur du biais n'a pas été mesurée.
- *Seuil à 0,5, ou fixé par une matrice de coûts* : 0,5 suppose des classes équilibrées ;
  aucune matrice de coûts n'existe pour pondérer un cas manqué contre une heure d'analyste.
  Le F1 reste le compromis neutre, et il est conservé.

**Pour les bandes.**

- *Abaisser le multiplicateur de la bande haute* : un coefficient ne convient qu'à un seuil
  donné ; il redeviendrait inatteignable après un réétalonnage au-dessus de 1/coefficient.
- *Bandes par rang* (les 10 % les plus hauts) : elles répondent à la capacité de l'analyste,
  pas à la confiance du modèle, et étiquetteraient *high* 10 % des transactions même dans un
  lot où rien ne paraît suspect.
- *Deux bandes seulement* : la bande *medium* garde son utilité mesurée. Sur le jeu de test,
  lire tout ce qui dépasse 0,60 fois le seuil (0,511) donne un rappel de 0,90 pour une
  précision de 0,435, contre 0,70 et 0,656 pour la seule bande *high*.

**Pour l'ancre.**

- *Variable d'environnement seule, ou option seule* : l'une convient à Docker et à la CI,
  l'autre à l'usage interactif ; les deux coûtent peu.
- *Conserver `datetime.now()`* : les chiffres publiés ne seraient reproduits qu'un lundi.
- *Fixer l'ancre dans `SyntheticConfig`* : casserait un test qui documente le défaut et la
  compatibilité de la bibliothèque.

## Conséquences

**Mesures avant et après** (ancre du 27 juillet 2026, jeu de test de 375 transactions dont
30 positives, source : `evidence/rejeu_modele_2026-07-27.json` pour « avant » et
`evidence/model_metrics.json` pour « après »).

| | Avant (sélection sur le test) | Après (sélection hors test) |
|---|---|---|
| AUC validation croisée, logistique / forêt | non calculé (0,9748 / 0,9661 au rejeu) | 0,9748 / 0,9661 |
| Modèle retenu | régression logistique | régression logistique |
| Seuil | 0,8335 (0,833) | 0,852 |
| AUC du test | 0,970 | 0,970 |
| Précision / rappel / F1 | 0,657 / 0,767 / 0,708 | 0,656 / 0,700 / 0,677 |
| Matrice VP / FP / FN / VN | 23 / 12 / 7 / 333 | 21 / 11 / 9 / 334 |
| Bandes sur 1 500 lignes | 0 *high*, 227 *medium*, 1 273 *low* | 126 *high*, 99 *medium*, 1 275 *low* |

L'AUC de test ne change pas : le modèle retenu est le même et le test n'a jamais servi à
l'entraîner. Le rappel et le F1 baissent parce que l'ancien seuil, choisi sur le test, était
ajusté à ces 30 positives : la part de l'écart due à ce réglage n'est pas isolable d'un
simple changement de seuil sur 30 cas. Le F1 de 0,677 est la mesure à retenir.

**Positives.** Les chiffres de performance sont indépendants de la sélection. Les trois
bandes sont atteignables et *high* se lit comme « prédite suspecte ». Une relance donne les
mêmes données, donc les mêmes chiffres, quel que soit le jour.

**Négatives.** 375 lignes et 30 positives gardent une incertitude large (intervalle à
95 % de l'AUC de test : [0,951 ; 0,985] au rejeu du 27 juillet). Le F1 hors-pli maximal à
trois décimales est légèrement sous le maximum exact (0,708 contre 0,712 sur les
prédictions hors-pli). Les 0,9675 et 0,9576 de l'ADR-0008 (21 juillet) restent inexpliqués.
**Effet de bord de l'ancre fixe : le contrôle de fraîcheur.** CTRL-007 (avertissement, 60 jours)
compare le dernier événement à l'horloge, pas à l'ancre du lot. Avec l'ancre par défaut, le dernier
événement date du 26 juillet 2026 à 23 h 02 : le contrôle passe en *WARN* à partir du 25 septembre 2026,
le verdict de publication devient *WARN* (`can_publish` reste vrai) et le tableau de bord affiche
« Release under reservation » au lieu du bandeau vert. C'est le cas à la date de cette décision :
la pile Docker du 11 octobre 2026 donne 6 contrôles réussis, 1 avertissement
(`evidence/vw_publication_verdict.txt`). Pour une démonstration, `--generated-at today` redonne un
lot frais et un verdict *PASS* 7/7 (`evidence/variante_ancre_aujourdhui/`). Faire mesurer la fraîcheur
par rapport à l'ancre serait un changement de contrôle, qui n'est pas pris ici.

**Ce qui ne change pas.** Le verdict de publication reste décidé par les contrôles
déterministes ; aucun score n'y entre (ADR-0002). La cible reste une règle du générateur.

**Documents alignés** : ADR-0008, ADR-0003, `docs/KPI_DEFINITIONS.md`,
`docs/data_dictionary.md`, dossier de projet § 6.4, registre des risques, `CHANGELOG.md`,
`evidence/README.md`. Les preuves de `evidence/` ont été régénérées avec la pile Docker.
