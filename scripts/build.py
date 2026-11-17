"""Deep Cuts builder.

1. For every episodes/*.txt without a matching audio/<slug>.mp3, synthesize
   narration with the Gemini TTS API and save the MP3.
2. Regenerate site/feed.xml (Apple Podcasts compatible RSS) and site/index.html.

Episode file format (episodes/YYYY-MM-DD-slug.txt):
    title: Episode title
    description: One or two sentence summary.
    format: conversation        (optional; "conversation" (default) or "narrative")
    hosts: Jack=Charon, Nora=Kore   (optional; defaults to podcast.json "hosts")
    ---
    Script paragraphs separated by blank lines.
    Conversation: every paragraph starts with a host name and a colon, e.g. "Jack: ...".
    Narrative: plain paragraphs, read by the single narrator voice.
    A line containing only [PAUSE] marks a section break.
"""
import base64, html, json, os, re, shutil, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EP_DIR, AUDIO_DIR, SITE = ROOT / "episodes", ROOT / "audio", ROOT / "site"
CONFIG = json.loads((ROOT / "podcast.json").read_text())
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
SR = 24000


def parse_episode(path):
    raw = path.read_text(encoding="utf-8")
    header, _, body = raw.partition("\n---\n")
    meta = {}
    for line in header.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})-(.+)", path.stem)
    meta["date"] = m.group(1) if m else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta["slug"] = path.stem
    meta["body"] = body.strip()
    return meta


def chunks(body, limit=2400, sep="\n\n"):
    """Split into sections at [PAUSE], then into <= limit char pieces at paragraph breaks."""
    out, cur = [], ""
    for section in re.split(r"\n\s*\[PAUSE\]\s*\n", body):
        paras = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip() and p.strip() != "[PAUSE]"]
        for p in paras:
            if cur and len(cur) + len(p) + 2 > limit:
                out.append((cur, False)); cur = ""
            cur = f"{cur}{sep}{p}" if cur else p
        # section break: close the chunk here (longer pause) if it is already reasonably long
        if cur and len(cur) > limit * 0.6:
            out.append((cur, True)); cur = ""
    if cur:
        out.append((cur, True))
    return out


def tts(text, key, hosts=None):
    """hosts: dict of speaker name -> Gemini voice for two-host mode; None for single narrator.
    Only the script text is sent: no style instructions, so nothing extra gets read aloud."""
    if hosts:
        speech = {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
            {"speaker": name, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
            for name, voice in hosts.items()]}}
    else:
        speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": CONFIG.get("voice", "Charon")}}}
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech},
    }
    last_err = None
    for model in CONFIG["tts_models"]:
        for attempt in range(3):  # free-tier daily quotas don't recover quickly; fall back to the next model
            req = urllib.request.Request(
                API.format(model=model), data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = json.load(r)
                part = data["candidates"][0]["content"]["parts"][0]["inlineData"]
                print(f"    ok via {model} ({part.get('mimeType')})")
                return base64.b64decode(part["data"])
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:500]
                last_err = f"{model}: HTTP {e.code} {msg}"
                if e.code in (429, 500, 503):
                    wait = 30 * (attempt + 1)
                    print(f"    {e.code} from {model}, retrying in {wait}s"); time.sleep(wait); continue
                print(f"    {last_err}"); break  # e.g. 404 unknown model -> try next model
            except (KeyError, IndexError) as e:
                last_err = f"{model}: unexpected response {str(data)[:300]}"; print("    " + last_err); break
    raise RuntimeError(f"TTS failed. Last error: {last_err}")


def episode_hosts(ep):
    """Return {name: voice} for conversation episodes, or None for narrative ones."""
    if ep.get("format", CONFIG.get("default_format", "conversation")).lower() != "conversation":
        return None
    if ep.get("hosts"):
        hosts = dict(h.strip().split("=", 1) for h in ep["hosts"].split(","))
        hosts = {k.strip(): v.strip() for k, v in hosts.items()}
    else:
        hosts = dict(CONFIG["hosts"])
    if len(hosts) != 2:
        sys.exit(f"{ep['slug']}: conversation mode needs exactly two hosts, got {list(hosts)}")
    for para in re.split(r"\n\s*\n", ep["body"]):
        para = para.strip()
        if not para or para == "[PAUSE]":
            continue
        name = para.split(":", 1)[0].strip()
        if ":" not in para or name not in hosts:
            sys.exit(f"{ep['slug']}: conversation paragraph must start with one of {list(hosts)} + ':' -> {para[:60]!r}")
    return hosts


def render(ep, key):
    tmp = ROOT / "tmp" / ep["slug"]; tmp.mkdir(parents=True, exist_ok=True)
    def silence(sec, name):
        p = tmp / name
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"anullsrc=r={SR}:cl=mono", "-t", str(sec), "-c:a", "pcm_s16le", str(p)], check=True)
        return p
    short, long_ = silence(0.5, "short.wav"), silence(1.3, "long.wav")
    pieces = []
    if (ROOT / "assets" / "chime.wav").exists():
        pieces += [ROOT / "assets" / "chime.wav", silence(0.4, "lead.wav")]
    hosts = episode_hosts(ep)
    parts = chunks(ep["body"], sep="\n" if hosts else "\n\n")
    print(f"  format: {'conversation ' + str(hosts) if hosts else 'narrative'}")
    for i, (text, section_end) in enumerate(parts):
        print(f"  chunk {i+1}/{len(parts)} ({len(text)} chars)")
        pcm = tts(text, key, hosts)
        raw, wav = tmp / f"c{i}.pcm", tmp / f"c{i}.wav"
        raw.write_bytes(pcm)
        # Newer models return a WAV container; older ones return raw 24 kHz 16-bit PCM.
        fmt = [] if pcm[:4] == b"RIFF" else ["-f", "s16le", "-ar", str(SR), "-ac", "1"]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *fmt, "-i", str(raw),
                        "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True)
        pieces += [wav, long_ if section_end else short]
        time.sleep(CONFIG.get("seconds_between_requests", 8))
    if (ROOT / "assets" / "chime.wav").exists():
        pieces.append(ROOT / "assets" / "chime.wav")
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in pieces))
    AUDIO_DIR.mkdir(exist_ok=True)
    out = AUDIO_DIR / f"{ep['slug']}.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "1", "-b:a", "96k", str(out)],
                   check=True)
    shutil.rmtree(tmp)
    print(f"  wrote {out.name}")


def duration(mp3):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                       capture_output=True, text=True)
    return int(float(r.stdout.strip() or 0))


def build_site(episodes):
    base = CONFIG["base_url"].rstrip("/")
    SITE.mkdir(exist_ok=True)
    (SITE / "audio").mkdir(exist_ok=True)
    for f in ("cover.jpg",):
        if (ROOT / "assets" / f).exists():
            shutil.copy(ROOT / "assets" / f, SITE / f)
    e = lambda s: html.escape(s or "", quote=True)
    items = []
    for ep in sorted(episodes, key=lambda x: x["slug"], reverse=True):
        mp3 = AUDIO_DIR / f"{ep['slug']}.mp3"
        if not mp3.exists():
            continue
        shutil.copy(mp3, SITE / "audio" / mp3.name)
        secs = duration(mp3)
        pub = format_datetime(datetime.strptime(ep["date"], "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc))
        url = f"{base}/audio/{mp3.name}"
        items.append(f"""    <item>
      <title>{e(ep.get('title', ep['slug']))}</title>
      <description>{e(ep.get('description', ''))}</description>
      <itunes:summary>{e(ep.get('description', ''))}</itunes:summary>
      <enclosure url="{e(url)}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <guid isPermaLink="false">{e(ep['slug'])}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:duration>{secs}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    c = CONFIG
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{e(c['title'])}</title>
    <link>{e(base)}/</link>
    <atom:link href="{e(base)}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{e(c['description'])}</description>
    <language>en-us</language>
    <itunes:author>{e(c['author'])}</itunes:author>
    <itunes:image href="{e(base)}/cover.jpg"/>
    <image><url>{e(base)}/cover.jpg</url><title>{e(c['title'])}</title><link>{e(base)}/</link></image>
    <itunes:category text="Society &amp; Culture"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (SITE / "feed.xml").write_text(feed, encoding="utf-8")
    (SITE / "index.html").write_text(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{e(c['title'])}</title><body style="font-family:system-ui;max-width:640px;margin:2rem auto;padding:0 1rem">
<h1>{e(c['title'])}</h1><p>{e(c['description'])}</p>
<p>Subscribe in Apple Podcasts: Library &rarr; &bull;&bull;&bull; &rarr; Follow a Show by URL &rarr; <code>{e(base)}/feed.xml</code></p>
</body>""", encoding="utf-8")
    (SITE / ".nojekyll").write_text("")
    print(f"feed: {len(items)} episode(s)")


def main():
    episodes = [parse_episode(p) for p in sorted(EP_DIR.glob("*.txt"))]
    key = os.environ.get("GEMINI_API_KEY", "")
    for ep in episodes:
        if not (AUDIO_DIR / f"{ep['slug']}.mp3").exists():
            if not key:
                sys.exit("GEMINI_API_KEY is not set")
            print(f"Rendering {ep['slug']}")
            render(ep, key)
    build_site(episodes)


if __name__ == "__main__":
    main()
