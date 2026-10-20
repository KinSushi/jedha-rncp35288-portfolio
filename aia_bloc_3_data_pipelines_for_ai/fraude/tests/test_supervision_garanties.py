# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Supervision : seuils exacts, messages exacts, gravité, codes de sortie (critère 6.1).

Une supervision qui crie trop est ignorée, une supervision qui se tait trompe. Ces tests fixent
donc chaque seuil *pile à la limite* (ne pas alerter), *une unité au-delà* (alerter, avec le
message exact) et vérifient que contrôler ne modifie jamais ce qu'il surveille.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from detection_fraude import supervision as module_supervision
from detection_fraude.configuration import Configuration
from detection_fraude.entrepot import Entrepot
from detection_fraude.modele import NOM_POINTEUR
from detection_fraude.notification import Notificateur
from detection_fraude.supervision import Constat, controler, notifier_constats
from tests.helpers import CanalEnregistreur
from tests.test_rapport import ligne_argent
from tests.test_rapport_garanties import inserer, instantane
from tests.test_supervision import alerte, en_service

MAINTENANT = datetime(2026, 10, 10, 7, 20, tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Outils de test
# --------------------------------------------------------------------------------------------
def il_y_a(**duree: float) -> str:
    """Instant ISO (UTC, à la seconde) situé ``duree`` avant MAINTENANT."""
    return (MAINTENANT - timedelta(**duree)).isoformat(timespec="seconds")


def traiter(entrepot: Entrepot, *instants: str) -> None:
    """Insère un paiement traité à chacun des instants donnés."""
    inserer(entrepot, [{**ligne_argent(100 + i), "traite_le": t} for i, t in enumerate(instants)])


def rejeter_a(entrepot: Entrepot, *instants: str) -> None:
    """Insère un rejet reçu à chacun des instants donnés."""
    with entrepot.transaction() as conn:
        for rang, instant in enumerate(instants):
            conn.execute(
                text(
                    "INSERT INTO meta_rejets (sujet, cle_message, etape, raison, trans_num, "
                    "erreurs, charge_utile, date_evenement, recu_le) VALUES ('r', :cle, "
                    "'producteur', 'x', NULL, '[]', '{}', '2026-10-10', :recu)"
                ),
                {"cle": str(rang), "recu": instant},
            )


def deposer(entrepot: Entrepot, *alertes: dict[str, Any]) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_alertes(conn, list(alertes))


def ecrire_etat_consommateur(config: Configuration, contenu: dict[str, Any]) -> None:
    etat = config.racine_donnees / "etat" / "consommateur.json"
    etat.parent.mkdir(parents=True, exist_ok=True)
    etat.write_text(json.dumps(contenu), encoding="utf-8")


def promu_depuis(config: Configuration, secondes: float) -> None:
    """Date le pointeur ``ACTUEL`` de ``secondes`` avant MAINTENANT."""
    instant = MAINTENANT.timestamp() - secondes
    os.utime(config.modele.dossier / NOM_POINTEUR, (instant, instant))


def avec_sla(config: Configuration, **seuils: float) -> Configuration:
    return replace(config, sla=replace(config.sla, **seuils))


def etat_complet(entrepot: Entrepot, config: Configuration) -> dict[str, Any]:
    """Base + fichiers du modèle et de l'état du consommateur (contenu et date de modification)."""
    fichiers = {}
    for dossier in (config.modele.dossier, config.racine_donnees / "etat"):
        for chemin in sorted(dossier.rglob("*")):
            if chemin.is_file():
                fichiers[str(chemin)] = (chemin.read_bytes(), chemin.stat().st_mtime)
    return {"base": instantane(entrepot), "fichiers": fichiers}


class HorlogeFixe(datetime):
    """Remplace la classe ``datetime`` du module : ``now`` rend toujours MAINTENANT."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return MAINTENANT if tz is None else MAINTENANT.astimezone(tz)


@pytest.fixture
def fermetures(monkeypatch):
    """Enregistre chaque fermeture de l'entrepôt ouvert par ``main``."""
    appels: list[str] = []

    class EntrepotEspion(Entrepot):
        def fermer(self) -> None:
            appels.append(self.dialecte)
            super().fermer()

    monkeypatch.setattr(module_supervision, "Entrepot", EntrepotEspion)
    return appels


@pytest.fixture
def journal_enregistre(monkeypatch):
    """Remplace ``configurer_journal`` : garde ses arguments et laisse le collecteur en place."""
    appels: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        module_supervision, "configurer_journal", lambda *args: appels.append(args)
    )
    return appels


@pytest.fixture
def canaux_construits(monkeypatch):
    """Espionne ``construire_canaux`` (avec les vrais canaux : journal et fichier)."""
    appels: list[Configuration] = []
    reel = module_supervision.construire_canaux

    def espion(config):
        appels.append(config)
        return reel(config)

    monkeypatch.setattr(module_supervision, "construire_canaux", espion)
    return appels


# --------------------------------------------------------------------------------------------
# Seuils et messages exacts de controler (un cas = une situation, un résultat complet)
# --------------------------------------------------------------------------------------------
MODELE_ABSENT = Constat("modele_absent", "critique", "Aucun modèle en service.")


def flux_en_retard(minutes: int, seuil: int = 15) -> Constat:
    return Constat(
        "flux_silencieux",
        "haute",
        f"Dernière transaction traitée il y a {minutes} min (seuil : {seuil} min).",
    )


def bloquees(nombre: int, seuil: int = 10) -> Constat:
    return Constat(
        "notifications_bloquees",
        "critique",
        f"{nombre} alerte(s) de fraude en attente d'envoi depuis plus de {seuil} min.",
    )


def rejets_eleves(rejets: int, total: int, pourcentage: str, seuil: str = "2.0") -> Constat:
    return Constat(
        "taux_rejets_eleve",
        "haute",
        f"{rejets} rejet(s) sur {total} lignes la dernière heure "
        f"({pourcentage} % ; seuil {seuil} %).",
    )


def non_applique(minutes: int, utilise: str = "v-ancien") -> Constat:
    return Constat(
        "modele_non_applique",
        "haute",
        f"Le modèle v1 est promu depuis {minutes} min, mais le consommateur note encore avec "
        f"{utilise}.",
    )


def sans_modele(entrepot, config):
    """Aucun fichier ACTUEL."""


def pointeur_vide(entrepot, config):
    config.modele.dossier.mkdir(parents=True, exist_ok=True)
    (config.modele.dossier / NOM_POINTEUR).write_text("\n", encoding="utf-8")


def rien_de_special(entrepot, config):
    en_service(config)


def un_paiement_il_y_a(**duree):
    def preparer(entrepot, config):
        en_service(config)
        traiter(entrepot, il_y_a(**duree))

    return preparer


def paiements_aux_instants(*instants):
    def preparer(entrepot, config):
        en_service(config)
        traiter(entrepot, *instants)

    return preparer


def alertes_a_envoyer_il_y_a(*ages):
    def preparer(entrepot, config):
        en_service(config)
        deposer(
            entrepot,
            *[alerte(f"{i:032x}", "a_envoyer", il_y_a(**age)) for i, age in enumerate(ages)],
        )

    return preparer


def lignes_de_la_derniere_heure(rejets: int, traites: int, age_rejets=None, age_traites=None):
    age_rejets = age_rejets or {"minutes": 5}
    age_traites = age_traites or {"minutes": 5}

    def preparer(entrepot, config):
        en_service(config)
        rejeter_a(entrepot, *[il_y_a(**age_rejets)] * rejets)
        traiter(entrepot, *[il_y_a(**age_traites)] * traites)

    return preparer


def modele_en_retard(secondes: float, contenu: dict[str, Any] | None):
    def preparer(entrepot, config):
        en_service(config)
        if contenu is not None:
            ecrire_etat_consommateur(config, contenu)
        promu_depuis(config, secondes)

    return preparer


def consommateur_actif_sans_modele_en_service(pointeur_vide: bool):
    """Un état de consommateur existe, mais plus aucun modèle n'est promu."""

    def preparer(entrepot, config):
        config.modele.dossier.mkdir(parents=True, exist_ok=True)
        if pointeur_vide:
            (config.modele.dossier / NOM_POINTEUR).write_text("\n", encoding="utf-8")
            promu_depuis(config, 600)
        ecrire_etat_consommateur(config, {"modele": "v-ancien"})

    return preparer


CAS_CONTROLE = {
    "aucun_modele": (sans_modele, {}, False, [MODELE_ABSENT]),
    "pointeur_vide_compte_comme_aucun_modele": (pointeur_vide, {}, False, [MODELE_ABSENT]),
    "tout_va_bien": (un_paiement_il_y_a(minutes=1), {}, True, []),
    "flux_jamais_traite": (
        rien_de_special,
        {},
        True,
        [Constat("flux_silencieux", "haute", "Aucune transaction traitée.")],
    ),
    "flux_non_surveille": (rien_de_special, {}, False, []),
    "flux_pile_au_seuil": (un_paiement_il_y_a(minutes=15), {}, True, []),
    "flux_une_seconde_au_dela": (
        un_paiement_il_y_a(minutes=15, seconds=1),
        {},
        True,
        [flux_en_retard(15)],
    ),
    "flux_45_minutes": (un_paiement_il_y_a(minutes=45), {}, True, [flux_en_retard(45)]),
    "flux_seuil_personnalise": (
        un_paiement_il_y_a(minutes=3),
        {"fraicheur_max_min": 2.0},
        True,
        [flux_en_retard(3, 2)],
    ),
    "flux_seuil_personnalise_pile": (
        un_paiement_il_y_a(minutes=2),
        {"fraicheur_max_min": 2.0},
        True,
        [],
    ),
    "flux_le_plus_recent_compte": (
        paiements_aux_instants(il_y_a(hours=2), il_y_a(minutes=1)),
        {},
        True,
        [],
    ),
    "flux_decalage_horaire_compare_en_instants": (
        paiements_aux_instants(
            (MAINTENANT - timedelta(minutes=1))
            .astimezone(timezone(timedelta(hours=2)))
            .isoformat(timespec="seconds")
        ),
        {},
        True,
        [],
    ),
    "alerte_en_attente_pile_au_seuil": (alertes_a_envoyer_il_y_a({"minutes": 10}), {}, False, []),
    "alerte_en_attente_une_seconde_au_dela": (
        alertes_a_envoyer_il_y_a({"minutes": 10, "seconds": 1}),
        {},
        False,
        [bloquees(1)],
    ),
    "alertes_en_attente_comptees": (
        alertes_a_envoyer_il_y_a({"minutes": 30}, {"hours": 5}, {"days": 2}, {"minutes": 1}),
        {},
        False,
        [bloquees(3)],
    ),
    "seuil_d_attente_personnalise": (
        alertes_a_envoyer_il_y_a({"minutes": 3}),
        {"alertes_en_attente_max_min": 2.0},
        False,
        [bloquees(1, 2)],
    ),
    "rejets_19_lignes_trop_peu_pour_juger": (
        lignes_de_la_derniere_heure(rejets=10, traites=9),
        {},
        False,
        [],
    ),
    "rejets_20_lignes_suffisent": (
        lignes_de_la_derniere_heure(rejets=10, traites=10),
        {},
        False,
        [rejets_eleves(10, 20, "50.0")],
    ),
    "rejets_ratio_egal_au_seuil": (
        lignes_de_la_derniere_heure(rejets=1, traites=49),
        {},
        False,
        [],
    ),
    "rejets_juste_au_dessus_du_seuil": (
        lignes_de_la_derniere_heure(rejets=2, traites=48),
        {},
        False,
        [rejets_eleves(2, 50, "4.0")],
    ),
    "rejets_seuil_personnalise_egal": (
        lignes_de_la_derniere_heure(rejets=10, traites=10),
        {"taux_rejets_max": 0.5},
        False,
        [],
    ),
    "rejets_seuil_personnalise_depasse": (
        lignes_de_la_derniere_heure(rejets=11, traites=9),
        {"taux_rejets_max": 0.5},
        False,
        [rejets_eleves(11, 20, "55.0", "50.0")],
    ),
    "rejets_et_traites_plus_vieux_qu_une_heure_ignores": (
        lignes_de_la_derniere_heure(
            rejets=10,
            traites=10,
            age_rejets={"hours": 1, "seconds": 1},
            age_traites={"hours": 1, "seconds": 1},
        ),
        {},
        False,
        [],
    ),
    "rejets_et_traites_pile_a_une_heure_comptent": (
        lignes_de_la_derniere_heure(
            rejets=10, traites=10, age_rejets={"hours": 1}, age_traites={"hours": 1}
        ),
        {},
        False,
        [rejets_eleves(10, 20, "50.0")],
    ),
    "modele_promu_depuis_180_s_pile_laisse_le_temps_de_recharger": (
        modele_en_retard(180, {"modele": "v-ancien"}),
        {},
        False,
        [],
    ),
    "modele_promu_depuis_181_s": (
        modele_en_retard(181, {"modele": "v-ancien"}),
        {},
        False,
        [non_applique(3)],
    ),
    "modele_promu_depuis_10_min": (
        modele_en_retard(600, {"modele": "v-ancien"}),
        {},
        False,
        [non_applique(10)],
    ),
    "modele_promu_depuis_une_heure": (
        modele_en_retard(3600, {"modele": "v-ancien"}),
        {},
        False,
        [non_applique(60)],
    ),
    "modele_utilise_egal_au_modele_promu": (
        modele_en_retard(600, {"modele": "v1"}),
        {},
        False,
        [],
    ),
    "modele_utilise_inconnu_du_consommateur": (modele_en_retard(600, {}), {}, False, []),
    "modele_utilise_nul": (modele_en_retard(600, {"modele": None}), {}, False, []),
    "modele_utilise_vide": (modele_en_retard(600, {"modele": ""}), {}, False, []),
    "consommateur_jamais_demarre": (modele_en_retard(600, None), {}, False, []),
    "pointeur_vide_et_consommateur_actif_seul_modele_absent_est_signale": (
        consommateur_actif_sans_modele_en_service(pointeur_vide=True),
        {},
        False,
        [MODELE_ABSENT],
    ),
    "pointeur_absent_et_consommateur_actif_seul_modele_absent_est_signale": (
        consommateur_actif_sans_modele_en_service(pointeur_vide=False),
        {},
        False,
        [MODELE_ABSENT],
    ),
}


@pytest.mark.parametrize(
    ("preparer", "seuils", "surveiller_flux", "attendu"),
    list(CAS_CONTROLE.values()),
    ids=list(CAS_CONTROLE),
)
def test_controler_donne_exactement_les_constats_attendus_aux_bornes_de_chaque_seuil(
    config, entrepot, preparer, seuils, surveiller_flux, attendu
):
    """Pile au seuil : silence ; une unité au-delà : le constat exact, avec son message."""
    preparer(entrepot, config)
    base_avant = instantane(entrepot)
    constats = controler(
        entrepot, avec_sla(config, **seuils), MAINTENANT, surveiller_flux=surveiller_flux
    )
    assert constats == attendu
    assert instantane(entrepot) == base_avant  # contrôler ne fait que lire


def test_controler_ne_modifie_ni_la_base_ni_les_fichiers_et_renvoie_tous_les_constats_dans_l_ordre(
    config, entrepot
):
    """Tout va mal à la fois : cinq constats ordonnés, et ni la base ni les fichiers ne bougent."""
    en_service(config)
    ecrire_etat_consommateur(config, {"modele": "v-ancien"})
    promu_depuis(config, 600)
    traiter(entrepot, *[il_y_a(minutes=45)] * 20)
    rejeter_a(entrepot, *[il_y_a(minutes=5)] * 10)
    deposer(
        entrepot,
        alerte("a" * 32, "a_envoyer", il_y_a(minutes=30)),
        alerte("b" * 32, "echec", il_y_a(minutes=30)),
    )
    etat_avant = etat_complet(entrepot, config)
    constats = controler(entrepot, config, MAINTENANT)
    assert constats == [
        non_applique(10),
        flux_en_retard(45),
        bloquees(1),
        Constat(
            "notifications_en_echec",
            "critique",
            "1 alerte(s) n'ont pas pu être envoyées (tentatives épuisées).",
        ),
        rejets_eleves(10, 30, "33.3"),
    ]
    assert etat_complet(entrepot, config) == etat_avant


def test_sans_instant_fourni_controler_prend_l_heure_courante_et_ne_modifie_pas_la_base(
    config, entrepot, monkeypatch
):
    """Par défaut, « maintenant » est l'heure réelle : un paiement vieux de 20 min est signalé."""
    monkeypatch.setattr(module_supervision, "datetime", HorlogeFixe)
    en_service(config)
    traiter(entrepot, il_y_a(minutes=20))
    etat_avant = etat_complet(entrepot, config)
    assert controler(entrepot, config) == [flux_en_retard(20)]
    assert controler(entrepot, config, surveiller_flux=False) == []
    assert etat_complet(entrepot, config) == etat_avant


# --------------------------------------------------------------------------------------------
# notifier_constats
# --------------------------------------------------------------------------------------------
def test_seuls_les_constats_haute_et_critique_sont_notifies_les_autres_sont_sans_effet(
    evenements,
):
    """Sujet, corps et données exacts pour les graves ; « info » ou inconnue ne part pas."""
    canal = CanalEnregistreur()
    constats = [
        Constat("information", "info", "Rien à signaler."),
        Constat("flux_silencieux", "haute", "Aucune transaction traitée."),
        Constat("modele_absent", "critique", "Aucun modèle en service."),
        Constat("autre", "moyenne", "Gravité non prévue."),
        Constat("INFO_MAJUSCULE", "INFO", "Casse différente."),
    ]
    constats_avant = list(constats)
    notifier_constats(Notificateur([canal]), constats)
    assert constats == constats_avant
    assert [(n.type, n.sujet, n.corps, n.donnees) for n in canal.recus] == [
        (
            "supervision",
            "[SUPERVISION HAUTE] flux_silencieux",
            "Aucune transaction traitée.",
            {
                "code": "flux_silencieux",
                "gravite": "haute",
                "message": "Aucune transaction traitée.",
            },
        ),
        (
            "supervision",
            "[SUPERVISION CRITIQUE] modele_absent",
            "Aucun modèle en service.",
            {
                "code": "modele_absent",
                "gravite": "critique",
                "message": "Aucun modèle en service.",
            },
        ),
    ]
    assert evenements.tous() == [
        ("WARNING", "constat_supervision", {"code": "flux_silencieux", "gravite": "haute"}),
        ("WARNING", "constat_supervision", {"code": "modele_absent", "gravite": "critique"}),
    ]


def test_notifier_une_liste_vide_ou_sans_gravite_est_sans_effet(evenements):
    """Aucun constat grave : aucun message, aucun événement d'alerte."""
    canal = CanalEnregistreur()
    notifier_constats(Notificateur([canal]), [])
    notifier_constats(Notificateur([canal]), [Constat("info_seule", "info", "Tout va bien.")])
    assert canal.recus == []
    assert evenements.tous() == []


def test_un_canal_en_panne_n_empeche_pas_les_autres_de_recevoir_les_constats(evenements):
    """Un canal tombé est tracé, mais le constat grave arrive quand même sur l'autre canal."""
    en_panne = CanalEnregistreur("panne", echecs=5)
    vivant = CanalEnregistreur("vivant")
    constats = [Constat("modele_absent", "critique", "Aucun modèle.")]
    constats_avant = list(constats)
    notifier_constats(Notificateur([en_panne, vivant]), constats)
    assert constats == constats_avant
    assert [n.sujet for n in vivant.recus] == ["[SUPERVISION CRITIQUE] modele_absent"]
    assert en_panne.recus == []
    assert evenements.nommes("canal_en_echec") == [
        ("WARNING", {"canal": "panne", "erreur": "ConnectionError"})
    ]


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
SORTIE_SANS_MODELE_AVEC_FLUX = """{
  "ok": false,
  "constats": [
    {
      "code": "modele_absent",
      "gravite": "critique",
      "message": "Aucun modèle en service."
    },
    {
      "code": "flux_silencieux",
      "gravite": "haute",
      "message": "Aucune transaction traitée."
    }
  ]
}
"""

SORTIE_SANS_MODELE_SANS_FLUX = """{
  "ok": false,
  "constats": [
    {
      "code": "modele_absent",
      "gravite": "critique",
      "message": "Aucun modèle en service."
    }
  ]
}
"""

SORTIE_SAINE = """{
  "ok": true,
  "constats": []
}
"""


def test_main_retourne_1_avec_les_constats_en_json_et_0_quand_tout_va_bien(
    config, entrepot, capsys, evenements, journal_enregistre, fermetures
):
    """Code 1 = alerte (sonde Docker), 0 = santé ; le JSON garde les accents et l'indentation."""
    code_sortie = module_supervision.main([])
    assert code_sortie == 1
    sortie = capsys.readouterr()
    assert sortie.out == SORTIE_SANS_MODELE_AVEC_FLUX
    assert sortie.err == ""
    en_service(config)
    inserer(entrepot, [ligne_argent(1)])  # traité à l'instant : le flux est frais
    assert module_supervision.main([]) == 0
    assert capsys.readouterr().out == SORTIE_SAINE
    assert journal_enregistre == [("INFO", "lisible", config.journal.fichier)] * 2
    assert fermetures == ["sqlite", "sqlite"]


def test_main_avec_sans_flux_ne_controle_pas_la_fraicheur_mais_garde_le_code_1(
    config, capsys, evenements, journal_enregistre
):
    """``--sans-flux`` retire seulement le constat de fraîcheur : le reste alerte toujours."""
    code_sortie = module_supervision.main(["--sans-flux"])
    assert code_sortie == 1
    assert capsys.readouterr().out == SORTIE_SANS_MODELE_SANS_FLUX


def test_main_sans_notifier_n_envoie_rien_meme_quand_il_y_a_des_constats(
    config, capsys, evenements, journal_enregistre, canaux_construits
):
    """Sans ``--notifier``, la commande observe seulement : aucun canal, aucun fichier d'alerte."""
    code_sortie = module_supervision.main([])
    assert code_sortie == 1
    capsys.readouterr()
    assert canaux_construits == []
    assert not config.notification.fichier_alertes.exists()


def test_main_avec_notifier_envoie_les_constats_graves_seulement_quand_il_y_en_a(
    config, entrepot, capsys, evenements, journal_enregistre, canaux_construits
):
    """``--notifier`` : rien d'envoyé si tout va bien, un message par constat grave sinon."""
    en_service(config)
    inserer(entrepot, [ligne_argent(1)])
    assert module_supervision.main(["--notifier"]) == 0
    assert capsys.readouterr().out == SORTIE_SAINE
    assert canaux_construits == []
    assert not config.notification.fichier_alertes.exists()
    (config.modele.dossier / NOM_POINTEUR).unlink()  # le modèle disparaît
    code_sortie = module_supervision.main(["--notifier", "--sans-flux"])
    assert code_sortie == 1
    capsys.readouterr()
    assert [c.environnement for c in canaux_construits] == ["dev"]
    lignes = config.notification.fichier_alertes.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ligne) for ligne in lignes] == [
        {
            "type": "supervision",
            "sujet": "[SUPERVISION CRITIQUE] modele_absent",
            "corps": "Aucun modèle en service.",
            "donnees": {
                "code": "modele_absent",
                "gravite": "critique",
                "message": "Aucun modèle en service.",
            },
        }
    ]


@pytest.mark.parametrize(
    ("argv", "flux_attendu"), [([], True), (["--sans-flux"], False)], ids=["defaut", "sans_flux"]
)
def test_main_transmet_surveiller_flux_a_controler_et_ferme_l_entrepot_si_le_controle_echoue(
    config, evenements, journal_enregistre, fermetures, monkeypatch, argv, flux_attendu
):
    """Une panne du contrôle remonte telle quelle, mais la base est toujours refermée."""
    appels: list[tuple[Any, dict[str, Any]]] = []

    def controle_en_panne(*args, **kwargs):
        appels.append((args, kwargs))
        raise RuntimeError("contrôle impossible (test)")

    monkeypatch.setattr(module_supervision, "controler", controle_en_panne)
    with pytest.raises(RuntimeError) as erreur:
        module_supervision.main(argv)
    assert str(erreur.value) == "contrôle impossible (test)"
    ((args, kwargs),) = appels
    assert isinstance(args[0], Entrepot) and args[1] == config
    assert args[2:] == ()  # seuls l'entrepôt et la configuration sont transmis par position
    assert kwargs == {"surveiller_flux": flux_attendu}
    assert fermetures == ["sqlite"]


def test_main_refuse_un_argument_inconnu_avec_le_code_2_et_ne_cree_aucune_base(
    config, capsys, journal_enregistre
):
    """Une option mal écrite n'ouvre ni base ni journal : la sonde reçoit le code d'usage 2."""
    with pytest.raises(SystemExit) as sortie:
        module_supervision.main(["--flux"])
    assert sortie.value.code == 2
    assert "error: unrecognized arguments: --flux" in capsys.readouterr().err
    assert not config.racine_donnees.exists()
    assert journal_enregistre == []


def test_main_aide_decrit_les_options_et_le_code_de_sortie_puis_quitte_avec_0(capsys, monkeypatch):
    """``--help`` annonce le code 1 en cas d'alerte et décrit les deux options."""
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as sortie:
        module_supervision.main(["--help"])
    assert sortie.value.code == 0
    aide = capsys.readouterr().out
    assert "Contrôles de santé (code de sortie 1 si alerte)." in aide
    assert "--sans-flux" in aide and "ne contrôle pas la fraîcheur" in aide
    assert "--notifier" in aide and "envoie les constats graves" in aide


# --------------------------------------------------------------------------------------------
# Précisions : texte exact des requêtes de supervision (relevé par la mutation)
# --------------------------------------------------------------------------------------------
def test_controler_interroge_la_base_avec_les_cinq_requetes_documentees_dans_l_ordre(
    config, entrepot, instructions_sql
):
    """Fraîcheur, alertes bloquées, alertes en échec, rejets et paiements de la dernière heure."""
    controler(entrepot, config, MAINTENANT)
    assert instructions_sql == [
        "SELECT MAX(traite_le) FROM argent_transactions",
        "SELECT COUNT(*) FROM or_alertes_fraude WHERE statut_notification = 'a_envoyer' "
        "AND cree_le < ?",
        "SELECT COUNT(*) FROM or_alertes_fraude WHERE statut_notification = 'echec'",
        "SELECT COUNT(*) FROM meta_rejets WHERE recu_le >= ?",
        "SELECT COUNT(*) FROM argent_transactions WHERE traite_le >= ?",
    ]
