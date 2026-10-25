# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Contraintes d'intégrité de l'OLTP : la base refuse les données incohérentes (ACID, le C).

Pourquoi tester la base et pas le code : la garantie doit tenir même si une application, un script
d'import ou une personne écrit directement dans la base.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import Column, MetaData, String, Table, insert, text
from sqlalchemy.exc import IntegrityError, OperationalError

from stripe_demo import modele_oltp as m


def _paiement(**champs) -> dict:
    """Un paiement valide (données du jeu de test) que chaque test altère d'un seul champ."""
    base = {"id_paiement": 900_001, "id_marchand": 1, "id_client": 1, "id_moyen": 1,
            "montant_mineur": 1000, "code_devise": "EUR", "statut": "reussi",
            "cree_le": datetime(2026, 8, 2, 12, 0, tzinfo=UTC), "pays_ip": "FR",
            "appareil": "mobile", "cle_idempotence": "test-idem-1"}
    base.update(champs)
    return base


COLONNES_INTERDITES = re.compile(r"(^pan$|numero_carte|num_carte|cvv|cvc|cryptogramme|expiration)")


def exiger_aucune_colonne_interdite(tables) -> None:
    """PCI-DSS : ni numéro complet, ni cryptogramme, ni date d'expiration dans aucune table."""
    for table in tables:
        for colonne in table.columns:
            assert not COLONNES_INTERDITES.search(colonne.name), f"{table.name}.{colonne.name}"


def exiger_courriels_pseudonymises(valeurs) -> None:
    """RGPD : aucun courriel en clair, uniquement une empreinte SHA-256 hexadécimale."""
    assert valeurs
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in valeurs), "courriel non pseudonymisé"


TABLES_DU_MODELE = {
    "pays", "devises", "taux_change", "marchands", "clients", "moyens_paiement", "produits",
    "abonnements", "paiements", "remboursements", "litiges", "journal_audit",
    "journal_changements"}


def test_treize_tables(mini_demo):
    """Le modèle compte exactement ces 13 tables, et toutes existent dans la base créée."""
    assert set(m.schema.tables) == TABLES_DU_MODELE
    with mini_demo.moteur.connect() as cx:
        noms = {ligne[0] for ligne in cx.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'"))}
    assert set(m.schema.tables) <= noms

    # Aucune table de cartes bancaires n'existe : la lire est refusée par la base.
    with pytest.raises(OperationalError, match="no such table"), mini_demo.moteur.connect() as cx:
        cx.execute(text("SELECT numero_carte FROM cartes_bancaires"))


def test_un_paiement_valide_est_accepte(mini_demo):
    with mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement()])

    # Le même numéro de paiement ne peut pas être écrit deux fois (clé primaire).
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement(cle_idempotence="test-idem-autre")])


@pytest.mark.parametrize("champs", [
    {"montant_mineur": 0},
    {"montant_mineur": -5},
    {"statut": "inconnu"},
    {"appareil": "montre"},
])
def test_check_refuse_les_valeurs_hors_domaine(mini_demo, champs):
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement(**champs)])


@pytest.mark.parametrize("champs", [
    {"id_marchand": 99_999},
    {"id_client": 99_999},
    {"id_moyen": 99_999},
    {"code_devise": "XXX"},
])
def test_cle_etrangere_refuse_une_reference_inexistante(mini_demo, champs):
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement(**champs)])


def test_cle_d_idempotence_unique_empeche_le_double_debit(mini_demo):
    """Rejouer la même requête de paiement (même clé) est refusé : pas de double débit."""
    with mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement()])
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement(id_paiement=900_002)])


def test_un_seul_litige_par_paiement(mini_demo):
    with mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement(statut="conteste")])
        cx.execute(insert(m.litiges), [{
            "id_litige": 1, "id_paiement": 900_001, "motif": "fraude", "statut": "ouvert",
            "cree_le": datetime(2026, 8, 3, tzinfo=UTC)}])
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.litiges), [{
            "id_litige": 2, "id_paiement": 900_001, "motif": "fraude", "statut": "ouvert",
            "cree_le": datetime(2026, 8, 3, tzinfo=UTC)}])


def test_jeton_du_prestataire_unique(mini_demo):
    with mini_demo.moteur.connect() as cx:
        jeton = cx.execute(text("SELECT jeton_psp FROM moyens_paiement WHERE id_moyen = 1")
                           ).scalar_one()
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.moyens_paiement), [{
            "id_moyen": 90_001, "id_client": 1, "type": "carte", "jeton_psp": jeton}])


def test_rollback_atomique(mini_demo):
    """Atomicité : si une instruction de la transaction échoue, la précédente est annulée."""
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement()])
        cx.execute(insert(m.paiements), [_paiement(montant_mineur=-1, id_paiement=900_003,
                                                   cle_idempotence="test-idem-2")])
    with mini_demo.moteur.connect() as cx:
        nombre = cx.execute(text("SELECT count(*) FROM paiements WHERE id_paiement = 900001")
                            ).scalar_one()
    assert nombre == 0


def test_aucune_colonne_ne_peut_porter_un_numero_de_carte():
    """PCI-DSS : le modèle ne prévoit ni numéro complet, ni cryptogramme, ni date d'expiration."""
    exiger_aucune_colonne_interdite(m.schema.tables.values())
    assert m.moyens_paiement.c.quatre_derniers.type.length == 4

    # Une table qui porterait l'une de ces colonnes serait refusée par la même règle.
    for nom in ("numero_carte", "cvv", "cryptogramme", "date_expiration", "pan"):
        fautive = Table("moyens_fautifs", MetaData(), Column(nom, String(19)))
        with pytest.raises(AssertionError, match=nom):
            exiger_aucune_colonne_interdite([fautive])


def test_courriel_pseudonymise(mini_demo):
    """RGPD : aucun courriel en clair, uniquement une empreinte SHA-256 hexadécimale."""
    with mini_demo.moteur.connect() as cx:
        valeurs = [ligne[0] for ligne in cx.execute(text("SELECT email_hache FROM clients"))]
    exiger_courriels_pseudonymises(valeurs)

    # Un courriel en clair, une empreinte tronquée ou en majuscules sont refusés.
    for fautive in ("alice@example.org", valeurs[0][:63], valeurs[0].upper()):
        with pytest.raises(AssertionError, match="non pseudonymisé"):
            exiger_courriels_pseudonymises([*valeurs, fautive])
    with pytest.raises(AssertionError):  # aucune valeur : rien ne prouve la pseudonymisation
        exiger_courriels_pseudonymises([])


def test_declencheurs_journalisent_insertion_et_mise_a_jour(mini_demo):
    """Le journal des changements (rôle du WAL en démonstration) reçoit chaque modification."""
    with mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement()])
        cx.execute(text("UPDATE paiements SET statut = 'rembourse' WHERE id_paiement = 900001"))
    with mini_demo.moteur.connect() as cx:
        lignes = cx.execute(text(
            "SELECT operation FROM journal_changements WHERE id_ligne = 900001 "
            "ORDER BY position")).fetchall()
    assert [ligne[0] for ligne in lignes] == ["INSERT", "UPDATE"]

    # Une modification refusée par la base ne laisse aucune trace dans le journal.
    with pytest.raises(IntegrityError), mini_demo.moteur.begin() as cx:
        cx.execute(insert(m.paiements), [_paiement(
            id_paiement=900_005, montant_mineur=-1, cle_idempotence="test-idem-5")])
    with mini_demo.moteur.connect() as cx:
        traces = cx.execute(text(
            "SELECT count(*) FROM journal_changements WHERE id_ligne = 900005")).scalar_one()
    assert traces == 0
