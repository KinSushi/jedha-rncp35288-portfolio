# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Notifications : refus de TLS manquant, isolation des canaux, boîte d'envoi, bornes du SLA."""

from __future__ import annotations

import json
import smtplib
import ssl
import time
from dataclasses import replace
from typing import Any

import pytest
import requests

from detection_fraude import notification as module_notification
from detection_fraude.configuration import Configuration, ErreurConfiguration
from detection_fraude.notification import (
    CanalFichier,
    CanalJournal,
    CanalSMTP,
    CanalWebhook,
    Notificateur,
    Notification,
    construire_canaux,
    envoyer_alertes_en_attente,
    notification_fraude,
)
from tests.helpers import CanalEnregistreur

ALERTE = {
    "trans_num": "a" * 32,
    "horodatage_utc": "2026-10-10T08:00:00+00:00",
    "montant": 321.456,
    "commercant": "Marchand 1",
    "categorie": "shopping_net",
    "carte_fin": "1234",
    "score": 0.987654,
    "seuil": 0.5,
    "version_modele": "v1",
}
PAN = 4_929_123_412_341_234  # numéro fictif de 16 chiffres : il ne doit sortir dans aucun canal


# --------------------------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------------------------
class ServeurSMTP:
    """Faux serveur : note chaque étape dans ``etapes`` ; peut échouer à une étape donnée."""

    def __init__(self, etapes: list, *, starttls_offert: bool = True, pannes: dict | None = None):
        self.etapes = etapes
        self.starttls_offert = starttls_offert
        self.pannes = pannes or {}

    def __enter__(self):
        self.etapes.append(("entree",))
        return self

    def __exit__(self, *_):
        self.etapes.append(("sortie",))
        return False

    def _etape(self, nom: str, *details: Any) -> None:
        self.etapes.append((nom, *details))
        if nom in self.pannes:
            raise self.pannes[nom]

    def has_extn(self, nom):
        self.etapes.append(("has_extn", nom))
        return self.starttls_offert

    def starttls(self, context=None):
        self._etape("starttls", context)

    def login(self, utilisateur, mot_de_passe):
        self._etape("login", utilisateur, mot_de_passe)

    def send_message(self, message):
        self._etape("envoi", message)


class FabriqueSMTP:
    """Remplace ``smtplib.SMTP`` / ``SMTP_SSL`` : note les arguments d'ouverture."""

    def __init__(self, etapes: list, **options: Any) -> None:
        self.etapes = etapes
        self.options = options
        self.appels: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.appels.append((args, kwargs))
        return ServeurSMTP(self.etapes, **self.options)


def canal_smtp(port=587, *, etapes=None, options=None, **parametres):
    """Un ``CanalSMTP`` branché sur deux fabriques espionnes (clair + TLS implicite)."""
    etapes = [] if etapes is None else etapes
    clair = FabriqueSMTP(etapes, **(options or {}))
    implicite = FabriqueSMTP(etapes, **(options or {}))
    canal = CanalSMTP(
        "smtp.test",
        port,
        "de@test",
        ("a@test", "b@test"),
        fabrique_smtp=clair,
        fabrique_smtp_ssl=implicite,
        **parametres,
    )
    return canal, etapes, clair, implicite


def noms(etapes: list) -> list[str]:
    return [etape[0] for etape in etapes]


def message_envoye(etapes: list):
    return next(etape[1] for etape in etapes if etape[0] == "envoi")


# --------------------------------------------------------------------------------------------
# CanalJournal
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("type_notification", "niveau"),
    [("fraude", "WARNING"), ("supervision", "WARNING"), ("rapport", "INFO"), ("autre", "INFO")],
)
def test_le_canal_journal_choisit_son_niveau_et_ne_modifie_pas_la_notification(
    evenements, type_notification, niveau
):
    """Fraude et supervision alertent (WARNING), le reste informe  ; notification intacte."""
    donnees = {
        "trans_num": "t" * 32,
        "score": 0.9,
        "jour": "2026-10-10",
        "montant": 12.5,
        "carte_fin": "1234",
        "commercant": "Marchand",
    }
    donnees_avant = dict(donnees)
    CanalJournal().envoyer(Notification(type_notification, "Sujet exact", "Corps secret", donnees))
    assert evenements.tous() == [
        (
            niveau,
            f"notification_{type_notification}",
            {"sujet": "Sujet exact", "trans_num": "t" * 32, "score": 0.9, "jour": "2026-10-10"},
        )
    ]
    assert donnees == donnees_avant


@pytest.mark.parametrize(
    ("donnees", "champs_attendus"),
    [
        ({}, {}),
        ({"score": 0}, {"score": 0}),  # une valeur nulle est journalisée : seule l'absence exclut
        ({"jour": None}, {"jour": None}),
        ({"montant": 5, "carte_fin": "1234", "corps": "x"}, {}),
        (
            {"trans_num": "t" * 32, "jour": "2026-10-10"},
            {"trans_num": "t" * 32, "jour": "2026-10-10"},
        ),
    ],
)
def test_le_canal_journal_ne_garde_que_les_champs_essentiels_presents(
    evenements, donnees, champs_attendus
):
    """Seuls ``trans_num``, ``score`` et ``jour`` passent, et seulement s'ils existent."""
    donnees_avant = dict(donnees)
    CanalJournal().envoyer(Notification("fraude", "S", "C", donnees))
    assert evenements.tous() == [
        ("WARNING", "notification_fraude", {"sujet": "S", **champs_attendus})
    ]
    assert donnees == donnees_avant  # le canal ne retire rien de la notification elle-même


def test_le_canal_journal_masque_un_numero_de_carte_glisse_dans_le_sujet(evenements):
    """Défense en profondeur : un numéro de carte ne sort pas dans le journal, même en sujet."""
    notification = Notification("fraude", f"carte {PAN} refusée", "C", {})
    sujet_avant = notification.sujet
    CanalJournal().envoyer(notification)
    assert evenements.tous() == [
        ("WARNING", "notification_fraude", {"sujet": "carte [PAN_MASQUE] refusée"})
    ]
    assert str(PAN) not in str(evenements.tous())
    assert notification.sujet == sujet_avant  # le masquage ne vaut que pour le journal


# --------------------------------------------------------------------------------------------
# CanalFichier
# --------------------------------------------------------------------------------------------
def test_le_canal_fichier_ajoute_sans_ecraser_et_masque_les_donnees_sensibles(tmp_path):
    """Une ligne JSON par notification, à la suite ; numéro de carte masqué ; UTF-8 lisible."""
    chemin = tmp_path / "dossier" / "alertes.jsonl"
    chemin.parent.mkdir()
    chemin.write_text('{"ancienne": "ligne"}\n', encoding="utf-8")
    lignes_avant = chemin.read_text(encoding="utf-8").splitlines()
    canal = CanalFichier(chemin)
    canal.envoyer(
        Notification(
            "fraude",
            "Sujet é",
            "Corps ü",
            {"cc_num": PAN, "libelle": f"carte {PAN} refusée", "montant": 5, "zip": 10001},
        )
    )
    canal.envoyer(Notification("rapport", "S2", "C2"))
    brut = chemin.read_bytes()
    texte = chemin.read_text(encoding="utf-8")  # fins de ligne normalisées (CRLF sous Windows)
    assert texte.endswith("}\n") and "\n\n" not in texte
    assert "Sujet é".encode() in brut  # accents écrits tels quels, pas en \uXXXX
    assert (chr(92) + "u00e9").encode() not in brut  # chr(92) = barre oblique inverse
    lignes = texte.splitlines()
    assert lignes[:1] == lignes_avant  # la ligne déjà présente est conservée
    assert json.loads(lignes[1]) == {
        "type": "fraude",
        "sujet": "Sujet é",
        "corps": "Corps ü",
        "donnees": {
            "cc_num": "[MASQUE]",
            "libelle": "carte [PAN_MASQUE] refusée",
            "montant": 5,
            "zip": "[MASQUE]",
        },
    }
    assert json.loads(lignes[2]) == {
        "type": "rapport",
        "sujet": "S2",
        "corps": "C2",
        "donnees": {},
    }
    assert str(PAN) not in texte
    assert [json.loads(x).get("type") for x in lignes] == [None, "fraude", "rapport"]
    profond = tmp_path / "a" / "b" / "c" / "alertes.jsonl"  # aucun dossier n'existe encore
    CanalFichier(profond).envoyer(Notification("rapport", "S3", "C3"))
    assert [json.loads(x)["sujet"] for x in profond.read_text(encoding="utf-8").splitlines()] == [
        "S3"
    ]


def test_le_canal_fichier_signale_un_dossier_impossible_a_creer_sans_toucher_au_fichier(tmp_path):
    """Si un fichier bloque le dossier, l'échec remonte et rien n'est écrit."""
    bloquant = tmp_path / "bloquant"
    bloquant.write_text("contenu initial", encoding="utf-8")
    with pytest.raises(FileExistsError):
        CanalFichier(bloquant / "alertes.jsonl").envoyer(Notification("fraude", "S", "C"))
    assert bloquant.read_text(encoding="utf-8") == "contenu initial"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bloquant"]


# --------------------------------------------------------------------------------------------
# CanalSMTP
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("port", "exiger_tls", "starttls_offert", "delai", "attendu"),
    [
        (587, True, True, None, ["entree", "starttls", "login", "envoi", "sortie"]),
        (587, True, False, 7.5, ["entree", "starttls", "login", "envoi", "sortie"]),
        (587, False, True, 7.5, ["entree", "has_extn", "starttls", "envoi", "sortie"]),
        (587, False, False, None, ["entree", "has_extn", "envoi", "sortie"]),
        (2525, False, True, 3.0, ["entree", "has_extn", "starttls", "envoi", "sortie"]),
        (465, True, True, 7.5, ["entree", "login", "envoi", "sortie"]),
        (465, False, False, None, ["entree", "envoi", "sortie"]),
    ],
)
def test_smtp_choisit_tls_implicite_ou_starttls_selon_le_port_et_l_exigence(
    port, exiger_tls, starttls_offert, delai, attendu
):
    """Port 465 : TLS implicite ; sinon STARTTLS exigé ou proposé ; le certificat est vérifié."""
    extra = {} if delai is None else {"delai_s": delai}
    identifiants = {"utilisateur": "util", "mot_de_passe": "secret"} if exiger_tls else {}
    canal, etapes, clair, implicite = canal_smtp(
        port,
        options={"starttls_offert": starttls_offert},
        exiger_tls=exiger_tls,
        **identifiants,
        **extra,
    )
    donnees = {"montant": 5, "trans_num": "t" * 32}
    donnees_avant = dict(donnees)
    canal.envoyer(Notification("fraude", "Sujet exact", "Corps exact", donnees))
    assert donnees == donnees_avant  # l'envoi ne touche pas aux données de la notification
    assert noms(etapes) == attendu
    delai_attendu = 20.0 if delai is None else delai
    if port == 465:
        assert clair.appels == []
        ((args, kwargs),) = implicite.appels
        assert args == ("smtp.test", 465)
        assert set(kwargs) == {"timeout", "context"}
        assert kwargs["timeout"] == delai_attendu
        contexte = kwargs["context"]
    else:
        assert implicite.appels == []
        assert clair.appels == [(("smtp.test", port), {"timeout": delai_attendu})]
        contexte = next((e[1] for e in etapes if e[0] == "starttls"), None)
    assert ("has_extn", "starttls") in etapes or "has_extn" not in attendu
    assert (("login", "util", "secret") in etapes) == exiger_tls
    if port == 465 or "starttls" in attendu:  # un contexte TLS est alors obligatoirement transmis
        assert isinstance(contexte, ssl.SSLContext)
        assert contexte.verify_mode == ssl.CERT_REQUIRED and contexte.check_hostname is True
    message = message_envoye(etapes)
    assert message["Subject"] == "Sujet exact"
    assert message["From"] == "de@test"
    assert message["To"] == "a@test, b@test"
    # Les noms d'en-tête partent tels qu'écrits : « Subject: » et non « subject: » (RFC 5322).
    assert message.as_string().splitlines()[:3] == [
        "Subject: Sujet exact",
        "From: de@test",
        "To: a@test, b@test",
    ]
    assert message.get_content() == "Corps exact" + chr(10)


def test_smtp_n_ouvre_pas_de_session_authentifiee_si_starttls_echoue():
    """Si le serveur refuse STARTTLS, ni identifiants ni message ne partent en clair."""
    canal, etapes, _, _ = canal_smtp(
        587,
        options={"pannes": {"starttls": smtplib.SMTPNotSupportedError("STARTTLS non offert")}},
        utilisateur="util",
        mot_de_passe="secret",
        exiger_tls=True,
    )
    with pytest.raises(smtplib.SMTPNotSupportedError) as exc:
        canal.envoyer(Notification("fraude", "S", "C"))
    assert str(exc.value) == "STARTTLS non offert"
    assert noms(etapes) == ["entree", "starttls", "sortie"]  # session fermée, rien d'envoyé


def test_smtp_n_envoie_pas_le_message_si_l_authentification_echoue():
    """Un mot de passe refusé arrête l'envoi, et la connexion est tout de même refermée."""
    erreur = smtplib.SMTPAuthenticationError(535, b"identifiants refuses")
    canal, etapes, _, _ = canal_smtp(
        587,
        options={"pannes": {"login": erreur}},
        utilisateur="util",
        mot_de_passe="secret",
        exiger_tls=True,
    )
    with pytest.raises(smtplib.SMTPAuthenticationError) as exc:
        canal.envoyer(Notification("fraude", "S", "C"))
    assert exc.value.args == (535, b"identifiants refuses")
    assert noms(etapes) == ["entree", "starttls", "login", "sortie"]


def test_smtp_referme_la_connexion_et_remonte_l_erreur_quand_l_envoi_echoue():
    """Un échec d'envoi remonte à l'appelant (qui réessaiera) sans laisser de session ouverte."""
    canal, etapes, _, _ = canal_smtp(
        465, options={"pannes": {"envoi": ConnectionResetError("coupure")}}, exiger_tls=True
    )
    with pytest.raises(ConnectionResetError) as exc:
        canal.envoyer(Notification("fraude", "S", "C"))
    assert str(exc.value) == "coupure"
    assert noms(etapes) == ["entree", "envoi", "sortie"]


def test_smtp_refuse_un_utilisateur_sans_tls_et_n_authentifie_pas_sans_mot_de_passe():
    """Un utilisateur seul exige TLS ; avec TLS il est accepté mais ``login`` exige les deux."""
    with pytest.raises(ErreurConfiguration) as exc:
        CanalSMTP("h", 25, "de@test", ("a@test",), utilisateur="util", exiger_tls=False)
    assert str(exc.value) == "Des identifiants SMTP exigent TLS (exiger_tls: true)."
    canal, etapes, _, _ = canal_smtp(587, utilisateur="util", exiger_tls=True)
    canal.envoyer(Notification("fraude", "S", "C"))
    assert noms(etapes) == ["entree", "starttls", "envoi", "sortie"]  # aucun login tenté


def test_smtp_refuse_un_mot_de_passe_sans_tls_et_n_authentifie_pas_sans_utilisateur():
    """Un mot de passe seul exige TLS ; avec TLS il est accepté mais ``login`` exige les deux."""
    with pytest.raises(ErreurConfiguration) as exc:
        CanalSMTP("h", 25, "de@test", ("a@test",), mot_de_passe="secret", exiger_tls=False)
    assert str(exc.value) == "Des identifiants SMTP exigent TLS (exiger_tls: true)."
    canal, etapes, _, _ = canal_smtp(587, mot_de_passe="secret", exiger_tls=True)
    canal.envoyer(Notification("fraude", "S", "C"))
    assert noms(etapes) == ["entree", "starttls", "envoi", "sortie"]


def test_smtp_refuse_les_identifiants_sans_tls_quel_que_soit_le_port_et_exige_tls_par_defaut():
    """Le refus ne dépend pas du port ; sans réglage explicite, TLS est exigé donc accepté."""
    for port in (25, 465, 587):
        with pytest.raises(ErreurConfiguration) as exc:
            CanalSMTP(
                "h",
                port,
                "de@test",
                ("a@test",),
                utilisateur="u",
                mot_de_passe="m",
                exiger_tls=False,
            )
        assert str(exc.value) == "Des identifiants SMTP exigent TLS (exiger_tls: true)."
    CanalSMTP("h", 25, "de@test", ("a@test",), utilisateur="u", mot_de_passe="m")  # défaut : TLS
    CanalSMTP("h", 25, "de@test", ("a@test",), utilisateur="", mot_de_passe="", exiger_tls=False)


# --------------------------------------------------------------------------------------------
# CanalWebhook
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://exemple.test/hook",
        "ftp://exemple.test/hook",
        "exemple.test/hook",
        "",
        "https:/exemple.test/hook",
        " https://exemple.test/hook",
        "xhttps://exemple.test/hook",
    ],
)
@pytest.mark.parametrize("options", [{}, {"exiger_tls": True}])
def test_le_webhook_refuse_toute_url_qui_n_est_pas_https_par_defaut(url, options):
    """HTTPS est exigé par défaut (le jeton de l'URL ne circule pas en clair)."""
    with pytest.raises(ErreurConfiguration) as exc:
        CanalWebhook(url, **options)
    assert str(exc.value) == "Le webhook doit utiliser HTTPS."


def test_le_webhook_envoie_un_json_nettoye_et_laisse_la_notification_inchangee():
    """Charge exacte, délai transmis, et ni numéro de carte ni donnée sensible dans le corps."""
    appels: list[tuple] = []

    class Reponse:
        def raise_for_status(self):
            appels.append("raise_for_status")

    def faux_post(url, json, timeout):
        appels.append((url, json, timeout))
        return Reponse()

    donnees = {"cc_num": PAN, "note": f"carte {PAN}", "montant": 5, "job": "pilote"}
    donnees_avant = dict(donnees)
    notification = Notification("fraude", "Sujet", "Corps", donnees)
    CanalWebhook("http://localhost:9/hook", exiger_tls=False, envoyer_http=faux_post).envoyer(
        notification
    )  # HTTP en clair accepté seulement parce que le TLS n'est pas exigé
    CanalWebhook("https://exemple.test/h", delai_s=3.5, envoyer_http=faux_post).envoyer(
        notification
    )
    charge = {
        "evenement": "fraude",
        "sujet": "Sujet",
        "text": "Sujet\nCorps",
        "donnees": {
            "cc_num": "[MASQUE]",
            "note": "carte [PAN_MASQUE]",
            "montant": 5,
            "job": "[MASQUE]",
        },
    }
    assert appels == [
        ("http://localhost:9/hook", charge, 10.0),  # délai par défaut
        "raise_for_status",
        ("https://exemple.test/h", charge, 3.5),
        "raise_for_status",
    ]
    assert donnees == donnees_avant
    assert str(PAN) not in json.dumps(appels[0][1])


def test_un_refus_du_serveur_webhook_remonte_a_l_appelant():
    """Un statut d'erreur HTTP lève : l'alerte sera réessayée, pas déclarée envoyée."""

    class Reponse:
        def raise_for_status(self):
            raise requests.HTTPError("503 Server Error")

    canal = CanalWebhook("https://exemple.test/h", envoyer_http=lambda *a, **k: Reponse())
    with pytest.raises(requests.HTTPError) as exc:
        canal.envoyer(Notification("fraude", "S", "C"))
    assert str(exc.value) == "503 Server Error"


# --------------------------------------------------------------------------------------------
# Notificateur
# --------------------------------------------------------------------------------------------
class CanalInterrompu:
    nom = "interrompu"

    def envoyer(self, notification):
        raise KeyboardInterrupt


def test_un_canal_en_panne_est_isole_et_journalise_par_son_type_mais_pas_une_interruption(
    evenements,
):
    """Une exception d'un canal n'arrête pas les autres ; une interruption (Ctrl+C), si."""
    a, b, c = CanalEnregistreur("a"), CanalEnregistreur("b", echecs=1), CanalEnregistreur("c")
    notification = Notification("fraude", "s", "c")
    assert Notificateur([a, b, c]).notifier(notification) == {"a": True, "b": False, "c": True}
    assert (a.recus, b.recus, c.recus) == ([notification], [], [notification])
    assert evenements.tous() == [
        ("WARNING", "canal_en_echec", {"canal": "b", "erreur": "ConnectionError"})
    ]  # le type seulement : le message pourrait contenir un jeton
    assert "canal en panne" not in str(evenements.tous())
    apres = CanalEnregistreur("apres")
    with pytest.raises(KeyboardInterrupt):
        Notificateur([a, CanalInterrompu(), apres]).notifier(notification)
    assert apres.recus == []  # le canal suivant n'est pas appelé
    assert len(evenements.tous()) == 1  # et l'interruption n'est pas journalisée comme un échec


def test_les_canaux_deja_satisfaits_ne_sont_pas_rappeles():
    """Un canal déjà réussi est donné pour réussi sans être rappelé (pas de double envoi)."""
    a, b = CanalEnregistreur("a"), CanalEnregistreur("b")
    notificateur = Notificateur([a, b])
    notification = Notification("fraude", "s", "c")
    notificateur.notifier(notification)
    recus_avant = (list(a.recus), list(b.recus))
    assert notificateur.notifier(notification, {"a"}) == {"a": True, "b": True}
    assert a.recus == recus_avant[0]  # a n'a rien reçu de plus
    assert len(b.recus) == len(recus_avant[1]) + 1
    assert notificateur.notifier(notification, {"inconnu"}) == {"a": True, "b": True}
    assert notificateur.notifier(notification, set()) == {"a": True, "b": True}
    assert notificateur.notifier(notification, None) == {"a": True, "b": True}
    assert (len(a.recus), len(b.recus)) == (4, 5)
    assert Notificateur([]).notifier(notification) == {}


# --------------------------------------------------------------------------------------------
# notification_fraude
# --------------------------------------------------------------------------------------------
def test_le_message_d_alerte_est_exact_arrondi_et_sans_donnee_en_trop():
    """Textes, arrondis (2 et 4 décimales) et champs exacts ; les champs en trop sont ignorés."""
    alerte = {**ALERTE, "cc_num": PAN, "first": "Prénom", "id_interne": 7}
    alerte_avant = dict(alerte)
    notification = notification_fraude(alerte)
    assert notification.type == "fraude"
    assert notification.sujet == "[FRAUDE] 321.46 chez Marchand 1 (carte ****1234)"
    assert notification.corps == (
        f"Fraude probable détectée sur la transaction {'a' * 32}.\n"
        "- Horodatage (UTC) : 2026-10-10T08:00:00+00:00\n"
        "- Montant : 321.46\n"
        "- Commerçant : Marchand 1 (catégorie shopping_net)\n"
        "- Carte se terminant par : 1234\n"
        "- Score du modèle : 0.9877 (seuil 0.5000)\n"
        "À vérifier par l'équipe antifraude : l'alerte est un signal, pas une décision de blocage."
    )
    assert notification.donnees == {
        "trans_num": "a" * 32,
        "horodatage": "2026-10-10T08:00:00+00:00",
        "montant": 321.46,
        "commercant": "Marchand 1",
        "categorie": "shopping_net",
        "carte_fin": "1234",
        "score": 0.9877,
        "version_modele": "v1",
    }
    assert alerte == alerte_avant
    assert str(PAN) not in notification.sujet + notification.corps + str(notification.donnees)
    autre = notification_fraude({**ALERTE, "montant": "5", "score": "0.5", "seuil": "0.25"})
    assert autre.sujet == "[FRAUDE] 5.00 chez Marchand 1 (carte ****1234)"
    assert "- Montant : 5.00\n" in autre.corps
    assert "- Score du modèle : 0.5000 (seuil 0.2500)\n" in autre.corps
    assert (autre.donnees["montant"], autre.donnees["score"]) == (5.0, 0.5)


@pytest.mark.parametrize("cle", sorted(ALERTE))
def test_une_alerte_incomplete_est_refusee_plutot_que_de_partir_avec_des_trous(cle):
    """Un champ manquant lève ``KeyError`` : aucun message à trous n'est envoyé à l'astreinte."""
    incomplete = {k: v for k, v in ALERTE.items() if k != cle}
    with pytest.raises(KeyError) as exc:
        notification_fraude(incomplete)
    assert exc.value.args == (cle,)


# --------------------------------------------------------------------------------------------
# construire_canaux
# --------------------------------------------------------------------------------------------
def avec_canaux(config: Configuration, *canaux: str, **notification: Any) -> Configuration:
    return replace(
        config, notification=replace(config.notification, canaux=canaux, **notification)
    )


def smtp_complet(config: Configuration, **surcharges: Any) -> Configuration:
    return avec_canaux(
        config,
        "smtp",
        smtp_hote="smtp.test",
        smtp_port=2525,
        smtp_expediteur="de@test",
        smtp_destinataires=("a@test", "b@test"),
        **surcharges,
    )


class CanalSMTPEspion:
    """Remplace ``CanalSMTP`` dans ``construire_canaux`` pour lire les paramètres reçus."""

    nom = "smtp"
    construits: list[tuple[tuple, dict]] = []

    def __init__(self, *args, **kwargs):
        type(self).construits.append((args, kwargs))


def test_les_canaux_de_developpement_suivent_l_ordre_demande_et_ecrivent_au_bon_endroit(
    config, evenements
):
    """Journal puis fichier par défaut ; l'ordre de la configuration est celui des canaux."""
    notification_avant = config.notification
    assert [type(c) for c in construire_canaux(config)] == [CanalJournal, CanalFichier]
    inverse = avec_canaux(config, "fichier", "journal")
    canaux = construire_canaux(inverse)
    assert [c.nom for c in canaux] == ["fichier", "journal"]
    canaux[0].envoyer(Notification("fraude", "S", "C"))
    assert config.notification.fichier_alertes.exists()  # le fichier de la configuration
    assert [c.nom for c in construire_canaux(avec_canaux(config))] == []
    assert evenements.tous() == []  # rien d'ignoré, rien d'avertissement
    assert config.notification == notification_avant  # la configuration n'est pas modifiée


@pytest.mark.parametrize(
    "surcharge", [{"smtp_hote": ""}, {"smtp_expediteur": ""}, {"smtp_destinataires": ()}]
)
def test_un_canal_smtp_incomplet_est_ignore_avec_un_avertissement_exact(
    config, evenements, surcharge
):
    """Hôte, expéditeur ou destinataires manquant : pas de canal SMTP bancal, un avertissement."""
    complet = smtp_complet(config)
    incomplet = replace(complet, notification=replace(complet.notification, **surcharge))
    incomplet_avant = replace(incomplet)
    assert construire_canaux(incomplet) == []
    assert incomplet == incomplet_avant  # la configuration reste inchangée
    assert evenements.tous() == [
        (
            "WARNING",
            "canal_ignore",
            {"canal": "smtp", "raison": "FRAUDE_SMTP_HOTE, _EXPEDITEUR ou _DESTINATAIRES absent"},
        )
    ]


def test_un_canal_smtp_sans_aucun_reglage_est_ignore_et_les_autres_canaux_sont_gardes(
    config, evenements
):
    """Un canal ignoré n'empêche pas les canaux suivants d'être construits, dans l'ordre."""
    configuration = avec_canaux(config, "smtp", "journal", "webhook", "fichier")
    configuration_avant = replace(configuration)
    canaux = construire_canaux(configuration)
    assert [c.nom for c in canaux] == ["journal", "fichier"]
    assert configuration == configuration_avant  # la configuration reste inchangée
    assert evenements.tous() == [
        (
            "WARNING",
            "canal_ignore",
            {"canal": "smtp", "raison": "FRAUDE_SMTP_HOTE, _EXPEDITEUR ou _DESTINATAIRES absent"},
        ),
        ("WARNING", "canal_ignore", {"canal": "webhook", "raison": "FRAUDE_WEBHOOK_URL absent"}),
    ]


@pytest.mark.parametrize("valeur", [None, "", "   "])
def test_un_webhook_sans_url_est_ignore_avec_un_avertissement_exact(
    config, evenements, monkeypatch, valeur
):
    """Pas d'URL (absente, vide ou blanche) : le canal est ignoré, jamais construit à moitié."""
    if valeur is None:
        monkeypatch.delenv("FRAUDE_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("FRAUDE_WEBHOOK_URL", valeur)
    configuration = avec_canaux(config, "webhook")
    configuration_avant = replace(configuration)
    assert construire_canaux(configuration) == []
    assert evenements.tous() == [
        ("WARNING", "canal_ignore", {"canal": "webhook", "raison": "FRAUDE_WEBHOOK_URL absent"})
    ]
    assert configuration == configuration_avant  # la configuration reste inchangée


def test_un_webhook_en_clair_est_refuse_quand_le_tls_est_exige_et_accepte_sinon(
    config, monkeypatch
):
    """``exiger_tls`` de la configuration décide : refus de l'HTTP en clair, ou acceptation."""
    monkeypatch.setenv("FRAUDE_WEBHOOK_URL", "http://localhost:9/hook")
    with pytest.raises(ErreurConfiguration) as exc:
        construire_canaux(avec_canaux(config, "webhook", exiger_tls=True))
    assert str(exc.value) == "Le webhook doit utiliser HTTPS."
    canaux = construire_canaux(avec_canaux(config, "webhook", exiger_tls=False))
    assert [type(c) for c in canaux] == [CanalWebhook]
    monkeypatch.setenv("FRAUDE_WEBHOOK_URL", "https://exemple.test/hook/jeton")
    assert [c.nom for c in construire_canaux(avec_canaux(config, "webhook", exiger_tls=True))] == [
        "webhook"
    ]


@pytest.mark.parametrize("nom", ["pigeon", "Journal", "SMTP", ""])
def test_un_canal_inconnu_est_refuse_avec_son_nom_dans_le_message(config, nom):
    """Une faute de frappe dans la configuration arrête tout : pas d'alerte perdue en silence."""
    with pytest.raises(ErreurConfiguration) as exc:
        construire_canaux(avec_canaux(config, "journal", nom))
    assert str(exc.value) == f"Canal de notification inconnu : {nom!r}"


def test_des_identifiants_smtp_sans_tls_sont_refuses_en_developpement_mais_pas_en_production(
    config, monkeypatch
):
    """En production TLS est imposé même si la configuration l'oublie ; en dev, le refus tombe."""
    monkeypatch.setenv("FRAUDE_SMTP_UTILISATEUR", "util")
    monkeypatch.setenv("FRAUDE_SMTP_MOT_DE_PASSE", "secret")
    sans_tls = smtp_complet(config, exiger_tls=False)
    with pytest.raises(ErreurConfiguration) as exc:
        construire_canaux(sans_tls)
    assert str(exc.value) == "Des identifiants SMTP exigent TLS (exiger_tls: true)."
    production = replace(sans_tls, environnement="prod")
    assert [type(c) for c in construire_canaux(production)] == [CanalSMTP]
    avec_tls = smtp_complet(config, exiger_tls=True)
    assert [type(c) for c in construire_canaux(avec_tls)] == [CanalSMTP]


@pytest.mark.parametrize(
    ("environnement", "exiger_tls", "attendu"),
    [("dev", False, False), ("dev", True, True), ("prod", False, True), ("prod", True, True)],
)
def test_le_canal_smtp_recoit_ses_reglages_et_les_secrets_de_l_environnement(
    config, monkeypatch, environnement, exiger_tls, attendu
):
    """Hôte, port, adresses, secrets lus dans l'environnement ; TLS = demandé OU production."""
    CanalSMTPEspion.construits = []
    monkeypatch.setattr(module_notification, "CanalSMTP", CanalSMTPEspion)
    monkeypatch.setenv("FRAUDE_SMTP_UTILISATEUR", "util")
    monkeypatch.setenv("FRAUDE_SMTP_MOT_DE_PASSE", "secret")
    configuration = replace(
        smtp_complet(config, exiger_tls=exiger_tls), environnement=environnement
    )
    configuration_avant = replace(configuration)
    canaux = construire_canaux(configuration)
    assert [type(c) for c in canaux] == [CanalSMTPEspion]
    assert configuration == configuration_avant  # les secrets ne sont pas copiés dans la config
    assert CanalSMTPEspion.construits == [
        (
            ("smtp.test", 2525, "de@test", ("a@test", "b@test")),
            {"utilisateur": "util", "mot_de_passe": "secret", "exiger_tls": attendu},
        )
    ]
    monkeypatch.delenv("FRAUDE_SMTP_UTILISATEUR")
    monkeypatch.delenv("FRAUDE_SMTP_MOT_DE_PASSE")
    CanalSMTPEspion.construits = []
    construire_canaux(configuration)
    assert CanalSMTPEspion.construits[0][1] == {
        "utilisateur": None,
        "mot_de_passe": None,
        "exiger_tls": attendu,
    }


# --------------------------------------------------------------------------------------------
# envoyer_alertes_en_attente
# --------------------------------------------------------------------------------------------
def alerte_en_base(rang: int, **surcharges: Any) -> dict[str, Any]:
    ligne: dict[str, Any] = {
        "trans_num": f"{rang:032x}",
        "date_transaction": "2026-10-10",
        "horodatage_utc": "2026-10-10T08:00:00+00:00",
        "carte_fin": "1234",
        "commercant": f"Marchand {rang}",
        "categorie": "shopping_net",
        "montant": 900.0,
        "score": 0.95,
        "seuil": 0.5,
        "version_modele": "v1",
        "source": "jeu",
        "statut_notification": "a_envoyer",
        "tentatives": 0,
        "derniere_tentative": None,
        "notifiee_le": None,
        "latence_notification_s": None,
        "canaux_ok": "",
        "cree_le": "2026-10-10T08:00:00+00:00",
        "detectee_le_s": 1000.0 + rang,
    }
    ligne.update(surcharges)
    return ligne


def installer(entrepot, *alertes: dict[str, Any]) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_alertes(conn, list(alertes))


def toutes(entrepot) -> dict[str, dict[str, Any]]:
    lignes = entrepot.requete("SELECT * FROM or_alertes_fraude ORDER BY trans_num")
    return {ligne["trans_num"]: ligne for ligne in lignes}


def ident(rang: int) -> str:
    return f"{rang:032x}"


class CanalSelectif:
    """Canal qui échoue pour certaines transactions et note celles qu'il a reçues."""

    def __init__(self, nom: str, echoue_pour: tuple[int, ...] = ()) -> None:
        self.nom = nom
        self.echoue = {ident(r) for r in echoue_pour}
        self.recus: list[str] = []

    def envoyer(self, notification: Notification) -> None:
        trans_num = notification.donnees["trans_num"]
        if trans_num in self.echoue:
            raise ConnectionError("canal en panne (test)")
        self.recus.append(trans_num)


def reglages(
    config: Configuration, *, max_tentatives: int = 3, sla: float = 60.0
) -> Configuration:
    return replace(
        config,
        notification=replace(config.notification, max_tentatives=max_tentatives),
        sla=replace(config.sla, latence_notification_s=sla),
    )


def test_les_compteurs_et_les_statuts_suivent_le_sort_de_chaque_alerte(entrepot, config):
    """Envoyée, reportée, en échec : compteurs, statuts, canaux ; les autres alertes intactes."""
    configuration = reglages(config, max_tentatives=3)
    installer(
        entrepot,
        alerte_en_base(1),  # tout réussit
        alerte_en_base(2),  # le canal b échoue : reportée (tentative 1 sur 3)
        alerte_en_base(3, tentatives=2),  # le canal b échoue : 3e tentative -> échec
        alerte_en_base(4, tentatives=3),  # au plafond : jamais reprise
        alerte_en_base(5, statut_notification="envoyee", tentatives=1, canaux_ok="a,b"),
        alerte_en_base(6, statut_notification="echec", tentatives=3, canaux_ok="a"),
    )
    dormantes_avant = {r: toutes(entrepot)[ident(r)] for r in (4, 5, 6)}
    a, b = CanalSelectif("a"), CanalSelectif("b", echoue_pour=(2, 3))
    compte = envoyer_alertes_en_attente(
        entrepot, Notificateur([a, b]), configuration, horloge=lambda: 1001.0 + 5.0
    )
    assert compte == {"envoyees": 1, "reportees": 1, "en_echec": 1}
    assert (a.recus, b.recus) == ([ident(1), ident(2), ident(3)], [ident(1)])  # ordre de détection
    apres = toutes(entrepot)
    envoyee, reportee, echec = apres[ident(1)], apres[ident(2)], apres[ident(3)]
    assert (envoyee["statut_notification"], envoyee["tentatives"]) == ("envoyee", 1)
    assert (envoyee["canaux_ok"], envoyee["latence_notification_s"]) == ("a,b", 5.0)
    assert envoyee["notifiee_le"] is not None and envoyee["derniere_tentative"] is not None
    assert (reportee["statut_notification"], reportee["tentatives"]) == ("a_envoyer", 1)
    assert (reportee["canaux_ok"], reportee["latence_notification_s"]) == ("a", None)
    assert reportee["notifiee_le"] is None and reportee["derniere_tentative"] is not None
    assert (echec["statut_notification"], echec["tentatives"]) == ("echec", 3)
    assert (echec["canaux_ok"], echec["latence_notification_s"]) == ("a", None)
    assert echec["notifiee_le"] is None
    assert {r: apres[ident(r)] for r in (4, 5, 6)} == dormantes_avant


@pytest.mark.parametrize(
    ("plafond", "depart", "statut", "compte"),
    [
        (1, 0, "echec", {"envoyees": 0, "reportees": 0, "en_echec": 1}),
        (2, 0, "a_envoyer", {"envoyees": 0, "reportees": 1, "en_echec": 0}),
        (2, 1, "echec", {"envoyees": 0, "reportees": 0, "en_echec": 1}),
        (5, 3, "a_envoyer", {"envoyees": 0, "reportees": 1, "en_echec": 0}),
        (5, 4, "echec", {"envoyees": 0, "reportees": 0, "en_echec": 1}),
    ],
)
def test_l_alerte_passe_en_echec_exactement_a_la_derniere_tentative_permise(
    entrepot, config, plafond, depart, statut, compte
):
    """Échec dès que ``tentatives + 1`` atteint le plafond ; au plafond, l'alerte reste intacte."""
    installer(
        entrepot, alerte_en_base(1, tentatives=depart), alerte_en_base(2, tentatives=plafond)
    )
    dormante_avant = toutes(entrepot)[ident(2)]
    mort = CanalSelectif("mort", echoue_pour=(1, 2))
    resultat = envoyer_alertes_en_attente(
        entrepot, Notificateur([mort]), reglages(config, max_tentatives=plafond)
    )
    assert resultat == compte
    apres = toutes(entrepot)
    assert (apres[ident(1)]["statut_notification"], apres[ident(1)]["tentatives"]) == (
        statut,
        depart + 1,
    )
    assert apres[ident(2)] == dormante_avant
    assert mort.recus == []


def test_sans_aucun_canal_une_alerte_n_est_jamais_declaree_envoyee(entrepot, config, evenements):
    """Aucun canal attendu : on ne ment pas en disant « envoyée » ; elle finit en échec visible."""
    configuration = reglages(config, max_tentatives=2)
    installer(entrepot, alerte_en_base(1), alerte_en_base(2, tentatives=2))
    dormante_avant = toutes(entrepot)[ident(2)]
    vide = Notificateur([])
    premier = envoyer_alertes_en_attente(entrepot, vide, configuration, horloge=lambda: 10**9)
    assert premier == {"envoyees": 0, "reportees": 1, "en_echec": 0}
    assert toutes(entrepot)[ident(1)]["statut_notification"] == "a_envoyer"
    second = envoyer_alertes_en_attente(entrepot, vide, configuration, horloge=lambda: 10**9)
    assert second == {"envoyees": 0, "reportees": 0, "en_echec": 1}
    assert toutes(entrepot)[ident(1)]["statut_notification"] == "echec"
    troisieme = envoyer_alertes_en_attente(entrepot, vide, configuration)
    assert troisieme == {"envoyees": 0, "reportees": 0, "en_echec": 0}
    assert toutes(entrepot)[ident(2)] == dormante_avant
    assert evenements.nommes("sla_depasse") == []  # le SLA ne concerne que les envoyées


def test_un_canal_deja_reussi_n_est_pas_rappele_et_ses_succes_sont_conserves(entrepot, config):
    """Seul le canal restant est réessayé ; les canaux réussis restent notés, triés."""
    configuration = reglages(config, max_tentatives=5)
    installer(
        entrepot,
        alerte_en_base(1, canaux_ok="b,ancien", tentatives=1),
        alerte_en_base(2, canaux_ok=None),  # NULL en base : traité comme « aucun canal réussi »
        alerte_en_base(3, canaux_ok="b", tentatives=1),
        alerte_en_base(4, tentatives=5),  # au plafond : ignorée
    )
    dormante_avant = toutes(entrepot)[ident(4)]
    a, b = CanalSelectif("a", echoue_pour=(3,)), CanalSelectif("b")
    compte = envoyer_alertes_en_attente(entrepot, Notificateur([a, b]), configuration)
    assert compte == {"envoyees": 2, "reportees": 1, "en_echec": 0}
    assert b.recus == [ident(2)]  # b déjà satisfait pour 1 et 3 : jamais rappelé
    assert a.recus == [ident(1), ident(2)]
    apres = toutes(entrepot)
    assert apres[ident(1)]["canaux_ok"] == "a,ancien,b"  # trié, canal inconnu conservé
    assert apres[ident(1)]["statut_notification"] == "envoyee"
    assert apres[ident(2)]["canaux_ok"] == "a,b"
    assert (apres[ident(3)]["canaux_ok"], apres[ident(3)]["statut_notification"]) == (
        "b",
        "a_envoyer",
    )
    assert apres[ident(3)]["tentatives"] == 2
    assert apres[ident(4)] == dormante_avant


@pytest.mark.parametrize(
    ("origine", "maintenant", "latence", "depasse"),
    [
        (1000.0, 1030.0, 30.0, None),
        (1000.0, 1059.9, 59.9, None),
        (1000.0, 1060.0, 60.0, None),  # pile au seuil : pas de dépassement
        (1000.0, 1060.04, 60.04, 60.0),  # dépasse de 0,04 s : signalé, arrondi à 60,0
        (1000.0, 1060.1, 60.1, 60.1),
        (1000.0, 1161.26, 161.26, 161.3),
        (1000.0, 990.0, 0.0, None),  # horloge en retard : latence ramenée à 0, jamais négative
        (None, 5000.0, None, None),  # détection inconnue : pas de latence
        (0.0, 5000.0, None, None),
    ],
)
def test_la_latence_est_mesuree_avec_l_horloge_et_le_sla_se_declenche_au_dela_du_seuil(
    entrepot, config, evenements, origine, maintenant, latence, depasse
):
    """Latence = horloge - détection (bornée à 0) ; avertissement strictement au-dessus."""
    installer(entrepot, alerte_en_base(1, detectee_le_s=origine), alerte_en_base(9, tentatives=5))
    dormante_avant = toutes(entrepot)[ident(9)]
    canal = CanalEnregistreur("seul")
    envoyer_alertes_en_attente(
        entrepot, Notificateur([canal]), reglages(config, sla=60.0), horloge=lambda: maintenant
    )
    stockee = toutes(entrepot)[ident(1)]["latence_notification_s"]
    if latence is None:
        assert stockee is None
    else:
        assert stockee == pytest.approx(latence, abs=1e-6)
    attendus = []
    if depasse is not None:
        attendus = [
            (
                "WARNING",
                "sla_depasse",
                {
                    "sla": "latence_notification",
                    "latence_s": depasse,
                    "seuil_s": 60.0,
                    "trans_num": ident(1),
                },
            )
        ]
    assert [e for e in evenements.tous() if e[1] == "sla_depasse"] == attendus
    assert toutes(entrepot)[ident(1)]["statut_notification"] == "envoyee"
    assert toutes(entrepot)[ident(9)] == dormante_avant


def test_le_seuil_du_sla_vient_de_la_configuration(entrepot, config, evenements):
    """Avec un seuil de 10 s : 11 s de retard est signalé, 10 s pile ne l'est pas."""
    installer(entrepot, alerte_en_base(1), alerte_en_base(2), alerte_en_base(9, tentatives=3))
    dormante_avant = toutes(entrepot)[ident(9)]
    notificateur = Notificateur([CanalEnregistreur("seul")])
    envoyer_alertes_en_attente(
        entrepot, notificateur, reglages(config, sla=10.0), horloge=lambda: 1012.0
    )  # détectées à 1001 et 1002 : latences de 11 s et de 10 s
    assert evenements.nommes("sla_depasse") == [
        (
            "WARNING",
            {
                "sla": "latence_notification",
                "latence_s": 11.0,
                "seuil_s": 10.0,
                "trans_num": ident(1),
            },
        )
    ]
    assert toutes(entrepot)[ident(9)] == dormante_avant


def test_rejouer_l_envoi_apres_un_succes_ne_renvoie_rien_et_laisse_la_base_inchangee(
    entrepot, config
):
    """Une alerte envoyée ne repart jamais ; l'horloge réelle est celle par défaut."""
    installer(entrepot, alerte_en_base(1, detectee_le_s=time.time()))
    canal = CanalEnregistreur("seul")
    notificateur = Notificateur([canal])
    premier = envoyer_alertes_en_attente(entrepot, notificateur, config)
    assert premier == {"envoyees": 1, "reportees": 0, "en_echec": 0}
    latence = toutes(entrepot)[ident(1)]["latence_notification_s"]
    assert 0.0 <= latence < 120.0  # horloge réelle, avec une marge de deux minutes
    alertes_avant = toutes(entrepot)
    recus_avant = list(canal.recus)
    second = envoyer_alertes_en_attente(entrepot, notificateur, config)
    assert second == {"envoyees": 0, "reportees": 0, "en_echec": 0}
    assert canal.recus == recus_avant
    assert toutes(entrepot) == alertes_avant
    notification = recus_avant[0]
    assert (notification.type, notification.donnees["trans_num"]) == ("fraude", ident(1))
    assert notification.donnees["montant"] == 900.0


# --------------------------------------------------------------------------------------------
# Précisions : ce que les tests ci-dessus laissaient passer (relevé par la mutation)
# --------------------------------------------------------------------------------------------
def test_les_compteurs_additionnent_plusieurs_alertes_reportees_et_plusieurs_en_echec(
    entrepot, config
):
    """Deux alertes reportées et deux en échec dans le même passage : 2 et 2, pas 1 et 1."""
    installer(
        entrepot,
        alerte_en_base(1),
        alerte_en_base(2),
        alerte_en_base(3, tentatives=2),
        alerte_en_base(4, tentatives=2),
    )
    mort = CanalSelectif("mort", echoue_pour=(1, 2, 3, 4))
    compte = envoyer_alertes_en_attente(
        entrepot, Notificateur([mort]), reglages(config, max_tentatives=3)
    )
    assert compte == {"envoyees": 0, "reportees": 2, "en_echec": 2}
    apres = toutes(entrepot)
    assert [apres[ident(rang)]["statut_notification"] for rang in (1, 2, 3, 4)] == [
        "a_envoyer",
        "a_envoyer",
        "echec",
        "echec",
    ]
