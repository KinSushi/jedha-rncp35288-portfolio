# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Rapport du matin : garanties de refus, bornes exactes et état inchangé (critères 1.1 et 6.1).

Chaque test de ce fichier énonce une règle que le rapport doit tenir **même quand l'entrée est
mauvaise** : une source muette, un jour mal écrit, un dossier inutilisable, une ligne invalide.
Dans tous ces cas, on compare un instantané pris *avant* à l'état *après* : rien ne doit avoir été
écrit, publié ni notifié. Les valeurs attendues sont écrites en toutes lettres (chaînes complètes,
dictionnaires complets) pour qu'une modification d'un seul caractère soit vue.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from detection_fraude import VERSION_REGLES
from detection_fraude import rapport as module_rapport
from detection_fraude.entrepot import Entrepot
from detection_fraude.notification import Notificateur
from detection_fraude.rapport import (
    DonneesAbsentes,
    calculer_rapport,
    ecrire_csv,
    ecrire_rapport,
    jour_de_la_veille,
    notifier_rapport,
    produire_rapport,
    rendre_html,
    verifier_delai_rapport,
    verifier_donnees_du_jour,
)
from tests.helpers import CanalEnregistreur
from tests.test_rapport import ligne_argent

JOUR = "2026-10-09"
VEILLE = "2026-10-08"
INSTANT_FIXE = "2026-10-10T07:05:00+00:00"
TOUTES_LES_TABLES = (
    "bronze_transactions",
    "argent_transactions",
    "or_alertes_fraude",
    "or_rapport_quotidien",
    "or_rapport_categorie",
    "meta_rejets",
    "meta_lignage",
)
CLES_FRAUDE = (
    "horodatage_utc",
    "trans_num",
    "carte_fin",
    "commercant",
    "categorie",
    "etat",
    "montant",
    "score",
    "etiquette_source",
)
MESSAGE_MUET = "0 transaction(s) le 2026-10-09 : source probablement muette."


# --------------------------------------------------------------------------------------------
# Outils de test
# --------------------------------------------------------------------------------------------
def instantane(entrepot: Entrepot, *tables: str) -> dict[str, list[dict[str, Any]]]:
    """Photographie triée du contenu des tables demandées (toutes, par défaut)."""
    return {
        table: sorted(entrepot.requete(f"SELECT * FROM {table}"), key=repr)
        for table in (tables or TOUTES_LES_TABLES)
    }


def inserer(entrepot: Entrepot, lignes: list[dict[str, Any]]) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, lignes)


def rejeter(entrepot: Entrepot, jour: str, cle: str) -> None:
    with entrepot.transaction() as conn:
        entrepot.enregistrer_rejet(conn, {"etape": "producteur", "raison": "x"}, cle, "r", jour)


def message_de_date(jour: str) -> str:
    """Message exact que ``date.fromisoformat`` donne pour un jour mal écrit."""
    with pytest.raises(ValueError) as attendu:
        date.fromisoformat(jour)
    return str(attendu.value)


class EntrepotInterdit:
    """Doublure qui échoue à la moindre requête : prouve qu'on refuse *avant* de lire la base."""

    def scalaire(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("l'entrepôt ne devait pas être interrogé")

    def requete(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("l'entrepôt ne devait pas être interrogé")


class HorlogeFixe(datetime):
    """Remplace la classe ``datetime`` du module : ``now`` rend toujours 10/10/2026 07:20 UTC."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is None:  # ni now() ni now(None) : l'heure locale de la machine est refusée
            raise AssertionError("l'heure se lit en UTC : now(UTC)")
        return datetime(2026, 10, 10, 7, 20, tzinfo=UTC).astimezone(tz)


@pytest.fixture
def horloge_et_instant_fixes(monkeypatch):
    """Fige « maintenant » (07 h 20 UTC) et l'horodatage de génération des rapports."""
    monkeypatch.setattr(module_rapport, "datetime", HorlogeFixe)
    monkeypatch.setattr(module_rapport, "maintenant_iso", lambda: INSTANT_FIXE)


@pytest.fixture
def fermetures(monkeypatch):
    """Enregistre chaque fermeture de l'entrepôt ouvert par ``main`` (dialecte de la base)."""
    appels: list[str] = []

    class EntrepotEspion(Entrepot):
        def fermer(self) -> None:
            appels.append(self.dialecte)
            super().fermer()

    monkeypatch.setattr(module_rapport, "Entrepot", EntrepotEspion)
    return appels


@pytest.fixture
def journal_enregistre(monkeypatch):
    """Remplace ``configurer_journal`` : garde ses arguments et laisse le collecteur en place."""
    appels: list[tuple[Any, ...]] = []
    monkeypatch.setattr(module_rapport, "configurer_journal", lambda *args: appels.append(args))
    return appels


def construire_journee() -> list[dict[str, Any]]:
    """Dix paiements du 9 octobre : tous les cas d'égalité du tri sont présents.

    * ``a_cat`` et ``b_cat`` ont 2 fraudes, mais ``a_cat`` en pèse 100,0 contre 30,5 ;
    * ``c_cat`` et ``z_cat`` sont à égalité complète (1 fraude de 5,0) : ordre alphabétique ;
    * ``d_cat`` et ``e_cat`` n'ont aucune fraude : ordre alphabétique ;
    * deux fraudes de ``b_cat`` ont le même horodatage : l'ordre se fait sur ``trans_num``.
    """
    lignes = [
        ligne_argent(1, montant=60.0, fraude=1, categorie="a_cat", etiquette=1),
        ligne_argent(2, montant=40.0, fraude=1, categorie="a_cat", etiquette=0),
        ligne_argent(3, montant=1.001, categorie="a_cat"),
        ligne_argent(4, montant=10.1254, fraude=1, categorie="b_cat"),
        ligne_argent(5, montant=20.3756, fraude=1, categorie="b_cat"),
        ligne_argent(6, montant=5.0, fraude=1, categorie="z_cat"),
        ligne_argent(7, montant=5.0, fraude=1, categorie="c_cat"),
        ligne_argent(8, montant=100.123, categorie="d_cat", etiquette=1),
        ligne_argent(9, montant=0.004, categorie="d_cat"),
        ligne_argent(10, montant=3.0, categorie="e_cat", etiquette=0),
    ]
    for ligne in lignes[:3]:
        ligne["version_modele"] = "v2"
    lignes[4]["horodatage_s"] = lignes[3]["horodatage_s"]
    lignes[4]["trans_num"] = f"{0:032x}"
    return lignes


def charger_journee(entrepot: Entrepot) -> list[dict[str, Any]]:
    """Insère la journée, la veille, des paiements « bruit » d'autres jours et des rejets."""
    journee = construire_journee()
    veille = [
        ligne_argent(11, jour=VEILLE, montant=10.0),
        ligne_argent(12, jour=VEILLE, montant=30.5, fraude=1, etiquette=1),
    ]
    bruit = [
        ligne_argent(13, jour="2026-10-07", montant=999.0, fraude=1, etiquette=1),
        ligne_argent(14, jour="2026-10-10", montant=888.0, fraude=1, etiquette=1),
    ]
    inserer(entrepot, list(reversed(journee + veille + bruit)))
    for cle in ("1", "2", "3"):
        rejeter(entrepot, JOUR, cle)
    rejeter(entrepot, VEILLE, "4")
    rejeter(entrepot, "2026-10-10", "5")
    return journee


VEILLE_TYPE = {
    "nb_paiements": 1_000_000,
    "montant_total": 2_000_000.0,
    "nb_fraudes": 4,
    "montant_fraudes": 100.0,
    "nb_etiquetees": 0,
    "nb_vrais_positifs": 0,
    "nb_fraudes_reelles": 0,
}


def rapport_complet() -> dict[str, Any]:
    """Un rapport déjà calculé (sans base) avec des valeurs choisies pour tester le rendu."""
    return {
        "jour": JOUR,
        "nb_paiements": 1_234_567,
        "montant_total": 1_234_567.89,
        "nb_fraudes": 3,
        "montant_fraudes": 1500.5,
        "taux_fraude_nb": 0.0025,
        "taux_fraude_montant": 1500.5 / 1_234_567.89,
        "nb_rejets": 12,
        "nb_etiquetees": 4,
        "nb_vrais_positifs": 1,
        "nb_fraudes_reelles": 2,
        "veille": dict(VEILLE_TYPE),
        "categories": [
            {
                "categorie": "shopping_net",
                "nb_paiements": 1200,
                "nb_fraudes": 2,
                "montant_total": 4321.5,
                "montant_fraudes": 1000.0,
            },
            {
                "categorie": "grocery_pos",
                "nb_paiements": 3,
                "nb_fraudes": 0,
                "montant_total": 10.25,
                "montant_fraudes": 0.0,
            },
        ],
        "fraudes": [
            {
                "horodatage_utc": "2026-10-09T10:01:00+00:00",
                "trans_num": "a" * 32,
                "carte_fin": "4242",
                "commercant": "Café <Zéro> L'Épicerie",
                "categorie": "shopping_net",
                "etat": "NY",
                "montant": 1000.0,
                "score": 0.98765,
                "etiquette_source": 1,
            }
        ],
        "versions_modele": ["v1", "v2"],
    }


def carte(titre: str, valeur: str, note: str) -> str:
    return (
        f'<div class="carte"><div class="titre">{titre}</div>'
        f'<div class="valeur">{valeur}</div><div class="note">{note}</div></div>'
    )


# --------------------------------------------------------------------------------------------
# jour_de_la_veille
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("instant", "attendu"),
    [
        (datetime(2026, 10, 10, 7, 0, tzinfo=UTC), "2026-10-09"),
        (datetime(2026, 10, 10, 0, 0, tzinfo=UTC), "2026-10-09"),  # minuit pile
        (datetime(2026, 10, 9, 23, 59, 59, tzinfo=UTC), "2026-10-08"),  # une seconde avant
        (datetime(2026, 1, 1, 0, 0, tzinfo=UTC), "2025-12-31"),  # passage d'année
        (datetime(2026, 3, 1, 0, 0, tzinfo=UTC), "2026-02-28"),  # février non bissextile
        (datetime(2028, 3, 1, 12, 0, tzinfo=UTC), "2028-02-29"),  # février bissextile
        (datetime(2026, 10, 10, 1, 0), "2026-10-09"),  # naïf = UTC, sans conversion locale
        (datetime(2026, 10, 10, 23, 30), "2026-10-09"),  # naïf tard le soir : idem
        # Avec fuseau : on convertit en UTC *avant* de reculer d'un jour.
        (datetime(2026, 10, 10, 1, 0, tzinfo=timezone(timedelta(hours=2))), "2026-10-08"),
        (datetime(2026, 10, 9, 22, 0, tzinfo=timezone(timedelta(hours=-5))), "2026-10-09"),
        (datetime(2026, 10, 10, 3, 0, tzinfo=UTC), "2026-10-09"),
    ],
)
def test_la_veille_se_compte_en_jours_utc_et_non_en_heure_locale(instant, attendu):
    """Les horodatages de la source sont en UTC : « la veille » ne dépend pas du fuseau."""
    assert jour_de_la_veille(instant) == attendu


# --------------------------------------------------------------------------------------------
# verifier_donnees_du_jour
# --------------------------------------------------------------------------------------------
def test_une_source_muette_leve_donnees_absentes_avec_le_message_exact_et_journalise(
    entrepot, evenements
):
    """Aucune transaction : on refuse un rapport vide et l'exploitation voit l'erreur."""
    etat_avant = instantane(entrepot)
    with pytest.raises(DonneesAbsentes) as erreur:
        verifier_donnees_du_jour(entrepot, JOUR)
    assert str(erreur.value) == MESSAGE_MUET
    assert evenements.tous() == [("ERROR", "donnees_absentes", {"jour": JOUR, "transactions": 0})]
    assert instantane(entrepot) == etat_avant


def test_pile_au_minimum_le_jour_est_accepte_et_un_de_moins_est_refuse(entrepot, evenements):
    """Borne du contrôle : N transactions pour un minimum de N passent, N - 1 échouent."""
    inserer(entrepot, [ligne_argent(1), ligne_argent(2)])
    with pytest.raises(DonneesAbsentes) as erreur:
        verifier_donnees_du_jour(entrepot, JOUR, minimum=3)
    assert str(erreur.value) == "2 transaction(s) le 2026-10-09 : source probablement muette."
    assert evenements.nommes("donnees_absentes") == [("ERROR", {"jour": JOUR, "transactions": 2})]
    inserer(entrepot, [ligne_argent(3)])
    assert verifier_donnees_du_jour(entrepot, JOUR, minimum=3) == 3
    assert evenements.nommes("donnees_absentes") == [("ERROR", {"jour": JOUR, "transactions": 2})]


def test_par_defaut_une_seule_transaction_suffit_et_zero_est_refuse(entrepot):
    """Le minimum par défaut est 1 : une journée non vide n'est jamais prise pour une panne."""
    with pytest.raises(DonneesAbsentes) as erreur:
        verifier_donnees_du_jour(entrepot, JOUR)
    assert str(erreur.value) == MESSAGE_MUET
    inserer(entrepot, [ligne_argent(1)])
    assert verifier_donnees_du_jour(entrepot, JOUR) == 1


def test_les_transactions_des_autres_jours_ne_comptent_pas_pour_le_jour_demande(entrepot):
    """Une source muette aujourd'hui n'est pas masquée par l'activité d'hier ou de demain."""
    inserer(
        entrepot,
        [
            ligne_argent(1, jour=VEILLE),
            ligne_argent(2, jour=VEILLE),
            ligne_argent(3, jour="2026-10-10"),
        ],
    )
    with pytest.raises(DonneesAbsentes) as erreur:
        verifier_donnees_du_jour(entrepot, JOUR)
    assert str(erreur.value) == MESSAGE_MUET
    assert verifier_donnees_du_jour(entrepot, VEILLE) == 2
    assert verifier_donnees_du_jour(entrepot, "2026-10-10") == 1


@pytest.mark.parametrize("jour", ["10/10/2026", "", "2026-13-01", "2026-02-30", "hier"])
def test_un_jour_mal_ecrit_est_refuse_avant_toute_lecture_de_la_base(jour, evenements):
    """Un format invalide est une erreur de l'appelant, pas une source muette."""
    with pytest.raises(ValueError) as erreur:
        verifier_donnees_du_jour(EntrepotInterdit(), jour)
    assert str(erreur.value) == message_de_date(jour)
    assert evenements.tous() == []


def test_le_jour_est_normalise_avant_d_etre_cite_dans_l_erreur_et_dans_le_journal(
    entrepot, evenements
):
    """« 20261009 » désigne le 9 octobre : le message et l'événement citent la forme ISO."""
    with pytest.raises(DonneesAbsentes) as erreur:
        verifier_donnees_du_jour(entrepot, "20261009")
    assert str(erreur.value) == MESSAGE_MUET
    assert evenements.nommes("donnees_absentes") == [("ERROR", {"jour": JOUR, "transactions": 0})]
    inserer(entrepot, [ligne_argent(1)])
    assert verifier_donnees_du_jour(entrepot, "20261009") == 1


# --------------------------------------------------------------------------------------------
# calculer_rapport
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("jour", ["10/10/2026", "", "2026-13-01", "demain"])
def test_calculer_rapport_refuse_un_jour_invalide_sans_interroger_l_entrepot(jour):
    """Le jour est validé en tout premier : une date mal écrite ne produit aucune requête."""
    with pytest.raises(ValueError) as erreur:
        calculer_rapport(EntrepotInterdit(), jour)
    assert str(erreur.value) == message_de_date(jour)


def test_calculer_rapport_donne_tous_les_champs_exacts_et_laisse_la_base_inchangee(entrepot):
    """Agrégats, tris à égalité, veille, rejets et versions exacts ; le calcul ne fait que lire."""
    journee = charger_journee(entrepot)
    etat_avant = instantane(entrepot)
    rapport = calculer_rapport(entrepot, JOUR)
    assert instantane(entrepot) == etat_avant
    ordre_fraudes = [journee[i] for i in (0, 1, 4, 3, 5, 6)]  # 5 avant 4 : même heure, id plus bas
    assert rapport == {
        "jour": JOUR,
        "nb_paiements": 10,
        "montant_total": 244.63,
        "nb_fraudes": 6,
        "montant_fraudes": 140.5,
        "nb_etiquetees": 4,
        "nb_vrais_positifs": 1,
        "nb_fraudes_reelles": 2,
        "taux_fraude_nb": 6 / 10,
        "taux_fraude_montant": 140.5 / 244.63,
        "nb_rejets": 3,
        "veille": {
            "nb_paiements": 2,
            "montant_total": 40.5,
            "nb_fraudes": 1,
            "montant_fraudes": 30.5,
            "nb_etiquetees": 1,
            "nb_vrais_positifs": 1,
            "nb_fraudes_reelles": 1,
        },
        "categories": [
            {
                "categorie": "a_cat",
                "nb_paiements": 3,
                "nb_fraudes": 2,
                "montant_total": 101.0,
                "montant_fraudes": 100.0,
            },
            {
                "categorie": "b_cat",
                "nb_paiements": 2,
                "nb_fraudes": 2,
                "montant_total": 30.5,
                "montant_fraudes": 30.5,
            },
            {
                "categorie": "c_cat",
                "nb_paiements": 1,
                "nb_fraudes": 1,
                "montant_total": 5.0,
                "montant_fraudes": 5.0,
            },
            {
                "categorie": "z_cat",
                "nb_paiements": 1,
                "nb_fraudes": 1,
                "montant_total": 5.0,
                "montant_fraudes": 5.0,
            },
            {
                "categorie": "d_cat",
                "nb_paiements": 2,
                "nb_fraudes": 0,
                "montant_total": 100.13,
                "montant_fraudes": 0.0,
            },
            {
                "categorie": "e_cat",
                "nb_paiements": 1,
                "nb_fraudes": 0,
                "montant_total": 3.0,
                "montant_fraudes": 0.0,
            },
        ],
        "fraudes": [{cle: ligne[cle] for cle in CLES_FRAUDE} for ligne in ordre_fraudes],
        "versions_modele": ["v1", "v2"],
    }


def test_une_journee_vide_ou_a_montant_nul_donne_des_taux_a_zero_sans_division(entrepot):
    """Aucun paiement, ou des paiements à 0, ne doit jamais faire échouer le rapport du matin."""
    zeros = {
        "nb_paiements": 0,
        "montant_total": 0.0,
        "nb_fraudes": 0,
        "montant_fraudes": 0.0,
        "nb_etiquetees": 0,
        "nb_vrais_positifs": 0,
        "nb_fraudes_reelles": 0,
    }
    etat_avant = instantane(entrepot)
    vide = calculer_rapport(entrepot, JOUR)
    assert vide == {
        "jour": JOUR,
        **zeros,
        "taux_fraude_nb": 0.0,
        "taux_fraude_montant": 0.0,
        "nb_rejets": 0,
        "veille": zeros,
        "categories": [],
        "fraudes": [],
        "versions_modele": [],
    }
    assert instantane(entrepot) == etat_avant
    inserer(entrepot, [ligne_argent(1, jour="2026-10-20", montant=0.0, fraude=1)])
    etat_avant = instantane(entrepot)
    nul = calculer_rapport(entrepot, "2026-10-20")
    assert (nul["montant_total"], nul["montant_fraudes"]) == (0.0, 0.0)
    assert (nul["taux_fraude_nb"], nul["taux_fraude_montant"]) == (1.0, 0.0)
    assert instantane(entrepot) == etat_avant


# --------------------------------------------------------------------------------------------
# rendre_html
# --------------------------------------------------------------------------------------------
CSS = [
    "body{font-family:Segoe UI,Arial,sans-serif;margin:2rem auto;max-width:1000px;color:#1b2430}",
    "h1{font-size:1.6rem} h2{margin-top:2rem;font-size:1.2rem}",
    ".cartes{display:flex;flex-wrap:wrap;gap:12px}",
    ".carte{flex:1 1 150px;border:1px solid #d5dbe3;border-radius:8px;padding:12px}",
    ".titre{font-size:.8rem;color:#5b6675} .valeur{font-size:1.5rem;font-weight:600}",
    ".note{font-size:.75rem;color:#5b6675}",
    "table{border-collapse:collapse;width:100%;font-size:.85rem}",
    "th,td{border-bottom:1px solid #e3e8ee;padding:6px 8px;text-align:left}",
    "th{background:#f3f5f8} td.n{text-align:right} td.m{font-family:Consolas,monospace}",
    "footer{margin-top:3rem;font-size:.75rem;color:#5b6675;border-top:1px solid #e3e8ee;",
    "padding-top:8px}",
]


def test_rendre_html_ne_modifie_pas_le_rapport_et_produit_la_page_exacte():
    """La page est une fonction pure du rapport : même entrée, même texte, entrée intacte."""
    rapport = rapport_complet()
    rapport_avant = copy.deepcopy(rapport)
    page = rendre_html(rapport)
    assert rapport == rapport_avant
    assert rendre_html(rapport) == page
    assert page.split("</style>", 1)[0] == (
        '<!DOCTYPE html>\n<html lang="fr"><head><meta charset="utf-8">\n'
        "<title>Rapport du matin - 2026-10-09</title>\n<style>\n" + "\n".join(CSS) + "\n"
    )
    cartes = "".join(
        [
            carte("Paiements", "1 234 567", "veille : +23.5 %"),
            carte("Montant total", "1 234 567.89", "veille : -38.3 %"),
            carte("Fraudes détectées", "3", "veille : -25.0 %"),
            carte("Montant des fraudes", "1 500.50", "0.122 % du montant"),
            carte("Taux de fraude", "0.250 %", "en nombre de paiements"),
            carte("Lignes rejetées", "12", "file des rejets (DLQ)"),
        ]
    )
    categories = (
        "<tr><td>shopping_net</td><td class='n'>1200</td><td class='n'>2</td>"
        "<td class='n'>4321.50</td><td class='n'>1000.00</td></tr>"
        "<tr><td>grocery_pos</td><td class='n'>3</td><td class='n'>0</td>"
        "<td class='n'>10.25</td><td class='n'>0.00</td></tr>"
    )
    fraudes = (
        "<tr><td>2026-10-09T10:01:00+00:00</td><td class='m'>" + "a" * 32 + "</td>"
        "<td>****4242</td><td>Café &lt;Zéro&gt; L&#x27;Épicerie</td><td>shopping_net</td>"
        "<td class='n'>1000.00</td><td class='n'>0.988</td></tr>"
    )
    controle = (
        "<h2>Contrôle du modèle</h2><p>La source fournit une étiquette pour 4 transaction(s) "
        "de la journée : 2 fraude(s) réelle(s), 1 détectée(s) par le modèle. "
        "Précision du jour : 33.3 % ; rappel du jour : 50.0 %.</p>"
    )
    assert page.split("</style>", 1)[1] == (
        "</head><body>\n"
        "<h1>Rapport du matin : paiements et fraudes du 2026-10-09</h1>\n"
        "<p>Jour UTC. Modèle(s) utilisé(s) : v1, v2.</p>\n"
        f'<div class="cartes">{cartes}</div>\n'
        "<h2>Par catégorie</h2>\n"
        "<table><tr><th>Catégorie</th><th>Paiements</th><th>Fraudes</th><th>Montant</th>\n"
        f"<th>Montant fraudes</th></tr>\n{categories}</table>\n"
        "<h2>Fraudes détectées</h2>\n"
        "<table><tr><th>Horodatage (UTC)</th><th>Transaction</th><th>Carte</th>"
        "<th>Commerçant</th>\n"
        f"<th>Catégorie</th><th>Montant</th><th>Score</th></tr>{fraudes}</table>\n"
        f"{controle}\n"
        "<footer>Données masquées : aucun nom, adresse ni numéro de carte complet.\n"
        "Copyright © 2026 · voir le fichier LICENSE du dépôt</footer>\n"
        "</body></html>\n"
    )


VEILLE_NULLE = dict.fromkeys(VEILLE_TYPE, 0)
HOSTILE = '<script>alert("x")</script>'
HOSTILE_ECHAPPE = "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"


def variante(**surcharges: Any) -> dict[str, Any]:
    return {**rapport_complet(), **surcharges}


CAS_HTML = {
    "hausse": (
        variante(nb_paiements=150, veille={**VEILLE_TYPE, "nb_paiements": 100}),
        [carte("Paiements", "150", "veille : +50.0 %")],
        [],
    ),
    "baisse": (
        variante(nb_paiements=75, veille={**VEILLE_TYPE, "nb_paiements": 100}),
        [carte("Paiements", "75", "veille : -25.0 %")],
        [],
    ),
    "stable_est_positif": (
        variante(nb_paiements=100, veille={**VEILLE_TYPE, "nb_paiements": 100}),
        [carte("Paiements", "100", "veille : +0.0 %")],
        [],
    ),
    "arrondi_a_une_decimale": (
        variante(nb_paiements=1, veille={**VEILLE_TYPE, "nb_paiements": 3}),
        [carte("Paiements", "1", "veille : -66.7 %")],
        [],
    ),
    "reference_nulle": (
        variante(veille=VEILLE_NULLE),
        [
            carte("Paiements", "1 234 567", "veille : n/d"),
            carte("Montant total", "1 234 567.89", "veille : n/d"),
            carte("Fraudes détectées", "3", "veille : n/d"),
        ],
        ["veille : +", "veille : -"],
    ),
    "separateur_de_milliers_pile_a_mille": (
        variante(
            nb_paiements=1000,
            montant_total=1000.0,
            montant_fraudes=999.99,
            veille={**VEILLE_TYPE, "nb_paiements": 2000, "montant_total": 2000.0},
        ),
        [
            carte("Paiements", "1 000", "veille : -50.0 %"),
            carte("Montant total", "1 000.00", "veille : -50.0 %"),
            carte("Montant des fraudes", "999.99", "0.122 % du montant"),
        ],
        [],
    ),
    "juste_sous_mille_sans_separateur": (
        variante(nb_paiements=999, montant_total=999.99, montant_fraudes=0.5),
        [
            carte("Paiements", "999", "veille : -99.9 %"),
            carte("Montant total", "999.99", "veille : -100.0 %"),
            carte("Montant des fraudes", "0.50", "0.122 % du montant"),
        ],
        [],
    ),
    "reference_flottante_nulle": (
        variante(veille={**VEILLE_TYPE, "montant_total": 0.0}),
        [
            carte("Paiements", "1 234 567", "veille : +23.5 %"),
            carte("Montant total", "1 234 567.89", "veille : n/d"),
        ],
        [],
    ),
    "sans_etiquette_pas_de_controle_du_modele": (
        variante(nb_etiquetees=0, nb_vrais_positifs=2, nb_fraudes_reelles=2),
        ["</table>\n\n<footer>"],  # le bloc absent laisse une ligne vide, pas « None »
        ["Contrôle du modèle", "La source fournit une étiquette", "Précision du jour", "None"],
    ),
    "precision_non_definie_sans_fraude_detectee": (
        variante(nb_etiquetees=2, nb_fraudes=0, nb_vrais_positifs=0, nb_fraudes_reelles=2),
        ["Précision du jour : n/d ; rappel du jour : 0.0 %.</p>"],
        [],
    ),
    "rappel_non_defini_sans_fraude_reelle": (
        variante(nb_etiquetees=1, nb_fraudes=2, nb_vrais_positifs=0, nb_fraudes_reelles=0),
        [
            "pour 1 transaction(s) de la journée : 0 fraude(s) réelle(s), 0 détectée(s)",
            "Précision du jour : 0.0 % ; rappel du jour : n/d.</p>",
        ],
        [],
    ),
    "precision_et_rappel_ne_sont_pas_interchangeables": (
        variante(nb_etiquetees=3, nb_fraudes=4, nb_vrais_positifs=1, nb_fraudes_reelles=2),
        ["Précision du jour : 25.0 % ; rappel du jour : 50.0 %.</p>"],
        [],
    ),
    "tableaux_vides": (
        variante(categories=[], fraudes=[]),
        [
            "<th>Montant fraudes</th></tr>\n</table>",
            "<th>Score</th></tr><tr><td colspan='7'>Aucune fraude détectée ce jour-là.</td></tr>"
            "</table>",
        ],
        ["<td class='n'>", "****"],
    ),
    "aucune_version_de_modele": (
        variante(versions_modele=[]),
        ["<p>Jour UTC. Modèle(s) utilisé(s) : n/d.</p>"],
        [],
    ),
    "valeurs_hostiles_echappees": (
        variante(
            jour=HOSTILE,
            versions_modele=[HOSTILE],
            categories=[{**rapport_complet()["categories"][0], "categorie": HOSTILE}],
            fraudes=[
                {
                    **rapport_complet()["fraudes"][0],
                    "horodatage_utc": HOSTILE,
                    "trans_num": HOSTILE,
                    "carte_fin": HOSTILE,
                    "commercant": HOSTILE,
                    "categorie": HOSTILE,
                }
            ],
        ),
        [
            f"<title>Rapport du matin - {HOSTILE_ECHAPPE}</title>",
            f"paiements et fraudes du {HOSTILE_ECHAPPE}</h1>",
            f"Modèle(s) utilisé(s) : {HOSTILE_ECHAPPE}.</p>",
            f"<tr><td>{HOSTILE_ECHAPPE}</td><td class='n'>1200</td>",
            f"<tr><td>{HOSTILE_ECHAPPE}</td><td class='m'>{HOSTILE_ECHAPPE}</td>"
            f"<td>****{HOSTILE_ECHAPPE}</td><td>{HOSTILE_ECHAPPE}</td><td>{HOSTILE_ECHAPPE}</td>",
        ],
        ["<script>", 'alert("x")'],
    ),
}


@pytest.mark.parametrize(
    ("rapport", "attendus", "interdits"), list(CAS_HTML.values()), ids=list(CAS_HTML)
)
def test_rendre_html_ne_modifie_pas_le_rapport_et_traite_chaque_variante(
    rapport, attendus, interdits
):
    """Signes, arrondis, « n/d », blocs facultatifs et échappement, sans toucher à l'entrée."""
    rapport_avant = copy.deepcopy(rapport)
    page = rendre_html(rapport)
    assert rapport == rapport_avant
    for fragment in attendus:
        assert fragment in page
    for fragment in interdits:
        assert fragment not in page


# --------------------------------------------------------------------------------------------
# ecrire_csv
# --------------------------------------------------------------------------------------------
def test_ecrire_csv_ne_modifie_pas_le_rapport_et_ecrit_les_octets_exacts(tmp_path):
    """Excel français : BOM, point-virgule, fins de ligne CRLF, formats .2f et .4f, UTF-8."""
    entete = "horodatage_utc;trans_num;carte_fin;commercant;categorie;etat;montant;score\r\n"
    bom = b"\xef\xbb\xbf"
    chemin = tmp_path / "fraudes.csv"
    chemin.write_text("ancien contenu qui doit disparaître\n" * 5, encoding="utf-8")
    rapport = {
        "fraudes": [
            {
                "horodatage_utc": "2026-10-09T10:01:00+00:00",
                "trans_num": "a" * 32,
                "carte_fin": "4242",
                "commercant": "Café;Étoile",
                "categorie": "shopping_net",
                "etat": "NY",
                "montant": 1234.5,
                "score": 0.98765432,
            },
            {
                "horodatage_utc": "2026-10-09T11:02:00+00:00",
                "trans_num": "b" * 32,
                "carte_fin": "0007",
                "commercant": "Simple",
                "categorie": "misc_net",
                "etat": "CA",
                "montant": 7,
                "score": 0.5,
            },
        ]
    }
    rapport_avant = copy.deepcopy(rapport)
    ecrire_csv(rapport, chemin)
    assert rapport == rapport_avant
    ligne_1 = (
        "2026-10-09T10:01:00+00:00;" + "a" * 32 + ';4242;"Café;Étoile";shopping_net;NY;'
        "1234.50;0.9877\r\n"
    )
    ligne_2 = "2026-10-09T11:02:00+00:00;" + "b" * 32 + ";0007;Simple;misc_net;CA;7.00;0.5000\r\n"
    assert chemin.read_bytes() == bom + (entete + ligne_1 + ligne_2).encode("utf-8")
    vide = tmp_path / "vide.csv"
    ecrire_csv({"fraudes": []}, vide)
    assert vide.read_bytes() == bom + entete.encode("utf-8")


# --------------------------------------------------------------------------------------------
# ecrire_rapport
# --------------------------------------------------------------------------------------------
def tables_du_jour(entrepot: Entrepot, jour: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "quotidien": entrepot.requete(
            "SELECT * FROM or_rapport_quotidien WHERE jour = :j", j=jour
        ),
        "categories": entrepot.requete(
            "SELECT * FROM or_rapport_categorie WHERE jour = :j ORDER BY categorie", j=jour
        ),
    }


def test_ecrire_rapport_ne_modifie_pas_le_rapport_et_ecrit_fichiers_et_tables_exacts(
    config, entrepot, horloge_et_instant_fixes
):
    """Noms de fichiers, contenu, dossiers parents créés et couche or : tout est exact."""
    dossier = config.racine_donnees / "profond" / "rapports"
    config_profonde = replace(config, rapport=replace(config.rapport, dossier=dossier))
    rapport = rapport_complet()
    rapport_avant = copy.deepcopy(rapport)
    chemins = ecrire_rapport(config_profonde, entrepot, rapport)
    html = str(dossier / "rapport_2026-10-09.html")
    csv_ = str(dossier / "fraudes_2026-10-09.csv")
    assert chemins == {"html": html, "csv": csv_}
    assert rapport == rapport_avant
    assert sorted(p.name for p in dossier.iterdir()) == [
        "fraudes_2026-10-09.csv",
        "rapport_2026-10-09.html",
    ]
    assert Path(html).read_text(encoding="utf-8") == rendre_html(rapport)
    assert Path(csv_).read_text(encoding="utf-8-sig").splitlines()[1] == (
        "2026-10-09T10:01:00+00:00;" + "a" * 32 + ";4242;Café <Zéro> L'Épicerie;shopping_net;NY;"
        "1000.00;0.9877"
    )
    ecrit = tables_du_jour(entrepot, JOUR)
    assert ecrit["quotidien"] == [
        {
            "jour": JOUR,
            "nb_paiements": 1_234_567,
            "nb_fraudes": 3,
            "montant_total": 1_234_567.89,
            "montant_fraudes": 1500.5,
            "taux_fraude_nb": 0.0025,
            "taux_fraude_montant": 1500.5 / 1_234_567.89,
            "nb_rejets": 12,
            "nb_etiquetees": 4,
            "nb_vrais_positifs": 1,
            "nb_fraudes_reelles": 2,
            "genere_le": INSTANT_FIXE,
            "version_regles": VERSION_REGLES,
            "chemin_html": html,
            "chemin_csv": csv_,
        }
    ]
    assert ecrit["categories"] == [
        {
            "jour": JOUR,
            "categorie": "grocery_pos",
            "nb_paiements": 3,
            "nb_fraudes": 0,
            "montant_total": 10.25,
            "montant_fraudes": 0.0,
        },
        {
            "jour": JOUR,
            "categorie": "shopping_net",
            "nb_paiements": 1200,
            "nb_fraudes": 2,
            "montant_total": 4321.5,
            "montant_fraudes": 1000.0,
        },
    ]


def test_reecrire_un_jour_remplace_ses_lignes_et_laisse_intacts_les_autres_jours(config, entrepot):
    """La réécriture du 9 est idempotente et ne touche jamais le rapport du 8."""
    ecrire_rapport(config, entrepot, {**rapport_complet(), "jour": VEILLE})
    ecrire_rapport(config, entrepot, rapport_complet())
    autre_jour_avant = tables_du_jour(entrepot, VEILLE)
    assert [c["categorie"] for c in autre_jour_avant["categories"]] == [
        "grocery_pos",
        "shopping_net",
    ]
    nouveau = {
        **rapport_complet(),
        "nb_fraudes": 9,
        "categories": [
            {
                "categorie": "travel",
                "nb_paiements": 8,
                "nb_fraudes": 1,
                "montant_total": 80.0,
                "montant_fraudes": 8.0,
            }
        ],
    }
    ecrire_rapport(config, entrepot, nouveau)
    assert tables_du_jour(entrepot, VEILLE) == autre_jour_avant
    du_jour = tables_du_jour(entrepot, JOUR)
    assert [c["categorie"] for c in du_jour["categories"]] == ["travel"]
    assert [q["nb_fraudes"] for q in du_jour["quotidien"]] == [9]
    assert entrepot.scalaire("SELECT COUNT(*) FROM or_rapport_quotidien") == 2


def test_une_ligne_invalide_annule_toute_la_reecriture_et_garde_l_ancien_rapport(config, entrepot):
    """Tout ou rien : une catégorie invalide ne laisse ni demi-rapport ni rapport effacé."""
    ecrire_rapport(config, entrepot, rapport_complet())
    tables = ("or_rapport_quotidien", "or_rapport_categorie")
    etat_avant = instantane(entrepot, *tables)
    bonne = rapport_complet()["categories"][0]
    corrompu = {
        **rapport_complet(),
        "nb_fraudes": 99,
        "categories": [bonne, {**bonne, "categorie": "autre", "nb_paiements": None}],
    }
    with pytest.raises(IntegrityError) as erreur:
        ecrire_rapport(config, entrepot, corrompu)
    assert (
        str(erreur.value.orig) == "NOT NULL constraint failed: or_rapport_categorie.nb_paiements"
    )
    assert instantane(entrepot, *tables) == etat_avant


def test_un_dossier_de_rapport_inutilisable_n_ecrit_rien_en_base_et_garde_le_fichier(
    config, entrepot
):
    """Si le dossier ne peut pas être créé, la couche or n'annonce pas un rapport fantôme."""
    config.rapport.dossier.parent.mkdir(parents=True, exist_ok=True)
    config.rapport.dossier.write_text("je suis un fichier, pas un dossier", encoding="utf-8")
    etat_avant = instantane(entrepot)
    with pytest.raises(FileExistsError):
        ecrire_rapport(config, entrepot, rapport_complet())
    assert instantane(entrepot) == etat_avant
    assert (
        config.rapport.dossier.read_text(encoding="utf-8") == "je suis un fichier, pas un dossier"
    )


# --------------------------------------------------------------------------------------------
# notifier_rapport
# --------------------------------------------------------------------------------------------
def test_notifier_rapport_ne_modifie_pas_le_rapport_et_envoie_sujet_corps_et_donnees_exacts():
    """Le résumé du matin dit combien de paiements, de fraudes, de rejets, et où lire le détail."""
    canal = CanalEnregistreur()
    rapport = {
        "jour": JOUR,
        "nb_paiements": 5,
        "montant_total": 1175.0,
        "nb_fraudes": 2,
        "montant_fraudes": 1000.0,
        "taux_fraude_nb": 0.4,
        "nb_rejets": 3,
    }
    rapport_avant = copy.deepcopy(rapport)
    resultat = notifier_rapport(Notificateur([canal]), rapport, {"html": "a.html", "csv": "b.csv"})
    assert rapport == rapport_avant
    assert resultat == {"enregistreur": True}
    (notification,) = canal.recus
    assert notification.type == "rapport"
    assert notification.sujet == "[Rapport du matin] 2026-10-09 : 2 fraude(s) sur 5 paiements"
    assert notification.corps == (
        "Rapport du matin pour le 2026-10-09 (jour UTC).\n"
        "- Paiements : 5 pour 1175.00\n"
        "- Fraudes détectées : 2 pour 1000.00 (40.000 % des paiements)\n"
        "- Lignes rejetées (DLQ) : 3\n"
        "- Rapport HTML : a.html\n"
        "- Liste CSV : b.csv"
    )
    assert notification.donnees == {
        "jour": JOUR,
        "nb_paiements": 5,
        "nb_fraudes": 2,
        "montant_fraudes": 1000.0,
    }


# --------------------------------------------------------------------------------------------
# verifier_delai_rapport
# --------------------------------------------------------------------------------------------
def test_le_delai_est_respecte_pile_a_la_limite_et_un_melange_naif_aware_est_refuse():
    """SLA : livré à l'heure dite + délai = respecté ; une seconde de plus = en retard."""
    prevu = datetime(2026, 10, 10, 5, 0, tzinfo=UTC)
    deux_heures = timezone(timedelta(hours=2))
    cas = [
        # (livraison, délai en minutes, attendu)
        (prevu - timedelta(minutes=10), 60, (True, 0.0)),
        (prevu, 60, (True, 0.0)),
        (prevu + timedelta(minutes=59, seconds=59), 60, (True, 0.0)),
        (prevu + timedelta(minutes=60), 60, (True, 0.0)),  # pile à la limite : respecté
        (prevu + timedelta(minutes=60, seconds=1), 60, (False, 1 / 60)),
        (prevu + timedelta(minutes=75), 60, (False, 15.0)),
        (prevu + timedelta(minutes=45), 30, (False, 15.0)),  # le délai compte en minutes
        (prevu + timedelta(minutes=30), 30, (True, 0.0)),
        (prevu, 0, (True, 0.0)),
        (prevu + timedelta(seconds=1), 0, (False, 1 / 60)),
        (datetime(2026, 10, 10, 7, 0, tzinfo=deux_heures), 0, (True, 0.0)),  # même instant
        (datetime(2026, 10, 10, 7, 30, tzinfo=deux_heures), 0, (False, 30.0)),
    ]
    for livraison, delai, attendu in cas:
        assert verifier_delai_rapport(livraison, prevu, delai) == attendu, (livraison, delai)
    with pytest.raises(TypeError) as erreur:
        verifier_delai_rapport(datetime(2026, 10, 10, 6, 0), prevu, 60)
    assert str(erreur.value) == "can't subtract offset-naive and offset-aware datetimes"


# --------------------------------------------------------------------------------------------
# produire_rapport
# --------------------------------------------------------------------------------------------
def test_produire_rapport_sur_source_muette_n_ecrit_ni_fichier_ni_ligne_et_garde_l_etat(
    config, entrepot
):
    """Pas de transaction : ni fichier, ni table or, ni lignage ne doivent être créés."""
    etat_avant = instantane(entrepot)
    with pytest.raises(DonneesAbsentes) as erreur:
        produire_rapport(config, entrepot, JOUR)
    assert str(erreur.value) == MESSAGE_MUET
    assert instantane(entrepot) == etat_avant
    assert not config.rapport.dossier.exists()


def test_produire_rapport_sur_source_muette_n_envoie_aucune_notification_ni_sla(
    config, entrepot, evenements
):
    """Une source muette ne doit pas produire un faux « rapport du matin » dans les boîtes."""
    canal = CanalEnregistreur()
    with pytest.raises(DonneesAbsentes):
        produire_rapport(
            config,
            entrepot,
            JOUR,
            Notificateur([canal]),
            planifie_a=datetime(2020, 1, 1, tzinfo=UTC),
        )
    assert canal.recus == []
    assert [nom for _, nom, _ in evenements.tous()] == ["donnees_absentes"]


@pytest.mark.parametrize("jour", ["10/10/2026", "2026-02-30", ""])
def test_produire_rapport_refuse_un_jour_invalide_sans_rien_ecrire(config, entrepot, jour):
    """Un jour mal écrit est refusé en premier ; la base et le disque restent comme avant."""
    inserer(entrepot, [ligne_argent(1)])
    canal = CanalEnregistreur()
    etat_avant = instantane(entrepot)
    with pytest.raises(ValueError) as erreur:
        produire_rapport(config, entrepot, jour, Notificateur([canal]))
    assert str(erreur.value) == message_de_date(jour)
    assert instantane(entrepot) == etat_avant
    assert not config.rapport.dossier.exists()
    assert canal.recus == []


def test_un_echec_d_ecriture_n_envoie_aucune_notification_et_ne_laisse_pas_de_lignage(
    config, entrepot, evenements
):
    """On ne notifie qu'un rapport réellement écrit : le disque en panne arrête la chaîne."""
    inserer(entrepot, [ligne_argent(1)])
    config.rapport.dossier.parent.mkdir(parents=True, exist_ok=True)
    config.rapport.dossier.write_text("occupé", encoding="utf-8")
    canal = CanalEnregistreur()
    etat_avant = instantane(entrepot)
    with pytest.raises(FileExistsError):
        produire_rapport(config, entrepot, JOUR, Notificateur([canal]))
    assert instantane(entrepot) == etat_avant
    assert canal.recus == []
    assert evenements.nommes("rapport_produit") == []


def test_produire_rapport_deux_fois_laisse_l_etat_inchange_et_ajoute_un_lignage_par_passage(
    config, entrepot, evenements, horloge_et_instant_fixes
):
    """Idempotence : le 2e passage rend les mêmes tables or ; le lignage trace chaque passage."""
    charger_journee(entrepot)
    canal = CanalEnregistreur()
    notificateur = Notificateur([canal])
    resultat = produire_rapport(config, entrepot, JOUR, notificateur)
    chemins = {
        "html": str(config.rapport.dossier / "rapport_2026-10-09.html"),
        "csv": str(config.rapport.dossier / "fraudes_2026-10-09.csv"),
    }
    assert resultat == {**calculer_rapport(entrepot, JOUR), "chemins": chemins}
    assert evenements.nommes("rapport_produit") == [
        ("INFO", {"jour": JOUR, "paiements": 10, "fraudes": 6, **chemins})
    ]
    assert evenements.nommes("sla_depasse") == []
    (lignage,) = entrepot.requete("SELECT * FROM meta_lignage")
    assert {cle: valeur for cle, valeur in lignage.items() if cle not in ("id", "fin")} == {
        "etape": "rapport_du_matin",
        "entree": "argent_transactions,meta_rejets",
        "sortie": "or_rapport_quotidien,or_rapport_categorie,fichiers",
        "nb_entrees": 10,
        "nb_sorties": 1,
        "nb_rejets": 3,
        "debut": INSTANT_FIXE,
        "version_regles": VERSION_REGLES,
        "version_modele": "v1,v2",
        "details": json.dumps({"jour": JOUR, **chemins}, ensure_ascii=False),
    }
    assert [n.type for n in canal.recus] == ["rapport"]
    assert canal.recus[0].sujet == "[Rapport du matin] 2026-10-09 : 6 fraude(s) sur 10 paiements"
    tables_stables = tuple(t for t in TOUTES_LES_TABLES if t != "meta_lignage")
    etat_initial = instantane(entrepot, *tables_stables)
    produire_rapport(config, entrepot, JOUR, notificateur)
    assert instantane(entrepot, *tables_stables) == etat_initial
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage") == 2
    assert [n.type for n in canal.recus] == ["rapport", "rapport"]


def test_une_version_de_modele_vide_donne_un_lignage_sans_version_et_une_relance_inchangee(
    config, entrepot, horloge_et_instant_fixes
):
    """Pas de version connue : le lignage garde NULL ; relancer ne change pas les tables or."""
    sans_version = ligne_argent(1)
    sans_version["version_modele"] = ""
    inserer(entrepot, [sans_version])
    produire_rapport(config, entrepot, JOUR)
    assert entrepot.scalaire("SELECT version_modele FROM meta_lignage") is None
    tables_or = ("or_rapport_quotidien", "or_rapport_categorie")
    etat_initial = instantane(entrepot, *tables_or)
    produire_rapport(config, entrepot, JOUR)
    assert instantane(entrepot, *tables_or) == etat_initial
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage WHERE version_modele IS NULL") == 2


@pytest.mark.parametrize(
    ("sla_minutes", "age_planification_s", "retard_attendu"),
    [
        (60, 3599, None),
        (60, 3600, None),  # pile à la limite : pas d'alerte
        (60, 3603.7, 0.1),  # 3,7 s de retard = 0,0617 min, arrondi à une décimale
        (60, 3660, 1.0),
        (60, 4500, 15.0),
        (30, 1800, None),
        (30, 1860, 1.0),
    ],
)
def test_produire_rapport_signale_sla_depasse_seulement_si_le_rapport_est_en_retard(
    config,
    entrepot,
    evenements,
    horloge_et_instant_fixes,
    sla_minutes,
    age_planification_s,
    retard_attendu,
):
    """L'alerte ``sla_depasse`` ne part qu'au-delà du délai, avec le retard exact en minutes."""
    inserer(entrepot, [ligne_argent(1)])
    config_sla = replace(config, rapport=replace(config.rapport, sla_minutes=sla_minutes))
    produire_rapport(config_sla, entrepot, JOUR)  # référence : sans surveillance du délai
    tables_or = ("or_rapport_quotidien", "or_rapport_categorie")
    etat_initial = instantane(entrepot, *tables_or)
    evenements.enregistrements.clear()
    planifie = HorlogeFixe.now(UTC) - timedelta(seconds=age_planification_s)
    resultat = produire_rapport(config_sla, entrepot, JOUR, planifie_a=planifie)
    assert resultat["nb_paiements"] == 1
    assert instantane(entrepot, *tables_or) == etat_initial  # le retard ne change pas le rapport
    attendu = (
        []
        if retard_attendu is None
        else [("WARNING", {"sla": "rapport_du_matin", "retard_min": retard_attendu})]
    )
    assert evenements.nommes("sla_depasse") == attendu
    assert [niveau for niveau, _ in evenements.nommes("rapport_produit")] == ["INFO"]


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
def test_main_retourne_3_sur_source_muette_puis_0_quand_les_donnees_arrivent(
    config, entrepot, capsys, evenements, journal_enregistre, fermetures
):
    """Code 3 = source muette (alerte pour l'orchestrateur) ; rien n'est écrit dans ce cas."""
    argv = ["--jour", JOUR, "--sans-notification"]
    code_sortie = module_rapport.main(argv)
    assert code_sortie == 3
    sortie = capsys.readouterr()
    assert sortie.out == ""
    assert sortie.err == f"Rapport impossible : {MESSAGE_MUET}\n"
    assert not config.rapport.dossier.exists()
    assert fermetures == ["sqlite"]
    inserer(entrepot, [ligne_argent(1), ligne_argent(2, fraude=1)])
    assert module_rapport.main(argv) == 0
    sortie = capsys.readouterr()
    html = config.rapport.dossier / "rapport_2026-10-09.html"
    csv_ = config.rapport.dossier / "fraudes_2026-10-09.csv"
    assert sortie.out == (
        f"Rapport {JOUR} : 2 paiements, 1 fraudes.\nHTML : {html}\nCSV  : {csv_}\n"
    )
    assert sortie.err == ""
    assert fermetures == ["sqlite", "sqlite"]


def test_main_sur_source_muette_avec_notification_n_envoie_rien_et_ferme_l_entrepot(
    config, entrepot, capsys, evenements, journal_enregistre, fermetures
):
    """Même quand les canaux sont actifs, une source muette n'écrit aucun message d'alerte."""
    code_sortie = module_rapport.main(["--jour", JOUR])
    assert code_sortie == 3
    assert capsys.readouterr().err == f"Rapport impossible : {MESSAGE_MUET}\n"
    assert not config.notification.fichier_alertes.exists()
    assert not config.rapport.dossier.exists()
    assert fermetures == ["sqlite"]


def test_main_refuse_un_argument_inconnu_avec_le_code_2_et_ne_cree_aucune_base(
    config, capsys, journal_enregistre
):
    """Une faute de frappe dans la commande n'ouvre ni base ni journal."""
    with pytest.raises(SystemExit) as sortie:
        module_rapport.main(["--inconnu"])
    assert sortie.value.code == 2
    assert "error: unrecognized arguments: --inconnu" in capsys.readouterr().err
    assert not config.racine_donnees.exists()
    assert journal_enregistre == []


def test_main_refuse_jour_sans_valeur_avec_le_code_2_et_ne_cree_aucune_base(
    config, capsys, journal_enregistre
):
    """``--jour`` sans date est une erreur d'usage, détectée avant toute écriture."""
    with pytest.raises(SystemExit) as sortie:
        module_rapport.main(["--jour"])
    assert sortie.value.code == 2
    assert "error: argument --jour: expected one argument" in capsys.readouterr().err
    assert not config.racine_donnees.exists()


def test_main_aide_liste_les_options_et_quitte_avec_le_code_0(capsys, monkeypatch):
    """``--help`` documente la commande et chaque option, puis s'arrête sans rien exécuter."""
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as sortie:
        module_rapport.main(["--help"])
    assert sortie.value.code == 0
    aide = capsys.readouterr().out
    assert "Produit le rapport du matin pour un jour." in aide
    assert "--jour JOUR" in aide
    assert "AAAA-MM-JJ (défaut : la veille, en UTC)" in aide
    assert "--sans-notification" in aide


@pytest.mark.parametrize(
    ("argv", "jour_attendu", "notifie"),
    [
        (["--sans-notification"], "2026-10-09", False),  # défaut : la veille de 07 h 20 UTC
        ([], "2026-10-09", True),
        (["--jour", VEILLE, "--sans-notification"], "2026-10-08", False),
        (["--jour", "2026-10-08"], "2026-10-08", True),
    ],
)
def test_main_utilise_la_veille_par_defaut_et_notifie_sauf_avec_sans_notification(
    config,
    entrepot,
    capsys,
    evenements,
    journal_enregistre,
    horloge_et_instant_fixes,
    argv,
    jour_attendu,
    notifie,
):
    """``--jour`` impose la date, ``--sans-notification`` coupe les canaux, le journal est réglé"""
    inserer(entrepot, [ligne_argent(1), ligne_argent(2, jour=VEILLE, fraude=1)])
    config.rapport.dossier.mkdir(parents=True)
    rapport_ancien = config.rapport.dossier / "rapport_2020-01-01.html"
    rapport_ancien.write_text("rapport d'un autre jour", encoding="utf-8")
    contenu_ancien = rapport_ancien.read_bytes()
    assert module_rapport.main(argv) == 0
    assert rapport_ancien.read_bytes() == contenu_ancien
    jour_pris = capsys.readouterr().out.splitlines()[0]
    assert (
        jour_pris
        == f"Rapport {jour_attendu} : 1 paiements, {int(jour_attendu == VEILLE)} fraudes."
    )
    assert journal_enregistre == [("INFO", "lisible", config.journal.fichier)]
    if notifie:
        lignes = config.notification.fichier_alertes.read_text(encoding="utf-8").splitlines()
        notifications = [json.loads(ligne) for ligne in lignes]
        assert [n["type"] for n in notifications] == ["rapport"]
        assert notifications[0]["sujet"].startswith(f"[Rapport du matin] {jour_attendu} : ")
    else:
        assert not config.notification.fichier_alertes.exists()


# --------------------------------------------------------------------------------------------
# Précisions : texte exact des requêtes et jointure des lignes (relevé par la mutation)
# --------------------------------------------------------------------------------------------
REQUETE_AGREGATS = (
    "SELECT COUNT(*) AS nb, COALESCE(SUM(montant), 0) AS total, "
    "COALESCE(SUM(est_fraude), 0) AS nb_fraudes, "
    "COALESCE(SUM(CASE WHEN est_fraude = 1 THEN montant ELSE 0 END), 0) AS montant_fraudes, "
    "COALESCE(SUM(CASE WHEN etiquette_source IS NOT NULL THEN 1 ELSE 0 END), 0) "
    "AS nb_etiquetees, "
    "COALESCE(SUM(CASE WHEN etiquette_source = 1 AND est_fraude = 1 THEN 1 ELSE 0 END), 0) "
    "AS vp, "
    "COALESCE(SUM(CASE WHEN etiquette_source = 1 THEN 1 ELSE 0 END), 0) AS reelles "
    "FROM argent_transactions WHERE date_transaction = ?"
)
REQUETE_REJETS = "SELECT COUNT(*) FROM meta_rejets WHERE date_evenement = ?"
REQUETE_CATEGORIES = (
    "SELECT categorie, COUNT(*) AS nb_paiements, SUM(est_fraude) AS nb_fraudes, "
    "SUM(montant) AS montant_total, "
    "SUM(CASE WHEN est_fraude = 1 THEN montant ELSE 0 END) AS montant_fraudes "
    "FROM argent_transactions WHERE date_transaction = ? GROUP BY categorie "
    "ORDER BY nb_fraudes DESC, montant_fraudes DESC, categorie"
)
REQUETE_FRAUDES = (
    "SELECT horodatage_utc, trans_num, carte_fin, commercant, categorie, etat, montant, "
    "score, etiquette_source FROM argent_transactions "
    "WHERE date_transaction = ? AND est_fraude = 1 "
    "ORDER BY horodatage_s, trans_num"
)
REQUETE_MODELES = (
    "SELECT DISTINCT version_modele FROM argent_transactions WHERE date_transaction = ? "
    "ORDER BY version_modele"
)


def test_le_calcul_du_rapport_envoie_les_six_requetes_documentees_dans_l_ordre(
    entrepot, instructions_sql
):
    """Agrégats du jour et de la veille, rejets, catégories, fraudes, versions de modèle."""
    calculer_rapport(entrepot, JOUR)
    assert instructions_sql == [
        REQUETE_AGREGATS,
        REQUETE_AGREGATS,
        REQUETE_REJETS,
        REQUETE_CATEGORIES,
        REQUETE_FRAUDES,
        REQUETE_MODELES,
    ]


def test_verifier_les_donnees_du_jour_envoie_le_comptage_documente(entrepot, instructions_sql):
    """Une seule requête : le nombre de transactions de la couche argent pour ce jour."""
    verifier_donnees_du_jour(entrepot, JOUR, minimum=0)
    assert instructions_sql == [
        "SELECT COUNT(*) FROM argent_transactions WHERE date_transaction = ?"
    ]


def test_ecrire_le_rapport_envoie_la_suppression_les_insertions_et_la_mise_a_jour_documentees(
    config, entrepot, instructions_sql
):
    """Remplacement du jour : suppression des catégories, une insertion par catégorie, upsert."""
    ecrire_rapport(config, entrepot, rapport_complet())
    colonnes = (
        "jour, nb_paiements, nb_fraudes, montant_total, montant_fraudes, taux_fraude_nb, "
        "taux_fraude_montant, nb_rejets, nb_etiquetees, nb_vrais_positifs, nb_fraudes_reelles, "
        "genere_le, version_regles, chemin_html, chemin_csv"
    )
    remplacees = (
        "nb_paiements, nb_fraudes, montant_total, montant_fraudes, taux_fraude_nb, "
        "taux_fraude_montant, nb_rejets, nb_etiquetees, nb_vrais_positifs, nb_fraudes_reelles, "
        "genere_le, version_regles, chemin_html, chemin_csv"
    )
    insertion_categorie = (
        "INSERT INTO or_rapport_categorie (jour, categorie, nb_paiements, nb_fraudes, "
        "montant_total, montant_fraudes) VALUES (?, ?, ?, ?, ?, ?)"
    )
    mise_a_jour = ", ".join(f"{c} = excluded.{c}" for c in remplacees.split(", "))
    assert instructions_sql == [
        "DELETE FROM or_rapport_categorie WHERE jour = ?",
        insertion_categorie,
        insertion_categorie,
        f"INSERT INTO or_rapport_quotidien ({colonnes}) VALUES ({', '.join(['?'] * 15)}) "
        f"ON CONFLICT (jour) DO UPDATE SET {mise_a_jour}",
    ]


def test_les_lignes_de_fraude_du_rapport_html_se_suivent_sans_separateur():
    """Deux fraudes : leurs lignes de tableau sont collées l'une à l'autre, rien entre elles."""
    rapport = rapport_complet()
    premiere = {**rapport["fraudes"][0], "commercant": "Alpha", "carte_fin": "1111"}
    seconde = {
        **premiere,
        "horodatage_utc": "2026-10-09T11:02:00+00:00",
        "commercant": "Beta",
        "carte_fin": "2222",
        "montant": 12.5,
        "score": 0.5,
    }
    rapport["fraudes"] = [premiere, seconde]
    page = rendre_html(rapport)

    def ligne(f: dict[str, Any]) -> str:
        return (
            f"<tr><td>{f['horodatage_utc']}</td><td class='m'>{f['trans_num']}</td>"
            f"<td>****{f['carte_fin']}</td><td>{f['commercant']}</td>"
            f"<td>{f['categorie']}</td><td class='n'>{float(f['montant']):.2f}</td>"
            f"<td class='n'>{float(f['score']):.3f}</td></tr>"
        )

    assert ligne(premiere) + ligne(seconde) in page
