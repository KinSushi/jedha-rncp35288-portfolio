# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Retraitement depuis le bronze : bornes de période, refus, alertes conservées, état intact.

Le retraitement réécrit le passé : c'est l'opération la plus dangereuse du pipeline. Ces tests
prouvent ce qu'il ne doit **jamais** faire (toucher au bronze, sortir de la période demandée,
renvoyer une alerte déjà partie, effacer une alerte envoyée, laisser croire qu'il a fini après
une panne) en comparant, à chaque fois, un instantané pris *avant* à l'état *après*.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pytest

from detection_fraude import VERSION_REGLES
from detection_fraude import consommateur as module_consommateur
from detection_fraude import retraitement as module_retraitement
from detection_fraude.entrepot import Entrepot
from detection_fraude.modele import ErreurModele, exporter_modele, promouvoir
from detection_fraude.retraitement import retraiter
from tests.test_rapport import ligne_argent
from tests.test_rapport_garanties import inserer, instantane

DEBUT_S = int(datetime(2020, 12, 21, tzinfo=UTC).timestamp())  # lundi 21/12/2020 00:00:00 UTC
JOUR_S = 86_400
INSTANT_FIXE = "2026-10-10T07:05:00+00:00"
LOT_ATTENDU = "retraitement-20261010T072000"
TABLES_ARGENT_ET_ALERTES = ("argent_transactions", "or_alertes_fraude")
MESSAGE_PERIODE_VIDE = "La période est vide : 'jusqu' doit être postérieure ou égale à 'depuis'."


# --------------------------------------------------------------------------------------------
# Outils de test
# --------------------------------------------------------------------------------------------
class ModeleSeuil:
    """Fraude si le montant dépasse ``plafond`` ; garde la taille de chaque lot noté."""

    seuil = 0.5

    def __init__(self, version: str, plafond: float) -> None:
        self.version = version
        self.plafond = plafond
        self.tailles: list[int] = []

    def noter(self, caracteristiques):
        self.tailles.append(len(caracteristiques))
        return np.where(caracteristiques["montant"].to_numpy() > self.plafond, 0.95, 0.05)


class ModeleQuiCasse(ModeleSeuil):
    """Tombe en panne au deuxième lot noté (le premier a donc déjà été écrit)."""

    def noter(self, caracteristiques):
        resultat = super().noter(caracteristiques)
        if self.tailles[1:2]:  # deuxième lot noté
            raise RuntimeError("panne du modèle (test)")
        return resultat


class PipelineMontant:
    """Pipeline minimal picklable : probabilité de fraude 0,9 au-dessus de ``plafond``."""

    def __init__(self, plafond: float) -> None:
        self.plafond = plafond

    def predict_proba(self, caracteristiques):
        p = np.where(caracteristiques["montant"].to_numpy() > self.plafond, 0.9, 0.1)
        return np.column_stack([1 - p, p])


class HorlogeFixe(datetime):
    """Remplace la classe ``datetime`` du module : ``now`` rend toujours 10/10/2026 07:20 UTC."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is None:  # ni now() ni now(None) : l'heure locale de la machine est refusée
            raise AssertionError("l'heure se lit en UTC : now(UTC)")
        return datetime(2026, 10, 10, 7, 20, tzinfo=UTC).astimezone(tz)


@pytest.fixture
def temps_fige(monkeypatch):
    """Fige l'horloge, l'horodatage ISO du retraitement et celui du consommateur."""
    monkeypatch.setattr(module_retraitement, "datetime", HorlogeFixe)
    monkeypatch.setattr(module_retraitement, "maintenant_iso", lambda: INSTANT_FIXE)
    monkeypatch.setattr(module_consommateur, "maintenant_iso", lambda: INSTANT_FIXE)


@pytest.fixture
def fermetures(monkeypatch):
    """Enregistre chaque fermeture de l'entrepôt ouvert par ``main``."""
    appels: list[str] = []

    class EntrepotEspion(Entrepot):
        def fermer(self) -> None:
            appels.append(self.dialecte)
            super().fermer()

    monkeypatch.setattr(module_retraitement, "Entrepot", EntrepotEspion)
    return appels


@pytest.fixture
def journal_enregistre(monkeypatch):
    """Remplace ``configurer_journal`` : garde ses arguments et laisse le collecteur en place."""
    appels: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        module_retraitement, "configurer_journal", lambda *args: appels.append(args)
    )
    return appels


@pytest.fixture
def garde_regeneration(monkeypatch):
    """Chien de garde : la boucle des jours s'arrête net si elle dépasse 4 jours (anti-boucle)."""
    appels: list[str] = []
    reel = module_retraitement.produire_rapport

    def surveille(config, entrepot, jour, *args, **kwargs):
        appels.append(jour)
        if len(appels) > 4:
            raise AssertionError("la régénération des rapports ne s'arrête plus")
        return reel(config, entrepot, jour, *args, **kwargs)

    monkeypatch.setattr(module_retraitement, "produire_rapport", surveille)
    return appels


def ligne_bronze(rang: int, horodatage_s: int, montant: float, carte: str | None = None):
    """Une ligne de la couche bronze (déjà masquée), avec une carte distincte par défaut."""
    return {
        "trans_num": f"{rang:032x}",
        "version_schema": 1,
        "source": "jeu",
        "horodatage_s": horodatage_s,
        "carte_hash": carte or f"{rang + 1000:032x}",
        "carte_fin": "4242",
        "commercant": "fraud_Marchand",
        "categorie": "grocery_pos",
        "montant": montant,
        "etat": "NY",
        "population_ville": 1000,
        "age": 40,
        "distance_km": 3.0,
        "lat_commercant": 40.8,
        "long_commercant": -73.9,
        "etiquette_source": None,
        "sujet": "transactions.valides",
        "position_file": str(rang),
        "publie_le_s": 1.0,
        "recu_le": "2020-12-21T00:00:00+00:00",
        "lot_id": "lot-initial",
    }


def deposer_bronze(entrepot: Entrepot, lignes: list[dict[str, Any]]) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, lignes)


def ancienne_ligne(bronze: dict[str, Any], fraude: int) -> dict[str, Any]:
    """Ce que l'ancienne règle (``factice-1``) avait écrit dans la couche argent."""
    ligne = ligne_argent(1, jour="2020-12-21", montant=bronze["montant"], fraude=fraude)
    ligne.update(
        trans_num=bronze["trans_num"],
        horodatage_s=bronze["horodatage_s"],
        version_modele="factice-1",
        lot_id="lot-initial",
        traite_le="2020-12-21T12:00:00+00:00",
    )
    return ligne


def alerte_envoyee(trans_num: str, **surcharges: Any) -> dict[str, Any]:
    """Alerte déjà partie : l'état d'envoi doit survivre à tout retraitement."""
    alerte = {
        "trans_num": trans_num,
        "date_transaction": "2020-12-21",
        "horodatage_utc": "2020-12-21T00:00:01+00:00",
        "carte_fin": "0000",
        "commercant": "Ancien",
        "categorie": "ancienne_cat",
        "montant": 1.0,
        "score": 0.6,
        "seuil": 0.4,
        "version_modele": "factice-1",
        "source": "ancienne_source",
        "statut_notification": "envoyee",
        "tentatives": 2,
        "derniere_tentative": "2020-12-21T10:00:00+00:00",
        "notifiee_le": "2020-12-21T10:00:00+00:00",
        "latence_notification_s": 3.5,
        "canaux_ok": "journal,fichier",
        "cree_le": "2020-12-21T09:59:00+00:00",
        "detectee_le_s": 1_608_544_740.0,
    }
    alerte.update(surcharges)
    return alerte


def deposer_alertes(entrepot: Entrepot, alertes: list[dict[str, Any]]) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_alertes(conn, alertes)


def scenario_complet() -> dict[str, dict[str, Any]]:
    """Sept lignes dans la période 21-22/12/2020, plus une juste avant et une juste après."""
    return {
        "avant": ligne_bronze(1, DEBUT_S - 1, 500.0),  # hors période (1 s trop tôt)
        "B": ligne_bronze(2, DEBUT_S, 150.0),  # pile au début : 0 -> 1
        "C": ligne_bronze(3, DEBUT_S + 100, 700.0),  # fraude avant et après
        "I": ligne_bronze(4, DEBUT_S + 200, 120.0),  # absente de l'argent : nouvelle fraude
        "D": ligne_bronze(5, DEBUT_S + 3600, 30.0),  # 1 -> 0
        "E": ligne_bronze(6, DEBUT_S + 7200, 220.0),  # absente de l'argent : nouvelle fraude
        "F": ligne_bronze(7, DEBUT_S + 9000, 90.0),  # absente, pas une fraude
        "G": ligne_bronze(8, DEBUT_S + 2 * JOUR_S - 1, 800.0),  # dernière seconde : 0 -> 1
        "apres": ligne_bronze(9, DEBUT_S + 2 * JOUR_S, 800.0),  # hors période (pile après)
    }


def charger_scenario(entrepot: Entrepot) -> dict[str, dict[str, Any]]:
    """Bronze complet + ancienne couche argent + deux alertes déjà envoyées."""
    lignes = scenario_complet()
    deposer_bronze(entrepot, list(lignes.values()))
    anciennes_decisions = {"avant": 0, "B": 0, "C": 1, "D": 1, "G": 0, "apres": 0}
    inserer(entrepot, [ancienne_ligne(lignes[nom], f) for nom, f in anciennes_decisions.items()])
    deposer_alertes(
        entrepot,
        [
            alerte_envoyee(lignes["C"]["trans_num"]),
            alerte_envoyee(lignes["D"]["trans_num"], montant=30.0),
        ],
    )
    return lignes


def lignes_argent_de(entrepot: Entrepot, *trans_nums: str) -> list[dict[str, Any]]:
    return [
        ligne
        for ligne in instantane(entrepot, "argent_transactions")["argent_transactions"]
        if ligne["trans_num"] in trans_nums
    ]


# --------------------------------------------------------------------------------------------
# Refus : période, dates, modèle
# --------------------------------------------------------------------------------------------
def test_une_periode_inversee_est_refusee_avec_le_message_exact_et_rien_n_est_ecrit(
    config, entrepot, evenements
):
    """« jusqu » avant « depuis » : on ne devine pas l'intention, on s'arrête sans rien toucher."""
    charger_scenario(entrepot)
    etat_avant = instantane(entrepot)
    with pytest.raises(ValueError) as erreur:
        retraiter(config, entrepot, "2020-12-26", "2020-12-21", modele=ModeleSeuil("m", 100.0))
    assert str(erreur.value) == MESSAGE_PERIODE_VIDE
    assert instantane(entrepot) == etat_avant
    assert evenements.tous() == []


def test_un_jour_de_fin_la_veille_du_debut_est_vide_mais_le_meme_jour_est_accepte(
    config, entrepot
):
    """Borne : [debut, fin[ vide si fin = début (refusé) ; un seul jour complet est accepté."""
    modele = ModeleSeuil("m", 100.0)
    with pytest.raises(ValueError) as erreur:
        retraiter(config, entrepot, "2020-12-22", "2020-12-21", modele=modele)
    assert str(erreur.value) == MESSAGE_PERIODE_VIDE
    resultat = retraiter(config, entrepot, "2020-12-22", "2020-12-22", modele=modele)
    assert resultat == {
        "lignes": 0,
        "decisions_modifiees": 0,
        "nouvelles_fraudes": 0,
        "rapports": 0,
        "version_modele": "m",
    }
    assert modele.tailles == []


@pytest.mark.parametrize(
    ("depuis", "jusqu"),
    [
        ("21/12/2020", "2020-12-22"),
        ("2020-12-21", "22/12/2020"),
        ("2020-13-01", "2020-12-22"),
        ("2020-12-21", "2020-02-30"),
        ("", "2020-12-22"),
    ],
)
def test_une_date_mal_ecrite_est_refusee_sans_rien_ecrire(config, entrepot, depuis, jusqu):
    """Une date invalide est une erreur de l'appelant : ni argent, ni alerte, ni lignage."""
    charger_scenario(entrepot)
    etat_avant = instantane(entrepot)
    faute = depuis if depuis in ("21/12/2020", "2020-13-01", "") else jusqu
    with pytest.raises(ValueError) as attendue:
        date.fromisoformat(faute)
    with pytest.raises(ValueError) as erreur:
        retraiter(config, entrepot, depuis, jusqu, modele=ModeleSeuil("m", 100.0))
    assert str(erreur.value) == str(attendue.value)
    assert instantane(entrepot) == etat_avant


def test_sans_modele_en_service_le_retraitement_est_refuse_et_rien_n_est_ecrit(config, entrepot):
    """Sans modèle promu ni modèle fourni, on ne note pas « au hasard » : erreur explicite."""
    charger_scenario(entrepot)
    etat_avant = instantane(entrepot)
    with pytest.raises(ErreurModele) as erreur:
        retraiter(config, entrepot, "2020-12-21", "2020-12-22")
    assert str(erreur.value) == (
        f"Aucun modèle en service dans {config.modele.dossier}. "
        "Lancer l'entraînement : python -m detection_fraude.entrainement"
    )
    assert instantane(entrepot) == etat_avant


# --------------------------------------------------------------------------------------------
# Bornes de période et lignes hors période
# --------------------------------------------------------------------------------------------
def test_les_bornes_sont_incluses_au_debut_et_a_la_derniere_seconde_et_exclues_au_dela(
    config, entrepot, temps_fige
):
    """Période [début du 21, début du 23[ : première et dernière seconde incluses, voisines non."""
    lignes = charger_scenario(entrepot)
    hors_periode = (lignes["avant"]["trans_num"], lignes["apres"]["trans_num"])
    hors_periode_avant = lignes_argent_de(entrepot, *hors_periode)
    resultat = retraiter(
        config, entrepot, "2020-12-21", "2020-12-22", modele=ModeleSeuil("n", 100)
    )
    assert lignes_argent_de(entrepot, *hors_periode) == hors_periode_avant
    assert {r["version_modele"] for r in hors_periode_avant} == {"factice-1"}
    dans_la_periode = [lignes[nom]["trans_num"] for nom in ("B", "C", "D", "G")]
    versions = {r["version_modele"] for r in lignes_argent_de(entrepot, *dans_la_periode)}
    assert versions == {"n"}
    assert resultat["lignes"] == 7
    # B (début) et G (dernière seconde) ont changé de décision ; les deux voisines hors période
    # auraient aussi basculé (500 et 800 > 100) mais ne sont ni comptées ni touchées.
    assert resultat["decisions_modifiees"] == 3


# --------------------------------------------------------------------------------------------
# Scénario complet : décisions, alertes, lignage, événement
# --------------------------------------------------------------------------------------------
def test_le_retraitement_compte_les_decisions_conserve_les_alertes_envoyees_intactes(
    config, entrepot, evenements, temps_fige, monkeypatch
):
    """Décisions, nouvelles fraudes, alertes « rejouée » et alertes envoyées : tout est exact."""
    monkeypatch.setattr(module_retraitement, "TAILLE_PAQUET", 2)
    lignes = charger_scenario(entrepot)
    alerte_d_avant = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=lignes["D"]["trans_num"]
    )
    alerte_c_avant = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=lignes["C"]["trans_num"]
    )[0]
    modele = ModeleSeuil("factice-2", 100.0)
    resultat = retraiter(config, entrepot, "2020-12-21", "2020-12-22", modele=modele)

    assert resultat == {
        "lignes": 7,
        "decisions_modifiees": 3,
        "nouvelles_fraudes": 4,
        "rapports": 0,
        "version_modele": "factice-2",
    }
    assert modele.tailles == [2, 2, 2, 1]  # paquets de TAILLE_PAQUET lignes, dans l'ordre
    assert evenements.tous() == [
        (
            "INFO",
            "retraitement_termine",
            {
                "depuis": "2020-12-21",
                "jusqu": "2020-12-22",
                "lignes": 7,
                "decisions_modifiees": 3,
                "nouvelles_fraudes": 4,
                "modele": "factice-2",
            },
        )
    ]
    # La couche argent d'une ligne retraitée : tous les champs, y compris le lot de retraitement.
    (ligne_b,) = lignes_argent_de(entrepot, lignes["B"]["trans_num"])
    assert ligne_b == {
        "trans_num": lignes["B"]["trans_num"],
        "date_transaction": "2020-12-21",
        "horodatage_utc": "2020-12-21T00:00:00+00:00",
        "horodatage_s": DEBUT_S,
        "carte_hash": lignes["B"]["carte_hash"],
        "carte_fin": "4242",
        "commercant": "Marchand",
        "categorie": "grocery_pos",
        "montant": 150.0,
        "etat": "NY",
        "population_ville": 1000,
        "age": 40,
        "distance_km": 3.0,
        "heure": 0,
        "jour_semaine": 0,
        "nb_tx_24h": 0,
        "montant_relatif": 1.0,
        "score": 0.95,
        "est_fraude": 1,
        "seuil": 0.5,
        "version_modele": "factice-2",
        "version_regles": VERSION_REGLES,
        "etiquette_source": None,
        "source": "jeu",
        "traite_le": INSTANT_FIXE,
        "lot_id": LOT_ATTENDU,
    }
    assert {r["lot_id"] for r in lignes_argent_de(entrepot, lignes["B"]["trans_num"])} == {
        LOT_ATTENDU
    }
    # Alertes : une « rejouée » par nouvelle fraude, jamais renvoyée.
    statuts = {
        a["trans_num"]: a["statut_notification"]
        for a in entrepot.requete("SELECT * FROM or_alertes_fraude")
    }
    attendus = {
        lignes[nom]["trans_num"]: statut
        for nom, statut in {
            "B": "rejouee",
            "C": "envoyee",
            "D": "envoyee",
            "E": "rejouee",
            "G": "rejouee",
            "I": "rejouee",
        }.items()
    }
    assert statuts == attendus
    assert entrepot.alertes_a_envoyer(max_tentatives=5) == []
    (alerte_b,) = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=lignes["B"]["trans_num"]
    )
    assert alerte_b == {
        "trans_num": lignes["B"]["trans_num"],
        "date_transaction": "2020-12-21",
        "horodatage_utc": "2020-12-21T00:00:00+00:00",
        "carte_fin": "4242",
        "commercant": "Marchand",
        "categorie": "grocery_pos",
        "montant": 150.0,
        "score": 0.95,
        "seuil": 0.5,
        "version_modele": "factice-2",
        "source": "jeu",
        "statut_notification": "rejouee",
        "tentatives": 0,
        "derniere_tentative": None,
        "notifiee_le": None,
        "latence_notification_s": None,
        "canaux_ok": "",
        "cree_le": INSTANT_FIXE,
        "detectee_le_s": None,
    }
    # Alerte C (toujours fraude) : la décision est mise à jour, l'état d'envoi est conservé.
    (alerte_c_apres,) = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=lignes["C"]["trans_num"]
    )
    mises_a_jour = {
        "date_transaction": "2020-12-21",
        "horodatage_utc": "2020-12-21T00:01:40+00:00",
        "carte_fin": "4242",
        "commercant": "Marchand",
        "categorie": "grocery_pos",
        "montant": 700.0,
        "score": 0.95,
        "seuil": 0.5,
        "version_modele": "factice-2",
        "source": "jeu",
    }
    assert alerte_c_apres == {**alerte_c_avant, **mises_a_jour}
    # Alerte D (n'est plus une fraude) : conservée telle quelle comme piste d'audit.
    assert (
        entrepot.requete(
            "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=lignes["D"]["trans_num"]
        )
        == alerte_d_avant
    )
    (lignage,) = entrepot.requete("SELECT * FROM meta_lignage")
    details = json.loads(lignage.pop("details"))
    assert {k: v for k, v in lignage.items() if k not in ("id", "fin")} == {
        "etape": "retraitement",
        "entree": "bronze_transactions",
        "sortie": "argent_transactions,or_alertes_fraude",
        "nb_entrees": 7,
        "nb_sorties": 7,
        "nb_rejets": 0,
        "debut": INSTANT_FIXE,
        "version_regles": VERSION_REGLES,
        "version_modele": "factice-2",
    }
    assert details == {
        "depuis": "2020-12-21",
        "jusqu": "2020-12-22",
        "lot_id": LOT_ATTENDU,
        "decisions_modifiees": 3,
        "rapports": 0,
    }


def test_relancer_le_meme_retraitement_laisse_l_etat_inchange_et_ne_compte_plus_rien(
    config, entrepot, temps_fige
):
    """Idempotence : même modèle, même période -> mêmes lignes, aucune décision nouvelle."""
    lignes = scenario_complet()
    deposer_bronze(entrepot, list(lignes.values()))
    dossier = config.modele.dossier
    exporter_modele(dossier, PipelineMontant(100.0), 0.5, {"version": "v1"})
    promouvoir(dossier, "v1")
    premier = retraiter(config, entrepot, "2020-12-21", "2020-12-22")  # modèle en service
    assert premier == {
        "lignes": 7,
        "decisions_modifiees": 0,  # l'argent était vide : aucune ancienne décision à comparer
        "nouvelles_fraudes": 5,
        "rapports": 0,
        "version_modele": "v1",
    }
    etat_initial = instantane(entrepot, *TABLES_ARGENT_ET_ALERTES)
    second = retraiter(config, entrepot, "2020-12-21", "2020-12-22")
    assert second == {
        "lignes": 7,
        "decisions_modifiees": 0,
        "nouvelles_fraudes": 0,
        "rapports": 0,
        "version_modele": "v1",
    }
    assert instantane(entrepot, *TABLES_ARGENT_ET_ALERTES) == etat_initial
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage") == 2
    assert entrepot.alertes_a_envoyer(max_tentatives=5) == []


def test_le_retraitement_ne_modifie_pas_le_bronze_et_tient_compte_de_l_historique_anterieur(
    config, entrepot
):
    """Le bronze, source de vérité, n'est jamais réécrit ; l'historique antérieur compte."""
    carte = "d" * 32
    anterieure = ligne_bronze(1, DEBUT_S - 3600, 10.0, carte)  # hors période, mais dans les 24 h
    premiere = ligne_bronze(2, DEBUT_S + 60, 30.0, carte)
    seconde = ligne_bronze(3, DEBUT_S + 120, 20.0, carte)
    deposer_bronze(entrepot, [seconde, anterieure, premiere])
    bronze_avant = instantane(entrepot, "bronze_transactions")
    resultat = retraiter(config, entrepot, "2020-12-21", "2020-12-21", modele=ModeleSeuil("m", 25))
    assert instantane(entrepot, "bronze_transactions") == bronze_avant
    assert resultat["lignes"] == 2
    argent = entrepot.requete(
        "SELECT trans_num, nb_tx_24h, montant_relatif, est_fraude FROM argent_transactions "
        "ORDER BY horodatage_s"
    )
    assert argent == [
        # 30 / moyenne(10) = 3,0 ; une transaction dans les 24 h précédentes
        {"trans_num": premiere["trans_num"], "nb_tx_24h": 1, "montant_relatif": 3.0,
         "est_fraude": 1},
        # 20 / moyenne(10, 30) = 1,0 ; deux transactions dans les 24 h précédentes
        {"trans_num": seconde["trans_num"], "nb_tx_24h": 2, "montant_relatif": 1.0,
         "est_fraude": 0},
    ]  # fmt: skip


# --------------------------------------------------------------------------------------------
# Panne en cours de route
# --------------------------------------------------------------------------------------------
def test_une_panne_du_modele_en_cours_de_route_ne_declare_rien_de_termine_et_la_relance_converge(
    config, entrepot, evenements, temps_fige, monkeypatch, tmp_path
):
    """Après une panne : ni lignage ni « terminé » ; la relance rejoint un passage propre."""
    monkeypatch.setattr(module_retraitement, "TAILLE_PAQUET", 2)
    lignes = charger_scenario(entrepot)
    propre = Entrepot(f"sqlite:///{(tmp_path / 'propre.db').as_posix()}")
    propre.initialiser()
    try:
        charger_scenario(propre)
        retraiter(config, propre, "2020-12-21", "2020-12-22", modele=ModeleSeuil("n", 100.0))
        etat_propre = instantane(propre, *TABLES_ARGENT_ET_ALERTES)
    finally:
        propre.fermer()
    evenements.enregistrements.clear()

    panne = ModeleQuiCasse("n", 100.0)
    with pytest.raises(RuntimeError) as erreur:
        retraiter(config, entrepot, "2020-12-21", "2020-12-22", modele=panne)
    assert str(erreur.value) == "panne du modèle (test)"
    assert panne.tailles == [2, 2]  # le 1er paquet est passé, le 2e a cassé
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage") == 0
    assert evenements.nommes("retraitement_termine") == []
    assert lignes["apres"]["trans_num"] in {
        r["trans_num"] for r in entrepot.requete("SELECT trans_num FROM argent_transactions")
    }

    retraiter(config, entrepot, "2020-12-21", "2020-12-22", modele=ModeleSeuil("n", 100.0))
    assert instantane(entrepot, *TABLES_ARGENT_ET_ALERTES) == etat_propre


# --------------------------------------------------------------------------------------------
# Rapports régénérés
# --------------------------------------------------------------------------------------------
def bronze_de_trois_jours() -> list[dict[str, Any]]:
    """Des paiements les 21 et 23/12 seulement : le 22 est un jour sans aucune transaction."""
    return [
        ligne_bronze(1, DEBUT_S + 60, 150.0),
        ligne_bronze(2, DEBUT_S + 120, 20.0),
        ligne_bronze(3, DEBUT_S + 2 * JOUR_S + 60, 300.0),
    ]


def test_sans_l_option_rapports_aucun_rapport_n_est_produit_et_les_tables_or_restent_intactes(
    config, entrepot
):
    """Par défaut le retraitement ne régénère rien : ni fichier, ni ligne de rapport."""
    deposer_bronze(entrepot, bronze_de_trois_jours())
    tables = ("or_rapport_quotidien", "or_rapport_categorie")
    etat_avant = instantane(entrepot, *tables)
    resultat = retraiter(
        config, entrepot, "2020-12-21", "2020-12-23", modele=ModeleSeuil("m", 100)
    )
    assert resultat["rapports"] == 0
    assert instantane(entrepot, *tables) == etat_avant
    assert not config.rapport.dossier.exists()


def test_avec_rapports_chaque_jour_est_regenere_les_jours_vides_sont_ignores_le_reste_intact(
    config, entrepot, garde_regeneration
):
    """Bornes incluses (21 et 23), jour vide (22) ignoré et non compté, hors période intact."""
    deposer_bronze(entrepot, bronze_de_trois_jours())
    with entrepot.transaction() as conn:
        conn.exec_driver_sql(
            "INSERT INTO or_rapport_quotidien (jour, nb_paiements, nb_fraudes, montant_total, "
            "montant_fraudes, taux_fraude_nb, taux_fraude_montant, nb_rejets, nb_etiquetees, "
            "nb_vrais_positifs, nb_fraudes_reelles, genere_le, version_regles) "
            "VALUES ('2020-12-20', 9, 1, 90.0, 9.0, 0.1, 0.1, 0, 0, 0, 0, 'hier', 'r0')"
        )
    rapport_hors_periode_avant = entrepot.requete(
        "SELECT * FROM or_rapport_quotidien WHERE jour = '2020-12-20'"
    )
    resultat = retraiter(
        config,
        entrepot,
        "2020-12-21",
        "2020-12-23",
        modele=ModeleSeuil("m", 100),
        regenerer_rapports=True,
    )
    assert resultat["rapports"] == 2
    assert garde_regeneration == ["2020-12-21", "2020-12-22", "2020-12-23"]
    jours = [r["jour"] for r in entrepot.requete("SELECT jour FROM or_rapport_quotidien")]
    assert sorted(jours) == ["2020-12-20", "2020-12-21", "2020-12-23"]
    assert (
        entrepot.requete("SELECT * FROM or_rapport_quotidien WHERE jour = '2020-12-20'")
        == rapport_hors_periode_avant
    )
    assert sorted(p.name for p in config.rapport.dossier.iterdir()) == [
        "fraudes_2020-12-21.csv",
        "fraudes_2020-12-23.csv",
        "rapport_2020-12-21.html",
        "rapport_2020-12-23.html",
    ]
    (lignage,) = entrepot.requete("SELECT details FROM meta_lignage WHERE etape = 'retraitement'")
    assert json.loads(lignage["details"])["rapports"] == 2


def test_une_erreur_autre_que_donnees_absentes_n_est_pas_avalee_et_aucun_lignage_n_est_ecrit(
    config, entrepot, evenements, garde_regeneration
):
    """Seul un jour sans donnée est toléré : un dossier de rapport inutilisable fait échouer."""
    deposer_bronze(entrepot, bronze_de_trois_jours())
    config.rapport.dossier.parent.mkdir(parents=True, exist_ok=True)
    config.rapport.dossier.write_text("occupé", encoding="utf-8")
    with pytest.raises(FileExistsError):
        retraiter(
            config,
            entrepot,
            "2020-12-21",
            "2020-12-23",
            modele=ModeleSeuil("m", 100),
            regenerer_rapports=True,
        )
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage WHERE etape = 'retraitement'") == 0
    assert evenements.nommes("retraitement_termine") == []
    assert config.rapport.dossier.read_text(encoding="utf-8") == "occupé"


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
def exporter_deux_modeles(config) -> None:
    dossier = config.modele.dossier
    exporter_modele(dossier, PipelineMontant(100.0), 0.5, {"version": "v1"})
    exporter_modele(dossier, PipelineMontant(500.0), 0.5, {"version": "v2"})
    promouvoir(dossier, "v1")


def test_main_refuse_une_version_inconnue_sans_rien_ecrire_puis_accepte_une_version_existante(
    config, entrepot, capsys, evenements, journal_enregistre, fermetures, temps_fige
):
    """``--modele`` inconnu : erreur nette, base intacte ; ``--modele v2`` change les décisions."""
    exporter_deux_modeles(config)
    deposer_bronze(
        entrepot, [ligne_bronze(1, DEBUT_S + 10, 150.0), ligne_bronze(2, DEBUT_S + 20, 700.0)]
    )
    argv = ["--depuis", "2020-12-21", "--jusqu", "2020-12-21"]
    etat_avant = instantane(entrepot)
    with pytest.raises(ErreurModele) as erreur:
        module_retraitement.main([*argv, "--modele", "v-inconnu"])
    assert str(erreur.value) == "Métadonnées absentes pour la version v-inconnu"
    assert instantane(entrepot) == etat_avant
    assert fermetures == ["sqlite"]
    assert capsys.readouterr().out == ""

    code_sortie = module_retraitement.main(argv)  # modèle en service : v1 (plafond 100)
    assert code_sortie == 0
    assert capsys.readouterr().out == (
        "{'lignes': 2, 'decisions_modifiees': 0, 'nouvelles_fraudes': 2, 'rapports': 0, "
        "'version_modele': 'v1'}\n"
    )
    code_sortie = module_retraitement.main([*argv, "--modele", "v2"])  # v2 : plafond 500
    assert code_sortie == 0
    assert capsys.readouterr().out == (
        "{'lignes': 2, 'decisions_modifiees': 1, 'nouvelles_fraudes': 0, 'rapports': 0, "
        "'version_modele': 'v2'}\n"
    )
    assert fermetures == ["sqlite", "sqlite", "sqlite"]
    assert journal_enregistre == [("INFO", "lisible", config.journal.fichier)] * 3


def test_main_sans_modele_en_service_ferme_l_entrepot_et_ne_modifie_pas_l_argent(
    config, entrepot, evenements, journal_enregistre, fermetures
):
    """Même en cas d'échec, la connexion à la base est rendue et rien n'a été réécrit."""
    deposer_bronze(entrepot, [ligne_bronze(1, DEBUT_S + 10, 150.0)])
    inserer(entrepot, [ancienne_ligne(ligne_bronze(1, DEBUT_S + 10, 150.0), 0)])
    argent_avant = instantane(entrepot, "argent_transactions")
    with pytest.raises(ErreurModele) as erreur:
        module_retraitement.main(["--depuis", "2020-12-21", "--jusqu", "2020-12-21"])
    assert str(erreur.value).startswith(f"Aucun modèle en service dans {config.modele.dossier}.")
    assert fermetures == ["sqlite"]
    assert instantane(entrepot, "argent_transactions") == argent_avant


@pytest.mark.parametrize(
    "argv",
    [[], ["--depuis", "2020-12-21"], ["--jusqu", "2020-12-21"]],
    ids=["aucun", "sans_jusqu", "sans_depuis"],
)
def test_main_exige_depuis_et_jusqu_avec_le_code_2_et_ne_cree_aucune_base(
    config, capsys, journal_enregistre, argv
):
    """Les deux bornes sont obligatoires : sans elles, rien n'est ouvert, rien n'est créé."""
    with pytest.raises(SystemExit) as sortie:
        module_retraitement.main(argv)
    assert sortie.value.code == 2
    manquantes = {
        (): "--depuis, --jusqu",
        ("--depuis", "2020-12-21"): "--jusqu",
        ("--jusqu", "2020-12-21"): "--depuis",
    }[tuple(argv)]
    assert f"error: the following arguments are required: {manquantes}" in capsys.readouterr().err
    assert not config.racine_donnees.exists()
    assert journal_enregistre == []


def test_main_aide_decrit_chaque_option_et_quitte_avec_le_code_0(capsys, monkeypatch):
    """``--help`` documente la commande et toutes ses options sans rien exécuter."""
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as sortie:
        module_retraitement.main(["--help"])
    assert sortie.value.code == 0
    aide = capsys.readouterr().out
    assert "Recalcule argent et alertes depuis le bronze." in aide
    assert "--depuis DEPUIS" in aide and "premier jour (AAAA-MM-JJ, UTC)" in aide
    assert "--jusqu JUSQU" in aide and "dernier jour inclus (AAAA-MM-JJ, UTC)" in aide
    assert "--modele MODELE" in aide and "version du modèle (défaut : celui en service)" in aide
    assert "--rapports" in aide and "régénère les rapports du jour" in aide


@pytest.mark.parametrize(
    ("options", "version_attendue", "rapports_attendus"),
    [
        ([], None, False),
        (["--modele", "v7"], "v7", False),
        (["--rapports"], None, True),
        (["--rapports", "--modele", "v9"], "v9", True),
    ],
)
def test_main_transmet_les_options_a_retraiter(
    config,
    entrepot,
    capsys,
    journal_enregistre,
    monkeypatch,
    options,
    version_attendue,
    rapports_attendus,
):
    """Les valeurs lues en ligne de commande sont exactement celles reçues par ``retraiter``."""
    modele = object()
    appels_charger: list[tuple[Any, ...]] = []
    appels_retraiter: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def faux_charger(dossier, version=None):
        appels_charger.append((dossier, version))
        return modele

    def faux_retraiter(*args, **kwargs):
        appels_retraiter.append((args, kwargs))
        return {"ok": 1}

    monkeypatch.setattr(module_retraitement, "charger_modele", faux_charger)
    monkeypatch.setattr(module_retraitement, "retraiter", faux_retraiter)
    base_avant = instantane(entrepot)
    code_sortie = module_retraitement.main(
        ["--depuis", "2020-12-21", "--jusqu", "2020-12-22", *options]
    )
    assert code_sortie == 0
    assert instantane(entrepot) == base_avant  # main n'écrit rien lui-même
    assert appels_charger == [(config.modele.dossier, version_attendue)]
    ((args, kwargs),) = appels_retraiter
    assert args[0] == config and isinstance(args[1], Entrepot)
    assert args[2:] == ("2020-12-21", "2020-12-22")
    assert kwargs == {"modele": modele, "regenerer_rapports": rapports_attendus}
    assert capsys.readouterr().out == "{'ok': 1}\n"


# --------------------------------------------------------------------------------------------
# Précisions : ce que les tests ci-dessus laissaient passer (relevé par la mutation)
# --------------------------------------------------------------------------------------------
def test_le_retraitement_commence_par_lire_les_decisions_anciennes_avec_la_requete_documentee(
    config, entrepot, temps_fige, instructions_sql
):
    """Avant de réécrire, il note la décision actuelle de chaque ligne de la période."""
    retraiter(config, entrepot, "2020-12-21", "2020-12-21", modele=ModeleSeuil("factice-2", 100.0))
    assert instructions_sql[0] == (
        "SELECT trans_num, est_fraude FROM argent_transactions "
        "WHERE horodatage_s >= ? AND horodatage_s < ?"
    )
