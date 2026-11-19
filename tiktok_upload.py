"""TikTok-Upload fuer @bedozgtwitchhighlights (Content Posting API, Direct Post).

Zugangsdaten: tiktok_client.json (client_key, client_secret der App "BedoZg",
Sandbox) und tiktok_token.json (access/refresh token). Beide nie ins Repo.

Ohne Audit durch TikTok sind nur private Posts erlaubt (SELF_ONLY). Passt zur
Entscheidung von Boss: privat hochladen, Boss schaltet selbst frei.

Einmalig lokal:
  python tiktok_upload.py --login-url        -> Link oeffnen, mit dem TikTok-Konto
                                                anmelden, Code auf der Rueckseite kopieren
  python tiktok_upload.py --code-file DATEI  -> Code gegen Token tauschen
Test:
  python tiktok_upload.py --test VIDEO.mp4 "Titel"
"""

import json
import math
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CLIENT = HERE / "tiktok_client.json"
TOKEN = HERE / "tiktok_token.json"
REDIRECT_URI = "https://bedo08.github.io/bedozg-clips/tiktok-callback.html"
SCOPES = "user.info.basic,video.upload,video.publish"
API = "https://open.tiktokapis.com/v2"
PRIVACY = "SELF_ONLY"


def _client():
    return json.loads(CLIENT.read_text(encoding="utf-8"))


def login_url():
    state = secrets.token_urlsafe(12)
    q = urllib.parse.urlencode({
        "client_key": _client()["client_key"],
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    return f"https://www.tiktok.com/v2/auth/authorize/?{q}"


def _save_token(data):
    data["obtained_at"] = int(time.time())
    TOKEN.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _token_request(payload):
    c = _client()
    payload.update({"client_key": c["client_key"], "client_secret": c["client_secret"]})
    r = requests.post(f"{API}/oauth/token/", data=payload,
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    data = r.json()
    if "access_token" not in data:
        raise SystemExit(f"TikTok-Token-Fehler: {data.get('error')} {data.get('error_description')}")
    return data


def exchange_code(code):
    _save_token(_token_request({"code": code.strip(), "grant_type": "authorization_code",
                                "redirect_uri": REDIRECT_URI}))


def access_token():
    """Access token gilt 24 h, refresh token 365 Tage. Vor jedem Lauf erneuern,
    die neue Datei muss danach wieder gesichert werden (TikTok kann den refresh
    token dabei austauschen)."""
    t = json.loads(TOKEN.read_text(encoding="utf-8"))
    if time.time() > t["obtained_at"] + t["expires_in"] - 600:
        t = _token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        _save_token(t)
    return t["access_token"]


def _post(path, token, body):
    r = requests.post(f"{API}{path}", json=body, timeout=60, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    data = r.json()
    err = data.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok {path}: {err.get('code')} {err.get('message')}")
    return data.get("data", {})


def whoami(token):
    r = requests.get(f"{API}/user/info/", params={"fields": "open_id,display_name"},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return r.json().get("data", {}).get("user", {})


def upload(video_path, title):
    """Laedt ein Video privat hoch, gibt die publish_id zurueck."""
    token = access_token()
    info = _post("/post/publish/creator_info/query/", token, {})
    if PRIVACY not in info.get("privacy_level_options", []):
        raise RuntimeError(f"TikTok erlaubt {PRIVACY} nicht: {info.get('privacy_level_options')}")

    size = Path(video_path).stat().st_size
    # Regeln laut TikTok: unter 64 MB ein Stueck, sonst Stuecke 5 bis 64 MB,
    # das letzte Stueck nimmt den Rest auf.
    if size <= 64 * 1024 * 1024:
        chunk, count = size, 1
    else:
        chunk = 10 * 1024 * 1024
        count = math.floor(size / chunk)

    data = _post("/post/publish/video/init/", token, {
        "post_info": {
            "title": title[:2200],
            "privacy_level": PRIVACY,
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
        },
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk, "total_chunk_count": count},
    })
    publish_id, url = data["publish_id"], data["upload_url"]

    with open(video_path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = requests.put(url, data=body, timeout=300, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok Upload Stueck {i + 1}/{count}: HTTP {r.status_code} {r.text[:200]}")
    return publish_id


def upload_draft(video_path):
    """Entwurf-Weg (scope video.upload): Video landet im TikTok-Postfach von
    Boss, er gibt Titel/Ton in der App ein und postet selbst. Direct Post geht
    ohne Audit nur bei privatem Konto (Fehler
    unaudited_client_can_only_post_to_private_accounts, 19.11.)."""
    token = access_token()
    size = Path(video_path).stat().st_size
    if size <= 64 * 1024 * 1024:
        chunk, count = size, 1
    else:
        chunk = 10 * 1024 * 1024
        count = math.floor(size / chunk)
    data = _post("/post/publish/inbox/video/init/", token, {
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk, "total_chunk_count": count}})
    publish_id, url = data["publish_id"], data["upload_url"]
    with open(video_path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = requests.put(url, data=body, timeout=300, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok Upload Stueck {i + 1}/{count}: HTTP {r.status_code} {r.text[:200]}")
    return publish_id


def status(publish_id):
    return _post("/post/publish/status/fetch/", access_token(), {"publish_id": publish_id})


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--login-url"]:
        print(login_url())
    elif a[:1] == ["--code-file"]:
        exchange_code(Path(a[1]).read_text(encoding="utf-8"))
        u = whoami(access_token())
        print(f"Angemeldet als: {u.get('display_name')}")
    elif a[:1] == ["--whoami"]:
        u = whoami(access_token())
        if not u.get("display_name"):
            print("::error::TikTok-Schluessel ungueltig, neu anmelden (tiktok_upload.py --login-url)")
            sys.exit(1)
        print(f"TikTok-Konto geprueft: {u['display_name']}")
    elif a[:1] in (["--test"], ["--test-draft"]):
        pid = upload(a[1], a[2]) if a[0] == "--test" else upload_draft(a[1])
        print(f"Hochgeladen, publish_id {pid}")
        for _ in range(20):
            time.sleep(6)
            s = status(pid)
            print(s.get("status"), s.get("fail_reason", ""))
            if s.get("status") in ("PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX"):
                break
    else:
        print(__doc__)
