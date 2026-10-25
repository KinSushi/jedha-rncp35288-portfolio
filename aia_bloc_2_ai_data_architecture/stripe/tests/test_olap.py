# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Entrepôt en étoile : les faits reflètent l'OLTP et les agrégats reflètent les faits.

Pourquoi : un tableau de bord faux est pire qu'un tableau de bord absent. Ces tests vérifient les
trois invariants dont dépend toute l'analytique : aucun paiement perdu ou créé par la
transformation, montants convertis au taux du jour, agrégats égaux à la somme des faits.

Chaque invariant est écrit comme une règle `exiger_...` qui lève `AssertionError` quand il est
violé. Le test l'applique à l'entrepôt sain, puis il ABÎME volontairement l'entrepôt (un fait
supprimé, un montant doublé, un agrégat truqué...) et vérifie que la règle le refuse : un
contrôle qui ne sait rien refuser serait toujours vert.
"""

from __future__ import annotations

import duckdb
import pytest
from sqlalchemy import text

from stripe_demo import olap
from stripe_demo.pipeline import RACINE, requetes_sql


def _un(entrepot, sql: str, *parametres):
    return entrepot.execute(sql, list(parametres)).fetchone()[0]


# ---------------------------------------------------------------- règles


def exiger_autant_de_faits_que_de_paiements(entrepot, nb_paiements_oltp: int) -> None:
    assert _un(entrepot, "SELECT count(*) FROM faits_paiements") == nb_paiements_oltp


def exiger_montants_au_taux_du_jour(entrepot, lignes) -> None:
    """Le montant en USD d'un fait = montant en unités mineures / 100 × taux du jour."""
    for id_paiement, montant_mineur, taux in lignes:
        fait = _un(entrepot, "SELECT montant_usd FROM faits_paiements WHERE id_paiement = ?",
                   id_paiement)
        assert fait == pytest.approx(montant_mineur / 100.0 * float(taux), rel=1e-9), id_paiement


def exiger_agregats_quotidiens_egaux_aux_faits(entrepot) -> None:
    chiffre_faits = _un(entrepot, "SELECT sum(montant_usd) FROM faits_paiements WHERE est_reussi")
    chiffre_agr = _un(entrepot, "SELECT sum(chiffre_usd) FROM agr_chiffre_quotidien")
    assert chiffre_agr == pytest.approx(chiffre_faits, rel=1e-9)
    assert _un(entrepot, "SELECT sum(nb_paiements) FROM agr_chiffre_quotidien") == _un(
        entrepot, "SELECT count(*) FROM faits_paiements")


def exiger_chaque_ligne_d_agregat_egale_son_recalcul(entrepot) -> None:
    """Pas seulement le total : chaque couple (jour, marchand) doit être exact."""
    ecarts = _un(entrepot, """
        SELECT count(*) FROM agr_chiffre_quotidien a
        JOIN (SELECT d.date, f.cle_marchand, count(*) AS n,
                     coalesce(sum(f.montant_usd) FILTER (WHERE f.est_reussi), 0) AS ca
              FROM faits_paiements f JOIN dim_date d USING (cle_date)
              GROUP BY d.date, f.cle_marchand) r
          ON r.date = a.date AND r.cle_marchand = a.cle_marchand
        WHERE a.nb_paiements <> r.n OR abs(coalesce(a.chiffre_usd, 0) - r.ca) > 1e-6
    """)
    assert ecarts == 0, f"{ecarts} ligne(s) d'agrégat différente(s) du recalcul"


def exiger_agregat_fraude_par_pays_exact(entrepot) -> None:
    assert _un(entrepot, "SELECT sum(nb_paiements) FROM agr_fraude_pays") == _un(
        entrepot, "SELECT count(*) FROM faits_paiements")
    ecarts = _un(entrepot, """
        SELECT count(*) FROM agr_fraude_pays a
        JOIN (SELECT pays_ip, avg(est_conteste::INTEGER) AS t FROM faits_paiements
              GROUP BY pays_ip) r ON r.pays_ip IS NOT DISTINCT FROM a.pays_ip
        WHERE abs(a.taux_litige - r.t) > 1e-12""")
    assert ecarts == 0


def exiger_agregats_et_dates_alignes_sur_les_faits(entrepot) -> None:
    assert _un(entrepot, "SELECT sum(nb_paiements) FROM agr_chiffre_quotidien") == _un(
        entrepot, "SELECT count(*) FROM faits_paiements")
    # Régression : un jour nouveau reçu par la CDC doit entrer dans la dimension de date.
    assert _un(entrepot, "SELECT count(*) FROM faits_paiements WHERE cle_date NOT IN "
                         "(SELECT cle_date FROM dim_date)") == 0


def exiger_dimensions_coherentes(entrepot) -> None:
    assert _un(entrepot, "SELECT count(*) FROM dim_marchand WHERE NOT est_courant") == 0
    assert _un(entrepot, "SELECT count(*) FROM dim_marchand WHERE valide_au IS NOT NULL") == 0
    segments = {r[0] for r in entrepot.execute("SELECT DISTINCT segment FROM dim_client")
                .fetchall()}
    assert segments <= {"fidele", "regulier", "occasionnel"}, segments
    # Intégrité référentielle de l'étoile (DuckDB ne la vérifie pas : on la contrôle ici).
    orphelins = _un(entrepot, """
        SELECT count(*) FROM faits_paiements f
        WHERE f.cle_date NOT IN (SELECT cle_date FROM dim_date)
           OR f.cle_marchand NOT IN (SELECT cle_marchand FROM dim_marchand)
           OR f.cle_client NOT IN (SELECT cle_client FROM dim_client)
           OR f.cle_moyen NOT IN (SELECT cle_moyen FROM dim_moyen)""")
    assert orphelins == 0


def exiger_calendrier_exact(entrepot) -> None:
    lignes = entrepot.execute(
        "SELECT cle_date, date, annee, mois, jour, jour_semaine, est_weekend FROM dim_date"
    ).fetchall()
    assert lignes
    for cle, jour_date, annee, mois, jour, jour_semaine, est_weekend in lignes:
        assert cle == annee * 10_000 + mois * 100 + jour
        assert (jour_date.year, jour_date.month, jour_date.day) == (annee, mois, jour)
        assert est_weekend == (jour_semaine >= 6)


def exiger_requetes_qui_rendent_des_lignes(entrepot, requetes) -> None:
    for question, sql in requetes:
        resultat = entrepot.execute(sql).df()
        assert len(resultat) > 0, question


# ---------------------------------------------------------------- les tests


def test_aucun_paiement_perdu_ni_cree(mini_demo):
    with mini_demo.moteur.connect() as cx:
        oltp_n = cx.execute(text("SELECT count(*) FROM paiements")).scalar_one()
    exiger_autant_de_faits_que_de_paiements(mini_demo.entrepot, oltp_n)

    with pytest.raises(AssertionError):  # un paiement de trop attendu : un fait aurait été perdu
        exiger_autant_de_faits_que_de_paiements(mini_demo.entrepot, oltp_n + 1)
    mini_demo.entrepot.execute("DELETE FROM faits_paiements WHERE id_paiement = 1")
    with pytest.raises(AssertionError):  # un fait supprimé
        exiger_autant_de_faits_que_de_paiements(mini_demo.entrepot, oltp_n)


def test_les_montants_sont_convertis_au_taux_du_jour(mini_demo):
    """Le montant en USD d'un fait = montant en unités mineures / 100 × taux du jour."""
    echantillon = 25
    with mini_demo.moteur.connect() as cx:
        lignes = cx.execute(text(
            "SELECT p.id_paiement, p.montant_mineur, t.taux_vers_usd "
            "FROM paiements p JOIN taux_change t ON t.code_devise = p.code_devise "
            f"AND t.date_taux = date(p.cree_le) ORDER BY p.id_paiement LIMIT {echantillon}")
        ).fetchall()
    assert len(lignes) == echantillon
    exiger_montants_au_taux_du_jour(mini_demo.entrepot, lignes)

    premier = lignes[0][0]
    mini_demo.entrepot.execute(
        "UPDATE faits_paiements SET montant_usd = montant_usd * 2 WHERE id_paiement = ?",
        [premier])
    with pytest.raises(AssertionError, match=str(premier)):  # un montant mal converti
        exiger_montants_au_taux_du_jour(mini_demo.entrepot, lignes)


def test_les_agregats_quotidiens_egalent_la_somme_des_faits(mini_demo):
    e = mini_demo.entrepot
    exiger_agregats_quotidiens_egaux_aux_faits(e)

    e.execute("UPDATE agr_chiffre_quotidien SET nb_paiements = nb_paiements + 1 "
              "WHERE date = (SELECT min(date) FROM agr_chiffre_quotidien)")
    with pytest.raises(AssertionError):  # un paiement compté en trop dans les agrégats
        exiger_agregats_quotidiens_egaux_aux_faits(e)


def test_chaque_ligne_d_agregat_egale_son_recalcul(mini_demo):
    """Pas seulement le total : chaque couple (jour, marchand) doit être exact."""
    e = mini_demo.entrepot
    exiger_chaque_ligne_d_agregat_egale_son_recalcul(e)

    # Un chiffre d'affaires gonflé sur un jour : le recalcul ligne par ligne le dénonce.
    e.execute("UPDATE agr_chiffre_quotidien SET chiffre_usd = coalesce(chiffre_usd, 0) + 5 "
              "WHERE date = (SELECT min(date) FROM agr_chiffre_quotidien)")
    with pytest.raises(AssertionError, match="différente"):
        exiger_chaque_ligne_d_agregat_egale_son_recalcul(e)


def test_agregat_fraude_par_pays(mini_demo):
    e = mini_demo.entrepot
    exiger_agregat_fraude_par_pays_exact(e)

    e.execute("UPDATE agr_fraude_pays SET taux_litige = taux_litige + 0.5 "
              "WHERE pays_ip = (SELECT min(pays_ip) FROM agr_fraude_pays)")
    with pytest.raises(AssertionError):  # un taux de litige truqué
        exiger_agregat_fraude_par_pays_exact(e)
    e.execute("UPDATE agr_fraude_pays SET nb_paiements = nb_paiements + 1 "
              "WHERE pays_ip = (SELECT min(pays_ip) FROM agr_fraude_pays)")
    with pytest.raises(AssertionError):  # un compte de paiements truqué
        exiger_agregat_fraude_par_pays_exact(e)


def test_les_agregats_suivent_la_cdc(mini_demo):
    """Après un micro-lot CDC, les agrégats sont recalculés et restent égaux aux faits."""
    mini_demo.ajouter_paiements(60, graine=9100)
    olap.appliquer_changements(mini_demo.entrepot, str(mini_demo.chemin_oltp),
                               mini_demo.position)
    e = mini_demo.entrepot
    exiger_agregats_et_dates_alignes_sur_les_faits(e)

    # Sans le recalcul des agrégats, un lot de plus de changements les laisserait en retard.
    mini_demo.ajouter_paiements(10, graine=9101)
    e.execute("DELETE FROM agr_chiffre_quotidien")
    with pytest.raises(AssertionError):
        exiger_agregats_et_dates_alignes_sur_les_faits(e)


def test_dimensions_coherentes(mini_demo):
    e = mini_demo.entrepot
    exiger_dimensions_coherentes(e)

    e.execute("UPDATE dim_client SET segment = 'vip' WHERE cle_client = "
              "(SELECT min(cle_client) FROM dim_client)")
    with pytest.raises(AssertionError, match="vip"):  # un segment hors du vocabulaire
        exiger_dimensions_coherentes(e)
    e.execute("UPDATE dim_client SET segment = 'fidele'")
    e.execute("UPDATE dim_marchand SET est_courant = FALSE")
    with pytest.raises(AssertionError):  # un marchand qui n'est plus la version courante
        exiger_dimensions_coherentes(e)


def test_dim_date_decrit_bien_le_calendrier(mini_demo):
    e = mini_demo.entrepot
    exiger_calendrier_exact(e)

    e.execute("UPDATE dim_date SET est_weekend = NOT est_weekend WHERE cle_date = "
              "(SELECT min(cle_date) FROM dim_date)")
    with pytest.raises(AssertionError):  # un lundi déclaré week-end (ou l'inverse)
        exiger_calendrier_exact(e)
    e.execute("UPDATE dim_date SET est_weekend = NOT est_weekend WHERE cle_date = "
              "(SELECT min(cle_date) FROM dim_date)")
    exiger_calendrier_exact(e)  # la correction rétablit le calendrier
    vide = olap.connecter(":memory:")
    with pytest.raises(AssertionError):  # un calendrier vide ne prouve rien
        exiger_calendrier_exact(vide)


def test_toutes_les_requetes_du_fichier_sql_s_executent(mini_demo):
    """Les requêtes livrées (et reprises dans la documentation) tournent sur l'entrepôt."""
    requetes = requetes_sql(RACINE / "sql" / "requetes_olap.sql")
    assert len(requetes) >= 7
    exiger_requetes_qui_rendent_des_lignes(mini_demo.entrepot, requetes)

    with pytest.raises(AssertionError, match="Question vide"):  # une requête sans résultat
        exiger_requetes_qui_rendent_des_lignes(
            mini_demo.entrepot, [("Question vide", "SELECT 1 WHERE FALSE")])
    with pytest.raises(duckdb.Error):  # une requête fausse
        exiger_requetes_qui_rendent_des_lignes(
            mini_demo.entrepot, [("Table inconnue", "SELECT * FROM table_inconnue")])
