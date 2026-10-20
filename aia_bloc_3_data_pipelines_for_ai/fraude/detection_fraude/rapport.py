# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Rapport du matin : paiements et fraudes de la veille (traitement par lot) — critères 1.1, 6.1.

Le métier veut, chaque matin, voir les paiements et les fraudes de la veille. Ce besoin n'a pas la
même fraîcheur que l'alerte : une journée complète est consolidée une fois, à 07:00, par un
traitement **par lot** (Airflow). Toute la logique est ici, en fonctions simples, testables sans
Airflow ; le DAG ne fait qu'enchaîner ces fonctions.

Étapes (une fonction chacune, enchaînées par le DAG) :

1. ``verifier_donnees_du_jour`` : refuse de produire un rapport vide, ce qui trahirait une
   source muette plutôt qu'une journée sans paiement ;
2. ``calculer_rapport``         : agrégats SQL sur la couche argent + rejets de la journée ;
3. ``ecrire_rapport``           : couche or (``or_rapport_*``), fichiers HTML et CSV ;
4. ``notifier_rapport``         : résumé aux canaux de notification ;
5. ``verifier_delai_rapport``   : contrôle du SLA (livraison dans l'heure qui suit 07:00).

« La veille » s'entend en jours UTC : les horodatages de la source sont en UTC (hypothèse
documentée dans le livrable cité plus bas).

CHOIX
-----
- Retenu : un lot quotidien orchestré, plutôt qu'un flux, pour le rapport : aucune exigence de
  fraîcheur à la seconde, mais une journée complète et consolidée, reproductible et rejouable.
- Retenu : refuser de produire un rapport vide (``DonneesAbsentes``) : une journée sans aucune
  transaction trahit une source muette.
- Retenu : un rapport idempotent : relancé, il remplace le résultat précédent (les lignes de la
  catégorie sont supprimées puis réécrites, le jour est remplacé par ``ON CONFLICT DO UPDATE``).
- Retenu : une page HTML autonome, sans ressource externe, où toute valeur est échappée ; un CSV
  avec séparateur point-virgule et BOM pour un Excel français.
- Retenu : la veille en jours UTC, car les horodatages de la source sont en UTC ; l'heure locale
  n'est pas étudiée ailleurs dans le dépôt (source : inconnue).

OÙ
---
- Place : lot du matin, après le flux ; lit la couche argent, écrit la couche or et des fichiers.
- Appelants : tâches Airflow du DAG ``fraude_rapport_du_matin``, retraiter
  (detection_fraude/retraitement.py:94-198) (régénération), demo
  (detection_fraude/local.py:188-299) et ``main``.
- Dépendances : SQLAlchemy, ``entrepot``, ``notification``, ``configuration`` et ``journal``.

COMMENT
-------
1. ``calculer_rapport`` agrège les paiements, fraudes et montants du jour et de la veille, les
   rejets de la journée, la répartition par catégorie, la liste des fraudes et les versions de
   modèle utilisées.
2. ``rendre_html`` produit la page ; ``ecrire_csv`` la liste des fraudes ; ``ecrire_rapport``
   écrit les fichiers puis, dans une transaction, les tables or.
3. ``verifier_delai_rapport`` rend (respecté, retard en minutes) pour un SLA de ``sla_minutes``.
Tests qui le prouvent : test_agregats_exacts (tests/test_rapport.py:85-92),
test_le_rapport_est_idempotent (tests/test_rapport.py:125-129), test_le_html_echappe_les_valeurs
(tests/test_rapport.py:132-138), test_source_muette_leve_donnees_absentes
(tests/test_rapport.py:153-155), test_sla_du_rapport (tests/test_rapport.py:169-174) ,
test_calculer_rapport_donne_tous_les_champs_exacts_et_laisse_la_base_inchangee
(tests/test_rapport_garanties.py:367-441) et
test_une_ligne_invalide_annule_toute_la_reecriture_et_garde_l_ancien_rapport
(tests/test_rapport_garanties.py:856-872).

POURQUOI
--------
- Choix lot pour le rapport du matin, idempotent : livrables/aia3/01_architecture.md:101-145.
- DAG, SLA de 60 minutes après 07:00 : livrables/aia3/02_pipeline_et_qualite.md:145-164.
- Hypothèse « la veille en jours UTC » : livrables/aia3/02_pipeline_et_qualite.md:109-117 et
  livrables/aia3/07_couverture_et_limites.md:53-66 (hypothèses et points à confirmer).
- Échappement HTML : livrables/aia3/03_securite_conformite.md:158-168.
- Indicateurs 1.1 et 6.1 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.rapport``) : ``donnees_absentes`` (ERROR : ``jour``,
``transactions``), ``sla_depasse`` (WARNING : ``sla``, ``retard_min``) et ``rapport_produit`` (INFO
: ``jour``, ``paiements``, ``fraudes``, ``html``, ``csv``). En ligne de commande, l'absence de
données écrit un message sur la sortie d'erreur et retourne le code 3.
"""

from __future__ import annotations

import argparse
import csv
import html
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from detection_fraude import VERSION_REGLES
from detection_fraude.configuration import Configuration, charger_configuration
from detection_fraude.entrepot import Entrepot, construire_insertion, maintenant_iso
from detection_fraude.journal import (
    avertissement,
    configurer_journal,
    erreur,
    info,
    obtenir_journal,
)
from detection_fraude.notification import Notificateur, Notification, construire_canaux

_journal = obtenir_journal("rapport")

COLONNES_RAPPORT_JOUR = (
    "jour",
    "nb_paiements",
    "nb_fraudes",
    "montant_total",
    "montant_fraudes",
    "taux_fraude_nb",
    "taux_fraude_montant",
    "nb_rejets",
    "nb_etiquetees",
    "nb_vrais_positifs",
    "nb_fraudes_reelles",
)
PIED_DE_PAGE = "Copyright © 2026 · voir le fichier LICENSE du dépôt"


class DonneesAbsentes(Exception):
    """Aucune transaction pour le jour demandé : la source est probablement en panne."""


def jour_de_la_veille(maintenant: datetime) -> str:
    """Date UTC (AAAA-MM-JJ) du jour précédent l'instant donné."""
    instant = maintenant.astimezone(UTC) if maintenant.tzinfo else maintenant.replace(tzinfo=UTC)
    return (instant.date() - timedelta(days=1)).isoformat()


def _jour_valide(jour: str) -> str:
    return date.fromisoformat(jour).isoformat()  # lève ValueError si le format est mauvais


def verifier_donnees_du_jour(entrepot: Entrepot, jour: str, minimum: int = 1) -> int:
    """Nombre de transactions du jour ; lève ``DonneesAbsentes`` sous ``minimum``."""
    jour = _jour_valide(jour)
    nombre = int(
        entrepot.scalaire(
            "SELECT COUNT(*) FROM argent_transactions WHERE date_transaction = :jour", jour=jour
        )
    )
    if nombre < minimum:
        erreur(_journal, "donnees_absentes", jour=jour, transactions=nombre)
        raise DonneesAbsentes(f"{nombre} transaction(s) le {jour} : source probablement muette.")
    return nombre


def _agregats(entrepot: Entrepot, jour: str) -> dict[str, Any]:
    ligne = entrepot.requete(
        "SELECT COUNT(*) AS nb, COALESCE(SUM(montant), 0) AS total, "
        "COALESCE(SUM(est_fraude), 0) AS nb_fraudes, "
        "COALESCE(SUM(CASE WHEN est_fraude = 1 THEN montant ELSE 0 END), 0) AS montant_fraudes, "
        "COALESCE(SUM(CASE WHEN etiquette_source IS NOT NULL THEN 1 ELSE 0 END), 0) "
        "AS nb_etiquetees, "
        "COALESCE(SUM(CASE WHEN etiquette_source = 1 AND est_fraude = 1 THEN 1 ELSE 0 END), 0) "
        "AS vp, "
        "COALESCE(SUM(CASE WHEN etiquette_source = 1 THEN 1 ELSE 0 END), 0) AS reelles "
        "FROM argent_transactions WHERE date_transaction = :jour",
        jour=jour,
    )[0]
    nb = int(ligne["nb"])
    return {
        "nb_paiements": nb,
        "montant_total": round(float(ligne["total"]), 2),
        "nb_fraudes": int(ligne["nb_fraudes"]),
        "montant_fraudes": round(float(ligne["montant_fraudes"]), 2),
        "nb_etiquetees": int(ligne["nb_etiquetees"]),
        "nb_vrais_positifs": int(ligne["vp"]),
        "nb_fraudes_reelles": int(ligne["reelles"]),
    }


def calculer_rapport(entrepot: Entrepot, jour: str) -> dict[str, Any]:
    """Indicateurs du jour, par catégorie, liste des fraudes et comparaison avec la veille."""
    jour = _jour_valide(jour)
    principaux = _agregats(entrepot, jour)
    precedent = _agregats(entrepot, (date.fromisoformat(jour) - timedelta(days=1)).isoformat())
    nb, total = principaux["nb_paiements"], principaux["montant_total"]
    principaux["taux_fraude_nb"] = principaux["nb_fraudes"] / nb if nb else 0.0
    principaux["taux_fraude_montant"] = principaux["montant_fraudes"] / total if total else 0.0
    nb_rejets = int(
        entrepot.scalaire(
            "SELECT COUNT(*) FROM meta_rejets WHERE date_evenement = :jour", jour=jour
        )
    )
    categories = entrepot.requete(
        "SELECT categorie, COUNT(*) AS nb_paiements, SUM(est_fraude) AS nb_fraudes, "
        "SUM(montant) AS montant_total, "
        "SUM(CASE WHEN est_fraude = 1 THEN montant ELSE 0 END) AS montant_fraudes "
        "FROM argent_transactions WHERE date_transaction = :jour GROUP BY categorie "
        "ORDER BY nb_fraudes DESC, montant_fraudes DESC, categorie",
        jour=jour,
    )
    fraudes = entrepot.requete(
        "SELECT horodatage_utc, trans_num, carte_fin, commercant, categorie, etat, montant, "
        "score, etiquette_source FROM argent_transactions "
        "WHERE date_transaction = :jour AND est_fraude = 1 "
        "ORDER BY horodatage_s, trans_num",
        jour=jour,
    )
    modeles = entrepot.requete(
        "SELECT DISTINCT version_modele FROM argent_transactions WHERE date_transaction = :jour "
        "ORDER BY version_modele",
        jour=jour,
    )
    return {
        "jour": jour,
        **principaux,
        "nb_rejets": nb_rejets,
        "veille": precedent,
        "categories": [
            {
                **c,
                "montant_total": round(float(c["montant_total"]), 2),
                "montant_fraudes": round(float(c["montant_fraudes"]), 2),
                "nb_fraudes": int(c["nb_fraudes"]),
            }
            for c in categories
        ],
        "fraudes": fraudes,
        "versions_modele": [m["version_modele"] for m in modeles],
    }


# --------------------------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------------------------
def _variation(valeur: float, reference: float) -> str:
    if not reference:
        return "n/d"
    return f"{(valeur - reference) / reference * 100:+.1f} %"


def rendre_html(rapport: dict[str, Any]) -> str:
    """Page HTML autonome (aucune ressource externe). Toute valeur est échappée."""
    e = html.escape
    veille = rapport["veille"]
    cartes = [
        (
            "Paiements",
            f"{rapport['nb_paiements']:,}".replace(",", " "),
            f"veille : {_variation(rapport['nb_paiements'], veille['nb_paiements'])}",
        ),
        (
            "Montant total",
            f"{rapport['montant_total']:,.2f}".replace(",", " "),
            f"veille : {_variation(rapport['montant_total'], veille['montant_total'])}",
        ),
        (
            "Fraudes détectées",
            str(rapport["nb_fraudes"]),
            f"veille : {_variation(rapport['nb_fraudes'], veille['nb_fraudes'])}",
        ),
        (
            "Montant des fraudes",
            f"{rapport['montant_fraudes']:,.2f}".replace(",", " "),
            f"{rapport['taux_fraude_montant'] * 100:.3f} % du montant",
        ),
        ("Taux de fraude", f"{rapport['taux_fraude_nb'] * 100:.3f} %", "en nombre de paiements"),
        ("Lignes rejetées", str(rapport["nb_rejets"]), "file des rejets (DLQ)"),
    ]
    cartes_html = "".join(
        f'<div class="carte"><div class="titre">{e(t)}</div><div class="valeur">{e(v)}</div>'
        f'<div class="note">{e(n)}</div></div>'
        for t, v, n in cartes
    )
    lignes_categories = "".join(
        f"<tr><td>{e(c['categorie'])}</td><td class='n'>{c['nb_paiements']}</td>"
        f"<td class='n'>{c['nb_fraudes']}</td><td class='n'>{c['montant_total']:.2f}</td>"
        f"<td class='n'>{c['montant_fraudes']:.2f}</td></tr>"
        for c in rapport["categories"]
    )
    lignes_fraudes = (
        "".join(
            f"<tr><td>{e(str(f['horodatage_utc']))}</td><td class='m'>{e(f['trans_num'])}</td>"
            f"<td>****{e(f['carte_fin'])}</td><td>{e(f['commercant'])}</td>"
            f"<td>{e(f['categorie'])}</td><td class='n'>{float(f['montant']):.2f}</td>"
            f"<td class='n'>{float(f['score']):.3f}</td></tr>"
            for f in rapport["fraudes"]
        )
        or "<tr><td colspan='7'>Aucune fraude détectée ce jour-là.</td></tr>"
    )
    qualite = ""
    if rapport["nb_etiquetees"]:
        vp, reelles, detectees = (
            rapport["nb_vrais_positifs"],
            rapport["nb_fraudes_reelles"],
            rapport["nb_fraudes"],
        )
        precision = f"{vp / detectees * 100:.1f} %" if detectees else "n/d"
        rappel = f"{vp / reelles * 100:.1f} %" if reelles else "n/d"
        qualite = (
            "<h2>Contrôle du modèle</h2><p>La source fournit une étiquette pour "
            f"{rapport['nb_etiquetees']} transaction(s) de la journée : {reelles} fraude(s) "
            f"réelle(s), {vp} détectée(s) par le modèle. Précision du jour : {e(precision)} ; "
            f"rappel du jour : {e(rappel)}.</p>"
        )
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Rapport du matin — {e(rapport["jour"])}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:2rem auto;max-width:1000px;color:#1b2430}}
h1{{font-size:1.6rem}} h2{{margin-top:2rem;font-size:1.2rem}}
.cartes{{display:flex;flex-wrap:wrap;gap:12px}}
.carte{{flex:1 1 150px;border:1px solid #d5dbe3;border-radius:8px;padding:12px}}
.titre{{font-size:.8rem;color:#5b6675}} .valeur{{font-size:1.5rem;font-weight:600}}
.note{{font-size:.75rem;color:#5b6675}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}
th,td{{border-bottom:1px solid #e3e8ee;padding:6px 8px;text-align:left}}
th{{background:#f3f5f8}} td.n{{text-align:right}} td.m{{font-family:Consolas,monospace}}
footer{{margin-top:3rem;font-size:.75rem;color:#5b6675;border-top:1px solid #e3e8ee;
padding-top:8px}}
</style></head><body>
<h1>Rapport du matin — paiements et fraudes du {e(rapport["jour"])}</h1>
<p>Jour UTC. Modèle(s) utilisé(s) : {e(", ".join(rapport["versions_modele"]) or "n/d")}.</p>
<div class="cartes">{cartes_html}</div>
<h2>Par catégorie</h2>
<table><tr><th>Catégorie</th><th>Paiements</th><th>Fraudes</th><th>Montant</th>
<th>Montant fraudes</th></tr>
{lignes_categories}</table>
<h2>Fraudes détectées</h2>
<table><tr><th>Horodatage (UTC)</th><th>Transaction</th><th>Carte</th><th>Commerçant</th>
<th>Catégorie</th><th>Montant</th><th>Score</th></tr>{lignes_fraudes}</table>
{qualite}
<footer>Données masquées : aucun nom, adresse ni numéro de carte complet.
{e(PIED_DE_PAGE)}</footer>
</body></html>
"""


def ecrire_csv(rapport: dict[str, Any], chemin: Path) -> None:
    """Liste des fraudes détectées, séparateur point-virgule et BOM pour un Excel français."""
    with chemin.open("w", encoding="utf-8-sig", newline="") as flux:
        auteur = csv.writer(flux, delimiter=";")
        auteur.writerow(
            [
                "horodatage_utc",
                "trans_num",
                "carte_fin",
                "commercant",
                "categorie",
                "etat",
                "montant",
                "score",
            ]
        )
        for f in rapport["fraudes"]:
            auteur.writerow(
                [
                    f["horodatage_utc"],
                    f["trans_num"],
                    f["carte_fin"],
                    f["commercant"],
                    f["categorie"],
                    f["etat"],
                    f"{float(f['montant']):.2f}",
                    f"{float(f['score']):.4f}",
                ]
            )


def ecrire_rapport(
    config: Configuration, entrepot: Entrepot, rapport: dict[str, Any]
) -> dict[str, str]:
    """Écrit les fichiers puis la couche or. Réexécutable : le 2e passage remplace le 1er."""
    jour = rapport["jour"]
    dossier = config.rapport.dossier
    dossier.mkdir(parents=True, exist_ok=True)
    chemin_html = dossier / f"rapport_{jour}.html"
    chemin_csv = dossier / f"fraudes_{jour}.csv"
    chemin_html.write_text(rendre_html(rapport), encoding="utf-8")
    ecrire_csv(rapport, chemin_csv)
    with entrepot.transaction() as conn:
        conn.execute(text("DELETE FROM or_rapport_categorie WHERE jour = :jour"), {"jour": jour})
        for c in rapport["categories"]:
            conn.execute(
                text(
                    "INSERT INTO or_rapport_categorie (jour, categorie, nb_paiements, nb_fraudes, "
                    "montant_total, montant_fraudes) VALUES (:jour, :categorie, :nb_paiements, "
                    ":nb_fraudes, :montant_total, :montant_fraudes)"
                ),
                {"jour": jour, **c},
            )
        valeurs = {k: rapport[k] for k in COLONNES_RAPPORT_JOUR if k in rapport}
        valeurs.update(
            genere_le=maintenant_iso(),
            version_regles=VERSION_REGLES,
            chemin_html=str(chemin_html),
            chemin_csv=str(chemin_csv),
        )
        sql = construire_insertion(
            "or_rapport_quotidien", list(valeurs), "jour", [c for c in valeurs if c != "jour"]
        )
        conn.execute(text(sql), valeurs)
    return {"html": str(chemin_html), "csv": str(chemin_csv)}


def notifier_rapport(
    notificateur: Notificateur, rapport: dict[str, Any], chemins: dict[str, str]
) -> dict[str, bool]:
    corps = (
        f"Rapport du matin pour le {rapport['jour']} (jour UTC).\n"
        f"- Paiements : {rapport['nb_paiements']} pour {rapport['montant_total']:.2f}\n"
        f"- Fraudes détectées : {rapport['nb_fraudes']} pour {rapport['montant_fraudes']:.2f} "
        f"({rapport['taux_fraude_nb'] * 100:.3f} % des paiements)\n"
        f"- Lignes rejetées (DLQ) : {rapport['nb_rejets']}\n"
        f"- Rapport HTML : {chemins['html']}\n- Liste CSV : {chemins['csv']}"
    )
    sujet = (
        f"[Rapport du matin] {rapport['jour']} : {rapport['nb_fraudes']} fraude(s) "
        f"sur {rapport['nb_paiements']} paiements"
    )
    return notificateur.notifier(
        Notification(
            "rapport",
            sujet,
            corps,
            {
                "jour": rapport["jour"],
                "nb_paiements": rapport["nb_paiements"],
                "nb_fraudes": rapport["nb_fraudes"],
                "montant_fraudes": rapport["montant_fraudes"],
            },
        )
    )


def verifier_delai_rapport(
    genere_le: datetime, planifie_a: datetime, sla_minutes: int
) -> tuple[bool, float]:
    """(respecté ?, retard en minutes) ; livraison attendue ``sla_minutes`` après l'heure dite."""
    limite = planifie_a + timedelta(minutes=sla_minutes)
    retard = (genere_le - limite).total_seconds() / 60
    return retard <= 0, max(0.0, retard)


def produire_rapport(
    config: Configuration,
    entrepot: Entrepot,
    jour: str,
    notificateur: Notificateur | None = None,
    *,
    planifie_a: datetime | None = None,
) -> dict[str, Any]:
    """Enchaîne toutes les étapes (usage en ligne de commande ; le DAG les appelle une à une)."""
    debut = maintenant_iso()
    verifier_donnees_du_jour(entrepot, jour)
    rapport = calculer_rapport(entrepot, jour)
    chemins = ecrire_rapport(config, entrepot, rapport)
    entrepot.enregistrer_lignage(
        "rapport_du_matin",
        "argent_transactions,meta_rejets",
        "or_rapport_quotidien,or_rapport_categorie,fichiers",
        nb_entrees=rapport["nb_paiements"],
        nb_sorties=1,
        nb_rejets=rapport["nb_rejets"],
        debut=debut,
        version_regles=VERSION_REGLES,
        version_modele=",".join(rapport["versions_modele"]) or None,
        details={"jour": jour, **chemins},
    )
    if notificateur is not None:
        notifier_rapport(notificateur, rapport, chemins)
    if planifie_a is not None:
        respecte, retard = verifier_delai_rapport(
            datetime.now(UTC), planifie_a, config.rapport.sla_minutes
        )
        if not respecte:
            avertissement(
                _journal, "sla_depasse", sla="rapport_du_matin", retard_min=round(retard, 1)
            )
    info(
        _journal,
        "rapport_produit",
        jour=jour,
        paiements=rapport["nb_paiements"],
        fraudes=rapport["nb_fraudes"],
        **chemins,
    )
    return {**rapport, "chemins": chemins}


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Produit le rapport du matin pour un jour.")
    analyseur.add_argument("--jour", help="AAAA-MM-JJ (défaut : la veille, en UTC)")
    analyseur.add_argument("--sans-notification", action="store_true")
    arguments = analyseur.parse_args(argv)
    config = charger_configuration()
    configurer_journal(config.journal.niveau, config.journal.console, config.journal.fichier)
    jour = arguments.jour or jour_de_la_veille(datetime.now(UTC))
    entrepot = Entrepot(config.dsn_entrepot())
    entrepot.initialiser()
    notificateur = None if arguments.sans_notification else Notificateur(construire_canaux(config))
    try:
        rapport = produire_rapport(config, entrepot, jour, notificateur)
    except DonneesAbsentes as exc:
        print(f"Rapport impossible : {exc}", file=sys.stderr)
        return 3
    finally:
        entrepot.fermer()
    print(
        f"Rapport {jour} : {rapport['nb_paiements']} paiements, {rapport['nb_fraudes']} fraudes."
    )
    print(f"HTML : {rapport['chemins']['html']}\nCSV  : {rapport['chemins']['csv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
