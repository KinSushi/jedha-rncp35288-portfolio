# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tableau de bord Getaround (Streamlit) - aide à la décision du chef de produit.

Deux pages :
1. **Retards et délai minimal** : indicateurs clés, puis un curseur (seuil) et un choix
   (périmètre) qui recalculent en direct les locations bloquées, la part des locations terminées
   touchée et les cas problématiques évités. Les calculs viennent du module ``retards.py``, le
   même que celui du notebook d'analyse : les chiffres sont identiques sur les deux supports.
2. **Prix conseillé** : formulaire décrivant une voiture, envoyé à l'API ``/predict`` (le
   tableau de bord UTILISE l'API, comme le demande le référentiel). Si l'API ne répond pas, le
   message le dit clairement. Les listes de choix (marques, couleurs, carburants, carrosseries)
   viennent de ``models/categories.json``, versionné avec le dépôt.

Lancement local : ``streamlit run dashboard/app.py`` (depuis la racine du dépôt).
Adresse de l'API : variable d'environnement ``GETAROUND_API_URL`` (par défaut le port local 7860).

CHOIX
-----
- Retenu : le tableau de bord UTILISE l'API pour le prix (POST ``/predict``) au lieu de charger
  le modèle lui-même. Le référentiel demande une application qui consomme l'API, et le tableau de
  bord n'a besoin ni de scikit-learn ni du fichier modèle dans son image Docker.
- Retenu : les indicateurs de retard viennent de retards.py, le module du notebook. Écarté : les
  recalculer ici, ce qui ferait diverger les deux supports.
- Retenu : les listes de choix du formulaire viennent de models/categories.json. Écarté : les
  recopier à la main dans le code, où elles dériveraient des données d'entraînement.
- Retenu : tout le code qui touche à Streamlit s'exécute dans ``main()``, appelé seulement quand le
  fichier est lancé comme script. Écarté : du code au niveau du module, qui s'exécuterait à
  chaque ``import`` et empêcherait de réutiliser les fonctions auxiliaires.
- Retenu : le classeur de l'énoncé n'est pas republié avec le code. ``charger`` lit
  data/get_around_delay_analysis.xlsx s'il existe (démonstration locale, archive remise), sinon
  le télécharge au démarrage depuis l'adresse officielle de l'énoncé. Écarté : copier le
  classeur dans l'image Docker, ce qui republierait des données qui ne sont pas les miennes.
- Écarté : réutiliser le thème Streamlit pour les graphiques (``theme=None`` laisse Plotly maître
  des couleurs, choisies lisibles sur fond clair comme sombre).
- Accessibilité (contrôle du 19/10/2026, rapport dans ACCESSIBILITE.md). Retenu : des couleurs de
  courbes à 3:1 au moins sur les deux fonds, doublées d'un trait, d'un symbole ou d'un motif (la
  couleur ne porte jamais seule l'information) ; un texte alternatif (``alt``) pour chaque
  graphique, et les valeurs du diagramme des retards dans un tableau ; la langue de la page
  déclarée en français ; un thème clair plus contrasté dans dashboard/.streamlit/config.toml.
  Écarté : réécrire l'apparence des composants de Streamlit par une feuille de style injectée,
  fragile d'une version à l'autre ; le thème officiel suffit pour les textes.

OÙ
---
- ``dashboard/app.py``. Lancé par ``streamlit run`` (dashboard/Dockerfile, port 7860).
- Dépendances : Streamlit, pandas, Plotly, requests, openpyxl (requirements-dashboard.txt),
  retards.py (copié dans l'image), data/get_around_delay_analysis.xlsx (s'il existe, sinon
  l'adresse officielle de l'énoncé), models/categories.json, dashboard/.streamlit/config.toml
  (thème clair, lu par Streamlit à côté du script), et l'API de api/main.py à l'adresse
  ``GETAROUND_API_URL``.

COMMENT
-------
1. ``main`` configure la page, affiche la barre latérale et appelle ``page_retards`` ou
   ``page_prix`` selon le choix de l'utilisateur.
2. ``page_retards`` charge le classeur une fois (``charger``), affiche quatre indicateurs, le
   simulateur (seuil et périmètre → locations bloquées, part des terminées, cas évités), la
   courbe coût/bénéfice et la répartition des retards par tranche (``retards.tranche_de_retard``).
3. ``page_prix`` affiche le formulaire, envoie la voiture à l'API et affiche le prix, ou le
   message d'erreur de l'API (jamais une trace Python).

POURQUOI
--------
- Le tableau de bord doit consommer l'API et afficher l'arbitrage seuil × périmètre : énoncé du
  projet ; synthèse dans README.md.
- Erreur absolue moyenne annoncée (10,5 €) : colonne ``mae_test`` du gradient boosting dans
  models/comparaison_modeles.csv.
- Bornes du formulaire (kilométrage 0 à 1 500 000, puissance 1 à 500 ch) : celles de l'API,
  api/main.py (constante ``BORNES``).
- Accessibilité : le référentiel RNCP35288 évalue en C5.3 l'« accessibilité de l'interface web
  incluant l'utilisation de l'interface de programmation applicative ». Seuils de contraste et
  critères cités : WCAG 2.1 du W3C, niveau AA (1.1.1, 1.4.1, 1.4.3, 1.4.11, 3.1.1). Mesures avant
  et après : ACCESSIBILITE.md.

JOURNALISATION
--------------
Aucun ``logging`` : l'application affiche à l'écran. Les échecs de l'API sont rendus à
l'utilisateur par ``st.error`` (réseau ou code HTTP et message de l'API). Streamlit journalise de
son côté les exceptions non gérées.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))  # pour importer retards.py, à la racine du dépôt
import retards  # noqa: E402

URL_API = os.getenv("GETAROUND_API_URL", "http://localhost:7860").rstrip("/")
SEUILS = list(range(0, 721, 30))
# Décision D3 (a) : le classeur de l'énoncé n'est pas republié. Fichier local s'il existe,
# sinon adresse officielle de l'énoncé.
FICHIER_DONNEES = RACINE / "data" / "get_around_delay_analysis.xlsx"
URL_DONNEES = (
    "https://full-stack-assets.s3.eu-west-3.amazonaws.com/Deployment/get_around_delay_analysis.xlsx"
)

# Couleurs des courbes et des barres. Contrôle d'accessibilité du 19/10/2026 : les anciennes
# (#ff7f0e et #2ca2ff) n'avaient qu'un contraste de 2,5 et 2,7 contre le fond blanc, sous le
# minimum de 3 pour un élément graphique (WCAG 2.1, critère 1.4.11). Celles-ci dépassent 3 sur le
# fond clair (#ffffff) comme sur le fond sombre de Streamlit (#0e1117) : orange 3,76 et 5,03,
# bleu 5,75 et 3,29. La couleur ne porte jamais
# seule l'information (critère 1.4.1) : chaque périmètre a aussi son trait et son symbole, chaque
# type de check-in son motif de barre. Les clés sont les libellés de ``retards.PERIMETRES`` : le
# style suit le périmètre, quel que soit le graphique.
COULEURS_PERIMETRES = {
    retards.PERIMETRES["toutes"]: "#d95f02",
    retards.PERIMETRES["connect"]: "#1565c0",
}
TRAITS_PERIMETRES = {retards.PERIMETRES["toutes"]: "solid", retards.PERIMETRES["connect"]: "dash"}
SYMBOLES_PERIMETRES = {
    retards.PERIMETRES["toutes"]: "circle",
    retards.PERIMETRES["connect"]: "square",
}
COULEURS_CHECKIN = {"mobile": "#d95f02", "connect": "#1565c0"}
MOTIFS_CHECKIN = {"mobile": "", "connect": "/"}
PAGE_RETARDS = "Retards et délai minimal"
PAGE_PRIX = "Prix conseillé"
# Streamlit écrit lang="en" sur la page ; le contenu est en français. Sans correction, un lecteur
# d'écran lirait le texte avec une prononciation anglaise (WCAG 2.1, critère 3.1.1).
LANGUE_DE_LA_PAGE = "<script>document.documentElement.lang = 'fr';</script>"


@st.cache_data
def charger() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Données des locations préparées et tableau de l'arbitrage (calculés une seule fois).

    Le classeur est lu dans ``data/`` s'il existe, sinon téléchargé depuis l'adresse officielle.
    """
    source = FICHIER_DONNEES if FICHIER_DONNEES.exists() else URL_DONNEES
    brut = pd.read_excel(source, sheet_name="rentals_data")
    preparees = retards.preparer(brut)
    return preparees, retards.tableau_seuils(preparees, SEUILS)


@st.cache_data
def charger_categories() -> dict[str, list[str]]:
    """Valeurs connues du modèle pour chaque catégorie (marque, carburant, couleur, carrosserie).

    Le fichier est versionné avec le dépôt (``train/train.py`` ne l'écrit pas) et correspond aux
    valeurs du fichier de données d'entraînement. Le formulaire propose donc ce que le modèle a
    vu, sans liste recopiée à la main dans le code.
    """
    contenu = json.loads((RACINE / "models" / "categories.json").read_text(encoding="utf-8"))
    return {cle: valeurs for cle, valeurs in contenu.items() if not cle.startswith("_")}


def nombre(n: int) -> str:
    """Entier avec espace comme séparateur de milliers (21 310), à la française."""
    return f"{n:,}".replace(",", " ")


def virgule(texte: str) -> str:
    """Nombre décimal écrit à la française : virgule à la place du point (57,5 %)."""
    return texte.replace(".", ",")


def choix_initial(valeurs: list[str], souhaite: str) -> int:
    """Position du choix proposé par défaut dans une liste, 0 si cette valeur n'existe pas.

    Les listes viennent du fichier de catégories : si les données changent, le formulaire doit
    continuer à s'afficher plutôt que planter sur une valeur qui n'y serait plus.
    """
    return valeurs.index(souhaite) if souhaite in valeurs else 0


def gabarit_graphique() -> str:
    """Gabarit Plotly assorti au thème de la page (clair ou sombre).

    ``theme=None`` laisse Plotly maîtriser les couleurs des courbes (le thème Streamlit les
    écraserait) : il faut alors choisir soi-même un fond et un texte lisibles, clairs ou sombres
    selon le thème actif de la page.
    """
    try:
        sombre = st.context.theme.type == "dark"
    except AttributeError:  # version de Streamlit sans st.context.theme : thème clair par défaut
        return "plotly_white"
    return "plotly_dark" if sombre else "plotly_white"


def page_retards() -> None:
    df, arbitrage = charger()
    terminees = df[df["state"] == "ended"]
    st.title("Quel délai minimal imposer entre deux locations ?")
    st.caption(
        f"Données Getaround ({nombre(len(df))} locations). Une location est « bloquée » si son "
        "écart avec la location précédente de la même voiture est inférieur au seuil."
    )

    rendues = terminees[terminees["delay_at_checkout_in_minutes"].notna()]
    connus = df[df["retard_precedent"].notna()]
    problemes = connus[connus["cas_problematique"]]
    annulation = connus.groupby("cas_problematique")["state"].apply(
        lambda s: (s == "canceled").mean() * 100
    )
    colonnes = st.columns(4)
    colonnes[0].metric(
        "Locations rendues en retard",
        virgule(f"{(rendues['delay_at_checkout_in_minutes'] > 0).mean():.1%}"),
    )
    colonnes[1].metric(
        "Locations à moins de 12 h d'une autre",
        virgule(f"{df['previous_ended_rental_id'].notna().mean():.1%}"),
    )
    colonnes[2].metric("Cas où le conducteur suivant attend", f"{len(problemes)}")
    colonnes[3].metric(
        "Annulations quand il attend",
        virgule(f"{annulation.get(True, 0):.1f} %"),
        delta=virgule(f"{annulation.get(True, 0) - annulation.get(False, 0):+.1f} pts"),
        delta_color="inverse",
        help=(
            "Écart observé sur peu de cas, association et non causalité : à lire avec prudence. "
            "Le détail est dans le notebook d'analyse."
        ),
    )

    st.subheader("Simuler la fonctionnalité")
    gauche, droite = st.columns([1, 2])
    with gauche:
        seuil = st.select_slider("Délai minimal (minutes)", options=SEUILS, value=60)
        perimetre = st.radio(
            "Périmètre",
            list(retards.PERIMETRES),
            format_func=retards.PERIMETRES.get,
        )
        ligne = arbitrage[
            (arbitrage["perimetre"] == perimetre) & (arbitrage["seuil_min"] == seuil)
        ].iloc[0]
        st.metric(
            "Locations qui ne pourraient plus être réservées",
            f"{ligne['locations_bloquees']}",
            virgule(f"{ligne['part_locations_pct']:.2f} % de toutes les locations"),
            delta_color="off",
        )
        # Une seule métrique pour l'indicateur de revenu (l'ancienne version en affichait deux,
        # identiques, dont une au libellé tronqué). Libellé court ; l'explication est dans
        # l'infobulle et dans la légende juste dessous.
        st.metric(
            "Part des locations terminées bloquées",
            virgule(f"{ligne['part_terminees_pct']:.2f} %"),
            help=(
                "Indicateur de substitution du revenu : les données ne contiennent aucun prix "
                "par location, le revenu perdu ne peut donc pas être calculé."
            ),
        )
        st.caption(
            f"{ligne['locations_terminees_bloquees']} locations terminées bloquées sur "
            f"{nombre(len(terminees))} - seules les locations terminées rapportent un revenu. "
            "Indicateur de substitution (aucun prix par location dans les données), ni "
            "majorant ni minorant du revenu perdu."
        )
        st.metric(
            "Cas problématiques évités",
            f"{ligne['cas_resolus']}",
            virgule(f"{ligne['part_cas_resolus_pct']:.1f} % des cas"),
            delta_color="off",
        )
    with droite:
        donnees = arbitrage.assign(perimetre=arbitrage["perimetre"].map(retards.PERIMETRES))
        courbes = donnees.melt(
            id_vars=["perimetre", "seuil_min"],
            value_vars=["part_locations_pct", "part_cas_resolus_pct"],
            var_name="indicateur",
            value_name="pourcentage",
        )
        courbes["indicateur"] = courbes["indicateur"].map(
            {
                "part_locations_pct": "coût : % de locations bloquées",
                "part_cas_resolus_pct": "bénéfice : % de cas évités",
            }
        )
        figure = px.line(
            courbes,
            x="seuil_min",
            y="pourcentage",
            color="perimetre",
            color_discrete_map=COULEURS_PERIMETRES,
            line_dash="perimetre",
            line_dash_map=TRAITS_PERIMETRES,
            symbol="perimetre",
            symbol_map=SYMBOLES_PERIMETRES,
            facet_col="indicateur",
            # Pas de « markers=True » : avec un symbole par périmètre (``symbol``), Plotly trace
            # déjà les points (mode « lines+markers », plotly 7.1.0, plotly/express/_core.py,
            # l. 2376) ; l'argument ne changeait rien.
            labels={
                "seuil_min": "délai minimal (min)",
                "pourcentage": "%",
                "perimetre": "périmètre",
            },
        )
        figure.update_yaxes(matches=None, showticklabels=True)
        figure.update_traces(line={"width": 3}, marker={"size": 7})
        # Titres des deux panneaux : on retire le préfixe technique « indicateur= » (et lui seul,
        # un libellé qui contiendrait un « = » resterait entier).
        figure.for_each_annotation(lambda a: a.update(text=a.text.removeprefix("indicateur=")))
        figure.add_vline(x=seuil, line_dash="dash", line_color="gray")
        # theme=None : avec le thème Streamlit par défaut, les deux périmètres reprenaient deux
        # teintes de bleu proches et les couleurs choisies ci-dessus étaient écrasées.
        figure.update_layout(
            template=gabarit_graphique(),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
        )
        # Texte alternatif (``alt``) : ce que lit un lecteur d'écran à la place du graphique. Les
        # valeurs exactes sont dans le tableau complet de l'arbitrage, en bas de la page.
        st.plotly_chart(
            figure,
            theme=None,
            alt=(
                "Deux graphiques en courbes selon le délai minimal, de 0 à 720 minutes : à "
                "gauche le pourcentage de locations bloquées (coût), à droite le pourcentage de "
                "cas problématiques évités (bénéfice), pour toutes les voitures (trait plein) et "
                "pour les seules voitures Connect (tirets). Trait vertical au seuil choisi, "
                f"{seuil} minutes. Valeurs exactes : tableau complet de l'arbitrage, plus bas."
            ),
        )

    st.subheader("Fréquence des retards au check-out")
    tranches = retards.tranche_de_retard(rendues["delay_at_checkout_in_minutes"])
    repartition = pd.crosstab(tranches, rendues["checkin_type"], normalize="columns") * 100
    repartition = repartition.reset_index().melt(
        id_vars="delay_at_checkout_in_minutes", var_name="check-in", value_name="part (%)"
    )
    barres = px.bar(
        repartition,
        x="delay_at_checkout_in_minutes",
        y="part (%)",
        color="check-in",
        color_discrete_map=COULEURS_CHECKIN,
        pattern_shape="check-in",
        pattern_shape_map=MOTIFS_CHECKIN,
        barmode="group",
        labels={"delay_at_checkout_in_minutes": "retard"},
    )
    barres.update_layout(
        template=gabarit_graphique(),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(
        barres,
        theme=None,
        alt=(
            "Diagramme en barres : part des locations rendues dans chaque tranche de retard au "
            "check-out, de « à l'heure ou en avance » à « plus de 24 h », pour le check-in mobile "
            "(barres pleines) et Connect (barres hachurées). Valeurs exactes : tableau juste "
            "en dessous."
        ),
    )
    # Alternative textuelle complète du diagramme : les mêmes valeurs, en tableau.
    with st.expander("Valeurs du diagramme des retards (tableau)"):
        st.dataframe(
            repartition.pivot(
                index="delay_at_checkout_in_minutes", columns="check-in", values="part (%)"
            )
            .round(1)
            .rename_axis(index="retard", columns=None)
            .reset_index(),
            hide_index=True,
        )
    with st.expander("Tableau complet de l'arbitrage"):
        st.dataframe(arbitrage, hide_index=True)


def page_prix() -> None:
    st.title("Prix de location conseillé")
    st.caption(f"Le formulaire interroge l'API de prédiction : {URL_API}/predict")
    categories = charger_categories()
    marques = categories["model_key"]
    with st.form("voiture"):
        a, b, c = st.columns(3)
        # Renault, marque très présente dans les données, comme choix initial si elle existe.
        marque = a.selectbox("Marque", marques, index=choix_initial(marques, "Renault"))
        kilometrage = a.number_input("Kilométrage", 0, 1_500_000, 120_000, step=5_000)
        puissance = a.number_input("Puissance (ch)", 1, 500, 120, step=5)
        carburant = b.selectbox(
            "Carburant", categories["fuel"], index=choix_initial(categories["fuel"], "diesel")
        )
        couleur = b.selectbox(
            "Couleur",
            categories["paint_color"],
            index=choix_initial(categories["paint_color"], "black"),
        )
        carrosserie = b.selectbox(
            "Carrosserie",
            categories["car_type"],
            index=choix_initial(categories["car_type"], "sedan"),
        )
        options = {
            "private_parking_available": c.checkbox("Parking privé", True),
            "has_gps": c.checkbox("GPS", True),
            "has_air_conditioning": c.checkbox("Climatisation", False),
            "automatic_car": c.checkbox("Boîte automatique", False),
            "has_getaround_connect": c.checkbox("Getaround Connect", True),
            "has_speed_regulator": c.checkbox("Régulateur de vitesse", True),
            "winter_tires": c.checkbox("Pneus hiver", True),
        }
        envoye = st.form_submit_button("Estimer le prix")
    if envoye:
        # Voiture décrite par un objet nommé (forme acceptée par l'API) : les noms de colonnes
        # restent lisibles dans la requête, et leur ordre n'a plus à être connu d'ici.
        voiture: dict[str, Any] = {
            "model_key": marque,
            "mileage": kilometrage,
            "engine_power": puissance,
            "fuel": carburant,
            "paint_color": couleur,
            "car_type": carrosserie,
            **options,
        }
        try:
            reponse = requests.post(f"{URL_API}/predict", json={"input": [voiture]}, timeout=20)
        except requests.RequestException as erreur:
            st.error(f"L'API ne répond pas ({erreur}). Vérifiez GETAROUND_API_URL.")
            return
        if not reponse.ok:
            # L'API répond toujours au format {"error": "..."} : on affiche son message tel quel.
            try:
                message = reponse.json()["error"]
            except (ValueError, KeyError, TypeError):
                message = reponse.text[:200]
            st.error(f"L'API a refusé la demande (code {reponse.status_code}) : {message}")
            return
        corps = reponse.json()
        st.success(f"Prix conseillé : **{corps['prediction'][0]:.0f} € par jour**")
        # Source du chiffre : MAE de test du champion, models/comparaison_modeles.csv.
        st.caption(
            "Erreur absolue moyenne du modèle sur des voitures jamais vues : environ 10,5 €."
        )


def main() -> None:
    st.set_page_config(page_title="Getaround - aide à la décision", page_icon="🚗", layout="wide")
    st.html(LANGUE_DE_LA_PAGE, unsafe_allow_javascript=True)
    page = st.sidebar.radio("Page", [PAGE_RETARDS, PAGE_PRIX])
    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Projet Getaround - certification RNCP35288, bloc 5.\n\n"
        "Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC"
    )
    if page == PAGE_RETARDS:
        page_retards()
    else:
        page_prix()


# Streamlit exécute le fichier comme un script (__name__ vaut alors « __main__ ») ; un simple
# « import dashboard.app » ne lance donc aucune page.
if __name__ == "__main__":
    main()
