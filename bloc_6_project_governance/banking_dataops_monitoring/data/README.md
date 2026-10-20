# Data folder

CSV files are generated locally with:

```bash
make generate
```

Every timestamp is derived from a **date anchor**, not from the clock. By default the anchor is
`2026-07-27T00:00:00Z`, so the same seed gives the same bytes on any day (batch fingerprint
`89ac138fb9943f00`, ADR-0009). To choose another anchor:

```bash
python -m banking_dataops.generate_synthetic_data --generated-at 2026-09-01T00:00:00Z
BANKING_DATAOPS_GENERATED_AT=2026-09-01 python run_pipeline.py     # the option wins over the variable
python run_pipeline.py --generated-at today                         # midnight UTC today: fresh, not reproducible
# In the containers: the variable below is forwarded by docker-compose.yml (shell or .env), or use
#   .\run.ps1 start -GeneratedAt today          (Windows)
#   sh docker/entrypoint.sh --generated-at today   (the container command, with this option added)
```

The freshness control CTRL-007 compares the newest event with the wall clock, not with the anchor: a batch
generated with the default anchor reads `WARN` from 25 September 2026. Use `--generated-at today` for a demonstration.

Generated files are synthetic and ignored by Git:

```text
synthetic_customers.csv
synthetic_accounts.csv
synthetic_transactions.csv
```

Do not place real banking, insurance, health, client, employer or private data in this folder.
