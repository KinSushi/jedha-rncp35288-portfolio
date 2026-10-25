# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests du data lake en mode S3, avec un faux client écrit à la main.

Le mode S3 n'était pas testé jusqu'ici : ni l'écriture chiffrée, ni la liste paginée, ni le cache
local, qui renvoyait un fichier déjà téléchargé sans jamais vérifier que l'objet distant n'avait
pas changé. Le faux client ci-dessous (``FauxClientS3``) imite les appels boto3 utilisés par
``DataLake`` ; aucune dépendance supplémentaire (moto n'est pas installé) et aucun accès réseau.
"""

import hashlib
import io
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from kayak.stockage.data_lake import DataLake


class FauxClientS3:
    """Faux client S3 qui imite les appels de boto3 utilisés par DataLake.

    Il garde les objets en mémoire avec un ETag (somme MD5 du contenu, comme S3 pour un envoi
    simple) et une date de modification qui avance à chaque écriture. Les listes sont paginées
    par ``taille_page`` objets, pour exercer le suivi des jetons de continuation. Il enregistre
    les appels pour que les tests puissent les compter.
    """

    def __init__(self, taille_page: int = 2) -> None:
        self.objets: dict[str, dict] = {}
        self.taille_page = taille_page
        self.horloge = 0
        self.appels: list[tuple] = []

    def put_object(self, Bucket, Key, Body, ServerSideEncryption=None):
        self.appels.append(("put_object", Key, ServerSideEncryption))
        self.horloge += 1
        self.objets[Key] = {
            "corps": Body,
            "etag": '"' + hashlib.md5(Body).hexdigest() + '"',
            "modifie": self.horloge,
            "chiffrement": ServerSideEncryption,
        }

    def _objet(self, Key, operation):
        if Key == "kayak/interdit.txt":  # un objet dont l'accès est refusé (403)
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, operation)
        if Key not in self.objets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, operation)
        return self.objets[Key]

    def get_object(self, Bucket, Key):
        self.appels.append(("get_object", Key))
        return {"Body": io.BytesIO(self._objet(Key, "GetObject")["corps"])}

    def head_object(self, Bucket, Key):
        self.appels.append(("head_object", Key))
        objet = self._objet(Key, "HeadObject")
        return {"ETag": objet["etag"], "LastModified": objet["modifie"]}

    def download_file(self, Bucket, Key, Filename):
        self.appels.append(("download_file", Key))
        Path(Filename).write_bytes(self._objet(Key, "GetObject")["corps"])

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        # Garde-fou : aucun test ne liste plus de quelques pages. Au-delà de 50 appels, la
        # pagination tourne en rond (jeton perdu, condition d'arrêt inversée) : le test échoue
        # tout de suite au lieu de bloquer jusqu'au délai maximal.
        if sum(1 for appel in self.appels if appel[0] == "list_objects_v2") >= 50:
            raise AssertionError("plus de 50 appels à list_objects_v2 : pagination sans fin")
        self.appels.append(("list_objects_v2", Prefix, ContinuationToken))
        cles = sorted(k for k in self.objets if k.startswith(Prefix))
        debut = int(ContinuationToken) if ContinuationToken else 0
        page = cles[debut : debut + self.taille_page]
        reponse = {"Contents": [{"Key": k} for k in page]}
        if debut + self.taille_page < len(cles):
            reponse["IsTruncated"] = True
            reponse["NextContinuationToken"] = str(debut + self.taille_page)
        return reponse

    def delete_objects(self, Bucket, Delete):
        self.appels.append(("delete_objects", len(Delete["Objects"])))
        for objet in Delete["Objects"]:
            self.objets.pop(objet["Key"], None)

    def nombre(self, operation: str) -> int:
        """Nombre d'appels d'une opération donnée."""
        return sum(1 for appel in self.appels if appel[0] == operation)


@pytest.fixture
def faux_client() -> FauxClientS3:
    return FauxClientS3()


@pytest.fixture
def lake_s3(tmp_path: Path, faux_client: FauxClientS3) -> DataLake:
    """Data lake en mode S3 (bucket configuré), avec le faux client injecté."""
    return DataLake(
        bucket="mon-bucket", prefixe="kayak", racine_locale=tmp_path, client_s3=faux_client
    )


def test_s3_mode_et_uri(lake_s3: DataLake):
    """Un bucket configuré active le mode S3 ; l'URI porte bucket, préfixe et chemin."""
    assert lake_s3.mode == "s3"
    assert lake_s3.ecrire("raw/a/f.txt", "x") == "s3://mon-bucket/kayak/raw/a/f.txt"


def test_s3_ecriture_chiffree_et_lecture(lake_s3: DataLake, faux_client: FauxClientS3):
    """L'écriture S3 demande le chiffrement AES256 côté serveur ; la lecture rend les octets."""
    lake_s3.ecrire("raw/a/f.txt", "texte avec accents : été")
    assert faux_client.objets["kayak/raw/a/f.txt"]["chiffrement"] == "AES256"
    assert lake_s3.lire("raw/a/f.txt").decode("utf-8") == "texte avec accents : été"


def test_s3_existe_distingue_absent_et_refuse(lake_s3: DataLake):
    """Absent (404) donne False ; un refus d'accès (403) doit remonter, pas passer pour absent."""
    lake_s3.ecrire("raw/a/f.txt", "1")
    assert lake_s3.existe("raw/a/f.txt")
    assert not lake_s3.existe("raw/a/absent.txt")
    with pytest.raises(ClientError):
        lake_s3.existe("interdit.txt")


def test_s3_lister_suit_les_pages(lake_s3: DataLake, faux_client: FauxClientS3):
    """La liste réunit toutes les pages (jeton de continuation) et rend des chemins logiques."""
    for nom in "abcde":
        lake_s3.ecrire(f"raw/x/{nom}.txt", nom)
    lake_s3.ecrire("curated/y.txt", "y")
    assert faux_client.taille_page < 5  # il y a bien plusieurs pages
    assert sorted(lake_s3.lister("raw/x")) == [f"raw/x/{n}.txt" for n in "abcde"]
    assert faux_client.nombre("list_objects_v2") >= 3


def test_s3_cache_reutilise_tant_que_l_objet_est_inchange(
    lake_s3: DataLake, faux_client: FauxClientS3
):
    """Le cache local évite de retélécharger un objet qui n'a pas changé."""
    lake_s3.ecrire("raw/a/f.csv", "a,b\n1,2\n")
    premier = lake_s3.chemin_local("raw/a/f.csv")
    second = lake_s3.chemin_local("raw/a/f.csv")
    assert premier == second and premier.read_text() == "a,b\n1,2\n"
    assert faux_client.nombre("download_file") == 1


def test_s3_cache_invalide_quand_l_objet_change(lake_s3: DataLake, faux_client: FauxClientS3):
    """Un objet réécrit dans le bucket est retéléchargé : plus jamais d'ancienne version lue."""
    lake_s3.ecrire("raw/a/f.csv", "ancien\n")
    assert lake_s3.chemin_local("raw/a/f.csv").read_text() == "ancien\n"
    lake_s3.ecrire("raw/a/f.csv", "nouveau, plus long\n")
    chemin = lake_s3.chemin_local("raw/a/f.csv")
    assert chemin.read_text() == "nouveau, plus long\n"
    assert faux_client.nombre("download_file") == 2


def test_s3_cache_invalide_si_seule_la_date_change(lake_s3: DataLake, faux_client: FauxClientS3):
    """Même contenu réécrit (ETag identique) mais nouvelle date de modification : retéléchargé."""
    lake_s3.ecrire("raw/a/f.csv", "meme\n")
    lake_s3.chemin_local("raw/a/f.csv")
    lake_s3.ecrire("raw/a/f.csv", "meme\n")
    lake_s3.chemin_local("raw/a/f.csv")
    assert faux_client.nombre("download_file") == 2


def test_s3_cache_ignore_un_fichier_sans_empreinte(lake_s3: DataLake, tmp_path: Path):
    """Un fichier du cache sans empreinte enregistrée n'est pas digne de confiance."""
    lake_s3.ecrire("raw/a/f.csv", "vrai contenu\n")
    orphelin = tmp_path / ".cache_s3" / "raw" / "a" / "f.csv"
    orphelin.parent.mkdir(parents=True)
    orphelin.write_text("contenu périmé\n")
    assert lake_s3.chemin_local("raw/a/f.csv").read_text() == "vrai contenu\n"


def test_s3_publier_dossier_remplace_les_anciennes_parties(lake_s3: DataLake, tmp_path: Path):
    """Publier un dossier envoie ses fichiers et supprime ceux d'une publication précédente."""
    premier = tmp_path / "jeu1"
    premier.mkdir()
    (premier / "part-00000.parquet").write_bytes(b"a")
    (premier / "part-00001.parquet").write_bytes(b"b")
    (premier / ".part-00000.parquet.crc").write_bytes(b"crc")  # somme Hadoop : ignorée
    lake_s3.publier_dossier("curated/t.parquet", premier)
    assert sorted(lake_s3.lister("curated/t.parquet")) == [
        "curated/t.parquet/part-00000.parquet",
        "curated/t.parquet/part-00001.parquet",
    ]
    second = tmp_path / "jeu2"
    second.mkdir()
    (second / "part-00000.parquet").write_bytes(b"c")
    lake_s3.publier_dossier("curated/t.parquet", second)
    assert lake_s3.lister("curated/t.parquet") == ["curated/t.parquet/part-00000.parquet"]
    assert lake_s3.lire("curated/t.parquet/part-00000.parquet") == b"c"


def test_s3_supprimer_un_prefixe(lake_s3: DataLake):
    """supprimer() retire tous les objets d'un préfixe et laisse les autres."""
    lake_s3.ecrire("raw/a/1.txt", "1")
    lake_s3.ecrire("raw/a/2.txt", "2")
    lake_s3.ecrire("raw/b/3.txt", "3")
    assert lake_s3.supprimer("raw/a") == 2
    assert lake_s3.lister("raw") == ["raw/b/3.txt"]


def test_local_publier_dossier_remplace_le_dossier(tmp_path: Path):
    """En local, republier un dossier remplace son contenu (pas de partie orpheline)."""
    lake = DataLake(bucket=None, racine_locale=tmp_path / "lake")
    source = tmp_path / "src"
    source.mkdir()
    (source / "part-00000.parquet").write_bytes(b"1")
    (source / "part-00001.parquet").write_bytes(b"2")
    lake.publier_dossier("curated/t.parquet", source)
    assert sorted(lake.lister("curated/t.parquet")) == [
        "curated/t.parquet/part-00000.parquet",
        "curated/t.parquet/part-00001.parquet",
    ]
    (source / "part-00001.parquet").unlink()
    lake.publier_dossier("curated/t.parquet", source)
    assert lake.lister("curated/t.parquet") == ["curated/t.parquet/part-00000.parquet"]
