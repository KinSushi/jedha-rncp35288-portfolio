# ADR-0008 — Classifieur de priorisation, jamais de blocage

**Date** : 21 juillet 2026 · **Statut** : acceptée · **Précisée en octobre 2026** (section finale) · **Complétée par l'ADR-0009** (11 octobre 2026), qui corrige les trois défauts que cette section décrit

## Contexte

L'ADR-0002 écartait tout modèle comme critère de blocage, tout en prévoyant
explicitement son usage « en complément, comme signal d'alerte pour la revue
humaine ». Le cadrage pédagogique impose par ailleurs une composante
d'apprentissage supervisé avec scikit-learn et un modèle persisté en `.joblib`.

Il fallait donc introduire un modèle **sans contredire** la décision initiale.

## Options

**Un modèle qui bloque.** Contredit frontalement l'ADR-0002 et rend la décision
inopposable à un auditeur.

**Un modèle décoratif**, entraîné et affiché mais sans rôle. Honnête sur le
papier, mais un artefact que personne n'utilise finit par ne plus être
maintenu — et un jury le voit.

**Un modèle qui priorise.** Il ordonne une file d'investigation. Le blocage
reste déterministe.

## Décision

Classifieur supervisé de la cible `is_suspicious`, produisant une file
d'investigation écrite dans `gold.predictions`. Le code définit trois bandes de
priorité (*high*, *medium*, *low*) ; avec le seuil mesuré le 3 août 2026, **deux seulement étaient
atteignables** (*medium* et *low*), car la bande *high* commençait au-dessus de 1
(voir la section finale).

**État actuel.** L'ADR-0009 (11 octobre 2026) ancre la bande *high* sur le seuil de décision : les trois bandes sont
atteignables (126 *high*, 99 *medium*, 1 275 *low* sur 1 500 transactions).

Aucun chemin de code ne fait dépendre le verdict de publication d'un score.

## Problème résolu en chemin : la fuite de cible

Le générateur dérivait `is_suspicious` d'un seuil sur `risk_score`. Un modèle
entraîné sur `risk_score` aurait donc obtenu une performance parfaite sans rien
apprendre — fuite de cible manuelle. À l'inverse, en excluant `risk_score`, la
donnée ne contenait aucun signal apprenable : `risk_score` était un tirage
aléatoire.

Deux corrections :

1. `risk_score` est interdit de features, par une liste explicite et une
   assertion qui lève — pas par un commentaire.
2. Le générateur injecte désormais des facteurs de risque plausibles et
   documentés, à la manière d'un moteur de scoring AML : activité nocturne,
   transfrontalier, montant élevé, canal, catégorie marchande, plus un bruit
   irréductible.

Le modèle doit retrouver ce motif à partir des variables observables. Il y
parvient en grande partie : parmi les douze poids les plus forts de la régression
logistique, dix correspondent à des facteurs injectés (nuit, transfrontalier,
canal, catégorie marchande). Deux ne le sont pas (des jours de la semaine, que le
générateur ignore) et le drapeau « montant élevé », injecté, a un poids proche de
zéro : le modèle ajuste aussi du bruit d'échantillon.

**Cadrage assumé** : sur données synthétiques, le label vient d'une règle. Sur
données réelles, il viendrait d'investigations confirmées. Le modèle démontre la
méthode et la chaîne, pas un taux de détection prêt pour la production. Le
dossier le dit plutôt que de le laisser croire.

## Sélection du modèle

Deux candidats comparés sur le même découpage : régression logistique en
référence, forêt aléatoire optimisée par recherche sur grille.

**La forêt n'est publiée que si elle dépasse la référence de 0,01 d'AUC.**
En deçà, le modèle simple et lisible est retenu.

Le 21 juillet 2026, la référence gagne — AUC 0,9675 contre 0,9576 (chiffres du
jour, conservés tels quels). Les exécutions suivantes donnent 0,970 contre 0,9573
(`evidence/pipeline_full.txt`, jeu généré avec l'ancre du 27 juillet 2026) : le
choix est le même. Le projet livre donc la régression logistique, et la raison de
ce choix est journalisée et affichée dans le tableau de bord.

Ce n'est pas une clause de style : payer en opacité un gain qui tient dans le
bruit d'un découpage est un mauvais échange. Un analyste lit des coefficients ;
il ne lit pas trois cents arbres.

## Conséquences

**Positives** : l'ADR-0002 est appliqué plutôt que contredit. Le seuil de
décision est choisi sur le compromis précision/rappel, pas laissé à 0,5, et il
est stocké avec chaque prédiction — une décision passée reste lisible après un
réétalonnage.

**Négatives** : une dépendance supplémentaire, un artefact à versionner, et un
risque de dérive qui n'est aujourd'hui pas mesuré. Le réentraînement n'est pas
automatique, précisément pour qu'il reste un acte conscient.

**Garde-fou** : un modèle sous 0,70 d'AUC n'est pas publié. Un invariant lève.
Envoyer des analystes enquêter au hasard en affichant un score de confiance
serait pire que ne rien fournir.

## Précisions d'octobre 2026 : l'état du code au 3 août 2026

Ces précisions ne changent pas la décision. Elles décrivent trois défauts constatés dans le code du 3 août 2026, avec
des chiffres rejoués sur le jeu généré avec l'ancre du 27 juillet 2026
(`evidence/rejeu_modele_2026-07-27.json`). **Les trois sont corrigés par l'ADR-0009** (voir la dernière section) ;
les chiffres de cette section sont ceux d'avant la correction.

**Deux niveaux de priorité atteignables, pas trois.** `predict.py` place la bande
*high* à 1,30 fois le seuil. Avec un seuil de 0,833, elle commence à 1,0835 : au-dessus
du maximum d'une probabilité. Sur les 1 500 transactions notées, le score maximal est
0,9994 ; 227 sont *medium*, 1 273 sont *low*, aucune n'est *high*. Le défaut est
décrit, non corrigé.

**Le seuil et le choix du modèle sont faits sur le jeu de test.** Dans `train.py`,
l'AUC de la régression logistique (ligne 823) et celle de la forêt (ligne 896) sont
calculées sur `y_test`, la marge de 0,01 les compare (lignes 913 à 917) et le seuil
maximisant le F1 est choisi sur les mêmes scores (ligne 984). L'AUC annoncée
(0,970 sur 375 transactions, dont 30 positives) n'est donc pas une mesure
indépendante de la sélection. Contrôles hors du jeu de test : en validation croisée
sur l'entraînement seul, 0,9748 pour la régression logistique contre 0,9661 pour la
meilleure forêt : même décision ; avec un seuil choisi hors du jeu de test, la
précision est de 0,656, le rappel de 0,70 et le F1 de 0,677, contre 0,657, 0,767 et
0,708.

**Les chiffres dépendent du jour de l'ancre.** `run_pipeline.py` ancre les
horodatages à minuit UTC du jour d'exécution : le jour de la semaine, donc deux
variables du modèle, change avec lui. Sur les sept jours possibles, l'AUC de la
régression logistique varie de 0,9696 à 0,9700 et celle de la forêt de 0,9471 à
0,9602 ; la matrice de confusion (23 / 12 / 7 / 333), le choix du modèle et la
répartition des bandes sont identiques. Les 0,9675 et 0,9576 du 21 juillet ne
correspondent à aucun de ces sept jours avec le code actuel : leur cause n'est pas
établie.

**Comparaison à des références mesurées.** L'ordre d'arrivée donne une AUC de 0,45 à
0,49 sur le jeu de test (niveau du hasard) ; une règle simple (nombre de facteurs
parmi nuit, transfrontalier, montant élevé) donne 0,811 ; le modèle, 0,970.


## Suite donnée — 11 octobre 2026

Les trois défauts décrits ci-dessus sont corrigés par l'[ADR-0009](0009-selection-hors-test-bandes-et-ancre.md) :
le modèle et le seuil sont choisis sur l'apprentissage seul (validation croisée stratifiée à
4 plis, seuil hors-pli), le jeu de test n'est évalué qu'une fois ; la bande *high* commence
au seuil de décision et devient atteignable ; l'ancre de date est fixe
(`2026-07-27T00:00:00Z` par défaut). Le texte ci-dessus reste tel qu'il a été écrit : il
décrit l'état du code au 3 août 2026. Les numéros de ligne de `train.py` qu'il cite
désignent cette version (commit `09c4f2d`).
