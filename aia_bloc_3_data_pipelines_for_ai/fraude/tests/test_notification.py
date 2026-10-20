# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Notifications : canaux, TLS, secrets, isolation des pannes — critères 5.2 et 5.3."""

from __future__ import annotations

import json
from email.message import EmailMessage

import pytest

from detection_fraude.configuration import ErreurConfiguration
from detection_fraude.notification import (
    CanalFichier,
    CanalJournal,
    CanalSMTP,
    CanalWebhook,
    Notificateur,
    Notification,
    construire_canaux,
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
    "score": 0.98765,
    "seuil": 0.5,
    "version_modele": "v1",
}


class FauxSMTP:
    journal: list[tuple] = []

    def __init__(self, hote, port, timeout=None, context=None):
        self.etapes = FauxSMTP.journal = []
        self.etapes.append(("connexion", hote, port, context is not None))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def has_extn(self, nom):
        return True

    def starttls(self, context=None):
        self.etapes.append(("starttls", context is not None))

    def login(self, utilisateur, mot_de_passe):
        self.etapes.append(("login", utilisateur))

    def send_message(self, message: EmailMessage):
        self.etapes.append(("envoi", message["To"], message["Subject"]))


def test_texte_de_l_alerte_sans_donnee_personnelle():
    n = notification_fraude(ALERTE)
    assert n.type == "fraude" and "321.46" in n.corps and "****1234" in n.sujet
    assert "pas une décision de blocage" in n.corps
    assert set(n.donnees) == {
        "trans_num",
        "horodatage",
        "montant",
        "commercant",
        "categorie",
        "carte_fin",
        "score",
        "version_modele",
    }


def test_canal_fichier_ajoute_une_ligne_json_par_notification(tmp_path):
    canal = CanalFichier(tmp_path / "dossier" / "alertes.jsonl")
    canal.envoyer(Notification("fraude", "s1", "c1", {"cc_num": 4929123412341234}))
    canal.envoyer(Notification("fraude", "s2", "c2", {}))
    lignes = (tmp_path / "dossier" / "alertes.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(ligne)["sujet"] for ligne in lignes] == ["s1", "s2"]
    assert json.loads(lignes[0])["donnees"]["cc_num"] == "[MASQUE]"


def test_canal_journal_n_echoue_pas():
    CanalJournal().envoyer(notification_fraude(ALERTE))


def test_smtp_negocie_tls_avant_d_envoyer_les_identifiants():
    canal = CanalSMTP(
        "smtp.test",
        587,
        "de@test",
        ("a@test", "b@test"),
        utilisateur="u",
        mot_de_passe="m",
        exiger_tls=True,
        fabrique_smtp=FauxSMTP,
    )
    canal.envoyer(Notification("fraude", "Sujet", "Corps"))
    noms = [etape[0] for etape in FauxSMTP.journal]
    assert noms == ["connexion", "starttls", "login", "envoi"]
    assert FauxSMTP.journal[-1][1] == "a@test, b@test"
    assert FauxSMTP.journal[1] == ("starttls", True)  # contexte vérifiant le certificat


def test_smtp_tls_implicite_sur_le_port_465():
    canal = CanalSMTP(
        "smtp.test", 465, "de@test", ("a@test",), exiger_tls=True, fabrique_smtp_ssl=FauxSMTP
    )
    canal.envoyer(Notification("fraude", "S", "C"))
    assert "starttls" not in [e[0] for e in FauxSMTP.journal]


def test_smtp_refuse_les_identifiants_sans_tls():
    with pytest.raises(ErreurConfiguration, match="TLS"):
        CanalSMTP(
            "h", 25, "de@test", ("a@test",), utilisateur="u", mot_de_passe="m", exiger_tls=False
        )


def test_webhook_exige_https_quand_demande():
    with pytest.raises(ErreurConfiguration, match="HTTPS"):
        CanalWebhook("http://exemple.test/hook", exiger_tls=True)
    CanalWebhook("http://localhost:9/hook", exiger_tls=False)


def test_webhook_envoie_du_json_masque():
    appels = []

    class Reponse:
        def raise_for_status(self):
            appels.append("ok")

    def faux_post(url, json, timeout):
        appels.append((url, json, timeout))
        return Reponse()

    canal = CanalWebhook("https://exemple.test/hook", envoyer_http=faux_post)
    canal.envoyer(
        Notification("fraude", "Sujet", "Corps", {"montant": 5, "cc_num": 4929123412341234})
    )
    url, charge, delai = appels[0]
    assert url == "https://exemple.test/hook" and delai > 0
    assert charge["evenement"] == "fraude" and charge["donnees"]["cc_num"] == "[MASQUE]"
    assert "Sujet" in charge["text"]


def test_un_canal_en_panne_ne_bloque_pas_les_autres():
    a, b, c = CanalEnregistreur("a"), CanalEnregistreur("b", echecs=1), CanalEnregistreur("c")
    resultat = Notificateur([a, b, c]).notifier(Notification("fraude", "s", "c"))
    assert resultat == {"a": True, "b": False, "c": True}
    assert len(a.recus) == 1 and len(c.recus) == 1


def test_les_canaux_deja_satisfaits_sont_sautes():
    a, b = CanalEnregistreur("a"), CanalEnregistreur("b")
    resultat = Notificateur([a, b]).notifier(Notification("fraude", "s", "c"), deja_envoye={"a"})
    assert resultat == {"a": True, "b": True} and a.recus == [] and len(b.recus) == 1


def test_construction_des_canaux_en_dev_journal_et_fichier(config):
    assert [c.nom for c in construire_canaux(config)] == ["journal", "fichier"]


def test_canaux_smtp_et_webhook_actives_par_l_environnement(config, monkeypatch):
    from dataclasses import replace

    notif = replace(
        config.notification,
        canaux=("journal", "smtp", "webhook"),
        smtp_hote="smtp.test",
        smtp_expediteur="de@test",
        smtp_destinataires=("a@test",),
        exiger_tls=True,
    )
    config = replace(config, notification=notif)
    monkeypatch.setenv("FRAUDE_WEBHOOK_URL", "https://exemple.test/hook/jeton-de-test")
    monkeypatch.setenv("FRAUDE_SMTP_UTILISATEUR", "utilisateur")
    monkeypatch.setenv("FRAUDE_SMTP_MOT_DE_PASSE", "mot-de-passe")
    assert [c.nom for c in construire_canaux(config)] == ["journal", "smtp", "webhook"]


def test_canal_sans_configuration_ignore_avec_avertissement(config, monkeypatch):
    from dataclasses import replace

    config = replace(
        config, notification=replace(config.notification, canaux=("journal", "smtp", "webhook"))
    )
    monkeypatch.delenv("FRAUDE_WEBHOOK_URL", raising=False)
    assert [c.nom for c in construire_canaux(config)] == ["journal"]


def test_canal_inconnu(config):
    from dataclasses import replace

    config = replace(config, notification=replace(config.notification, canaux=("pigeon",)))
    with pytest.raises(ErreurConfiguration, match="inconnu"):
        construire_canaux(config)
