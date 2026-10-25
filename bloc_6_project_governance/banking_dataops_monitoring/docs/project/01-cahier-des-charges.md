# Cahier des charges

**Projet** : banking-dataops-monitoring  
**Version** : 1.0, 20 juillet 2026, précisée en octobre 2026 (tableau des exclus, § 4 ; évolutions du périmètre, § 4 bis ; contraintes, § 7 ; critère d'acceptation, § 9 ; parties prenantes, § 10)  
**Statut** : validé pour réalisation

---

## 1. Contexte

Dans une institution financière, la donnée traverse plusieurs systèmes avant
d'alimenter le reporting réglementaire, les décisions de crédit et les états
financiers. Chaque transfert est une occasion de perte, de duplication ou de
corruption silencieuse.

Le problème n'est pas l'existence d'erreurs, elle est inévitable, mais le
délai entre l'erreur et sa découverte. Une anomalie détectée à l'ingestion
se corrige en heures ; la même anomalie découverte à l'audit se corrige en
semaines, avec un effet de cascade sur tous les systèmes aval et une charge de
justification auprès du régulateur.

## 2. Objectif

Outiller une décision quotidienne, unique et traçable :

> Ce lot de données peut-il alimenter les systèmes en aval, oui ou non ?

## 3. Objectifs mesurables

| Réf | Objectif | Indicateur | Cible |
|---|---|---|---|
| OBJ-01 | Réduire le délai de détection | Écart entre ingestion et alerte | < 5 minutes |
| OBJ-02 | Rendre la décision traçable | Part des verdicts horodatés en base | 100 % |
| OBJ-03 | Rendre chaque échec exploitable | Contrôles reliés à une procédure | 100 % |
| OBJ-04 | Garantir la reproductibilité | Résultats identiques à graine et à ancre de date égales (ADR-0009) | 100 % |
| OBJ-05 | Rendre le verdict lisible sans SQL | Temps de lecture du tableau de bord | < 30 secondes |

## 4. Périmètre

### Inclus

- Génération déterministe d'un jeu de données bancaires synthétique
- Ingestion vers PostgreSQL par `COPY`
- Sept contrôles qualité SQL nommés et versionnés
- Réconciliation par système source
- Création automatique d'incidents à chaque échec
- Tableau de bord avec verdict de publication
- Procédure d'investigation documentée
- Scénario de démonstration reproductible

### Exclus

| Élément exclu | Motif | Décision |
|---|---|---|
| Traitement en flux continu | Cadence métier journalière | ADR-0001 |
| Modèle comme critère de blocage de la publication, et modèle de détection d'anomalies non supervisé au cœur du dispositif | Exigence d'explicabilité en contexte régulé | ADR-0002 (9 juin 2026). Le classifieur de priorisation n'est pas exclu : ADR-0008 (21 juillet 2026), voir § 4 bis |
| Connecteurs vers des sources réelles | Contrainte de sûreté publique du dépôt | ADR-0003 |
| Alertes et notification | Sans effet sur la démonstration de la décision | Feuille de route |
| Réconciliation entre deux systèmes indépendants | Suppose une seconde source | Feuille de route |

Au 20 juillet 2026, cette ligne s'intitulait « Détection par apprentissage automatique », sans qualificatif, parce que l'ADR-0008 n'existait pas encore. L'état exact des exclusions est : un modèle qui bloque (ADR-0002, maintenu) et un modèle d'anomalies au cœur du dispositif (ADR-0002). Un classifieur supervisé qui ordonne la file d'investigation, sans jamais bloquer, a été ajouté le 21 juillet 2026 (ADR-0008).

## 4 bis. Évolutions du périmètre après la version 1.0

Ajouts réalisés après le 20 juillet 2026, sans révision de la charge estimée (81 heures) ni du budget (annexes 03 et 04) :

| Ajout | Date et preuve |
|---|---|
| Couche Gold (schéma en étoile) et orchestrateur `run_pipeline.py` | 21 juillet 2026, CHANGELOG 2.0.0 |
| Classifieur de priorisation (`gold.predictions`) | 21 juillet 2026, ADR-0008, CHANGELOG 2.0.0 |
| Nettoyage avec quarantaine, Bronze horodaté | 21 juillet 2026, CHANGELOG 2.1.0 |
| Rapport Power BI (fichier `.pbix`, huit vues) | Commits du 2 août 2026 ; `evidence/powerbi_views.txt` |
| Space Hugging Face de secours, en lecture seule | A existé d'après l'historique du dépôt (capture de son verdict dans le commit du 2 août 2026) ; non vérifiable depuis l'archive de code |

## 5. Exigences fonctionnelles

| Réf | Exigence | Priorité |
|---|---|---|
| EF-01 | Générer un jeu de données reproductible à partir d'une graine | Haute |
| EF-02 | Charger clients, comptes et transactions en base | Haute |
| EF-03 | Exécuter les contrôles CTRL-001 à CTRL-007 à chaque passe | Haute |
| EF-04 | Persister chaque résultat avec horodatage et sévérité | Haute |
| EF-05 | Produire un verdict global PASS / WARN / FAIL | Haute |
| EF-06 | Créer un incident pour chaque contrôle en échec | Haute |
| EF-07 | Réconcilier volumes et montants par système source | Moyenne |
| EF-08 | Afficher le verdict et les indicateurs sans requête manuelle | Haute |
| EF-09 | Offrir une descente au détail sur chaque contrôle en échec | Moyenne |
| EF-10 | Permettre l'injection et le retrait d'un lot dégradé | Moyenne |

## 6. Exigences non fonctionnelles

| Réf | Exigence | Cible mesurable |
|---|---|---|
| ENF-01 | Reproductibilité | Graine identique, résultats identiques |
| ENF-02 | Démarrage à froid | Pile complète opérationnelle en moins de 2 minutes |
| ENF-03 | Portabilité | Une seule commande, aucun prérequis hors Docker |
| ENF-04 | Testabilité | Suite de tests exécutable sans base de données |
| ENF-05 | Traçabilité | Chaque contrôle porte un identifiant stable |
| ENF-06 | Sûreté publique | Aucune donnée réelle, aucun secret dans le dépôt |
| ENF-07 | Lisibilité du code | `ruff` sans avertissement |
| ENF-08 | Intégration continue | Vérification de style et tests à chaque poussée |

## 7. Contraintes

| Type | Contrainte |
|---|---|
| Réglementaire | Aucune donnée à caractère personnel réelle |
| Technique | Technologies retenues et maîtrisées : PostgreSQL, SQL, Python, pandas, scikit-learn, Docker, Streamlit, FastAPI, Power BI, Git et intégration continue (arbitrages en annexe 02) |
| Temporelle | Version démontrable livrée au 3 août 2026 (jalon J6, annexe 03) ; dépôt du dossier de certification du bloc 6 au plus tard le 1er novembre 2026 |
| Humaine | Un seul intervenant |
| Budgétaire | Aucun coût d'infrastructure engagé pendant la réalisation |

## 8. Livrables

| Réf | Livrable | État |
|---|---|---|
| LIV-01 | Dépôt de code documenté et testé | Livré |
| LIV-02 | Pile exécutable en une commande | Livré |
| LIV-03 | Tableau de bord de décision | Livré |
| LIV-04 | Documentation technique et opérationnelle | Livré |
| LIV-05 | Dossier de gestion de projet | Livré |
| LIV-06 | Dossier de projet de certification | Livré |
| LIV-07 | Support de présentation | Livré |
| LIV-08 | Scénario de démonstration reproductible | Livré |

## 9. Critères d'acceptation

Le projet est considéré comme livré lorsque :

1. `docker compose up --build` produit un tableau de bord opérationnel depuis une base vierge ;
2. les sept contrôles s'exécutent et persistent leurs résultats ;
3. l'injection du lot dégradé fait basculer le verdict au rouge et crée des incidents ;
4. la remise à zéro restaure l'état initial sans toucher aux données de référence ;
5. `ruff check .` et `pytest` passent sans échec ;
6. la présentation tient en dix minutes, démonstration comprise, et laisse dix minutes aux questions (format de l'épreuve du bloc 6).

## 10. Parties prenantes

| Rôle | Acteur | Responsabilité |
|---|---|---|
| Évaluateur | Évaluateurs de la certification, bloc 6 | Évalue la conformité au référentiel de compétences |
| Chef de projet | Auteur du dépôt | Conçoit, réalise, présente |
| Utilisateur cible | DataOps engineer, responsable qualité | Prend la décision de publication |
| Partie prenante indirecte | Auditeur, conformité | Consomme la trace produite |

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
