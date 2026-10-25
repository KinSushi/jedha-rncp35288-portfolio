# `scripts/` : outils d'exploitation

| Script | Rôle | Sortie attendue |
|---|---|---|
| `smoke_check.py` | valide le lot de bout en bout : silver, contrôles, gold, ML, journal, garantie ADR-0002 | `DEMO READY - 15 checks passed` |
| `smoke_test.sh` | variante shell, pour un enchaînement CI | code de sortie 0 |
| `preflight.sh` | vérifie les prérequis avant une démonstration | code de sortie 0 |
| `publish_to_cloud.py` | publie le lot vers Neon ou Supabase pour Power BI | `PUBLIÉ : N lignes, …` (le script écrit « 9 vues » en dur ; le schéma en étoile en compte 8, voir `evidence/powerbi_views.txt`) |

## `smoke_check.py`

Quinze contrôles, six sections. Le dernier vérifie qu'aucun score de modèle
n'atteint le verdict de publication (ADR-0002) : en lisant le code source de
l'endpoint, pas la documentation.

Le comptage des violations d'invariants est **borné au dernier run pipeline
complet**. Le journal d'audit est append-only et jamais purgé : un comptage sur
tout l'historique deviendrait rouge définitivement dès qu'un reverse test
enregistre une violation attendue, ou qu'une injection de démonstration est
rejouée en répétition.

```bash
python scripts/smoke_check.py
```

## `publish_to_cloud.py`

Un rapport Power BI publié ne peut pas atteindre un PostgreSQL local. Ce script
recopie le lot vers une base managée gratuite et y recrée le schéma en étoile.

```bash
export BANKING_DATAOPS_CLOUD_DSN="postgresql://user:pwd@host/db?sslmode=require"
python scripts/publish_to_cloud.py --dry-run   # contrôle à blanc
python scripts/publish_to_cloud.py             # publication
python scripts/publish_to_cloud.py --verify    # comparaison des volumes
```

Le mot de passe n'est jamais journalisé. `sslmode=require` est ajouté d'office
s'il manque : Neon et Supabase refusent les connexions en clair.

Procédure complète : [`docs/powerbi/connexion-cloud-neon-supabase.md`](../docs/powerbi/connexion-cloud-neon-supabase.md)

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
