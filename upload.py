"""YouTube-Upload fuer den Kanal "BedoZg" (YouTube Data API v3).

Zugangsdaten: client_secret.json (OAuth-Client aus dem EIGENEN Google-Cloud-
Projekt dieses Kanals: Tageslimit 10.000 Einheiten pro Projekt, ein Upload
kostet 1.600, 4 am Tag = 6.400) und token.json. Beide nie ins Repo, in GitHub
als Secrets YT_CLIENT_SECRET / YT_TOKEN.

Einmalig lokal: python upload.py --login  -> Browser, mit dem Google-Konto
anmelden, das den Kanal "BedoZg" besitzt, und den Kanal waehlen.
"""

import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

HERE = Path(__file__).resolve().parent
CLIENT_SECRET = HERE / "client_secret.json"
TOKEN = HERE / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
EXPECTED_CHANNEL = "BedoZg"


def client(interactive=False):
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif interactive:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError("kein gueltiger token.json, einmal 'python upload.py --login' ausfuehren")
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def assert_channel(yt):
    """Kanal-Sperre: Am 2026-11-08 landeten bei Deutschestreamerclips Clips auf
    dem falschen Kanal, weil das falsche Token im Secret lag. Vor jedem Lauf
    pruefen, wem der Schluessel gehoert, sonst abbrechen."""
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    names = [i["snippet"]["title"] for i in items]
    if EXPECTED_CHANNEL not in names:
        raise SystemExit(f"KANAL-SPERRE: Schluessel gehoert zu {names}, erwartet "
                         f"'{EXPECTED_CHANNEL}'. Nichts hochgeladen. Secret YT_TOKEN pruefen.")
    print(f"Kanal geprueft: {EXPECTED_CHANNEL}", flush=True)


def make_description(login):
    """Ohne Links: Externe Links in Shorts-Beschreibungen verlangen eine
    Identitaetsbestaetigung des Kanals (Erfahrung Deutschestreamerclips)."""
    return "\n".join([
        f"Live auf Twitch: {login}",
        "",
        "Die besten Momente aus meinen Streams, jeden Tag neu.",
        "",
        f"#shorts #{login} #twitch #gaming #clips",
    ])


def upload(yt, video, login, name, privacy, title):
    body = {
        "snippet": {"title": title,
                    "description": make_description(login),
                    "tags": [name, login, "Twitch", "Clips", "Gaming", "Shorts"],
                    "categoryId": "20",          # Gaming
                    "defaultLanguage": "de", "defaultAudioLanguage": "de"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), chunksize=-1, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


if __name__ == "__main__":
    if "--login" in sys.argv:
        yt = client(interactive=True)
        ch = yt.channels().list(part="snippet", mine=True).execute()
        for item in ch.get("items", []):
            print("Angemeldet fuer Kanal:", item["snippet"]["title"])
