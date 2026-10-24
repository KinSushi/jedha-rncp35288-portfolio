# 08 - Requêtes métier : SQL (entrepôt) et NoSQL (documents), avec résultats réels

> Répond au livrable 8 de l'énoncé (requêtes SQL et NoSQL répondant à des questions métier).
> Les **résultats ci-dessous sont ceux de la démonstration** (`python -m stripe_demo.pipeline`), copiés depuis `livrables/aia2/donnees/resultats_demo.json` par un script, non retapés.
> Les données sont synthétiques et générées avec une graine fixe : les résultats SQL et NoSQL sont reproductibles à l'identique d'une exécution à l'autre (seules les latences et durées varient).

## Comment reproduire

```text
cd stripe
pip install -r requirements.txt
python -m stripe_demo.pipeline        # affiche les résultats et écrit sorties/rapport_demo.json
```

La démonstration charge 48 000 paiements sur 60 jours (du 1er août au 29 septembre 2026), puis fait arriver 1 000 paiements supplémentaires par la capture des changements (cinq micro-lots), soit 49 000 faits au total. **Les deux derniers jours du jeu (30 septembre et 1er octobre) ne contiennent que ces 1 000 paiements de flux** : ce sont des jours incomplets, ce qui explique des chiffres quotidiens plus bas à la fin des séries.

> **Correction apportée pour obtenir ces résultats.** Dans la version d'origine de la démonstration, les paiements du flux d'un jour nouveau avaient une clé de date absente de la dimension de date et étaient donc ignorés par toutes les requêtes qui la joignent (par exemple le chiffre mensuel). Le chargement ajoute désormais ces jours (`olap.inserer_faits`, protégé par un test). Les chiffres mensuels ci-dessous incluent donc le flux ; ils diffèrent de ceux de la première exécution du dépôt de départ (par exemple septembre : 20 824 paiements réussis auparavant, 21 449 maintenant).

## Requêtes SQL (entrepôt en étoile)

Les 7 requêtes sont dans `stripe/sql/requetes_olap.sql` (DuckDB en démonstration ; SQL standard, exécutable sur Redshift sans changement de syntaxe attendu, **non testé** sur Redshift).

### Q1 - Quel est le chiffre d'affaires mensuel et le nombre de paiements réussis ?

```sql
SELECT d.annee, d.mois, round(sum(f.montant_usd), 2) AS chiffre_usd, count(*) AS nb_paiements
FROM faits_paiements f JOIN dim_date d USING (cle_date)
WHERE f.est_reussi
GROUP BY d.annee, d.mois ORDER BY d.annee, d.mois;
```

| Année | Mois | Chiffre d'affaires (USD) | Paiements réussis |
|---|---|---|---|
| 2026 | 8 | 880 432,47 | 22 238 |
| 2026 | 9 | 853 875,51 | 21 449 |
| 2026 | 10 | 12 276,43 | 288 |

**Lecture.** 43 975 paiements réussis pour 1 746 584,41 USD au total. Octobre ne contient que la fin du flux (288 paiements réussis) : la ligne est partielle. Le chiffre est converti en USD au taux du jour du paiement ; seuls les paiements au statut `reussi` comptent (un paiement remboursé ou contesté en est exclu). **Optimisation** : la clé de tri `cle_date` limite les blocs lus à la période demandée ; `dim_date` (copiée sur chaque nœud) évite toute redistribution.

### Q2 - Quels sont les 5 marchands qui réalisent le plus de chiffre d'affaires ?

```sql
SELECT m.raison_sociale, round(sum(a.chiffre_usd), 2) AS chiffre_usd
FROM agr_chiffre_quotidien a JOIN dim_marchand m USING (cle_marchand)
GROUP BY m.raison_sociale ORDER BY chiffre_usd DESC LIMIT 5;
```

| Marchand | Chiffre d'affaires (USD) |
|---|---|
| Marchand 037 | 58 143,71 |
| Marchand 035 | 54 912,40 |
| Marchand 016 | 54 347,88 |
| Marchand 036 | 53 518,14 |
| Marchand 025 | 53 434,00 |

**Lecture.** La requête lit l'agrégat `agr_chiffre_quotidien` (un enregistrement par jour et par marchand) et non les 49 000 faits. Les 40 marchands ont des volumes proches (données simulées de façon uniforme) : l'écart entre le premier et le cinquième est de 4 709,71 USD.

### Q3 - Quels sont les taux de remboursement et de litige par catégorie d'activité (MCC) ?

```sql
SELECT m.code_mcc, count(*) AS nb_paiements,
       round(avg(f.est_rembourse::INTEGER) * 100, 2) AS taux_remboursement_pct,
       round(avg(f.est_conteste::INTEGER) * 100, 2) AS taux_litige_pct
FROM faits_paiements f JOIN dim_marchand m USING (cle_marchand)
GROUP BY m.code_mcc ORDER BY taux_litige_pct DESC;
```

| MCC | Paiements | Remboursement (%) | Litige (%) |
|---|---|---|---|
| 4121 | 5 170 | 2,55 | 2,67 |
| 5999 | 4 274 | 2,46 | 2,57 |
| 5812 | 10 571 | 2,71 | 2,56 |
| 5732 | 11 290 | 3,03 | 2,52 |
| 5411 | 7 771 | 3,15 | 2,41 |
| 7372 | 9 924 | 2,84 | 2,40 |

**Lecture.** Les taux de litige vont de 2,40 % à 2,67 % et ceux de remboursement de 2,46 % à 3,15 % : **aucune catégorie ne se détache**, ce qui est normal puisque le simulateur tire la fraude sans tenir compte du MCC. Sur de vraies données, cette requête servirait à repérer les catégories à risque. Elle rattache chaque fait à son marchand par la clé de substitution portée par le fait, c'est-à-dire la version du marchand valable au moment du chargement.

### Q4 - La fraude est-elle plus fréquente quand le pays de l'adresse IP diffère du pays du client ?

```sql
SELECT (f.pays_ip <> c.code_pays) AS pays_different, count(*) AS nb_paiements,
       round(avg(f.est_conteste::INTEGER) * 100, 2) AS taux_litige_pct
FROM faits_paiements f JOIN dim_client c USING (cle_client)
GROUP BY pays_different ORDER BY pays_different;
```

| Pays différent | Paiements | Litige (%) |
|---|---|---|
| non | 46 451 | 1,28 |
| oui | 2 549 | 24,87 |

**Lecture.** Le taux de litige est environ 19 fois plus élevé quand le pays diffère (24,87 % contre 1,28 %). **Cet écart est construit par la simulation** (`oltp.paiements_synthetiques` ajoute 0,25 à la probabilité de fraude dans ce cas) : il valide que la chaîne d'analyse retrouve un signal connu, pas qu'il existe dans le monde réel. C'est ce signal que le modèle de fraude apprend (document 07).

### Q5 - La fraude est-elle plus fréquente la nuit ?

```sql
SELECT CASE WHEN f.heure < 6 THEN 'nuit (0-6 h)' ELSE 'journée' END AS tranche,
       count(*) AS nb_paiements, round(avg(f.est_conteste::INTEGER) * 100, 2) AS taux_litige_pct
FROM faits_paiements f GROUP BY tranche ORDER BY tranche;
```

| Tranche | Paiements | Litige (%) |
|---|---|---|
| journée | 36 574 | 1,50 |
| nuit (0-6 h) | 12 426 | 5,47 |

**Lecture.** Le taux de litige est environ 3.6 fois plus élevé la nuit (5,47 % contre 1,50 %), là encore par construction (0,04 de probabilité ajoutée la nuit dans le simulateur). La variable `heure` du fait est utilisée telle quelle.

### Q6 - Comment se répartissent les clients par segment, et quel est leur panier moyen ?

```sql
SELECT c.segment, count(DISTINCT f.cle_client) AS clients, count(*) AS paiements,
       round(avg(f.montant_usd), 2) AS panier_moyen_usd
FROM faits_paiements f JOIN dim_client c USING (cle_client)
GROUP BY c.segment ORDER BY paiements DESC;
```

| Segment | Clients | Paiements | Panier moyen (USD) |
|---|---|---|---|
| regulier | 1 739 | 40 501 | 39,67 |
| fidele | 259 | 8 480 | 40,93 |
| occasionnel | 2 | 19 | 44,64 |

**Lecture.** Le segment est calculé au chargement par le nombre de paiements du client (30 ou plus : « fidèle » ; 10 à 29 : « régulier » ; sinon « occasionnel »). Les 49 000 paiements sont tous répartis : la somme des segments égale le nombre de faits. Le segment « occasionnel » (2 clients) est trop petit pour être interprété. Les paniers moyens des segments sont voisins : le simulateur ne lie pas le montant au segment.

### Q7 - Quelle est la tendance du chiffre quotidien (moyenne mobile sur 7 jours) ?

```sql
SELECT date, round(chiffre, 2) AS chiffre_usd,
       round(avg(chiffre) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2)
           AS moyenne_mobile_7j
FROM (SELECT date, sum(chiffre_usd) AS chiffre FROM agr_chiffre_quotidien GROUP BY date)
ORDER BY date DESC LIMIT 7;
```

| Date | Chiffre du jour (USD) | Moyenne mobile 7 jours (USD) |
|---|---|---|
| 2026-10-01 | 12 276,43 | 25 674,40 |
| 2026-09-30 | 22 574,64 | 28 218,39 |
| 2026-09-29 | 30 270,48 | 29 050,79 |
| 2026-09-28 | 28 478,08 | 28 461,65 |
| 2026-09-27 | 30 037,64 | 28 625,89 |
| 2026-09-26 | 27 203,99 | 28 497,47 |
| 2026-09-25 | 28 879,53 | 29 040,18 |

**Lecture.** La fonction fenêtre `AVG(…) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)` lisse les variations quotidiennes. Les deux premières lignes (1er octobre et 30 septembre) sont les **jours incomplets du flux** : le chiffre du jour est plus bas qu'un jour complet (autour de 28 000 USD) et tire la moyenne mobile vers le bas. Ce n'est pas une baisse d'activité. En production, il faut exclure les jours en cours d'une telle série.

## Requêtes NoSQL (base de documents)

Les 3 pipelines d'agrégation sont dans `stripe/stripe_demo/nosql.py` (exécutés par `mongomock`) et, en syntaxe `mongosh`, dans `stripe/nosql/requetes_mongo.js`. Un test exécute le fichier JavaScript contre un simulateur et vérifie que les pipelines sont **identiques** à ceux de Python.

Le jeu : 5 000 événements répartis sur 30 jours (du 1er au 30 septembre 2026, tirés au hasard à la minute près) et 400 avis. L'intitulé « (30 jours) » de la première requête est celui du code ; le pipeline ne filtre pas sur la date, et les données couvrent exactement cette période.

### Q8 - Quelles erreurs de paiement sont les plus fréquentes ?

```js
db.evenements.aggregate([
  { $match: { type: "erreur_paiement" } },
  { $group: { _id: "$erreur.code", nombre: { $sum: 1 } } },
  { $sort: { nombre: -1 } },
])
```

| Code d'erreur | Nombre |
|---|---|
| card_declined | 437 |
| insufficient_funds | 432 |
| expired_card | 380 |

**Lecture.** 1 249 événements d'erreur, répartis presque à parts égales entre trois causes (carte refusée, fonds insuffisants, carte expirée) : tirage uniforme du simulateur. En production, cette requête orienterait l'effort (relance de paiement, message d'erreur plus clair). L'accès aux sous-documents (`$erreur.code`) illustre l'intérêt de l'**imbrication**.

### Q9 - Quelles sont les sessions mobiles abandonnées les plus longues ?

```js
db.evenements.aggregate([
  { $match: { type: "abandon", "session.appareil": "mobile" } },
  { $sort: { "session.duree_s": -1 } },
  { $limit: 3 },
  { $project: { _id: 0, id_client: 1, duree_s: "$session.duree_s" } },
])
```

| Client | Durée de session (s) |
|---|---|
| 249 | 898 |
| 1714 | 897 |
| 26 | 895 |

**Lecture.** Trois clients sont restés environ 15 minutes (maximum du simulateur : 900 s) avant d'abandonner sur mobile : candidats à une relance ciblée. La requête filtre d'abord, puis trie et limite : seules les sessions utiles sont triées.

### Q10 - Quels thèmes reviennent dans les avis négatifs ?

```js
db.retours_clients.aggregate([
  { $match: { "analyse.sentiment": "negatif" } },
  { $unwind: "$analyse.themes" },
  { $group: { _id: "$analyse.themes", nombre: { $sum: 1 } } },
  { $sort: { nombre: -1 } },
])
```

| Thème | Occurrences |
|---|---|
| frais | 64 |
| rapidite | 64 |
| litige | 62 |
| integration | 57 |
| support | 57 |

**Lecture.** `$unwind` éclate le tableau de thèmes pour compter chaque thème séparément. Les écarts sont faibles (57 à 64) et deux thèmes sont à égalité ; **l'ordre entre ex æquo n'est pas garanti** par le moteur, donc ne pas s'appuyer dessus.

## Ce que ces requêtes montrent de l'architecture

| Question | Moteur | Pourquoi ce moteur |
|---|---|---|
| Q1 à Q7 | entrepôt | agrégations sur de gros volumes, jointures en étoile, fonctions fenêtre |
| Q8 à Q10 | documents | schéma souple (le sous-document `erreur` n'existe que pour un type d'événement), tableaux à éclater |
| (hors liste) | OLTP | lectures et écritures unitaires (historique d'un client en 0,12 ms avec l'index, document 02 § 6) |

Les 7 requêtes SQL sont exécutées sur chaque jeu de test (`test_toutes_les_requetes_du_fichier_sql_s_executent`) ; la cohérence des agrégats qu'elles lisent est vérifiée (document 03 § 4).

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
