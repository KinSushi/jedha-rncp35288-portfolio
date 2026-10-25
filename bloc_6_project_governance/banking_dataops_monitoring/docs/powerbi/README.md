# Rapport Power BI

Construction et utilisation du rapport de restitution, à partir des vues en étoile exposées par PostgreSQL.

Pourquoi ce rapport existe. Le règlement de certification cite les rapports Power BI parmi les éléments dont la fonctionnalité peut être démontrée en direct. C'est aussi le format qu'attendent les directions métier en banque : un tableau de bord Streamlit convainc un ingénieur, un rapport Power BI convainc un comité.

Ce que ce dossier contient. Le fichier `.pbix` est un format binaire propriétaire : il se construit dans Power BI Desktop, il ne se génère pas. Ce dossier fournit le rapport livré (`banking_dataops_monitoring.pbix`) et tout ce qui permet de le reconstruire en une session : le modèle, les mesures DAX prêtes à coller, la spécification page par page et les thèmes visuels.

---

## 1. Prérequis

| Élément | Détail |
|---|---|
| Power BI Desktop | Version récente, Windows uniquement |
| Connecteur PostgreSQL | Intégré depuis 2021, aucune installation manuelle sur les versions actuelles |
| La stack démarrée | `make stack-up` |
| Les vues créées | `make powerbi-views` |

Vérifier que les vues répondent :

```bash
make powerbi-check
```

Le résultat attendu est une ligne : `controls_total`, `controls_failed`, `verdict`, `can_publish`.

---

## 2. Connexion

Dans Power BI Desktop : Obtenir les données → Base de données PostgreSQL.

| Champ | Valeur |
|---|---|
| Serveur | `localhost:5432`, ou le port surchargé par `POSTGRES_PORT` |
| Base de données | `banking_dataops` |
| Mode de connectivité | Import |
| Utilisateur | `dataops` |
| Mot de passe | `dataops` (identifiants de développement d'une base locale synthétique, définis dans `docker-compose.yml`) |

Import plutôt que DirectQuery. La volumétrie est faible, le rafraîchissement est journalier, et l'import autorise l'ensemble des fonctions DAX : DirectQuery en restreint plusieurs, dont une partie de la Time Intelligence. Ce choix est à savoir justifier : c'est une question classique de la certification PL-300.

Sélectionner les huit vues préfixées `vw_`. Ne sélectionner aucune table brute : le modèle doit reposer sur les vues, pas sur le schéma physique.

---

## 3. Modèle en étoile

```
                  vw_dim_date
                       |
        +--------------+--------------+
        |                             |
vw_fact_transactions        vw_fact_quality_results
        |                             |
  vw_dim_account                vw_dim_control
        |
  vw_dim_customer
```

### Relations à créer

| De | Vers | Cardinalité | Direction |
|---|---|---|---|
| `vw_fact_transactions[date_key]` | `vw_dim_date[date_key]` | Plusieurs à un | Simple |
| `vw_fact_transactions[account_id]` | `vw_dim_account[account_id]` | Plusieurs à un | Simple |
| `vw_dim_account[customer_id]` | `vw_dim_customer[customer_id]` | Plusieurs à un | Simple |
| `vw_fact_quality_results[date_key]` | `vw_dim_date[date_key]` | Plusieurs à un | Simple |
| `vw_fact_quality_results[control_id]` | `vw_dim_control[control_id]` | Plusieurs à un | Simple |
| `vw_fact_incidents[control_id]` | `vw_dim_control[control_id]` | Plusieurs à un | Simple |

Toutes les relations sont en filtre simple. Le filtrage bidirectionnel crée des ambiguïtés de chemin dès qu'un modèle comporte deux tables de faits, ce qui est le cas ici ; c'est un point d'examen de la PL-300.

### Table de dates

Marquer `vw_dim_date` comme table de dates : Modélisation → Marquer comme table de dates → colonne `date_key`.

Sans cette déclaration, les fonctions de Time Intelligence renvoient des résultats faux sans lever d'erreur : c'est le piège le plus fréquent.

### Masquer les colonnes techniques

Masquer de la vue rapport toutes les clés `*_id` des tables de faits, `check_id` et `is_demo_row`. Un modèle propre n'expose que ce qu'un utilisateur métier peut manipuler.

---

## 4. Mesures DAX

Toutes les mesures sont dans `dax-measures.md`, prêtes à coller. Créer une table de mesures dédiée : Accueil → Entrer des données, la nommer `_Measures`, charger une table vide, puis y déplacer toutes les mesures.

---

## 5. Pages du rapport

Spécification détaillée dans `report-specification.md`. Quatre pages :

| Page | Objet | Audience |
|---|---|---|
| 1 : Verdict | La décision de publication, en trois secondes | Direction |
| 2 : Contrôles | Détail des sept contrôles, structurel contre métier | Analyste qualité |
| 3 : Volumétrie | Volumes et montants par date, canal, système source | Métier |
| 4 : Risque | Concentration du risque, transactions suspectes | Conformité |

L'état du fichier livré et les améliorations prévues sont décrits dans `AMELIORATIONS-POWER-BI.md`.

---

## 6. Thème visuel

`theme.json` reprend la palette du projet : marine, vert de validation, rouge de blocage. `theme-sombre.json` en est la variante sombre.

Affichage → Thèmes → Parcourir les thèmes → sélectionner le fichier.

Le vert et le rouge y sont réservés au verdict. Leur force tient à leur rareté.

---

## 7. Publication

Pour la certification, un fichier local suffit. Pour publier sur le service Power BI :

| Point | À faire |
|---|---|
| Espace de travail | Créer un espace dédié, ne pas publier dans « Mon espace » |
| Passerelle | Une passerelle de données locale est nécessaire pour rafraîchir depuis un PostgreSQL local ; une base managée (`connexion-cloud-neon-supabase.md`) l'évite |
| Sécurité au niveau des lignes | Non applicable ici : aucune donnée personnelle, aucune segmentation par utilisateur |
| Rafraîchissement | Planifié quotidiennement, aligné sur la cadence du lot |

La sécurité au niveau des lignes est un point d'examen de la PL-300. Elle ne s'applique pas ici : le rapport porte sur la qualité d'un lot, pas sur des données clients segmentées par utilisateur.

---

## 8. Brancher le rapport livré sur la pile locale

Le fichier `banking_dataops_monitoring.pbix` livré pour la certification (non publié dans ce dépôt) est configuré sur une base PostgreSQL managée (Neon), et non sur `localhost` : ses huit requêtes pointent vers un hôte `ep-…neon.tech`, port 5432, base `neondb`, schéma `public`. Pour qu'il affiche l'état de la pile Docker lancée en local :

1. démarrer la pile (`docker compose up --build`) ; les huit vues sont créées par le pipeline (`evidence/powerbi_views.txt`) ;
2. travailler sur une copie du fichier, hors de l'arborescence du dépôt ;
3. dans Power BI Desktop : Transformer les données → Paramètres de la source de données → Changer la source, pour la source (les huit requêtes partagent le même serveur et la même base) : serveur `localhost:5432` (ou le port défini par `POSTGRES_PORT`), base `banking_dataops` ;
4. Modifier les autorisations : type « Base de données », utilisateur `dataops`, mot de passe `dataops` ;
5. Fermer et appliquer, puis Accueil → Actualiser.

Les vues lisent les tables du schéma `public` : l'injection d'un lot dégradé par le tableau de bord (`Inject degraded batch`) se voit dans le rapport à l'actualisation suivante (contrôles en échec, incidents ouverts, verdict). La connexion à Neon a été rejouée le 24 octobre 2026 : le lot de la pile locale (ancre par défaut) a été publié par `scripts/publish_to_cloud.py`, avec 14 tables identiques des deux côtés au contrôle `--verify`, puis le rapport livré a été actualisé depuis cette base : verdict WARN, 120 dates, 1 500 transactions.

---

## 9. Démonstration en direct

Le rapport est présenté à la fin de la démonstration, après la détection et le modèle (voir le script de soutenance) :

- l'ouvrir avant la présentation, branché sur la pile locale (§ 8), et l'actualiser une fois 30 minutes avant, quand le lot est vert ;
- pendant la démonstration, l'actualiser une seule fois, après l'injection du lot dégradé : le verdict change ;
- avoir des captures de secours « avant » et « après », au cas où l'actualisation échouerait.

> Le tableau de bord répond à l'ingénieur qui doit agir. Le rapport Power BI répond au comité qui doit comprendre. L'API répond au pipeline qui doit s'arrêter. Trois publics, une seule décision.

---

## 10. Ce que ce rapport apporte à la certification

| Compétence | Où elle se démontre |
|---|---|
| Préparation des données | Vues SQL en étoile plutôt que transformations enfouies dans le rapport |
| Modélisation | Schéma en étoile, deux tables de faits, table de dates marquée |
| DAX | Mesures explicites, Time Intelligence, calculs de variation |
| Visualisation | Pages graduées par audience |
| Déploiement et sécurité | Espace de travail, passerelle, position argumentée sur la sécurité au niveau des lignes |

La couverture des cinq domaines de la PL-300 est un objectif de conception : le fichier livré en réalise une partie (`AMELIORATIONS-POWER-BI.md`).

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
