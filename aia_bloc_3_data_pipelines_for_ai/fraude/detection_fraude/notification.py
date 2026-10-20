# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Notifications : journal, fichier, e-mail SMTP, webhook — critères 5.2, 5.3 et 6.1.

Le métier veut être prévenu *dès* qu'une fraude est détectée. Trois principes guident le code :

1. **Plusieurs canaux indépendants.** Le journal et le fichier fonctionnent toujours (démonstration
   sans Docker) ; l'e-mail et le webhook s'activent par variables d'environnement. L'échec d'un
   canal n'empêche jamais les autres.
2. **Aucun secret dans le code.** Identifiants SMTP et URL du webhook (qui contient souvent un
   jeton) sont lus dans l'environnement. En production, le courrier passe obligatoirement par TLS
   et le webhook par HTTPS.
3. **Livraison fiable (schéma « boîte d'envoi »).** L'alerte est d'abord écrite dans l'entrepôt
   (``or_alertes_fraude``, statut ``a_envoyer``) *dans la même transaction* que la transaction
   notée, puis envoyée. Si l'envoi échoue ou si le processus tombe, l'alerte reste à envoyer et
   repart au tour suivant, canal par canal (on ne renvoie pas là où l'envoi a réussi).

Une notification ne contient que des données masquées : jamais de nom, d'adresse ni de numéro de
carte complet — seulement les quatre derniers chiffres.

CHOIX
-----
- Retenu : le schéma « boîte d'envoi » plutôt qu'un envoi direct : l'alerte survit à une panne du
  canal ou du processus, et reste visible dans l'entrepôt.
- Retenu : TLS exigé pour SMTP en production, avec vérification du certificat et du nom d'hôte
  (``ssl.create_default_context``) ; STARTTLS sur les ports usuels, TLS implicite sur le 465 ;
  les identifiants SMTP sont refusés sans TLS.
- Retenu : le webhook exige HTTPS dès que ``exiger_tls`` est vrai (toujours en production).
- Retenu : après ``max_tentatives`` (5) échecs l'alerte passe en ``echec`` et la supervision lève
  un constat critique.
- Écarté : un canal unique ; un envoi qui échoue pour tous dès qu'un canal est en panne ; un
  envoi en clair des identifiants.

OÙ
---
- Place : sortie du consommateur et des tâches Airflow vers les humains.
- Appelants : Consommateur.envoyer_alertes (detection_fraude/consommateur.py:411-414),
  notifier_constats (detection_fraude/supervision.py:215-229), notifier_rapport
  (detection_fraude/rapport.py:396-423) et alerte_echec_tache
  (detection_fraude/taches_airflow.py:285-302).
- Dépendances : ``smtplib``, ``ssl``, ``requests``, ``entrepot`` (alertes à envoyer),
  ``configuration`` (secrets) et ``journal``.

COMMENT
-------
1. ``Notificateur.notifier`` envoie la notification à chaque canal en isolant les échecs et rend
   ``{canal: succès}`` ; les canaux déjà satisfaits sont sautés.
2. ``envoyer_alertes_en_attente`` lit les alertes ``a_envoyer``, notifie les canaux restants,
   marque ``envoyee`` quand tous les canaux attendus ont réussi, ``echec`` quand le nombre maximal
   de tentatives est atteint, sinon laisse ``a_envoyer`` ; la latence est mesurée depuis la
   détection et comparée au SLA.
3. ``construire_canaux`` instancie les canaux demandés et ignore, avec un avertissement, un canal
   sans configuration.
Tests qui le prouvent : test_smtp_negocie_tls_avant_d_envoyer_les_identifiants
(tests/test_notification.py:92-107), test_smtp_refuse_les_identifiants_sans_tls
(tests/test_notification.py:118-122), test_webhook_exige_https_quand_demande
(tests/test_notification.py:125-128), test_un_canal_en_panne_ne_bloque_pas_les_autres
(tests/test_notification.py:152-156), test_apres_trop_d_echecs_l_alerte_passe_en_echec
(tests/test_consommateur.py:199-207),
test_smtp_referme_la_connexion_et_remonte_l_erreur_quand_l_envoi_echoue
(tests/test_notification_garanties.py:345-353) et
test_un_webhook_sans_url_est_ignore_avec_un_avertissement_exact
(tests/test_notification_garanties.py:652-667).

POURQUOI
--------
- Chiffrement des transferts, TLS imposé pour SMTP et webhook :
  livrables/aia3/03_securite_conformite.md:113-127.
- Canal en panne : seul le canal en échec est réessayé, puis statut ``echec`` :
  livrables/aia3/01_architecture.md:190-199.
- Chemin de l'alerte, SLA de latence de 60 s : livrables/aia3/04_exploitation.md:41-90.
- Texte « l'alerte est un signal, pas une décision de blocage » (article 22 du RGPD) :
  livrables/aia3/03_securite_conformite.md:75-90 ; texte du règlement : source à joindre.
- Indicateurs 5.2, 5.3 et 6.1 du référentiel AIA RNCP41993 : texte du référentiel à joindre.

JOURNALISATION
--------------
Événements écrits (journal ``fraude.notification``) :
- ``notification_fraude`` et ``notification_supervision`` (WARNING), ``notification_rapport``
  (INFO) : ``sujet`` plus, quand ils existent, ``trans_num``, ``score``, ``jour`` ;
- ``canal_en_echec`` (WARNING) : ``canal``, ``erreur`` (nom du type d'exception, jamais le message)
  ;
- ``canal_ignore`` (WARNING) : ``canal``, ``raison`` ;
- ``sla_depasse`` (WARNING) : ``sla``, ``latence_s``, ``seuil_s``, ``trans_num``.
Les données jointes à une notification passent par ``journal.nettoyer`` avant d'être écrites.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

import requests

from detection_fraude.configuration import Configuration, ErreurConfiguration, lire_secret
from detection_fraude.entrepot import Entrepot
from detection_fraude.journal import avertissement, info, nettoyer, obtenir_journal

_journal = obtenir_journal("notification")


@dataclass(frozen=True)
class Notification:
    type: str  # "fraude", "rapport" ou "supervision"
    sujet: str
    corps: str
    donnees: dict[str, Any] = field(default_factory=dict)


class Canal(Protocol):
    nom: str

    def envoyer(self, notification: Notification) -> None:
        """Envoie ou lève une exception ; l'appelant journalise et réessaiera."""
        ...


class CanalJournal:
    nom = "journal"

    def envoyer(self, notification: Notification) -> None:
        niveau = avertissement if notification.type in ("fraude", "supervision") else info
        # On ne répète pas tout le contenu : le fichier d'alertes et l'entrepôt le conservent.
        essentiel = {
            k: notification.donnees[k]
            for k in ("trans_num", "score", "jour")
            if k in notification.donnees
        }
        niveau(
            _journal, f"notification_{notification.type}", sujet=notification.sujet, **essentiel
        )


class CanalFichier:
    """Ajoute une ligne JSON par notification (consultable avec ``tail -f``)."""

    nom = "fichier"

    def __init__(self, chemin: Path) -> None:
        self._chemin = chemin

    def envoyer(self, notification: Notification) -> None:
        self._chemin.parent.mkdir(parents=True, exist_ok=True)
        ligne = {
            "type": notification.type,
            "sujet": notification.sujet,
            "corps": notification.corps,
            "donnees": nettoyer(notification.donnees),
        }
        with self._chemin.open("a", encoding="utf-8") as flux:
            flux.write(json.dumps(ligne, ensure_ascii=False) + "\n")


class CanalSMTP:
    """Courrier électronique. TLS est négocié ; les identifiants ne partent jamais en clair."""

    nom = "smtp"

    def __init__(
        self,
        hote: str,
        port: int,
        expediteur: str,
        destinataires: tuple[str, ...],
        *,
        utilisateur: str | None = None,
        mot_de_passe: str | None = None,
        exiger_tls: bool = True,
        delai_s: float = 20.0,
        fabrique_smtp: Callable[..., Any] = smtplib.SMTP,
        fabrique_smtp_ssl: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        if (utilisateur or mot_de_passe) and not exiger_tls:
            raise ErreurConfiguration("Des identifiants SMTP exigent TLS (exiger_tls: true).")
        self._parametres = (hote, port, expediteur, destinataires, utilisateur, mot_de_passe)
        self._exiger_tls = exiger_tls
        self._delai = delai_s
        self._smtp = fabrique_smtp
        self._smtp_ssl = fabrique_smtp_ssl

    def envoyer(self, notification: Notification) -> None:
        hote, port, expediteur, destinataires, utilisateur, mot_de_passe = self._parametres
        message = EmailMessage()
        message["Subject"] = notification.sujet
        message["From"] = expediteur
        message["To"] = ", ".join(destinataires)
        message.set_content(notification.corps)
        contexte = ssl.create_default_context()  # vérifie le certificat et le nom d'hôte
        if port == 465:  # TLS implicite
            serveur = self._smtp_ssl(hote, port, timeout=self._delai, context=contexte)
        else:
            serveur = self._smtp(hote, port, timeout=self._delai)
        with serveur:
            if port != 465 and (self._exiger_tls or serveur.has_extn("starttls")):
                serveur.starttls(context=contexte)
            if utilisateur and mot_de_passe:
                serveur.login(utilisateur, mot_de_passe)
            serveur.send_message(message)


class CanalWebhook:
    """Appel HTTP POST JSON (Slack, Teams, outil d'astreinte…). HTTPS exigé si ``exiger_tls``."""

    nom = "webhook"

    def __init__(
        self,
        url: str,
        *,
        exiger_tls: bool = True,
        delai_s: float = 10.0,
        envoyer_http: Callable[..., Any] = requests.post,
    ) -> None:
        if exiger_tls and not url.startswith("https://"):
            raise ErreurConfiguration("Le webhook doit utiliser HTTPS.")
        self._url = url
        self._delai = delai_s
        self._post = envoyer_http

    def envoyer(self, notification: Notification) -> None:
        charge = {
            "evenement": notification.type,
            "sujet": notification.sujet,
            "text": f"{notification.sujet}\n{notification.corps}",
            "donnees": nettoyer(notification.donnees),
        }
        reponse = self._post(self._url, json=charge, timeout=self._delai)
        reponse.raise_for_status()


class Notificateur:
    """Envoie une notification à tous les canaux, chacun isolé des autres."""

    def __init__(self, canaux: list[Canal]) -> None:
        self.canaux = canaux

    def notifier(
        self, notification: Notification, deja_envoye: set[str] | None = None
    ) -> dict[str, bool]:
        """Renvoie ``{canal: succès}`` pour les canaux non encore satisfaits."""
        resultat: dict[str, bool] = {}
        for canal in self.canaux:
            if deja_envoye and canal.nom in deja_envoye:
                resultat[canal.nom] = True
                continue
            try:
                canal.envoyer(notification)
                resultat[canal.nom] = True
            except Exception as erreur:  # noqa: BLE001 - un canal en panne ne doit rien bloquer
                resultat[canal.nom] = False
                avertissement(
                    _journal, "canal_en_echec", canal=canal.nom, erreur=type(erreur).__name__
                )
        return resultat


def construire_canaux(config: Configuration) -> list[Canal]:
    """Instancie les canaux demandés ; un canal sans configuration est ignoré (avertissement)."""
    notif = config.notification
    canaux: list[Canal] = []
    for nom in notif.canaux:
        if nom == "journal":
            canaux.append(CanalJournal())
        elif nom == "fichier":
            canaux.append(CanalFichier(notif.fichier_alertes))
        elif nom == "smtp":
            if not (notif.smtp_hote and notif.smtp_expediteur and notif.smtp_destinataires):
                avertissement(
                    _journal,
                    "canal_ignore",
                    canal="smtp",
                    raison="FRAUDE_SMTP_HOTE, _EXPEDITEUR ou _DESTINATAIRES absent",
                )
                continue
            canaux.append(
                CanalSMTP(
                    notif.smtp_hote,
                    notif.smtp_port,
                    notif.smtp_expediteur,
                    notif.smtp_destinataires,
                    utilisateur=lire_secret("SMTP_UTILISATEUR"),
                    mot_de_passe=lire_secret("SMTP_MOT_DE_PASSE"),
                    exiger_tls=notif.exiger_tls or config.environnement == "prod",
                )
            )
        elif nom == "webhook":
            url = config.webhook_url()
            if not url:
                avertissement(
                    _journal, "canal_ignore", canal="webhook", raison="FRAUDE_WEBHOOK_URL absent"
                )
                continue
            canaux.append(CanalWebhook(url, exiger_tls=notif.exiger_tls))
        else:
            raise ErreurConfiguration(f"Canal de notification inconnu : {nom!r}")
    return canaux


# --------------------------------------------------------------------------------------------
# Alertes de fraude (boîte d'envoi)
# --------------------------------------------------------------------------------------------
def notification_fraude(alerte: dict[str, Any]) -> Notification:
    """Message d'alerte à partir d'une ligne de ``or_alertes_fraude`` (donnée déjà masquée)."""
    donnees = {
        "trans_num": alerte["trans_num"],
        "horodatage": alerte["horodatage_utc"],
        "montant": round(float(alerte["montant"]), 2),
        "commercant": alerte["commercant"],
        "categorie": alerte["categorie"],
        "carte_fin": alerte["carte_fin"],
        "score": round(float(alerte["score"]), 4),
        "version_modele": alerte["version_modele"],
    }
    corps = (
        f"Fraude probable détectée sur la transaction {donnees['trans_num']}.\n"
        f"- Horodatage (UTC) : {donnees['horodatage']}\n"
        f"- Montant : {donnees['montant']:.2f}\n"
        f"- Commerçant : {donnees['commercant']} (catégorie {donnees['categorie']})\n"
        f"- Carte se terminant par : {donnees['carte_fin']}\n"
        f"- Score du modèle : {donnees['score']:.4f} (seuil {float(alerte['seuil']):.4f})\n"
        "À vérifier par l'équipe antifraude : l'alerte est un signal, pas une décision de blocage."
    )
    sujet = (
        f"[FRAUDE] {donnees['montant']:.2f} chez {donnees['commercant']} "
        f"(carte ****{donnees['carte_fin']})"
    )
    return Notification("fraude", sujet, corps, donnees)


def envoyer_alertes_en_attente(
    entrepot: Entrepot,
    notificateur: Notificateur,
    config: Configuration,
    *,
    horloge: Callable[[], float] = time.time,
) -> dict[str, int]:
    """Envoie les alertes ``a_envoyer`` ; marque ``envoyee`` ou, après trop d'échecs, ``echec``."""
    compte = {"envoyees": 0, "reportees": 0, "en_echec": 0}
    attendus = {canal.nom for canal in notificateur.canaux}
    for alerte in entrepot.alertes_a_envoyer(config.notification.max_tentatives):
        deja = {c for c in (alerte.get("canaux_ok") or "").split(",") if c}
        resultat = notificateur.notifier(notification_fraude(alerte), deja)
        reussis = {nom for nom, ok in resultat.items() if ok} | deja
        tentatives = int(alerte["tentatives"]) + 1
        if attendus and attendus <= reussis:
            statut = "envoyee"
            origine = alerte.get("detectee_le_s")
            latence = max(0.0, horloge() - float(origine)) if origine else None
            compte["envoyees"] += 1
            if latence is not None and latence > config.sla.latence_notification_s:
                avertissement(
                    _journal,
                    "sla_depasse",
                    sla="latence_notification",
                    latence_s=round(latence, 1),
                    seuil_s=config.sla.latence_notification_s,
                    trans_num=alerte["trans_num"],
                )
        elif tentatives >= config.notification.max_tentatives:
            statut, latence = "echec", None
            compte["en_echec"] += 1
        else:
            statut, latence = "a_envoyer", None
            compte["reportees"] += 1
        entrepot.marquer_alerte(
            alerte["trans_num"],
            statut=statut,
            tentatives=tentatives,
            canaux_ok=sorted(reussis),
            latence_s=latence,
        )
    return compte
