"""NZBGet, qBittorrent and Transmission, each driven through the real download pipeline against a fake."""

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from narratarr import config, db, worker
from narratarr.worker import Worker, link_into

HASH = "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"
NZB = b'<?xml version="1.0"?>\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)


def torrent_book(**extra) -> dict:
    return db.insert({
        "source": "audiobookbay", "source_url": "https://audiobookbay.lu/abss/dune/", "title": "Dune",
        "author": "Frank Herbert", "info_hash": HASH, "status": "queued", **extra,
    })


def finished_download(root: Path) -> Path:
    folder = root / "downloads" / "Dune"
    folder.mkdir(parents=True)
    (folder / "Dune.m4b").write_bytes(b"audio")
    return folder


class FakeNZBGet:
    def __init__(self, dest: Path, history="SUCCESS/UNPACK"):
        self.dest, self.history, self.polls, self.appended, self.calls = dest, history, 0, None, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "prowlarr":
            return httpx.Response(200, content=NZB)
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"nzbget:secret").decode()
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        result = None
        if method == "append":
            self.appended = params
            result = 42
        elif method == "listgroups":
            self.polls += 1
            result = [{"NZBID": 42, "Status": "DOWNLOADING", "FileSizeMB": 100, "RemainingSizeMB": 25}] if self.polls == 1 else []
        elif method == "status":
            result = {"DownloadRate": 5000, "DownloadPaused": False}
        elif method == "history":
            result = [{"NZBID": 42, "Status": self.history, "DestDir": str(self.dest), "FinalDir": ""}]
        elif method == "editqueue":
            result = True
        return httpx.Response(200, json={"result": result})


async def test_nzbget_pipeline(isolated_data):
    config.update({
        "usenet_client": "nzbget", "nzbget_url": "http://nzbget:6789", "nzbget_username": "nzbget",
        "nzbget_password": "secret", "nzbget_category": "Audiobooks",
        "prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "K",
    })
    fake = FakeNZBGet(finished_download(isolated_data))
    book = db.insert({
        "source": "prowlarr", "source_url": "https://nzbs.example/1", "title": "Dune", "author": "Frank Herbert",
        "protocol": "usenet", "download_url": "http://prowlarr:9696/1/download?link=x", "status": "queued",
    })

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert (Path(done["path"]) / "Dune.m4b").read_bytes() == b"audio"
    name, content, category = fake.appended[:3]
    assert name == "Frank Herbert - Dune.nzb" and category == "Audiobooks"
    assert base64.b64decode(content) == NZB
    assert "editqueue" in fake.calls  # cleared from NZBGet's history


async def test_nzbget_failure(isolated_data):
    config.update({"usenet_client": "nzbget", "nzbget_url": "http://nzbget:6789", "nzbget_username": "nzbget",
                   "nzbget_password": "secret", "prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "K"})
    book = db.insert({"source": "prowlarr", "source_url": "https://nzbs.example/2", "title": "Dune",
                      "protocol": "usenet", "download_url": "http://prowlarr:9696/x", "status": "queued"})
    await Worker(transport=httpx.MockTransport(FakeNZBGet(isolated_data, history="FAILURE/PAR")))._run(book["id"])
    failed = db.get(book["id"])
    assert failed["status"] == "failed" and "failure/par" in failed["error"]


class FakeQbit:
    def __init__(self, path: Path):
        self.path, self.logged_in, self.added, self.polls, self.deleted = path, False, None, 0, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/auth/login":
            ok = parse_qs(request.content.decode()) == {"username": ["admin"], "password": ["pw"]}
            self.logged_in = ok
            # qBittorrent 5.x: 204 on success, 401 on failure
            return httpx.Response(204 if ok else 401, headers={"set-cookie": "SID=abc; path=/"} if ok else {})
        if not self.logged_in or "SID=abc" not in request.headers.get("cookie", ""):
            return httpx.Response(403)
        if path == "/api/v2/torrents/add":
            self.added = request.content
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            self.polls += 1
            done = self.polls > 2
            t = {"hash": HASH, "state": "uploading" if done else "downloading", "progress": 1 if done else 0.5,
                 "dlspeed": 1000, "num_seeds": 3, "content_path": str(self.path)}
            return httpx.Response(200, json=[t])
        if path == "/api/v2/torrents/delete":
            self.deleted = True
            return httpx.Response(200)
        return httpx.Response(404)


async def test_qbittorrent_keeps_seeding(isolated_data):
    config.update({"torrent_client": "qbittorrent", "qbittorrent_url": "http://qbit:8080",
                   "qbittorrent_username": "admin", "qbittorrent_password": "pw"})
    original = finished_download(isolated_data)
    fake = FakeQbit(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert (Path(done["path"]) / "Dune.m4b").read_bytes() == b"audio"
    assert (original / "Dune.m4b").exists() and not fake.deleted  # still there to seed
    assert HASH.encode() in fake.added and b"narratarr-" in fake.added and b"audiobooks" in fake.added
    assert done["download_client"] == "qbittorrent" and done["client_job_id"] == HASH


async def test_qbittorrent_bad_password(isolated_data):
    config.update({"torrent_client": "qbittorrent", "qbittorrent_url": "http://qbit:8080",
                   "qbittorrent_username": "admin", "qbittorrent_password": "nope"})
    book = torrent_book()
    await Worker(transport=httpx.MockTransport(FakeQbit(isolated_data)))._run(book["id"])
    assert "password" in db.get(book["id"])["error"]


async def test_qbittorrent_old_style_login(isolated_data):
    from narratarr.qbittorrent import QBittorrent

    def handler(request):
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"})
        return httpx.Response(200, text="v4.6.7")

    async with QBittorrent("http://q:8080", "admin", "pw", transport=httpx.MockTransport(handler)) as q:
        assert await q.version() == "v4.6.7"


class FakeTransmission:
    def __init__(self, folder: Path):
        self.folder, self.polls, self.added = folder, 0, None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-transmission-session-id") != "S1":
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "S1"})
        body = json.loads(request.content)
        if body["method"] == "torrent-add":
            self.added = body["arguments"]
            return httpx.Response(200, json={"result": "success", "arguments": {
                "torrent-added": {"hashString": HASH.upper(), "id": 1, "name": "Dune"}}})
        if body["method"] == "torrent-get":
            self.polls += 1
            done = self.polls > 1
            if body["arguments"].get("fields") == ["labels"]:
                return httpx.Response(200, json={"result": "success", "arguments": {"torrents": [{"labels": getattr(self, "labels", [])}]}})
            t = {"hashString": HASH, "name": self.folder.name, "status": 6 if done else 4,
                 "percentDone": 1 if done else 0.3, "leftUntilDone": 0 if done else 10, "rateDownload": 500,
                 "error": 0, "downloadDir": "/data/torrents", "metadataPercentComplete": 1, "peersSendingToUs": 2}
            return httpx.Response(200, json={"result": "success", "arguments": {"torrents": [t]}})
        if body["method"] == "torrent-set":
            self.labels = body["arguments"].get("labels", [])
            return httpx.Response(200, json={"result": "success", "arguments": {}})
        if body["method"] == "torrent-remove":
            self.removed = body["arguments"]
            return httpx.Response(200, json={"result": "success", "arguments": {}})
        return httpx.Response(200, json={"result": "method not recognized"})


async def test_transmission_with_path_mapping(isolated_data):
    original = finished_download(isolated_data)
    config.update({"torrent_client": "transmission", "transmission_url": "http://transmission:9091",
                   "path_remote": "/data/torrents", "path_local": str(original.parent)})
    fake = FakeTransmission(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert fake.added["filename"].startswith(f"magnet:?xt=urn:btih:{HASH}")
    assert (original / "Dune.m4b").exists()


class FakeDeluge:
    """Deluge's web API answers 200 with an error object, and reports hashes in upper case."""

    def __init__(self, folder: Path):
        self.folder, self.polls, self.added, self.options, self.calls = folder, 0, None, None, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if method == "auth.login":
            return httpx.Response(200, json={"result": params == ["pw"], "error": None, "id": body["id"]})
        if method == "web.connected":
            return httpx.Response(200, json={"result": True, "error": None, "id": body["id"]})
        if method == "get_plugins":
            return httpx.Response(200, json={"result": {"Label": {"enabled": True}}, "error": None, "id": body["id"]})
        if method == "daemon.get_version":
            return httpx.Response(200, json={"result": "2.1.1", "error": None, "id": body["id"]})
        if method == "core.add_torrent_magnet":
            self.added, self.options = params[0], params[1]
            return httpx.Response(200, json={"result": HASH.upper(), "error": None, "id": body["id"]})
        if method == "core.get_torrent_status":
            if params[1] == ["label"]:
                return httpx.Response(200, json={"result": {"label": self.options.get("label", "")}, "error": None, "id": body["id"]})
            self.polls += 1
            done = self.polls > 1
            status = {"name": self.folder.name, "download_location": "/data/torrents",
                      "state": "Seeding" if done else "Downloading", "progress": 100 if done else 30,
                      "is_finished": done, "download_payload_rate": 500, "message": ""}
            return httpx.Response(200, json={"result": status, "error": None, "id": body["id"]})
        if method == "core.remove_torrent":
            self.removed = params
            return httpx.Response(200, json={"result": True, "error": None, "id": body["id"]})
        return httpx.Response(200, json={"result": None, "error": {"message": "Unknown method" + "", "code": 1}, "id": body["id"]})


async def test_deluge_with_path_mapping(isolated_data):
    original = finished_download(isolated_data)
    config.update({"torrent_client": "deluge", "deluge_url": "http://deluge:8112", "deluge_password": "pw",
                   "path_remote": "/data/torrents", "path_local": str(original.parent)})
    fake = FakeDeluge(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert fake.added.startswith(f"magnet:?xt=urn:btih:{HASH}")
    assert fake.options["label"] == f"narratarr-{book['id']}"  # our ownership tag, from the Label plugin
    assert done["client_job_id"] == HASH  # Deluge's upper-case hash normalised
    assert (original / "Dune.m4b").exists()


async def test_deluge_reports_an_rpc_error(isolated_data):
    """A failed call is a 200 with an error object, not an HTTP status."""

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "auth.login":
            return httpx.Response(200, json={"result": False, "error": {"message": "Bad login", "code": 1}, "id": 1})
        return httpx.Response(200, json={"result": None, "error": None, "id": 1})

    from narratarr.deluge import Deluge, DelugeError

    with pytest.raises(DelugeError):
        async with Deluge("http://deluge:8112", "wrong", transport=httpx.MockTransport(handler)) as d:
            await d.version()


def test_link_into_single_file_and_folder(tmp_path):
    src = tmp_path / "t"
    (src / "CD1").mkdir(parents=True)
    (src / "CD1" / "01.mp3").write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    link_into(src, out)
    link_into(src / "CD1" / "01.mp3", out)
    assert (out / "CD1" / "01.mp3").read_bytes() == b"x" and (out / "01.mp3").exists()
    assert (src / "CD1" / "01.mp3").exists()


def test_settings_carry_over_old_sabnzbd_path_mapping(isolated_data, monkeypatch):
    raw = json.loads(config.SETTINGS_FILE.read_text())
    raw.pop("path_remote"), raw.pop("path_local")
    raw.update({"sabnzbd_remote_path": "/downloads", "sabnzbd_local_path": "/mnt/usenet"})
    config.SETTINGS_FILE.write_text(json.dumps(raw))
    monkeypatch.setattr(config, "_settings", None)
    s = config.load()
    assert (s.path_remote, s.path_local) == ("/downloads", "/mnt/usenet")
    assert config.update({"torrent_client": "bogus"}).torrent_client == "realdebrid"
    pub = config.update({"torrent_client": "qbittorrent", "qbittorrent_url": "http://q", "qbittorrent_password": "secret1"}).public()
    assert pub["torrent_configured"] and "secret" not in pub["qbittorrent_password"]
