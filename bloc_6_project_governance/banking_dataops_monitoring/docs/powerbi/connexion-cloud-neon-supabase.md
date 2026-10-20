# Connecter Power BI à une base PostgreSQL cloud gratuite

Objectif : rendre le schéma en étoile joignable depuis Power BI Desktop **et**
depuis Power BI Service, sans exposer le poste local.

---

## Pourquoi une base cloud

Le pipeline écrit dans un PostgreSQL local (Docker ou natif). Cela suffit pour
une démonstration en direct sur le poste du présentateur, mais pas pour :

- un rapport **publié** sur Power BI Service (il ne peut pas atteindre `localhost`) ;
- un jury qui ouvre le rapport depuis sa propre machine ;
- un plan de secours si Docker refuse de démarrer le jour J.

La parade : publier le lot dans une base managée gratuite, puis pointer Power BI
dessus. Le script `scripts/publish_to_cloud.py` fait la recopie.

> **Plan de secours.** Une fois le lot publié dans le cloud, la démonstration
> Power BI ne dépend plus du tout de Docker ni du réseau local. C'est le seul
> composant de la soutenance qui survit à une panne complète du poste.

---

## Choix du fournisseur

| Critère | Neon | Supabase |
|---|---|---|
| Offre gratuite | 0,5 Go, projet illimité dans le temps | 500 Mo, projet mis en pause après 7 j d'inactivité |
| PostgreSQL | 16 / 17 | 15 / 17 |
| Mise en veille | reprise à froid ~500 ms | réveil manuel depuis le dashboard |
| Chaîne de connexion | fournie telle quelle | onglet *Connection string* → **URI** |
| Recommandé ici | **oui** — rien à réveiller avant la soutenance | acceptable si déjà utilisé |

Volume réellement publié par ce projet : **environ 3 200 lignes**, très en deçà
des deux offres. Le critère décisif est la mise en veille, pas la taille.

> Le jour de la présentation, une base Supabase en pause coûte plusieurs minutes de
> réveil. Neon évite ce risque. Si Supabase est déjà en place, ouvrir le
> dashboard **la veille** et lancer une requête pour maintenir le projet actif.

---

## Étape 1 — Créer la base

### Neon

1. Créer un compte sur `neon.tech`, puis un projet (région : Europe pour la latence).
2. Dans *Dashboard → Connection string*, choisir **Parameters only** ou l'URI complète.
3. La chaîne ressemble à :

```
postgresql://<user>:<password>@ep-xxxx-xxxx.eu-central-1.aws.neon.tech/<db>?sslmode=require
```

### Supabase

1. Créer un projet sur `supabase.com`.
2. *Project Settings → Database → Connection string → URI*.
3. Utiliser le **port 5432** (session pooler). Le port 6543 est le *transaction
   pooler* : il ne supporte pas les instructions préparées et fait échouer
   certaines opérations de recopie.

```
postgresql://postgres.<ref>:<password>@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require
```

---

## Étape 2 — Publier le lot

```bash
# 1. produire un lot vert en local
python run_pipeline.py --seed 42 --transactions 1500 --customers 100 --train
python -m banking_dataops.ml.predict   # indispensable : --train entraîne, il ne score pas
python -m banking_dataops.powerbi_views
python scripts/smoke_check.py          # doit afficher DEMO READY

# 2. publier vers le cloud
export BANKING_DATAOPS_CLOUD_DSN="postgresql://user:pwd@host/db?sslmode=require"
python scripts/publish_to_cloud.py --dry-run   # contrôle à blanc, n'écrit rien
python scripts/publish_to_cloud.py             # publication réelle
python scripts/publish_to_cloud.py --verify    # compare les volumes des deux côtés
```

Sous PowerShell, remplacer `export` par :

```powershell
$env:BANKING_DATAOPS_CLOUD_DSN = "postgresql://user:pwd@host/db?sslmode=require"
```

Ce que le script fait, dans l'ordre : applique le DDL (`00_schema.sql`,
`07_audit_journal.sql`, `08_gold_layer.sql`), recopie les 14 tables via
`COPY ... CSV`, recrée les 8 vues du schéma en étoile
(`06_powerbi_star_schema.sql`), puis compare les volumes table par table et lit
le verdict publié.

Le mot de passe n'apparaît jamais dans la sortie : la chaîne est masquée.

---

## Étape 3 — Connecter Power BI Desktop

1. **Obtenir les données → Base de données PostgreSQL**.
2. Renseigner :

   | Champ | Valeur |
   |---|---|
   | Serveur | l'hôte de la chaîne, **port inclus** — ex. `ep-xxx.eu-central-1.aws.neon.tech:5432` |
   | Base de données | le nom de base de la chaîne (`banking_dataops`, ou `postgres` chez Supabase) |
   | Mode | **Import** |

3. Authentification : **Base de données**, avec l'utilisateur et le mot de passe
   de la chaîne.
4. Niveau de confidentialité : **Organizational**.

**Import plutôt que DirectQuery** : le modèle fait quelques milliers de lignes.
L'import est instantané, fonctionne hors ligne une fois chargé, et supprime la
dépendance réseau pendant la soutenance. DirectQuery n'apporterait ici qu'une
latence supplémentaire et un point de panne.

### Si la connexion échoue

| Symptôme | Cause probable | Correction |
|---|---|---|
| `SSL connection required` | `sslmode` absent | ajouter `?sslmode=require` à la chaîne |
| Délai d'attente dépassé | base Supabase en pause | ouvrir le dashboard, lancer une requête |
| `prepared statement already exists` | port 6543 (transaction pooler) | basculer sur le port **5432** |
| Erreur de certificat | inspection TLS d'entreprise | tester depuis un autre réseau |

---

## Étape 4 — Tables à charger

Charger les **7 vues** du schéma en étoile, pas les tables brutes :

| Vue | Rôle | Volume indicatif |
|---|---|---|
| `vw_dim_date` | dimension date | 91 |
| `vw_dim_customer` | dimension client | 100 |
| `vw_dim_account` | dimension compte | 200 |
| `vw_dim_control` | dimension contrôle | 7 |
| `vw_fact_transactions` | table de faits | 1 500 |
| `vw_fact_quality_results` | résultats de contrôles | 7 |
| `vw_fact_incidents` | incidents | variable |

Ajouter `vw_publication_verdict` : une seule ligne, qui alimente la carte KPI
« lot publiable ou non ».

Les vues sont préférées aux tables : elles portent les libellés métier, les
indicateurs `is_pass` / `is_fail` / `is_warn` déjà calculés, et isolent le
rapport des changements de structure interne.

---

## Étape 5 — Relations du modèle

Power BI détecte souvent mal les cardinalités. À poser explicitement, toutes en
**un-vers-plusieurs**, filtre **simple**, de la dimension vers le fait :

```
vw_dim_date[date_key]           1 --* vw_fact_transactions[date_key]
vw_dim_account[account_id]      1 --* vw_fact_transactions[account_id]
vw_dim_customer[customer_id]    1 --* vw_dim_account[customer_id]
vw_dim_control[control_id]      1 --* vw_fact_quality_results[control_id]
vw_dim_date[date_key]           1 --* vw_fact_quality_results[date_key]
```

Marquer `vw_dim_date` comme **table de dates** (*Modélisation → Marquer comme
table de dates*, colonne `date_key`) : sans cela, les fonctions de time
intelligence DAX ne fonctionnent pas.

Les mesures DAX sont dans [`dax-measures.md`](dax-measures.md), le thème visuel
dans [`theme.json`](theme.json), la maquette du rapport dans
[`report-specification.md`](report-specification.md).

---

## Republier après un nouveau lot

`publish_to_cloud.py` fait un `TRUNCATE` puis un `COPY` : il est **rejouable**
sans état résiduel. Après un nouveau run du pipeline :

```bash
python run_pipeline.py --seed 42 --transactions 1500 --customers 100 --train
python scripts/publish_to_cloud.py
```

Puis, dans Power BI : **Actualiser**.

---

## Sécurité

- La chaîne de connexion contient un mot de passe : elle vit dans une variable
  d'environnement, **jamais** dans un fichier versionné. `.env` est dans
  `.gitignore`, seul `.env.example` est publié.
- Les données publiées sont **entièrement synthétiques** (`generate_synthetic_data`,
  `seed=42`). Aucune donnée personnelle réelle ne quitte le poste — c'est ce qui
  rend cette publication cloud acceptable dans un contexte bancaire.
- Le compte utilisé par Power BI peut être limité à la lecture :

```sql
CREATE ROLE powerbi_reader LOGIN PASSWORD '***';
GRANT CONNECT ON DATABASE banking_dataops TO powerbi_reader;
GRANT USAGE ON SCHEMA public, gold TO powerbi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public, gold TO powerbi_reader;
```

---

## Vérification avant la soutenance

```bash
python scripts/publish_to_cloud.py --verify
```

Attendu : `volumes identiques.` — chaque table affiche le même compte des deux
côtés. Si un écart apparaît, republier.
