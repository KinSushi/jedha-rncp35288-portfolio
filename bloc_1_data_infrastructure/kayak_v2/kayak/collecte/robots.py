# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Lecture, archivage et interprétation de robots.txt, règle par règle.

Pourquoi ce module : affirmer « robots.txt autorise la collecte » sans preuve ne vaut rien. Il
faut (1) conserver le fichier lu, daté, dans la zone « raw » du data lake, et (2) l'interpréter
de façon vérifiable. Le module standard ``urllib.robotparser`` ne gère pas les jokers (``*``,
``$``) que Booking.com emploie partout ; l'interprétation est donc faite ici, d'après l'essentiel
de la RFC 9309 (pas tout : la normalisation des caractères codés en pourcentage n'est pas
reprise) :

- le groupe qui s'applique est celui dont le « User-agent » correspond au jeton produit du
  robot (ici « KayakEtudeCertif »), à défaut le groupe « * » ; si plusieurs groupes
  correspondent, leurs règles sont fusionnées (§ 2.2.1) ;
- une règle correspond si son motif est un préfixe du chemin demandé (avec ses paramètres) ;
  ``*`` remplace n'importe quelle suite de caractères et ``$`` ancre la fin du chemin ;
- la règle la plus spécifique (motif le plus long) l'emporte ; à longueur égale, « Allow »
  l'emporte ; si aucune règle ne correspond, l'accès est autorisé ;
- un fichier absent (HTTP 404) n'impose aucune restriction (§ 2.3.1.3) ; un fichier
  inaccessible pour une autre raison (403, 5xx...) arrête la collecte par prudence.

Le robots.txt n'est qu'un volet de la légalité d'une collecte : il n'exprime ni les conditions
d'utilisation du site, ni son contrôle anti-robot technique.

CHOIX
-----
- **Retenu : une interprétation écrite dans le projet**, règle par règle, avec les jokers ``*`` et
  ``$``. *Écarté* : ``urllib.robotparser`` de la bibliothèque standard, qui ignore ces jokers que
  Booking.com emploie dans la plupart de ses règles.
- **Retenu : archiver le fichier tel que reçu, daté, avec son statut, sa date serveur et son
  empreinte SHA-256**, pour pouvoir prouver plus tard ce que le site déclarait ce jour-là.
  *Écarté* : lire le fichier sans le conserver.
- **Retenu : fichier illisible (403, 5xx...) = collecte arrêtée** (``PermissionError``) ; seuls
  404 et 410 valent « pas de restriction ». *Écarté* : traiter tout échec comme une absence, car
  un 403 est souvent un contrôle anti-robot et un 5xx rend le fichier injoignable.

OÙ
---
- **Place** : première étape de chaque collecte web ; le verdict est demandé avant toute requête
  de données. **Appelants** : ``kayak/collecte/destinations.py``
  (kayak/collecte/destinations.py:181) et ``kayak/collecte/booking_spider.py``
  (kayak/collecte/booking_spider.py:372), et le notebook (lecture des archives).
- **Dépendances** : ``requests``, ``kayak.config`` (User-Agent, date),
  ``kayak.stockage.data_lake``.

COMMENT
-------
- ``lire_groupes`` découpe le fichier en groupes ; ``regles_applicables`` choisit le groupe du
  robot (ou ``*``) et fusionne les groupes de même agent ; ``verdict`` compare le chemin à chaque
  règle (motif traduit en expression régulière ancrée) et garde la règle la plus longue ;
  ``archiver_robots`` télécharge, archive et rend texte et métadonnées.
- **Preuves directes** : jokers, ancre et préfixe (tests/test_robots.py:87) ; règle la plus longue
  (tests/test_robots.py:129) ; fichier réel de Booking (tests/test_robots.py:230) ; recoupement
  avec Protego (tests/test_robots.py:251).
- **Refus** (tests inverses) : un fichier illisible arrête la collecte (tests/test_robots.py:191)
  avec son message (tests/test_robots_inverses.py:437) ; un refus n'archive rien
  (tests/test_robots_inverses.py:473) ; un groupe nommé qui interdit tout vaut pour ce robot seul
  (tests/test_robots.py:142).

POURQUOI
--------
- Règles de groupe, de fusion, de plus longue règle et de fichier absent : RFC 9309, « Robots
  Exclusion Protocol » (https://www.rfc-editor.org/rfc/rfc9309, lecture non consignée, à
  revérifier), §§ 2.2.1 et 2.3.1.3 cités dans le texte du module.
- Les jokers absents de ``urllib.robotparser`` : constat du projet, vérifié par le recoupement
  avec Protego, l'analyseur de Scrapy (tests/test_robots.py:251).
- Le robots.txt réel de Booking.com archivé le 11/10/2026 est dans le data lake livré ; il n'est
  pas cité ici par chemin, son nom de dossier contenant le signe « = ».
- Une marque d'ordre des octets (U+FEFF) en tête de fichier est ignorée ; sans cela le premier «
  User-agent » n'était pas reconnu et tout devenait autorisé
  (tests/test_corrections_conformite.py:65).
- Limite connue, non corrigée : si un fichier 200 est archivé puis qu'un 404 arrive le même jour,
  l'ancien robots.txt reste sur disque alors que ``robots_meta.json`` annonce un fichier absent.
- Limite assumée : la normalisation des caractères codés en pourcentage de la RFC n'est pas
  reprise.

JOURNALISATION
--------------
- Aucun message ``logging``. La trace est une ARCHIVE : ``robots.txt`` et ``robots_meta.json``
  dans le lake, qui consigne l'URL, le statut HTTP, la date du serveur, la taille, l'empreinte
  SHA-256 et le User-Agent (kayak/collecte/robots.py:213).
- Un refus de collecte est une exception (``PermissionError``) dont le message cite le statut et
  l'URL.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from kayak import config
from kayak.stockage.data_lake import DataLake


def jeton_produit(user_agent: str) -> str:
    """Jeton produit d'un User-Agent : « KayakEtudeCertif/1.0 (...) » → « kayaketudecertif »."""
    return re.split(r"[/\s(]", user_agent.strip(), maxsplit=1)[0].lower()


def lire_groupes(texte: str) -> list[dict[str, Any]]:
    """Découpe un robots.txt en groupes ``{"agents": [...], "regles": [(directive, motif)]}``.

    Plusieurs lignes « User-agent » consécutives partagent les mêmes règles. Les commentaires
    (« # ... ») et les directives sans lien avec l'accès (Sitemap, Crawl-delay) sont ignorés.
    Une règle « Disallow: » à motif vide ne limite rien : elle est écartée.
    """
    groupes: list[dict[str, Any]] = []
    courant: dict[str, Any] | None = None
    dernier_etait_agent = False
    # Un fichier encodé en UTF-8 avec marque d'ordre des octets (BOM) commence par U+FEFF : sans le
    # retirer, la première ligne « User-agent » ne serait pas reconnue et tout serait autorisé.
    for ligne in texte.removeprefix("﻿").splitlines():
        ligne = ligne.split("#", 1)[0].strip()
        if ":" not in ligne:
            continue
        directive, valeur = (morceau.strip() for morceau in ligne.split(":", 1))
        directive = directive.lower()
        if directive == "user-agent":
            if not dernier_etait_agent or courant is None:
                courant = {"agents": [], "regles": []}
                groupes.append(courant)
            courant["agents"].append(valeur.lower())
            dernier_etait_agent = True
            continue
        dernier_etait_agent = False
        if directive in ("allow", "disallow") and courant is not None and valeur:
            courant["regles"].append((directive.capitalize(), valeur))
    return groupes


def regles_applicables(texte: str, user_agent: str) -> tuple[str, list[tuple[str, str]]]:
    """Règles qui s'appliquent à un robot : renvoie (agent du groupe retenu, règles).

    On cherche d'abord les groupes nommés d'après le robot ; à défaut, les groupes génériques
    « * ». Quand plusieurs groupes correspondent (le même agent cité à deux endroits du
    fichier), leurs règles sont fusionnées, comme le demande la RFC 9309. Sans aucun groupe, il
    n'y a aucune règle : tout est autorisé.
    """
    jeton = jeton_produit(user_agent)
    groupes = lire_groupes(texte)
    nommes = [g for g in groupes if jeton and jeton in g["agents"]]
    if nommes:
        return jeton, [regle for g in nommes for regle in g["regles"]]
    return "*", [regle for g in groupes if "*" in g["agents"] for regle in g["regles"]]


def _motif_vers_regex(motif: str) -> re.Pattern[str]:
    """Motif robots.txt → expression régulière ancrée au début du chemin."""
    fin = motif.endswith("$")
    corps = motif[:-1] if fin else motif
    expression = "".join(".*" if c == "*" else re.escape(c) for c in corps)
    return re.compile("^" + expression + ("$" if fin else ""))


def verdict(texte: str, user_agent: str, url_ou_chemin: str) -> dict[str, Any]:
    """Dit si un robot peut demander une URL, avec la règle qui décide et toutes celles qui
    correspondent (preuve vérifiable).

    Renvoie ``{"autorise": bool, "regle": (directive, motif) | None, "correspondantes": [...],
    "groupe": "*" | jeton}``.
    """
    morceaux = urlsplit(url_ou_chemin)
    chemin = (morceaux.path or "/") + ("?" + morceaux.query if morceaux.query else "")
    groupe, regles = regles_applicables(texte, user_agent)
    correspondantes = [r for r in regles if _motif_vers_regex(r[1]).match(chemin)]
    if not correspondantes:
        return {"autorise": True, "regle": None, "correspondantes": [], "groupe": groupe}
    # Le motif le plus long l'emporte ; à longueur égale, « Allow » l'emporte (le booléen vaut
    # True pour Allow, donc max() le préfère).
    decisive = max(correspondantes, key=lambda r: (len(r[1]), r[0] == "Allow"))
    return {
        "autorise": decisive[0] == "Allow",
        "regle": decisive,
        "correspondantes": correspondantes,
        "groupe": groupe,
    }


def archiver_robots(
    lake: DataLake,
    url_site: str,
    source: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Télécharge le robots.txt d'un site et l'archive daté dans la zone « raw » du lake.

    Écrit ``raw/<source>/date=AAAA-MM-JJ/robots.txt`` (contenu exact, tel que reçu) et
    ``robots_meta.json`` (URL, statut HTTP, date de réponse du serveur, taille, empreinte
    SHA-256) : on peut ainsi prouver plus tard ce que le site déclarait ce jour-là.

    Statut 404 ou 410 : le site n'a pas de robots.txt, ce qui n'impose aucune restriction
    (RFC 9309, § 2.3.1.3). On archive alors le statut seul (``robots_meta.json``, sans fichier
    robots.txt) et on renvoie un texte vide. Tout autre statut que 200 lève une erreur : un 403
    est souvent un contrôle anti-robot, pas une absence, et un 5xx signifie que le fichier est
    injoignable (la RFC demande alors de supposer tout interdit) ; on ne collecte pas sur la foi
    d'un fichier illisible. Renvoie le texte du fichier et ses métadonnées.
    """
    session = session or requests.Session()
    url_robots = urljoin(url_site, "/robots.txt")
    reponse = session.get(url_robots, headers={"User-Agent": config.USER_AGENT}, timeout=30)
    absent = reponse.status_code in (404, 410)
    if reponse.status_code != 200 and not absent:
        raise PermissionError(f"robots.txt illisible ({reponse.status_code}) : {url_robots}")
    date_jour = config.date_collecte()
    dossier = f"raw/{source}/date={date_jour}"
    contenu = b"" if absent else reponse.content
    if not absent:
        lake.ecrire(f"{dossier}/robots.txt", contenu)
    meta = {
        "url": url_robots,
        "statut_http": reponse.status_code,
        "date_reponse_serveur": reponse.headers.get("Date"),
        "date_archive": date_jour,
        "taille_octets": len(contenu),
        "sha256": hashlib.sha256(contenu).hexdigest(),
        "user_agent": config.USER_AGENT,
        "restriction": "aucune (fichier absent)" if absent else "voir robots.txt",
    }
    lake.ecrire(f"{dossier}/robots_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return {"texte": "" if absent else reponse.text, "meta": meta}
