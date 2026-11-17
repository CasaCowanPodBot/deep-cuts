"""Quick check that two-host Gemini TTS works. Writes nothing to the feed."""
import subprocess, sys
sys.path.insert(0, "scripts")
import build, os
hosts = dict(build.CONFIG["hosts"])
a, b = list(hosts)
text = f"{a}: Quick sound check for the two-host format.\n{b}: Loud and clear. Two voices, no stage directions."
pcm = build.tts(text, os.environ["GEMINI_API_KEY"], hosts)
open("/tmp/smoke.bin", "wb").write(pcm)
fmt = [] if pcm[:4] == b"RIFF" else ["-f", "s16le", "-ar", "24000", "-ac", "1"]
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *fmt, "-i", "/tmp/smoke.bin", "/tmp/smoke.mp3"], check=True)
print("SMOKE OK", build.duration("/tmp/smoke.mp3"), "seconds")
