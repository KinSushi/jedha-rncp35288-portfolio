# Power BI : état du fichier livré et améliorations prévues

## État actuel du fichier `banking_dataops_monitoring.pbix` (relevé par lecture du fichier, non publié dans ce dépôt)

Une page, quatre visuels :

- une carte « contrôles » : `Controls Total`, `Controls Passed`, `Control Pass Rate` ;
- une carte « incidents et suspicion » : `Suspicious Rate`, `Suspicious Count`, `High Severity Incidents`, `Open Incidents` ;
- un graphique combiné à **cinq** mesures ;
- une jauge : `Rolling 7-Day Average` et `Publication Verdict`.

Huit requêtes, une par vue `vw_` (`public.vw_dim_*`, `public.vw_fact_*`, `public.vw_publication_verdict`), sur une base PostgreSQL managée (Neon) : le fichier n'est pas configuré sur `localhost` (voir `README.md`, § 8, pour le brancher sur la pile locale). Cinq relations en filtre simple. Fonctionnel, mais en deçà de la spécification du dépôt (`report-specification.md`).

## Priorité 1 : retouches du fichier existant

1. **Importer le thème sombre** : Affichage → Thèmes → Rechercher des thèmes → `theme-sombre.json`. Le canevas passe en sombre, cohérent avec le tableau de bord Streamlit.
2. **Éclater les cartes multi-champs** en cartes KPI individuelles avec titres français : « Contrôles réussis » · « Contrôles évalués » · « Taux de conformité » · « Lignes en violation » · « Transactions ».
3. **Assainir le graphique combiné** (5 mesures vers 2) : colonnes = `Total Amount CHF` ; ligne = `Rolling 7-Day Average` ; axe X = `vw_dim_date[date]` ; titre « Montants quotidiens et moyenne glissante sur 7 jours ».
4. **Donner un sens à la jauge** : champ = `Control Pass Rate` (format %), minimum 0, maximum 1, **cible 1,0** ; titre « Taux de conformité des contrôles ». Alternative : une carte « Verdict » avec mise en forme conditionnelle (PASS vert `#27C08D`, FAIL rouge `#FF5C5C`) sur `Publication Verdict`.
5. **Formats** : montants en CHF (format monétaire), dates courtes, libellés français.

## Priorité 2 : construire les pages de la spécification

Créer trois pages supplémentaires depuis les vues `vw_` (dix dans le code, dont huit pour le schéma en étoile) et les mesures de `dax-measures.md` :

- **Décision de publication** : verdict, contrôles en échec, lignes en violation par contrôle, historique des verdicts ;
- **Priorisation par le modèle** : file triée par score, distribution des scores, seuil, volume à examiner (nécessite d'exposer `gold.predictions` dans une vue) ;
- **Journal d'audit** : événements par type et par jour, table de détail (`run_id`, raison), zoom sur le dernier run.

## Notes techniques

- Sources : PostgreSQL local (pile Docker) ou base managée Neon ; voir `README.md` § 8 et `connexion-cloud-neon-supabase.md`. Les identifiants d'une base managée ne se saisissent jamais dans un fichier versionné.
- À l'enregistrement, Power BI Desktop re-signe le fichier (`SecurityBindings`) : c'est normal ; le manifeste d'intégrité de l'archive fige la version fournie.
