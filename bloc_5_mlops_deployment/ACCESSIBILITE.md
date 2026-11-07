# Contrôle d'accessibilité - tableau de bord et API Getaround

Contrôle fait le 19/10/2026, **en local** (tableau de bord Streamlit et API lancés sur le poste,
avec les commandes de la section « Lancer en local » du README). Aucune adresse publique n'existe à
cette date : le contrôle sur l'application déployée reste à faire après la mise en ligne.

Référentiel RNCP35288, compétence C5.3, critère « Accessibilité de l'interface web incluant
l'utilisation de l'interface de programmation applicative ». Grille utilisée : WCAG 2.1 du W3C,
niveau AA (critères cités entre parenthèses).

## 1. Méthode et outil

- **Méthode** : navigateur sans fenêtre, piloté par script, hors ligne (aucun service en ligne,
  aucune extension). Mesures faites :
  - contraste de chaque texte affiché (1.4.3 : 4,5:1, ou 3:1 pour un grand texte) et de chaque
    courbe ou barre (1.4.11 : 3:1), calculé par la formule de luminance relative de WCAG sur le
    fond réellement affiché ;
  - nom accessible de chaque commande (4.1.2), lu dans l'arbre d'accessibilité du navigateur ;
  - texte alternatif des graphiques et des images (1.1.1) ;
  - langue de la page (3.1.1), titres (1.3.1, 2.4.6) ;
  - clavier (2.1.1, 2.4.7) : parcours complet par la touche Tab, focus visible, curseur réglé aux
    flèches, formulaire envoyé par Entrée ;
  - téléphone (1.4.10) : fenêtre de 390 × 844 pixels, défilement horizontal.
- **Pages** : « Retards et délai minimal » (thème clair, thème sombre, téléphone), « Prix
  conseillé » (thème clair, téléphone), page `/docs` de l'API (poste et téléphone).
- **Avant / après** : la même mesure, lancée sur la version d'avant correction et sur la version
  corrigée, avec le même outil.

## 2. Résultats

| Contrôle | Avant correction | Après correction |
|---|---|---|
| Contraste des textes, thème clair, page des retards (textes contrôlés : 76 avant, 78 après) | 5 textes sous 4,5:1 : valeur du curseur (3,3:1), bornes 0 et 720 (3,7:1), deux variations grises (3,45:1) | **0** texte sous le seuil |
| Contraste des textes, thème sombre et page du prix | 0 texte sous le seuil | 0 texte sous le seuil |
| Contraste des courbes et des barres sur fond blanc (minimum 3:1) | orange 2,53:1, bleu 2,72:1 | orange 3,76:1, bleu 5,75:1 |
| Même contrôle sur fond sombre | 7,46:1 et 6,95:1 | 5,03:1 et 3,29:1 |
| Information portée par la seule couleur (1.4.1) | oui : deux courbes et deux séries de barres distinguées par la couleur seule | non : trait plein ou tirets, rond ou carré, barres pleines ou hachurées |
| Texte alternatif des deux graphiques (1.1.1) | aucun | un texte par graphique ; valeurs du diagramme des retards aussi en tableau |
| Langue de la page (3.1.1) | « en » (anglais) | « fr » |
| Commandes sans nom accessible (4.1.2) | 0 sur 28 | 0 sur 28 |
| Clavier : toutes les commandes atteintes par Tab, dans l'ordre de lecture | oui | oui |
| Clavier : curseur réglé aux flèches | oui (60 → 90 minutes, indicateurs recalculés) | oui |
| Clavier : formulaire du prix envoyé par Entrée | oui (« Prix conseillé : 135 € par jour ») | oui |
| Focus visible | oui sur les commandes (bordure rouge des champs, contour des boutons) | oui |
| Téléphone (390 pixels) : défilement horizontal | aucun | aucun |
| Titres | un titre de niveau 1 par page, puis des sous-titres | identique |
| API, page `/docs` | langue « fr », titres h1 puis h2 par endpoint, aucun défilement horizontal sur téléphone, aucune image sans texte alternatif | inchangé (déjà conforme) |
| API, messages d'erreur | en français, au format `{"error": ..., "details": [...]}`, avec la valeur en cause et les valeurs admises | inchangé |

## 3. Corrections apportées

| Écart | Correction | Fichier |
|---|---|---|
| Courbes et barres sous 3:1 sur fond blanc | orange `#d95f02`, bleu `#1565c0` | `dashboard/app.py` |
| Couleur seule pour distinguer les séries | trait et symbole par périmètre, motif de barre par type de check-in | `dashboard/app.py` |
| Graphiques sans texte alternatif | paramètre `alt` de `st.plotly_chart` ; volet « Valeurs du diagramme des retards (tableau) » | `dashboard/app.py` |
| Page déclarée en anglais | petit script qui déclare le français | `dashboard/app.py` |
| Textes du thème clair sous 4,5:1 | thème clair : rouge `#e0282f` (4,65:1), texte `#111111` (bornes du curseur à 4,95:1), gris `#5f6270` (6,06:1) | `dashboard/.streamlit/config.toml` |

Aucun chiffre affiché ne change : les indicateurs, les courbes et le prix (135 € pour la voiture
du formulaire) sont les mêmes avant et après.

## 4. Limites

- Contrôle **en local**, pas sur l'application déployée (aucune adresse publique à cette date).
- Les mesures ci-dessus portent sur les critères cités et ne couvrent pas tous les critères de
  WCAG 2.1.
- Aucun essai avec un lecteur d'écran : le nom des commandes et les textes alternatifs sont lus
  dans l'arbre d'accessibilité du navigateur, pas écoutés.
- Composants de Streamlit et de Plotly non modifiables simplement :
  - la barre d'outils des graphiques (zoom, export en image) est en anglais et entre dans le
    parcours au clavier ;
  - la zone principale de la page, qui défile, reçoit le focus sans contour visible ;
  - le menu principal de Streamlit est libellé en anglais ; le bouton « Deploy » est retiré par
    le réglage `toolbarMode = "viewer"` de `dashboard/.streamlit/config.toml`.
- Thème sombre : le bleu des courbes est à 3,29:1, au-dessus du seuil de 3:1 mais près de lui.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
