# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests INVERSES du data lake (``kayak.stockage.data_lake``), en mode local et en mode S3.

Le data lake est la mémoire du pipeline : si une écriture tronque un fichier, si une suppression
déborde sur le voisin ou si un cache périmé est relu, tout l'aval est faux sans qu'aucune erreur
ne se voie. Ce fichier prouve donc surtout des REFUS et des ABSENCES D'EFFET : un fichier absent
est une erreur, un échec d'écriture laisse l'ancien contenu intact, une erreur de droits (403)
n'est jamais prise pour « absent », supprimer un préfixe n'emporte pas les voisins, un cache
valide n'est pas retéléchargé, un téléchargement interrompu n'abîme pas le cache.

Aucun réseau ni compte AWS : le client S3 est ``FauxClientS3``, un faux écrit à la main qui imite
les contraintes réelles de S3 (bucket vérifié, jeton de continuation jamais ``None``, au plus
1 000 clés par suppression, page vide sans clé « Contents »). Les tests directs, peu nombreux,
fixent les valeurs exactes (URI, clés, paramètres envoyés, empreinte du cache).
"""

import hashlib
import io
import os
import re
import shutil
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, ParamValidationError

from kayak.stockage.data_lake import DataLake

SANS_JETON = object()
MD5_CSV = hashlib.md5(b"a,b\n1,2\n").hexdigest()  # ETag que S3 donnerait à ce petit CSV


class FauxClientS3:
    """Faux client S3 qui imite les appels de boto3 utilisés par DataLake, et ses contraintes.

    Les objets sont gardés en mémoire avec un ETag (MD5 du contenu, entre guillemets comme S3)
    et une date de modification qui avance à chaque écriture. ``appels`` note chaque appel dans
    l'ordre, avec ses paramètres utiles. ``erreurs`` associe une clé à la réponse d'erreur que S3
    renverrait pour elle ; ``panne_telechargement`` fait échouer ``download_file`` après avoir
    écrit un fichier partiel ; ``pannes`` associe une opération à l'exception qu'elle doit lever.
    """

    def __init__(self, bucket: str = "mon-bucket", taille_page: int = 2) -> None:
        self.bucket = bucket
        self.taille_page = taille_page
        self.objets: dict[str, dict[str, Any]] = {}
        self.horloge = 0
        self.appels: list[tuple[Any, ...]] = []
        self.erreurs: dict[str, dict[str, Any]] = {}
        self.pannes: dict[str, Exception] = {}
        self.panne_telechargement: Exception | None = None
        self.entetes_forces: dict[str, Any] | None = None  # réponse imposée à head_object

    def _verifier(self, bucket: str, operation: str) -> None:
        if bucket != self.bucket:
            reponse = {"Error": {"Code": "NoSuchBucket", "Message": f"bucket {bucket}"}}
            raise ClientError(reponse, operation)
        if operation in self.pannes:
            raise self.pannes[operation]

    def _objet(self, cle: str, operation: str) -> dict[str, Any]:
        if cle in self.erreurs:
            raise ClientError(self.erreurs[cle], operation)
        if cle not in self.objets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, operation)
        return self.objets[cle]

    def appels_de(self, operation: str) -> list[tuple[Any, ...]]:
        """Appels d'une opération donnée, dans l'ordre."""
        return [appel for appel in self.appels if appel[0] == operation]

    def put_object(self, Bucket, Key, Body, ServerSideEncryption=None):
        self._verifier(Bucket, "PutObject")
        self.appels.append(("put_object", Key, ServerSideEncryption))
        self.horloge += 1
        self.objets[Key] = {
            "corps": Body,
            "etag": '"' + hashlib.md5(Body).hexdigest() + '"',
            "modifie": self.horloge,
            "chiffrement": ServerSideEncryption,
        }

    def get_object(self, Bucket, Key):
        self._verifier(Bucket, "GetObject")
        self.appels.append(("get_object", Key))
        return {"Body": io.BytesIO(self._objet(Key, "GetObject")["corps"])}

    def head_object(self, Bucket, Key):
        self._verifier(Bucket, "HeadObject")
        self.appels.append(("head_object", Key))
        objet = self._objet(Key, "HeadObject")
        if self.entetes_forces is not None:
            return dict(self.entetes_forces)
        return {"ETag": objet["etag"], "LastModified": objet["modifie"]}

    def download_file(self, Bucket, Key, Filename):
        self._verifier(Bucket, "GetObject")
        self.appels.append(("download_file", Key, Filename))
        corps = self._objet(Key, "GetObject")["corps"]
        if self.panne_telechargement:
            Path(Filename).write_bytes(corps[:1])  # un fichier tronqué reste sur le disque
            raise self.panne_telechargement
        Path(Filename).write_bytes(corps)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=SANS_JETON):
        self._verifier(Bucket, "ListObjectsV2")
        # Garde-fou : aucun test ne liste plus de quelques pages. Au-delà de 50 appels, la
        # pagination tourne en rond (jeton perdu, condition d'arrêt inversée) : le test échoue
        # tout de suite au lieu de bloquer jusqu'au délai maximal.
        if sum(1 for appel in self.appels if appel[0] == "list_objects_v2") >= 50:
            raise AssertionError("plus de 50 appels à list_objects_v2 : pagination sans fin")
        if ContinuationToken is None:  # boto3 refuse un jeton None
            raise ParamValidationError(report="ContinuationToken ne peut pas être None")
        jeton = None if ContinuationToken is SANS_JETON else ContinuationToken
        self.appels.append(("list_objects_v2", Prefix, jeton))
        cles = sorted(k for k in self.objets if k.startswith(Prefix))
        debut = int(jeton) if jeton else 0
        page = cles[debut : debut + self.taille_page]
        reponse: dict[str, Any] = {}
        if page:  # comme S3 : pas de clé « Contents » quand la page est vide
            reponse["Contents"] = [{"Key": k} for k in page]
        if debut + self.taille_page < len(cles):
            reponse["IsTruncated"] = True
            reponse["NextContinuationToken"] = str(debut + self.taille_page)
        return reponse

    def delete_objects(self, Bucket, Delete):
        self._verifier(Bucket, "DeleteObjects")
        cles = [objet["Key"] for objet in Delete["Objects"]]
        if len(cles) > 1000:  # limite réelle de S3
            raise ClientError({"Error": {"Code": "MalformedXML", "Message": "> 1000"}}, "Delete")
        self.appels.append(("delete_objects", cles))
        for cle in cles:
            self.objets.pop(cle, None)


# --------------------------------------------------------------------------------------------
# Fixtures et outils
# --------------------------------------------------------------------------------------------


@pytest.fixture
def racine(tmp_path: Path) -> Path:
    """Racine locale du data lake (distincte des dossiers sources des tests)."""
    return tmp_path / "lac"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Dossier source d'une publication (hors du lake)."""
    return tmp_path / "source"


@pytest.fixture
def encodage_implicite_non_utf8():
    """Remplace l'encodage implicite par ASCII, comme sous Windows en cp1252.

    ``Path.write_text(data, encoding=None)`` et ``Path.read_text(encoding=None)`` demandent
    à ``io.text_encoding`` l'encodage par défaut. Sous Linux, Python 3.7+ active le mode UTF-8
    (PEP 540) : l'encodage implicite y est toujours UTF-8, comme l'encodage explicite du
    code, et une empreinte non ASCII s'écrit dans les deux cas de la même façon. Ce test
    remet l'encodage implicite à une valeur qui ne sait pas écrire un accent, et vérifie
    alors que la marque du cache est bien écrite en UTF-8.

    Sans ce test, un mutant qui retire ``encoding="utf-8"`` de la lecture ou de l'écriture de
    la marque est indiscernable de l'original, et la liste des mutants équivalents le déclare
    équivalent à tort.
    """
    original = io.text_encoding

    def text_encoding(encoding: str | None, *args: object, **kwargs: object) -> str:
        if encoding is None:
            return "ascii"
        return original(encoding, *args, **kwargs)  # type: ignore[arg-type]

    io.text_encoding = text_encoding  # type: ignore[assignment]
    try:
        yield
    finally:
        io.text_encoding = original  # type: ignore[assignment]


@pytest.fixture
def lake(racine: Path) -> DataLake:
    """Data lake local, sans bucket."""
    return DataLake(bucket=None, prefixe="kayak", racine_locale=racine)


@pytest.fixture
def faux_client() -> FauxClientS3:
    return FauxClientS3()


@pytest.fixture
def lake_s3(racine: Path, faux_client: FauxClientS3) -> DataLake:
    """Data lake en mode S3 avec le faux client injecté."""
    return DataLake(
        bucket="mon-bucket", prefixe="kayak", racine_locale=racine, client_s3=faux_client
    )


def _arbre(dossier: Path) -> dict[str, bytes | None]:
    """Photographie d'un dossier : chemin relatif -> contenu (None pour un sous-dossier)."""
    return {
        element.relative_to(dossier).as_posix(): element.read_bytes() if element.is_file() else None
        for element in dossier.rglob("*")
    }


def _ecrire_arbre(dossier: Path, fichiers: dict[str, bytes]) -> None:
    """Crée des fichiers (chemin relatif -> contenu) sous ``dossier``."""
    for relatif, contenu in fichiers.items():
        cible = dossier / relatif
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(contenu)


def _peupler(client: FauxClientS3, cles: list[str]) -> None:
    """Dépose directement des objets dans le faux bucket (clés complètes, préfixe compris)."""
    for cle in cles:
        client.put_object(Bucket=client.bucket, Key=cle, Body=b"x")


# --------------------------------------------------------------------------------------------
# Mode local : refus de lecture, d'écriture et absence d'effet
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("chemin", ["raw/absent.txt", "/raw/absent.txt"])
def test_lire_local_fichier_absent_leve_filenotfound(lake, chemin):
    """Lire un objet qui n'existe pas est une erreur (jamais des octets vides)."""
    with pytest.raises(FileNotFoundError, match="absent.txt"):
        lake.lire(chemin)


def test_lire_local_sur_un_dossier_est_refuse(lake):
    """Un dossier n'est pas un objet : sa lecture est refusée."""
    lake.ecrire("raw/a.txt", "1")
    with pytest.raises(OSError, match="raw"):
        lake.lire("raw")


def test_ecrire_local_echec_du_remplacement_laisse_la_cible_intacte(lake, racine, monkeypatch):
    """L'écriture passe par un fichier voisin ``.tmp`` : si le remplacement échoue (disque plein,
    coupure), l'ancien fichier reste intact et le nouveau contenu n'a touché que le ``.tmp``."""
    lake.ecrire("raw/f.txt", "version 1")

    def remplacement_en_panne(source, destination):
        raise OSError("disque plein")

    monkeypatch.setattr(os, "replace", remplacement_en_panne)
    with pytest.raises(OSError, match="disque plein"):
        lake.ecrire("raw/f.txt", "version 2")
    assert (racine / "raw" / "f.txt").read_bytes() == b"version 1"
    assert (racine / "raw" / "f.txt.tmp").read_bytes() == b"version 2"


def test_ecrire_local_contenu_d_un_autre_type_est_refuse_et_l_ancien_fichier_reste_intact(
    lake, racine
):
    """Seuls ``bytes`` et ``str`` sont acceptés : un entier est refusé, sans fichier tronqué."""
    contenu_invalide: Any = 123
    lake.ecrire("raw/f.txt", b"version 1")
    with pytest.raises(TypeError, match="bytes-like object is required"):
        lake.ecrire("raw/f.txt", contenu_invalide)
    with pytest.raises(TypeError, match="bytes-like object is required"):
        lake.ecrire("raw/nouveau.txt", contenu_invalide)
    assert lake.lire("raw/f.txt") == b"version 1"
    assert sorted(_arbre(racine)) == ["raw", "raw/f.txt"]


def test_ecrire_local_sous_un_fichier_est_refuse_et_le_fichier_reste_intact(lake):
    """Un fichier ne devient pas un dossier : écrire « sous » ``f.txt`` échoue sans l'abîmer."""
    lake.ecrire("raw/f.txt", "1")
    with pytest.raises(OSError, match="f.txt"):
        lake.ecrire("raw/f.txt/enfant.txt", "2")
    assert lake.lire("raw/f.txt") == b"1"


def test_existe_local_repond_sans_effet_sur_le_lac(lake, racine):
    """``existe`` ne fait que lire : fichier et dossier présents, le reste absent, rien créé."""
    lake.ecrire("raw/a/f.txt", "1")
    avant = _arbre(racine)
    assert lake.existe("raw/a/f.txt") is True
    assert lake.existe("/raw/a/f.txt") is True
    assert lake.existe("raw/a") is True
    assert lake.existe("raw/a/absent.txt") is False
    assert lake.existe("jamais/vu/ici") is False
    assert lake.existe("raw/a/f.txt/enfant") is False
    assert _arbre(racine) == avant


@pytest.mark.parametrize("prefixe", ["absent", "/absent", "raw/f.txt/enfant"])
def test_lister_local_prefixe_absent_renvoie_vide_sans_effet_sur_le_disque(lake, racine, prefixe):
    """Lister un préfixe qui n'existe pas rend ``[]`` et ne crée aucun dossier."""
    lake.ecrire("raw/f.txt", "1")
    avant = _arbre(racine)
    assert lake.lister(prefixe) == []
    assert _arbre(racine) == avant


@pytest.mark.parametrize("prefixe", ["absent", "/absent"])
def test_supprimer_local_prefixe_absent_renvoie_zero_et_laisse_le_lac_inchange(
    lake, racine, prefixe
):
    """Supprimer un préfixe inexistant n'est pas une erreur : 0 supprimé, rien d'autre ne bouge."""
    lake.ecrire("raw/f.txt", "1")
    avant = _arbre(racine)
    assert lake.supprimer(prefixe) == 0
    assert _arbre(racine) == avant


def test_supprimer_local_dossier_retire_tout_son_contenu_et_laisse_les_voisins_intacts(
    lake, racine
):
    """Un dossier disparaît avec ses sous-dossiers ; ``raw/ab`` et ``raw/b`` restent."""
    for chemin in [
        "raw/a/1.txt",
        "raw/a/sous/2.txt",
        "raw/a/sous/profond/3.txt",
        "raw/ab/4.txt",
        "raw/b/5.txt",
        "autre.txt",
    ]:
        lake.ecrire(chemin, chemin)
    voisins = ["raw/ab/4.txt", "raw/b/5.txt", "autre.txt"]
    supprimes = sorted(lake.lister("raw/a"))
    assert supprimes == ["raw/a/1.txt", "raw/a/sous/2.txt", "raw/a/sous/profond/3.txt"]
    assert lake.supprimer("raw/a") == len(supprimes)
    for chemin in supprimes:
        with pytest.raises(FileNotFoundError, match=chemin.rsplit("/", 1)[-1]):
            lake.lire(chemin)
    assert not (racine / "raw" / "a").exists()
    assert {chemin: lake.lire(chemin) for chemin in voisins} == {c: c.encode() for c in voisins}


def test_supprimer_local_fichier_seul_le_retire_et_laisse_ses_voisins_intacts(lake):
    """Supprimer un chemin qui est un fichier retire ce fichier seulement."""
    lake.ecrire("raw/seul.txt", "1")
    lake.ecrire("raw/voisin.txt", "2")
    lake.supprimer("raw/seul.txt")
    with pytest.raises(FileNotFoundError, match="seul.txt"):
        lake.lire("raw/seul.txt")
    assert lake.lire("raw/voisin.txt") == b"2"


@pytest.mark.parametrize("chemin", ["raw/absent.csv", "/raw/absent.csv"])
def test_chemin_local_en_mode_local_est_sans_effet_sur_le_disque(lake, racine, chemin):
    """En mode local, ``chemin_local`` calcule le chemin sans créer ni fichier ni dossier."""
    avant = _arbre(racine)
    resultat = lake.chemin_local(chemin)
    assert resultat == racine / "raw" / "absent.csv"
    assert resultat.is_absolute()
    assert _arbre(racine) == avant


def test_dossier_temporaire_rappele_laisse_son_contenu_intact(lake, racine):
    """Créé à la demande sous ``.tmp``, le dossier garde ce qu'on y a mis aux appels suivants ;
    il est recréé, avec la racine, si tout le lake a été supprimé entre-temps."""
    assert not (racine / ".tmp").exists()
    dossier = lake.dossier_temporaire()
    assert dossier == racine / ".tmp"
    assert dossier.is_dir()
    (dossier / "travail.txt").write_bytes(b"en cours")
    assert lake.dossier_temporaire() == dossier
    assert (dossier / "travail.txt").read_bytes() == b"en cours"
    lake.supprimer("")
    assert not racine.exists()
    assert lake.dossier_temporaire() == dossier
    assert dossier.is_dir()


def test_racine_relative_est_figee_en_absolu_et_la_liste_reste_inchangee_si_le_cwd_change(
    tmp_path, monkeypatch
):
    """Le lake garde sa racine absolue : changer de répertoire courant ne change pas ``lister``."""
    monkeypatch.chdir(tmp_path)
    lac = DataLake(bucket=None, racine_locale=Path("lac_relatif"))
    lac.ecrire("a/f.txt", "1")
    avant = lac.lister("")
    ailleurs = tmp_path / "ailleurs"
    ailleurs.mkdir()
    monkeypatch.chdir(ailleurs)
    assert lac.lister("") == avant == ["a/f.txt"]
    assert lac.lire("a/f.txt") == b"1"
    assert lac.chemin_local("a/f.txt") == tmp_path / "lac_relatif" / "a" / "f.txt"


# --------------------------------------------------------------------------------------------
# Mode local : publier_dossier
# --------------------------------------------------------------------------------------------


def test_publier_dossier_local_fichiers_caches_et_crc_ne_sont_jamais_publies(lake, source):
    """Les fichiers cachés et les ``.crc`` de Hadoop sont ignorés : les lire dans le lake échoue.

    Frontières : ``crc`` (sans point) et ``note.crc.txt`` ne finissent pas par « .crc » : publiés.
    """
    _ecrire_arbre(
        source,
        {
            "part-00000.parquet": b"a",
            ".cache": b"x",
            ".part-00000.parquet.crc": b"c",
            "part-00000.parquet.crc": b"c",
            "sous/.cache2": b"x",
            "sous/b.crc": b"x",
            "crc": b"garde",
            "note.crc.txt": b"garde",
        },
    )
    publies = lake.publier_dossier("curated/t", source)
    assert publies == ["curated/t/crc", "curated/t/note.crc.txt", "curated/t/part-00000.parquet"]
    for ignore in [
        ".cache",
        ".part-00000.parquet.crc",
        "part-00000.parquet.crc",
        "sous/.cache2",
        "sous/b.crc",
    ]:
        with pytest.raises(FileNotFoundError, match=re.escape(Path(ignore).name)):
            lake.lire(f"curated/t/{ignore}")
    assert lake.lire("curated/t/crc") == b"garde"


def test_publier_dossier_local_remplace_l_ancien_jeu_et_laisse_les_voisins_intacts(
    lake, source, tmp_path
):
    """Republier remplace tout le jeu (plus de partie orpheline), voisins non touchés."""
    ancien = tmp_path / "ancien"
    _ecrire_arbre(ancien, {"part-00000.parquet": b"v1-a", "part-00001.parquet": b"v1-b"})
    lake.publier_dossier("curated/t.parquet", ancien)
    lake.ecrire("curated/autre.parquet/part-00000.parquet", b"voisin")
    lake.ecrire("curated/t.parquet.old/part-00000.parquet", b"vieux")
    _ecrire_arbre(source, {"part-00000.parquet": b"v2-a"})
    lake.publier_dossier("curated/t.parquet", source)
    assert lake.lire("curated/t.parquet/part-00000.parquet") == b"v2-a"
    with pytest.raises(FileNotFoundError, match="part-00001.parquet"):
        lake.lire("curated/t.parquet/part-00001.parquet")
    assert lake.lister("curated/t.parquet") == ["curated/t.parquet/part-00000.parquet"]
    assert lake.lire("curated/autre.parquet/part-00000.parquet") == b"voisin"
    assert lake.lire("curated/t.parquet.old/part-00000.parquet") == b"vieux"


def test_publier_dossier_local_laisse_la_source_intacte(lake, source):
    """La publication copie : le dossier source garde tous ses fichiers, cachés compris."""
    _ecrire_arbre(
        source,
        {"part-00000.parquet": b"a", ".part-00000.parquet.crc": b"c", "sous/part-1": b"b"},
    )
    avant = _arbre(source)
    lake.publier_dossier("curated/t", source)
    assert _arbre(source) == avant


def test_publier_dossier_local_echec_de_copie_laisse_l_ancien_jeu_intact(
    lake, source, tmp_path, monkeypatch
):
    """Si la copie du nouveau jeu échoue en route, l'ancien jeu publié reste complet et lisible."""
    ancien = tmp_path / "ancien"
    _ecrire_arbre(ancien, {"part-00000.parquet": b"v1-a", "part-00001.parquet": b"v1-b"})
    lake.publier_dossier("curated/t.parquet", ancien)
    _ecrire_arbre(source, {"part-00000.parquet": b"v2-a", "part-00001.parquet": b"v2-b"})
    vraie_copie = shutil.copy2

    def copie_en_panne(origine, destination, *args, **kwargs):
        if Path(origine).name == "part-00001.parquet":  # la seconde partie du nouveau jeu
            raise OSError("disque plein")
        return vraie_copie(origine, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", copie_en_panne)
    with pytest.raises(OSError, match="disque plein"):
        lake.publier_dossier("curated/t.parquet", source)
    assert sorted(lake.lister("curated/t.parquet")) == [
        "curated/t.parquet/part-00000.parquet",
        "curated/t.parquet/part-00001.parquet",
    ]
    assert lake.lire("curated/t.parquet/part-00000.parquet") == b"v1-a"
    assert lake.lire("curated/t.parquet/part-00001.parquet") == b"v1-b"


# --------------------------------------------------------------------------------------------
# Mode S3 : refus, erreurs propagées, absence d'effet
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["404", "403", "500"])
def test_s3_lire_leve_clienterror_et_ne_cree_rien_en_local(lake_s3, faux_client, racine, code):
    """Un objet absent (404) ou une erreur S3 (403, 500) remonte telle quelle ; rien n'est créé."""
    faux_client.erreurs["kayak/raw/f.txt"] = {"Error": {"Code": code, "Message": "refus"}}
    with pytest.raises(ClientError, match=rf"\({code}\)"):
        lake_s3.lire("raw/f.txt")
    assert faux_client.appels == [("get_object", "kayak/raw/f.txt")]
    assert _arbre(racine) == {}


def test_s3_ecrire_lire_lister_supprimer_sans_effet_sur_le_disque_local(
    lake_s3, faux_client, racine
):
    """En mode S3, ces opérations passent par le bucket seul : aucun fichier local n'apparaît."""
    lake_s3.ecrire("raw/a/f.txt", "1")
    assert lake_s3.lire("raw/a/f.txt") == b"1"
    assert lake_s3.existe("raw/a/f.txt") is True
    assert lake_s3.lister("raw") == ["raw/a/f.txt"]
    lake_s3.supprimer("raw/a/")
    assert faux_client.objets == {}
    assert _arbre(racine) == {}


def test_s3_existe_ne_traite_comme_absent_que_404_nosuchkey_et_notfound(lake_s3, faux_client):
    """Ces trois codes donnent « faux » (un ``head`` seul, jamais de téléchargement) ; tout autre
    code, même voisin, remonte : un refus de droits n'est jamais pris pour une absence."""
    cle = "kayak/raw/f.txt"
    lake_s3.ecrire("raw/present.txt", "1")
    faux_client.appels.clear()
    for code in ["404", "NoSuchKey", "NotFound"]:
        faux_client.erreurs[cle] = {"Error": {"Code": code, "Message": "absent"}}
        assert lake_s3.existe("raw/f.txt") is False
    assert lake_s3.existe("raw/present.txt") is True
    assert faux_client.appels == [
        ("head_object", cle),
        ("head_object", cle),
        ("head_object", cle),
        ("head_object", "kayak/raw/present.txt"),
    ]
    for code in ["403", "AccessDenied", "500", "SlowDown", "NoSuchBucket", "404 ", "notfound"]:
        faux_client.erreurs[cle] = {"Error": {"Code": code, "Message": "refus"}}
        with pytest.raises(ClientError, match=re.escape(f"({code})")):
            lake_s3.existe("raw/f.txt")


@pytest.mark.parametrize("reponse", [{}, {"Error": {}}], ids=["sans_error", "sans_code"])
def test_s3_existe_propage_une_erreur_sans_code(lake_s3, faux_client, reponse):
    """Une erreur dont la réponse ne porte aucun code n'est pas prise pour « absent »."""
    faux_client.erreurs["kayak/raw/f.txt"] = reponse
    with pytest.raises(ClientError, match="Unknown"):
        lake_s3.existe("raw/f.txt")


@pytest.mark.parametrize(
    "operation, action",
    [
        ("PutObject", lambda lac: lac.ecrire("raw/a/g.txt", "2")),
        ("ListObjectsV2", lambda lac: lac.lister("raw")),
        ("DeleteObjects", lambda lac: lac.supprimer("raw/a/")),
    ],
    ids=["ecriture", "liste", "suppression"],
)
def test_s3_une_erreur_du_service_remonte_et_laisse_le_bucket_inchange(
    lake_s3, faux_client, operation, action
):
    """Écriture, liste ou suppression en panne : l'erreur remonte, aucun objet ajouté ni retiré."""
    lake_s3.ecrire("raw/a/f.txt", "1")
    avant = {cle: objet["corps"] for cle, objet in faux_client.objets.items()}
    faux_client.pannes[operation] = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "panne"}}, operation
    )
    with pytest.raises(ClientError, match="ServiceUnavailable"):
        action(lake_s3)
    assert {cle: objet["corps"] for cle, objet in faux_client.objets.items()} == avant


def test_s3_deux_prefixes_voisins_ne_se_voient_pas(racine, faux_client):
    """Les préfixes « kayak » et « kayak2 » (l'un commence comme l'autre) sont étanches."""
    kayak = DataLake(
        bucket="mon-bucket", prefixe="kayak", racine_locale=racine / "k", client_s3=faux_client
    )
    kayak2 = DataLake(
        bucket="mon-bucket", prefixe="kayak2", racine_locale=racine / "k2", client_s3=faux_client
    )
    kayak2.ecrire("raw/f.txt", "du voisin")
    assert kayak.lister("raw") == []
    assert kayak.lister("") == []
    assert kayak.existe("raw/f.txt") is False
    with pytest.raises(ClientError, match="404"):
        kayak.lire("raw/f.txt")
    assert kayak2.lire("raw/f.txt") == b"du voisin"


@pytest.mark.parametrize(
    "prefixe, cle", [("absent", "kayak/absent"), ("/absent/", "kayak/absent/")]
)
def test_s3_lister_prefixe_absent_renvoie_vide_sans_modifier_le_bucket(
    lake_s3, faux_client, prefixe, cle
):
    """Une page sans clé « Contents » (préfixe vide) donne ``[]`` en un appel, rien ne change."""
    lake_s3.ecrire("raw/f.txt", "1")
    faux_client.appels.clear()
    avant = dict(faux_client.objets)
    assert lake_s3.lister(prefixe) == []
    assert faux_client.appels == [("list_objects_v2", cle, None)]
    assert faux_client.objets == avant


def test_s3_supprimer_prefixe_absent_renvoie_zero_et_laisse_le_bucket_inchange(
    lake_s3, faux_client
):
    """Rien à supprimer : 0, un seul appel de liste, aucun ``delete_objects``, bucket inchangé."""
    lake_s3.ecrire("raw/f.txt", "1")
    faux_client.appels.clear()
    assert lake_s3.supprimer("absent/") == 0
    assert faux_client.appels == [("list_objects_v2", "kayak/absent/", None)]
    assert sorted(faux_client.objets) == ["kayak/raw/f.txt"]


def test_s3_supprimer_un_dossier_laisse_les_autres_objets_intacts(lake_s3, faux_client):
    """Supprimer ``raw/a/`` envoie les clés complètes de ce dossier seul et en rend le nombre."""
    for chemin in ["raw/a/1.txt", "raw/a/2.txt", "raw/b/3.txt", "autre.txt"]:
        lake_s3.ecrire(chemin, chemin)
    supprimes = lake_s3.lister("raw/a/")
    faux_client.appels.clear()
    assert lake_s3.supprimer("raw/a/") == len(supprimes)
    assert faux_client.appels_de("delete_objects") == [
        ("delete_objects", ["kayak/raw/a/1.txt", "kayak/raw/a/2.txt"])
    ]
    assert sorted(faux_client.objets) == ["kayak/autre.txt", "kayak/raw/b/3.txt"]


def test_s3_publier_dossier_ignore_caches_et_crc_et_chiffre_chaque_objet(
    lake_s3, faux_client, source
):
    """Cachés et ``.crc`` jamais envoyés (lecture : 404) ; tout objet envoyé est chiffré."""
    _ecrire_arbre(
        source,
        {"part-00001.parquet": b"b", "part-00000.parquet": b"a", ".cache": b"x", "p.crc": b"c"},
    )
    publies = lake_s3.publier_dossier("curated/t", source)
    assert publies == ["curated/t/part-00000.parquet", "curated/t/part-00001.parquet"]
    assert faux_client.appels_de("put_object") == [
        ("put_object", "kayak/curated/t/part-00000.parquet", "AES256"),
        ("put_object", "kayak/curated/t/part-00001.parquet", "AES256"),
    ]
    for ignore in [".cache", "p.crc"]:
        with pytest.raises(ClientError, match="404"):
            lake_s3.lire(f"curated/t/{ignore}")


def test_s3_publier_dossier_sans_changement_ne_supprime_rien(lake_s3, faux_client, source):
    """Republier le même jeu n'émet aucun ``delete_objects`` : le dossier publié reste inchangé."""
    _ecrire_arbre(source, {"part-00000.parquet": b"a", "part-00001.parquet": b"b"})
    publies = lake_s3.publier_dossier("curated/t.parquet", source)
    faux_client.appels.clear()
    assert lake_s3.publier_dossier("curated/t.parquet", source) == publies
    assert faux_client.appels_de("delete_objects") == []
    assert lake_s3.lister("curated/t.parquet/") == publies


def test_s3_publier_dossier_supprime_les_obsoletes_par_lots_et_laisse_les_voisins_intacts(
    racine, source
):
    """1 001 anciennes parties sont supprimées en deux lots (1 000 puis 1) APRÈS l'envoi du
    nouveau jeu ; les dossiers voisins au nom proche restent intacts."""
    client = FauxClientS3(taille_page=1000)
    lac = DataLake(bucket="mon-bucket", prefixe="kayak", racine_locale=racine, client_s3=client)
    anciennes = [f"kayak/curated/t.parquet/part-{i:05d}.parquet" for i in range(1002)]
    voisins = [
        "kayak/curated/t.parquet.old/part-00000.parquet",
        "kayak/curated/autre.parquet/part-00000.parquet",
    ]
    _peupler(client, [*anciennes, *voisins])
    client.appels.clear()
    _ecrire_arbre(source, {"part-00000.parquet": b"nouveau"})
    publies = lac.publier_dossier("curated/t.parquet", source)
    assert publies == ["curated/t.parquet/part-00000.parquet"]
    tailles = [len(appel[1]) for appel in client.appels_de("delete_objects")]
    assert tailles == [1000, 1]
    assert sorted(k for k in client.objets if k.startswith("kayak/curated/t.parquet/")) == [
        "kayak/curated/t.parquet/part-00000.parquet"
    ]
    assert client.objets["kayak/curated/t.parquet/part-00000.parquet"]["corps"] == b"nouveau"
    assert all(voisin in client.objets for voisin in voisins)
    operations = [appel[0] for appel in client.appels]
    dernier_envoi = max(i for i, op in enumerate(operations) if op == "put_object")
    assert operations.index("delete_objects") > dernier_envoi


def test_s3_publier_dossier_envoi_interrompu_laisse_l_ancien_jeu_intact(
    lake_s3, faux_client, source, tmp_path, monkeypatch
):
    """Si un envoi échoue, rien n'est supprimé : l'ancien jeu publié reste complet (jamais vide)."""
    ancien = tmp_path / "ancien"
    _ecrire_arbre(ancien, {"part-00000.parquet": b"v1-a", "part-00001.parquet": b"v1-b"})
    lake_s3.publier_dossier("curated/t.parquet", ancien)
    _ecrire_arbre(source, {"part-00002.parquet": b"v2-a", "part-00003.parquet": b"v2-b"})
    envois = faux_client.put_object

    def envoi_en_panne(Bucket, Key, Body, ServerSideEncryption=None):
        if Key.endswith("part-00003.parquet"):
            raise ClientError({"Error": {"Code": "RequestTimeout", "Message": "t"}}, "PutObject")
        envois(Bucket=Bucket, Key=Key, Body=Body, ServerSideEncryption=ServerSideEncryption)

    monkeypatch.setattr(faux_client, "put_object", envoi_en_panne)
    with pytest.raises(ClientError, match="RequestTimeout"):
        lake_s3.publier_dossier("curated/t.parquet", source)
    assert faux_client.appels_de("delete_objects") == []
    assert lake_s3.lire("curated/t.parquet/part-00000.parquet") == b"v1-a"
    assert lake_s3.lire("curated/t.parquet/part-00001.parquet") == b"v1-b"


# --------------------------------------------------------------------------------------------
# Mode S3 : cache local de chemin_local
# --------------------------------------------------------------------------------------------


def test_s3_chemin_local_objet_absent_leve_clienterror_sans_rien_telecharger(
    lake_s3, faux_client, racine
):
    """Un objet absent fait échouer ``chemin_local`` : ni téléchargement, ni fichier de cache."""
    with pytest.raises(ClientError, match="404"):
        lake_s3.chemin_local("raw/absent.csv")
    assert faux_client.appels == [("head_object", "kayak/raw/absent.csv")]
    assert [e for e in racine.rglob("*") if e.is_file()] == []


def test_s3_chemin_local_refus_de_droits_remonte_sans_rien_telecharger(lake_s3, faux_client):
    """Un 403 sur l'empreinte remonte tel quel : on ne télécharge pas un objet interdit."""
    faux_client.erreurs["kayak/raw/f.csv"] = {"Error": {"Code": "403", "Message": "Forbidden"}}
    with pytest.raises(ClientError, match=r"\(403\)"):
        lake_s3.chemin_local("raw/f.csv")
    assert faux_client.appels_de("download_file") == []


def test_s3_chemin_local_telechargement_interrompu_laisse_le_cache_intact(
    lake_s3, faux_client
):
    """Un téléchargement coupé ne remplace pas le cache valide : fichier et empreinte inchangés."""
    lake_s3.ecrire("raw/a/f.csv", "ancien\n")
    cible = lake_s3.chemin_local("raw/a/f.csv")
    marque = cible.with_name("f.csv.empreinte")
    empreinte_avant = marque.read_text(encoding="utf-8")
    lake_s3.ecrire("raw/a/f.csv", "nouveau, plus long\n")
    faux_client.panne_telechargement = OSError("connexion coupée")
    with pytest.raises(OSError, match="connexion coupée"):
        lake_s3.chemin_local("raw/a/f.csv")
    assert cible.read_text(encoding="utf-8") == "ancien\n"
    assert marque.read_text(encoding="utf-8") == empreinte_avant
    faux_client.panne_telechargement = None
    assert lake_s3.chemin_local("raw/a/f.csv").read_text(encoding="utf-8") == "nouveau, plus long\n"


@pytest.mark.parametrize(
    "entete",
    [None, {"ETag": '"abc"', "LastModified": "dimanche été"}],
    ids=["entetes_reels", "date_avec_accents"],
)
def test_s3_chemin_local_cache_valide_reste_intact_et_n_est_pas_retelecharge(
    lake_s3, faux_client, entete
):
    """Objet inchangé : seule l'empreinte est vérifiée (un ``head``), rien n'est retéléchargé."""
    lake_s3.ecrire("raw/a/f.csv", "a,b\n1,2\n")
    faux_client.entetes_forces = entete
    cible = lake_s3.chemin_local("raw/a/f.csv")
    modifie_avant = cible.stat().st_mtime_ns
    faux_client.appels.clear()
    assert lake_s3.chemin_local("raw/a/f.csv") == cible
    assert faux_client.appels == [("head_object", "kayak/raw/a/f.csv")]
    assert cible.stat().st_mtime_ns == modifie_avant


def test_s3_marque_du_cache_ecrite_en_utf8_meme_si_l_encodage_implicite_est_autre(
    lake_s3, faux_client, encodage_implicite_non_utf8
):
    """La marque porte l'empreinte en UTF-8, quel que soit l'encodage implicite.

    L'empreinte est « ETag sans guillemets|LastModified » : elle n'est ASCII que si l'en-tête
    de l'objet l'est, et ``LastModified`` peut contenir un accent. Sans ``encoding="utf-8"``,
    l'écriture suivrait l'encodage implicite, qui ne sait pas toujours écrire un accent.
    """
    lake_s3.ecrire("raw/a/f.csv", "a,b\n1,2\n")
    faux_client.entetes_forces = {"ETag": '"abc"', "LastModified": "dimanche été"}
    cible = lake_s3.chemin_local("raw/a/f.csv")
    marque = cible.with_name(cible.name + ".empreinte")
    assert marque.read_bytes() == "abc|dimanche été".encode()  # UTF-8 par defaut


def test_s3_cache_reutilise_meme_si_l_encodage_implicite_est_autre(
    lake_s3, faux_client, encodage_implicite_non_utf8
):
    """Objet inchangé, date non ASCII : le second appel réutilise le cache.

    La marque relue avec un encodage implicite qui ne sait pas lire un accent ne serait pas
    égale à l'empreinte : l'objet serait retéléchargé à chaque appel, sans message.
    """
    lake_s3.ecrire("raw/a/f.csv", "a,b\n1,2\n")
    faux_client.entetes_forces = {"ETag": '"abc"', "LastModified": "dimanche été"}
    premier = lake_s3.chemin_local("raw/a/f.csv")
    faux_client.appels.clear()
    second = lake_s3.chemin_local("raw/a/f.csv")
    assert second == premier
    assert faux_client.appels == [("head_object", "kayak/raw/a/f.csv")]


def test_s3_chemin_local_deux_objets_gardent_chacun_leur_cache_intact(lake_s3, faux_client):
    """Le cache de ``a.csv`` n'est pas invalidé par celui de ``b.csv`` (une empreinte par objet)."""
    lake_s3.ecrire("raw/a.csv", "A")
    lake_s3.ecrire("raw/b.csv", "B")
    a = lake_s3.chemin_local("raw/a.csv")
    b = lake_s3.chemin_local("raw/b.csv")
    faux_client.appels.clear()
    assert lake_s3.chemin_local("raw/a.csv") == a
    assert lake_s3.chemin_local("raw/b.csv") == b
    assert faux_client.appels == [
        ("head_object", "kayak/raw/a.csv"),
        ("head_object", "kayak/raw/b.csv"),
    ]
    assert (a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")) == ("A", "B")


def test_s3_lire_laisse_le_cache_local_intact(lake_s3, faux_client):
    """``lire`` va toujours chercher la version distante ; le cache local reste intact."""
    lake_s3.ecrire("raw/a/f.csv", "v1")
    cible = lake_s3.chemin_local("raw/a/f.csv")
    lake_s3.ecrire("raw/a/f.csv", "v2")
    assert lake_s3.lire("raw/a/f.csv") == b"v2"
    assert cible.read_text(encoding="utf-8") == "v1"


# --------------------------------------------------------------------------------------------
# Construction : effets de bord et client S3
# --------------------------------------------------------------------------------------------


def test_construction_sans_effet_au_dela_de_la_racine_et_sans_appel_s3(tmp_path, faux_client):
    """Construire un lake crée sa racine et rien d'autre, et n'appelle jamais S3."""
    racine = tmp_path / "nouvelle" / "racine"
    assert not racine.exists()
    DataLake(bucket="mon-bucket", racine_locale=racine, client_s3=faux_client)
    assert racine.is_dir()
    assert list(racine.iterdir()) == []
    assert faux_client.appels == []


def test_construire_un_second_lake_sur_la_meme_racine_laisse_son_contenu_intact(racine):
    """Une racine déjà existante n'est pas une erreur : le second lake y retrouve les objets."""
    DataLake(bucket=None, racine_locale=racine).ecrire("raw/f.txt", "1")
    second = DataLake(bucket=None, racine_locale=racine)
    assert second.lire("raw/f.txt") == b"1"
    assert second.lister("") == ["raw/f.txt"]


def test_mode_local_laisse_le_client_s3_injecte_intact(racine, faux_client, source):
    """Sans bucket, aucune opération ne sollicite le client S3, même s'il est injecté."""
    lac = DataLake(bucket=None, racine_locale=racine, client_s3=faux_client)
    lac.ecrire("raw/f.txt", "1")
    assert lac.lire("raw/f.txt") == b"1"
    assert lac.existe("raw/f.txt") is True
    assert lac.lister("raw") == ["raw/f.txt"]
    assert lac.chemin_local("raw/f.txt").is_file()
    _ecrire_arbre(source, {"part-00000.parquet": b"a"})
    lac.publier_dossier("curated/t", source)
    lac.supprimer("raw")
    lac.dossier_temporaire()
    assert faux_client.appels == []


def test_client_injecte_laisse_boto3_intact_aucun_client_n_est_cree(lake_s3, monkeypatch):
    """Avec un client injecté, ``boto3.client`` n'est jamais appelé (pas de compte AWS requis)."""

    def interdit(*args, **kwargs):
        raise AssertionError("boto3.client ne doit pas être appelé")

    monkeypatch.setattr(boto3, "client", interdit)
    lake_s3.ecrire("raw/f.txt", "1")
    assert lake_s3.lire("raw/f.txt") == b"1"
    assert lake_s3.existe("raw/f.txt") is True
    assert lake_s3.lister("raw") == ["raw/f.txt"]
    assert lake_s3.supprimer("raw/") == 1


# --------------------------------------------------------------------------------------------
# Tests directs : valeurs exactes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chemin, contenu, octets",
    [
        ("raw/a/b/f.txt", "été", b"\xc3\xa9t\xc3\xa9"),
        ("/XML/b.bin", b"\xff\x00\xfe", b"\xff\x00\xfe"),
        ("sans_extension", b"x", b"x"),
    ],
    ids=["texte_utf8", "binaire_chemin_absolu", "sans_extension"],
)
def test_ecrire_local_ecrit_les_octets_remplace_l_existant_et_renvoie_l_uri(
    lake, racine, chemin, contenu, octets
):
    """Écriture locale : str encodé en UTF-8, bytes tels quels, dossiers créés, ancien contenu (plus
    long) entièrement remplacé, aucun ``.tmp`` laissé, URI ``file://`` exacte."""
    cible = racine / chemin.lstrip("/")
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_bytes(b"ANCIEN CONTENU BIEN PLUS LONG QUE LE NOUVEAU" * 3)
    uri = lake.ecrire(chemin, contenu)
    assert uri == f"file://{cible.resolve()}"
    assert cible.read_bytes() == octets
    assert [e.name for e in cible.parent.iterdir()] == [cible.name]


def test_lister_local_renvoie_des_chemins_logiques_separes_par_des_slash(lake, racine):
    """Chemins relatifs à la racine, séparés par « / », fichiers seulement, préfixe « / » toléré."""
    lake.ecrire("a/f1.txt", "1")
    lake.ecrire("a/b/c/f2.txt", "2")
    lake.ecrire("z/f3.txt", "3")
    lake.ecrire("XML/f4.xml", "4")
    (racine / "a" / "vide").mkdir()
    assert sorted(lake.lister("a")) == ["a/b/c/f2.txt", "a/f1.txt"]
    assert sorted(lake.lister("/a")) == ["a/b/c/f2.txt", "a/f1.txt"]
    assert sorted(lake.lister("a/b")) == ["a/b/c/f2.txt"]
    assert sorted(lake.lister("/XML")) == ["XML/f4.xml"]
    assert sorted(lake.lister("")) == ["XML/f4.xml", "a/b/c/f2.txt", "a/f1.txt", "z/f3.txt"]


def test_publier_dossier_local_monte_le_jeu_a_cote_puis_renvoie_les_chemins_tries(
    lake, racine, source, monkeypatch
):
    """Le jeu est monté dans un dossier voisin ``<nom>.montage-...`` puis échangé en une fois ;
    il ne reste que la cible (dossiers parents créés au besoin) ; les chemins publiés sont
    triés, sous-dossiers conservés."""
    _ecrire_arbre(
        source,
        {
            "part-00001.parquet": b"b",
            "part-00000.parquet": b"a",
            "sous/dossier/part-00002.parquet": b"c",
        },
    )
    echanges: list[tuple[Path, Path]] = []
    vrai_echange = os.replace

    def echange_espion(origine, destination):
        echanges.append((Path(origine), Path(destination)))
        return vrai_echange(origine, destination)

    monkeypatch.setattr(os, "replace", echange_espion)
    publies = lake.publier_dossier("/XML/villes/date=2026-10-10/villes.parquet/", source)
    dossier = "XML/villes/date=2026-10-10/villes.parquet"
    assert publies == [
        f"{dossier}/part-00000.parquet",
        f"{dossier}/part-00001.parquet",
        f"{dossier}/sous/dossier/part-00002.parquet",
    ]
    [(montage, cible)] = echanges
    assert cible == racine / "XML" / "villes" / "date=2026-10-10" / "villes.parquet"
    assert montage.parent == cible.parent
    assert montage.name.startswith("villes.parquet.montage-")
    assert [e.name for e in cible.parent.iterdir()] == ["villes.parquet"]
    assert {chemin: lake.lire(chemin) for chemin in publies} == {
        f"{dossier}/part-00000.parquet": b"a",
        f"{dossier}/part-00001.parquet": b"b",
        f"{dossier}/sous/dossier/part-00002.parquet": b"c",
    }


@pytest.mark.parametrize(
    "bucket, prefixe, chemin, mode, cle",
    [
        (None, "kayak", "x/y.txt", "local", None),
        ("", "kayak", "x/y.txt", "local", None),
        ("b", "kayak", "x/y.txt", "s3", "kayak/x/y.txt"),
        ("b", "/kayak/", "/x/y.txt", "s3", "kayak/x/y.txt"),
        ("b", "a/b", "x.txt", "s3", "a/b/x.txt"),
        ("b", "/Xkayak/", "/x/y.txt", "s3", "Xkayak/x/y.txt"),
    ],
    ids=[
        "sans_bucket",
        "bucket_vide",
        "s3",
        "slashs_en_trop",
        "prefixe_a_deux_niveaux",
        "prefixe_commencant_par_x",
    ],
)
def test_mode_selon_le_bucket_prefixe_nettoye_et_uri_sans_double_slash(
    racine, bucket, prefixe, chemin, mode, cle
):
    """Un bucket non vide active S3 ; le préfixe est débarrassé de ses « / » ; l'URI est propre."""
    client = FauxClientS3(bucket=bucket or "b")
    lac = DataLake(bucket=bucket, prefixe=prefixe, racine_locale=racine, client_s3=client)
    assert lac.mode == mode
    uri = lac.ecrire(chemin, "1")
    if cle is None:
        assert uri == f"file://{(racine / chemin.lstrip('/')).resolve()}"
        assert client.appels == []
    else:
        assert uri == f"s3://{bucket}/{cle}"
        assert list(client.objets) == [cle]


def test_client_boto3_par_defaut_est_cree_une_seule_fois(racine, monkeypatch):
    """Sans client injecté, ``boto3.client("s3")`` est appelé une fois puis réutilisé."""
    client = FauxClientS3()
    creations: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fabrique(*args, **kwargs):
        creations.append((args, kwargs))
        return client

    monkeypatch.setattr(boto3, "client", fabrique)
    lac = DataLake(bucket="mon-bucket", racine_locale=racine)
    lac.ecrire("a.txt", "1")
    assert lac.lire("a.txt") == b"1"
    assert lac.existe("a.txt") is True
    assert creations == [(("s3",), {})]


@pytest.mark.parametrize("chemin", ["raw/a/f.txt", "/raw/a/f.txt"])
def test_s3_ecrire_envoie_un_objet_chiffre_a_la_bonne_cle_et_renvoie_l_uri(
    lake_s3, faux_client, chemin
):
    """Écriture S3 : clé ``kayak/...``, octets UTF-8, chiffrement AES256, URI ``s3://`` exacte."""
    assert lake_s3.ecrire(chemin, "été") == "s3://mon-bucket/kayak/raw/a/f.txt"
    assert faux_client.appels == [("put_object", "kayak/raw/a/f.txt", "AES256")]
    assert faux_client.objets["kayak/raw/a/f.txt"]["corps"] == b"\xc3\xa9t\xc3\xa9"
    assert lake_s3.lire(chemin) == b"\xc3\xa9t\xc3\xa9"


def test_s3_lister_suit_les_jetons_de_continuation_dans_l_ordre(lake_s3, faux_client):
    """Cinq objets en pages de deux : trois appels, le premier sans jeton, puis les jetons."""
    for nom in "abcde":
        lake_s3.ecrire(f"raw/x/{nom}.txt", nom)
    lake_s3.ecrire("curated/y.txt", "y")
    faux_client.appels.clear()
    assert lake_s3.lister("raw/x/") == [f"raw/x/{nom}.txt" for nom in "abcde"]
    assert faux_client.appels == [
        ("list_objects_v2", "kayak/raw/x/", None),
        ("list_objects_v2", "kayak/raw/x/", "2"),
        ("list_objects_v2", "kayak/raw/x/", "4"),
    ]
    assert lake_s3.lister("") == [
        "curated/y.txt",
        *[f"raw/x/{nom}.txt" for nom in "abcde"],
    ]


@pytest.mark.parametrize(
    "nombre, tailles",
    [(1, [1]), (1000, [1000]), (1001, [1000, 1]), (2500, [1000, 1000, 500])],
    ids=["un", "mille", "mille_un", "deux_mille_cinq_cents"],
)
def test_s3_supprimer_envoie_les_cles_par_lots_de_mille_au_plus(racine, nombre, tailles):
    """S3 refuse plus de 1 000 clés par suppression : les lots font 1 000 au plus, sans oubli."""
    client = FauxClientS3(taille_page=1000)
    lac = DataLake(bucket="mon-bucket", prefixe="kayak", racine_locale=racine, client_s3=client)
    _peupler(client, [f"kayak/raw/{i:05d}.txt" for i in range(nombre)])
    client.appels.clear()
    assert lac.supprimer("raw/") == nombre
    assert [len(appel[1]) for appel in client.appels_de("delete_objects")] == tailles
    assert client.objets == {}


@pytest.mark.parametrize(
    "chemin, relatif, cle",
    [
        ("raw/a/f.csv", ("raw", "a", "f.csv"), "kayak/raw/a/f.csv"),
        ("/XML/a/f.csv", ("XML", "a", "f.csv"), "kayak/XML/a/f.csv"),
    ],
    ids=["chemin_relatif", "chemin_absolu_commencant_par_x"],
)
@pytest.mark.parametrize(
    "entete, empreinte",
    [
        (None, f"{MD5_CSV}|1"),
        ({}, "|"),
        ({"ETag": "abc"}, "abc|"),
        ({"ETag": '"abc"'}, "abc|"),
        ({"ETag": '"abc"', "LastModified": "2026-10-10"}, "abc|2026-10-10"),
        ({"ETag": '"abc"', "LastModified": "dimanche été"}, "abc|dimanche été"),
    ],
    ids=[
        "entetes_reels",
        "aucun_entete",
        "etag_sans_guillemets",
        "etag_cite",
        "etag_et_date",
        "date_avec_accents",
    ],
)
def test_s3_chemin_local_telecharge_dans_le_cache_avec_l_empreinte_etag_et_date(
    lake_s3, faux_client, racine, chemin, relatif, cle, entete, empreinte
):
    """Le fichier est téléchargé vers ``.partiel`` puis posé dans ``.cache_s3`` ; son empreinte
    « ETag sans guillemets|date » est écrite à côté (en UTF-8) ; aucun ``.partiel`` ne subsiste."""
    lake_s3.ecrire(chemin, "a,b\n1,2\n")
    faux_client.entetes_forces = entete
    cible = lake_s3.chemin_local(chemin)
    assert cible == racine.joinpath(".cache_s3", *relatif)
    assert cible.read_text(encoding="utf-8") == "a,b\n1,2\n"
    assert cible.with_name("f.csv.empreinte").read_text(encoding="utf-8") == empreinte
    assert sorted(e.name for e in cible.parent.iterdir()) == ["f.csv", "f.csv.empreinte"]
    assert faux_client.appels_de("download_file") == [("download_file", cle, f"{cible}.partiel")]


@pytest.mark.parametrize("alteration", ["cible_absente", "empreinte_absente", "empreinte_fausse"])
def test_s3_chemin_local_retelecharge_quand_le_cache_n_est_pas_digne_de_confiance(
    lake_s3, faux_client, alteration
):
    """Fichier manquant, empreinte manquante ou empreinte différente : le cache est retéléchargé."""
    lake_s3.ecrire("raw/a/f.csv", "contenu\n")
    cible = lake_s3.chemin_local("raw/a/f.csv")
    marque = cible.with_name("f.csv.empreinte")
    empreinte = marque.read_text(encoding="utf-8")
    if alteration == "cible_absente":
        cible.unlink()
    elif alteration == "empreinte_absente":
        marque.unlink()
    else:
        marque.write_text("autre|empreinte", encoding="utf-8")
    assert lake_s3.chemin_local("raw/a/f.csv") == cible
    assert cible.read_text(encoding="utf-8") == "contenu\n"
    assert marque.read_text(encoding="utf-8") == empreinte
    telechargements = faux_client.appels_de("download_file")
    assert [appel[1] for appel in telechargements] == ["kayak/raw/a/f.csv"] * 2


@pytest.mark.parametrize("prefixe", ["/raw/a", "//raw/a", "raw/a"])
def test_lister_ignore_les_barres_obliques_de_tete_en_local_et_en_s3(lake, lake_s3, prefixe):
    """Un préfixe écrit avec une ou deux « / » en tête désigne le même dossier logique que sans.

    En local, une « / » de tête ferait sortir de la racine du lake (chemin absolu) ; en S3, elle
    ferait chercher une clé qui n'existe pas. Les deux modes rendent ici le même chemin.
    """
    for lac in (lake, lake_s3):
        lac.ecrire("raw/a/f.txt", "1")
        assert lac.lister(prefixe) == ["raw/a/f.txt"]
