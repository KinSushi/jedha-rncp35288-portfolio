# Mesures DAX

À coller dans une table `_Measures` dédiée. Chaque mesure porte un commentaire expliquant son intention : un relecteur du modèle demande systématiquement pourquoi une mesure est écrite ainsi.

---

## Verdict de publication

```dax
Controls Total = COUNTROWS ( vw_dim_control )
```

```dax
Controls Failed =
CALCULATE (
    COUNTROWS ( vw_fact_quality_results ),
    vw_fact_quality_results[status] = "FAIL"
)
```

```dax
Controls Passed =
CALCULATE (
    COUNTROWS ( vw_fact_quality_results ),
    vw_fact_quality_results[status] = "PASS"
)
```

```dax
Controls Warning =
CALCULATE (
    COUNTROWS ( vw_fact_quality_results ),
    vw_fact_quality_results[status] = "WARN"
)
```

```dax
-- Le verdict global. La hiérarchie compte : un échec l'emporte sur un
-- avertissement, et un avertissement n'est pas un blocage.
Publication Verdict =
VAR Failed = [Controls Failed]
VAR Warned = [Controls Warning]
RETURN
    SWITCH (
        TRUE (),
        Failed > 0, "BLOCKED",
        Warned > 0, "UNDER RESERVATION",
        "CLEARED"
    )
```

```dax
-- Couleur du bandeau. À brancher sur la mise en forme conditionnelle
-- « Couleur d'arrière-plan » → « Format par champ ».
Verdict Color =
SWITCH (
    [Publication Verdict],
    "BLOCKED", "#D64545",
    "UNDER RESERVATION", "#E0A03C",
    "#1B9E5A"
)
```

```dax
Rows In Violation =
CALCULATE (
    SUM ( vw_fact_quality_results[failed_rows] ),
    vw_fact_quality_results[status] = "FAIL"
)
```

```dax
-- Taux de réussite. DIVIDE plutôt que l'opérateur / : il gère la division
-- par zéro sans produire d'erreur, ce qui arrive avant la première exécution.
Control Pass Rate =
DIVIDE ( [Controls Passed], [Controls Total], BLANK () )
```

---

## Volumétrie

```dax
Transaction Count = COUNTROWS ( vw_fact_transactions )
```

```dax
Total Amount CHF = SUM ( vw_fact_transactions[amount_chf] )
```

```dax
Average Amount CHF = AVERAGE ( vw_fact_transactions[amount_chf] )
```

```dax
-- Volume hors lot de démonstration. Utile pour comparer un état réel
-- à un état dégradé sans fausser les moyennes.
Transaction Count Excl Demo =
CALCULATE (
    [Transaction Count],
    vw_fact_transactions[is_demo_row] = FALSE ()
)
```

---

## Time Intelligence

Ces mesures exigent que `vw_dim_date` soit marquée comme table de dates. Sans cela, elles renvoient des résultats faux sans lever d'erreur.

```dax
Transactions MTD =
TOTALMTD ( [Transaction Count], vw_dim_date[date_key] )
```

```dax
Amount MTD =
TOTALMTD ( [Total Amount CHF], vw_dim_date[date_key] )
```

```dax
Transactions Previous Day =
CALCULATE (
    [Transaction Count],
    DATEADD ( vw_dim_date[date_key], -1, DAY )
)
```

```dax
-- Variation journalière en pourcentage. Le BLANK() en troisième argument
-- de DIVIDE évite d'afficher 0 % là où il n'y a simplement rien à comparer.
Volume Change vs Previous Day =
VAR Today = [Transaction Count]
VAR Yesterday = [Transactions Previous Day]
RETURN
    DIVIDE ( Today - Yesterday, Yesterday, BLANK () )
```

```dax
Rolling 7-Day Average =
AVERAGEX (
    DATESINPERIOD ( vw_dim_date[date_key], MAX ( vw_dim_date[date_key] ), -7, DAY ),
    [Transaction Count]
)
```

---

## Risque

```dax
Suspicious Count =
CALCULATE (
    [Transaction Count],
    vw_fact_transactions[is_suspicious] = TRUE ()
)
```

```dax
Suspicious Rate =
DIVIDE ( [Suspicious Count], [Transaction Count], BLANK () )
```

```dax
Average Risk Score = AVERAGE ( vw_fact_transactions[risk_score] )
```

```dax
-- Montant exposé sur les transactions à risque élevé.
-- Le seuil de 0,7 est un paramètre métier, à calibrer avec les directions métier.
High Risk Amount =
CALCULATE (
    [Total Amount CHF],
    vw_fact_transactions[risk_score] >= 0.7
)
```

---

## Contrôles par famille

C'est la mesure qui rend visible l'argument central du projet.

```dax
Structural Controls =
CALCULATE (
    [Controls Total],
    vw_dim_control[control_family] = "Structural"
)
```

```dax
Business Rule Controls =
CALCULATE (
    [Controls Total],
    vw_dim_control[control_family] = "Business rule"
)
```

```dax
-- Les échecs ne peuvent survenir que sur les règles métier : le schéma
-- empêche structurellement les trois autres. Cette mesure le démontre
-- chiffre à l'appui plutôt que par affirmation.
Failures On Business Rules =
CALCULATE (
    [Controls Failed],
    vw_dim_control[control_family] = "Business rule"
)
```

---

## Incidents

```dax
Open Incidents =
CALCULATE (
    COUNTROWS ( vw_fact_incidents ),
    vw_fact_incidents[status] = "open"
)
```

```dax
High Severity Incidents =
CALCULATE ( [Open Incidents], vw_fact_incidents[severity] = "high" )
```

---

## Formatage

| Mesure | Format |
|---|---|
| `Total Amount CHF`, `Amount MTD`, `High Risk Amount` | Monétaire, CHF, 0 décimale |
| `Control Pass Rate`, `Suspicious Rate`, `Volume Change vs Previous Day` | Pourcentage, 1 décimale |
| `Average Risk Score` | Nombre décimal, 3 décimales |
| Tous les compteurs | Nombre entier, séparateur de milliers |

---

## Trois questions qu'un relecteur posera

« Pourquoi `DIVIDE` plutôt que l'opérateur `/` ? »
> Parce que `DIVIDE` gère la division par zéro sans produire d'erreur. Avant la première exécution des contrôles, le dénominateur est nul : avec `/`, le visuel afficherait une erreur au lieu d'un blanc.

« Pourquoi une table de dates séparée ? »
> Parce que les fonctions de Time Intelligence exigent un calendrier continu et sans trou. Une colonne de date issue d'une table de faits comporte des jours manquants, et les fonctions renvoient alors des résultats faux sans le signaler.

« Pourquoi aucune relation bidirectionnelle ? »
> Parce que le modèle comporte deux tables de faits reliées à la même dimension de dates. Un filtre bidirectionnel y créerait un chemin ambigu, que le moteur résout de façon non déterministe.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
