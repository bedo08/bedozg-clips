"""Taeglicher Lauf fuer den Kanal "BedoZg" (Boss' eigener Twitch, seit 2026-11-18).

Kleiner Kanal = kaum Zuschauer-Clips. Deshalb zwei Quellen:
1. Echte Twitch-Clips von bedozg (letzte 7 Tage), die kommen zuerst dran.
2. Gespeicherte Streams (VODs): Pro neuem VOD wird nur die Tonspur geladen,
   find_highlights.py sucht die spannendsten Stellen (Lautstaerke-Sprung +
   Hochfrequenz = Schreien, Lachen, Hype). Die Stellen landen in vods.json und
   werden ueber die Tage verteilt abgearbeitet, beste zuerst. Geladen wird pro
   Short nur der Abschnitt in 1080p, nie der ganze Stream.
   Twitch loescht VODs von Nicht-Partnern nach 7 Tagen; was aus der Liste
   verschwindet, fliegt auch aus vods.json.

Pro Short: Facecam finden (detect_cam.py), Layout + Untertitel in EINEM
ffmpeg-Durchlauf, Zitat-Titel (titles.py), Upload mit Kanal-Sperre (upload.py).
posted.json wird nach jedem Erfolg sofort gespeichert (keine Doppelten).

Aufruf:  python daily_run.py                    (echter Lauf mit Upload)
         python daily_run.py --dry-run          (alles ausser Upload, Ergebnis in work/)
         python daily_run.py --dry-run --login X --test-minutes 60
             (Test mit fremdem Kanal: nur neuestes VOD, nur die ersten 60 Minuten)
"""

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import detect_cam
import find_highlights
import subtitles
import titles

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
POSTED = HERE / "posted.json"
VODS = HERE / "vods.json"
WORK = HERE / "work"
FONTS = HERE / "fonts"
W, H = 1080, 1920
YTDLP = [sys.executable, "-m", "yt_dlp"]
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ytdlp_list(url, fields, end=15):
    out = subprocess.run(YTDLP + ["--flat-playlist", "--playlist-end", str(end), "--print",
                                  "\t".join(f"%({f})s" for f in fields), url],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env=ENV).stdout or ""
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == len(fields) and parts[0].startswith("http"):
            rows.append(dict(zip(fields, parts)))
    return rows


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def list_clips(login):
    """Echte Zuschauer-Clips der letzten 7 Tage."""
    rows = ytdlp_list(f"https://www.twitch.tv/{login}/clips?filter=clips&range=7d",
                      ["url", "view_count", "duration", "title"])
    return [{"id": r["url"], "kind": "clip", "url": r["url"], "views": int(num(r["view_count"])),
             "duration": num(r["duration"]), "title": r["title"].strip(), "score": 1e9}
            for r in rows
            if num(r["view_count"]) >= CFG["min_clip_views"] and num(r["duration"]) >= CFG["min_duration"]]


def list_vods(login):
    """Gespeicherte Streams, neueste zuerst. 'id' ist bei Twitch-VODs 'v123...'."""
    return ytdlp_list(f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time",
                      ["url", "id", "title"], end=10)


def window_features(audio_file):
    """Lautstaerke und Hochfrequenz je 1-Sekunden-Fenster, stueckweise aus ffmpeg
    gelesen. Der ganze Ton im Speicher waere bei langen Streams zu viel
    (Test 2026-11-18: Marathon-Streams bis 48 h = ueber 10 GB)."""
    import numpy as np
    win = int(find_highlights.WINDOW_SEC * find_highlights.SR)
    p = subprocess.Popen(["ffmpeg", "-i", str(audio_file), "-vn", "-ac", "1",
                          "-ar", str(find_highlights.SR), "-f", "s16le", "-loglevel", "error", "-"],
                         stdout=subprocess.PIPE)
    loud, hf = [], []
    while True:
        raw = p.stdout.read(win * 2)
        if len(raw) < win * 2:
            break
        chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        loud.append(find_highlights.rms(chunk))
        hf.append(find_highlights.high_freq_energy(chunk))
    p.wait()
    if p.returncode:
        raise RuntimeError(f"ffmpeg Tonspur-Fehler {p.returncode}")
    return np.array(loud), np.array(hf)


def analyze_vod(vod, test_minutes=None):
    """Nur Tonspur laden, Highlights suchen, Stellen zurueckgeben."""
    audio_file = WORK / f"{vod['id']}_audio.m4a"
    cmd = YTDLP + ["-q", "--no-progress", "-f", "bestaudio/worst", "-o", str(audio_file),
                   "--force-overwrites"]
    if test_minutes:
        cmd += ["--download-sections", f"*0-{test_minutes * 60}"]
    subprocess.run(cmd + [vod["url"]], check=True, env=ENV)
    try:
        loud, hf = window_features(audio_file)
    finally:
        audio_file.unlink(missing_ok=True)
    import numpy as np
    duration = len(loud) * find_highlights.WINDOW_SEC
    n = find_highlights.BASELINE_WINDOWS
    baseline = np.convolve(loud, np.ones(n) / n, mode="same")
    z = find_highlights.zscore
    score = 0.6 * z(loud / (baseline + 1e-6)) + 0.4 * z(hf)
    moments = find_highlights.pick_top_moments(score, duration, n=CFG["highlights_per_vod"],
                                               min_gap_sec=CFG["min_gap_sec"])
    segs = []
    for t in moments:
        start = max(0.0, t - find_highlights.PRE_ROLL)
        end = min(duration, start + CFG["clip_len"])
        segs.append({"start": round(start, 1), "end": round(end, 1),
                     "score": round(float(score[int(t / find_highlights.WINDOW_SEC)]), 3)})
    print(f"  VOD {vod['id']} ({duration/60:.0f} Min.): {len(segs)} Stellen gefunden", flush=True)
    return {"title": vod["title"], "url": vod["url"], "duration": round(duration),
            "analyzed": datetime.now(timezone.utc).isoformat(timespec="seconds"), "segments": segs}


def is_live(login):
    """True, wenn der Kanal gerade sendet. Bei Fehler/offline False."""
    out = subprocess.run(YTDLP + ["--skip-download", "--print", "%(is_live)s",
                                  f"https://www.twitch.tv/{login}"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env=ENV).stdout or ""
    return out.strip().splitlines()[-1:] == ["True"]


def refresh_vods(login, state, test_minutes=None):
    """Neue VODs analysieren, verschwundene (von Twitch geloescht) entfernen."""
    live = list_vods(login)
    if test_minutes:
        live = live[:1]
    ids = {v["id"] for v in live}
    for gone in [k for k in state if k not in ids]:
        print(f"  VOD {gone} gibt es nicht mehr, entfernt", flush=True)
        del state[gone]
    # Laeuft der Stream noch, ist das neueste VOD unfertig. Einmal analysiert
    # gilt es als erledigt, der Rest des Streams waere verloren (19.11. gefunden,
    # Boss' 12-h-Stream lief zum Wecker 18:45 noch). Also erst nach Stream-Ende.
    if live and not test_minutes and is_live(login) and live[0]["id"] not in state:
        print(f"  {login} ist live, VOD {live[0]['id']} wartet bis nach dem Stream", flush=True)
        live = live[1:]
    for vod in live:
        if vod["id"] in state:
            continue
        try:
            state[vod["id"]] = analyze_vod(vod, test_minutes)
            if login == CFG["twitch_login"]:  # Testlaeufe mit fremdem Kanal nie speichern
                save(VODS, state)
        except Exception as e:
            print(f"  VOD {vod['id']} nicht analysierbar: {e}", flush=True)
            gh_error(f"VOD {vod['id']} Analyse: {type(e).__name__}: {e}")
    return state


def pick(login, posted, state, want):
    done = {p["id"] for p in posted}
    pool = [c for c in list_clips(login) if c["id"] not in done]
    for vid, v in state.items():
        for s in v["segments"]:
            sid = f"vod:{vid}:{int(s['start'])}"
            if sid not in done:
                pool.append({"id": sid, "kind": "vod", "url": v["url"], "title": v["title"],
                             "start": s["start"], "end": s["end"], "score": s["score"], "views": 0})
    # echte Clips (score 1e9) zuerst, dann die spannendsten VOD-Stellen
    pool.sort(key=lambda c: (-c["score"], -c["views"]))
    return pool[:want]


def download(c, src):
    cmd = YTDLP + ["-q", "--no-progress", "-f", "best[height<=1080]/best", "-o", str(src),
                   "--force-overwrites"]
    if c["kind"] == "vod":
        cmd += ["--download-sections", f"*{c['start']}-{c['end']}", "--force-keyframes-at-cuts"]
    subprocess.run(cmd + [c["url"]], check=True, env=ENV)


def layout_filter(src_w, src_h, cam):
    """Identisch mit Deutschestreamerclips (von Boss abgenommen): Facecam oben,
    ganzes Spielbild darunter, Hintergrund unscharf. Gibt (filter, untertitel_y)."""
    game_h = 608
    game_src = ""
    if cam.get("game_crop"):
        # BedoZg (Boss, 20.11.): unten nur das reine Spiel links der Kamera,
        # ohne doppelte Kamera und Chat. Volle Hoehe, dadurch groesser.
        gx, gy, gw, gh = cam["game_crop"]
        game_h = min(900, int(W * gh / gw) // 2 * 2)
        game_src = f"crop={gw}:{gh}:{gx}:{gy},"
    bg = (f"[a]scale=-2:{H},crop={W}:{H},boxblur=20:2,eq=brightness=-0.15[bg];"
          f"[c]{game_src}scale={W}:{game_h}:flags=lanczos[game];")
    if cam["mode"] == "cam":
        x, y, bw, bh = cam["box"]
        cam_h = min(760, int(W * bh / bw) // 2 * 2)
        crop_h = min(bh, int(bw * cam_h / W) // 2 * 2)
        fy = cam["face"][1] + cam["face"][3] / 2
        cy = int(min(max(y, fy - crop_h / 2), y + bh - crop_h)) // 2 * 2
        cam_y = 120
        game_y = cam_y + cam_h + 24
        f = ("[0:v]split=3[a][b][c];" + bg +
             f"[b]crop={bw}:{crop_h}:{x}:{cy},scale={W}:{cam_h}:flags=lanczos[cam];"
             f"[bg][cam]overlay=0:{cam_y}[t1];[t1][game]overlay=0:{game_y}")
        sub_y = game_y + game_h + (H - game_y - game_h) // 2
    elif "face" in cam:
        lo, hi = cam.get("span", [0, src_w])
        cw = int(min(src_w, max(src_h, hi - lo))) // 2 * 2
        x = int(min(max(0, (lo + hi) / 2 - cw / 2), src_w - cw)) // 2 * 2
        out_h = int(W * src_h / cw) // 2 * 2
        sq_y = max(160, (H - out_h) // 2 - 220)
        f = ("[0:v]split=2[a][c];"
             f"[a]scale=-2:{H},crop={W}:{H},boxblur=20:2,eq=brightness=-0.15[bg];"
             f"[c]crop={cw}:{src_h}:{x}:0,scale={W}:{out_h}:flags=lanczos[sq];"
             f"[bg][sq]overlay=0:{sq_y}")
        sub_y = sq_y + out_h + 220
    else:
        game_y = 560
        f = ("[0:v]split=2[a][c];" + bg + f"[bg][game]overlay=0:{game_y}")
        sub_y = game_y + game_h + 250
    return f, min(sub_y, H - 140)


def render(src, out, cam, words):
    w, h, _ = detect_cam.probe(src)
    f, sub_y = layout_filter(w, h, cam)
    ass = out.with_suffix(".ass")
    ass.write_text(subtitles.build_ass(words, (W // 2, sub_y)), encoding="utf-8")
    fonts_rel = Path("..") / FONTS.name if out.parent.parent == HERE else FONTS
    f += f",ass={ass.name}:fontsdir={fonts_rel.as_posix()},format=yuv420p[v]"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src.name,
                    "-filter_complex", f, "-map", "[v]", "-map", "0:a:0?",
                    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-r", "30",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name],
                   check=True, cwd=out.parent)


def main():
    dry = "--dry-run" in sys.argv
    login = CFG["twitch_login"]
    if "--login" in sys.argv:
        if not dry:
            raise SystemExit("--login nur zusammen mit --dry-run (Test mit fremdem Kanal)")
        login = sys.argv[sys.argv.index("--login") + 1]
    name = CFG["display_name"]
    WORK.mkdir(exist_ok=True)
    posted = load(POSTED, [])
    vods_file_state = load(VODS, {}) if login == CFG["twitch_login"] else {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    already = sum(1 for p in posted if p.get("date") == today)
    want = CFG["clips_per_day"] if dry else CFG["clips_per_day"] - already
    if want <= 0:
        print(f"Heute ({today}) schon {already} Shorts hochgeladen, nichts zu tun.")
        gh_notice(f"Heute schon {already} Shorts hochgeladen, nichts zu tun")
        return

    print(f"Twitch {login}: VODs pruefen...", flush=True)
    test_minutes = None
    if "--test-minutes" in sys.argv:
        if not dry:
            raise SystemExit("--test-minutes nur zusammen mit --dry-run")
        test_minutes = int(sys.argv[sys.argv.index("--test-minutes") + 1])
    state = refresh_vods(login, vods_file_state, test_minutes)
    if login == CFG["twitch_login"]:
        save(VODS, state)
    clips = pick(login, posted, state, want if "--only-vods" not in sys.argv else 99)
    if "--only-vods" in sys.argv:
        # Test: nur VOD-Highlights schneiden (sonst gewinnen echte Clips immer)
        if not dry:
            raise SystemExit("--only-vods nur zusammen mit --dry-run")
        clips = [c for c in clips if c["kind"] == "vod"][:want]
    print(f"{len(clips)} ausgewaehlt:", flush=True)
    for c in clips:
        where = f"{c['start']:.0f}s" if c["kind"] == "vod" else f"{c['views']} Aufrufe"
        print(f"  {c['kind']:4} {where:>12}  {c['title']}", flush=True)
    if not clips:
        print("Nichts Neues (keine Clips, keine VODs). Ende.")
        gh_notice("Nichts Neues: keine Clips und keine gespeicherten Streams")
        return

    from faster_whisper import WhisperModel
    model = WhisperModel("medium", device="cpu", compute_type="int8")
    yt = None
    if not dry:
        import upload
        try:
            yt = upload.client()
            upload.assert_channel(yt)
        except BaseException as e:
            gh_error(f"YouTube-Anmeldung: {type(e).__name__}: {e}")
            raise

    ok = 0
    for i, c in enumerate(clips):
        slug = c["id"].rstrip("/").rsplit("/", 1)[-1].replace(":", "_")[:60]
        src = WORK / f"{slug}_src.mp4"
        out = WORK / f"{slug}.mp4"
        try:
            download(c, src)
            cam = detect_cam.detect(src, fixed_box=CFG.get("cam_box"))
            print(f"[{i+1}] {c['id']}: Facecam {cam['mode']} {cam.get('box', '')}", flush=True)
            audio = subtitles.load_audio(src)
            words = subtitles.transcribe(model, src, audio)
            yt_title, how = titles.make_title(words, audio, c["title"], name)
            print(f"    Titel ({how}): {yt_title}", flush=True)
            render(src, out, cam, words)
            if dry:
                print(f"    fertig (Testlauf): {out}", flush=True)
                continue
            vid = upload.upload(yt, out, login, name, CFG["privacy"], yt_title)
            gh_notice(f"Titel ({how}): {yt_title}")
            posted.append({"id": c["id"], "kind": c["kind"], "title": c["title"],
                           "yt_title": yt_title, "title_method": how,
                           "youtube_id": vid, "date": today})
            save(POSTED, posted)
            ok += 1
            print(f"    hochgeladen: https://youtube.com/shorts/{vid}", flush=True)
            send_tiktok(out, posted, yt_title)
            src.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
        except Exception as e:
            print(f"[{i+1}] FEHLER bei {c['id']}, naechster Lauf versucht es erneut:", flush=True)
            traceback.print_exc()
            gh_error(f"{c['id']}: {type(e).__name__}: {e}")
    print(f"Fertig: {ok} hochgeladen." if not dry else "Testlauf fertig.")
    if not dry:
        gh_notice(f"{ok} von {len(clips)} Shorts hochgeladen")
        if ok == 0:
            sys.exit(1)


def send_tiktok(out, posted, yt_title):
    """Zusaetzlich als Entwurf ins TikTok-Postfach von Boss (Boss, 19.11.: Weg A).
    Ohne Audit darf die App nicht direkt oeffentlich posten; Boss tippt den
    Titel in der App und postet selbst. Ein TikTok-Fehler stoppt YouTube nie."""
    import tiktok_upload
    if not tiktok_upload.TOKEN.exists():
        print("    TikTok: kein tiktok_token.json, uebersprungen", flush=True)
        return
    try:
        posted[-1]["tiktok_publish_id"] = tiktok_upload.upload_draft(out)
        save(POSTED, posted)
        print(f"    TikTok-Entwurf gesendet, Titel zum Abtippen: {yt_title}", flush=True)
        gh_notice(f"TikTok-Entwurf, Titel: {yt_title}")
    except Exception as e:
        print(f"    TikTok FEHLER: {e}", flush=True)
        gh_error(f"TikTok-Entwurf: {type(e).__name__}: {e}")


def gh_error(msg):
    if os.environ.get("GITHUB_ACTIONS"):
        print("::error::" + " ".join(str(msg).split())[:900], flush=True)


def gh_notice(msg):
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice::" + " ".join(str(msg).split())[:900], flush=True)


if __name__ == "__main__":
    main()
