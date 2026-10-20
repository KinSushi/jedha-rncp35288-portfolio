# `tools/` — régénération des figures

Ce script existe pour une raison simple : **un livrable qu'on ne sait pas
régénérer n'est pas reproductible**. Les copies d'écran affichent des chiffres
(nombre de tests, ROC-AUC 0,970, 7/7 contrôles). Si ces chiffres changent et
que personne ne sait reconstruire les artefacts, la documentation ment sans que
personne s'en aperçoive.

| Script | Produit | Entrée |
|---|---|---|
| `build_screenshots.py` | 5 SVG dans `docs/screenshots/` (`test-smoke-check`, `test-suite-pytest`, `test-reverse-paths`, `pipeline-run`, `powerbi-star-schema`) | `evidence/*.txt` |

`docs/screenshots/architecture-pipeline.svg` est dessiné à la main : il n'est pas produit par ce script.

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

# 3. Convertir en PNG
python -c "
import cairosvg, pathlib
for n in ['test-smoke-check','test-suite-pytest','test-reverse-paths','pipeline-run','powerbi-star-schema','architecture-pipeline']:
    cairosvg.svg2png(url=f'docs/screenshots/{n}.svg', write_to=f'docs/screenshots/{n}.png', scale=2)
"
```

## Principe

Aucun chiffre n'est saisi à la main dans ce script à partir d'une capture
d'écran ou d'un souvenir : `build_screenshots.py` lit les sorties brutes de
`evidence/`. Si le pipeline change, on rejoue la chaîne — pas on retouche
une image.

## Dépendances

- `cairosvg` (rendu SVG → PNG) : `pip install cairosvg`

## Chemins

Le script résout ses chemins **relativement à la racine du dépôt**, pas en
absolu. Il tourne donc chez n'importe qui, pas seulement sur la machine qui l'a
écrit. `build_screenshots.py` accepte `BANKING_DATAOPS_EVIDENCE_DIR` pour
pointer un autre dossier de preuves.
