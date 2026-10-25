# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Retraitement de l'historique depuis le bronze (critère 2.3) : idempotent, sans renotifier."""

from __future__ import annotations

import pytest

from detection_fraude.consommateur import Consommateur
from detection_fraude.file_attente import FileSQLite
from detection_fraude.masquage import masquer
from detection_fraude.notification import Notificateur
from detection_fraude.retraitement import retraiter
from tests.helpers import CanalEnregistreur, ChargeurFactice, ModeleFactice, ligne_brute


class ModeleSeuilBas(ModeleFactice):
    """Nouvelle règle : le seuil de fraude passe de 500 à 100."""

    version = "factice-2"

    def noter(self, caracteristiques):
        import numpy as np

        return np.where(caracteristiques["montant"].to_numpy() > 100, 0.95, 0.05)


@pytest.fixture
def historique(config, entrepot, hacheur):
    """Trois jours de transactions traités avec l'ancienne règle (fraude si montant > 500)."""
    file = FileSQLite(config.file.chemin_sqlite)
    canal = CanalEnregistreur()
    consommateur = Consommateur(config, file, entrepot, ChargeurFactice(), Notificateur([canal]))
    montants = [20.0, 150.0, 700.0, 30.0, 220.0, 90.0]
    for rang, montant in enumerate(montants):
        ligne = ligne_brute(
            rang, amt=montant, current_time=1_608_508_800_000 + rang * 86_400_000 // 2
        )
        valeur = masquer(ligne, hacheur, source="jeu")
        file.publier(config.file.sujet_valides, valeur, cle=valeur["carte_hash"])
    consommateur.executer_une_fois(attente_s=0)
    file.fermer()
    return canal


def decisions(entrepot):
    return {
        r["trans_num"]: r["est_fraude"]
        for r in entrepot.requete("SELECT trans_num, est_fraude FROM argent_transactions")
    }


def test_etat_initial(entrepot, historique):
    assert sum(decisions(entrepot).values()) == 1
    assert len(historique.recus) == 1  # une seule notification, pour la fraude initiale


def test_nouvelle_regle_appliquee_au_passe_sans_renotifier(config, entrepot, historique):
    resultat = retraiter(config, entrepot, "2020-12-21", "2020-12-26", modele=ModeleSeuilBas())
    assert resultat["lignes"] == 6 and resultat["version_modele"] == "factice-2"
    assert resultat["decisions_modifiees"] == 2  # 150 et 220 deviennent des fraudes
    assert sum(decisions(entrepot).values()) == 3
    statuts = {
        r["trans_num"]: r["statut_notification"]
        for r in entrepot.requete("SELECT * FROM or_alertes_fraude")
    }
    assert sorted(statuts.values()) == ["envoyee", "rejouee", "rejouee"]
    # Les alertes « rejouées » ne repartent jamais : la boîte d'envoi ne les voit pas.
    assert entrepot.alertes_a_envoyer(max_tentatives=5) == []
    versions = {
        r["version_modele"]
        for r in entrepot.requete("SELECT version_modele FROM argent_transactions")
    }
    assert versions == {"factice-2"}


def test_le_retraitement_est_idempotent(config, entrepot, historique):
    premier = retraiter(config, entrepot, "2020-12-21", "2020-12-26", modele=ModeleSeuilBas())
    avant = decisions(entrepot)
    second = retraiter(config, entrepot, "2020-12-21", "2020-12-26", modele=ModeleSeuilBas())
    assert decisions(entrepot) == avant
    assert second["decisions_modifiees"] == 0 and premier["decisions_modifiees"] == 2
    assert entrepot.scalaire("SELECT COUNT(*) FROM argent_transactions") == 6
    assert entrepot.scalaire("SELECT COUNT(*) FROM or_alertes_fraude") == 3


def test_retraitement_sur_une_sous_periode(config, entrepot, historique):
    resultat = retraiter(config, entrepot, "2020-12-21", "2020-12-21", modele=ModeleSeuilBas())
    assert 0 < resultat["lignes"] < 6
    versions = {
        r["version_modele"]
        for r in entrepot.requete("SELECT version_modele FROM argent_transactions")
    }
    assert versions == {"factice-1", "factice-2"}  # seule la période demandée est recalculée


def test_lignage_du_retraitement(config, entrepot, historique):
    retraiter(config, entrepot, "2020-12-21", "2020-12-26", modele=ModeleSeuilBas())
    (ligne,) = entrepot.requete("SELECT * FROM meta_lignage WHERE etape = 'retraitement'")
    assert ligne["entree"] == "bronze_transactions" and ligne["version_modele"] == "factice-2"


def test_regeneration_des_rapports(config, entrepot, historique):
    resultat = retraiter(
        config,
        entrepot,
        "2020-12-21",
        "2020-12-26",
        modele=ModeleSeuilBas(),
        regenerer_rapports=True,
    )
    assert resultat["rapports"] >= 1
    assert entrepot.scalaire("SELECT COUNT(*) FROM or_rapport_quotidien") == resultat["rapports"]


def test_periode_vide_refusee(config, entrepot):
    with pytest.raises(ValueError, match="vide"):
        retraiter(config, entrepot, "2020-12-26", "2020-12-21", modele=ModeleSeuilBas())
