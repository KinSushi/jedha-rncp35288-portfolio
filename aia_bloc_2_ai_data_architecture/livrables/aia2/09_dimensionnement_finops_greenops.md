# 09 - Dimensionnement, FinOps et GreenOps

> Répond aux indicateurs 1.1, 1.2, 1.3, 5.1, 5.2, 8.1 et 8.2.
> **Méthode** : on part de **mesures** réelles (PostgreSQL 16, démonstration), on les multiplie par des **hypothèses** nommées H1, H2… dont le registre est au § 2. Aucun volume ni aucun coût n'est présenté comme un fait quand il est supposé. Les prix unitaires sont relevés, datés et sourcés au § 5.5 (liste publique d'AWS) ; ils changent : refaire le relevé le jour de la décision.
> Calculs reproductibles : `python -m stripe_demo.dimensionnement` ; mesures : `python -m stripe_demo.mesures postgres|demo`.

## 1. Mesures réelles

### 1.1 Occupation disque dans PostgreSQL 16 (48 000 paiements, après `VACUUM ANALYZE`)

| Table | Lignes | Tas (octets) | Index (octets) | Total (octets) | Octets par ligne |
|---|---|---|---|---|---|
| `paiements` | 48 000 | 6 168 576 | 8 126 464 | 14 327 808 | 298,5 |
| `clients` | 2 000 | 253 952 | 65 536 | 352 256 | 176,1 |
| `moyens_paiement` | 2 000 | 196 608 | 221 184 | 450 560 | 225,3 |
| `litiges` | 1 206 | 90 112 | 98 304 | 221 184 | 183,4 |
| `remboursements` | 1 364 | 106 496 | 49 152 | 188 416 | 138,1 |

Journal de transactions (WAL) produit par le chargement complet : 31 022 552 octets pour 48 000 paiements, soit **646,3 octets par paiement** (toutes tables confondues, référentiels compris). Chargement : 12,2 s sur un poste de travail.

Coût disque d'un paiement dans l'OLTP en tenant compte de ses éventuels remboursements et litiges (pondérés par leur fréquence mesurée) : **307,0 octets**, dont environ 57 % d'index (169 octets sur 298 pour la table `paiements` seule).

### 1.2 Démonstration autonome (SQLite + DuckDB + modèle)

| Mesure | Valeur |
|---|---|
| Pic de mémoire du processus (échantillonnage) | 283 660 288 octets |
| Pic de mémoire du conteneur (cgroup, limite 1 Gio, Python 3.14 dans l'image d'essai) | 558 837 760 octets |
| Durée de la démonstration complète (poste de travail) | 33,9 s |
| Fichier SQLite (OLTP) | 13 369 344 octets |
| Fichier DuckDB (entrepôt, 48 000 faits au chargement) | 4 730 880 octets |
| Faits en Parquet | 897 258 octets, 18,3 octets par fait |
| Message JSON d'un paiement (sans enveloppe Debezium, approximation) | 302,6 octets |
| Entraînement du modèle (38 400 lignes) | 0,5 s |
| Latence de notation d'un paiement (médiane, dernière exécution) | 9,1 ms |

Le pic du conteneur est supérieur à celui du processus car il inclut le cache de fichiers et les systèmes de fichiers temporaires en mémoire (`tmpfs`) du conteneur.

## 2. Registre des hypothèses

| Réf. | Hypothèse | Valeur retenue | Pourquoi / comment la lever |
|---|---|---|---|
| H1 | Volume quotidien de paiements | S1 : 100 000 ; S2 : 1 000 000 ; S3 : 10 000 000 | **illustratif, non issu d'un opérateur réel** ; trois ordres de grandeur pour voir quelles décisions changent |
| H2 | Facteur de pointe (débit de pointe ÷ débit moyen) | 5 | à remplacer par la courbe de trafic observée |
| H3 | Durée de conservation des paiements dans l'OLTP | 13 mois | couvre un cycle annuel de comparaison et les délais de contestation ; à confirmer avec le métier et le DPO |
| H4 | Région | eu-west-1 (Irlande) | Union européenne ; disponibilité de MSK Serverless et Redshift Serverless à confirmer |
| H5 | Objectifs de reprise | OLTP : RTO 15 min, RPO quasi nul (panne de zone) ; erreur humaine : RTO quelques heures | à valider avec les parties prenantes puis à mesurer par un exercice |
| H6 | Tailles d'instances et capacités | dev : `db.t4g.medium`, 1 instance, 8 à 16 RPU ; prod : `db.r6g.large` ×2 (Aurora), ×3 (DocumentDB), 32 à 128 RPU | point de départ raisonné ; à valider par un banc d'essai de charge |
| H7 | Plafond mensuel de consommation de l'entrepôt | dev : 100 RPU-heures ; prod : 5 000 RPU-heures | garde-fou à fixer avec la direction financière |
| H8 | Seuils d'alerte et objectifs applicatifs | PSI > 0,2 ; score ≥ 0,8 ; latence de notation de l'ordre de la dizaine de ms en mémoire | conventions ou valeurs de démonstration ; à régler avec les coûts réels |
| H9 | Fraîcheur de l'entrepôt | quelques minutes (micro-lots) | à fixer avec les utilisateurs de tableaux de bord |
| H10 | Traitement du numéro de carte | hors de cette plateforme (jetons seulement) | à confirmer par l'évaluateur PCI |
| H11 | Base légale | exécution du contrat ; intérêt légitime (fraude) | à valider par le DPO |
| H12 | Pas de vente ni de partage de données personnelles | périmètre actuel | à réviser si le modèle économique change |
| H13 | Sujets Kafka et trafic du bus | 8 sujets (les 8 tables publiées) de 3 partitions, soit 24 partitions ; trafic sortant = 2 × entrant (deux groupes de consommateurs : lac et score) | à remplacer par le trafic mesuré |
| H14 | Usage de l'entrepôt en production | 2 heures par jour à 32 RPU, soit 1 920 RPU-heures par mois | à mesurer sur les premiers mois ; plafond H7 |
| H15 | Volume stocké dans S3 | 110 Gio (majorant : flux brut conservé 12 mois et faits Parquet) | à mesurer |
| H16 | Tâches de calcul en production (module Terraform `calcul`) | score : 1 vCPU, 2 Gio, 2 à 6 tâches, CPU visé 60 %, 60 s entre deux ajouts, 300 s entre deux retraits ; ETL : 2 vCPU, 8 Gio (dev : 1 à 2 tâches de score, ETL 1 vCPU et 4 Gio) | bornes du score tirées du § 4.1 (1 tâche à la pointe en S2, 6 en S3 ; au moins 2 pour survivre à la perte d'une zone) ; le reste est un point de départ, à régler par un banc d'essai |
| H17 | Seuil d'alerte du retard de l'emplacement de réplication | 128 Mio (134 217 728 octets) | une heure de journal à la pointe en S2 : 0,60 Gio par jour (§ 3) ÷ 24 × 5 (H2) ; vérifié par un test à 1 % près ; à régler avec l'espace disque réel |

## 3. Volumes extrapolés

| Grandeur | S1 | S2 | S3 |
|---|---|---|---|
| Paiements par jour (H1) | 100 000 | 1 000 000 | 10 000 000 |
| Débit moyen (paiements/s) | 1,16 | 11,57 | 115,74 |
| Débit de pointe, facteur 5 (H2), paiements/s | 5,79 | 57,87 | 578,70 |
| OLTP : croissance par jour (Gio) | 0,03 | 0,29 | 2,86 |
| OLTP : volume en ligne, 13 mois (H3), Gio | 11,3 | 113,1 | 1 130,7 |
| Index des paiements du dernier mois, à loger en cache (Gio) | 0,48 | 4,80 | 47,96 |
| Journal de transactions (WAL) par jour (Gio) | 0,06 | 0,60 | 6,02 |
| Flux Kafka par jour, JSON brut sans enveloppe (Gio) | 0,03 | 0,28 | 2,82 |
| Lac : faits en Parquet par an (Gio) | 0,62 | 6,22 | 62,21 |

Lecture : ces chiffres sont **linéaires** en H1 (le test `test_le_calcul_est_lineaire_en_volume` vérifie que multiplier H1 par 10 multiplie chaque ligne par 10). Ils ne tiennent pas compte de la fragmentation des index, des lignes supprimées en attente de nettoyage ni de la compression à grande échelle ; ce sont des ordres de grandeur à valider par un banc d'essai.

## 4. Analyse des besoins de calcul et de stockage

### 4.1 Calcul : CPU ou GPU ? (indicateur 1.1)

| Charge | Nature | Ressource décisive | Justification chiffrée |
|---|---|---|---|
| OLTP (écritures, lectures unitaires) | CPU et **mémoire** | mémoire de cache pour l'index chaud | index du dernier mois : 0,48 Gio (S1), 4,80 Gio (S2), 47,96 Gio (S3) : la classe d'instance doit offrir une mémoire nettement supérieure à cette valeur (marge à fixer par banc d'essai) |
| Capture des changements | léger | réseau | un seul lecteur du journal par emplacement ; 0,60 Gio de journal par jour en S2 |
| Notation de fraude | CPU | cœurs | un processus note environ 110 paiements/s (1 ÷ 9,1 ms, en mémoire) ; tâches nécessaires à la pointe : voir tableau ci-dessous |
| Entraînement | CPU | cœurs et mémoire | 0,5 s pour 38 400 lignes |
| Entrepôt | CPU, mémoire, E/S | RPU, élastiques | à valider (hypothèse H6) |

| Grandeur | S1 | S2 | S3 |
|---|---|---|---|
| Tâches de notation nécessaires à la pointe (extrapolation linéaire) | 1 | 1 | 6 |
| Lignes d'entraînement (fenêtre de 60 jours) | 6 000 000 | 60 000 000 | 600 000 000 |
| Durée d'entraînement extrapolée (s) | 78 | 781 | 7 812 |
| Mémoire minimale de la matrice d'entraînement (Gio, 6 variables × 8 octets) | 0,3 | 2,7 | 26,8 |

**Pas de GPU** : le modèle est un ensemble d'arbres sur 6 variables numériques ; l'entraînement mesuré dure 0,5 s. Extrapolé linéairement sur un seul cœur, il reste de l'ordre d'une dizaine de minutes en S2 (13 min) et de quelques heures en S3 (2,2 h). En S3, la **mémoire** (de l'ordre de 27 Gio pour la seule matrice) devient le facteur limitant avant le temps de calcul : la parade est d'échantillonner les paiements non frauduleux (la fraude est rare) ou d'entraîner sur un entrepôt de données, pas d'ajouter un GPU. Un GPU se justifierait pour une famille de modèles différente (réseaux de neurones profonds sur séquences d'événements, représentations vectorielles), non retenue dans ce périmètre.

### 4.2 Stockage : volumes, typologie, performance (indicateur 1.2)

| Magasin | Typologie | Volume (voir § 3) | Performance visée | Réglage |
|---|---|---|---|---|
| Aurora (OLTP) | blocs gérés, extensible automatiquement | croissance par jour × 13 mois (H3) | latence de lecture par index de l'ordre de la fraction de milliseconde (mesuré : 0,12 ms) | pas d'IOPS à provisionner ; surveiller la mémoire |
| DocumentDB | documents, stockage géré | événements bornés par le TTL de 90 jours | lectures par index | réplicas |
| Kafka | journal de messages | débit × durée de rétention | débit soutenu en pointe | géré par MSK Serverless |
| Lac S3 | objets Parquet, classes de stockage par âge | faits en Parquet par an (§ 3) + événements bruts archivés | débit de lecture parallèle | cycle de vie 30 jours → accès peu fréquent, 90 jours → archivage (seuils de départ à recaler sur les accès réels) |
| Entrepôt | colonnes compressées | ordre du volume du lac | requêtes d'analyse | clés de distribution et de tri |
| Archive d'audit | objets verrouillés | export de `journal_audit` | écriture rare, lecture exceptionnelle | rétention 365 jours |

### 4.3 Cohérence avec l'entraînement et l'inférence (indicateur 1.3)

| Besoin ML | Dimensionnement correspondant |
|---|---|
| Données d'entraînement reproductibles | l'entrepôt est la source unique ; requête d'entraînement à ordre déterministe ; reconstitution exacte vérifiée (ROC-AUC 0,872 retrouvé) |
| Fenêtre d'entraînement de 60 jours (démonstration) | à S2, 60 000 000 lignes, 2,7 Gio de matrice : tient dans une instance de calcul courante |
| Inférence en ligne | notation à la pointe : 1 tâche(s) en S2, 6 en S3 ; caractéristiques en ligne dans la base de documents (lectures par index sur `id_client`) |
| Pas de concurrence entre inférence et analytique | l'entrepôt est séparé de l'OLTP ; le service de score ne lit que la base de documents |

### 4.4 Structuration du stockage sans goulot d'étranglement (indicateur 5.1)

- **Lac organisé par date** (`paiements/ds=AAAA-MM-JJ/`) en fichiers Parquet : les lectures de l'entraînement et de l'entrepôt ne parcourent que les dates utiles.
- **Compacter les micro-lots** : beaucoup de petits fichiers dégradent les lectures ; une tâche de compaction quotidienne regroupe les micro-lots du jour (volume cible à fixer par mesure).
- **Séparer lecture et écriture** : l'entraînement lit le lac et l'entrepôt, jamais l'OLTP.
- **Base vectorielle** : non retenue. Le périmètre n'inclut aucune recherche par similarité ; l'ajouter sans besoin créerait un composant de plus à exploiter. Si des représentations vectorielles devenaient nécessaires (personnalisation avancée), deux options seraient à comparer : une extension vectorielle de PostgreSQL (un composant de moins) ou un service dédié (passage à l'échelle).
- **Goulots possibles à surveiller** : retard du connecteur (WAL), nombre de partitions du sujet limitant le parallélisme de la notation, mémoire du cache de l'OLTP.

### 4.5 Environnements d'exécution : conteneurs (indicateur 5.2)

Configuration dans `stripe/docker-compose.yml` ; même logique pour les tâches de production.

| Service | Mémoire | CPU | Processus max. | Isolation | Origine de la valeur |
|---|---|---|---|---|---|
| `demonstration` | 1 Gio | 1,0 | 100 | lecture seule, sans capacité Linux, utilisateur sans privilège, `tmpfs` pour `/tmp` et `sorties` | **mesuré** : pic de 558 837 760 octets, soit 52 % de la limite (marge de 1,9×) |
| `amorcage` | 512 Mio | 1,0 | 100 | idem | hypothèse (H6) |
| `postgres` | 1 Gio | 1,0 | 200 | capacités Linux minimales rendues une à une | hypothèse (H6) : poste de développement |
| `mongodb` | 1 Gio | 1,0 | 400 | idem | hypothèse (H6) |
| `redpanda` | 1 Gio | 1,0 | 400 | `no-new-privileges`, capacités retirées | hypothèse (H6) |
| `connect` (Debezium) | 1 Gio, tas JVM 768 Mio | 1,0 | 500 | idem | hypothèse (H6) |

**Tâches de production** (module Terraform `calcul`, hypothèse H16) : mêmes principes d'isolation, appliqués aux tâches ECS sur Fargate.

| Tâche | Forme | CPU | Mémoire | Nombre | Isolation | Origine de la valeur |
|---|---|---|---|---|---|---|
| `score` | service ECS permanent, autoscaling par suivi du CPU (cible 60 %) | 1 vCPU | 2 Gio | 2 à 6 en prod, 1 à 2 en dev | lecture seule, `/tmp` éphémère, utilisateur 10001, capacités Linux retirées, aucune adresse publique | bornes : § 4.1 (6 tâches à la pointe en S3) et deux zones ; taille : hypothèse (le conteneur de démonstration, modèle compris, culmine à 52 % de 1 Gio) |
| `etl` | tâche lancée chaque jour par le DAG, sans service permanent | 2 vCPU (dev : 1) | 8 Gio (dev : 4) | 1 par exécution | idem | hypothèse : matrice d'entraînement de 2,7 Gio en S2 (§ 4.1), avec marge ; en S3 (26,8 Gio), échantillonner plutôt qu'agrandir |

Fargate n'admet que certaines combinaisons de CPU et de mémoire : la variable `taches` du module les vérifie avant tout appel à AWS, et un test rejoue la même règle sur les valeurs de `dev` et de `prod`.

Limites de ressources, absence de privilèges et système de fichiers en lecture seule servent à **stabiliser** (un service qui dérape ne prend pas toute la machine) et à **isoler** (une compromission ne peut ni écrire dans l'image ni élever ses droits). Un service qui dépasserait sa limite de mémoire serait arrêté par le noyau : c'est pourquoi la marge sur la valeur mesurée est explicite.

## 5. FinOps : maîtriser les coûts (indicateur 8.1)

### 5.1 Où va le coût

| Poste | Unité de facturation (à vérifier dans la grille du fournisseur) | Ce qui le fait croître | Levier | Où dans le code |
|---|---|---|---|---|
| Aurora | heures d'instance + stockage + E/S + sauvegardes au-delà de la rétention incluse | nombre et classe d'instances, volume en ligne | une seule instance en `dev` ; rétention en ligne bornée (H3) ; archivage vers le lac | `nb_instances`, `classe_instance`, `retention_sauvegardes_jours` |
| DocumentDB | heures d'instance + stockage + E/S | idem | TTL de 90 jours, une instance en `dev` | `nb_instances`, index TTL |
| MSK Serverless | capacité de cluster + débit/partitions | débit, nombre de partitions | rétention des sujets courte, messages compacts | module `msk_serverless` |
| Redshift Serverless | RPU-heures consommées + stockage | requêtes, taille de la capacité de base | **plafond mensuel**, durée maximale de requête, agrégats pré-calculés | `plafond_rpu_heures_mensuel`, `duree_max_requete_s`, `capacite_base_rpu` |
| S3 | Go-mois par classe + requêtes | volume, nombre de petits fichiers | cycle de vie vers classes froides, compaction, purge des anciennes versions | module `bucket_s3` |
| KMS, Secrets Manager | nombre de clés/secrets + appels | nombre d'objets, volume d'appels | clé de bucket activée (moins d'appels KMS) | `bucket_key_enabled` |
| CloudWatch Logs | volume ingéré + stockage | verbosité des journaux | rétention uniforme de 365 jours (exigence de conformité, donc non réductible) ; niveaux de journalisation raisonnés | `retention_journaux_jours` |
| Réseau | pas de NAT (poste souvent coûteux) ; points de terminaison d'interface facturés à l'heure | nombre de points de terminaison | limiter la liste à ceux utilisés | `services_points_terminaison` |
| AWS Backup | stockage des points de restauration | rétention | rétention bornée | `retention_jours` |

### 5.2 Méthode de chiffrage

Pour chaque scénario S1 à S3 : **coût mensuel = Σ (quantité du poste × prix unitaire)**. Les quantités viennent des § 3 et 4 (heures d'instance, Gio stockés, RPU-heures) et des hypothèses H13 à H15. Les **prix unitaires** changent et dépendent de la région : ils sont relevés dans la liste de prix officielle du fournisseur, datés, et appliqués au § 5.5 dans un tableau `quantité × prix`. Un chiffrage non daté serait un chiffre inventé.

### 5.3 Garde-fous et gouvernance

- **Plafond de consommation** de l'entrepôt (variable `plafond_rpu_heures_mensuel`) avec alerte à l'approche.
- **Étiquettes** appliquées à toutes les ressources par le fournisseur Terraform (`projet`, `environnement`, `gere_par`, `donnees`) : prérequis pour ventiler les coûts par environnement (à activer comme étiquettes de répartition des coûts).
- **Budgets et alertes de coût** : AWS Budgets recommandé, non codé ici ; l'alarme `consommation_entrepot` (module `supervision`) prévient dès qu'un jour consomme plus d'un trentième du plafond mensuel de l'entrepôt.
- **`dev` détruit et recréé** sans protection : l'environnement de développement peut être éteint en dehors des heures de travail.
- **Réservations** : après plusieurs mois d'usage stable, des engagements (plans d'économies, instances réservées) deviennent pertinents pour l'OLTP, dont la charge est continue ; ils sont inutiles pour les composants serverless.

### 5.4 Arbitrage serverless / provisionné (lien avec la note d'arbitrage, document 01 § 7)

Le critère est le **taux d'utilisation**. Charge irrégulière ou inconnue : serverless (on ne paie pas l'inactivité). Charge continue et prévisible : capacité provisionnée, réservable. L'entrepôt (rafales de requêtes) et le bus (débit variable) sont serverless ; l'OLTP (charge permanente) est provisionné.

### 5.5 Chiffrage daté : scénario S2, région eu-west-1 (relevé le 10/10/2026)

La méthode du § 5.2 appliquée avec des prix **relevés**, jamais saisis de mémoire. Source
principale : la liste de prix publique d'AWS (fichiers `offers/v1.0/aws/<offre>/current/eu-west-1/
index.json`, lus par l'outil `livrables/aia2/outils/lire_prix_aws.py`), publiée le 11/09/2026 (MSK, Redshift,
DocumentDB, KMS, Secrets Manager), le 28/09/2026 (S3) et le 06/10/2026 (offre AmazonRDS, pour
Aurora : prix d'abord relevé dans un catalogue tiers, puis vérifié identique dans ce fichier
officiel). Prix à la demande, hors taxes, 730 heures par mois.

| Prix unitaire (eu-west-1) | Valeur | Source |
|---|---|---|
| Aurora PostgreSQL `db.r6g.large` / `db.t4g.medium` (Aurora Standard ; l'option I/O-Optimized coûte 0,372 / 0,101) | 0,286 / 0,078 USD de l'heure | liste AWS |
| DocumentDB `db.r6g.large` / `db.t4g.medium` (stockage standard) | 0,2907 / 0,08148 USD de l'heure | liste AWS |
| DocumentDB : stockage ; entrées-sorties | 0,11 USD par Go-mois ; 0,22 USD par million | liste AWS |
| MSK Serverless : cluster ; partition | 0,8625 USD de l'heure ; 0,001725 USD par partition-heure | liste AWS |
| MSK Serverless : données entrantes ; sortantes ; stockage | 0,115 ; 0,0575 ; 0,115 USD par Go(-mois) | liste AWS |
| Redshift Serverless : calcul ; stockage géré | 0,387 USD par RPU-heure ; 0,024 USD par Go-mois | liste AWS |
| S3 Standard (50 premiers To) | 0,023 USD par Go-mois | liste AWS |
| KMS (clé gérée par le client) ; Secrets Manager (secret) | 1 USD ; 0,40 USD par mois | liste AWS |

Hypothèses (registre du § 2) : **H13** : 24 partitions Kafka, trafic sortant double de
l'entrant (deux groupes de consommateurs) ; **H14** : 1 920 RPU-heures par mois ; **H15** :
110 Gio dans S3.

| Poste, environnement `prod` (S2 : 1 million de paiements par jour) | USD par mois | Part |
|---|---|---|
| Redshift Serverless, 1 920 RPU-heures (H14) | 743,04 | 30 % |
| DocumentDB, 3 × `db.r6g.large` | 636,63 | 26 % |
| MSK Serverless, cluster | 629,62 | 25 % |
| Aurora PostgreSQL, 2 × `db.r6g.large` | 417,56 | 17 % |
| MSK Serverless : 24 partitions (H13), 8,4 Gio entrants, 16,8 Gio sortants (H13) | 32,15 | 1 % |
| S3 (H15), 7 clés KMS, 5 secrets (la clé `alertes`, ajoutée le 19/10/2026, n'est pas comptée) | 11,53 | < 1 % |
| **Total des postes chiffrés (plancher : postes principaux seulement)** | **≈ 2 471** | |
| Majorant si l'entrepôt atteint son plafond de 5 000 RPU-heures (H7) | ≈ 3 662 | |

| Poste, environnement `dev` | USD par mois | Part |
|---|---|---|
| **MSK Serverless, cluster** (facturé à l'heure, même sans trafic) | **629,62** | **76 %** |
| DocumentDB, 1 × `db.t4g.medium` | 59,48 | 7 % |
| Aurora PostgreSQL, 1 × `db.t4g.medium` | 56,94 | 7 % |
| Redshift Serverless, plafond de 100 RPU-heures (H7, majorant) | 38,70 | 5 % |
| Partitions MSK, clés KMS, secrets | 39,22 | 5 % |
| **Total des postes chiffrés (plancher)** | **≈ 824** | |

**Ce que le chiffrage change.** En `dev`, trois quarts de la facture paient un cluster de messages
au repos. **Recommandation** (non appliquée : le Terraform `dev` déploie aujourd'hui MSK
Serverless ; Redpanda n'existe que dans le `docker-compose.yml` local) : un courtier Redpanda
auto-hébergé supprimerait le cluster (629,62 USD) et les partitions MSK (30,22 USD) ; les postes
chiffrés restants font **≈ 164 USD par mois**, **auxquels s'ajoute l'hébergement de Redpanda**
(non chiffré). En `prod`, aucun
poste ne domine : l'entrepôt, la base de documents et le bus pèsent chacun un quart ; le levier
de l'entrepôt est son plafond (H7) et ses agrégats, celui de la base de documents le nombre de
réplicas (3, pour la haute disponibilité). **Non chiffrés**, donc les totaux sont des planchers :
connecteur Debezium sur MSK Connect, tâches Fargate du score et de l'ETL, points de terminaison
d'interface (4 services × 3 zones), stockage et entrées-sorties d'Aurora, stockage de
DocumentDB, de Redshift et de MSK (prix relevés ci-dessus mais volumes non estimés),
CloudWatch Logs, AWS Backup, transferts réseau, et l'hébergement de Redpanda dans la variante
recommandée. Ces postes ne sont pas négligeables a priori : ils doivent être chiffrés avant une
décision. Ces prix changent : refaire le relevé le jour de la décision avec le même outil.

## 6. GreenOps : réduire l'empreinte (indicateur 8.2)

**Principe** : l'énergie consommée croît avec les heures de calcul, les Go stockés et les données transférées. On réduit l'empreinte en demandant moins de ressources (dimensionnement juste), en ne les gardant allumées que quand elles servent, et en stockant moins.

| Levier | Mise en œuvre | Indicateur de suivi |
|---|---|---|
| Dimensionnement juste | tailles de départ modestes en `dev` ; mesure avant d'augmenter | heures d'instance par environnement |
| Mise à l'échelle à la demande | entrepôt et bus serverless (pas de capacité inactive) ; `dev` éteint hors des heures de travail | RPU-heures ; heures d'instance |
| Pas de GPU sans besoin | modèle d'arbres sur CPU (§ 4.1) | heures de calcul d'entraînement |
| Stocker moins | TTL de 90 jours sur les événements ; rétention OLTP bornée (H3) puis archivage ; versions anciennes purgées ; classes froides | Gio-mois par classe |
| Stocker mieux | format Parquet colonnaire compressé : 18,3 octets par fait contre 307 octets par paiement dans l'OLTP, index compris. Les deux ne contiennent pas les mêmes colonnes ; le rapport montre l'effet combiné de la compression en colonnes et de l'absence d'index, il ne chiffre pas un gain énergétique | octets par paiement et par couche |
| Ne pas déplacer les données | analyse dans l'entrepôt, sans copies vers des postes ; COPY depuis S3 par le réseau interne (routage VPC amélioré, pas d'Internet) | volume transféré |
| Ne pas recalculer | agrégats pré-calculés (les tableaux de bord lisent quelques milliers de lignes) ; entraînement planifié ou déclenché par la dérive (PSI), pas à chaque exécution | nombre d'entraînements |
| Région | région de l'Union européenne ; le fournisseur publie un outil d'empreinte carbone du compte à utiliser pour suivre l'évolution | rapport d'empreinte du compte |

**Ce qui n'est pas chiffré** : aucune émission de CO₂ n'est donnée ici, car elle exige des facteurs d'émission (par région et par service) qui ne sont ni mesurés dans ce dépôt ni fournis par le sujet. Les indicateurs ci-dessus (heures de calcul, Gio-mois, volumes transférés) sont les entrées que prendrait ce calcul. Mesure de la démonstration : 34 s de calcul sur un poste pour 48 000 paiements, entraînement compris.

## 7. Limites

- Hypothèses H1 à H17 non validées par un banc d'essai de charge ; les volumes sont linéaires.
- Chiffrage daté au § 5.5 (relevé du 10/10/2026) : un plancher ; plusieurs postes restent non chiffrés (MSK Connect, Fargate, points de terminaison, stockages, journaux, sauvegardes, transferts : liste au § 5.5).
- Les performances de l'entrepôt et du bus n'ont pas été mesurées (services AWS non utilisés).

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
