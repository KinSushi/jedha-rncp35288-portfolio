# Validation

## Static validation

``powershell
python -m compileall -q src tests
python -m pytest -q --maxfail=1
python -m ruff check .
``

## Public-safety validation

``powershell
Get-ChildItem -Recurse -File |
  Where-Object { $_.FullName -notmatch "\\.git\\" -and $_.FullName -notmatch "\\.venv\\" } |
  Select-String -Pattern "BEGIN .*PRIVATE KEY","gho_","api_key","secret","token","password"
``

Placeholder matches in .env.example, .gitignore and documentation require manual review.

## Boundary

Public technical evidence only. No CVs, cover letters, salary targets, private school documents, real client data, employer data, credentials or production decisioning claims.