# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Une heure de transaction illisible va en file des rejets sans bloquer le flux (critère 4.2).

Régression : ``masquer_pour_rejet`` comparait ``current_time`` à une plage numérique avant d'en
contrôler le type. Une ligne dont l'heure est absente, ``None`` ou textuelle (exactement ce que le
schéma refuse) faisait donc lever ``TypeError`` dans la voie des rejets, et le lot entier était
perdu au lieu de seulement la ligne corrompue.
"""

from __future__ import annotations

import pytest

from detection_fraude.file_attente import FileSQLite
from detection_fraude.masquage import masquer_pour_rejet
from detection_fraude.producteur import Producteur
from tests.helpers import ligne_brute


@pytest.mark.parametrize(
    ("heure", "attendu"),
    [
        (None, None),
        ("hier", "hier"),
        ("", ""),
        # un nombre de 13 chiffres dans un texte est effacé
        (["1608548400000"], "['[PAN_MASQUE]']"),
        ({"ms": 1}, "{'ms': 1}"),
    ],
)
def test_une_heure_non_entiere_est_tronquee_sans_comparaison_ni_erreur(heure, attendu):
    """Aucune comparaison numérique n'est tentée sur une heure qui n'est pas un entier."""
    rejet = masquer_pour_rejet(ligne_brute(1, current_time=heure))
    assert rejet["champs_conserves"]["current_time"] == attendu
    assert "current_time" not in rejet["champs_masques"]


@pytest.mark.parametrize("heure", [None, "hier", ["x"]])
def test_une_ligne_a_l_heure_illisible_va_en_dlq_et_les_lignes_valides_voisines_passent(
    config, hacheur, heure
):
    """Le lot continue : seule la ligne corrompue est rejetée, les deux voisines sont publiées."""
    file = FileSQLite(config.file.chemin_sqlite)
    try:
        producteur = Producteur(config, file, hacheur, source="jeu")
        bilan = producteur.traiter_lignes(
            [ligne_brute(1), ligne_brute(2, current_time=heure), ligne_brute(3)]
        )
        assert (bilan.lues, bilan.publiees, bilan.rejetees) == (3, 2, 1)
        publies = file.lire(config.file.sujet_valides, "controle", 10, 0)
        rejets = file.lire(config.file.sujet_rejets, "controle", 10, 0)
    finally:
        file.fermer()
    assert [m.valeur["trans_num"] for m in publies] == [
        ligne_brute(1)["trans_num"],
        ligne_brute(3)["trans_num"],
    ]
    assert [m.valeur["trans_num"] for m in rejets] == [ligne_brute(2)["trans_num"]]
    assert rejets[0].valeur["raison"] == "schema_invalide"
    assert rejets[0].valeur["erreurs"][0]["champ"] == "current_time"
