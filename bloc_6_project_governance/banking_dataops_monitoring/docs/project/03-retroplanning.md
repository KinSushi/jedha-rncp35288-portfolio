# Rétroplanning

**Compétence visée** : C6.3 — définir un rétroplanning et le défendre.

Le projet a été mené en deux temps : une phase de construction technique en juin 2026, puis une phase de cadrage, d'industrialisation et de préparation à la soutenance en juillet.

**Ce document a deux états.** Les sections 1 à 5 donnent le **plan** établi au 20 juillet 2026. La section 6 donne le **bilan réalisé contre prévu**, arrêté au 11 octobre 2026, à partir des seules pièces du dépôt.

---

## 1. Vue d'ensemble

*Le diagramme est le plan du 20 juillet 2026 : les statuts « terminé » et « en cours » sont ceux de cette date.*

```mermaid
gantt
    title Rétroplanning banking-dataops-monitoring
    dateFormat YYYY-MM-DD
    axisFormat %d/%m

    section Construction
    Modèle de données et schéma SQL      :done, c1, 2026-06-08, 3d
    Générateur synthétique déterministe  :done, c2, 2026-06-10, 2d
    Ingestion et contrôles qualité       :done, c3, 2026-06-11, 3d
    Réconciliation et incidents          :done, c4, 2026-06-13, 2d
    Tableau de bord et documentation     :done, c5, 2026-06-14, 3d

    section Cadrage
    Cadrage métier et périmètre          :done, p1, 2026-07-20, 2d
    Stack conteneurisée                  :active, p2, 2026-07-20, 2d
    Scénario de démonstration            :active, p3, 2026-07-21, 2d

    section Soutenance
    Vérification et répétition technique :r1, 2026-07-22, 3d
    Construction du support              :r2, 2026-07-25, 4d
    Répétitions chronométrées            :r3, 2026-07-29, 3d
    Répétition à froid et gel du code    :crit, r4, 2026-08-01, 2d
    Demo Day                             :milestone, m1, 2026-08-03, 0d

    section Certification
    Dossier de projet et support jury    :cert1, 2026-08-04, 10d
    Passage des blocs 2, 3 et 6          :cert2, 2026-08-17, 14d
```

## 2. Jalons

| Jalon | Date | Critère de franchissement |
|---|---|---|
| J1 — Socle technique | 16 juin 2026 | Pipeline complet, tests verts, CI opérationnelle |
| J2 — Cadrage validé | 21 juillet 2026 | Problématique, périmètre et coupes documentés |
| J3 — Stack démontrable | 24 juillet 2026 | Démarrage à froid sous deux minutes, scénario rejouable |
| J4 — Support prêt | 28 juillet 2026 | Huit slides, trame minutée écrite |
| J5 — Gel du code | 2 août 2026 | Plus aucune modification jusqu'à la présentation |
| **J6 — Demo Day** | **3 août 2026** | Présentation tenue dans le temps imparti |
| J7 — Dossier de projet | 14 août 2026 | Document complet, relu, prêt à imprimer |
| J8 — Certification | Août-septembre 2026 | Blocs 2, 3 et 6 passés (non tenu dans cette fenêtre : voir § 6) |

## 3. Charge par phase

| Phase | Charge estimée | Charge consommée |
|---|---|---|
| Construction (juin) | 40 h | 40 h |
| Cadrage et industrialisation | 8 h | 7 h |
| Préparation soutenance | 13 h | — |
| Dossier de certification | 20 h | — |
| **Total** | **81 h** | **47 h** |

*Charge consommée au 20 juillet 2026. Aucune mesure postérieure n'est conservée (§ 6).*

## 4. Chemin critique

Le chemin critique passe par **J3 → J4 → J5**. Un retard sur la stack démontrable décale mécaniquement les répétitions, et la répétition à froid n'est pas compressible : c'est elle qui révèle les défaillances de démarrage.

Marge disponible : deux jours entre J4 et J5. Elle est réservée à la correction de ce que la première répétition chronométrée fera apparaître.

## 5. Règle de gel

À compter du 2 août, aucune modification n'est apportée au code, aux dépendances ou au support. Cette règle n'est pas cosmétique : la dernière modification avant une démonstration est statistiquement celle qui la casse.

Respect de la règle : voir § 6. Le code n'a pas changé après le 27 juillet 2026 ; le support et la documentation ont changé le soir du 2 août.

## 6. Bilan réalisé contre prévu (arrêté au 11 octobre 2026)

Ce bilan ne retient que ce que les pièces du dépôt datent (CHANGELOG, historique Git, ADR, preuves). Quand aucune pièce ne date un fait, la case le dit : le projet n'écrit ni date ni heure qu'il ne peut pas prouver. Le CHANGELOG, l'historique Git d'origine et le dossier de projet ne sont pas publiés dans ce dépôt : les dates qui en viennent sont reprises telles quelles.

### 6.1 Jalons

| Jalon | Prévu | Réalisé : ce que les pièces datent | Écart |
|---|---|---|---|
| J1 — Socle technique | 16 juin 2026 | CHANGELOG 0.1.0 daté du 16 juin 2026 (modèle de données, générateur, ingestion, sept contrôles, réconciliation, incidents, tableau de bord, intégration continue) | Aucun écart de date. Les tests verts de ce jalon ne sont pas conservés comme preuve datée |
| J2 — Cadrage validé | 21 juillet 2026 | Cahier des charges v1.0 daté du 20 juillet 2026 ; ADR-0005 à ADR-0007 du 20 juillet ; ADR-0008 du 21 juillet | Un jour d'avance pour le cahier des charges |
| J3 — Stack démontrable | 24 juillet 2026 | Aucune mesure de démarrage à froid dans les pièces. CHANGELOG 2.0.1 : budgets de démarrage « non vérifiés de bout en bout » ; CHANGELOG 2.6.0 : premier démarrage impossible (schéma jamais créé), corrigé | Critère « sous deux minutes » non établi |
| J4 — Support prêt | 28 juillet 2026 | Support de présentation présent dans le premier commit conservé, du 27 juillet 2026 | Jalon atteint selon l'historique Git |
| J5 — Gel du code | 2 août 2026 | Voir § 6.3 | Gel respecté pour le code, pas pour le support ni la documentation |
| J6 — Demo Day | 3 août 2026 | Événement de fin de formation, distinct de la certification. Le dépôt conserve les pièces de préparation du 2 août et trois commits automatiques de la CI du 3 août (UTC) | Aucune pièce du dépôt n'atteste le déroulement de l'événement |
| J7 — Dossier de projet | 14 août 2026 | Version initiale datée « août 2026 » ; révision d'octobre 2026 | Date de la version initiale non précisée au jour |
| J8 — Certification | Août-septembre 2026 | Le dépôt de certification est préparé en octobre 2026 (commits des 10 et 11 octobre 2026) | Non tenu dans la fenêtre prévue ; à replanifier |

### 6.2 Versions et commits

| Version du CHANGELOG | Date | Contenu |
|---|---|---|
| 0.1.0 | 16 juin 2026 | Socle technique initial |
| 1.0.0 | 20 juillet 2026 | Stack conteneurisée, scénario de démonstration, verdict de publication, API, journal d'audit et reverse tests, dossier de gestion de projet |
| 2.0.0 | 21 juillet 2026 | Couche Gold, classifieur de priorisation (ADR-0008), orchestrateur `run_pipeline.py` |
| 2.0.1 à 2.6.0 | 21 juillet 2026 | Nettoyage et quarantaine, Bronze horodaté, audits et correctifs, support Windows, correction du premier démarrage |

| Historique Git conservé | Date | Nature |
|---|---|---|
| Premier commit | 27 juillet 2026 | Dépôt complet |
| Deux commits | 2 août 2026 au soir (19 h 42 et 20 h 06, heure locale UTC−5) | Support en version française, script d'oral, fiche d'architecture, fichier Power BI, captures, preuves, README |
| Trois commits automatiques de la CI | 3 août 2026 (UTC) | Rapports de validation régénérés |
| Commits de préparation de la certification | 10 et 11 octobre 2026 | Preuves de tests réconciliées, corrections de documentation, présentateur unique |

Les travaux de juin et de juillet antérieurs au 27 juillet ne sont datés que par le CHANGELOG, pas par l'historique Git.

### 6.3 Respect de la règle de gel

Le gel du 2 août portait sur le code, les dépendances et le support. Les deux commits du 2 août au soir ajoutent un support en version française, un script d'oral, une fiche d'architecture, un fichier Power BI, des captures et des preuves, et modifient le README : **le gel n'a pas été respecté pour le support et la documentation**. Entre le commit du 27 juillet et ceux du 2 août, aucun fichier de `src/`, `dashboard/`, `sql/`, `docker/`, `tests/`, `scripts/`, ni du `Makefile`, de `requirements.txt`, de `run_pipeline.py` ou des workflows n'a changé (vérifié par `git diff`) : **le gel est respecté pour le code**.

### 6.4 Charge et périmètre

| Mesure | Prévu | Réalisé |
|---|---|---|
| Charge totale | 81 h | 47 h consommées au 20 juillet 2026 ; **aucune mesure postérieure** : l'écart final n'est pas établi |
| Périmètre | Cahier des charges du 20 juillet 2026 | Dépassé : modèle, couche Gold, nettoyage, rapport Power BI, Space de secours (annexe 01, § 4 bis), sans charge estimée ni budget |

### 6.5 Enseignements

1. Le planning n'a pas été remis à jour après le 20 juillet : l'ajout du modèle et de Gold le 21 juillet n'a pas modifié l'estimation de 81 heures.
2. Le jalon de certification a glissé de plusieurs semaines ; une réestimation s'impose.
3. Une règle de gel doit nommer ce qu'elle gèle : ici, « support » a été gelé sur le papier et modifié le soir même.
