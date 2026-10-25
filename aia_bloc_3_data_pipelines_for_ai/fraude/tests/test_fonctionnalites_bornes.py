# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Caractéristiques : bornes exactes des fenêtres, plafond, élagage et refus des entrées invalides.

Les fenêtres sont écrites ici en secondes littérales (24 h, 30 jours) et non avec les constantes du
module : si quelqu'un change une fenêtre, ces tests le voient au lieu de suivre le changement.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from detection_fraude.fonctionnalites import (
    FENETRE_COMPTE_S,
    FENETRE_MOYENNE_S,
    PLAFOND_MONTANT_RELATIF,
    EtatCartes,
    HistoriqueCarte,
    _montant_relatif,
    ajouter_caracteristiques_temporelles,
    ajouter_historique_cartes,
    construire_jeu_modele,
    derivees_temporelles,
    normaliser_commercant,
)

JOUR = 86_400
TRENTE_JOURS = 30 * JOUR
T = 100 * JOUR  # instant de référence : assez loin de l'époque pour que les fenêtres tiennent

# (décalage entre l'instant interrogé et la transaction passée, comptée sur 24 h, comptée sur 30 j)
BORNES = [
    (-1, False, False),  # une seconde dans le futur : invisible
    (0, False, False),  # même seconde : une transaction ne se compte pas elle-même
    (1, True, True),
    (JOUR - 1, True, True),
    (JOUR, True, True),  # pile 24 h : incluse (borne gauche fermée)
    (JOUR + 1, False, True),  # 24 h + 1 s : sortie du compte, encore dans la moyenne
    (TRENTE_JOURS - 1, False, True),
    (TRENTE_JOURS, False, True),  # pile 30 jours : incluse
    (TRENTE_JOURS + 1, False, False),  # 30 jours + 1 s : sortie de la moyenne
]


def cumuls(historique: HistoriqueCarte) -> list[float]:
    return [float(c) for c in historique.cumul]


# --------------------------------------------------------------------------------------------
# normaliser_commercant, derivees_temporelles, _montant_relatif
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nom",
    [
        "Sans prefixe",
        "Marchand  et  Fils",
        "xfraud_Marchand",
        "FRAUD_Marchand",
        "fraud",
        "Frau_d_Marchand",
        "Marchand fraud_ et Fils",
    ],
)
def test_un_nom_sans_prefixe_en_tete_reste_inchange(nom):
    """Seul le préfixe parasite « fraud_ » placé en tête est retiré, jamais un autre morceau."""
    assert normaliser_commercant(nom) == nom


def test_normalisation_du_commercant_retire_le_prefixe_une_fois_et_nettoie_les_bords():
    """Le préfixe ne part qu'une fois, après le nettoyage des bords, et les bords sont nettoyés."""
    cas = {
        "fraud_Marchand et Fils ": "Marchand et Fils",
        "  fraud_Marchand": "Marchand",  # malgré les espaces de tête, le préfixe est retiré
        "fraud_  Marchand  ": "Marchand",  # les espaces qui suivent le préfixe sont retirés aussi
        "\tfraud_Marchand\n": "Marchand",
        "fraud_fraud_Marchand": "fraud_Marchand",  # une seule fois
        "fraud_": "",
        "  fraud_  ": "",
        "   ": "",
        "": "",
    }
    obtenu = {entree: normaliser_commercant(entree) for entree in cas}
    assert obtenu == cas
    with pytest.raises(AttributeError):
        normaliser_commercant(None)  # type: ignore[arg-type]  # un nom non textuel est refusé


def test_derivees_temporelles_sont_calculees_en_utc_et_tronquees_a_la_seconde():
    """Date, instant ISO, heure et jour de semaine (lundi = 0) sont ceux de l'UTC, sans arrondi."""
    attendu = {
        0: ("1970-01-01", "1970-01-01T00:00:00+00:00", 0, 3),  # jeudi
        86_399: ("1970-01-01", "1970-01-01T23:59:59+00:00", 23, 3),
        86_400: ("1970-01-02", "1970-01-02T00:00:00+00:00", 0, 4),  # vendredi
        1_608_548_400: ("2020-12-21", "2020-12-21T11:00:00+00:00", 11, 0),  # lundi
        1_609_027_199: ("2020-12-26", "2020-12-26T23:59:59+00:00", 23, 5),  # samedi
        1_609_027_200: ("2020-12-27", "2020-12-27T00:00:00+00:00", 0, 6),  # dimanche
        59.9: ("1970-01-01", "1970-01-01T00:00:59+00:00", 0, 3),  # 59,9 s -> 59 s, pas 60
    }
    assert {t: derivees_temporelles(t) for t in attendu} == attendu
    with pytest.raises(TypeError):
        derivees_temporelles(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("horodatage", "erreur"),
    [
        ("abc", ValueError),
        (None, TypeError),
        (10**30, (OverflowError, OSError, ValueError)),
    ],
)
def test_derivees_temporelles_refuse_un_horodatage_illisible_ou_hors_plage(horodatage, erreur):
    """Un horodatage non numérique ou hors plage est refusé : on n'invente jamais une date."""
    with pytest.raises(erreur):
        derivees_temporelles(horodatage)


def test_montant_relatif_vaut_un_sans_historique_exploitable_et_est_plafonne():
    """Le rapport est neutre sans historique ou à somme nulle, exact sinon, plafonné à 100."""
    assert (FENETRE_COMPTE_S, FENETRE_MOYENNE_S, PLAFOND_MONTANT_RELATIF) == (
        JOUR,
        TRENTE_JOURS,
        100.0,
    )
    neutres = [
        (10.0, 0.0, 0),  # aucune transaction passée
        (10.0, 50.0, 0),  # nombre nul : on ne divise jamais par zéro, même avec une somme
        (10.0, 0.0, 3),  # somme nulle : montants passés tous nuls
        (10.0, -5.0, 2),  # somme négative : même traitement
    ]
    assert [_montant_relatif(*cas) for cas in neutres] == [1.0, 1.0, 1.0, 1.0]
    exacts = [
        (10.0, 20.0, 2, 1.0),  # moyenne 10 : montant égal à la moyenne
        (30.0, 20.0, 2, 3.0),
        (0.0, 20.0, 2, 0.0),  # montant nul : rapport nul, pas neutre
        (99.0, 1.0, 1, 99.0),  # juste sous le plafond : conservé
        (100.0, 1.0, 1, 100.0),  # pile au plafond
        (100.5, 1.0, 1, 100.0),  # au-dessus : ramené à 100
        (1e9, 1.0, 1, 100.0),
    ]
    assert [_montant_relatif(m, s, n) for m, s, n, _ in exacts] == [e[3] for e in exacts]
    with pytest.raises(TypeError):
        _montant_relatif("10", 5.0, 1)  # type: ignore[arg-type]  # un montant texte est refusé


# --------------------------------------------------------------------------------------------
# HistoriqueCarte
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("decalage", "dans_24h", "dans_30j"), BORNES)
def test_les_fenetres_de_24_h_et_30_jours_sont_exactes_a_la_seconde_et_la_requete_est_pure(
    decalage, dans_24h, dans_30j
):
    """Borne gauche incluse, borne droite exclue, et interroger l'historique ne le modifie pas."""
    historique = HistoriqueCarte()
    historique.ajouter(T - decalage, 8.0)
    temps_avant, cumul_avant = list(historique.temps), list(historique.cumul)
    resultat = historique.statistiques(T)
    assert resultat == (int(dans_24h), 8.0 if dans_30j else 0.0, int(dans_30j))
    assert (historique.temps, historique.cumul) == (temps_avant, cumul_avant)


def test_les_sommes_de_fenetre_retirent_exactement_ce_qui_est_sorti_de_la_moyenne():
    """Avec cinq transactions autour des bornes, seules les bonnes entrent dans la somme."""
    historique = HistoriqueCarte()
    # Ajoutées dans l'ordre : de la plus ancienne (30 j + 1 s, montant 16) à la plus récente (1 s).
    for decalage, montant in [
        (TRENTE_JOURS + 1, 16.0),
        (TRENTE_JOURS, 8.0),
        (JOUR + 1, 4.0),
        (JOUR, 2.0),
        (1, 1.0),
    ]:
        historique.ajouter(T - decalage, montant)
    temps_avant = list(historique.temps)
    assert historique.statistiques(T) == (2, 15.0, 4)  # 24 h : 2 + 1 ; 30 j : 8 + 4 + 2 + 1
    assert historique.temps == temps_avant


def test_interroger_avant_la_premiere_transaction_ne_voit_rien_et_laisse_tout_intact():
    """Interroger avant la première transaction (ou la même seconde) ne voit jamais le futur."""
    historique = HistoriqueCarte()
    for decalage, montant in [(JOUR, 3.0), (60, 5.0)]:
        historique.ajouter(T - decalage, montant)
    instantane_avant = (list(historique.temps), list(historique.cumul))
    assert historique.statistiques(T - JOUR) == (0, 0.0, 0)  # pile à la 1re : elle-même exclue
    assert historique.statistiques(T - JOUR - 500) == (0, 0.0, 0)
    assert (historique.temps, historique.cumul) == instantane_avant


def test_l_historique_garde_les_transactions_et_les_cumuls_dans_l_ordre_du_temps():
    """Dans l'ordre, à égalité ou en retard : même liste triée, mêmes cumuls, aucune perte."""
    en_ordre = HistoriqueCarte()
    for t, montant in [(100, 10.0), (200, 20.0)]:
        en_ordre.ajouter(t, montant)
    cumul_avant = cumuls(en_ordre)
    en_ordre.ajouter(300, 30.0)
    assert cumuls(en_ordre) == [*cumul_avant, 60.0]  # l'arrivée en ordre n'altère rien de l'acquis
    assert en_ordre.temps == [100, 200, 300]

    egalite = HistoriqueCarte()
    for t, montant in [(100, 10.0), (100, 20.0)]:  # même seconde : conservées toutes les deux
        egalite.ajouter(t, montant)
    assert (egalite.temps, cumuls(egalite)) == ([100, 100], [10.0, 30.0])
    # Une arrivée à la même seconde est dans l'ordre : elle s'ajoute au dernier cumul (flottant
    # natif), sans recalcul de toute la liste (qui rendrait des flottants numpy).
    assert [type(c) for c in egalite.cumul] == [float, float]

    tardive = HistoriqueCarte()
    for t, montant in [(100, 10.0), (300, 30.0), (200, 20.0)]:
        tardive.ajouter(t, montant)
    assert (tardive.temps, cumuls(tardive)) == ([100, 200, 300], [10.0, 30.0, 60.0])

    # Arrivée tardive à égalité avec une transaction existante : elle se place après elle.
    jumelle = HistoriqueCarte()
    for t, montant in [(100, 10.0), (300, 30.0), (100, 1.0)]:
        jumelle.ajouter(t, montant)
    assert (jumelle.temps, cumuls(jumelle)) == ([100, 100, 300], [10.0, 11.0, 41.0])

    # La toute première transaction d'une carte peut être plus ancienne que celles qui suivent.
    debut = HistoriqueCarte()
    for t, montant in [(500, 5.0), (400, 4.0), (300, 3.0)]:
        debut.ajouter(t, montant)
    assert (debut.temps, cumuls(debut)) == ([300, 400, 500], [3.0, 7.0, 12.0])


def test_une_arrivee_tardive_ne_change_ni_le_nombre_ni_la_somme_des_autres_transactions():
    """Insérer une transaction en retard conserve toutes les autres et leur somme totale."""
    historique = HistoriqueCarte()
    for t, montant in [(100, 1.0), (200, 2.0), (400, 8.0)]:
        historique.ajouter(t, montant)
    avant = historique.statistiques(1000)
    historique.ajouter(300, 4.0)
    apres = historique.statistiques(1000)
    assert apres == (avant[0] + 1, avant[1] + 4.0, avant[2] + 1)  # une de plus, et rien de perdu
    assert historique.temps == [100, 200, 300, 400]


def test_l_elagage_oublie_ce_qui_depasse_deux_fenetres_et_rebase_les_cumuls():
    """Au-delà de 60 jours, une transaction est oubliée sans changer aucune réponse récente."""
    deux_fenetres = 2 * TRENTE_JOURS
    historique = HistoriqueCarte()
    for t, montant in [(0, 5.0), (10, 6.0), (20, 7.0)]:
        historique.ajouter(t, montant)
    assert (historique.temps, cumuls(historique)) == ([0, 10, 20], [5.0, 11.0, 18.0])
    # Le même historique, mais sans élagage (listes posées à la main), sert de référence.
    complet = HistoriqueCarte()
    complet.temps = [0, 10, 20, deux_fenetres + 15]
    complet.cumul = [5.0, 11.0, 18.0, 118.0]
    instants = [deux_fenetres + 16, deux_fenetres + 15 + JOUR, deux_fenetres + 15 + TRENTE_JOURS]
    reponses_avant = [complet.statistiques(t) for t in instants]
    historique.ajouter(deux_fenetres + 15, 100.0)  # limite = 15 : seules 0 et 10 sont oubliées
    assert historique.temps == [20, deux_fenetres + 15]
    assert cumuls(historique) == [7.0, 107.0]  # rebasés : 18 - 11 = 7, 118 - 11 = 107
    assert [historique.statistiques(t) for t in instants] == reponses_avant
    assert reponses_avant[0] == (1, 100.0, 1)


def test_un_historique_pile_a_deux_fenetres_reste_intact():
    """La transaction exactement à 60 jours de la plus récente est gardée : l'oubli est strict."""
    historique = HistoriqueCarte()
    historique.ajouter(0, 5.0)
    temps_avant, cumul_avant = list(historique.temps), cumuls(historique)
    historique.ajouter(2 * TRENTE_JOURS, 7.0)  # limite = 0 : 0 < 0 est faux, rien n'est oublié
    assert historique.temps == [*temps_avant, 2 * TRENTE_JOURS]
    assert cumuls(historique) == [*cumul_avant, 12.0]
    historique.ajouter(2 * TRENTE_JOURS + 1, 1.0)  # limite = 1 : l'instant 0 sort
    assert historique.temps == [2 * TRENTE_JOURS, 2 * TRENTE_JOURS + 1]
    assert cumuls(historique) == [7.0, 8.0]


# --------------------------------------------------------------------------------------------
# EtatCartes
# --------------------------------------------------------------------------------------------
def test_interroger_les_caracteristiques_ne_modifie_pas_l_etat():
    """Seul ``enregistrer`` ajoute une transaction : interroger ne la compte pas pour la suite."""
    etat = EtatCartes()
    etat.enregistrer("c", 500, 10.0)
    attendu_initial = (1, 5.0)
    assert etat.caracteristiques("c", 1000, 50.0) == attendu_initial
    etat.caracteristiques("c", 1000, 70.0)  # une autre interrogation, un autre montant
    assert etat.caracteristiques("c", 2000, 50.0) == attendu_initial  # la tx de 1000 n'existe pas
    etat.enregistrer("c", 1000, 50.0)
    assert etat.caracteristiques("c", 2000, 50.0) == (2, 50.0 / 30.0)


def test_deux_cartes_distinctes_ne_partagent_pas_leur_historique():
    """Les transactions d'une carte n'influencent jamais les caractéristiques d'une autre."""
    etat = EtatCartes()  # sans chargeur : une carte inconnue part d'un historique vide
    assert len(etat) == 0
    etat.enregistrer("a", 100, 40.0)
    etat.enregistrer("a", 200, 60.0)
    attendu_avant = etat.caracteristiques("a", 300, 50.0)
    assert etat.caracteristiques("b", 300, 50.0) == (0, 1.0)  # b ne voit rien de a
    etat.enregistrer("b", 250, 1000.0)
    assert etat.caracteristiques("a", 300, 50.0) == attendu_avant  # a ne voit rien de b
    assert (attendu_avant, len(etat)) == ((2, 1.0), 2)
    taille_avant = len(etat)
    etat.caracteristiques("c", 300, 50.0)  # même une simple interrogation ouvre l'historique
    assert len(etat) == taille_avant + 1


def test_une_carte_deja_connue_n_est_pas_relue_dans_le_bronze():
    """Le chargeur n'est appelé qu'à la première apparition d'une carte, avec ses bornes."""
    appels = []

    def chargeur(carte, avant_s, depuis_s):
        appels.append((carte, avant_s, depuis_s))
        return [(1000, 10.0), (2000, 30.0)]

    etat = EtatCartes(chargeur=chargeur)
    assert etat.caracteristiques("c", 5000, 40.0) == (2, 2.0)
    appels_avant = list(appels)
    assert appels_avant == [("c", 5000, 5000 - TRENTE_JOURS)]
    etat.caracteristiques("c", 6000, 40.0)
    etat.enregistrer("c", 7000, 5.0)
    assert appels == appels_avant  # aucune relecture
    etat.enregistrer("d", 8000, 5.0)  # une autre carte, elle, est chargée
    assert appels == [*appels_avant, ("d", 8000, 8000 - TRENTE_JOURS)]


# --------------------------------------------------------------------------------------------
# Version vectorisée
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("decalage", "dans_24h", "dans_30j"), BORNES)
def test_la_version_vectorisee_respecte_les_memes_bornes_exactes(decalage, dans_24h, dans_30j):
    """Le lot (entraînement) et le flux (scoring) tracent les fenêtres à la même seconde près."""
    df = pd.DataFrame(
        {
            "carte_hash": ["c", "c"],
            "horodatage_s": [T - decalage, T],
            "montant": [8.0, 24.0],
        }
    )
    avant = df.copy()
    with np.errstate(all="raise"):
        lot = ajouter_historique_cartes(df)
    ligne = lot.iloc[1]  # la transaction interrogée, à l'instant T
    assert ligne["nb_tx_24h"] == int(dans_24h)
    assert ligne["montant_relatif"] == (3.0 if dans_30j else 1.0)
    assert df.to_dict("list") == avant.to_dict("list")  # le tableau d'entrée est resté inchangé


def test_ajouter_historique_cartes_aligne_les_resultats_sur_l_ordre_d_entree():
    """Lignes dans le désordre, avec une transaction trop ancienne : chacune garde ses valeurs."""
    df = pd.DataFrame(
        {
            "carte_hash": ["c1", "c2", "c1", "c1", "c1"],
            "horodatage_s": [T + JOUR + 3600, T + 10, T, T + 3600, T - 40 * JOUR],
            "montant": [100.0, 5.0, 10.0, 30.0, 1000.0],
        },
        index=[7, 8, 9, 10, 11],
    )
    avant = df.copy()
    with np.errstate(all="raise"):
        lot = ajouter_historique_cartes(df)
    assert df.to_dict("list") == avant.to_dict("list")  # l'entrée garde son désordre
    # c1 triée : 1000 (il y a 40 jours : hors fenêtre pour tous les autres), 10, 30, 100.
    # T (10) : aucun historique exploitable -> neutre ; T+3600 (30) : une tx, moyenne 10 ;
    # T+JOUR+3600 (100) : la tx de T+3600 est à pile 24 h, et la moyenne des deux = 20.
    assert lot["nb_tx_24h"].tolist() == [1, 0, 0, 1, 0]
    assert lot["montant_relatif"].tolist() == [5.0, 1.0, 1.0, 3.0, 1.0]
    assert lot.index.tolist() == [7, 8, 9, 10, 11]
    assert str(lot["nb_tx_24h"].dtype) == "int64"
    assert str(lot["montant_relatif"].dtype) == "float64"


def test_la_version_vectorisee_ne_divise_jamais_par_zero_et_plafonne_comme_le_flux():
    """Montants passés nuls ou rapport énorme : mêmes valeurs que ``_montant_relatif``."""
    df = pd.DataFrame(
        {
            "carte_hash": ["z"] * 4 + ["g"] * 2 + ["p"] * 3,
            "horodatage_s": [T, T + 10, T + 20, T + 30, T, T + 10, T, T + 10, T + 20],
            "montant": [0.0, 0.0, 30.0, 0.0, 0.01, 1_000_000.0, 1.0, 100.0, 100.5],
        }
    )
    avant = df.copy()
    with np.errstate(all="raise"):  # une division par zéro, même masquée, ferait échouer le test
        lot = ajouter_historique_cartes(df)
    assert df.to_dict("list") == avant.to_dict("list")  # le tableau d'entrée est resté inchangé
    # z : somme passée nulle -> neutre (montant 0 puis 30), puis 0 / 10 = 0 (un montant nul n'est
    #     pas neutre quand la moyenne passée est non nulle).
    # g : 1_000_000 / 0,01 = 1e8 -> plafonné à 100.
    # p : 100 / 1 = 100 pile ; 100,5 / ((1 + 100) / 2) : la moyenne, pas le dernier montant.
    attendu = [1.0, 1.0, 1.0, 0.0, 1.0, 100.0, 1.0, 100.0, 100.5 / 50.5]
    obtenu = lot["montant_relatif"].tolist()
    assert obtenu == pytest.approx(attendu, rel=1e-12)
    assert (obtenu[5], obtenu[7]) == (100.0, 100.0)  # le plafond est atteint exactement
    flux = EtatCartes()
    relatifs_flux = []
    for ligne in df.itertuples():
        _, relatif = flux.caracteristiques(ligne.carte_hash, ligne.horodatage_s, ligne.montant)
        flux.enregistrer(ligne.carte_hash, ligne.horodatage_s, ligne.montant)
        relatifs_flux.append(relatif)
    assert obtenu == pytest.approx(relatifs_flux, rel=1e-12)


def test_la_version_vectorisee_accepte_des_montants_entiers_et_tronque_les_fractions_de_seconde():
    """Montants entiers = montants flottants ; une fraction de seconde ne sépare pas deux tx."""
    entiers = pd.DataFrame(
        {
            "carte_hash": ["c", "c", "c"],
            "horodatage_s": [T, T + 60, T + 120],
            "montant": [10, 30, 50],
        }
    )
    flottants = entiers.astype({"montant": "float64"})
    avant = entiers.copy()
    with np.errstate(all="raise"):
        lot_entiers = ajouter_historique_cartes(entiers)
        lot_flottants = ajouter_historique_cartes(flottants)
    assert entiers.to_dict("list") == avant.to_dict("list")
    assert lot_entiers["montant_relatif"].tolist() == [1.0, 3.0, 2.5]
    assert lot_entiers["montant_relatif"].tolist() == lot_flottants["montant_relatif"].tolist()
    assert str(lot_entiers["montant_relatif"].dtype) == "float64"
    fractions = pd.DataFrame(
        {
            "carte_hash": ["c", "c", "c"],
            "horodatage_s": [T + 0.9, T + 0.1, T + 1.2],
            "montant": [20.0, 40.0, 60.0],
        }
    )
    lot_fractions = ajouter_historique_cartes(fractions)
    # T+0,9 et T+0,1 tombent dans la même seconde T : aucune ne voit l'autre ; T+1,2 les voit.
    assert lot_fractions["nb_tx_24h"].tolist() == [0, 0, 2]
    assert lot_fractions["montant_relatif"].tolist() == [1.0, 1.0, 2.0]


@pytest.mark.parametrize("colonne", ["carte_hash", "horodatage_s", "montant"])
def test_ajouter_historique_cartes_refuse_un_tableau_sans_colonne_requise(colonne):
    """Une colonne manquante est signalée par son nom, et le tableau reçu n'est pas modifié."""
    complet = pd.DataFrame({"carte_hash": ["c"], "horodatage_s": [T], "montant": [1.0]})
    incomplet = complet.drop(columns=[colonne])
    avant = incomplet.copy()
    with pytest.raises(KeyError) as exc:
        ajouter_historique_cartes(incomplet)
    assert exc.value.args == (colonne,)
    pd.testing.assert_frame_equal(incomplet, avant)


def test_ajouter_caracteristiques_temporelles_laisse_le_tableau_d_entree_inchange():
    """Le tableau reçu garde ses colonnes et ses valeurs ; le résultat est une copie enrichie."""
    df = pd.DataFrame(
        {"horodatage_s": [1_608_595_199, 1_608_595_200, 0], "autre": ["a", "b", "c"]},
        index=[4, 5, 6],
    )
    avant = df.copy()
    lot = ajouter_caracteristiques_temporelles(df)
    assert df.to_dict("list") == avant.to_dict("list")
    assert lot is not df
    assert list(lot.columns) == ["horodatage_s", "autre", "heure", "jour_semaine"]
    assert lot["heure"].tolist() == [23, 0, 0]  # 2020-12-21 23:59:59, puis minuit, puis l'époque
    assert lot["jour_semaine"].tolist() == [0, 1, 3]
    assert (str(lot["heure"].dtype), str(lot["jour_semaine"].dtype)) == ("int64", "int64")
    assert lot.index.tolist() == [4, 5, 6]
    assert lot["autre"].tolist() == ["a", "b", "c"]


@pytest.mark.parametrize(
    "valeurs", [[1_608_548_400.0, float("nan")], ["abc", "1608548400"], [float("inf")]]
)
def test_un_horodatage_manquant_ou_illisible_est_refuse_et_le_tableau_reste_inchange(valeurs):
    """Pas d'heure inventée : une valeur manquante ou non numérique arrête le calcul."""
    df = pd.DataFrame({"horodatage_s": valeurs})
    avant = df.copy()
    with pytest.raises(ValueError):
        ajouter_caracteristiques_temporelles(df)
    pd.testing.assert_frame_equal(df, avant)


def test_construire_jeu_modele_enchaine_les_deux_ajouts_et_accepte_un_tableau_vide():
    """Le jeu complet porte les quatre caractéristiques ; vide en entrée, vide en sortie."""
    df = pd.DataFrame(
        {
            "carte_hash": ["c", "c"],
            "horodatage_s": [1_608_548_400, 1_608_548_400 + 3600],
            "montant": [10.0, 30.0],
        }
    )
    avant = df.copy()
    jeu = construire_jeu_modele(df)
    assert df.to_dict("list") == avant.to_dict("list")
    assert list(jeu.columns) == [
        "carte_hash",
        "horodatage_s",
        "montant",
        "heure",
        "jour_semaine",
        "nb_tx_24h",
        "montant_relatif",
    ]
    assert jeu["heure"].tolist() == [11, 12]
    assert jeu["jour_semaine"].tolist() == [0, 0]
    assert jeu["nb_tx_24h"].tolist() == [0, 1]
    assert jeu["montant_relatif"].tolist() == [1.0, 3.0]
    vide = construire_jeu_modele(df.iloc[0:0])
    assert vide.empty
    assert list(vide.columns) == list(jeu.columns)


# --------------------------------------------------------------------------------------------
# Précisions : ce que les tests ci-dessus laissaient passer (relevé par la mutation)
# --------------------------------------------------------------------------------------------
# (rang de la seconde, montant) de vingt transactions d'une même carte réparties sur quatre
# secondes seulement : de nombreuses égalités, dont l'ordre d'addition change les derniers
# chiffres.
EGALITES = [
    (3, 2811.33), (1, 750.32), (0, 2163.16), (1, 3346.49), (1, 2113.93), (3, 3165.93),
    (1, 4837.18), (0, 3415.33), (1, 1958.13), (2, 936.27), (3, 1729.81), (2, 2555.33),
    (3, 4456.05), (0, 3877.82), (3, 1590.74), (0, 4621.09), (2, 2354.55), (1, 3468.8),
    (0, 536.05), (2, 522.73),
]  # fmt: skip


def montants_relatifs_par_definition(df: pd.DataFrame) -> list[float]:
    """Définition du montant relatif, en Python simple : les égalités sont additionnées dans
    l'ordre d'arrivée, comme le fait le flux (``HistoriqueCarte.ajouter``)."""
    ordre = sorted(range(len(df)), key=lambda i: df["horodatage_s"].iloc[i])  # tri stable
    temps = [int(df["horodatage_s"].iloc[i]) for i in ordre]
    montants = [float(df["montant"].iloc[i]) for i in ordre]
    cumul = [0.0]
    for montant in montants:
        cumul.append(cumul[-1] + montant)
    resultat = [0.0] * len(df)
    for rang, i in enumerate(ordre):
        avant = sum(1 for t in temps if t < temps[rang])
        debut = sum(1 for t in temps if t < temps[rang] - TRENTE_JOURS)
        nombre = avant - debut
        moyenne = (cumul[avant] - cumul[debut]) / nombre if nombre else 0.0
        rapport = montants[rang] / moyenne if moyenne > 0 else 1.0
        resultat[i] = min(rapport, PLAFOND_MONTANT_RELATIF)
    return resultat


def test_les_egalites_sont_additionnees_dans_l_ordre_d_arrivee_au_dernier_chiffre_pres():
    """Version par lot et définition donnent exactement les mêmes flottants, égalités comprises."""
    df = pd.DataFrame(
        {
            "carte_hash": ["c" * 32] * len(EGALITES),
            "horodatage_s": [10_000 + 100 * rang for rang, _ in EGALITES],
            "montant": [montant for _, montant in EGALITES],
        }
    )
    avant = df.copy()
    resultat = ajouter_historique_cartes(df)
    assert list(resultat["montant_relatif"]) == montants_relatifs_par_definition(df)
    assert df.equals(avant)
