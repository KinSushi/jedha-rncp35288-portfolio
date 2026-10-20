# Note de veille technologique

**Compétence visée** : C6.2 — maîtriser les technologies récentes du marché par la veille, afin de proposer les solutions les plus adaptées.

---

## 1. Méthode de veille

| Canal | Nature | Fréquence |
|---|---|---|
| Documentation officielle des éditeurs | Source primaire | À chaque décision technique |
| Notes de version des dépendances | Source primaire | Mensuelle |
| Publications d'ingénierie des acteurs data | Retour d'expérience | Hebdomadaire |
| Référentiels de qualité de données ouverts | Comparaison de pratiques | Ponctuelle |

Principe retenu : **aucune technologie n'entre dans le projet sans qu'une alternative ait été écartée explicitement.** Chaque arbitrage est consigné dans `docs/adr/`.

## 2. Arbitrages par brique

### Base de données

| Option | Avantage | Inconvénient | Décision |
|---|---|---|---|
| **PostgreSQL 16** | Contraintes déclaratives fortes, SQL analytique complet, standard bancaire | Nécessite un service | **Retenue** |
| SQLite | Zéro service, fichier unique | Contraintes plus faibles, pas de rôle serveur réaliste | Écartée |
| DuckDB | Excellent en analytique embarquée | Modèle transactionnel inadapté à un contrôle d'ingestion | Écartée |

Motif déterminant : le projet démontre que **la base assure l'intégrité structurelle** et que la couche de contrôle traite les règles métier. Cette thèse suppose des contraintes réellement appliquées.

### Orchestration

| Option | Avantage | Inconvénient | Décision |
|---|---|---|---|
| **Orchestrateur Python `run_pipeline.py`, cibles Make et entrypoint** | Lisible, sans service supplémentaire | Pas de reprise sur échec | **Retenue** |
| Apache Airflow | Standard du marché, reprise et calendrier | Surdimensionné pour un enchaînement séquentiel unique | Écartée, en roadmap |
| Prefect / Dagster | Plus léger qu'Airflow, typage moderne | Ajoute une dépendance sans bénéfice à ce périmètre | Écartée |

### Interface

| Option | Avantage | Inconvénient | Décision |
|---|---|---|---|
| **Streamlit** | Développement rapide, enseigné dans le cursus | Peu adapté au multi-utilisateur | **Retenue** pour le verdict |
| Power BI | Standard en banque, certification PL-300 associée | Dépendance à un éditeur, pas de conteneurisation, licences non chiffrées dans le budget | **Retenue en complément** : rapport livré (fichier `.pbix`, huit vues `vw_`), ouvert et actualisé pendant la démonstration en direct. Écartée à la date du cadrage (20 juillet 2026), puis livrée (commits du 2 août 2026) |
| Grafana | Référence en supervision | Orienté séries temporelles, moins adapté à un verdict tabulaire | Écartée |

### Distribution

| Option | Avantage | Inconvénient | Décision |
|---|---|---|---|
| **Docker Compose** | Reproductible, une commande, hors ligne | Mono-machine | **Retenue** : deux services, `postgres` et `app` |
| Hébergement managé | Accessible par lien | Dépendance réseau, mise en veille des offres gratuites | Écartée comme voie principale ; un Space Hugging Face de secours, en lecture seule, a existé d'après l'historique du dépôt ; il n'est pas vérifiable depuis l'archive de code |
| Kubernetes | Réaliste en production | Sans objet à cette échelle | Écartée |

Motif déterminant : la démonstration doit fonctionner **sans dépendance réseau**. Une application hébergée sur une offre gratuite se met en veille et fait courir un risque inutile en présentation. Le Space de secours ne remplace pas la démonstration locale : il lit une base managée alimentée depuis une exécution locale.

### Apprentissage automatique

| Option | Avantage | Inconvénient | Décision |
|---|---|---|---|
| Aucun modèle | Opposabilité maximale, aucune dette de gouvernance de modèle | Aucun signal statistique pour orienter la revue humaine | Position de l'ADR-0002 (9 juin 2026), **précisée** le 21 juillet 2026 |
| Modèle d'anomalies non supervisé (isolation forest, autoencodeurs) au cœur du dispositif | Capte des motifs non anticipés | Verdict non explicable en une phrase | Écartée (ADR-0002) |
| Modèle qui bloque la publication | — | Contredit l'ADR-0002 et rend la décision inopposable (ADR-0008) | Écartée |
| **Classifieur supervisé qui ordonne la file d'investigation** : régression logistique | Lisible (coefficients), ne bloque rien | Cible issue d'une règle du générateur sur données synthétiques ; dérive non mesurée | **Retenue** (ADR-0008, 21 juillet 2026) |
| Forêt aléatoire optimisée par recherche sur grille | Peut capter des interactions | Opacité ; n'a pas gagné la marge de 0,01 d'AUC exigée | Comparée, non retenue |

La chronologie complète de cet arbitrage figure au § 2.3 du dossier de projet.

### Validation

| Option | Avantage | Inconvénient | Décision |
|---|---|---|---|
| **pytest + ruff** | Standard Python, rapide, intégré à la CI | — | **Retenue** |
| Great Expectations | Spécialisé qualité de données, riche | Ajoute un référentiel parallèle aux contrôles SQL | Écartée |
| dbt tests | Excellent si le socle est dbt | Suppose de reconstruire le pipeline sur dbt | Écartée, à réévaluer |

## 3. Signaux à surveiller

| Sujet | Pourquoi | Effet attendu sur le projet |
|---|---|---|
| Adoption de dbt en contexte régulé | Pourrait remplacer la couche de contrôle SQL | Refonte de la couche `quality_checks` |
| Contrats de données et catalogues | Formalisation des attentes entre producteurs et consommateurs | Ajout d'une couche contractuelle amont |
| Évolution du règlement européen sur l'IA | Pourrait qualifier un usage de modèle en contexte régulé ; l'annexe III, point 5, b, du règlement (UE) 2024/1689 (EUR-Lex, consulté le 11 octobre 2026) vise l'évaluation de la solvabilité et exclut expressément « les systèmes d'IA utilisés à des fins de détection de fraudes financières » : le classifieur de priorisation relève de cette exception | Les obligations de transparence et l'article 22 du RGPD restent à instruire si le modèle passait en production |
| OpenTelemetry pour les traitements de données | Standardisation de l'observabilité | Instrumentation du pipeline |

## 4. Conclusion

Les choix retenus privilégient trois propriétés dans cet ordre : **explicabilité**, **reproductibilité**, **absence de dépendance externe**. Chacune découle directement du contexte régulé. Un projet en contexte non régulé aurait justifié des arbitrages différents, notamment sur l'orchestration et sur le rôle de l'apprentissage automatique : ici, il ordonne une file d'instruction et ne décide jamais de la publication.
