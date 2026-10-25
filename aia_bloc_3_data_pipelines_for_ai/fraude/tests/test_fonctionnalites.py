# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Caractéristiques : version vectorisée (lot) et incrémentale (flux) identiques (critère 2.2)."""

from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from detection_fraude.fonctionnalites import (
    FENETRE_COMPTE_S,
    FENETRE_MOYENNE_S,
    EtatCartes,
    HistoriqueCarte,
    ajouter_caracteristiques_temporelles,
    construire_jeu_modele,
    derivees_temporelles,
    normaliser_commercant,
)


def jeu_aleatoire(n=1500, cartes=12, graine=3):
    hasard = random.Random(graine)
    lignes = []
    for i in range(n):
        lignes.append(
            {
                "trans_num": f"{i:032x}",
                "carte_hash": f"c{hasard.randrange(cartes)}",
                # Beaucoup d'égalités d'horodatage et quelques trous de plus de 30 jours.
                "horodatage_s": 1_600_000_000
                + hasard.randrange(0, 70) * 86_400 // 7
                + hasard.choice([0, 0, 0, 3600, 86_400 * 40]),
                "montant": round(hasard.uniform(1, 400), 2),
            }
        )
    return pd.DataFrame(lignes).sort_values("horodatage_s", kind="stable").reset_index(drop=True)


def test_lot_et_flux_donnent_les_memes_caracteristiques():
    df = jeu_aleatoire()
    lot = construire_jeu_modele(df)
    etat = EtatCartes()
    for rang, ligne in df.iterrows():
        nb_24h, relatif = etat.caracteristiques(
            ligne["carte_hash"], int(ligne["horodatage_s"]), float(ligne["montant"])
        )
        etat.enregistrer(ligne["carte_hash"], int(ligne["horodatage_s"]), float(ligne["montant"]))
        assert lot.loc[rang, "nb_tx_24h"] == nb_24h
        assert lot.loc[rang, "montant_relatif"] == pytest.approx(relatif, rel=1e-9)


def test_une_transaction_ne_se_compte_pas_elle_meme_ni_ses_jumelles():
    df = pd.DataFrame(
        {
            "trans_num": ["a" * 32, "b" * 32],
            "carte_hash": ["c", "c"],
            "horodatage_s": [1_000_000, 1_000_000],  # même seconde
            "montant": [10.0, 90.0],
        }
    )
    lot = construire_jeu_modele(df)
    assert list(lot["nb_tx_24h"]) == [0, 0]
    assert list(lot["montant_relatif"]) == [1.0, 1.0]


def test_carte_inconnue_valeurs_neutres():
    etat = EtatCartes()
    assert etat.caracteristiques("nouvelle", 1_000_000, 50.0) == (0, 1.0)


def test_fenetre_de_24_heures_bornes_incluse_a_gauche_exclue_a_droite():
    historique = HistoriqueCarte()
    t = 10 * FENETRE_COMPTE_S
    historique.ajouter(t - FENETRE_COMPTE_S, 10.0)  # pile à 24 h : compté
    historique.ajouter(t - FENETRE_COMPTE_S - 1, 10.0)  # 24 h + 1 s : non compté
    historique.ajouter(t, 10.0)  # même instant : non compté
    nb_24h, _, nb_30j = historique.statistiques(t)
    assert nb_24h == 1 and nb_30j == 2


def test_la_moyenne_porte_sur_30_jours_seulement():
    etat = EtatCartes()
    t = 100 * 86_400
    etat.enregistrer("c", t - FENETRE_MOYENNE_S - 10, 1000.0)  # trop ancien
    etat.enregistrer("c", t - 3600, 20.0)
    _, relatif = etat.caracteristiques("c", t, 40.0)
    assert relatif == pytest.approx(2.0)


def test_le_montant_relatif_est_plafonne():
    etat = EtatCartes()
    etat.enregistrer("c", 1000, 0.01)
    assert etat.caracteristiques("c", 2000, 1_000_000.0)[1] == 100.0


def test_arrivee_tardive_inseree_a_sa_place():
    historique = HistoriqueCarte()
    for t, m in [(100, 10.0), (300, 30.0), (200, 20.0)]:
        historique.ajouter(t, m)
    assert historique.temps == [100, 200, 300]
    _, somme, nombre = historique.statistiques(250)
    assert (nombre, somme) == (2, 30.0)


def test_l_etat_se_reconstruit_depuis_l_historique_charge():
    passe = [(100, 10.0), (200, 30.0)]

    def chargeur(carte, avant_s, depuis_s):
        assert (carte, avant_s) == ("c", 300)
        return passe

    etat = EtatCartes(chargeur=chargeur)
    assert etat.caracteristiques("c", 300, 40.0) == (2, 2.0)
    assert len(etat) == 1


def test_elagage_de_la_memoire():
    historique = HistoriqueCarte()
    historique.ajouter(0, 5.0)
    historique.ajouter(3 * FENETRE_MOYENNE_S, 7.0)
    assert historique.temps == [3 * FENETRE_MOYENNE_S]
    assert historique.cumul == [7.0]


def test_caracteristiques_temporelles_lot_et_ligne_concordent():
    df = pd.DataFrame({"horodatage_s": [1_608_550_800, 1_608_597_599]})
    lot = ajouter_caracteristiques_temporelles(df)
    for rang, t in enumerate(df["horodatage_s"]):
        _, _, heure, jour = derivees_temporelles(int(t))
        assert (lot.loc[rang, "heure"], lot.loc[rang, "jour_semaine"]) == (heure, jour)


def test_derivees_temporelles():
    date_iso, instant, heure, jour = derivees_temporelles(1_608_548_400)  # 2020-12-21 11:00 UTC
    assert date_iso == "2020-12-21" and instant.startswith("2020-12-21T11:00:00")
    assert (heure, jour) == (11, 0)  # lundi


def test_normalisation_du_commercant():
    assert normaliser_commercant("fraud_Marchand et Fils ") == "Marchand et Fils"
    assert normaliser_commercant("Sans prefixe") == "Sans prefixe"


def test_aucun_nan_dans_les_caracteristiques():
    lot = construire_jeu_modele(jeu_aleatoire(300))
    assert not lot[["nb_tx_24h", "montant_relatif"]].isna().any().any()
    assert np.isfinite(lot["montant_relatif"]).all()
