# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Couche d'abstraction du data lake, local ou S3.

L'objectif est de fournir une interface unique pour lire, écrire et lister
des objets, quel que soit le backend. En mode local, les écritures sont
atomiques (fichier temporaire puis remplacement) pour éviter un fichier
tronqué en cas d'interruption. En mode S3, les objets sont chiffrés côté
serveur et un cache local est utilisé pour les lectures par Spark ; ce cache
est invalidé dès que l'objet distant change (ETag), pour ne jamais servir une
version périmée.

Le client S3 peut être injecté (paramètre ``client_s3``) : c'est ce qui permet
de tester toute la branche S3 avec un faux client, sans réseau ni compte AWS.

CHOIX
-----
- **Retenu : une seule interface** (``ecrire``, ``lire``, ``existe``, ``lister``, ``supprimer``,
  ``publier_dossier``, ``chemin_local``) pour un dossier local ou un bucket S3 : le reste du
  projet ne sait pas où sont les données. *Écarté* : un code différent pour S3 dans chaque module.
- **Retenu : écriture locale atomique** (fichier ``.tmp`` puis ``os.replace``). *Écarté* : écrire
  directement la cible, qui laisserait un fichier tronqué si le programme est interrompu.
- **Retenu : chiffrement AES-256 côté serveur** pour chaque objet S3, et cache local invalidé par
  l'ETag et la date de modification. *Écarté* : un cache jamais vérifié, qui servirait sans
  message une ancienne version d'un objet réécrit.
- **Retenu : remplacer un dossier Parquet par un échange de dossier** (le nouveau jeu est monté à
  côté puis mis en place). *Écarté* : écrire dans le dossier existant, où une relance qui produit
  moins de fichiers laisserait d'anciennes parties qui seraient relues avec les nouvelles.
- **Retenu : distinguer « absent » (404) d'une vraie erreur (403)** pour ``existe``. *Écarté* :
  répondre « n'existe pas » à toute erreur, ce qui masquerait un défaut de permissions.
- **Retenu : client S3 injectable**, pour tester sans réseau ni compte AWS.

OÙ
---
- **Place** : couche de stockage, utilisée par TOUTES les étapes (collecte, ETL, cartes,
  notebook).
- **Appelants** : ``kayak/pipeline.py``, ``kayak/collecte/*``, ``kayak/etl/spark_etl.py``,
  ``kayak/outils_notebook.py``. **Dépendances** : ``kayak.config`` (bucket, préfixe, dossier
  local), boto3 et botocore (importés à la demande, seulement en mode S3).

COMMENT
-------
- Le mode vient du bucket : ``s3`` s'il est configuré, ``local`` sinon. En local, les chemins
  logiques sont résolus sous la racine du lake et renvoyés avec des « / » ; en S3, la clé est
  préfixée et ``lister`` suit les jetons de continuation (1 000 clés au plus par appel).
- **Preuves** : écriture et lecture (tests/test_data_lake.py:17), liste
  (tests/test_data_lake.py:37), mode S3 chiffré (tests/test_data_lake_s3.py:110), liste paginée
  (tests/test_data_lake_s3.py:126), cache (tests/test_data_lake_s3.py:147).
- **Refus** (tests inverses) : un fichier absent lève ``FileNotFoundError``
  (tests/test_data_lake_inverses.py:203) ; un contenu d'un autre type est refusé et l'ancien
  fichier reste intact (tests/test_data_lake_inverses.py:231) ; une erreur du service S3 remonte
  et laisse le bucket inchangé (tests/test_data_lake_inverses.py:522) ; un refus d'accès 403
  remonte au lieu de passer pour une absence (tests/test_data_lake_s3.py:117) ; un fichier de
  cache sans empreinte n'est pas cru (tests/test_data_lake_s3.py:166) ; republier un dossier ne
  laisse pas de partie orpheline (tests/test_data_lake_s3.py:204).

POURQUOI
--------
- Pagination : ListObjectsV2 renvoie au plus 1 000 clés par appel et un jeton de continuation
  (https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html,
  lecture non consignée, à revérifier) ; code : kayak/stockage/data_lake.py:245.
- Le défaut du cache sans vérification est décrit dans l'en-tête de tests/test_data_lake_s3.py :
  le mode S3 n'était pas testé et le cache renvoyait un fichier déjà téléchargé.
- Limite : le mode S3 n'a jamais été exécuté contre un vrai bucket (faute de compte AWS) ; il est
  testé avec un faux client écrit à la main, qui imite les appels boto3 utilisés.

JOURNALISATION
--------------
- Aucune : ce module ne journalise rien et n'utilise pas ``logging``. Les erreurs sont des
  exceptions (``FileNotFoundError`` en local, ``ClientError`` de botocore en S3), propagées à
  l'appelant.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from kayak import config

# Codes d'erreur S3 qui signifient « l'objet n'existe pas » (et non un problème de droits).
CODES_ABSENT = ("404", "NoSuchKey", "NotFound")


class DataLake:
    """Data lake local ou distant (AWS S3).

    Parameters
    ----------
    bucket : str | None
        Nom du bucket S3. Si None, le data lake fonctionne en mode local.
    prefixe : str
        Préfixe commun appliqué à tous les objets S3.
    racine_locale : Path
        Répertoire racine du data lake en mode local, ou cache temporaire
        en mode S3.
    client_s3 : object | None
        Client S3 déjà construit (injection pour les tests). Si None, il est
        créé à la première utilisation avec ``boto3.client("s3")``.
    """

    def __init__(
        self,
        bucket: str | None = config.S3_BUCKET,
        prefixe: str = config.S3_PREFIXE,
        racine_locale: Path = config.DATALAKE_LOCAL,
        client_s3: Any = None,
    ) -> None:
        self._bucket = bucket
        self._prefixe = prefixe.strip("/")
        # Chemin absolu : les chemins relatifs renvoyés par lister() en dépendent.
        self._racine_locale = Path(racine_locale).resolve()
        self._cache_s3 = self._racine_locale / ".cache_s3"
        self._client = client_s3
        # Le répertoire racine est créé à la demande pour rester inoffensif
        # à l'import du module.
        self._racine_locale.mkdir(parents=True, exist_ok=True)

    @property
    def mode(self) -> str:
        """'s3' si un bucket est configuré, 'local' sinon."""
        return "s3" if self._bucket else "local"

    def _client_s3(self) -> Any:
        """Client S3 : celui qui a été injecté, sinon un client boto3 créé une seule fois.

        boto3 n'est importé qu'au moment de l'usage pour ne pas imposer la dépendance en mode
        local ; le client est conservé, car en créer un par appel est coûteux.
        """
        if self._client is None:
            import boto3  # type: ignore[import-untyped]  # noqa: PLC0415

            self._client = boto3.client("s3")
        return self._client

    def _cle_s3(self, chemin: str) -> str:
        """Clé complète d'un objet dans le bucket : préfixe commun puis chemin logique."""
        return f"{self._prefixe}/{chemin.lstrip('/')}"

    def _uri(self, chemin: str) -> str:
        """Construit l'URI logique d'un objet, sans double slash."""
        chemin = chemin.lstrip("/")
        if self.mode == "s3":
            return f"s3://{self._bucket}/{self._prefixe}/{chemin}"
        return f"file://{(self._racine_locale / chemin).resolve()}"

    def _chemin_local_absolu(self, chemin: str) -> Path:
        """Renvoie le chemin absolu local correspondant à un chemin logique."""
        chemin = chemin.lstrip("/")
        return (self._racine_locale / chemin).resolve()

    def ecrire(self, chemin: str, contenu: bytes | str) -> str:
        """Écrit un objet dans le lake et renvoie son URI.

        En mode local, l'écriture passe par un fichier temporaire puis un
        os.replace atomique. En mode S3, l'objet est envoyé via boto3 avec
        chiffrement AES256 côté serveur.
        """
        uri = self._uri(chemin)
        if isinstance(contenu, str):
            donnees = contenu.encode("utf-8")
        else:
            donnees = contenu

        if self.mode == "local":
            cible = self._chemin_local_absolu(chemin)
            cible.parent.mkdir(parents=True, exist_ok=True)
            temp = cible.with_suffix(cible.suffix + ".tmp")
            temp.write_bytes(donnees)
            os.replace(temp, cible)
            return uri

        self._client_s3().put_object(
            Bucket=self._bucket,
            Key=self._cle_s3(chemin),
            Body=donnees,
            ServerSideEncryption="AES256",
        )
        return uri

    def lire(self, chemin: str) -> bytes:
        """Renvoie le contenu brut d'un objet."""
        if self.mode == "local":
            return self._chemin_local_absolu(chemin).read_bytes()

        reponse = self._client_s3().get_object(Bucket=self._bucket, Key=self._cle_s3(chemin))
        corps: bytes = reponse["Body"].read()
        return corps

    def existe(self, chemin: str) -> bool:
        """Indique si l'objet existe dans le lake."""
        if self.mode == "local":
            return self._chemin_local_absolu(chemin).exists()

        from botocore.exceptions import ClientError  # type: ignore[import-untyped]  # noqa: PLC0415

        try:
            self._client_s3().head_object(Bucket=self._bucket, Key=self._cle_s3(chemin))
        except ClientError as exc:
            # On distingue « absent » d'une vraie erreur pour ne pas masquer un
            # problème de permissions (403) en répondant « n'existe pas ».
            if exc.response.get("Error", {}).get("Code") in CODES_ABSENT:
                return False
            raise
        return True

    def lister(self, prefixe: str) -> list[str]:
        """Liste les chemins logiques dont la clé commence par le préfixe.

        Les « / » de tête du préfixe sont retirées par ``_chemin_local_absolu`` et ``_cle_s3``.
        """
        if self.mode == "local":
            racine = self._chemin_local_absolu(prefixe)
            if not racine.exists():
                return []
            return [
                # Séparateur « / » sur tous les systèmes, comme les clés S3.
                chemin.relative_to(self._racine_locale).as_posix()
                for chemin in racine.rglob("*")
                if chemin.is_file()
            ]

        client = self._client_s3()
        cle_prefixe = self._cle_s3(prefixe)
        resultats: list[str] = []
        continuation_token = None
        # S3 rend au plus 1 000 clés par appel : on suit les jetons de continuation.
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": cle_prefixe,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            reponse = client.list_objects_v2(**kwargs)
            for obj in reponse.get("Contents", []):
                cle = obj["Key"]
                # On retire le préfixe commun pour renvoyer un chemin relatif
                # cohérent avec le mode local.
                resultats.append(cle.removeprefix(self._prefixe + "/"))
            if not reponse.get("IsTruncated"):
                break
            continuation_token = reponse.get("NextContinuationToken")
        return resultats

    def supprimer(self, prefixe: str) -> int:
        """Supprime tous les objets sous un préfixe (dossier) et renvoie leur nombre.

        Sert à remplacer proprement un jeu de fichiers (ex. les parties d'un dossier Parquet) :
        sans cela, une relance qui produit moins de fichiers laisserait d'anciennes parties
        qui seraient relues avec les nouvelles.
        """
        chemins = self.lister(prefixe)
        if self.mode == "local":
            cible = self._chemin_local_absolu(prefixe)
            if cible.is_dir():
                shutil.rmtree(cible)
            elif cible.exists():
                cible.unlink()
            return len(chemins)
        for debut in range(0, len(chemins), 1000):  # delete_objects accepte 1 000 clés au plus
            lot = chemins[debut : debut + 1000]
            self._client_s3().delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": self._cle_s3(c)} for c in lot]},
            )
        return len(chemins)

    def dossier_temporaire(self) -> Path:
        """Dossier de travail local, sur le même disque que le lake (pour des déplacements
        atomiques) ; en mode S3, c'est un sous-dossier du cache local."""
        dossier = self._racine_locale / ".tmp"
        dossier.mkdir(parents=True, exist_ok=True)
        return dossier

    def publier_dossier(self, chemin: str, dossier_local: Path) -> list[str]:
        """Publie dans le lake un dossier produit en local (ex. un jeu de fichiers Parquet).

        Les fichiers cachés et les sommes de contrôle ``.crc`` de Hadoop sont ignorés. Le
        contenu précédent du même chemin est remplacé : en local par un échange de dossier
        (le nouveau jeu est complet avant d'être mis en place), sur S3 par l'envoi des
        nouveaux objets puis la suppression de ceux qui ne figurent plus dans le jeu.
        Renvoie les chemins logiques publiés.
        """
        dossier_local = Path(dossier_local)
        fichiers = sorted(
            f
            for f in dossier_local.rglob("*")
            if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".crc")
        )
        chemin = chemin.strip("/")
        publies = [f"{chemin}/{f.relative_to(dossier_local).as_posix()}" for f in fichiers]

        if self.mode == "local":
            cible = self._chemin_local_absolu(chemin)
            cible.parent.mkdir(parents=True, exist_ok=True)
            # Le nouveau jeu est monté à côté, puis échangé : jamais de dossier à moitié écrit.
            montage = Path(tempfile.mkdtemp(dir=cible.parent, prefix=cible.name + ".montage-"))
            for fichier, logique in zip(fichiers, publies, strict=True):
                destination = montage / Path(logique).relative_to(chemin)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fichier, destination)
            if cible.exists():
                shutil.rmtree(cible)
            os.replace(montage, cible)
            return publies

        for fichier, logique in zip(fichiers, publies, strict=True):
            self.ecrire(logique, fichier.read_bytes())
        # Seuls les objets absents du nouveau jeu sont supprimés : le dossier n'est jamais vide.
        obsoletes = [c for c in self.lister(chemin + "/") if c not in publies]
        for debut in range(0, len(obsoletes), 1000):
            lot = obsoletes[debut : debut + 1000]
            self._client_s3().delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": self._cle_s3(c)} for c in lot]},
            )
        return publies

    def _empreinte_distante(self, chemin: str) -> str:
        """Empreinte de la version actuelle d'un objet S3 : son ETag et sa date de modification.

        L'ETag change dès que le contenu change ; la date de modification double cette garantie
        pour les rares cas où un même contenu est réécrit (on retélécharge, par prudence).
        """
        entete = self._client_s3().head_object(Bucket=self._bucket, Key=self._cle_s3(chemin))
        return f"{str(entete.get('ETag', '')).strip(chr(34))}|{entete.get('LastModified', '')}"

    def chemin_local(self, chemin: str) -> Path:
        """Renvoie un chemin utilisable localement.

        En mode local, c'est le fichier lui-même. En mode S3, l'objet est
        téléchargé dans un cache local, ce qui permet à Spark de lire un fichier local sans
        connecteur S3 supplémentaire. Le cache est valide tant que l'empreinte (ETag et date
        de modification) enregistrée à côté du fichier est celle de l'objet distant : sinon
        l'objet est retéléchargé. Sans cette vérification, un objet réécrit dans le bucket
        resterait lu dans son ancienne version, sans aucun message.
        """
        if self.mode == "local":
            return self._chemin_local_absolu(chemin)

        cible = self._cache_s3 / chemin.lstrip("/")
        cible.parent.mkdir(parents=True, exist_ok=True)
        marque = cible.with_name(cible.name + ".empreinte")
        empreinte = self._empreinte_distante(chemin)
        if cible.exists() and marque.exists() and marque.read_text(encoding="utf-8") == empreinte:
            return cible

        # Téléchargement vers un fichier voisin puis remplacement : une coupure ne laisse pas
        # dans le cache un fichier tronqué qui serait pris pour valide.
        partiel = cible.with_name(cible.name + ".partiel")
        self._client_s3().download_file(self._bucket, self._cle_s3(chemin), str(partiel))
        os.replace(partiel, cible)
        marque.write_text(empreinte, encoding="utf-8")
        return cible
