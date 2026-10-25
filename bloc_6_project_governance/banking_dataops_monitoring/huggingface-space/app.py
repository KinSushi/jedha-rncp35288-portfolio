"""Tableau de bord de monitoring live : banking-dataops-monitoring.

Application autonome, volontairement indépendante du package `banking_dataops`. Elle affiche, en lecture seule, le
verdict de publication et les contrôles d'une base PostgreSQL cloud.

CHOIX
-----
Retenu :

- Un fichier unique, sans import relatif ni dépendance au package `banking_dataops`. Ce Space est un point de
  défaillance
  public et visible ; le package interne est riche, versionné avec le reste du dépôt, et n'est pas fait pour
  être
  installé isolément. La surface de risque d'un build cassé devant l'auditoire se réduit à une chose : la connexion à la
  base.
- La lecture seule, jamais l'écriture : cohérent avec ADR-0002 (le verdict de publication ne dépend jamais d'un score de
  modèle) et avec ADR-0006 (API en lecture seule). Un Space public qui permettrait d'écrire dans la base serait une
  porte
  ouverte à n'importe quel visiteur.
- Une connexion, toutes les requêtes, fermeture garantie, résultat gardé 30 secondes (`st.cache_data(ttl=30)`) : les
  visiteurs multiples partagent le même résultat plutôt que de rouvrir une connexion à chaque rendu, la base cloud
  gratuite
  limitant le nombre de connexions.
- Les mêmes variables d'environnement `BANKING_DATAOPS_DATABASE_*` que le reste du projet, pour ne pas introduire une
  seconde convention de configuration. Une valeur vide est traitée comme absente : certaines plateformes exposent un
  secret
  non renseigné comme une chaîne vide, et libpq rejette `sslmode=` vide avec une erreur peu lisible.

Écarté :

- Importer `banking_dataops.config` pour construire le DSN : cela rattacherait le Space au package, ce que le premier
  choix
  évite.
- Une action d'écriture ou de relance des contrôles depuis la page : voir la lecture seule ci-dessus.

OÙ
---
Rôle : page publique du Space Hugging Face (dossier `huggingface-space/`), lancée par Streamlit. Elle lit la base cloud
alimentée par `scripts/publish_to_cloud.py` : le même schéma en étoile que Power BI (`vw_publication_verdict`,
`vw_fact_quality_results`, `vw_dim_control`, définies dans `sql/06_powerbi_star_schema.sql`), plus `gold.predictions`,
`audit_events` et les tables de volumétrie. Aucune donnée personnelle : le générateur est synthétique (seed 42).
Dépendances : streamlit, pandas, psycopg. Appelant : le runtime du Space. Aucun module du projet ne l'importe.

COMMENT
-------
1. `_dsn` construit la chaîne de connexion depuis l'environnement ; sans hôte, utilisateur ou mot de passe, elle lève
   `ConfigurationError`.
2. `load_snapshot` ouvre une connexion et lit en six requêtes : le verdict, les contrôles, la volumétrie, les bandes de
   priorité, le modèle en production et le dernier événement d'audit.
3. La page affiche la carte du verdict, le tableau des sept contrôles, la volumétrie et la priorisation. Une
   configuration
   absente et une panne de base sont deux cas distincts (`ConfigurationError` contre toute autre exception), chacun
   avec son
   message, puis `st.stop()`.
4. L'actualisation automatique, si elle est activée, attend 30 secondes puis relance la page.
Tests qui le prouvent : aucun test automatisé ne couvre cette page, qui exige Streamlit et une base joignable. Elle se
vérifie
à la main sur le Space, par une capture du verdict en direct (dossier de preuves de travail, non inclus dans
l'archive de certification).

POURQUOI
--------
- Verdict indépendant du modèle : ADR-0002. Lecture seule de l'API sœur : ADR-0006.
- `ConfigurationError` distincte d'une panne réseau, pour que le message affiché oriente vers la bonne action
  (renseigner les
  secrets du Space, ou réveiller la base).
- `ttl=30` et `connect_timeout=8` : valeurs choisies par le projet, source : inconnue (aucune mesure de la limite de
  connexions
  de la base cloud).

JOURNALISATION
--------------
Aucune : l'application n'utilise pas `logging`. Les pannes sont affichées à l'écran (`st.error`, détail technique dans
un
volet repliable). Le DSN n'est jamais affiché, ni le mot de passe ; seul le message de psycopg l'est en cas de panne de
connexion.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import psycopg
import streamlit as st

# ---------------------------------------------------------------- configuration

st.set_page_config(
    page_title="banking-dataops-monitoring - Live",
    page_icon="🏦",
    layout="wide",
)

NAVY = "#0D1B2A"
PANEL = "#16283C"
LINE = "#2C4159"
FG = "#E8EDF4"
MUTED = "#8BA3BF"
GOLD = "#E8B64C"
GREEN = "#3DDC84"
RED = "#FF5C5C"

GITHUB_URL = os.environ.get("BANKING_DATAOPS_GITHUB_URL", "")


class ConfigurationError(RuntimeError):
    """Levée quand les secrets requis sont absents ou vides : distincte d'une
    panne réseau, pour que le message affiché oriente vers la bonne action."""


def _dsn() -> str:
    """Construit le DSN depuis les mêmes variables d'environnement que le
    reste du projet (BANKING_DATAOPS_DATABASE_*), pour ne pas introduire une
    seconde convention de configuration à maintenir.

    Une valeur vide ("") est traitée comme absente, pas comme une valeur
    valide : certaines plateformes exposent un secret non renseigné comme une
    chaîne vide plutôt que comme une variable manquante, et libpq rejette
    `sslmode=` vide avec une erreur peu lisible ('invalid sslmode value: ""').
    """

    def env(name: str, default: str = "") -> str:
        value = os.environ.get(name, "").strip()
        return value or default

    host = env("BANKING_DATAOPS_DATABASE_HOST")
    user = env("BANKING_DATAOPS_DATABASE_USER")
    password = env("BANKING_DATAOPS_DATABASE_PASSWORD")
    if not host or not user or not password:
        raise ConfigurationError(
            "Secrets manquants : BANKING_DATAOPS_DATABASE_HOST, _USER et "
            "_PASSWORD doivent être renseignés (Space → Settings → Variables "
            "and secrets)."
        )

    port = env("BANKING_DATAOPS_DATABASE_PORT", "5432")
    name = env("BANKING_DATAOPS_DATABASE_NAME", "banking_dataops")
    sslmode = env("BANKING_DATAOPS_DATABASE_SSLMODE", "require")
    return f"host={host} port={port} dbname={name} user={user} password={password} sslmode={sslmode}"


# ---------------------------------------------------------------- style

st.markdown(
    f"""
    <style>
    .stApp {{ background-color: {NAVY}; }}
    h1, h2, h3, p, span, div {{ color: {FG}; }}
    .verdict-pass {{
        background: linear-gradient(135deg, #123322, #0D1B2A);
        border: 1px solid {GREEN}; border-radius: 12px; padding: 24px;
        text-align: center;
    }}
    .verdict-fail {{
        background: linear-gradient(135deg, #331414, #0D1B2A);
        border: 1px solid {RED}; border-radius: 12px; padding: 24px;
        text-align: center;
    }}
    .verdict-value {{ font-size: 46px; font-weight: 700; margin: 0; }}
    .muted {{ color: {MUTED}; font-size: 13px; }}
    .card {{
        background-color: {PANEL}; border: 1px solid {LINE};
        border-radius: 10px; padding: 18px 20px; margin-bottom: 12px;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- data access


@st.cache_data(ttl=30, show_spinner=False)
def load_snapshot() -> dict[str, Any]:
    """Une connexion, toutes les requêtes, fermeture garantie.

    ttl=30 : les visiteurs multiples pendant la démonstration partagent le
    même résultat pendant 30 secondes plutôt que de rouvrir une connexion à
    chaque rendu : la base cloud gratuite a un nombre de connexions limité.
    """
    with psycopg.connect(_dsn(), connect_timeout=8) as conn:

        def q(sql: str) -> pd.DataFrame:
            with conn.cursor() as cur:
                cur.execute(sql)
                description = cur.description
                assert description is not None, "a SELECT always describes its columns"
                cols = [c.name for c in description]
                return pd.DataFrame(cur.fetchall(), columns=cols)

        verdict = q("SELECT * FROM vw_publication_verdict")
        controls = q(
            """
            SELECT d.control_id, d.check_name, d.severity, f.status,
                   f.failed_rows, f.executed_at
            FROM vw_fact_quality_results f
            JOIN vw_dim_control d ON d.control_id = f.control_id
            ORDER BY d.severity_rank, d.control_id
            """
        )
        volumes = q(
            """
            SELECT 'customers' AS table_name, COUNT(*) AS rows FROM customers
            UNION ALL SELECT 'accounts', COUNT(*) FROM accounts
            UNION ALL SELECT 'transactions', COUNT(*) FROM transactions
            UNION ALL SELECT 'gold.fact_transactions', COUNT(*) FROM gold.fact_transactions
            UNION ALL SELECT 'gold.predictions', COUNT(*) FROM gold.predictions
            """
        )
        priorities = q(
            """
            SELECT priority_band, COUNT(*) AS n
            FROM gold.predictions
            GROUP BY priority_band
            ORDER BY CASE priority_band
                WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
            """
        )
        model = q(
            """
            SELECT DISTINCT model_name, model_version
            FROM gold.predictions
            ORDER BY 1 LIMIT 1
            """
        )
        last_run = q("SELECT MAX(occurred_at) AS last_event FROM audit_events")

    return {
        "verdict": verdict,
        "controls": controls,
        "volumes": volumes,
        "priorities": priorities,
        "model": model,
        "last_run": last_run,
        "fetched_at": datetime.now(UTC),
    }


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.markdown("### banking-dataops-monitoring")
    st.caption("Tableau de bord live, lecture seule")
    st.markdown("---")
    auto = st.toggle("Actualisation automatique (30 s)", value=False)
    if st.button("Actualiser maintenant", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.markdown("---")
    st.caption(
        "Données entièrement synthétiques (générateur déterministe, seed=42). "
        "Ce tableau de bord ne fait que lire la base : aucune écriture n'est "
        "possible depuis cette page."
    )
    if GITHUB_URL:
        st.markdown(f"[Code source]({GITHUB_URL})")

# ---------------------------------------------------------------- header

st.markdown(
    """
    <div style="display:flex;justify-content:space-between;align-items:baseline;">
      <div>
        <h1 style="margin-bottom:0;">banking-dataops-monitoring</h1>
        <p class="muted" style="margin-top:4px;">
          Ce lot de données bancaires est-il publiable ? Verdict en direct
        </p>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- data load

data: dict[str, Any] | None
try:
    data = load_snapshot()
    connection_error: Exception | None = None
    config_error: ConfigurationError | None = None
except ConfigurationError as exc:
    data = None
    connection_error = None
    config_error = exc
except Exception as exc:  # noqa: BLE001 toute panne réseau/DB est affichée, pas masquée
    data = None
    connection_error = exc
    config_error = None

if config_error is not None:
    st.error(str(config_error), icon="🔑")
    st.stop()

if connection_error is not None:
    st.error(
        "Base de données injoignable pour l'instant.\n\n"
        "Si elle tourne sur une offre gratuite (Neon, Supabase), elle peut être "
        "en veille après une période d'inactivité : le premier réveil prend "
        "quelques secondes. Cliquez sur *Actualiser maintenant* dans la barre "
        "latérale.",
        icon="⚠️",
    )
    with st.expander("Détail technique"):
        st.code(str(connection_error))
    st.stop()

assert data is not None, "the two branches above stop the script when the load failed"
verdict_row = data["verdict"].iloc[0]
is_pass = bool(verdict_row["can_publish"])

# ---------------------------------------------------------------- verdict card

col_verdict, col_stats = st.columns([1, 2])

with col_verdict:
    css_class = "verdict-pass" if is_pass else "verdict-fail"
    color = GREEN if is_pass else RED
    st.markdown(
        f"""
        <div class="{css_class}">
          <p class="muted" style="letter-spacing:1.5px;">VERDICT DE PUBLICATION</p>
          <p class="verdict-value" style="color:{color};">{verdict_row["verdict"]}</p>
          <p class="muted">can_publish = {"true" if is_pass else "false"}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

with col_stats:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contrôles passés", f"{verdict_row['controls_passed']}/{verdict_row['controls_total']}")
    c2.metric("En échec", int(verdict_row["controls_failed"]))
    c3.metric("Lignes en violation", int(verdict_row["rows_in_violation"]))
    evaluated = pd.to_datetime(verdict_row["evaluated_at"])
    c4.metric("Évalué", evaluated.strftime("%d/%m %H:%M UTC"))

st.markdown("")

# ---------------------------------------------------------------- controls table

st.markdown("#### Les 7 contrôles déterministes")


def _style_status(val: str) -> str:
    color = {"PASS": GREEN, "FAIL": RED, "WARN": GOLD}.get(val, MUTED)
    return f"color: {color}; font-weight: 700;"


styled = data["controls"].rename(
    columns={
        "control_id": "Contrôle",
        "check_name": "Nom",
        "severity": "Sévérité",
        "status": "Statut",
        "failed_rows": "Lignes en échec",
        "executed_at": "Exécuté",
    }
)
st.dataframe(
    styled.style.map(_style_status, subset=["Statut"]),
    use_container_width=True,
    hide_index=True,
)

st.markdown("")

# ---------------------------------------------------------------- volumes + ML

col_vol, col_ml = st.columns(2)

with col_vol:
    st.markdown("#### Volumétrie")
    vol = data["volumes"].rename(columns={"table_name": "Table", "rows": "Lignes"})
    st.dataframe(vol, use_container_width=True, hide_index=True)

with col_ml:
    st.markdown("#### Priorisation par apprentissage")
    if not data["model"].empty:
        m = data["model"].iloc[0]
        st.caption(f"Modèle en production : **{m['model_name']}** (version {m['model_version']})")
    prio = data["priorities"].rename(columns={"priority_band": "Bande", "n": "Transactions"})
    st.bar_chart(prio.set_index("Bande"), color=GOLD)
    st.caption(
        "Ce classement priorise la file des analystes. Il n'influence jamais "
        "le verdict de publication ci-dessus : celui-ci ne dérive que des "
        "7 contrôles déterministes (ADR-0002)."
    )

# ---------------------------------------------------------------- footer

last_event = data["last_run"].iloc[0]["last_event"]
fetched = data["fetched_at"].strftime("%H:%M:%S UTC")
st.markdown(
    f"""
    <div class="muted" style="margin-top:24px;border-top:1px solid {LINE};padding-top:12px;">
      Dernier événement journalisé : {last_event} ·
      Page chargée : {fetched} ·
      Rafraîchissement en cache 30 s
    </div>
    """,
    unsafe_allow_html=True,
)

if auto:
    time.sleep(30)
    st.cache_data.clear()
    st.rerun()
