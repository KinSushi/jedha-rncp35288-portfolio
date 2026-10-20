# Rollback Plan — banking-dataops-monitoring

## Purpose

This rollback plan defines how to safely reset the synthetic local environment.

It is not a production rollback plan. It is public technical evidence showing operational thinking.

## Reset strategy

The local environment is disposable and synthetic. The safest rollback is to remove local containers and volumes, then regenerate the synthetic dataset.

```bash
make reset
```

## Manual rollback

```bash
docker compose down -v
docker compose up -d
python -m banking_dataops.generate_synthetic_data --output-dir data
python -m banking_dataops.ingest
python -m banking_dataops.quality_checks
python -m banking_dataops.reconciliation
```

## Pre-rollback checklist

- [ ] confirm this is the local synthetic environment;
- [ ] no real data has been loaded;
- [ ] current failure has been documented if relevant;
- [ ] no private logs or screenshots will be published.

## Public-safety note

Do not use this rollback pattern for a production system. It is a portfolio artifact for synthetic local data only.
