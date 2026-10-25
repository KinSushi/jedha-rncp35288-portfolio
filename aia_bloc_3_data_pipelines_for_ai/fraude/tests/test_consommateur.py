# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Consommateur : idempotence, atomicité, DLQ, boîte d'envoi, alertes (critères 4.2, 6.1)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from detection_fraude.consommateur import Consommateur
from detection_fraude.file_attente import FileSQLite
from detection_fraude.masquage import masquer
from detection_fraude.notification import Notificateur
from tests.helpers import MARQUEUR, CanalEnregistreur, ChargeurFactice, ligne_brute


@pytest.fixture
def file(config):
    f = FileSQLite(config.file.chemin_sqlite)
    yield f
    f.fermer()


@pytest.fixture
def canal():
    return CanalEnregistreur()


@pytest.fixture
def consommateur(config, file, entrepot, canal):
    return Consommateur(config, file, entrepot, ChargeurFactice(), Notificateur([canal]))


def publier(file, config, hacheur, *rangs, **surcharges):
    for rang in rangs:
        valeur = masquer(ligne_brute(rang, **surcharges), hacheur, source="jeu")
        file.publier(config.file.sujet_valides, valeur, cle=valeur["carte_hash"])


def compter(entrepot, table):
    return entrepot.scalaire(f"SELECT COUNT(*) FROM {table}")


def test_chaque_couche_recoit_les_transactions(config, file, entrepot, consommateur, hacheur):
    publier(file, config, hacheur, 1, 2, 3)
    bilan = consommateur.executer_une_fois(attente_s=0)
    assert (bilan.recus, bilan.nouveaux, bilan.fraudes) == (3, 3, 0)
    for table in ("bronze_transactions", "argent_transactions"):
        assert compter(entrepot, table) == 3
    (ligne,) = entrepot.requete(
        "SELECT * FROM argent_transactions WHERE trans_num = :t", t=ligne_brute(1)["trans_num"]
    )
    assert ligne["commercant"] == "Marchand 1"  # préfixe « fraud_ » retiré à l'argent
    assert ligne["version_modele"] == "factice-1" and ligne["score"] == 0.05
    (brute,) = entrepot.requete(
        "SELECT commercant FROM bronze_transactions WHERE trans_num = :t",
        t=ligne_brute(1)["trans_num"],
    )
    assert brute["commercant"] == "fraud_Marchand 1"  # le bronze garde la valeur reçue


def test_une_fraude_cree_une_alerte_envoyee_une_seule_fois(
    config, file, entrepot, consommateur, canal, hacheur
):
    publier(file, config, hacheur, 1)
    publier(file, config, hacheur, 2, amt=900.0)
    bilan = consommateur.executer_une_fois(attente_s=0)
    assert bilan.fraudes == 1
    (alerte,) = entrepot.requete("SELECT * FROM or_alertes_fraude")
    assert alerte["statut_notification"] == "envoyee" and alerte["tentatives"] == 1
    assert alerte["latence_notification_s"] is not None and alerte["latence_notification_s"] >= 0
    assert len(canal.recus) == 1
    notification = canal.recus[0]
    assert notification.type == "fraude" and "900.00" in notification.corps
    assert notification.donnees["carte_fin"] == alerte["carte_fin"]


def test_idempotence_meme_message_deux_fois(config, file, entrepot, consommateur, canal, hacheur):
    publier(file, config, hacheur, 7, amt=900.0)
    publier(file, config, hacheur, 7, amt=900.0)  # même trans_num publié deux fois
    consommateur.executer_une_fois(attente_s=0)
    assert compter(entrepot, "bronze_transactions") == 1
    assert compter(entrepot, "argent_transactions") == 1
    assert compter(entrepot, "or_alertes_fraude") == 1
    assert len(canal.recus) == 1


def test_idempotence_apres_relivraison_d_un_lot_deja_traite(
    config, file, entrepot, canal, hacheur
):
    """Panne entre le commit de la base et la validation de la file : le lot est relivré."""
    publier(file, config, hacheur, 1, 2)
    publier(file, config, hacheur, 3, amt=900.0)
    premier = Consommateur(config, file, entrepot, ChargeurFactice(), Notificateur([canal]))
    messages = file.lire(config.file.sujet_valides, config.file.groupe_consommateur, 100, 0)
    premier.traiter_lot(messages)
    premier.envoyer_alertes()
    autre_connexion = sqlite3.connect(str(config.file.chemin_sqlite), isolation_level=None)
    try:
        # simule la validation perdue : tout est relivré
        autre_connexion.execute("DELETE FROM positions")
    finally:
        autre_connexion.close()
    relivres = file.lire(config.file.sujet_valides, config.file.groupe_consommateur, 100, 0)
    assert [m.valeur["trans_num"] for m in relivres] == [
        ligne_brute(rang)["trans_num"] for rang in (1, 2, 3)
    ]  # les trois messages reviennent, dans l'ordre
    redemarre = Consommateur(config, file, entrepot, ChargeurFactice(), Notificateur([canal]))
    bilan = redemarre.traiter_lot(relivres)
    redemarre.envoyer_alertes()
    assert (bilan.nouveaux, bilan.doublons) == (0, 3)
    assert compter(entrepot, "bronze_transactions") == 3
    assert compter(entrepot, "or_alertes_fraude") == 1
    assert len(canal.recus) == 1  # pas de seconde notification


def test_atomicite_une_panne_d_ecriture_n_ecrit_rien_et_ne_valide_pas_la_file(
    config, file, entrepot, consommateur, hacheur, monkeypatch
):
    publier(file, config, hacheur, 1, 2, 3)

    def panne(*args, **kwargs):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(entrepot, "inserer_argent", panne)
    with pytest.raises(RuntimeError):
        consommateur.executer_une_fois(attente_s=0)
    assert compter(entrepot, "bronze_transactions") == 0  # annulé avec le reste du lot
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 3
    monkeypatch.undo()
    consommateur.executer_une_fois(attente_s=0)  # le lot est rejoué en entier
    assert compter(entrepot, "argent_transactions") == 3


def test_message_hors_contrat_vers_la_dlq_sans_bloquer_le_lot(
    config, file, entrepot, consommateur, hacheur
):
    publier(file, config, hacheur, 1)
    intrus = masquer(ligne_brute(2), hacheur, source="jeu")
    intrus["first"] = f"Prenom{MARQUEUR}"  # un champ sensible qui aurait échappé au masquage
    file.publier(config.file.sujet_valides, intrus)
    file.publier(config.file.sujet_valides, {"__illisible__": True})
    publier(file, config, hacheur, 3)
    bilan = consommateur.executer_une_fois(attente_s=0)
    assert (bilan.nouveaux, bilan.rejets) == (2, 2)
    rejets = file.lire(config.file.sujet_rejets, "controle", 100, 0)
    assert {r.valeur["etape"] for r in rejets} == {"consommateur"}
    # l'intrus garde son identifiant, la charge illisible n'en a aucun
    assert sorted(str(r.valeur["trans_num"]) for r in rejets) == sorted(
        [intrus["trans_num"], "None"]
    )
    assert MARQUEUR not in json.dumps(
        [r.valeur for r in rejets]
    )  # la DLQ ne recopie pas le message
    assert compter(entrepot, "meta_rejets") == 2  # archivée dans l'entrepôt
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 0


def test_les_rejets_du_producteur_sont_archives(config, file, entrepot, consommateur, hacheur):
    file.publier(
        config.file.sujet_rejets,
        {
            "etape": "producteur",
            "raison": "schema_invalide",
            "trans_num": None,
            "rejete_le": "2026-10-10T08:00:00+00:00",
            "erreurs": [{"champ": "amt", "code": "type_invalide", "detail": ""}],
            "charge_utile_masquee": {"champs_conserves": {"current_time": 1_608_548_400_000}},
        },
    )
    consommateur.executer_une_fois(attente_s=0)
    (rejet,) = entrepot.requete("SELECT * FROM meta_rejets")
    assert rejet["date_evenement"] == "2020-12-21"  # date de la transaction, pas du rejet
    assert json.loads(rejet["erreurs"])[0]["champ"] == "amt"


def test_boite_d_envoi_un_canal_en_panne_est_reessaye_sans_renvoyer_aux_autres(
    config, file, entrepot, hacheur
):
    stable, capricieux = CanalEnregistreur("stable"), CanalEnregistreur("capricieux", echecs=2)
    consommateur = Consommateur(
        config, file, entrepot, ChargeurFactice(), Notificateur([stable, capricieux])
    )
    publier(file, config, hacheur, 1, amt=900.0)
    consommateur.executer_une_fois(attente_s=0)
    (alerte,) = entrepot.requete("SELECT * FROM or_alertes_fraude")
    assert alerte["statut_notification"] == "a_envoyer" and alerte["canaux_ok"] == "stable"
    consommateur.envoyer_alertes()  # 2e tentative : le canal capricieux échoue encore
    consommateur.envoyer_alertes()  # 3e tentative : il réussit
    (alerte,) = entrepot.requete("SELECT * FROM or_alertes_fraude")
    assert alerte["statut_notification"] == "envoyee" and alerte["tentatives"] == 3
    assert len(stable.recus) == 1  # jamais renvoyé là où l'envoi avait réussi
    assert len(capricieux.recus) == 1


def test_apres_trop_d_echecs_l_alerte_passe_en_echec(config, file, entrepot, hacheur):
    mort = CanalEnregistreur("mort", echecs=10_000)
    consommateur = Consommateur(config, file, entrepot, ChargeurFactice(), Notificateur([mort]))
    publier(file, config, hacheur, 1, amt=900.0)
    for _ in range(config.notification.max_tentatives + 2):
        consommateur.executer_une_fois(attente_s=0)
    (alerte,) = entrepot.requete("SELECT * FROM or_alertes_fraude")
    assert alerte["statut_notification"] == "echec"
    assert alerte["tentatives"] == config.notification.max_tentatives


def test_depassement_de_sla_journalise(config, file, entrepot, canal, hacheur, caplog):
    consommateur = Consommateur(
        config,
        file,
        entrepot,
        ChargeurFactice(),
        Notificateur([canal]),
        horloge=lambda: (
            time.time() + 10_000
        ),  # notification « envoyée » 10 000 s après la détection
    )
    publier(file, config, hacheur, 1, amt=900.0)
    import logging

    logger = logging.getLogger("fraude")
    logger.propagate = True
    with caplog.at_level(logging.WARNING, logger="fraude"):
        consommateur.executer_une_fois(attente_s=0)
    logger.propagate = False
    assert any("sla_depasse" in r.getMessage() for r in caplog.records)


def test_les_caracteristiques_d_historique_traversent_les_lots(
    config, file, entrepot, consommateur, hacheur
):
    # Deux transactions de la même carte dans deux lots successifs.
    publier(file, config, hacheur, 1, cc_num=4_929_000_000_000_042, amt=20.0)
    consommateur.executer_une_fois(attente_s=0)
    publier(file, config, hacheur, 2, cc_num=4_929_000_000_000_042, amt=60.0)
    consommateur.executer_une_fois(attente_s=0)
    lignes = entrepot.requete(
        "SELECT nb_tx_24h, montant_relatif FROM argent_transactions ORDER BY horodatage_s"
    )
    assert (lignes[0]["nb_tx_24h"], lignes[1]["nb_tx_24h"]) == (0, 1)
    assert lignes[1]["montant_relatif"] == pytest.approx(3.0)


def test_apres_redemarrage_l_etat_est_reconstruit_depuis_le_bronze(
    config, file, entrepot, canal, hacheur
):
    avant = Consommateur(config, file, entrepot, ChargeurFactice(), Notificateur([canal]))
    publier(file, config, hacheur, 1, cc_num=4_929_000_000_000_042, amt=20.0)
    avant.executer_une_fois(attente_s=0)
    apres = Consommateur(
        config, file, entrepot, ChargeurFactice(), Notificateur([canal])
    )  # « redémarré »
    publier(file, config, hacheur, 2, cc_num=4_929_000_000_000_042, amt=60.0)
    apres.executer_une_fois(attente_s=0)
    derniere = entrepot.requete(
        "SELECT nb_tx_24h FROM argent_transactions ORDER BY horodatage_s DESC LIMIT 1"
    )
    assert derniere[0]["nb_tx_24h"] == 1


def test_lignage_ecrit_par_lot(config, file, entrepot, consommateur, hacheur):
    publier(file, config, hacheur, 1, 2)
    consommateur.executer_une_fois(attente_s=0)
    (ligne,) = entrepot.requete("SELECT * FROM meta_lignage WHERE etape = 'consommateur'")
    assert ligne["nb_entrees"] == 2 and ligne["nb_sorties"] == 2
    assert ligne["version_modele"] == "factice-1" and ligne["version_regles"]


def test_la_boucle_s_arrete_quand_la_file_est_vide(config, file, consommateur, hacheur):
    publier(file, config, hacheur, 1, 2, 3)
    assert consommateur.boucle(threading.Event(), jusqu_a_vide=True) == 3


def test_la_boucle_respecte_l_arret(consommateur, monkeypatch):
    def tour_interdit(*args, **options):
        raise AssertionError("arrêt déjà demandé : la boucle ne devait faire aucun tour")

    # Sans ce garde, une boucle qui ignorerait l'arrêt tournerait sans fin au lieu d'échouer.
    monkeypatch.setattr(consommateur, "executer_une_fois", tour_interdit)
    arret = threading.Event()
    arret.set()
    assert consommateur.boucle(arret) == 0


def test_etat_de_sante_ecrit(config, file, consommateur, hacheur):
    publier(file, config, hacheur, 1)
    consommateur.executer_une_fois(attente_s=0)
    assert (config.racine_donnees / "etat" / "consommateur.json").exists()


def test_la_boucle_verifie_le_modele_meme_sans_message(config, file, entrepot, canal):
    class ChargeurCompteur(ChargeurFactice):
        appels = 0

        def obtenir(self):
            type(self).appels += 1
            return super().obtenir()

    consommateur = Consommateur(config, file, entrepot, ChargeurCompteur(), Notificateur([canal]))
    consommateur.executer_une_fois(attente_s=0)  # file vide
    assert (
        ChargeurCompteur.appels >= 1
    )  # une promotion est vue dans la seconde, pas au prochain lot
    etat = json.loads((config.racine_donnees / "etat" / "consommateur.json").read_text("utf-8"))
    assert etat["modele"] == "factice-1"
