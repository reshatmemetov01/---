"""ElevenLabs: клон своего голоса по образцу и озвучка им (ключ ELEVENLABS_API_KEY)."""
import json, os, urllib.error, urllib.request, uuid

API = "https://api.elevenlabs.io/v1"


def _key():
    k = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not k:
        raise RuntimeError("Нет ключа ELEVENLABS_API_KEY (впиши его в /opt/pdd3d/bot3d.env)")
    return k


def _req(url, data, headers, timeout=180):
    req = urllib.request.Request(url, data=data, headers={"xi-api-key": _key(), **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ElevenLabs ответил {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")


def clone(name, audio_path):
    """Создаёт голос по образцу (10 с - 2 мин чистой речи) -> voice_id."""
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n{name}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"sample.mp3\"\r\n"
            f"Content-Type: audio/mpeg\r\n\r\n").encode() + open(audio_path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    r = _req(f"{API}/voices/add", body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    return json.loads(r)["voice_id"]


def synth(text, voice_id, speed=1.0):
    body = json.dumps({"text": text, "model_id": os.environ.get("ELEVEN_MODEL", "eleven_multilingual_v2"),
                       "voice_settings": {"stability": 0.45, "similarity_boost": 0.85, "style": 0.2,
                                          "speed": max(0.7, min(1.2, float(speed)))}}).encode()
    return _req(f"{API}/text-to-speech/{voice_id}?output_format=mp3_44100_128", body,
                {"Content-Type": "application/json", "Accept": "audio/mpeg"})
