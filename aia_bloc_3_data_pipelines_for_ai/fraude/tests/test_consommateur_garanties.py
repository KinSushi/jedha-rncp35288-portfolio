# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Consommateur : rejets exacts, atomicité, relivraison, bornes de dates et boucle d'arrêt."""

from __future__ import annotations

import copy
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

from detection_fraude import VERSION_REGLES
from detection_fraude import consommateur as module_consommateur
from detection_fraude.consommateur import (
    BilanLot,
    Consommateur,
    _date_evenement,
    construire_ligne_bronze,
    enrichir,
    ligne_alerte,
    noter_lignes,
)
from detection_fraude.file_attente import FileSQLite, Message
from detection_fraude.fonctionnalites import COLONNES_MODELE, EtatCartes
from detection_fraude.masquage import masquer
from detection_fraude.modele import ErreurModele
from detection_fraude.notification import Notificateur
from tests.helpers import MARQUEUR, CanalEnregistreur, ChargeurFactice, ligne_brute

FORME_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
HORODATAGE_0 = int(datetime(2026, 10, 9, 12, 0, tzinfo=UTC).timestamp())
ILLISIBLE = {"champ": "*", "code": "charge_illisible", "detail": ""}
COUCHES = ("bronze_transactions", "argent_transactions", "or_alertes_fraude")
INCONNUS_D_UN_MESSAGE_VALIDE = [
    "age",
    "carte_fin",
    "carte_hash",
    "commercant",
    "distance_km",
    "etiquette_source",
    "lat_commercant",
    "long_commercant",
    "population_ville",
    "source",
    "version_schema",
]


# --------------------------------------------------------------------------------------------
# Doublures et utilitaires
# --------------------------------------------------------------------------------------------
class FileEspion:
    """Enveloppe une vraie file et note les lectures, publications et validations."""

    def __init__(self, vraie: FileSQLite, journal: list | None = None) -> None:
        self.vraie = vraie
        self.journal = journal if journal is not None else []
        self.lectures: list[tuple] = []
        self.publications: list[tuple] = []
        self.validations: list[tuple] = []
        self.avant_validation = None  # appelé juste avant chaque validation

    def publier(self, sujet, valeur, cle=None):
        self.publications.append((sujet, valeur, cle))
        self.vraie.publier(sujet, valeur, cle=cle)

    def lire(self, sujet, groupe, max_messages=500, attente_s=1.0):
        self.lectures.append((sujet, groupe, max_messages, attente_s))
        self.journal.append(f"lire:{sujet}")
        return self.vraie.lire(sujet, groupe, max_messages, attente_s)

    def valider(self, sujet, groupe, messages):
        if self.avant_validation:
            self.avant_validation()
        self.validations.append((sujet, groupe, [m.position for m in messages]))
        self.vraie.valider(sujet, groupe, messages)

    def retard(self, sujet, groupe):
        return self.vraie.retard(sujet, groupe)

    def vider(self):
        self.vraie.vider()

    def fermer(self):
        self.vraie.fermer()


class HorlogeMonotone:
    """Remplace le module ``time`` du consommateur : ``monotonic`` rend des valeurs imposées."""

    def __init__(self, valeurs: list[float]) -> None:
        self.valeurs = iter(valeurs)

    def monotonic(self) -> float:
        return next(self.valeurs)


class FileScriptee:
    """File factice : ``lire`` rend toujours les mêmes messages, sans jamais attendre."""

    def __init__(self, messages: list[Message] | None = None) -> None:
        self.messages = messages or []
        self.lectures: list[tuple] = []
        self.validations: list[tuple] = []

    def lire(self, sujet, groupe, max_messages=500, attente_s=1.0):
        self.lectures.append((sujet, groupe, max_messages, attente_s))
        return list(self.messages)

    def valider(self, sujet, groupe, messages):
        self.validations.append((sujet, groupe, [m.position for m in messages]))

    def publier(self, sujet, valeur, cle=None):
        raise AssertionError("aucune publication attendue")


@contextmanager
def chien_de_garde(arreter: threading.Event, delai: float = 1.0):
    """Lève l'arrêt après ``delai`` s : une boucle qui ne s'arrête pas ne bloque pas le test."""
    minuteur = threading.Timer(delai, arreter.set)
    minuteur.daemon = True
    minuteur.start()
    try:
        yield
    finally:
        minuteur.cancel()


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


def valeur(hacheur, rang: int, **brut: Any) -> dict[str, Any]:
    return masquer(ligne_brute(rang, **brut), hacheur, source="jeu")


def publier(file, config, *valeurs: Any) -> None:
    for v in valeurs:
        cle = v.get("carte_hash") if isinstance(v, dict) else None
        file.publier(config.file.sujet_valides, v, cle=cle)


def lot(file, config, maximum: int = 500) -> list[Message]:
    """Lit comme le consommateur (même groupe) : ces messages portent les positions réelles."""
    return file.lire(config.file.sujet_valides, config.file.groupe_consommateur, maximum, 0)


def rejets_de_la_file(file, config) -> list[Message]:
    return file.lire(config.file.sujet_rejets, "inspection", 1000, 0)


def couches(entrepot) -> list[list[dict[str, Any]]]:
    return [entrepot.requete(f"SELECT * FROM {t} ORDER BY 1") for t in COUCHES]


def lignage(entrepot) -> list[dict[str, Any]]:
    return entrepot.requete("SELECT * FROM meta_lignage ORDER BY id")


def lignage_du_lot(entrepot, lot_id: str) -> dict[str, Any]:
    """La seule ligne de lignage d'un lot (retrouvée par son identifiant, pas par son rang)."""
    (ligne,) = [x for x in lignage(entrepot) if json.loads(x["details"])["lot_id"] == lot_id]
    return ligne


def alerte_en_base(entrepot, rang: int, **surcharges: Any) -> dict[str, Any]:
    """Insère une alerte complète (construite par ``ligne_alerte``) et la renvoie."""
    argent = {
        "trans_num": f"{rang:032x}",
        "date_transaction": "2026-10-09",
        "horodatage_utc": "2026-10-09T12:00:00+00:00",
        "carte_fin": "0099",
        "commercant": "Ancien commerce",
        "categorie": "misc_net",
        "montant": 700.0,
        "score": 0.9,
        "seuil": 0.5,
        "version_modele": "factice-1",
        "source": "jeu",
    }
    alerte = {**ligne_alerte(argent, 1.0), **surcharges}
    with entrepot.transaction() as conn:
        entrepot.inserer_alertes(conn, [alerte])
    return alerte


def sans_date(rejet: dict[str, Any]) -> dict[str, Any]:
    """Le rejet sans sa date (vérifiée au passage : ISO UTC)."""
    copie = dict(rejet)
    assert FORME_ISO.match(copie.pop("rejete_le"))
    return copie


def rejet_attendu(
    position, erreurs, *, trans_num=None, source=None, conserves=None, inconnus=None
):
    return {
        "version_schema": 1,
        "etape": "consommateur",
        "raison": "contrat_file_viole",
        "erreurs": erreurs,
        "trans_num": trans_num,
        "source": source,
        "position_origine": position,
        "charge_utile_masquee": {
            "champs_conserves": conserves or {},
            "champs_inconnus": inconnus or [],
        },
    }


def evenements_rejet(evenements) -> list[tuple]:
    return [e for e in evenements.tous() if e[1] == "message_rejete"]


# --------------------------------------------------------------------------------------------
# Contrat de la file : messages rejetés
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("charge", [["a", "b"], "texte", 42, None])
def test_un_message_qui_n_est_pas_un_objet_est_rejete_comme_illisible_et_rien_n_est_ecrit(
    config, file, entrepot, consommateur, evenements, charge
):
    """Une charge qui n'est pas un objet JSON ne peut pas être contrôlée : DLQ, aucune écriture."""
    couches_avant = couches(entrepot)
    file.publier(config.file.sujet_valides, charge)
    messages = lot(file, config)
    bilan = consommateur.traiter_lot(messages)
    assert (bilan.recus, bilan.nouveaux, bilan.doublons, bilan.rejets, bilan.fraudes) == (
        1,
        0,
        0,
        1,
        0,
    )
    (rejet,) = [m.valeur for m in rejets_de_la_file(file, config)]
    assert sans_date(rejet) == rejet_attendu(messages[0].position, [ILLISIBLE])
    assert couches(entrepot) == couches_avant
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 0
    assert evenements_rejet(evenements) == [
        (
            "WARNING",
            "message_rejete",
            {"raison": "contrat_file_viole", "trans_num": None, "codes": "charge_illisible"},
        )
    ]


def test_la_charge_marquee_illisible_par_la_file_est_rejetee_sans_rien_ecrire(
    config, file, entrepot, consommateur
):
    """Kafka marque ``__illisible__`` une charge non décodable : DLQ, avec son seul nom."""
    couches_avant = couches(entrepot)
    file.publier(config.file.sujet_valides, {"__illisible__": True})
    messages = lot(file, config)
    bilan = consommateur.traiter_lot(messages)
    assert (bilan.nouveaux, bilan.rejets) == (0, 1)
    (rejet,) = [m.valeur for m in rejets_de_la_file(file, config)]
    assert sans_date(rejet) == rejet_attendu(
        messages[0].position, [ILLISIBLE], inconnus=["__illisible__"]
    )
    assert couches(entrepot) == couches_avant


def test_un_message_valide_portant_le_marqueur_illisible_est_quand_meme_rejete(
    config, file, entrepot, consommateur, hacheur
):
    """Le marqueur suffit au rejet, même si tout le reste est conforme : rien n'entre en base."""
    couches_avant = couches(entrepot)
    intrus = valeur(hacheur, 1)
    intrus["__illisible__"] = True
    publier(file, config, intrus)
    messages = lot(file, config)
    consommateur.traiter_lot(messages)
    (rejet,) = [m.valeur for m in rejets_de_la_file(file, config)]
    assert rejet["erreurs"] == [ILLISIBLE]
    assert rejet["trans_num"] == intrus["trans_num"]
    assert "__illisible__" in rejet["charge_utile_masquee"]["champs_inconnus"]
    assert couches(entrepot) == couches_avant


def test_un_champ_inattendu_est_rejete_nomme_sans_sa_valeur_et_n_entre_pas_en_base(
    config, file, entrepot, consommateur, hacheur, evenements
):
    """Un champ sensible échappé au masquage arrête le message ; seul son nom est consigné."""
    couches_avant = couches(entrepot)
    intrus = valeur(hacheur, 1)
    intrus["first"] = f"Prenom{MARQUEUR}"
    publier(file, config, intrus)
    messages = lot(file, config)
    bilan = consommateur.traiter_lot(messages)
    assert (bilan.nouveaux, bilan.rejets) == (0, 1)
    rejets = rejets_de_la_file(file, config)
    assert sans_date(rejets[0].valeur) == rejet_attendu(
        messages[0].position,
        [{"champ": "first", "code": "champ_inattendu", "detail": ""}],
        trans_num=intrus["trans_num"],
        source="jeu",
        conserves={
            "montant": intrus["montant"],
            "categorie": intrus["categorie"],
            "etat": "NY",
            "horodatage_s": intrus["horodatage_s"],
        },
        inconnus=sorted([*INCONNUS_D_UN_MESSAGE_VALIDE, "first"]),
    )
    assert rejets[0].cle == intrus["trans_num"]  # le rejet est rangé sous le trans_num
    assert MARQUEUR not in json.dumps([r.valeur for r in rejets])
    assert couches(entrepot) == couches_avant
    assert evenements_rejet(evenements) == [
        (
            "WARNING",
            "message_rejete",
            {
                "raison": "contrat_file_viole",
                "trans_num": intrus["trans_num"],
                "codes": "champ_inattendu",
            },
        )
    ]


def test_plusieurs_violations_sont_toutes_consignees_dans_l_ordre_du_schema_et_codes_tries(
    config, file, entrepot, consommateur, hacheur, evenements
):
    """Toutes les raisons sont listées (ordre du contrat) ; le journal trie et dédoublonne."""
    couches_avant = couches(entrepot)
    abime = valeur(hacheur, 1)
    del abime["montant"]
    del abime["age"]
    abime["etat"] = "ny"
    publier(file, config, abime)
    consommateur.traiter_lot(lot(file, config))
    (rejet,) = [m.valeur for m in rejets_de_la_file(file, config)]
    assert rejet["erreurs"] == [
        {"champ": "montant", "code": "absent", "detail": ""},
        {"champ": "etat", "code": "format_invalide", "detail": ""},
        {"champ": "age", "code": "absent", "detail": ""},
    ]
    assert rejet["charge_utile_masquee"]["champs_conserves"] == {
        "categorie": abime["categorie"],
        "etat": "ny",
        "horodatage_s": abime["horodatage_s"],
    }
    assert evenements_rejet(evenements) == [
        (
            "WARNING",
            "message_rejete",
            {
                "raison": "contrat_file_viole",
                "trans_num": abime["trans_num"],
                "codes": "absent,format_invalide",
            },
        )
    ]
    assert couches(entrepot) == couches_avant


def test_un_trans_num_qui_n_est_pas_du_texte_n_est_pas_recopie_dans_le_rejet(
    config, file, entrepot, consommateur, hacheur, evenements
):
    """Seul un ``trans_num`` textuel sert de clé de rejet ; un entier est ignoré (clé vide)."""
    couches_avant = couches(entrepot)
    bizarre = valeur(hacheur, 1)
    bizarre["trans_num"] = 12345
    publier(file, config, bizarre)
    consommateur.traiter_lot(lot(file, config))
    (rejet,) = rejets_de_la_file(file, config)
    assert rejet.cle is None and rejet.valeur["trans_num"] is None
    assert rejet.valeur["erreurs"] == [
        {"champ": "trans_num", "code": "type_invalide", "detail": ""}
    ]
    assert "trans_num" not in rejet.valeur["charge_utile_masquee"]["champs_conserves"]
    assert "trans_num" not in rejet.valeur["charge_utile_masquee"]["champs_inconnus"]
    assert evenements_rejet(evenements)[0][2]["trans_num"] is None
    assert couches(entrepot) == couches_avant


def test_le_rejet_ne_conserve_que_les_champs_simples_connus_et_trie_les_inconnus(
    config, file, entrepot, consommateur
):
    """Textes et nombres des quatre champs sûrs sont gardés ; listes, vides et dicts non."""
    couches_avant = couches(entrepot)
    file.publier(
        config.file.sujet_valides,
        {"montant": 12.5, "categorie": "x", "etat": 5, "horodatage_s": "x", "zz": 1, "aa": 2},
    )
    file.publier(
        config.file.sujet_valides,
        {
            "montant": [1],
            "categorie": None,
            "etat": {"a": 1},
            "horodatage_s": 1.5,
            "trans_num": "t" * 32,
            "source": "jeu",
        },
    )
    messages = lot(file, config)
    consommateur.traiter_lot(messages)
    premier, second = [m.valeur for m in rejets_de_la_file(file, config)]
    assert premier["charge_utile_masquee"] == {
        "champs_conserves": {"montant": 12.5, "categorie": "x", "etat": 5, "horodatage_s": "x"},
        "champs_inconnus": ["aa", "zz"],
    }
    assert second["charge_utile_masquee"] == {
        "champs_conserves": {"horodatage_s": 1.5},
        "champs_inconnus": ["source"],
    }
    assert (premier["trans_num"], second["trans_num"]) == (None, "t" * 32)
    assert (premier["source"], second["source"]) == (None, "jeu")
    assert (premier["position_origine"], second["position_origine"]) == (
        messages[0].position,
        messages[1].position,
    )
    assert couches(entrepot) == couches_avant


# --------------------------------------------------------------------------------------------
# Lots : vide, relivré, doublons, résultat complet
# --------------------------------------------------------------------------------------------
def test_un_lot_vide_ne_touche_a_rien_et_laisse_la_base_et_la_file_intactes(
    config, file, entrepot, evenements
):
    """Aucun message : bilan à zéro, pas de modèle chargé, pas de lignage, pas de validation."""
    chargeur_appels = []

    class ChargeurEspion(ChargeurFactice):
        def obtenir(self):
            chargeur_appels.append("obtenir")
            return super().obtenir()

    espion = FileEspion(file)
    consommateur = Consommateur(
        config, espion, entrepot, ChargeurEspion(), Notificateur([CanalEnregistreur()])
    )
    couches_avant, lignage_avant = couches(entrepot), lignage(entrepot)
    bilan = consommateur.traiter_lot([])
    assert (bilan.recus, bilan.nouveaux, bilan.doublons, bilan.rejets, bilan.fraudes) == (0,) * 5
    assert (bilan.duree_ms, bilan.details) == (0, {})
    assert re.fullmatch(r"[0-9a-f]{12}", bilan.lot_id)
    assert consommateur.traiter_lot([]).lot_id != bilan.lot_id  # un identifiant par lot
    assert chargeur_appels == []
    assert espion.validations == [] and espion.publications == []
    assert couches(entrepot) == couches_avant and lignage(entrepot) == lignage_avant
    assert evenements.tous() == []


def test_la_relivraison_d_un_lot_deja_traite_ne_change_aucune_couche_et_ne_renvoie_rien(
    config, file, entrepot, consommateur, canal, hacheur, evenements, monkeypatch
):
    """Une relivraison compte trois doublons, n'écrit que son lignage et n'envoie aucune alerte."""
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2), valeur(hacheur, 3, amt=900.0))
    messages = lot(file, config)
    premier = consommateur.traiter_lot(messages)
    consommateur.envoyer_alertes()
    couches_avant, recus_avant = couches(entrepot), list(canal.recus)
    # Horloge du consommateur remplacée (elle seule) : le lot dure exactement 2 s, soit 2000 ms.
    with monkeypatch.context() as local:
        local.setattr(module_consommateur, "time", HorlogeMonotone([100.0, 102.0]))
        second = consommateur.traiter_lot(messages)  # la validation s'était perdue : tout revient
    consommateur.envoyer_alertes()
    assert second.duree_ms == 2000
    assert (premier.nouveaux, premier.doublons, premier.fraudes) == (3, 0, 1)
    assert (second.recus, second.nouveaux, second.doublons, second.rejets, second.fraudes) == (
        3,
        0,
        3,
        0,
        0,
    )
    assert couches(entrepot) == couches_avant
    assert canal.recus == recus_avant
    ligne_du_rejeu = lignage_du_lot(entrepot, second.lot_id)
    assert (ligne_du_rejeu["nb_entrees"], ligne_du_rejeu["nb_sorties"]) == (3, 0)
    assert json.loads(ligne_du_rejeu["details"]) == {
        "lot_id": second.lot_id,
        "doublons": 3,
        "fraudes": 0,
        "duree_ms": 2000,
    }
    dernier = [e for e in evenements.tous() if e[1] == "lot_traite"][-1]
    assert dernier[2]["nouveaux"] == 0 and dernier[2]["doublons"] == 3
    assert dernier[2]["duree_ms"] == 2000


def test_les_doublons_du_lot_et_de_l_entrepot_s_additionnent_et_le_premier_exemplaire_gagne(
    config, file, entrepot, consommateur, hacheur
):
    """Un doublon dans le lot et un déjà stocké comptent chacun ; les lignes stockées restent."""
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2))
    consommateur.traiter_lot(lot(file, config))
    bronze_avant, argent_avant, _ = couches(entrepot)
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2))
    publier(file, config, valeur(hacheur, 3), valeur(hacheur, 3))  # nouvelle, publiée deux fois
    messages = lot(file, config)
    bilan = consommateur.traiter_lot(messages)
    assert (bilan.recus, bilan.nouveaux, bilan.doublons, bilan.rejets) == (4, 1, 3, 0)
    bronze, argent, _ = couches(entrepot)
    trois = valeur(hacheur, 3)["trans_num"]
    (nouvelle,) = [b for b in bronze if b["trans_num"] == trois]
    assert nouvelle["position_file"] == messages[2].position  # le premier exemplaire a gagné
    assert [b for b in bronze if b["trans_num"] != trois] == bronze_avant
    assert [a for a in argent if a["trans_num"] != trois] == argent_avant


def test_les_messages_arrives_en_desordre_sont_traites_dans_l_ordre_du_temps(
    config, file, entrepot, consommateur, hacheur
):
    """Une transaction ancienne publiée en dernier ne voit pas les plus récentes (pas de fuite)."""
    publier(file, config, valeur(hacheur, 7))  # une autre carte, déjà stockée
    consommateur.traiter_lot(lot(file, config))
    bronze_avant, argent_avant, _ = couches(entrepot)
    carte = {"cc_num": 4_929_000_000_000_042}
    heure = (
        3600  # trois transactions espacées d'une heure, publiées de la plus récente à l'ancienne
    )
    publier(
        file,
        config,
        valeur(hacheur, 3, current_time=(HORODATAGE_0 + 3 * heure) * 1000, amt=60.0, **carte),
        valeur(hacheur, 2, current_time=(HORODATAGE_0 + 2 * heure) * 1000, amt=40.0, **carte),
        valeur(hacheur, 1, current_time=(HORODATAGE_0 + 1 * heure) * 1000, amt=20.0, **carte),
    )
    consommateur.traiter_lot(lot(file, config))
    autre = valeur(hacheur, 7)["trans_num"]
    lignes = entrepot.requete(
        "SELECT horodatage_s, nb_tx_24h, montant_relatif FROM argent_transactions "
        "WHERE trans_num != :autre ORDER BY horodatage_s",
        autre=autre,
    )
    # 20 : historique vide ; 40 : une transaction (moyenne 20) ; 60 : deux (moyenne 30).
    assert [(x["nb_tx_24h"], x["montant_relatif"]) for x in lignes] == [
        (0, 1.0),
        (1, 2.0),
        (2, 2.0),
    ]
    bronze, argent, _ = couches(entrepot)
    assert [
        b for b in bronze if b["trans_num"] == autre
    ] == bronze_avant  # l'autre carte : intacte
    assert [a for a in argent if a["trans_num"] == autre] == argent_avant


def test_un_lot_complet_produit_le_bilan_les_lignes_le_lignage_et_l_evenement_exacts(
    config, file, entrepot, consommateur, canal, hacheur, evenements
):
    """Six messages : rejet, doublon de lot, déjà stocké, fraude : tout est compté exactement."""
    publier(file, config, valeur(hacheur, 1))
    consommateur.traiter_lot(lot(file, config))
    bronze_avant, argent_avant, _ = couches(entrepot)  # la transaction 1 est déjà stockée
    evenements.enregistrements.clear()
    abime = valeur(hacheur, 9)
    del abime["montant"]
    publier(
        file,
        config,
        valeur(hacheur, 2),
        valeur(hacheur, 3, amt=900.0),
        abime,
        valeur(hacheur, 2),  # doublon dans le lot
        valeur(hacheur, 1),  # déjà dans l'entrepôt
        valeur(hacheur, 4),
    )
    messages = lot(file, config)
    bilan = consommateur.traiter_lot(messages)
    assert (bilan.recus, bilan.nouveaux, bilan.doublons, bilan.rejets, bilan.fraudes) == (
        6,
        3,
        2,
        1,
        1,
    )
    assert re.fullmatch(r"[0-9a-f]{12}", bilan.lot_id)
    assert isinstance(bilan.duree_ms, int) and 0 <= bilan.duree_ms < 60_000
    lot_traite = [e for e in evenements.tous() if e[1] == "lot_traite"]
    assert lot_traite == [
        (
            "INFO",
            "lot_traite",
            {
                "lot": bilan.lot_id,
                "recus": 6,
                "nouveaux": 3,
                "doublons": 2,
                "rejets": 1,
                "fraudes": 1,
                "duree_ms": bilan.duree_ms,
                "modele": "factice-1",
            },
        )
    ]
    ligne = lignage_du_lot(entrepot, bilan.lot_id)
    assert ligne["etape"] == "consommateur"
    assert ligne["entree"] == "file:transactions.valides"
    assert ligne["sortie"] == "bronze_transactions,argent_transactions,or_alertes_fraude"
    assert (ligne["nb_entrees"], ligne["nb_sorties"], ligne["nb_rejets"]) == (6, 3, 1)
    assert FORME_ISO.match(ligne["debut"]) and FORME_ISO.match(ligne["fin"])
    assert (ligne["version_regles"], ligne["version_modele"]) == (VERSION_REGLES, "factice-1")
    assert json.loads(ligne["details"]) == {
        "lot_id": bilan.lot_id,
        "doublons": 2,
        "fraudes": 1,
        "duree_ms": bilan.duree_ms,
    }
    (alerte,) = entrepot.requete("SELECT * FROM or_alertes_fraude")
    assert alerte["trans_num"] == valeur(hacheur, 3)["trans_num"]
    assert alerte["statut_notification"] == "a_envoyer" and alerte["tentatives"] == 0
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 0
    bronze, argent, _ = couches(entrepot)
    premiere = valeur(hacheur, 1)["trans_num"]
    assert [b for b in bronze if b["trans_num"] == premiere] == bronze_avant
    assert [a for a in argent if a["trans_num"] == premiere] == argent_avant


def test_les_lignes_bronze_et_argent_reprennent_chaque_champ_du_message(
    config, file, entrepot, consommateur, hacheur
):
    """Chaque colonne bronze/argent vient du bon champ du message ; le bronze garde « fraud_ »."""
    v = valeur(hacheur, 1, amt=45.0)
    publier(file, config, v)
    messages = lot(file, config)
    valeurs_avant = [dict(m.valeur) for m in messages]
    bilan = consommateur.traiter_lot(messages)
    assert [m.valeur for m in messages] == valeurs_avant  # le traitement ne modifie pas le message
    (bronze,) = entrepot.requete("SELECT * FROM bronze_transactions")
    assert FORME_ISO.match(bronze.pop("recu_le"))
    assert bronze == {
        "trans_num": v["trans_num"],
        "version_schema": 1,
        "source": "jeu",
        "horodatage_s": v["horodatage_s"],
        "carte_hash": v["carte_hash"],
        "carte_fin": "0001",
        "commercant": "fraud_Marchand 1",
        "categorie": v["categorie"],
        "montant": 45.0,
        "etat": "NY",
        "population_ville": 8_000_000,
        "age": 41,
        "distance_km": v["distance_km"],
        "lat_commercant": 40.8,
        "long_commercant": -73.95,
        "etiquette_source": 0,
        "sujet": "transactions.valides",
        "position_file": messages[0].position,
        "publie_le_s": messages[0].publie_le,
        "lot_id": bilan.lot_id,
    }
    (argent,) = entrepot.requete("SELECT * FROM argent_transactions")
    assert FORME_ISO.match(argent.pop("traite_le"))
    assert argent == {
        "trans_num": v["trans_num"],
        "date_transaction": "2026-10-09",
        "horodatage_utc": "2026-10-09T12:00:01+00:00",
        "horodatage_s": v["horodatage_s"],
        "carte_hash": v["carte_hash"],
        "carte_fin": "0001",
        "commercant": "Marchand 1",
        "categorie": v["categorie"],
        "montant": 45.0,
        "etat": "NY",
        "population_ville": 8_000_000,
        "age": 41,
        "distance_km": v["distance_km"],
        "heure": 12,
        "jour_semaine": 4,
        "nb_tx_24h": 0,
        "montant_relatif": 1.0,
        "score": 0.05,
        "est_fraude": 0,
        "seuil": 0.5,
        "version_modele": "factice-1",
        "version_regles": VERSION_REGLES,
        "etiquette_source": 0,
        "source": "jeu",
        "lot_id": bilan.lot_id,
    }


# --------------------------------------------------------------------------------------------
# Atomicité
# --------------------------------------------------------------------------------------------
def panne(message: str, exception=RuntimeError):
    def lever(*args, **kwargs):
        raise exception(message)

    return lever


@pytest.mark.parametrize("etape", ["inserer_bronze", "inserer_argent", "inserer_alertes"])
def test_un_echec_d_ecriture_n_ecrit_rien_ne_valide_pas_la_file_et_n_annonce_aucun_lot(
    config, file, entrepot, consommateur, hacheur, monkeypatch, evenements, etape
):
    """Quelle que soit la couche qui échoue, tout est annulé et le lot reste à rejouer."""
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2, amt=900.0))
    messages = lot(file, config)
    couches_avant, lignage_avant = couches(entrepot), lignage(entrepot)
    monkeypatch.setattr(entrepot, etape, panne("couche indisponible"))
    with pytest.raises(RuntimeError) as exc:
        consommateur.traiter_lot(messages)
    assert str(exc.value) == "couche indisponible"
    assert couches(entrepot) == couches_avant and lignage(entrepot) == lignage_avant
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 2
    assert [e for e in evenements.tous() if e[1] == "lot_traite"] == []


def test_un_echec_du_lignage_annule_aussi_les_trois_couches_deja_ecrites(
    config, file, entrepot, consommateur, hacheur, monkeypatch
):
    """Le lignage fait partie du lot : s'il échoue, bronze, argent et alertes sont annulés."""
    publier(file, config, valeur(hacheur, 1, amt=900.0))
    messages = lot(file, config)
    couches_avant = couches(entrepot)
    monkeypatch.setattr(entrepot, "enregistrer_lignage", panne("lignage indisponible"))
    with pytest.raises(RuntimeError):
        consommateur.traiter_lot(messages)
    assert couches(entrepot) == couches_avant
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 1


def test_une_panne_du_modele_pendant_la_notation_annule_le_lot_et_garde_la_file(
    config, file, entrepot, canal, hacheur
):
    """Un modèle qui plante ne laisse aucune ligne non notée ni position validée."""

    class ModeleCasse:
        seuil = 0.5
        version = "casse"

        def noter(self, caracteristiques):
            raise ErreurModele("modèle illisible")

    consommateur = Consommateur(
        config, file, entrepot, ChargeurFactice(ModeleCasse()), Notificateur([canal])
    )
    publier(file, config, valeur(hacheur, 1))
    messages = lot(file, config)
    couches_avant = couches(entrepot)
    with pytest.raises(ErreurModele) as exc:
        consommateur.traiter_lot(messages)
    assert str(exc.value) == "modèle illisible"
    assert couches(entrepot) == couches_avant
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 1


def test_un_modele_introuvable_arrete_le_lot_avant_toute_transaction(
    config, file, entrepot, canal, hacheur
):
    """Si le chargeur échoue, rien n'est ouvert ni écrit et la position n'avance pas."""

    class ChargeurSansModele:
        def obtenir(self):
            raise ErreurModele("aucun modèle en service")

    consommateur = Consommateur(
        config, file, entrepot, ChargeurSansModele(), Notificateur([canal])
    )
    publier(file, config, valeur(hacheur, 1))
    messages = lot(file, config)
    couches_avant, lignage_avant = couches(entrepot), lignage(entrepot)
    with pytest.raises(ErreurModele) as exc:
        consommateur.traiter_lot(messages)
    assert str(exc.value) == "aucun modèle en service"
    assert couches(entrepot) == couches_avant and lignage(entrepot) == lignage_avant
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 1


def test_une_interruption_pendant_le_lot_annule_tout_et_oublie_l_etat_des_cartes(
    config, file, entrepot, consommateur, hacheur, monkeypatch
):
    """Même Ctrl+C (BaseException) annule le lot ; le rejeu retrouve les bonnes valeurs."""
    carte = {"cc_num": 4_929_000_000_000_042}
    publier(
        file,
        config,
        valeur(hacheur, 1, current_time=HORODATAGE_0 * 1000, amt=20.0, **carte),
        valeur(hacheur, 2, current_time=(HORODATAGE_0 + 60) * 1000, amt=60.0, **carte),
    )
    messages = lot(file, config)
    couches_avant = couches(entrepot)
    monkeypatch.setattr(entrepot, "inserer_argent", panne("interrompu", KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        consommateur.traiter_lot(messages)
    assert couches(entrepot) == couches_avant
    monkeypatch.delattr(entrepot, "inserer_argent")  # retour à la vraie méthode
    consommateur.traiter_lot(messages)  # rejeu sur la même instance
    lignes = entrepot.requete(
        "SELECT nb_tx_24h, montant_relatif FROM argent_transactions ORDER BY horodatage_s"
    )
    # Si la première tentative avait laissé ses deux transactions en mémoire, la seconde en
    # compterait 2 (au lieu de 1) et la première n'aurait plus un historique vide.
    assert [(x["nb_tx_24h"], x["montant_relatif"]) for x in lignes] == [(0, 1.0), (1, 3.0)]


# --------------------------------------------------------------------------------------------
# La position n'avance qu'après le commit
# --------------------------------------------------------------------------------------------
def test_la_file_est_validee_apres_le_commit_et_une_validation_en_echec_ne_defait_rien(
    config, file, entrepot, hacheur
):
    """Validation après le commit, lot entier ; si elle échoue, les données restent."""
    espion = FileEspion(file)
    consommateur = Consommateur(
        config, espion, entrepot, ChargeurFactice(), Notificateur([CanalEnregistreur()])
    )
    vus_a_la_validation = []
    espion.avant_validation = lambda: vus_a_la_validation.append(
        entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions")
    )
    abime = valeur(hacheur, 9)
    del abime["montant"]
    publier(file, config, valeur(hacheur, 1), abime, valeur(hacheur, 1), valeur(hacheur, 2))
    messages = lot(file, config)
    consommateur.traiter_lot(messages)
    assert vus_a_la_validation == [2]
    assert espion.validations == [
        (
            config.file.sujet_valides,
            config.file.groupe_consommateur,
            [m.position for m in messages],
        )
    ]
    # La file tombe au moment de valider : le commit a déjà eu lieu, la position n'avance pas.
    espion.avant_validation = panne("file indisponible")
    publier(file, config, valeur(hacheur, 3))
    suivants = lot(file, config)
    with pytest.raises(RuntimeError) as exc:
        consommateur.traiter_lot(suivants)
    assert str(exc.value) == "file indisponible"
    assert entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 3
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == 1
    espion.avant_validation = None
    bronze_avant = couches(entrepot)[0]
    rejeu = consommateur.traiter_lot(suivants)  # relivré : reconnu comme déjà stocké
    assert (rejeu.nouveaux, rejeu.doublons) == (0, 1)
    assert couches(entrepot)[0] == bronze_avant


# --------------------------------------------------------------------------------------------
# Archivage de la file des rejets
# --------------------------------------------------------------------------------------------
def test_archiver_une_file_de_rejets_vide_ne_touche_a_rien_et_lit_avec_les_bons_parametres(
    config, entrepot, evenements
):
    """Rien à archiver : zéro, aucune écriture, aucune validation, aucun événement."""
    vide = FileScriptee()
    consommateur = Consommateur(
        config, vide, entrepot, ChargeurFactice(), Notificateur([CanalEnregistreur()])
    )
    rejets_avant = entrepot.requete("SELECT * FROM meta_rejets")
    assert consommateur.archiver_rejets() == 0
    assert consommateur.archiver_rejets(2.5) == 0
    assert vide.lectures == [
        ("transactions.rejets", "archiveur-rejets", 500, 0.0),
        ("transactions.rejets", "archiveur-rejets", 500, 2.5),
    ]
    assert vide.validations == []
    assert entrepot.requete("SELECT * FROM meta_rejets") == rejets_avant
    assert evenements.tous() == []


def test_les_rejets_sont_archives_avec_leur_date_leur_position_et_valides(
    config, file, entrepot, consommateur, evenements
):
    """Chaque rejet est copié avec son jour d'événement ; la file est validée ensuite."""
    sujet = config.file.sujet_rejets
    file.publier(
        sujet,
        {
            "etape": "producteur",
            "raison": "schema_invalide",
            "trans_num": "p" * 32,
            "erreurs": [{"champ": "amt", "code": "type_invalide", "detail": ""}],
            "rejete_le": "2026-10-10T08:00:00+00:00",
            "charge_utile_masquee": {"champs_conserves": {"current_time": 1_608_548_400_000}},
        },
    )
    file.publier(
        sujet,
        {"etape": "consommateur", "rejete_le": "2026-10-11T00:00:00+00:00", "raison": "r"},
    )
    file.publier(sujet, ["pas", "un", "objet"])
    avant = datetime.now(UTC).date().isoformat()
    assert consommateur.archiver_rejets() == 3
    apres = datetime.now(UTC).date().isoformat()
    lignes = entrepot.requete("SELECT * FROM meta_rejets ORDER BY cle_message")
    assert [(x["sujet"], x["etape"], x["raison"], x["trans_num"]) for x in lignes] == [
        (sujet, "producteur", "schema_invalide", "p" * 32),
        (sujet, "consommateur", "r", None),
        (sujet, "inconnue", "inconnue", None),
    ]
    assert [x["date_evenement"] for x in lignes[:2]] == ["2020-12-21", "2026-10-11"]
    assert lignes[2]["date_evenement"] in {avant, apres}
    assert json.loads(lignes[0]["erreurs"]) == [
        {"champ": "amt", "code": "type_invalide", "detail": ""}
    ]
    assert file.retard(sujet, config.file.groupe_rejets) == 0
    assert [e for e in evenements.tous() if e[1] == "rejets_archives"] == [
        ("INFO", "rejets_archives", {"nombre": 3})
    ]
    archive_avant = entrepot.requete("SELECT * FROM meta_rejets ORDER BY cle_message")
    assert consommateur.archiver_rejets() == 0  # tout est validé : le second passage ne fait rien
    assert entrepot.requete("SELECT * FROM meta_rejets ORDER BY cle_message") == archive_avant


def test_un_echec_pendant_l_archivage_n_archive_rien_et_ne_valide_pas_la_file_des_rejets(
    config, file, entrepot, consommateur, monkeypatch, evenements
):
    """L'archivage est atomique : un rejet qui échoue annule les précédents et reste à rejouer."""
    sujet = config.file.sujet_rejets
    for n in range(3):
        file.publier(sujet, {"etape": "producteur", "raison": f"r{n}", "rejete_le": "2026-10-10"})
    rejets_avant = entrepot.requete("SELECT * FROM meta_rejets")
    reel = entrepot.enregistrer_rejet
    appels = []

    def echoue_au_troisieme(conn, rejet, cle, sujet_, date):
        appels.append(cle)
        if len(appels) > 2:
            raise RuntimeError("archive pleine")
        reel(conn, rejet, cle, sujet_, date)

    monkeypatch.setattr(entrepot, "enregistrer_rejet", echoue_au_troisieme)
    with pytest.raises(RuntimeError) as exc:
        consommateur.archiver_rejets()
    assert str(exc.value) == "archive pleine"
    assert entrepot.requete("SELECT * FROM meta_rejets") == rejets_avant
    assert file.retard(sujet, config.file.groupe_rejets) == 3
    assert [e for e in evenements.tous() if e[1] == "rejets_archives"] == []
    monkeypatch.delattr(entrepot, "enregistrer_rejet")  # retour à la vraie méthode
    assert consommateur.archiver_rejets() == 3  # rejeu complet
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_rejets") == 3


def test_archiver_deux_fois_les_memes_rejets_laisse_la_premiere_archive_inchangee(
    config, file, entrepot
):
    """Une relivraison de la file des rejets ne crée pas de doublon dans ``meta_rejets``."""
    sujet = config.file.sujet_rejets
    file.publier(sujet, {"etape": "producteur", "raison": "premiere", "rejete_le": "2026-10-10"})
    messages = file.lire(sujet, "lecteur-annexe", 100, 0)
    relivree = FileScriptee(messages)
    consommateur = Consommateur(
        config, relivree, entrepot, ChargeurFactice(), Notificateur([CanalEnregistreur()])
    )
    assert consommateur.archiver_rejets() == 1
    archive_initiale = entrepot.requete("SELECT * FROM meta_rejets")
    assert consommateur.archiver_rejets() == 1  # relivrée : même résultat, aucune écriture de plus
    assert entrepot.requete("SELECT * FROM meta_rejets") == archive_initiale
    assert relivree.validations == [(sujet, "archiveur-rejets", [messages[0].position])] * 2


# --------------------------------------------------------------------------------------------
# _date_evenement
# --------------------------------------------------------------------------------------------
def rejet(champs: dict[str, Any] | None = None, rejete_le: Any = None) -> dict[str, Any]:
    """Un rejet minimal : champs conservés de la charge masquée et date du rejet facultatifs."""
    resultat: dict[str, Any] = {}
    if champs is not None:
        resultat["charge_utile_masquee"] = {"champs_conserves": champs}
    if rejete_le is not None:
        resultat["rejete_le"] = rejete_le
    return resultat


@pytest.mark.parametrize(
    ("entree", "jour"),
    [
        (rejet({"current_time": 1_608_548_400_000}), "2020-12-21"),
        (rejet({"current_time": 1_608_595_199_999}), "2020-12-21"),  # 23 h 59 min 59,999 s
        (rejet({"current_time": 1_608_595_200_000}), "2020-12-22"),
        (rejet({"current_time": 1_608_548_400_000.0}), "2020-12-21"),
        (rejet({"current_time": 0}), "1970-01-01"),
        (rejet({"horodatage_s": 1_608_595_199}), "2020-12-21"),
        (rejet({"horodatage_s": 1_608_595_200}), "2020-12-22"),
        (rejet({"horodatage_s": 1_608_595_200.0}), "2020-12-22"),
        (rejet({"current_time": 1_608_548_400_000, "horodatage_s": 1_700_000_000}), "2020-12-21"),
        (rejet({"current_time": True, "horodatage_s": 1_608_548_400}), "2020-12-21"),
        (rejet({"current_time": False, "horodatage_s": 1_608_548_400}), "2020-12-21"),
        (rejet({"current_time": "1608548400000"}, "2026-10-10T08:00:00"), "2026-10-10"),
        (rejet({"horodatage_s": True}, "2026-10-10T08:00:00"), "2026-10-10"),
        (rejet({"horodatage_s": None}, "2026-10-10T08:00:00"), "2026-10-10"),
        (rejet(None, "2026-10-10T08:00:00+00:00"), "2026-10-10"),
        ({"charge_utile_masquee": None, "rejete_le": "2026-10-09T01:02:03"}, "2026-10-09"),
        (
            {"charge_utile_masquee": {"champs_conserves": None}, "rejete_le": "2026-10-08"},
            "2026-10-08",
        ),
        (rejet({}, "2026-10-10"), "2026-10-10"),  # dix caractères pile : accepté
        # Une date de transaction hors plage est écartée au profit de celle du rejet, même si
        # l'autre champ de date, lui, serait lisible (la lecture s'arrête au premier illisible).
        (rejet({"current_time": 10**30}, "2026-10-10T08:00:00"), "2026-10-10"),
        (
            rejet({"current_time": 10**30, "horodatage_s": 1_608_548_400}, "2026-09-09"),
            "2026-09-09",
        ),
    ],
)
def test_le_jour_d_un_rejet_vient_de_la_transaction_si_elle_est_lisible_sinon_du_rejet(
    entree, jour
):
    """Millisecondes pour ``current_time``, secondes pour ``horodatage_s`` ; sinon le rejet."""
    entree_avant = copy.deepcopy(entree)
    assert _date_evenement(entree) == jour
    assert entree == entree_avant


@pytest.mark.parametrize(
    "entree",
    [
        {},
        rejet(None, ""),
        rejet({}, "2026-10-1"),  # neuf caractères : trop court pour être une date
        {"rejete_le": None},
        rejet({"current_time": "texte"}),
    ],
)
def test_sans_date_exploitable_le_jour_d_un_rejet_est_celui_d_aujourd_hui(entree):
    """Pas de date lisible : repli sur le jour courant (UTC), le rejet reste inchangé."""
    entree_avant = copy.deepcopy(entree)
    avant = datetime.now(UTC).date().isoformat()
    jour = _date_evenement(entree)
    apres = datetime.now(UTC).date().isoformat()
    assert jour in {avant, apres}
    assert entree == entree_avant


# --------------------------------------------------------------------------------------------
# executer_une_fois, état de santé, délégations
# --------------------------------------------------------------------------------------------
def test_executer_une_fois_lit_les_deux_sujets_avec_la_configuration_et_ne_valide_rien_a_vide(
    config, entrepot, tmp_path
):
    """Sujet, groupe, taille, attente : ceux de la config (0 reste 0) ; à vide, rien d'écrit."""
    racine = tmp_path / "absente" / "donnees"  # l'état de santé crée les dossiers manquants
    reglee = replace(
        config,
        file=replace(config.file, taille_lot=7, attente_lecture_s=0.25),
        racine_donnees=racine,
    )
    vide = FileScriptee()
    consommateur = Consommateur(
        reglee, vide, entrepot, ChargeurFactice(), Notificateur([CanalEnregistreur()])
    )
    couches_avant, lignage_avant = couches(entrepot), lignage(entrepot)
    bilan = consommateur.executer_une_fois()
    consommateur.executer_une_fois(attente_s=0)
    consommateur.executer_une_fois(attente_s=0.75)
    assert isinstance(bilan, BilanLot) and bilan.recus == 0
    valides, rejets = "transactions.valides", "transactions.rejets"
    assert vide.lectures == [
        (valides, "consommateur-fraude", 7, 0.25),
        (rejets, "archiveur-rejets", 7, 0.0),
        (valides, "consommateur-fraude", 7, 0),
        (rejets, "archiveur-rejets", 7, 0.0),
        (valides, "consommateur-fraude", 7, 0.75),
        (rejets, "archiveur-rejets", 7, 0.0),
    ]
    assert vide.validations == []
    assert couches(entrepot) == couches_avant and lignage(entrepot) == lignage_avant
    assert (racine / "etat" / "consommateur.json").is_file()  # écrit à chaque tour, sans erreur


def test_executer_une_fois_enchaine_modele_lecture_rejets_alertes_puis_etat(
    config, file, entrepot, monkeypatch
):
    """Le modèle est vérifié d'abord, puis lot, rejets et alertes ; à vide, la base est intacte."""
    journal: list[str] = []

    class ChargeurTemoin(ChargeurFactice):
        def obtenir(self):
            journal.append("modele")
            return super().obtenir()

    espion = FileEspion(file, journal)
    consommateur = Consommateur(
        config, espion, entrepot, ChargeurTemoin(), Notificateur([CanalEnregistreur()])
    )
    monkeypatch.setattr(consommateur, "envoyer_alertes", lambda: journal.append("alertes"))
    chemin = config.racine_donnees / "etat" / "consommateur.json"
    assert not chemin.exists()
    couches_avant, lignage_avant = couches(entrepot), lignage(entrepot)
    bilan = consommateur.executer_une_fois(attente_s=0)
    assert journal == [
        "modele",
        "lire:transactions.valides",
        "lire:transactions.rejets",
        "alertes",
        "modele",  # relu pour l'état de santé
    ]
    assert json.loads(chemin.read_text(encoding="utf-8"))["dernier_lot"] == bilan.lot_id
    assert couches(entrepot) == couches_avant and lignage(entrepot) == lignage_avant


def test_l_etat_de_sante_est_un_json_complet_recrit_a_chaque_boucle(
    config, file, entrepot, consommateur, hacheur
):
    """Dernier lot, nombre reçu, modèle et heure UTC ; le fichier est remplacé, pas allongé."""
    chemin = config.racine_donnees / "etat" / "consommateur.json"
    assert not chemin.parent.exists()
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2), valeur(hacheur, 3))
    avant = datetime.now(UTC)
    premier = consommateur.executer_une_fois(attente_s=0)
    apres = datetime.now(UTC)
    etat_avant = json.loads(chemin.read_text(encoding="utf-8"))
    assert set(etat_avant) == {"derniere_boucle", "dernier_lot", "recus", "modele"}
    assert (etat_avant["dernier_lot"], etat_avant["recus"]) == (premier.lot_id, 3)
    horodatage = datetime.fromisoformat(etat_avant["derniere_boucle"])
    assert horodatage.utcoffset().total_seconds() == 0
    assert avant <= horodatage <= apres
    second = consommateur.executer_une_fois(attente_s=0)
    etat_apres = json.loads(chemin.read_text(encoding="utf-8"))  # un seul document, pas deux
    assert (etat_apres["dernier_lot"], etat_apres["recus"]) == (second.lot_id, 0)
    assert etat_apres["modele"] == etat_avant["modele"] == "factice-1"
    assert second.lot_id != premier.lot_id


def test_executer_une_fois_traite_lot_rejets_et_alertes_dans_le_meme_tour(
    config, file, entrepot, consommateur, canal, hacheur
):
    """Un tour complet : lot stocké, rejet archivé, alerte envoyée ; l'ancienne alerte reste."""
    ancienne = alerte_en_base(entrepot, 99, statut_notification="envoyee", tentatives=2)
    ancienne_avant = entrepot.requete("SELECT * FROM or_alertes_fraude")
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2, amt=900.0))
    file.publier(config.file.sujet_valides, ["intrus"])
    bilan = consommateur.executer_une_fois(attente_s=0)
    assert (bilan.recus, bilan.nouveaux, bilan.rejets, bilan.fraudes) == (3, 2, 1, 1)
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_rejets") == 1
    alertes = entrepot.requete("SELECT * FROM or_alertes_fraude")
    (alerte,) = [a for a in alertes if a["trans_num"] != ancienne["trans_num"]]
    assert [a for a in alertes if a["trans_num"] == ancienne["trans_num"]] == ancienne_avant
    assert alerte["statut_notification"] == "envoyee"
    assert [n.donnees["trans_num"] for n in canal.recus] == [alerte["trans_num"]]
    assert alerte["detectee_le_s"] == entrepot.scalaire(
        "SELECT publie_le_s FROM bronze_transactions WHERE trans_num = :t", t=alerte["trans_num"]
    )
    assert 0 <= alerte["latence_notification_s"] < 120  # horloge réelle par défaut


def test_l_horloge_injectee_sert_a_mesurer_la_latence_des_alertes(
    config, file, entrepot, canal, hacheur, evenements
):
    """Horloge à 2 000 000 000 s : la latence enregistrée est l'écart à la détection."""
    consommateur = Consommateur(
        config,
        file,
        entrepot,
        ChargeurFactice(),
        Notificateur([canal]),
        horloge=lambda: 2_000_000_000.0,
    )
    abandonnee = alerte_en_base(entrepot, 98, statut_notification="echec", tentatives=5)
    abandonnee_avant = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=abandonnee["trans_num"]
    )
    publier(file, config, valeur(hacheur, 1, amt=900.0))
    consommateur.executer_une_fois(attente_s=0)
    alertes = entrepot.requete("SELECT * FROM or_alertes_fraude")
    (alerte,) = [a for a in alertes if a["trans_num"] != abandonnee["trans_num"]]
    latence = 2_000_000_000.0 - alerte["detectee_le_s"]
    assert alerte["latence_notification_s"] == pytest.approx(latence)
    assert evenements.nommes("sla_depasse") == [
        (
            "WARNING",
            {
                "sla": "latence_notification",
                "latence_s": round(latence, 1),
                "seuil_s": 60.0,
                "trans_num": alerte["trans_num"],
            },
        )
    ]
    assert [a for a in alertes if a["trans_num"] == abandonnee["trans_num"]] == abandonnee_avant


def test_envoyer_alertes_delegue_avec_l_entrepot_le_notificateur_la_config_et_l_horloge(
    config, file, entrepot, monkeypatch
):
    """Les quatre éléments sont transmis tels quels et le compte rendu est renvoyé."""
    recus = []

    def faux_envoi(*args, **kwargs):
        recus.append((args, kwargs))
        return {"envoyees": 7, "reportees": 8, "en_echec": 9}

    monkeypatch.setattr(module_consommateur, "envoyer_alertes_en_attente", faux_envoi)
    notificateur = Notificateur([CanalEnregistreur()])

    def horloge():
        return 123.0

    consommateur = Consommateur(
        config, file, entrepot, ChargeurFactice(), notificateur, horloge=horloge
    )
    couches_avant = couches(entrepot)
    assert consommateur.envoyer_alertes() == {"envoyees": 7, "reportees": 8, "en_echec": 9}
    ((args, kwargs),) = recus
    assert args == (entrepot, notificateur, config)  # objets sans égalité propre : identité
    assert kwargs == {"horloge": horloge}
    assert couches(entrepot) == couches_avant


# --------------------------------------------------------------------------------------------
# boucle
# --------------------------------------------------------------------------------------------
class TourScripte:
    """Remplace ``executer_une_fois`` : rend des bilans prévus et lève l'arrêt par sécurité."""

    def __init__(self, arreter: threading.Event, recus: list[int], arrete_au_tour: int = 0):
        self.arreter, self.recus, self.arrete_au_tour = arreter, recus, arrete_au_tour
        self.appels: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.appels.append((args, kwargs))
        rang = len(self.appels)
        if rang == self.arrete_au_tour or rang >= len(self.recus) + 3:
            self.arreter.set()  # filet : jamais plus de quelques tours même si la boucle déraille
        return BilanLot(recus=self.recus[rang - 1] if rang <= len(self.recus) else 0)


@pytest.mark.parametrize(
    ("tours", "jusqu_a_vide", "arret_au_tour", "total", "nombre_de_tours"),
    [
        ([3, 1, 0, 9], True, 0, 4, 3),  # s'arrête au premier tour vide, pas avant, pas après
        ([0], True, 0, 0, 1),  # file déjà vide : un seul tour
        ([0, 0, 2, 0], False, 4, 2, 4),  # sans l'option, un tour vide n'arrête rien
        ([0, 0, 2, 0], None, 4, 2, 4),  # option omise : même chose (défaut = ne pas s'arrêter)
        (
            [4, 5, 6],
            False,
            2,
            9,
            2,
        ),  # arrêt levé pendant un tour : ce tour est gardé, pas le suivant
    ],
)
def test_la_boucle_s_arrete_au_bon_tour_selon_l_option_et_ne_lit_rien_par_elle_meme(
    config,
    file,
    consommateur,
    hacheur,
    monkeypatch,
    tours,
    jusqu_a_vide,
    arret_au_tour,
    total,
    nombre_de_tours,
):
    """``jusqu_a_vide`` stoppe au premier tour vide, sinon l'arrêt ; la file n'est pas touchée."""
    publier(file, config, valeur(hacheur, 1))
    retard_avant = file.retard(config.file.sujet_valides, config.file.groupe_consommateur)
    arret = threading.Event()
    scripte = TourScripte(arret, tours, arret_au_tour)
    monkeypatch.setattr(consommateur, "executer_une_fois", scripte)
    with chien_de_garde(arret):
        options = {} if jusqu_a_vide is None else {"jusqu_a_vide": jusqu_a_vide}
        assert consommateur.boucle(arret, **options) == total
    assert arret.is_set() == (arret_au_tour != 0)  # pas levé quand la boucle s'arrête seule
    assert scripte.appels == [((), {})] * nombre_de_tours  # appelée sans argument
    retard_apres = file.retard(config.file.sujet_valides, config.file.groupe_consommateur)
    assert retard_apres == retard_avant  # tout passe par ``executer_une_fois``, jamais la boucle


def test_la_boucle_jusqu_a_vide_traite_la_file_puis_s_arrete_d_elle_meme(
    config, file, entrepot, canal, hacheur
):
    """Trois messages puis file vide : la boucle rend 3 et s'arrête toute seule ; rejouée, rien."""
    sans_attente = replace(config, file=replace(config.file, attente_lecture_s=0.0))
    consommateur = Consommateur(
        sans_attente, file, entrepot, ChargeurFactice(), Notificateur([canal])
    )
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2), valeur(hacheur, 3))
    arret = threading.Event()
    with chien_de_garde(arret):
        total = consommateur.boucle(arret, jusqu_a_vide=True)
    assert not arret.is_set()  # arrêtée par elle-même, pas par le chien de garde
    assert total == 3
    couches_avant = couches(entrepot)
    with chien_de_garde(arret):
        assert consommateur.boucle(arret, jusqu_a_vide=True) == 0  # file vidée : rien à faire
    assert not arret.is_set()
    assert couches(entrepot) == couches_avant


def test_une_boucle_dont_l_arret_est_deja_demande_ne_lit_rien_et_laisse_la_file_intacte(
    config, file, consommateur, hacheur, monkeypatch
):
    """Arrêt demandé avant le démarrage : aucun tour, aucun message consommé."""
    publier(file, config, valeur(hacheur, 1), valeur(hacheur, 2))
    arret = threading.Event()
    arret.set()

    def tour_interdit(*args, **options):
        raise AssertionError("la boucle ne devait pas faire un seul tour")

    monkeypatch.setattr(consommateur, "executer_une_fois", tour_interdit)
    retard_avant = file.retard(config.file.sujet_valides, config.file.groupe_consommateur)
    assert consommateur.boucle(arret) == 0
    assert consommateur.boucle(arret, jusqu_a_vide=True) == 0
    assert file.retard(config.file.sujet_valides, config.file.groupe_consommateur) == retard_avant


# --------------------------------------------------------------------------------------------
# Fonctions de couche : bronze, argent, notation, alerte
# --------------------------------------------------------------------------------------------
def message_brut(**valeurs: Any) -> Message:
    base = {
        "trans_num": "t" * 32,
        "version_schema": 1,
        "source": "api",
        "horodatage_s": 1_700_000_000,
        "carte_hash": "c" * 32,
        "carte_fin": "4242",
        "commercant": "fraud_Marchand et Fils ",
        "categorie": "grocery_pos",
        "montant": 12,
        "etat": "NY",
        "population_ville": 1000,
        "age": 37,
        "distance_km": 5,
        "lat_commercant": 40,
        "long_commercant": -74,
    }
    base.update(valeurs)
    return Message("transactions.valides", None, base, "42", 1_700_000_001.5)


def test_construire_ligne_bronze_convertit_les_types_et_ajoute_la_provenance():
    """Entiers et flottants sont forcés ; l'étiquette est facultative ; provenance recopiée."""
    message = message_brut(horodatage_s=1_700_000_000.0, population_ville=1000.0, age=37.0)
    valeurs_avant = dict(message.valeur)
    ligne = construire_ligne_bronze(message, "lot123", "2026-10-10T08:00:00+00:00")
    assert ligne == {
        "trans_num": "t" * 32,
        "version_schema": 1,
        "source": "api",
        "horodatage_s": 1_700_000_000,
        "carte_hash": "c" * 32,
        "carte_fin": "4242",
        "commercant": "fraud_Marchand et Fils ",
        "categorie": "grocery_pos",
        "montant": 12.0,
        "etat": "NY",
        "population_ville": 1000,
        "age": 37,
        "distance_km": 5.0,
        "lat_commercant": 40.0,
        "long_commercant": -74.0,
        "etiquette_source": None,
        "sujet": "transactions.valides",
        "position_file": "42",
        "publie_le_s": 1_700_000_001.5,
        "recu_le": "2026-10-10T08:00:00+00:00",
        "lot_id": "lot123",
    }
    types = {k: type(ligne[k]) for k in ("horodatage_s", "population_ville", "age")}
    assert types == {"horodatage_s": int, "population_ville": int, "age": int}
    flottants = ("montant", "distance_km", "lat_commercant", "long_commercant")
    assert {type(ligne[k]) for k in flottants} == {float}
    assert message.valeur == valeurs_avant
    avec_etiquette = construire_ligne_bronze(message_brut(etiquette_source=1), "l", "r")
    assert avec_etiquette["etiquette_source"] == 1


@pytest.mark.parametrize(
    "cle",
    [
        "trans_num",
        "version_schema",
        "source",
        "horodatage_s",
        "carte_hash",
        "carte_fin",
        "commercant",
        "categorie",
        "montant",
        "etat",
        "population_ville",
        "age",
        "distance_km",
        "lat_commercant",
        "long_commercant",
    ],
)
def test_construire_ligne_bronze_refuse_un_message_auquel_il_manque_un_champ(cle):
    """Un champ obligatoire absent lève ``KeyError`` : on n'écrit jamais une ligne à trous."""
    message = message_brut()
    del message.valeur[cle]
    with pytest.raises(KeyError) as exc:
        construire_ligne_bronze(message, "lot", "recu")
    assert exc.value.args == (cle,)


def ligne_bronze_pour_enrichir(**surcharges: Any) -> dict[str, Any]:
    base = construire_ligne_bronze(message_brut(), "lot9", "2026-10-10T08:00:00+00:00")
    base.update(surcharges)
    return base


def test_enrichir_calcule_l_argent_et_enregistre_la_transaction_sans_toucher_au_bronze():
    """Normalisation, dérivées de temps, historique de la carte ; la ligne bronze reste intacte."""
    etat = EtatCartes()
    premiere = ligne_bronze_pour_enrichir()
    premiere_avant = dict(premiere)
    argent = enrichir(premiere, etat)
    assert argent == {
        "trans_num": "t" * 32,
        "date_transaction": "2023-11-14",
        "horodatage_utc": "2023-11-14T22:13:20+00:00",
        "horodatage_s": 1_700_000_000,
        "carte_hash": "c" * 32,
        "carte_fin": "4242",
        "commercant": "Marchand et Fils",
        "categorie": "grocery_pos",
        "montant": 12.0,
        "etat": "NY",
        "population_ville": 1000,
        "age": 37,
        "distance_km": 5.0,
        "heure": 22,
        "jour_semaine": 1,
        "nb_tx_24h": 0,
        "montant_relatif": 1.0,
        "etiquette_source": None,
        "source": "api",
        "lot_id": "lot9",
    }
    assert premiere == premiere_avant
    suivante = enrichir(
        ligne_bronze_pour_enrichir(horodatage_s=1_700_000_600, montant=36.0, etiquette_source=1),
        etat,
    )  # la première transaction est maintenant dans l'état de la carte
    assert (suivante["nb_tx_24h"], suivante["montant_relatif"]) == (1, 3.0)
    assert suivante["etiquette_source"] == 1
    sans_lot = ligne_bronze_pour_enrichir()
    del sans_lot["lot_id"], sans_lot["etiquette_source"]
    ancienne = enrichir(sans_lot, EtatCartes())
    assert (ancienne["lot_id"], ancienne["etiquette_source"]) == (None, None)


class ModeleScore:
    version = "m-test"
    seuil = 0.5

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.recus: list[pd.DataFrame] = []

    def noter(self, caracteristiques: pd.DataFrame):
        self.recus.append(caracteristiques)
        return self.scores


def ligne_a_noter(rang: int) -> dict[str, Any]:
    return {
        "montant": 10.0 * rang,
        "age": 30,
        "distance_km": 1.0,
        "population_ville": 500,
        "heure": 8,
        "jour_semaine": 2,
        "nb_tx_24h": rang,
        "montant_relatif": 1.0,
        "categorie": "grocery_pos",
        "etat": "NY",
        "carte_hash": "ne-doit-pas-etre-transmis",
    }


def test_noter_lignes_decide_au_seuil_inclus_et_ne_transmet_au_modele_que_ses_colonnes():
    """Score >= seuil = fraude (seuil inclus) ; champs existants gardés ; carte non transmise."""
    modele = ModeleScore([0.49, 0.5, 0.51])
    lignes = [ligne_a_noter(1), ligne_a_noter(2), ligne_a_noter(3)]
    lignes_avant = [dict(ligne) for ligne in lignes]
    noter_lignes(lignes, modele)
    (transmis,) = modele.recus
    assert list(transmis.columns) == list(COLONNES_MODELE)
    assert transmis["montant"].tolist() == [10.0, 20.0, 30.0]
    assert [x["est_fraude"] for x in lignes] == [0, 1, 1]
    assert [x["score"] for x in lignes] == [0.49, 0.5, 0.51]
    for ligne, ligne_avant in zip(lignes, lignes_avant, strict=True):
        assert {cle: ligne[cle] for cle in ligne_avant} == ligne_avant  # rien d'existant modifié
        assert (ligne["seuil"], ligne["version_modele"]) == (0.5, "m-test")
        assert ligne["version_regles"] == VERSION_REGLES
        assert FORME_ISO.match(ligne["traite_le"])
        assert type(ligne["score"]) is float and type(ligne["est_fraude"]) is int


def test_noter_ne_sollicite_pas_le_modele_sans_ligne_et_refuse_un_nombre_de_scores_incoherent():
    """Aucune ligne : le modèle n'est pas appelé ; scores trop courts ou trop longs : erreur."""
    modele = ModeleScore([])
    lignes: list[dict[str, Any]] = []
    assert noter_lignes(lignes, modele) is None
    assert modele.recus == [] and lignes == []
    with pytest.raises(ValueError) as exc:
        noter_lignes([ligne_a_noter(1), ligne_a_noter(2)], ModeleScore([0.9]))
    assert str(exc.value) == "zip() argument 2 is shorter than argument 1"
    with pytest.raises(ValueError) as exc_long:
        noter_lignes([ligne_a_noter(1)], ModeleScore([0.9, 0.1]))
    assert str(exc_long.value) == "zip() argument 2 is longer than argument 1"


def test_ligne_alerte_ne_reprend_que_les_champs_d_alerte_et_part_a_envoyer():
    """L'alerte part « à envoyer », sans tentative, et ne recopie ni carte hachée ni âge."""
    argent = {
        "trans_num": "t" * 32,
        "date_transaction": "2026-10-10",
        "horodatage_utc": "2026-10-10T08:00:00+00:00",
        "carte_fin": "4242",
        "commercant": "Marchand",
        "categorie": "misc_net",
        "montant": 900.0,
        "score": 0.97,
        "seuil": 0.5,
        "version_modele": "m1",
        "source": "api",
        "carte_hash": "c" * 32,
        "age": 41,
        "heure": 8,
    }
    argent_avant = dict(argent)
    alerte = ligne_alerte(argent, 1234.5)
    assert FORME_ISO.match(alerte.pop("cree_le"))
    assert alerte == {
        "trans_num": "t" * 32,
        "date_transaction": "2026-10-10",
        "horodatage_utc": "2026-10-10T08:00:00+00:00",
        "carte_fin": "4242",
        "commercant": "Marchand",
        "categorie": "misc_net",
        "montant": 900.0,
        "score": 0.97,
        "seuil": 0.5,
        "version_modele": "m1",
        "source": "api",
        "statut_notification": "a_envoyer",
        "tentatives": 0,
        "derniere_tentative": None,
        "notifiee_le": None,
        "latence_notification_s": None,
        "canaux_ok": "",
        "detectee_le_s": 1234.5,
    }
    assert argent == argent_avant


# --------------------------------------------------------------------------------------------
# Précisions : ce que les tests ci-dessus laissaient passer (relevé par la mutation)
# --------------------------------------------------------------------------------------------
class HorlogeUTCSeulement(datetime):
    """``now`` n'accepte que l'UTC : une heure locale (``now()`` ou ``now(None)``) est refusée."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is not UTC:
            raise AssertionError("le jour courant d'un rejet doit se lire en UTC")
        return datetime(2026, 10, 10, 23, 59, tzinfo=UTC)


def test_sans_date_exploitable_le_jour_d_un_rejet_est_celui_de_l_utc_et_non_du_fuseau_local(
    monkeypatch,
):
    """Un rejet sans date lisible prend le jour UTC courant, même à 23 h 59 UTC."""
    monkeypatch.setattr(module_consommateur, "datetime", HorlogeUTCSeulement)
    assert _date_evenement({}) == "2026-10-10"
    assert _date_evenement({"rejete_le": "2026-10-1"}) == "2026-10-10"
