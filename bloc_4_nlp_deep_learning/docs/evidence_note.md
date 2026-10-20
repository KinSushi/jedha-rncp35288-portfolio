# Note de preuve — Bloc 4 — Analyse prédictive de données non structurées

## Statut

Publié : le détecteur de SMS indésirables, présenté dans le [README du bloc](../README.md).

## Ce que le bloc démontre

Préparation de textes courts, réseaux de neurones entraînés de zéro (TextCNN, BiGRU), transfert d'apprentissage (DistilBERT), augmentation de données par un réseau antagoniste génératif, comparaison des modèles avec intervalles de confiance et tests appariés, et analyse des erreurs.

## Preuves

| Preuve | Chemin |
|---|---|
| Notebook exécuté | [`att_spam_detector/ATT_Spam_Bloc4_CDSD.ipynb`](../att_spam_detector/ATT_Spam_Bloc4_CDSD.ipynb) |
| Paquet (données, réseaux, GAN, DistilBERT, évaluation) | [`att_spam_detector/att_spam/`](../att_spam_detector/att_spam/) |
| Tests | [`att_spam_detector/tests/`](../att_spam_detector/tests/) |
| Résultats sur l'échantillon de test | [`att_spam_detector/modeles/resultats_test.csv`](../att_spam_detector/modeles/resultats_test.csv) |
| Obtention des données | [`README.md`](../README.md), section « Données » |

Le corpus de SMS n'est pas redistribué : les sorties du notebook qui recopiaient un SMS du corpus sont remplacées par un marqueur.
