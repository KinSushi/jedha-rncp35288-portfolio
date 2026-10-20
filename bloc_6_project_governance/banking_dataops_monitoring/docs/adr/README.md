# Décisions d'architecture

Chaque décision structurante est consignée dans un document court et daté :
le contexte, les options envisagées, la décision, ses conséquences.

L'intérêt n'est pas documentaire. Devant un jury comme devant une équipe,
la question n'est jamais « qu'avez-vous construit ? » mais « pourquoi ce
choix-là plutôt qu'un autre ? ». Un registre de décisions répond sans
reconstitution a posteriori.

| Réf | Décision | Statut |
|---|---|---|
| [ADR-0001](0001-traitement-par-lots.md) | Traitement par lots plutôt qu'en flux continu | Acceptée |
| [ADR-0002](0002-regles-deterministes.md) | Règles déterministes plutôt qu'apprentissage automatique | Acceptée, précisée par ADR-0008 |
| [ADR-0003](0003-donnees-synthetiques.md) | Données synthétiques déterministes | Acceptée, complétée par ADR-0009 |
| [ADR-0004](0004-contraintes-en-base.md) | Contraintes structurelles en base, règles métier en contrôle | Acceptée |
| [ADR-0005](0005-execution-locale.md) | Exécution locale conteneurisée plutôt qu'hébergement managé | Acceptée |
| [ADR-0006](0006-api-en-lecture-seule.md) | API de décision en lecture seule | Acceptée |
| [ADR-0007](0007-tracabilite-et-reverse-tests.md) | Traçabilité par journal d'audit et reverse tests | Acceptée |
| [ADR-0008](0008-classifieur-de-priorisation.md) | Classifieur de priorisation, jamais de blocage | Acceptée, complétée par ADR-0009 |
| [ADR-0009](0009-selection-hors-test-bandes-et-ancre.md) | Sélection hors du jeu de test, bandes ancrées sur le seuil, ancre de date fixe | Acceptée |
