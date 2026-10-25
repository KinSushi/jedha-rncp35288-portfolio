# Standard de code

Ce que ce dépôt exige de lui-même. Un standard qui n'est pas vérifié par un test est une intention, pas un standard : chaque règle ci-dessous est donc rattachée au mécanisme qui la fait respecter.

---

## 1. Les quatre modes de défaillance inacceptables

| Mode | Définition | Où il est fermé |
|---|---|---|
| Fuite | Une opération atteint des données hors de sa portée | `tests/test_reverse_paths.py`, section LEAK |
| Échec silencieux | Une erreur est absorbée et l'appelant croit avoir réussi | section SILENT CRASH |
| Corruption d'état | Un invariant est rompu et l'exécution continue | section STATE CORRUPTION |
| Anticipation | Une décision utilise une information qu'elle ne devrait pas avoir | section LOOK-AHEAD |

Un test avant prouve que le chemin nominal fonctionne. Un reverse test prouve que le chemin interdit ne peut pas être emprunté, et que le refus est explicite.

---

## 2. Journalisation

Un contrôle qui échoue sans laisser de trace est pire que pas de contrôle du tout : il produit une décision que personne ne peut justifier ensuite.

| Règle | Mise en œuvre |
|---|---|
| Chaque étape déclare ce qu'elle a fait, sur combien de lignes, en combien de temps | `RunJournal`, dans `audit.py` |
| Chaque enregistrement porte un identifiant d'exécution | `RunContextFilter`, propagé par `contextvars` |
| Deux formats, une seule structure | `HumanFormatter` pour l'écran, `JsonFormatter` pour l'audit |
| La journalisation ne lève jamais | `SafeHandler`, vérifié par un reverse test |
| Une bibliothèque ne configure jamais la journalisation | `configure_logging` n'est appelée que depuis un `main()` |

Le dernier point mérite d'être explicité : une bibliothèque qui configure la journalisation confisque une décision qui appartient à l'application. Elle ne le fait pas.

Pourquoi le `SafeHandler` avale les exceptions. C'est une entorse assumée à la règle « ne jamais avaler une exception ». Un tuyau rompu ou un disque plein ne doit pas interrompre un traitement de données : l'observabilité est subordonnée au travail qu'elle observe, jamais l'inverse. L'entorse est couverte par un test qui vérifie que l'absorption a bien lieu.

---

## 3. Audit

La journalisation répond à « que se passe-t-il » pendant l'exécution. L'audit répond à « que s'est-il passé, et pourquoi » des mois plus tard, en SQL, à quelqu'un qui n'était pas là.

### Structure d'un événement

| Champ | Rôle | Contrainte |
|---|---|---|
| `run_id` | Corrèle tous les événements d'une exécution | 16 caractères hexadécimaux |
| `category` | Liste fermée de six valeurs | `CHECK` déclaratif en base |
| `action` | Ce qui s'est produit, au passé | Non vide |
| `reason` | Pourquoi, en termes métier | Non vide, refusé sinon |
| `row_count` | Combien de lignes affectées | Positif ou nul |
| `details` | Contexte structuré libre | `jsonb` |

Le champ `reason` est refusé s'il est vide. Un journal qui enregistre le quoi sans le pourquoi est un log, pas une piste d'audit. C'est la seule ligne du standard qui produit une exception à la construction.

### Double écriture, délibérée

L'événement part toujours dans le log structuré, qui survit à une panne de base. Il part au mieux dans la table `audit_events`, qui survit à un redémarrage de conteneur. Si l'écriture en base échoue, l'événement reste dans le log et le traitement continue.

L'inverse rendrait l'observabilité capable de casser le travail qu'elle observe.

### Invariants

`assert_invariant` journalise aussi les succès. Un invariant vérifié en silence est indiscernable d'un invariant jamais exécuté : l'auditeur doit pouvoir prouver que le contrôle a eu lieu, pas seulement qu'il n'a pas échoué.

Une violation lève. Un état corrompu qui continue de circuler produit un verdict auquel personne ne peut se fier.

---

## 4. Reproductibilité

| Garantie | Preuve |
|---|---|
| Même graine, mêmes octets | `test_the_same_seed_gives_byte_identical_files_another_seed_does_not_and_a_missing_file_has_no_fingerprint` |
| Graines différentes, données différentes | le même test (`tests/test_reproducibility.py`) |
| L'empreinte ne dépend pas de l'ordre du système de fichiers | `test_the_directory_fingerprint_depends_on_names_not_on_order_and_the_anchor_removes_the_clock_leak` |
| L'empreinte couvre les noms, pas seulement le contenu | le même test (`tests/test_reproducibility.py`) |

Un défaut réel trouvé par ces tests. Le générateur appelait `datetime.now()` à trois endroits et écrivait l'horloge murale dans les données. Deux exécutions avec la même graine produisaient donc des fichiers différents, et la reproductibilité affirmée par le dossier était fausse.

Correction : un ancrage temporel injectable, `SyntheticConfig.generated_at`. Par défaut `None`, ce qui conserve le comportement antérieur ; renseigné, il rend la sortie identique à l'octet près. Deux tests verrouillent le défaut et sa correction : l'un assertant que la fuite existe sans ancrage, l'autre qu'elle disparaît avec.

Effet secondaire du correctif : une seule lecture d'horloge au lieu de trois. Trois lectures pouvaient enjamber minuit et produire un jeu de données dont la création de compte est postérieure à ses propres transactions.

---

## 5. Sécurité des requêtes

| Règle | Mise en œuvre |
|---|---|
| Toute valeur externe voyage en paramètre lié | `fetch_rows(..., params=...)` |
| Aucune interpolation de chaîne dans une requête | Reverse test sur `_safe_run_id` |
| La validation est une liste blanche | Un identifiant est hexadécimal, rien d'autre n'est accepté |
| Validation et liaison | Les deux, dans cet ordre |

Le dernier point est un choix. La validation seule serait une ligne de défense unique. La liaison seule accepterait un identifiant malformé et renverrait un résultat vide au lieu de le signaler. Les deux, en commençant par la validation.

Une faille réelle corrigée : `fetch_rows` n'acceptait aucun paramètre, ce qui imposait l'interpolation à tout appelant. Le paramètre `params` a été ajouté de façon rétrocompatible.

---

## 6. Documentation dans le code

Chaque module public répond à quatre questions, dans son docstring :

| Question | Ce qu'elle recouvre |
|---|---|
| POURQUOI ce module existe | Le problème métier, pas la fonction technique |
| COMMENT il s'y prend | Le mécanisme, et les alternatives écartées |
| OÙ il est appelé | Les appelants légitimes, et les illégitimes |
| CE QUI EST GARANTI | Les invariants que l'appelant peut tenir pour acquis |

Les chemins interdits sont énumérés dans le docstring du module, avec le renvoi vers le test qui les ferme. Un chemin interdit non testé n'est pas interdit, il est déconseillé.

### Ce qui est commenté et ce qui ne l'est pas

Un commentaire explique pourquoi, jamais quoi. `# incrémente le compteur` au-dessus de `count += 1` est du bruit. `# une seule lecture d'horloge : trois pouvaient enjamber minuit` est de l'information.

Les seuils métier sont toujours commentés avec leur origine. Un `0.7` sans explication devient, en six mois, un nombre que personne n'ose changer.

---

## 7. Échecs

| Situation | Comportement exigé |
|---|---|
| Entrée invalide | Lever à la construction, avec un message actionnable |
| Dépendance absente | Lever, avec la commande qui la fournit |
| Action inconnue en ligne de commande | Refuser, jamais retomber sur un défaut destructif |
| Clic d'interface qui échoue | Message maîtrisé, jamais de trace d'exception |
| Invariant rompu | Lever, arrêter le traitement |
| Écriture d'audit impossible | Avertir, continuer |

Un message d'erreur actionnable contient la commande à taper. `No account found` est un constat ; `No account found. Run: docker compose exec app python -m banking_dataops.ingest` est une réparation.

---

## 8. Vérification du standard

```bash
make lint            # ruff, aucun avertissement toléré
make test            # suite pytest (comptes datés dans evidence/01_pytest_summary.txt), dont 13 cas de reverse tests
make smoke           # 15 assertions de bout en bout
```

| Contrôle | Ce qu'il garantit |
|---|---|
| `ruff check .` | Style, imports inutilisés, complexité |
| `pytest tests/test_reverse_paths.py` | Les quatre modes de défaillance sont fermés |
| `pytest tests/test_reproducibility.py` | La reproductibilité est mécanique, pas déclarative |
| Intégration continue | Les deux à chaque poussée |

---

## 8 bis. Couverture réelle de la journalisation

Un standard qui prétend s'appliquer partout sans le mesurer n'est pas vérifiable.
Voici l'état exact, mesuré, pas affirmé.

### Modules journalisés : tout ce qui change un état

| Module | Journal | Événements | Invariants |
|---|---|---|---|
| `ingest.py` | oui | oui | oui, silver non vide |
| `quality_checks.py` | oui | un par contrôle, avec motif métier | oui, un résultat par contrôle déclaré |
| `pipeline/cleaning.py` | oui | oui | oui, taux de quarantaine sous 5 % |
| `pipeline/gold.py` | oui | oui | oui, Gold égal à Silver |
| `ml/train.py` | oui | oui | oui, AUC minimale, équilibre des classes |
| `ml/predict.py` | oui | oui | oui, un score par transaction |
| `demo_scenario.py` | oui | oui | oui, reset n'atteint que les lignes marquées |
| `monitoring.py` | traçage en lecture | non, délibérément | - |

Pourquoi `monitoring.py` ne produit pas d'événement d'audit. Lire n'est pas
un acte qui change l'état. Inonder `audit_events` de rendus de page noierait les
événements qui comptent. Les requêtes sont en revanche tracées au niveau
`DEBUG`, et une réponse vide déclenche un `WARNING` : un panneau vide a deux
causes très différentes, et sans trace elles sont indiscernables.

### Modules non journalisés, et pourquoi

| Module | Raison |
|---|---|
| `config.py` | Chargement de configuration, exécuté avant que la journalisation ne soit configurée |
| `db.py` | Couche d'accès pure ; journaliser ici produirait une ligne par requête, sans contexte métier |
| `generate_synthetic_data.py` | Appelé par `run_pipeline.py`, qui journalise l'étape et l'empreinte du résultat |
| `powerbi_views.py` | Création de vues idempotente, sans effet de bord observable |
| `reconciliation.py` | Corrigé : journal, invariant sur lot vide, événement nommé `coherence_summary` et non `reconciled`, pour que la piste d'audit ne prétende pas plus que le code |
| `incident_report.py` | Corrigé : écrit un fichier, pas une table ; journalisé car un rapport dont personne ne sait qu'il existe n'est pas lu |
| `powerbi_views.py` | Corrigé : journal et invariant : une vue manquante se manifeste par un panneau vide, jamais par une erreur |
| `api.py` | Corrigé : middleware traçant chaque requête, identifiant d'exécution propre par appel, renvoyé au client dans `X-Run-Id` |

Les quatre entrées listées en dette lors de la passe précédente ont été traitées.
Nommer une dette n'est pas la traiter : le critère demandait des journaux
partout, pas un inventaire des endroits où il n'y en a pas.

Restent exemptés, et c'est délibéré : `config.py`, chargé avant que la
journalisation ne soit configurée, et `db.py`, couche d'accès pure où une ligne
par requête produirait du bruit sans contexte métier : ces deux-là sont exclus
explicitement dans le garde-fou.

## 9. Ce que ce standard ne couvre pas

Par honnêteté, et parce qu'un standard qui prétend tout couvrir n'est pas crédible.

| Absent | Conséquence |
|---|---|
| Tests d'intégration contre une vraie base | Les tests sont unitaires ; le test de fumée comble partiellement |
| Tests de charge | Aucun chiffre au-delà de 1 500 lignes |
| Mesure de couverture | Le nombre de tests est connu, la couverture ne l'est pas |
| Analyse de sécurité des dépendances | Aucun `pip-audit` en intégration continue |
| Tests de mutation systématiques | Pratiqués ponctuellement (voir ci-dessous) mais pas automatisés |

Les deux derniers sont les plus rentables à ajouter, dans cet ordre.

### Mutation vérifiée manuellement

Le garde-fou `test_every_state_changing_module_is_journalled_and_the_scan_refuses_a_module_that_leaves_no_trace`
a été validé par mutation : `RunJournal` retiré de `pipeline/gold.py`, test relancé. Cette validation est maintenant
dans le test lui-même : le balayage est une fonction qui doit refuser des sources volontairement fautives
(module sans journal, journal sans événement, écriture sans trace, import résiduel).

Première version : le test est resté vert. Il acceptait `RunJournal` *ou*
`record_event`, et le module conservait le second. Un garde-fou qui ne voit pas
la panne qu'il est censé voir.

Corrigé pour exiger les deux : un module peut émettre des événements isolés tout
en ayant perdu son cadre d'exécution, et ces événements n'ont alors plus de run
auquel se rattacher. Revérifié par mutation : le test échoue désormais, puis
repasse au vert une fois le code restauré.

C'est l'illustration littérale de la règle du dépôt : **un test qu'on n'a jamais
vu échouer une fois n'est pas un test.**

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
