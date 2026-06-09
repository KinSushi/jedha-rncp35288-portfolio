# Validation

## Purpose

This file documents the public validation path for this sanitized Jedha evidence portfolio.

This repository is documentary by design. It is not expected to run a Python package. Validation focuses on file structure, public-safety boundaries and absence of private artifacts.

---

## Required public files

The following files must exist:

```text
README.md
PORTFOLIO.md
docs/evidence_index.md
docs/public_safety_rules.md
docs/certification_mapping.md
docs/portfolio_review_checklist.md
bloc_1_data_infrastructure/README.md
bloc_2_eda_statistics/README.md
bloc_3_machine_learning/README.md
bloc_4_nlp_deep_learning/README.md
bloc_5_mlops_deployment/README.md
bloc_6_project_governance/README.md
```

---

## Forbidden public artifacts

The repository must not contain:

```text
*.pdf
*.docx
*.xlsx
*.pptx
private/
school_documents/
certificates_private/
grades/
exam_subjects/
exam_answers/
cpf/
contracts/
invoices/
cv/
resume/
cover-letters/
applications/
job-tracker/
```

---

## PowerShell validation

```powershell
$Required = @(
  "README.md",
  "PORTFOLIO.md",
  "docs/evidence_index.md",
  "docs/public_safety_rules.md",
  "docs/certification_mapping.md",
  "docs/portfolio_review_checklist.md",
  "bloc_1_data_infrastructure/README.md",
  "bloc_2_eda_statistics/README.md",
  "bloc_3_machine_learning/README.md",
  "bloc_4_nlp_deep_learning/README.md",
  "bloc_5_mlops_deployment/README.md",
  "bloc_6_project_governance/README.md"
)

$Required | ForEach-Object {
  if (!(Test-Path $_)) { throw "Missing required file: $_" }
}

$ForbiddenExtensions = @("*.pdf", "*.docx", "*.xlsx", "*.pptx")
foreach ($Pattern in $ForbiddenExtensions) {
  $Matches = Get-ChildItem -Recurse -File -Filter $Pattern -ErrorAction SilentlyContinue
  if ($Matches) { throw "Forbidden file extension found: $Pattern" }
}

$ForbiddenFolders = @(
  "private", "school_documents", "certificates_private", "grades",
  "exam_subjects", "exam_answers", "cpf", "contracts", "invoices",
  "cv", "resume", "cover-letters", "applications", "job-tracker"
)

foreach ($Folder in $ForbiddenFolders) {
  if (Test-Path $Folder) { throw "Forbidden folder found: $Folder" }
}
```

---

## Public-safety validation

```powershell
Get-ChildItem -Recurse -File |
  Where-Object { $_.FullName -notmatch "\\.git\\" -and $_.FullName -notmatch "\\.venv\\" } |
  Select-String -Pattern "BEGIN .*PRIVATE KEY","gho_","api_key","secret","token","password"
```

Expected review notes:

- documentation may contain public-safety terms such as `secret` or `token`;
- matches in public-safety files are review items, not automatic leaks;
- real credentials must never appear.

---

## Boundary

Public technical evidence only. No CVs, cover letters, salary targets, private school documents, grades, exams, real client data, employer data, credentials or production decisioning claims.
