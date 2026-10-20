# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests de `att_spam.reseaux` : perte pondérée, TextCNN, BiGRU, boucle d'apprentissage."""

import math

import numpy as np
import pytest
import torch
from sklearn.metrics import f1_score
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from att_spam.donnees import Augmenteur
from att_spam.reseaux import (
    BiGRU,
    TextCNN,
    creer_perte,
    entrainer,
    ligne_epoque,
    poids_spam,
    probabilites,
)

# --------------------------------------------------------------------------- perte pondérée


def test_perte_ponderee_par_le_rapport_legitimes_sur_spams():
    """Poids exact, terme des spams multiplié, spam manqué plus coûteux qu'un faux positif."""
    assert poids_spam(torch.tensor([0.0, 0.0, 0.0, 1.0])) == pytest.approx(3.0)
    assert poids_spam(torch.tensor([0.0, 1.0, 1.0])) == pytest.approx(0.5)
    assert isinstance(poids_spam(torch.tensor([0.0, 1.0])), float)

    y = torch.tensor([0.0, 0.0, 0.0, 1.0])  # poids de la classe spam : 3
    perte = creer_perte(y)
    assert perte.pos_weight.tolist() == [3.0]
    # score nul : probabilité 0,5, donc -log(0,5) = ln 2 pour un légitime, 3 ln 2 pour un spam
    cibles = torch.tensor([1.0, 0.0])
    valeur = perte(torch.zeros(2), cibles).item()
    assert valeur == pytest.approx((3 * math.log(2) + math.log(2)) / 2)

    perte = creer_perte(torch.tensor([0.0] * 7 + [1.0]))
    spam_manque = perte(torch.tensor([-2.0]), torch.tensor([1.0])).item()
    faux_positif = perte(torch.tensor([2.0]), torch.tensor([0.0])).item()
    assert spam_manque == pytest.approx(7 * faux_positif)


def test_poids_spam_refuse_un_jeu_sans_spam():
    with pytest.raises(ValueError) as erreur:
        poids_spam(torch.zeros(5))
    message = "aucun spam dans les cibles : le poids de la classe spam est indéfini"
    assert str(erreur.value) == message


# --------------------------------------------------------------------------- TextCNN


def test_textcnn_structure():
    """Structure par défaut, dimensions demandées, vecteur de remplissage nul."""
    modele = TextCNN(taille_vocab=50)
    assert modele.plongement.num_embeddings == 50
    assert modele.plongement.embedding_dim == 100
    assert modele.plongement.padding_idx == 0
    assert [c.kernel_size for c in modele.convolutions] == [(3,), (4,), (5,)]
    assert [c.out_channels for c in modele.convolutions] == [64, 64, 64]
    assert [c.in_channels for c in modele.convolutions] == [100, 100, 100]
    assert modele.abandon.p == 0.5
    assert modele.sortie.in_features == 3 * 64
    assert modele.sortie.out_features == 1

    modele = TextCNN(taille_vocab=7, dim=10, filtres=4)
    assert modele.plongement.embedding_dim == 10
    assert modele.convolutions[0].in_channels == 10
    assert modele.convolutions[0].out_channels == 4
    assert modele.sortie.in_features == 12
    # Un lot d'un seul SMS garde sa dimension de lot : un vecteur d'un score, pas un scalaire
    # (le dernier lot d'une époque peut ne compter qu'un message).
    assert modele(torch.randint(1, 7, (1, 8))).shape == (1,)

    modele = TextCNN(taille_vocab=7, dim=10, filtres=4)
    assert modele.plongement.weight[0].abs().sum().item() == 0.0


def regler_textcnn_a_la_main():
    """TextCNN minuscule dont chaque filtre compte les composantes des mots de sa fenêtre."""
    modele = TextCNN(taille_vocab=6, dim=2, filtres=1)
    with torch.no_grad():
        modele.plongement.weight.copy_(
            torch.tensor([[0.0, 0.0], [0.5, 0.5], [1.0, 0.0], [0.0, 1.0], [2.0, 0.0], [0.0, 2.0]])
        )
        for convolution in modele.convolutions:
            convolution.weight.fill_(1.0)
            convolution.bias.fill_(-1.5)
        modele.sortie.weight.fill_(1.0)
        modele.sortie.bias.zero_()
    return modele.eval()


def test_textcnn_somme_des_fenetres_relu_puis_maximum():
    modele = regler_textcnn_a_la_main()
    x = torch.tensor(
        [
            [2, 3, 0, 0, 0, 0, 0, 0],  # fenêtre utile : 1 + 1 = 2 ; 2 - 1,5 = 0,5 ; trois filtres
            [0, 0, 0, 0, 0, 0, 0, 0],  # toutes les fenêtres valent -1,5 : la ReLU les ramène à 0
            [4, 5, 0, 0, 0, 0, 0, 0],  # fenêtre utile : 2 + 2 = 4 ; 4 - 1,5 = 2,5 ; trois filtres
            [0, 0, 0, 0, 0, 0, 3, 3],  # le signal est à la fin : le maximum le retrouve
        ]
    )
    logits = modele(x)
    assert logits.shape == (4,)
    assert logits.tolist() == pytest.approx([1.5, 0.0, 7.5, 3 * (1 + 1 - 1.5)])


def test_textcnn_dropout_en_entrainement_et_graine():
    """Le dropout ne joue qu'en entraînement ; deux constructions sous la même graine sont
    identiques."""
    torch.manual_seed(0)
    modele = TextCNN(taille_vocab=20)
    x = torch.randint(1, 20, (4, 8))
    modele.eval()
    assert torch.equal(modele(x), modele(x))
    modele.train()
    assert not torch.equal(modele(x), modele(x))

    torch.manual_seed(5)
    a = TextCNN(taille_vocab=20)
    torch.manual_seed(5)
    b = TextCNN(taille_vocab=20)
    assert all(torch.equal(p, q) for p, q in zip(a.parameters(), b.parameters(), strict=True))


def test_textcnn_refuse_une_sequence_plus_courte_que_le_plus_grand_filtre():
    modele = TextCNN(taille_vocab=20).eval()
    with pytest.raises(RuntimeError):
        modele(torch.ones(2, 4, dtype=torch.long))


# --------------------------------------------------------------------------- BiGRU


def test_bigru_structure():
    """Structure par défaut, dimensions demandées, vecteur de remplissage nul."""
    modele = BiGRU(taille_vocab=50)
    assert modele.plongement.num_embeddings == 50
    assert modele.plongement.embedding_dim == 100
    assert modele.plongement.padding_idx == 0
    assert modele.gru.input_size == 100
    assert modele.gru.hidden_size == 64
    assert modele.gru.bidirectional is True
    assert modele.gru.batch_first is True
    assert modele.abandon.p == 0.5
    assert modele.sortie.in_features == 2 * 64
    assert modele.sortie.out_features == 1

    modele = BiGRU(taille_vocab=9, dim=6, cache=5)
    assert modele.gru.input_size == 6
    assert modele.gru.hidden_size == 5
    assert modele.sortie.in_features == 10
    # Un lot d'un seul SMS garde sa dimension de lot : un vecteur d'un score, pas un scalaire.
    assert modele(torch.randint(1, 9, (1, 8))).shape == (1,)

    assert BiGRU(taille_vocab=9, dim=6, cache=5).plongement.weight[0].abs().sum().item() == 0.0


def test_bigru_logit_et_ordre_des_mots():
    """Logit = maximum dans le temps des états des deux sens ; l'ordre des mots compte."""
    torch.manual_seed(1)
    modele = BiGRU(taille_vocab=12, dim=6, cache=5).eval()
    x = torch.randint(1, 12, (3, 7))
    etats, _ = modele.gru(modele.plongement(x))
    attendu = modele.sortie(etats.max(dim=1).values).squeeze(1)
    assert modele(x).shape == (3,)
    assert modele(x).tolist() == pytest.approx(attendu.tolist(), abs=1e-6)
    assert etats.shape == (3, 7, 10)  # 2 sens x 5 unités cachées

    torch.manual_seed(2)
    modele = BiGRU(taille_vocab=12, dim=6, cache=5).eval()
    x = torch.tensor([[1, 2, 3, 4, 5]])
    assert not torch.allclose(modele(x), modele(x.flip(1)))


def test_bigru_le_dropout_ne_joue_qu_en_entrainement():
    torch.manual_seed(0)
    modele = BiGRU(taille_vocab=20)
    x = torch.randint(1, 20, (4, 8))
    modele.eval()
    assert torch.equal(modele(x), modele(x))
    modele.train()
    assert not torch.equal(modele(x), modele(x))


# --------------------------------------------------------------------------- probabilites


def test_probabilites_sigmoide_en_mode_evaluation():
    """Sigmoïde du score ; dropout coupé même si le modèle était en entraînement."""
    torch.manual_seed(3)
    modele = TextCNN(taille_vocab=20)
    x = torch.randint(1, 20, (5, 8))
    probas = probabilites(modele, x)
    assert probas.dtype == np.float32
    assert probas.shape == (5,)
    assert probas.tolist() == pytest.approx(torch.sigmoid(modele.eval()(x)).tolist(), abs=1e-6)
    assert ((probas > 0) & (probas < 1)).all()

    torch.manual_seed(3)
    modele = TextCNN(taille_vocab=20)
    modele.train()
    x = torch.randint(1, 20, (5, 8))
    assert probabilites(modele, x).tolist() == probabilites(modele, x).tolist()
    assert modele.training is False


# --------------------------------------------------------------------------- ligne affichée


def test_ligne_epoque_met_en_forme_la_derniere_valeur_de_chaque_courbe():
    # trois époques : « la dernière » se distingue de « la deuxième » et de « l'avant-dernière »
    historique = {
        "perte_app": [0.5, 0.4, 0.123456],
        "perte_val": [0.6, 0.7, 0.2345],
        "f1_app": [0.7, 0.75, 0.98765],
        "f1_val": [0.8, 0.85, 0.91234],
    }
    assert ligne_epoque("TextCNN", 3, historique) == (
        "TextCNN époque  3 | perte app 0.123 val 0.234 | F1 app 0.988 val 0.912"
    )
    assert ligne_epoque("BiGRU", 12, historique) == (
        "BiGRU époque 12 | perte app 0.123 val 0.234 | F1 app 0.988 val 0.912"
    )
    assert ligne_epoque("D", 1, historique, largeur=1) == (
        "D époque 1 | perte app 0.123 val 0.234 | F1 app 0.988 val 0.912"
    )


# ---------------------------------------------------------------- entrainer (modèle scripté)


class ModeleScripte(nn.Module):
    """Modèle dont les prédictions sont dictées par un scénario, pour tester la boucle seule.

    En entraînement il rend `p` pour chaque message (un seul paramètre : de quoi calculer un
    gradient). En évaluation, sur le jeu de validation, `positifs[époque]` messages sont déclarés
    spam ; sur le jeu d'apprentissage, les `n_app_positifs` premiers.
    """

    def __init__(self, n_val, positifs, n_app_positifs=5):
        super().__init__()
        self.p = nn.Parameter(torch.tensor(1.0))
        self.n_val = n_val
        self.positifs = positifs
        self.n_app_positifs = n_app_positifs
        self.epoque = 0
        self.p_fin_epoque = []
        self.lots_vus = []

    def train(self, mode=True):
        if mode:
            self.epoque += 1
        return super().train(mode)

    def forward(self, x):
        if self.training:
            self.lots_vus.append(x.clone())
            return self.p * torch.ones(len(x))
        if len(x) == self.n_val:
            self.p_fin_epoque.append(self.p.item())
            nombre = self.positifs[self.epoque - 1]
        else:
            nombre = self.n_app_positifs
        scores = -torch.ones(len(x))
        scores[:nombre] = 1.0
        return scores


def jeux_jouets(n_app=70, n_val=4):
    """Apprentissage : n_app messages, les 10 premiers spams ; validation : que des spams."""
    x_app = torch.arange(n_app).reshape(-1, 1).repeat(1, 4)
    y_app = torch.zeros(n_app)
    y_app[: n_app // 7] = 1.0
    x_val = torch.zeros(n_val, 4, dtype=torch.long)
    y_val = torch.ones(n_val)
    return x_app, y_app, x_val, y_val


#: F1 de validation si k spams sur les 4 de la validation sont trouvés (aucun faux positif).
F1_PAR_POSITIFS = {1: 2 / 5, 2: 2 / 3, 3: 6 / 7, 4: 1.0}


def lancer(modele, graine=0, **options):
    x_app, y_app, x_val, y_val = jeux_jouets()
    perte = creer_perte(y_app)
    historique = entrainer(
        modele, x_app, y_app, x_val, y_val, perte, Augmenteur(4, graine), graine=graine, **options
    )
    return historique, perte


def test_entrainer_arret_precoce_selon_la_patience():
    """Arrêt après `patience` époques sans progrès ; égalité sans progrès ; borne incluse."""
    modele = ModeleScripte(n_val=4, positifs=[2, 3, 3, 2, 2, 2, 4, 4, 4])
    historique, _ = lancer(modele, patience=3, bavard=False)
    attendu = [F1_PAR_POSITIFS[k] for k in (2, 3, 3, 2, 2)]
    assert historique["f1_val"] == pytest.approx(attendu)

    modele = ModeleScripte(n_val=4, positifs=[2, 2, 4, 4])
    historique, _ = lancer(modele, patience=1, bavard=False)
    assert historique["f1_val"] == pytest.approx([F1_PAR_POSITIFS[2], F1_PAR_POSITIFS[2]])

    modele = ModeleScripte(n_val=4, positifs=[3, 3, 3, 4])
    historique, _ = lancer(modele, patience=2, bavard=False)
    assert historique["f1_val"] == pytest.approx([F1_PAR_POSITIFS[3]] * 3)

    modele = ModeleScripte(n_val=4, positifs=[1, 2, 3, 4])
    historique, _ = lancer(modele, epoques=3, bavard=False)
    assert historique["f1_val"] == pytest.approx([F1_PAR_POSITIFS[k] for k in (1, 2, 3)])
    assert modele.epoque == 3

    modele = ModeleScripte(n_val=4, positifs=[1, 2, 3, 4])
    historique, _ = lancer(modele, epoques=1, bavard=False)
    assert historique["f1_val"] == pytest.approx([F1_PAR_POSITIFS[1]])


@pytest.mark.filterwarnings("ignore::sklearn.exceptions.UndefinedMetricWarning")
def test_entrainer_un_f1_nul_a_la_premiere_epoque_est_deja_le_meilleur_etat():
    modele = ModeleScripte(n_val=4, positifs=[0, 0, 0, 0, 0])
    historique, _ = lancer(modele, patience=2, bavard=False)
    assert historique["f1_val"] == [0.0, 0.0, 0.0]
    assert modele.p.item() == modele.p_fin_epoque[0]


def test_entrainer_rend_le_meilleur_etat_copie():
    """Le modèle revient à sa meilleure époque, et l'état est copié, pas référencé."""
    modele = ModeleScripte(n_val=4, positifs=[2, 3, 3, 2, 2, 2])
    lancer(modele, patience=3, bavard=False)
    meilleure = modele.p_fin_epoque[1]  # la deuxième époque (3 spams sur 4) est la meilleure
    assert modele.p.item() == meilleure
    assert meilleure not in (modele.p_fin_epoque[0], modele.p_fin_epoque[-1])

    # Sans copie, l'état « sauvegardé » suivrait l'entraînement et vaudrait le dernier état.
    modele = ModeleScripte(n_val=4, positifs=[3, 2, 2, 2])
    lancer(modele, patience=3, bavard=False)
    assert modele.p.item() == modele.p_fin_epoque[0]
    assert modele.p_fin_epoque[0] != modele.p_fin_epoque[-1]


def test_entrainer_courbes_de_perte_et_de_f1():
    """Perte et F1 sur l'apprentissage et la validation ; quatre courbes."""
    modele = ModeleScripte(n_val=4, positifs=[2, 2], n_app_positifs=5)
    historique, perte = lancer(modele, epoques=2, bavard=False)
    _, y_app, _, y_val = jeux_jouets()
    z_app = torch.tensor([1.0] * 5 + [-1.0] * 65)
    z_val = torch.tensor([1.0, 1.0, -1.0, -1.0])
    assert historique["perte_app"] == pytest.approx([perte(z_app, y_app).item()] * 2)
    assert historique["perte_val"] == pytest.approx([perte(z_val, y_val).item()] * 2)
    assert historique["f1_app"] == pytest.approx([f1_score(y_app.numpy(), (z_app > 0).numpy())] * 2)
    assert historique["f1_app"][0] == pytest.approx(2 / 3)  # 5 spams trouvés sur 10, aucun faux
    assert historique["f1_val"] == pytest.approx([F1_PAR_POSITIFS[2]] * 2)

    modele = ModeleScripte(n_val=4, positifs=[2, 2])
    historique, _ = lancer(modele, epoques=2, bavard=False)
    assert set(historique) == {"perte_app", "perte_val", "f1_app", "f1_val"}


def test_entrainer_lots_de_64_melanges_par_la_graine():
    """Lots de 64, messages mélangés, ordre dépendant de la graine."""
    modele = ModeleScripte(n_val=4, positifs=[1, 1])
    lancer(modele, epoques=1, augmentation=False, bavard=False)
    assert [lot.shape[0] for lot in modele.lots_vus] == [64, 6]
    vus = torch.cat([lot[:, 0] for lot in modele.lots_vus])
    assert sorted(vus.tolist()) == list(range(70))
    assert vus.tolist() != list(range(70))  # ordre mélangé

    def ordre(graine):
        modele = ModeleScripte(n_val=4, positifs=[1, 1])
        lancer(modele, graine=graine, epoques=1, augmentation=False, bavard=False)
        return torch.cat([lot[:, 0] for lot in modele.lots_vus]).tolist()

    assert ordre(3) == ordre(3)
    assert ordre(3) != ordre(4)


def test_entrainer_le_taux_d_apprentissage_est_un_millieme():
    x_app, y_app, x_val, y_val = jeux_jouets(n_app=10)
    modele = ModeleScripte(n_val=4, positifs=[2], n_app_positifs=5)
    perte = creer_perte(y_app)
    entrainer(
        modele, x_app, y_app, x_val, y_val, perte, Augmenteur(4, 0), graine=0, epoques=1,
        bavard=False,
    )
    # un seul lot, donc un seul pas d'Adam : le paramètre bouge d'environ le taux d'apprentissage
    assert abs(modele.p_fin_epoque[0] - 1.0) == pytest.approx(1e-3, rel=1e-3)


class AugmenteurEspion:
    """Faux augmenteur : note la graine reçue et remplace chaque lot par des zéros."""

    def __init__(self):
        self.graines = []
        self.lots = []

    def reinitialiser(self, graine):
        self.graines.append(graine)

    def augmenter_lot(self, x, y):
        self.lots.append((x.clone(), y.clone()))
        return torch.zeros_like(x)


def test_entrainer_applique_ou_non_l_augmentation():
    """Avec augmentation, le réseau s'entraîne sur son résultat ; sans, l'augmenteur n'est pas
    sollicité."""
    x_app, y_app, x_val, y_val = jeux_jouets()
    espion = AugmenteurEspion()
    modele = ModeleScripte(n_val=4, positifs=[1])
    entrainer(
        modele, x_app, y_app, x_val, y_val, creer_perte(y_app), espion, graine=9, epoques=1,
        bavard=False,
    )
    assert espion.graines == [9]
    assert [x.shape[0] for x, _ in espion.lots] == [64, 6]
    assert all(torch.equal(lot, torch.zeros_like(lot)) for lot in modele.lots_vus)

    x_app, y_app, x_val, y_val = jeux_jouets()
    espion = AugmenteurEspion()
    modele = ModeleScripte(n_val=4, positifs=[1])
    entrainer(
        modele, x_app, y_app, x_val, y_val, creer_perte(y_app), espion, graine=9, epoques=1,
        augmentation=False, bavard=False,
    )
    assert espion.lots == []
    assert espion.graines == [9]
    assert any(lot.sum() > 0 for lot in modele.lots_vus)


def test_entrainer_affiche_la_progression_si_bavard(capsys):
    """Une ligne par époque puis l'arrêt précoce ; rien si `bavard` est faux."""
    modele = ModeleScripte(n_val=4, positifs=[2, 3, 3, 2, 2, 2])
    historique, _ = lancer(modele, patience=3, nom="Scripté")
    sortie = capsys.readouterr().out.splitlines()
    attendu = [
        ligne_epoque("Scripté", e, {cle: valeurs[:e] for cle, valeurs in historique.items()})
        for e in range(1, len(historique["f1_val"]) + 1)
    ]
    attendu.append("Scripté arrêt précoce : pas de progrès depuis 3 époques")
    assert sortie == attendu

    modele = ModeleScripte(n_val=4, positifs=[1])
    lancer(modele, epoques=1)
    assert capsys.readouterr().out.startswith(" époque  1 | perte app")

    modele = ModeleScripte(n_val=4, positifs=[2, 3, 3, 2, 2, 2])
    lancer(modele, patience=3, bavard=False)
    assert capsys.readouterr().out == ""

    modele = ModeleScripte(n_val=4, positifs=[1, 2])
    lancer(modele, epoques=2)
    assert "arrêt précoce" not in capsys.readouterr().out


@pytest.mark.parametrize("epoques", [0, -1])
def test_entrainer_refuse_moins_d_une_epoque(epoques):
    modele = ModeleScripte(n_val=4, positifs=[1])
    with pytest.raises(ValueError) as erreur:
        lancer(modele, epoques=epoques, bavard=False)
    assert str(erreur.value) == "il faut au moins une époque pour retenir un meilleur état"
    assert modele.epoque == 0  # rien n'a été entraîné


def test_entrainer_laisse_les_donnees_d_entree_inchangees():
    x_app, y_app, x_val, y_val = jeux_jouets()
    copies = [t.clone() for t in (x_app, y_app, x_val, y_val)]
    entrainer(
        ModeleScripte(n_val=4, positifs=[1]), x_app, y_app, x_val, y_val, creer_perte(y_app),
        Augmenteur(4, 0), graine=0, epoques=1, bavard=False,
    )
    assert all(torch.equal(t, c) for t, c in zip((x_app, y_app, x_val, y_val), copies, strict=True))


# ---------------------------------------------------------------- entrainer (vrai réseau)


def jouet_textuel():
    """Messages de 8 mots : les spams contiennent le mot 2, les légitimes le mot 3."""
    generateur = torch.Generator().manual_seed(0)
    x = torch.randint(4, 30, (80, 8), generator=generateur)
    y = (torch.arange(80) % 4 == 0).float()
    x[:, 3] = torch.where(y == 1, 2, 3)
    return x[:60], y[:60], x[60:], y[60:]


def test_entrainer_un_vrai_reseau_apprend_et_reste_reproductible():
    """Un TextCNN apprend un signal évident ; deux exécutions sous la même graine donnent la même
    histoire."""
    x, y, x_val, y_val = jouet_textuel()
    torch.manual_seed(1)
    modele = TextCNN(taille_vocab=30)
    historique = entrainer(
        modele, x, y, x_val, y_val, creer_perte(y), Augmenteur(8, 0), graine=0, epoques=8,
        bavard=False, augmentation=False,
    )
    assert max(historique["f1_val"]) == pytest.approx(1.0)
    f1_final = f1_score(y_val.numpy(), probabilites(modele, x_val) >= 0.5)
    assert f1_final == pytest.approx(max(historique["f1_val"]))
    assert historique["perte_app"][-1] < historique["perte_app"][0]

    x, y, x_val, y_val = jouet_textuel()

    def histoire(graine):
        torch.manual_seed(1)
        modele = BiGRU(taille_vocab=30, dim=8, cache=4)
        return entrainer(
            modele, x, y, x_val, y_val, creer_perte(y), Augmenteur(8, 0), graine=graine,
            epoques=3, bavard=False,
        )

    assert histoire(5) == histoire(5)
    assert histoire(5) != histoire(6)




# ---------------------------------------------------------------- entrées refusées
# Chaque test ci-dessous porte sur une entrée interdite ou absurde : elle doit être refusée par
# une exception, ou laisser les données de l'appelant exactement comme elles étaient.


def test_creer_perte_refuse_un_jeu_sans_spam():
    with pytest.raises(ValueError):
        creer_perte(torch.zeros(4))


def test_poids_spam_laisse_les_cibles_inchangees():
    y = torch.tensor([0.0, 1.0, 0.0])
    avant = y.clone()
    poids_spam(y)
    creer_perte(y)
    assert torch.equal(y, avant)


def test_perte_refuse_des_scores_et_des_cibles_de_tailles_differentes():
    perte = creer_perte(torch.tensor([0.0, 1.0]))
    with pytest.raises(ValueError):
        perte(torch.zeros(3), torch.zeros(2))


@pytest.mark.parametrize("modele", [TextCNN, BiGRU])
def test_reseau_refuse_un_indice_hors_du_vocabulaire(modele):
    reseau = modele(taille_vocab=10).eval()
    with pytest.raises(IndexError):
        reseau(torch.full((2, 8), 10, dtype=torch.long))


@pytest.mark.parametrize("modele", [TextCNN, BiGRU])
def test_reseau_refuse_des_indices_non_entiers(modele):
    reseau = modele(taille_vocab=10).eval()
    with pytest.raises(RuntimeError):
        reseau(torch.ones(2, 8))


@pytest.mark.parametrize("modele", [TextCNN, BiGRU])
def test_reseau_refuse_un_vocabulaire_sans_place_pour_le_remplissage(modele):
    with pytest.raises(IndexError):
        modele(taille_vocab=0)


@pytest.mark.parametrize("modele", [TextCNN, BiGRU])
def test_reseau_laisse_la_matrice_d_entree_inchangee(modele):
    x = torch.randint(1, 10, (3, 8))
    avant = x.clone()
    reseau = modele(taille_vocab=10)
    reseau(x)
    probabilites(reseau, x)
    assert torch.equal(x, avant)


def test_probabilites_refuse_une_entree_sans_dimension_de_lot():
    with pytest.raises((IndexError, RuntimeError)):
        probabilites(TextCNN(taille_vocab=10), torch.ones(8, dtype=torch.long))


def test_entrainer_va_par_defaut_jusqu_a_quinze_epoques():
    """Sans l'argument `epoques`, la boucle s'arrête à 15 (une patience large l'y autorise)."""
    modele = ModeleScripte(n_val=4, positifs=[3] * 20)
    historique, _ = lancer(modele, patience=100, bavard=False)
    assert historique["f1_val"] == pytest.approx([F1_PAR_POSITIFS[3]] * 15)


def test_entrainer_la_patience_par_defaut_est_de_trois_epoques():
    modele = ModeleScripte(n_val=4, positifs=[3, 2, 2, 2, 4, 4])
    historique, _ = lancer(modele, bavard=False)
    assert historique["f1_val"] == pytest.approx([F1_PAR_POSITIFS[k] for k in (3, 2, 2, 2)])


def test_ligne_epoque_refuse_un_historique_vide():
    vide = {"perte_app": [], "perte_val": [], "f1_app": [], "f1_val": []}
    with pytest.raises(IndexError):
        ligne_epoque("TextCNN", 1, vide)


class ModeleToujoursNul(nn.Module):
    """Modèle qui rend toujours un score de 0 : ni spam, ni légitime, juste sur la frontière."""

    def __init__(self):
        super().__init__()
        self.p = nn.Parameter(torch.tensor(1.0))

    def forward(self, x):
        return self.p * torch.zeros(len(x))


@pytest.mark.filterwarnings("ignore::sklearn.exceptions.UndefinedMetricWarning")
def test_entrainer_un_score_nul_n_est_pas_un_spam():
    x_app, y_app, x_val, y_val = jeux_jouets()
    historique = entrainer(
        ModeleToujoursNul(), x_app, y_app, x_val, y_val, creer_perte(y_app), Augmenteur(4, 0),
        graine=0, epoques=1, bavard=False,
    )  # fmt: skip
    # score 0 : probabilité 0,5, strictement non positif : personne n'est déclaré spam
    assert historique["f1_app"] == [0.0]
    assert historique["f1_val"] == [0.0]


def test_entrainer_refuse_des_cibles_de_longueur_differente_des_messages():
    x_app, y_app, x_val, y_val = jeux_jouets()
    with pytest.raises(AssertionError):
        entrainer(
            ModeleScripte(n_val=4, positifs=[1]), x_app, y_app[:60], x_val, y_val,
            creer_perte(y_app), Augmenteur(4, 0), graine=0, epoques=1, bavard=False,
        )  # fmt: skip


def test_entrainer_refuse_une_graine_qui_n_est_pas_un_entier():
    x_app, y_app, x_val, y_val = jeux_jouets()
    modele = ModeleScripte(n_val=4, positifs=[1])
    with pytest.raises(ValueError):
        entrainer(
            modele, x_app, y_app, x_val, y_val, creer_perte(y_app), Augmenteur(4, 0),
            graine="graine", epoques=1, bavard=False,
        )  # fmt: skip
    assert modele.epoque == 0  # aucune époque n'a été entraînée


def test_entrainer_refuse_une_validation_incompatible_avec_le_reseau():
    x, y, _, y_val = jouet_textuel()
    modele = TextCNN(taille_vocab=30)
    trop_court = torch.randint(4, 30, (y_val.shape[0], 2))  # plus court que le filtre de 5 mots
    with pytest.raises(RuntimeError):
        entrainer(
            modele, x, y, trop_court, y_val, creer_perte(y), Augmenteur(8, 0), graine=0,
            epoques=1, bavard=False,
        )  # fmt: skip


def test_probabilites_rend_toujours_du_float32_meme_pour_un_modele_en_double_precision():
    modele = TextCNN(taille_vocab=20).double()
    x = torch.randint(1, 20, (3, 8))
    probas = probabilites(modele, x)
    assert probas.dtype == np.float32
    assert ((probas > 0) & (probas < 1)).all()


def test_entrainer_utilise_adam_a_un_millieme_et_l_ordre_des_lots_de_la_graine(monkeypatch):
    options_adam = []
    adam_reel = torch.optim.Adam

    def espion_adam(parametres, **options):
        options_adam.append(options)
        return adam_reel(parametres, **options)

    monkeypatch.setattr(torch.optim, "Adam", espion_adam)
    modele = ModeleScripte(n_val=4, positifs=[1])
    lancer(modele, graine=5, epoques=1, augmentation=False, bavard=False)
    assert options_adam == [{"lr": 1e-3}]
    x_app, y_app, _, _ = jeux_jouets()
    attendu = DataLoader(
        TensorDataset(x_app, y_app), batch_size=64, shuffle=True,
        generator=torch.Generator().manual_seed(5),
    )  # fmt: skip
    ordre_attendu = torch.cat([lot_x[:, 0] for lot_x, _ in attendu])
    assert torch.cat([lot[:, 0] for lot in modele.lots_vus]).tolist() == ordre_attendu.tolist()


def test_probabilites_refuse_des_indices_hors_du_vocabulaire():
    with pytest.raises(IndexError):
        probabilites(BiGRU(taille_vocab=10), torch.full((2, 8), 10, dtype=torch.long))


def test_probabilites_laisse_les_poids_du_modele_inchanges():
    modele = TextCNN(taille_vocab=10)
    avant = [p.clone() for p in modele.parameters()]
    probabilites(modele, torch.randint(1, 10, (3, 8)))
    assert all(torch.equal(p, q) for p, q in zip(modele.parameters(), avant, strict=True))
