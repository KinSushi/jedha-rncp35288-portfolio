# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Indice de stabilité de population (PSI) : surveillance de la dérive des scores du modèle.

Seuils usuels : en dessous de 0,1 la distribution est stable ; au-delà de 0,2 elle a dérivé et la
démonstration déclenche un réentraînement (`pipeline.py`). Ces seuils sont une convention de
la profession, pas une loi : ils se règlent selon le coût d'un faux positif.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from stripe_demo import fraude

MESSAGE_SANS_SCORE = "le PSI exige au moins un score de référence et un de production"


def test_deux_distributions_identiques_donnent_zero():
    alea = np.random.default_rng(0)
    scores = alea.beta(2, 5, 5000)
    assert fraude.psi(scores, scores.copy()) == pytest.approx(0.0, abs=1e-9)

    # Une production sans aucun score est refusée : le calcul rendrait NaN, et « NaN > 0,2 » est
    # faux, donc la démonstration conclurait à tort que le modèle est stable.
    with pytest.raises(ValueError) as erreur:
        fraude.psi(scores, scores[:0])
    assert str(erreur.value) == MESSAGE_SANS_SCORE


def test_calcul_a_la_main_sur_deux_classes():
    """Référence 50/50 ; production 80/20 : PSI = Σ (p - r) × ln(p / r), calculé ici à la main."""
    reference = np.arange(100, dtype=float)
    production = np.concatenate([np.full(80, 10.0), np.full(20, 90.0)])
    attendu = (0.8 - 0.5) * math.log(0.8 / 0.5) + (0.2 - 0.5) * math.log(0.2 / 0.5)
    assert fraude.psi(reference, production, classes=2) == pytest.approx(attendu, rel=1e-4)
    assert attendu == pytest.approx(0.4159, abs=1e-3)

    with pytest.raises(ValueError) as erreur:  # une seule classe : le PSI vaudrait toujours 0
        fraude.psi(reference, production, classes=1)
    assert str(erreur.value) == "le PSI exige au moins 2 classes (reçu : 1)"

    # Sans réglage, les scores sont répartis en dix classes (déciles de la référence), comme
    # dans la note 07 : référence uniforme, production concentrée dans les deux classes
    # extrêmes (moitié chacune). Les huit classes vides valent 1e-6, les deux pleines 0,5.
    plate = np.arange(100, dtype=float)
    extremes = np.concatenate([np.full(50, 5.0), np.full(50, 95.0)])
    ref = 0.1 + 1e-6
    pleine, vide = 0.5 + 1e-6, 1e-6
    attendu_dix = (2 * (pleine - ref) * math.log(pleine / ref)
                   + 8 * (vide - ref) * math.log(vide / ref))
    assert fraude.psi(plate, extremes) == pytest.approx(attendu_dix, rel=1e-9)
    assert attendu_dix == pytest.approx(fraude.psi(plate, extremes, classes=10), rel=1e-12)
    assert fraude.psi(plate, extremes, classes=11) != pytest.approx(attendu_dix, rel=1e-3)


def test_un_echantillon_de_meme_loi_reste_stable():
    alea = np.random.default_rng(1)
    reference = alea.beta(2, 5, 20_000)
    production = alea.beta(2, 5, 5_000)
    assert fraude.psi(reference, production) < 0.1

    for classes in (0, -3):
        with pytest.raises(ValueError) as erreur:
            fraude.psi(reference, production, classes=classes)
        assert str(erreur.value) == f"le PSI exige au moins 2 classes (reçu : {classes})"


def test_une_derive_nette_depasse_le_seuil_de_reentrainement():
    alea = np.random.default_rng(2)
    reference = alea.beta(2, 5, 20_000)
    production = alea.beta(5, 2, 5_000)  # la loi des scores s'est inversée
    assert fraude.psi(reference, production) > 0.2

    with pytest.raises(ValueError) as erreur:  # pas de référence : refusé
        fraude.psi(reference[:0], production)
    assert str(erreur.value) == MESSAGE_SANS_SCORE


def test_le_psi_croit_avec_l_ampleur_de_la_derive():
    alea = np.random.default_rng(3)
    reference = alea.normal(0.0, 1.0, 20_000)
    valeurs = [fraude.psi(reference, alea.normal(decalage, 1.0, 20_000))
               for decalage in (0.0, 0.25, 0.5, 1.0)]
    assert valeurs == sorted(valeurs)
    assert valeurs[0] < 0.01 < 0.1 < valeurs[-1]

    with pytest.raises(ValueError) as erreur:  # deux séries vides
        fraude.psi(reference[:0], reference[:0])
    assert str(erreur.value) == MESSAGE_SANS_SCORE


def test_le_psi_n_est_jamais_negatif():
    alea = np.random.default_rng(4)
    for _ in range(20):
        a, b = alea.uniform(0.5, 4, 2)
        assert fraude.psi(alea.beta(a, b, 2000), alea.beta(b, a, 2000)) >= 0.0

    with pytest.raises(ValueError) as erreur:  # le message cite la valeur refusée
        fraude.psi(alea.beta(2, 5, 100), alea.beta(2, 5, 100), classes=-5)
    assert str(erreur.value) == "le PSI exige au moins 2 classes (reçu : -5)"
