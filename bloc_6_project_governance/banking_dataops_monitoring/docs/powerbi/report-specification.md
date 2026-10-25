# Spécification du rapport

Quatre pages, graduées par audience. Chaque visuel est spécifié : type, champs, mise en forme.

Format : 16:9, 1280 × 720. Marges de 20 pixels.

---

## Page 1 : Verdict

**Audience** : direction. **Objectif** : la décision en trois secondes.

| Position | Visuel | Champs | Mise en forme |
|---|---|---|---|
| Bandeau, pleine largeur, h. 90 px | Carte | `[Publication Verdict]` | Police 40, couleur d'arrière-plan par champ sur `[Verdict Color]` |
| Ligne 1, quatre cartes | Carte | `[Controls Passed]` sur `[Controls Total]` | Étiquette « Contrôles réussis » |
| | Carte | `[Rows In Violation]` | Étiquette « Lignes en violation » |
| | Carte | `[Transaction Count]` | Étiquette « Transactions sous contrôle » |
| | Carte | `MAX(vw_fact_quality_results[executed_at])` | Étiquette « Dernière exécution » |
| Ligne 2, gauche | Table | `control_id`, `check_name`, `status`, `failed_rows`, `severity` | Mise en forme conditionnelle sur `status` : vert PASS, orange WARN, rouge FAIL |
| Ligne 2, droite | Anneau | `[Controls Passed]` et `[Controls Failed]` par `control_family` | Palette limitée à vert et rouge |
| Bas, pleine largeur | Zone de texte | Mention de démonstration sur données synthétiques | Police 10, gris |

Le bandeau est l'élément le plus important de tout le rapport. Il doit être lisible depuis le fond d'une salle.

---

## Page 2 : Contrôles

Audience : analyste qualité. Objectif : comprendre ce qui a échoué et pourquoi.

| Position | Visuel | Champs |
|---|---|---|
| Segment, haut gauche | Segment | `vw_dim_control[control_family]` |
| Segment, haut droite | Segment | `vw_dim_control[severity]` |
| Corps, gauche | Barres empilées horizontales | Axe `control_id`, valeur `[Controls Failed]`, légende `severity` |
| Corps, droite | Matrice | Lignes `control_family` puis `control_id`, colonnes `status`, valeurs nombre |
| Bas, gauche | Courbe | Axe `date_key`, valeur `[Control Pass Rate]` |
| Bas, droite | Table | Incidents ouverts : `incident_id`, `title`, `severity`, `created_at` |

Le segment par famille de contrôle porte l'argument du projet. Filtre sur « Structural » : aucun échec, jamais. Filtre sur « Business rule » : c'est là que tout se passe. La démonstration est visuelle, elle ne repose pas sur une affirmation.

---

## Page 3 : Volumétrie et réconciliation

Audience : métier. Objectif : ordres de grandeur et anomalies de volume.

| Position | Visuel | Champs |
|---|---|---|
| Ligne 1, trois cartes | Carte | `[Transaction Count]`, `[Total Amount CHF]`, `[Volume Change vs Previous Day]` |
| Corps, gauche | Courbe et histogramme groupés | Axe `date_key`, colonnes `[Transaction Count]`, courbe `[Rolling 7-Day Average]` |
| Corps, droite | Barres groupées | Axe `source_system`, valeurs `[Transaction Count]` et `[Total Amount CHF]` |
| Bas, gauche | Secteurs | `channel` par `[Transaction Count]` |
| Bas, droite | Table | `status`, `[Transaction Count]`, `[Total Amount CHF]` |
| Segment latéral | Segment de dates | `vw_dim_date[date_key]`, type intervalle |

La courbe glissante sur sept jours révèle les décrochages de volume : un lot partiellement chargé se voit immédiatement.

---

## Page 4 : Risque

Audience : conformité. Objectif : où se concentre le risque.

| Position | Visuel | Champs |
|---|---|---|
| Ligne 1, trois cartes | Carte | `[Suspicious Count]`, `[Suspicious Rate]`, `[High Risk Amount]` |
| Corps, gauche | Nuage de points | X `risk_score`, Y `amount_chf`, taille nombre, légende `is_suspicious` |
| Corps, droite | Carte choroplèthe ou barres | `transaction_country` par `[Suspicious Count]` |
| Bas, gauche | Barres | `merchant_category` par `[Average Risk Score]`, tri décroissant |
| Bas, droite | Table | Vingt transactions les plus risquées, tri par `risk_score` décroissant |

Ne présente jamais cette page comme de la détection de fraude. C'est de l'exploration de concentration de risque sur données synthétiques. La distinction est exactement ce que le référentiel attend en matière de prudence, et un auditeur la vérifiera.

---

## Interactions

| Réglage | Valeur |
|---|---|
| Filtres croisés entre visuels | Actifs par défaut |
| Segments | Page unique, sauf le segment de dates en synchronisation sur les pages 3 et 4 |
| Boutons de navigation | Un par page, en pied |
| Info-bulles | Page d'info-bulle dédiée pour le nuage de points, affichant l'identifiant de transaction |

---

## Accessibilité

| Point | Traitement |
|---|---|
| Contraste | Marine sur blanc, ratio supérieur à 12:1 |
| Daltonisme | Le verdict ne repose jamais sur la seule couleur : le texte l'énonce |
| Ordre de tabulation | Défini page par page, du verdict vers le détail |
| Texte de remplacement | Renseigné sur chaque visuel |

L'accessibilité est au programme du PL-300. Le point décisif est le premier : ne jamais faire porter une information par la couleur seule.

---

## Contrôle avant présentation

- [ ] Les huit vues sont importées, aucune table brute
- [ ] `vw_dim_date` est marquée comme table de dates
- [ ] Toutes les relations sont en filtre simple
- [ ] Les colonnes techniques sont masquées
- [ ] Le thème `theme.json` est appliqué
- [ ] Les mesures sont formatées
- [ ] Chaque page a un titre explicite
- [ ] Le rapport se rafraîchit sans erreur
- [ ] Filtrer sur « Structural » ne montre aucun échec

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
