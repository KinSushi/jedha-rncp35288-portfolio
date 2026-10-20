# Validation

## Purpose

This file documents the local and CI validation path for this repository.

## Static validation

```powershell
python -m compileall -q src tests dashboard
python -m pytest -q --maxfail=1
python -m ruff check .
```

## DataOps execution checks

```powershell
python -m banking_dataops.generate_synthetic_data --output-dir data --customers 5 --transactions 20
python -m banking_dataops.quality_checks
python -m banking_dataops.reconciliation
```

Database-backed commands require PostgreSQL through Docker Compose:

```powershell
docker compose up -d
python -m banking_dataops.ingest
python -m banking_dataops.quality_checks
python -m banking_dataops.reconciliation
```

## Public-safety validation

```powershell
Get-ChildItem -Recurse -File |
  Where-Object { $_.FullName -notmatch "\\.git\\" -and $_.FullName -notmatch "\\.venv\\" } |
  Select-String -Pattern "BEGIN .*PRIVATE KEY","gho_","api_key","secret","token","password"
```

Expected review notes:

- `.env.example` may contain local placeholder names.
- `.gitignore` and documentation may contain safety words such as `secret` or `password`.
- Real credentials must never appear.

## Portfolio rule

This repository is public technical evidence. It must not contain CVs, cover letters, salary targets, private school documents, real client data, employer data, credentials or any claim of connection to a production system.
