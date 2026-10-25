# `tools/` : régénération des figures

Ces scripts existent pour une raison simple : **un livrable qu'on ne sait pas
régénérer n'est pas reproductible**. Les copies d'écran affichent des chiffres
(nombre de tests, ROC-AUC 0,970, 7 contrôles persistés). Si ces chiffres changent et
que personne ne sait reconstruire les artefacts, la documentation ment sans que
personne s'en aperçoive.

| Script | Produit | Entrée |
|---|---|---|
| `build_screenshots.py` | 5 SVG dans `docs/screenshots/` (`test-smoke-check`, `test-suite-pytest`, `test-reverse-paths`, `pipeline-run`, `powerbi-star-schema`) | `evidence/*.txt` |
| `render_png.py` (non publié dans ce dépôt) | le PNG de chacun de ces 5 SVG, de `docs/screenshots/architecture-pipeline.svg`, de `assets/banner.svg` et de `assets/social-preview.svg` | les SVG |

`docs/screenshots/architecture-pipeline.svg` est dessiné à la main, d'après le code : il n'est pas produit par
`build_screenshots.py`, et ses chiffres viennent de `evidence/`.

## Chaîne complète

```bash
# 1. Produire les preuves réelles (nécessite PostgreSQL et la base peuplée)
mkdir -p evidence
python run_pipeline.py --seed 42 --transactions 1500 --customers 100 --train \
  2>&1 | grep -viE "audit_event_not_persisted|the journal" > evidence/pipeline_full.txt
python -m banking_dataops.ml.predict
python -m banking_dataops.powerbi_views 2>&1 > evidence/powerbi_views.txt
python scripts/smoke_check.py 2>&1 > evidence/smoke_check_full.txt
python -m pytest -q --no-header 2>&1 | grep -vE "^$" > evidence/pytest_full.txt
python -m pytest tests/test_reverse_paths.py -v --no-header 2>&1 \
  | grep -E "PASSED|SKIPPED|passed|skipped" > evidence/reverse_full.txt

# 2. Rendre les copies d'écran
python tools/build_screenshots.py

# 3. Convertir en PNG : Microsoft Edge sans interface, puis pymupdf
python tools/render_png.py                         # les huit figures, PNG écrits à côté des SVG (script non publié ici)
python tools/render_png.py --sortie ../rendu       # même rendu dans un autre dossier, pour comparer
```

## Rendu des PNG

Les PNG enregistrés sont produits ainsi : Microsoft Edge sans interface ouvre chaque SVG seul dans une page dont la
taille est celle du SVG, sans marge, et l'imprime en PDF (`--print-to-pdf`) ; pymupdf rastérise cette page unique,
à l'échelle 2 pour les figures de `docs/screenshots/` et pour la bannière, à l'échelle 1 pour l'aperçu social ;
Pillow recadre et enregistre le PNG, avec un fond transparent pour la bannière seule.

Contrôle du 24 octobre 2026, sous Windows : à partir des SVG enregistrés, `render_png.py` redonne au pixel près les
huit PNG du dépôt. L'option `--screenshot` d'Edge n'est pas employée : sur la machine qui a produit ces images, elle
ne rendait pas la main. Le script n'a été essayé que sous Windows ; le chemin d'Edge se change par la variable
`BANKING_DATAOPS_EDGE`.

## Principe

Aucun chiffre n'est saisi à la main dans ce script à partir d'une capture
d'écran ou d'un souvenir : `build_screenshots.py` lit les sorties brutes de
`evidence/`. Si le pipeline change, on rejoue la chaîne : pas on retouche
une image.

## Dépendances

- Microsoft Edge, pour le rendu du SVG et l'impression en PDF ;
- `pymupdf` et `Pillow`, pour passer du PDF au PNG : `pip install pymupdf pillow`. Ni l'un ni l'autre n'est dans
  `requirements-dev.txt`.

## Chemins

Les deux scripts résolvent leurs chemins relativement à la racine du dépôt, pas
en absolu. `build_screenshots.py` tourne donc chez n'importe qui, et accepte
`BANKING_DATAOPS_EVIDENCE_DIR` pour pointer un autre dossier de preuves ;
`render_png.py` demande en plus Edge, dont le chemin par défaut est celui de
Windows.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
