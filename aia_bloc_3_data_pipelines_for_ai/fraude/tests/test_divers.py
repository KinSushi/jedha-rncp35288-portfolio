# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""FinOps, téléchargement vérifié, réinitialisation du mode local (critères 3.3 et 6.2)."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from detection_fraude.finops import estimer
from detection_fraude.local import etat, reinitialiser
from detection_fraude.telechargement import telecharger_jeu

CONSOMMATIONS = {
    "tx_par_jour": 1440,
    "octets_message_file": 500,
    "octets_bronze_par_tx": 300,
    "octets_argent_par_tx": 400,
    "octets_reponse_api": 700,
    "appels_api_par_jour": 1440,
    "retention_file_jours": 7,
    "retention_entrepot_jours": 90,
    "vcpu_services_permanents": 4,
    "duree_entrainement_s": 45,
}


def test_finops_total_egal_somme_des_postes():
    resultat = estimer(**CONSOMMATIONS)
    assert resultat["total_mensuel_eur"] == pytest.approx(
        sum(p["cout_eur"] for p in resultat["postes"].values()), abs=0.011
    )
    assert "hypothétiques" in resultat["avertissement"]


def test_finops_le_cout_suit_les_prix_et_les_volumes():
    base = estimer(**CONSOMMATIONS)
    double_prix = estimer(**CONSOMMATIONS, prix={"vcpu_heure_eur": 0.06})
    assert double_prix["total_mensuel_eur"] > base["total_mensuel_eur"]
    gros_volume = estimer(**{**CONSOMMATIONS, "tx_par_jour": 1440 * 1000})
    assert gros_volume["postes"]["stockage_entrepot"]["consommation"] == pytest.approx(
        base["postes"]["stockage_entrepot"]["consommation"] * 1000, rel=1e-2
    )  # la consommation est arrondie à 4 décimales
    assert base["postes"]["calcul_services_permanents"]["consommation"] == pytest.approx(4 * 730)


class FauxFlux:
    def __init__(self, contenu: bytes):
        self.contenu = contenu

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.contenu[: len(self.contenu) // 2]
        yield self.contenu[len(self.contenu) // 2 :]


def test_telechargement_verifie_taille_et_empreinte(config, monkeypatch):
    contenu = b"a;b\n1;2\n" * 50
    source = replace(
        config.source,
        jeu_taille_octets=len(contenu),
        jeu_sha256=hashlib.sha256(contenu).hexdigest(),
        jeu_url="https://exemple.test/jeu.csv",
    )
    config = replace(config, source=source)
    monkeypatch.setattr("requests.get", lambda *a, **k: FauxFlux(contenu))
    chemin = telecharger_jeu(config)
    assert chemin.read_bytes() == contenu
    assert telecharger_jeu(config) == chemin  # déjà présent : pas de nouveau téléchargement


def test_telechargement_refuse_un_fichier_altere(config, monkeypatch):
    contenu = b"a;b\n1;2\n" * 50
    source = replace(
        config.source,
        jeu_taille_octets=len(contenu),
        jeu_sha256="0" * 64,
        jeu_url="https://exemple.test/jeu.csv",
    )
    config = replace(config, source=source)
    monkeypatch.setattr("requests.get", lambda *a, **k: FauxFlux(contenu))
    with pytest.raises(RuntimeError, match="refusé"):
        telecharger_jeu(config)
    assert not config.source.jeu_chemin.exists()  # le fichier suspect n'est pas conservé
    assert not config.source.jeu_chemin.with_suffix(".partiel").exists()


def test_reinitialisation_supprime_les_donnees_generees_mais_pas_les_modeles(config, entrepot):
    config.modele.dossier.mkdir(parents=True)
    (config.modele.dossier / "ACTUEL").write_text("v1\n", encoding="utf-8")
    config.notification.fichier_alertes.parent.mkdir(parents=True, exist_ok=True)
    config.notification.fichier_alertes.write_text("{}\n", encoding="utf-8")
    config.rapport.dossier.mkdir(parents=True, exist_ok=True)
    (config.rapport.dossier / "rapport_x.html").write_text("x", encoding="utf-8")
    entrepot.fermer()
    reinitialiser(config)
    assert not config.notification.fichier_alertes.exists()
    assert not (config.racine_donnees / "entrepot.db").exists()
    assert not (config.rapport.dossier / "rapport_x.html").exists()
    assert (config.modele.dossier / "ACTUEL").exists()


def test_etat_compte_les_couches(config, entrepot):
    comptes = etat(config)
    assert comptes["bronze_transactions"] == 0 and "or_alertes_fraude" in comptes


def test_le_gitignore_protege_secrets_donnees_et_jeu():
    """Garde-fou : secrets générés, fichier .env et jeu de 150 Mo ne sont jamais versionnés."""
    from detection_fraude.configuration import RACINE_PROJET

    gitignore = RACINE_PROJET.parent / ".gitignore"
    if not gitignore.exists():
        pytest.skip("dossier fraude/ utilisé hors du dépôt")
    lignes = {ligne.strip() for ligne in gitignore.read_text(encoding="utf-8").splitlines()}
    for motif in ("fraude/secrets/", ".env", "fraude/data/*.csv", "fraude/donnees/", "*.db"):
        assert motif in lignes, f"{motif} absent du .gitignore"
