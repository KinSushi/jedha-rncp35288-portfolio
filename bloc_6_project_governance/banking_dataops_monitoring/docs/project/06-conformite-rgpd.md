# Conformité RGPD

**Compétence visée** : C6.4 — gérer un projet de données dans le respect du RGPD.

---

## 1. Situation du projet au regard du règlement

Le projet ne traite **aucune donnée à caractère personnel**. Les jeux de données sont intégralement générés par un script déterministe : identifiants séquentiels, segments et pays tirés d'une liste fermée, montants et horodatages produits par un générateur pseudo-aléatoire à graine fixe.

En l'absence de personne physique identifiée ou identifiable, le règlement ne s'applique pas au dispositif tel qu'il est publié. Cette absence est **vérifiable** par lecture du générateur (`src/banking_dataops/generate_synthetic_data.py`) : toutes les valeurs sont tirées par le script.

La reproduction du jeu de données produit les mêmes lignes **à graine et à date d'ancre égales**. La graine fixe les tirages ; l'ancre (`generated_at`) fixe les horodatages. L'ancre se règle par `--generated-at` ou par `BANKING_DATAOPS_GENERATED_AT` ; par défaut elle vaut le 27 juillet 2026 à minuit UTC (ADR-0009), si bien que deux jours différents donnent les mêmes fichiers. La valeur `today` rétablit minuit UTC du jour : les fichiers changent alors d'un jour à l'autre.

## 2. Pourquoi documenter la conformité malgré tout

Le système est **conçu pour de la donnée bancaire réelle** et **démontré sur du synthétique**. Sa conception intègre donc les contraintes qui s'appliqueraient en production. C'est cette anticipation qui est documentée ici.

Le synthétique n'est pas un contournement : c'est la manière conforme de rendre publiquement démontrable un système destiné à de la donnée régulée.

## 3. Principes appliqués par conception

| Principe | Article | Mise en œuvre dans le projet |
|---|---|---|
| Minimisation | Art. 5.1.c | Le modèle ne comporte ni nom, ni adresse, ni date de naissance, ni identifiant national. Un client est un segment, un pays et une date de création. |
| Pseudonymisation | Art. 4.5, 32.1.a | **Non réalisée.** Les clients, comptes et transactions sont désignés par des identifiants séquentiels (`CUST-00001`, `ACC-00001-01`, `TX-00000001`) : ce sont des identifiants synthétiques, pas une pseudonymisation au sens de l'article 4, point 5, qui suppose que l'attribution à une personne exige des informations supplémentaires conservées séparément. En production, il faudrait des identifiants dérivés par un secret conservé à part. |
| Limitation des finalités | Art. 5.1.b | Les données servent au contrôle qualité et à la priorisation de l'instruction des transactions suspectes. Aucun usage de scoring commercial ou de ciblage. **Un score de suspicion est calculé par transaction** : il porte sur la transaction, pas sur une personne, mais rapporté à un client il évaluerait des aspects de sa personne, donc un profilage au sens de l'article 4, point 4. L'affirmation « aucun profilage » n'est plus tenue ; en production, la base légale, l'information des personnes et l'article 22 (décision individuelle automatisée) seraient à instruire. Ici, le score ne déclenche aucune décision. |
| Exactitude | Art. 5.1.d | C'est l'objet même du dispositif : détecter et bloquer les données inexactes avant propagation. Le modèle de priorisation n'a pas cet objet et ne bloque rien. |
| Intégrité et confidentialité | Art. 5.1.f | Exécution locale. Seules des données synthétiques peuvent quitter le poste, par la publication facultative vers une base managée pour Power BI (`scripts/publish_to_cloud.py`). Les identifiants de `docker-compose.yml` sont ceux d'une base de développement locale et synthétique ; aucun identifiant de production n'est dans le dépôt. |
| Responsabilité | Art. 5.2 | Chaque contrôle est nommé, horodaté et persisté. La trace est produite par le système, pas reconstituée après coup. |

## 4. Ce qu'il faudrait ajouter en production

Passage à des données réelles : les points suivants deviendraient obligatoires et ne sont **pas** couverts par le dispositif actuel.

| Exigence | Ce qui manque aujourd'hui |
|---|---|
| Base légale du traitement | À qualifier — vraisemblablement obligation légale de contrôle interne |
| Registre des activités de traitement | À rédiger et tenir à jour |
| Durée de conservation | Aucune politique de purge des tables de résultats |
| Droits des personnes | Aucun mécanisme d'accès, de rectification ou d'effacement |
| Chiffrement au repos et en transit | Non implémenté, exécution locale sans TLS |
| Journalisation des accès | Aucune trace des consultations du tableau de bord |
| Analyse d'impact | À conduire si le traitement présente un risque élevé |
| Sous-traitance | À encadrer contractuellement en cas d'hébergement externalisé |

Cette liste fait partie de la roadmap et constitue, en soi, un livrable de gouvernance.

## 4 bis. Règlement européen sur l'intelligence artificielle

L'annexe III du règlement (UE) 2024/1689, point 5 (« Accès et droit aux services privés essentiels et aux services publics et prestations sociales essentiels »), b), vise les « systèmes d'IA destinés à être utilisés pour évaluer la solvabilité des personnes physiques ou pour établir leur note de crédit, à l'exception des systèmes d'IA utilisés à des fins de détection de fraudes financières ». Source : EUR-Lex, texte français du règlement publié au Journal officiel de l'Union européenne du 12 juillet 2024, consulté le 11 octobre 2026. C'est le texte d'origine, pas une version consolidée : une modification ultérieure de l'annexe III n'a pas été recherchée.

**Conclusion.** Le classifieur de ce projet priorise des transactions suspectes pour la revue humaine : il relève de l'exception de détection de fraudes financières et n'est donc pas un système « à haut risque » au titre de ce point. Il ne bloque rien, n'évalue la solvabilité de personne et ne traite que des données synthétiques. Les obligations de transparence du règlement et le RGPD (article 22) restent à respecter en production, et la qualification complète du système resterait à instruire.

## 5. Sûreté publique du dépôt

Le dépôt est conçu pour pouvoir être publié sans risque. Sont interdits par règle interne : données bancaires, d'assurance ou de santé réelles, données de clients ou d'employeurs, identifiants, jetons, noms d'hôtes privés.

La procédure de vérification figure dans `docs/VALIDATION.md`. Elle est exécutée avant chaque publication.

## 6. Formulation pour la soutenance

> Aucune donnée personnelle n'est traitée : tout est généré par script, à graine fixe et à date d'ancre fixe pour être reproductible. Le modèle de données a été conçu sans champ nominatif, ce qui est une décision de conception et non une conséquence du caractère synthétique. Les identifiants séquentiels ne sont pas une pseudonymisation, et le score de suspicion par transaction interdit de dire « aucun profilage » : en production, il resterait à définir la base légale, une vraie pseudonymisation, la durée de conservation et les modalités d'exercice des droits, points recensés dans ce document.
