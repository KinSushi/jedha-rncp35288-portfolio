# Bloc 4 : analyse prédictive de données non structurées

## AT&T Spam Detector : reconnaître un SMS indésirable à son seul texte

Un détecteur de SMS indésirables (*spam*) construit par apprentissage profond : réparation des données, découpage sans fuite, deux réseaux construits et leur fonction de coût, un réseau pré-entraîné ajusté (DistilBERT), un réseau adverse (GAN) qui fabrique de nouveaux SMS, puis une évaluation unique sur un jeu de test jamais vu, avec ses intervalles de confiance.

Le code est dans [`att_spam_detector/`](att_spam_detector/) : un notebook exécuté, sorties comprises, qui raconte la démarche, et un paquet Python testé, `att_spam/`, qui porte les calculs. Chaque chiffre de cette page renvoie à la section du notebook et au compteur `In [n]` de la cellule qui l'affiche, à un fichier publié dans ce dossier, ou à une commande de la section 5 qui le reproduit.

Titre visé : Concepteur développeur en science des données (RNCP35288), bloc 4.

---

## 1. Problématique

Les abonnés d'AT&T reçoivent des SMS indésirables. L'opérateur les signalait à la main et veut un **détecteur automatique fondé uniquement sur le contenu du message** ; le livrable doit **énoncer clairement la performance atteinte** (énoncé du projet, repris en tête du [notebook](att_spam_detector/ATT_Spam_Bloc4_CDSD.ipynb)).

C'est une classification binaire de textes courts, sous deux exigences contraires : intercepter le plus de spams possible (rappel) sans bloquer de messages légitimes (précision), car un faux positif prive l'abonné d'un vrai message.

Les classes sont déséquilibrées : un modèle qui répondrait toujours « légitime » aurait 87 % d'exactitude sans rien détecter. Les indicateurs retenus sont donc la précision, le rappel, le F1, la ROC-AUC et la PR-AUC (notebook : fiche en tête, rubrique « CHOIX », et § 7).

## 2. Données

**Le fichier de données n'est pas publié ici.** Le corpus a été fourni avec l'énoncé du projet ; le droit de le redistribuer n'est pas établi. Ni le fichier, ni son adresse de téléchargement ne figurent donc dans ce dépôt.

| Élément | Valeur | Source |
|---|---|---|
| SMS lus | 5 572, en anglais, étiquetés `ham` (légitime) ou `spam` | notebook § 1, `In [2]` |
| SMS coupés aux virgules lors de l'export, recollés | 50 | idem |
| Doublons exacts retirés **avant** tout découpage | 403 | idem |
| SMS uniques | 5 169 : 4 516 légitimes, 653 spams (12,6 %) | idem |

**Obtenir les données.** Le fichier se trouve parmi les ressources de l'énoncé du projet AT&T Spam Detector. Placer le fichier sous le nom `spam.csv` dans `att_spam_detector/`, à côté du notebook. Format attendu par `charger_sms` ([`att_spam/donnees.py`](att_spam_detector/att_spam/donnees.py)) : CSV encodé en `latin-1`, colonnes `v1` (étiquette) et `v2` (texte), plus trois colonnes `Unnamed: 2` à `Unnamed: 4` qui portent les morceaux de SMS coupés aux virgules. Pour vérifier que l'on a le même fichier que celui des résultats ci-dessous : 503 663 octets, empreinte SHA-256 `440e6ea9fa825578abfdd7b7932ef8393d72ef86c0c33f64676705ce40b1dfc2`. Le [`.gitignore`](att_spam_detector/.gitignore) du projet l'exclut, pour qu'il ne soit pas publié par erreur. Sans ce fichier, le notebook ne peut pas être rejoué ; ses sorties enregistrées restent lisibles.

**Données personnelles.** Les SMS sont des communications privées ; le corpus contient des numéros, des adresses et des prénoms. Avant tout apprentissage, pour **tous** les modèles, la plupart des numéros, liens et montants sont remplacés par des jetons génériques ; les limites de cette minimisation sont écrites dans le notebook (§ 10).

## 3. Méthode

### Préparer sans fuite (notebook § 1 et § 2)

- **Réparation** : trois colonnes presque vides ne sont pas des variables mais des morceaux de SMS coupés aux virgules ; elles sont recollées. Les doublons exacts sont retirés avant le découpage.
- **Découpage par groupes de quasi-doublons.** Une campagne de spam circule en variantes qui ne diffèrent que par un numéro ou un montant : si une variante est apprise et une autre testée, le modèle reconnaît la campagne au lieu de détecter un spam nouveau. Les SMS dont les ensembles de mots ont une similarité de Jaccard d'au moins 0,8 sont regroupés de proche en proche (composantes connexes), et chaque groupe va tout entier dans un seul jeu (`StratifiedGroupKFold`, 7 plis : un pour le test, un pour la validation, cinq pour l'apprentissage).

| Contrôle du découpage | Valeur | Source |
|---|---|---|
| Groupes | 4 969 pour 5 169 SMS ; aucun groupe ne mêle spam et légitime | § 2.2, `In [5]` |
| SMS ayant au moins un quasi-doublon | 330, dont 252 spams sur 653 | idem |
| Apprentissage / validation / test | 3 691 (466 spams) / 739 (94) / 739 (93) | § 2.2, `In [6]` |
| Groupes partagés entre les jeux | **0** | idem |
| Similarité maximale entre un SMS de test et l'apprentissage | **0,786**, sous le seuil de 0,8 | idem |
| Ressemblances qui subsistent | 34 spams de test sur 93 ont un voisin d'apprentissage à une similarité d'au moins 0,5 | idem |
| Contre-épreuve : découpage stratifié au hasard, même graine | **62** groupes partagés, similarité maximale **1,000** (copies parfaites des deux côtés) | test de la contre-épreuve dans [`tests/test_chiffres_reels.py`](att_spam_detector/tests/test_chiffres_reels.py) |

- **Du texte aux tenseurs.** Les liens, montants, numéros d'au moins cinq chiffres et nombres deviennent `<url>`, `<argent>`, `<tel>` et `<nombre>` : leur valeur exacte est sans intérêt, leur présence est un signal. Le vocabulaire est appris **sur l'apprentissage seul** : 2 996 entrées ; chaque SMS devient une suite de 33 indices ; le jeu d'apprentissage est un tenseur d'entiers de forme (3 691, 33) (§ 2.3, `In [8]`).
- **Augmentation à la volée** des spams d'apprentissage (suppression de mots, permutation de deux voisins), sans changer la proportion de spams (§ 2.4). Son effet est mesuré plus bas : nul.

### Modéliser (notebook § 3 à § 6)

- **Référence non profonde** : TF-IDF et régression logistique, F1 de validation 0,947 au seuil 0,5 (§ 3, `In [10]`). Un réseau n'a d'intérêt que s'il fait mieux.
- **Fonction de coût** : entropie croisée binaire pondérée, calculée à partir du score brut (`BCEWithLogitsLoss`), avec un poids de la classe spam égal au rapport légitimes / spams, **6,92** (§ 4.1, `In [11]`). Une base sans spam est refusée, car le poids serait indéfini.
- **Deux réseaux construits** ([`att_spam/reseaux.py`](att_spam_detector/att_spam/reseaux.py)) : un **TextCNN** (convolutions 1D de largeurs 3, 4 et 5, qui captent des expressions de quelques mots où qu'elles soient) et un **BiGRU** (récurrent bidirectionnel, qui capte l'ordre et le contexte). Boucle d'apprentissage écrite à la main : Adam, lots de 64, au plus 15 époques, arrêt précoce après 3 époques sans progrès du F1 de validation, copie du meilleur état.
- **Transfer learning** ([`att_spam/bert.py`](att_spam_detector/att_spam/bert.py)) : DistilBERT (66 954 241 paramètres), tête remplacée par une sortie unique, ajusté deux époques avec la **même perte pondérée**, sur le texte normalisé (§ 5, `In [14]`).
- **Réseau adverse** ([`att_spam/gan.py`](att_spam_detector/att_spam/gan.py)) : générateur LSTM et discriminateur convolutif, entraînés sur les 466 spams d'apprentissage (vocabulaire de 916 mots, longueur 25). Choisir un mot n'est pas dérivable : le générateur passe par un Gumbel-softmax *straight-through* (un vrai mot en avant, le gradient de la distribution adoucie en arrière), et tout ce qui suit le jeton `<fin>` est masqué, pour que le discriminateur juge le contenu et non la forme. Pré-entraînement par maximum de vraisemblance (40 époques), puis 300 itérations adverses ; le générateur descend « perte adverse + vraisemblance » (§ 6, `In [15]` et `In [16]`).

### Évaluer une seule fois (notebook § 7)

Pour chaque modèle, le seuil de décision est choisi sur la **validation** (F1 maximal) ; le modèle retenu est celui qui a le meilleur F1 de validation ; chaque modèle est ensuite évalué **une seule fois** sur le test. Incertitude : intervalle de confiance à 95 % par *bootstrap* sur le F1, intervalles de Wilson sur le rappel et la précision, test exact de McNemar entre modèles ([`att_spam/evaluation.py`](att_spam_detector/att_spam/evaluation.py)).

## 4. Résultats chiffrés

Graine 42 pour tous les modèles du tableau. **Test : 739 SMS jamais vus, dont 93 spams**, sans quasi-doublon d'un SMS d'apprentissage au seuil de 0,8.

| Modèle | Seuil (validation) | F1 validation | Précision | Rappel | F1 test [IC 95 %] | Erreurs | PR-AUC |
|---|---|---|---|---|---|---|---|
| TF-IDF + régression logistique (référence) | 0,44 | 0,958 | 0,957 | 0,968 | 0,963 [0,931 ; 0,988] | 7 | 0,9815 |
| TextCNN | 0,46 | 0,926 | 0,900 | 0,968 | 0,933 [0,892 ; 0,966] | 13 | 0,9744 |
| BiGRU | 0,73 | 0,952 | 0,989 | 0,946 | 0,967 [0,938 ; 0,990] | 6 | 0,9824 |
| TextCNN + 300 SMS générés (GAN) | 0,74 | 0,924 | 0,957 | 0,946 | 0,951 [0,914 ; 0,980] | 9 | 0,9753 |
| **DistilBERT ajusté (retenu sur la validation)** | 0,91 | 0,989 | **0,989** | **0,989** | **0,989 [0,971 ; 1,000]** | **2** | **0,9978** |

Source : [`modeles/resultats_test.csv`](att_spam_detector/modeles/resultats_test.csv), écrit par la dernière cellule du notebook ; même tableau en § 7, `In [20]`.

### La performance atteinte, et pourquoi elle est optimiste

**DistilBERT intercepte 92 spams de test sur 93** (rappel 0,989, intervalle de Wilson à 95 % de 0,942 à 0,998) **et ne bloque à tort qu'un message légitime sur 646** (§ 7, `In [21]`) ; ROC-AUC 0,9996 (§ 7, `In [20]` ; `resultats_test.csv`).

Ce chiffre est le **haut de la fourchette** (§ 7, `In [22]`) :

- au seuil 0,5, le même modèle fait un F1 de test de 0,979 (4 erreurs) ; au seuil 0,81, 0,984 (3 erreurs) : le seuil de 0,91 choisi sur la validation est un maximum étroit ;
- le pli de test est favorable : la référence TF-IDF, réentraînée en prenant tour à tour chacun des 7 plis comme test, va de 0,926 à 0,973 (moyenne 0,950, écart-type 0,016), et le pli de test du notebook donne le meilleur des sept.

Lecture plus prudente, écrite dans le notebook : **un F1 attendu autour de 0,96 à 0,98** pour DistilBERT.

**DistilBERT est-il vraiment meilleur ?** Test exact de McNemar sur les messages où un seul des deux modèles se trompe (§ 7, `In [21]`) :

| Comparaison | Messages où seul DistilBERT a raison / seul l'autre | p |
|---|---|---|
| contre TF-IDF + régression logistique | 6 / 1 | 0,125 |
| contre BiGRU | 5 / 1 | 0,219 |
| contre TextCNN | 12 / 1 | 0,003 |
| contre TextCNN + SMS générés | 8 / 1 | 0,039 |

Avec la correction de Bonferroni pour quatre comparaisons (seuil 0,0125), l'avance n'est significative que contre le TextCNN seul, et pour la seule graine 42. Sur des textes courts aux signaux lexicaux très marqués, 3 691 exemples ne suffisent pas à un réseau entraîné de zéro pour dépasser nettement un bon modèle linéaire ; le pré-entraînement apporte une connaissance de l'anglais venue d'ailleurs.

### Réseaux construits : apprentissage et sur-apprentissage

| Réseau | Meilleure époque | F1 validation | F1 apprentissage | Écart | Durée (notebook publié) |
|---|---|---|---|---|---|
| TextCNN | 5 (arrêt à 8) | 0,926 | 0,994 | + 0,067 | 6 s |
| BiGRU | 6 (arrêt à 9) | 0,948 | 0,986 | + 0,038 | 18 s |
| DistilBERT | 2 (sur 2) | 0,968 | 0,987 | + 0,019 | 463 s |

Sources : § 4, `In [12]` ; § 5, `In [14]` ; écarts : § 7, `In [21]`. F1 de validation au seuil 0,5, pendant l'entraînement. Les durées suivent la charge de la machine : la première exécution affichait 31 s, 22 s et 526 s, pour les mêmes résultats (§ 5, texte sous `In [14]`).

### Le réseau adverse : qualité mesurée, aucun gain démontré

Une mesure n'a de sens que comparée à un point de repère : 500 SMS générés, contre trois corpus de comparaison (§ 6.1, `In [17]`).

| Corpus | distinct-2 | 4-grammes déjà vus dans les spams d'apprentissage | Classés spam par le TextCNN |
|---|---|---|---|
| SMS générés (GAN), 500 | 0,798 | **5,5 %** | 93,8 % |
| Vrais spams de validation, 94 | 0,650 | **20,6 %** | 93,6 % |
| Vrais spams, mots mélangés, 94 | 0,922 | 0,1 % | 91,5 % |
| Mots tirés indépendamment, 500 | 0,946 | 0,0 % | 89,4 % |

« Classés spam à 93,8 % » ne prouve rien : des mots tirés au hasard atteignent 89,4 %. La structure locale (5,5 % de 4-grammes réels) montre un début d'apprentissage, loin des vrais spams (20,6 %). **Le générateur reproduit le vocabulaire du spam, pas des messages crédibles.** Un indice de forme demeure : 77,6 % seulement des phrases générées contiennent `<fin>`, contre tous les vrais spams (§ 6, `In [16]`).

**Utilité, mesurée sur cinq graines** (§ 6.2 et 6.3, `In [18]` ; 20 entraînements du TextCNN) :

| Variante | F1 de validation moyen | Écart-type |
|---|---|---|
| Référence (avec augmentation) | 0,929 | 0,011 |
| Sans augmentation | 0,927 | 0,008 |
| + 300 SMS générés | 0,927 | 0,009 |
| + 300 vrais spams (témoin) | 0,929 | 0,011 |

Les écarts entre variantes (0,002 au plus) sont bien inférieurs au bruit d'une graine à l'autre : **ni l'augmentation ni les SMS générés n'améliorent le détecteur de façon démontrée.** Le GAN est ici une démonstration de la technique, avec sa qualité mesurée, pas un outil de production.

### Signaux détectés (notebook § 8)

Dans le modèle linéaire, les signaux de spam les plus forts sont les jetons `<tel>`, `<argent>`, `<url>` et `<nombre>`, puis « free », « text », « reply », « txt », « call » ; les messages légitimes se reconnaissent à la première personne et au registre familier. DistilBERT ne dépend d'aucun signal isolé : retirer `<tel>` ne fait repasser sous le seuil que 4 des 75 spams détectés qui le contiennent (`In [24]`). Ce sont des associations, pas des causes.

### Qualité du code

| Mesure | Résultat | Source |
|---|---|---|
| Tests (20 octobre 2026, sur la version publiée) | 260 réussis avec `spam.csv` en place (section 2), dépréciations traitées comme des erreurs ; l'un d'eux, marqué `notebook`, exécute le notebook entier (1 h 22 sur processeur) et compare toutes ses sorties, durées et SMS du corpus exceptés, à des sorties de référence | commande de la section 5 ; [`tests/`](att_spam_detector/tests/) |
| Style | `ruff`, règles E, F, I, B, SLF, UP et W, lignes de 100 caractères au plus : 0 constat | commande de la section 5 |
| Types | `mypy --strict` sur le paquet : 0 erreur | commande de la section 5 |
| Tests de mutation (`mutmut`, cinq modules du paquet) | 1 657 mutants : 1 646 tués à la première mesure, dix de plus par des tests ajoutés ensuite ; le dernier (`bias=None` au lieu de `bias=False`) ne change pas le comportement, preuve exécutée à l'appui | [`MUTANTS_EQUIVALENTS.md`](att_spam_detector/MUTANTS_EQUIVALENTS.md) |

## 5. Reproduire

Prérequis : Python 3.12, et le fichier `spam.csv` placé dans `att_spam_detector/` (section 2). Toutes les commandes se lancent depuis `att_spam_detector/`.

```bash
# Sous Linux, installer d'abord PyTorch pour processeur (la roue par défaut embarque CUDA) :
pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

Ouvrir ensuite `ATT_Spam_Bloc4_CDSD.ipynb` dans Jupyter ou un éditeur compatible (noyau Python 3.12) et exécuter toutes les cellules dans l'ordre, depuis le dossier du notebook.

- **Durée** : environ 28 minutes sur processeur (en-tête du notebook). Dans la version publiée, l'ajustement de DistilBERT prend 463 s (§ 5, `In [14]`) et l'étude sur cinq graines 644 s (§ 6.2 et 6.3, `In [18]`) ; une première exécution affichait 526 s pour DistilBERT (§ 5, texte sous `In [14]`) : les durées suivent la charge de la machine.
- **Réseau** : au premier lancement, le notebook télécharge `distilbert/distilbert-base-uncased` depuis le dépôt public Hugging Face, sans compte ; sans réseau ni copie en cache, les cellules DistilBERT échouent.
- **Déterminisme** : graine 42 pour Python, NumPy et PyTorch ; deux exécutions donnent les mêmes sorties chiffrées, durées exceptées. Les versions sont toutes épinglées dans [`requirements.txt`](att_spam_detector/requirements.txt) : une autre version de PyTorch, de scikit-learn ou de NumPy peut déplacer les chiffres.
- Les poids ajustés de DistilBERT (environ 268 Mo : 66 954 241 paramètres de 4 octets, § 5, `In [14]`) ne sont pas joints : la dernière cellule les régénère dans `modeles/distilbert_ajuste/`, que le `.gitignore` exclut.

**Tests et contrôles** :

```bash
pip install -r requirements-dev.txt
pytest -W error::DeprecationWarning -W error::PendingDeprecationWarning
ruff check --select E,F,I,B,SLF,UP,W --line-length 100 .
mypy --strict att_spam
```

`pytest` exécute aussi le notebook entier, et dure donc au moins autant qu'une exécution complète ; `pytest -m "not notebook"` laisse de côté ce seul test, marqué `notebook` dans [`pyproject.toml`](att_spam_detector/pyproject.toml). Deux fichiers de tests lisent le vrai `spam.csv` (`tests/test_chiffres_reels.py`, `tests/test_notebook.py`) : sans lui, ils échouent. Les tests de `att_spam/bert.py` utilisent un mini-DistilBERT construit localement, sans réseau.

## 6. Limites

Ce que le projet ne démontre pas, dit tel quel :

- **Décalage de domaine** : le corpus est britannique et ancien ; le spam reçu aujourd'hui par les abonnés américains d'AT&T est différent. Il faudrait réévaluer le modèle sur des SMS récents avant tout déploiement, puis le réentraîner régulièrement (notebook § 11).
- **Petit test** : 93 spams ; les intervalles de confiance se chevauchent largement et les écarts entre les meilleurs modèles ne sont pas significatifs.
- **Une seule graine** (42) pour les modèles du tableau de résultats ; cinq graines seulement pour l'étude du TextCNN ; DistilBERT n'est refait ni sur plusieurs graines ni sur plusieurs plis. Une troisième époque de DistilBERT n'est pas testée, alors que la meilleure époque est la dernière.
- **Seuil** choisi pour maximiser le F1 ; en production, il se fixerait selon le coût d'un message légitime bloqué comparé à celui d'un spam qui passe.
- **Le contenu seul**, comme l'exige l'énoncé : l'expéditeur, la fréquence d'envoi ou les signalements des abonnés amélioreraient la détection.
- **Découpage** : le seuil de similarité de 0,8 est un choix de conception, sans étude citée ; il écarte les quasi-doublons, pas toute ressemblance entre campagnes. Le notebook ne compare pas les F1 obtenus avec un découpage au hasard ; la contre-épreuve ne mesure que les groupes partagés et la similarité.
- **Minimisation imparfaite** : un numéro collé à un mot échappe à la règle des cinq chiffres ; DistilBERT, qui découpe en sous-mots, voit encore les prénoms (notebook § 10).
- **GAN** : SMS non grammaticaux, aucun gain démontré.
- **Code** : cinq petites fonctions d'affichage et d'assemblage restent dans les cellules du notebook ; elles ne sont couvertes que par le test qui exécute le notebook entier, et pas par les tests de mutation. Les lignes vont jusqu'à 100 caractères (réglage de `ruff`, section 5), au-delà des 79 recommandés par la PEP 8.
- **Non réalisé** : aucun déploiement, aucune API, aucun suivi en production. Les modèles sont seulement sauvegardés.

## 7. Organisation du dossier

```text
att_spam_detector/
|-- ATT_Spam_Bloc4_CDSD.ipynb   notebook exécuté : démarche, sorties, interprétations
|-- att_spam/                   paquet appelé par le notebook, un module par étape
|   |-- donnees.py              chargement, réparation, normalisation, quasi-doublons, découpage, tenseurs, augmentation
|   |-- reseaux.py              perte pondérée, TextCNN, BiGRU, boucle d'apprentissage
|   |-- bert.py                 DistilBERT : chargement, ajustement, probabilités
|   |-- gan.py                  générateur, discriminateur, masquage, entraînement adverse, qualité
|   `-- evaluation.py           seuil, bootstrap, Wilson, McNemar, ablation
|-- tests/                      tests du paquet, du notebook et des chiffres réels ; sorties de référence
|-- modeles/resultats_test.csv  résultats de test des cinq modèles
|-- MUTANTS_EQUIVALENTS.md      le mutant non tué, et pourquoi il est équivalent
`-- requirements.txt, requirements-dev.txt, pyproject.toml, .gitignore
```

## 8. Licence

Code, tests et notebook : licence du dépôt, voir le fichier [`LICENSE`](../LICENSE) à sa racine. Le corpus de SMS n'est pas couvert par cette licence et n'est pas publié ici ; DistilBERT, téléchargé à l'exécution, relève des conditions de son propre dépôt.

## 9. Différences avec le livrable déposé

- Les SMS du corpus ne sont pas reproduits : dans `tests/test_chiffres_reels.py` et `tests/test_notebook.py`, leur texte est remplacé par « <SMS du corpus, non reproduit> » ; les comptes et les métriques comparés sont les mêmes.
- Dans `tests/test_donnees.py`, l'adresse d'essai de la branche `co.uk` du motif d'URL est placée sous le domaine réservé `.example` ; le cas testé est le même.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
