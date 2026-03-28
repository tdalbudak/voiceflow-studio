import os
import re
import uuid
import asyncio
import httpx
import shutil
import subprocess
import json
import logging
from fastapi import FastAPI, UploadFile, Form, BackgroundTasks, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from deepgram import (
    DeepgramClient,
    PrerecordedOptions,
    FileSource,
)

load_dotenv()
DEEPGRAM_API_KEY   = os.getenv("DEEPGRAM_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
DEEPL_API_KEY      = os.getenv("DEEPL_API_KEY")

# ── Loglama ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voiceflow")

# ── Dosya limitleri ──
MAX_DOSYA_MB   = int(os.getenv("MAX_DOSYA_MB", "500"))    # 500MB varsayılan
MAX_SURE_DAKIKA = int(os.getenv("MAX_SURE_DAKIKA", "30")) # 30 dakika

app = FastAPI(title="VoiceFlow Studio API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "ciktilar")
TEMP_DIR   = os.getenv("TEMP_DIR",   "gecici")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR,   exist_ok=True)
app.mount("/ciktilar", StaticFiles(directory=OUTPUT_DIR), name="ciktilar")

islem_durumlari: dict = {}

DEEPL_DILLER = {
    "EN": "EN-US", "DE": "DE", "FR": "FR", "ES": "ES",
    "IT": "IT",    "PT": "PT-BR", "NL": "NL", "PL": "PL",
    "RU": "RU",    "JA": "JA",    "ZH": "ZH", "TR": "TR",
    "AR": "AR",    "KO": "KO",
}

# ── Index.html serve et (production'da ayrı sunucu yoksa) ──
@app.get("/", response_class=HTMLResponse)
async def root():
    landing = "landing.html" if os.path.exists("landing.html") else "index.html"
    if os.path.exists(landing):
        with open(landing, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>VoiceFlow Studio API</h1><p>index.html bulunamadı.</p>")

# ── Sağlık kontrolü ──
@app.get("/health")
async def health():
    uyarilar = []
    if not DEEPGRAM_API_KEY:   uyarilar.append("DEEPGRAM_API_KEY eksik")
    if not ELEVENLABS_API_KEY: uyarilar.append("ELEVENLABS_API_KEY eksik")
    if not DEEPL_API_KEY:      uyarilar.append("DEEPL_API_KEY eksik — çeviri çalışmaz")
    if not ffmpeg_var_mi():    uyarilar.append("FFmpeg bulunamadı")

    return {
        "status": "ok" if not uyarilar else "degraded",
        "ffmpeg": ffmpeg_var_mi(),
        "deepgram_key": bool(DEEPGRAM_API_KEY),
        "elevenlabs_key": bool(ELEVENLABS_API_KEY),
        "deepl_key": bool(DEEPL_API_KEY),
        "uyarilar": uyarilar,
        "max_dosya_mb": MAX_DOSYA_MB,
        "max_sure_dakika": MAX_SURE_DAKIKA,
        "versiyon": "2.0.0",
    }


@app.on_event("startup")
async def baslangic_kontrolu():
    """Uygulama başlarken key kontrolü yap, eksikleri logla."""
    log.info("=" * 50)
    log.info("VoiceFlow Studio başlatılıyor...")
    if not DEEPGRAM_API_KEY:   log.warning("⚠ DEEPGRAM_API_KEY eksik — deşifre çalışmaz")
    if not ELEVENLABS_API_KEY: log.warning("⚠ ELEVENLABS_API_KEY eksik — TTS çalışmaz")
    if not DEEPL_API_KEY:      log.warning("⚠ DEEPL_API_KEY eksik — çeviri çalışmaz")
    if ffmpeg_var_mi():        log.info("✓ FFmpeg mevcut")
    else:                      log.warning("⚠ FFmpeg bulunamadı")
    log.info("=" * 50)
    # Başlangıçta eski dosyaları temizle
    asyncio.create_task(_periyodik_temizle())


async def _periyodik_temizle():
    """Her 6 saatte bir 24 saatten eski geçici dosyaları sil."""
    while True:
        await asyncio.sleep(6 * 3600)  # 6 saat bekle
        try:
            import time
            sinir = time.time() - 86400  # 24 saat önce
            temizlenen = 0
            for klasor in [TEMP_DIR, OUTPUT_DIR]:
                for dosya in os.listdir(klasor):
                    yol = os.path.join(klasor, dosya)
                    if os.path.isfile(yol) and os.path.getmtime(yol) < sinir:
                        os.unlink(yol)
                        temizlenen += 1
            if temizlenen:
                log.info(f"[Temizlik] {temizlenen} eski dosya silindi")
        except Exception as e:
            log.error(f"[Temizlik Hata] {e}")

# ── Dosya boyutu kontrolü ──
def _dosya_kontrol(dosya_yolu: str) -> tuple[bool, str]:
    """Dosya boyutu ve süre limitlerini kontrol eder."""
    boyut_mb = os.path.getsize(dosya_yolu) / (1024 * 1024)
    if boyut_mb > MAX_DOSYA_MB:
        return False, f"Dosya çok büyük: {boyut_mb:.0f}MB (limit: {MAX_DOSYA_MB}MB)"

    # Video süresini kontrol et
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", dosya_yolu],
            capture_output=True, text=True, timeout=15
        )
        sure = float(r.stdout.strip())
        if sure > MAX_SURE_DAKIKA * 60:
            return False, f"Video çok uzun: {sure/60:.1f} dakika (limit: {MAX_SURE_DAKIKA} dakika)"
    except Exception:
        pass  # Süre ölçülemezse geç

    return True, ""

# ── Kullanıcı dostu hata mesajları ──
HATA_MESAJLARI = {
    "deepgram_401": "Deepgram API key geçersiz. .env dosyasındaki DEEPGRAM_API_KEY'i kontrol edin.",
    "deepgram_400": "Deepgram bu dili desteklemiyor. Kaynak dil seçimini değiştirin.",
    "deepgram_null": "Deepgram yanıt vermedi. İnternet bağlantınızı kontrol edin.",
    "elevenlabs_401": "ElevenLabs API key geçersiz. .env dosyasındaki ELEVENLABS_API_KEY'i kontrol edin.",
    "elevenlabs_quota": "ElevenLabs karakter kotanız doldu. Planınızı yükseltin veya ay sonunu bekleyin.",
    "elevenlabs_voice": "Seçilen ses bu hesapta mevcut değil. Farklı bir ses seçin.",
    "deepl_401": "DeepL API key geçersiz. .env dosyasındaki DEEPL_API_KEY'i kontrol edin.",
    "ffmpeg_yok": "FFmpeg yüklü değil. https://ffmpeg.org/download.html adresinden indirin.",
    "dosya_buyuk": "Dosya boyutu limitini aştı.",
    "video_uzun": "Video süresi limitini aştı.",
    "segment_yok": "Videoda konuşma tespit edilemedi. Farklı bir kaynak dil seçin.",
}

def _hata_mesaji(kod: str) -> str:
    return HATA_MESAJLARI.get(kod, "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.")

# ============================================================
# MOTOR 1 — DEEPGRAM
# ============================================================
async def deepgram_desifre_et(audio_path: str, kaynak_dil: str = "tr"):
    try:
        deepgram = DeepgramClient(DEEPGRAM_API_KEY)
        with open(audio_path, "rb") as f:
            buffer_data = f.read()
        payload: FileSource = {"buffer": buffer_data}

        # Auto algılama: detect_language=True ile dil tespiti yap
        otomatik = kaynak_dil in ["auto", None, ""]
        options = PrerecordedOptions(
            model="nova-2",
            smart_format=True,
            punctuate=True,
            diarize=True,
            utterances=True,
            **({"detect_language": True} if otomatik else {"language": kaynak_dil}),
        )
        response = await asyncio.to_thread(
            deepgram.listen.rest.v("1").transcribe_file, payload, options
        )

        # Algılanan dili logla
        if otomatik:
            try:
                detected = response.results.channels[0].detected_language
                confidence = response.results.channels[0].language_confidence
                log.info(f"[Deepgram] Otomatik algılanan dil: {detected} (güven: {confidence:.2f})")
            except Exception:
                pass

        return response
    except Exception as e:
        err = str(e).lower()
        if "401" in err or "invalid credentials" in err:
            log.error(f"[Deepgram] API key geçersiz: {e}")
            raise ValueError("deepgram_401")
        elif "400" in err or "no such model" in err:
            log.error(f"[Deepgram] Dil/model hatası: {e}")
            raise ValueError("deepgram_400")
        else:
            log.error(f"[Deepgram] {e}")
            raise ValueError("deepgram_null")

# ============================================================
# MOTOR 2 — DEEPL
# ============================================================
def _deepl_base_url() -> str:
    if DEEPL_API_KEY and DEEPL_API_KEY.endswith(":fx"):
        return "https://api-free.deepl.com"
    return "https://api.deepl.com"

async def _deepl_chunk_cevir(client: httpx.AsyncClient, satirlar: list, hedef_dil: str) -> list:
    deepl_hedef = DEEPL_DILLER.get(hedef_dil.upper(), hedef_dil.upper())
    try:
        r = await client.post(
            f"{_deepl_base_url()}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            json={"text": satirlar, "target_lang": deepl_hedef, "preserve_formatting": True},
            timeout=30.0,
        )
        r.raise_for_status()
        return [t["text"] for t in r.json()["translations"]]
    except Exception as e:
        print(f"[DeepL Chunk Hata] {e}")
        return satirlar

async def deepl_paralel_cevir_listesi(metin_listesi: list, hedef_dil: str) -> list:
    if not metin_listesi:
        return []
    # 100'den az segment → tek API çağrısı (en hızlı)
    if len(metin_listesi) <= 100:
        async with httpx.AsyncClient() as client:
            sonuc = await _deepl_chunk_cevir(client, metin_listesi, hedef_dil)
        return sonuc
    # 100+ segment → 100'lük batch'ler halinde paralel
    CHUNK = 100
    chunks = [metin_listesi[i:i+CHUNK] for i in range(0, len(metin_listesi), CHUNK)]
    async with httpx.AsyncClient() as client:
        sonuclar = await asyncio.gather(*[_deepl_chunk_cevir(client, c, hedef_dil) for c in chunks])
    return [m for chunk in sonuclar for m in chunk]

async def srt_paralel_cevir(srt_icerik: str, hedef_dil: str) -> str:
    bloklar = []
    for blok in srt_icerik.strip().split("\n\n"):
        s = blok.strip().split("\n")
        if len(s) >= 3:
            bloklar.append({"num": s[0], "zaman": s[1], "metin": "\n".join(s[2:])})
    if not bloklar:
        return srt_icerik
    cevrilmis = await deepl_paralel_cevir_listesi([b["metin"] for b in bloklar], hedef_dil)
    satirlar = []
    for i, blok in enumerate(bloklar):
        satirlar.extend([blok["num"], blok["zaman"], cevrilmis[i] if i < len(cevrilmis) else blok["metin"], ""])
    return "\n".join(satirlar)

# ============================================================
import hashlib as _hashlib

# ── TTS Cache ──────────────────────────────────────────────────
TTS_CACHE_DIR = os.path.join(os.path.dirname(__file__), "gecici", "tts_cache")
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

def _tts_cache_key(metin: str, ses_id: str) -> str:
    """Metin + ses_id kombinasyonundan MD5 hash üretir."""
    raw = f"{ses_id}::{metin.strip()}"
    return _hashlib.md5(raw.encode("utf-8")).hexdigest()

def _tts_cache_get(metin: str, ses_id: str) -> str | None:
    """Cache'te varsa dosya yolunu, yoksa None döner."""
    key  = _tts_cache_key(metin, ses_id)
    path = os.path.join(TTS_CACHE_DIR, f"tts_{key}.mp3")
    return path if os.path.exists(path) and os.path.getsize(path) > 100 else None

def _tts_cache_set(metin: str, ses_id: str, kaynak: str) -> str:
    """Üretilen sesi cache'e kopyalar, cache yolunu döner."""
    key  = _tts_cache_key(metin, ses_id)
    path = os.path.join(TTS_CACHE_DIR, f"tts_{key}.mp3")
    try:
        shutil.copy2(kaynak, path)
    except Exception as e:
        log.warning(f"[TTS Cache] Kayıt hatası: {e}")
    return path

# ── Cache boyut limiti (500MB) ─────────────────────────────────
def _tts_cache_temizle(max_mb: int = 500):
    """En eski cache dosyalarını silerek limiti korur."""
    try:
        dosyalar = sorted(
            [os.path.join(TTS_CACHE_DIR, f) for f in os.listdir(TTS_CACHE_DIR) if f.endswith(".mp3")],
            key=os.path.getmtime
        )
        toplam = sum(os.path.getsize(f) for f in dosyalar)
        while toplam > max_mb * 1024 * 1024 and dosyalar:
            sil = dosyalar.pop(0)
            toplam -= os.path.getsize(sil)
            os.remove(sil)
            log.info(f"[TTS Cache] Temizlendi: {os.path.basename(sil)}")
    except Exception as e:
        log.warning(f"[TTS Cache] Temizleme hatası: {e}")
async def elevenlabs_ses_uret(metin: str, ses_id: str, output_path: str, retry: int = 2) -> bool:
    if not metin or not metin.strip():
        log.warning("[ElevenLabs] Boş metin gönderildi")
        return False
    if not ses_id or not ses_id.strip():
        log.warning("[ElevenLabs] ses_id boş — Brian kullanılıyor")
        ses_id = "nPczCjzI2devNBz1zQrb"

    # ── Cache kontrolü ──────────────────────────────────────────
    cached = _tts_cache_get(metin, ses_id)
    if cached:
        log.info(f"[TTS Cache] HIT → {os.path.basename(cached)}")
        shutil.copy2(cached, output_path)
        return True

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ses_id}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY,
    }
    data = {
        "text": metin[:5000],
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    for deneme in range(retry + 1):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=data, headers=headers, timeout=60.0)
            if r.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(r.content)
                # Cache'e kaydet
                _tts_cache_set(metin, ses_id, output_path)
                _tts_cache_temizle()
                return True
            # Kota doldu veya API key geçersiz — retry YOK, direkt çık
            elif r.status_code in (401, 403):
                log.error(f"[ElevenLabs] API key geçersiz ({r.status_code}) — işlem iptal")
                return False
            elif r.status_code == 429:
                # Rate limit — kısa bekleme sonra tekrar dene
                bekle = 5 * (deneme + 1)
                log.warning(f"[ElevenLabs] Rate limit → {bekle}s bekleniyor")
                await asyncio.sleep(bekle)
                continue
            elif r.status_code == 422:
                log.error(f"[ElevenLabs] Ses ID geçersiz: {ses_id} — işlem iptal")
                return False
            else:
                hata_detay = r.text[:300]
                log.error(f"[ElevenLabs {r.status_code}] ses_id={ses_id} hata={hata_detay}")
                # Kota mesajı içeriyorsa direkt çık
                if any(k in hata_detay.lower() for k in ["quota", "limit", "insufficient"]):
                    log.error("[ElevenLabs] Kota doldu — işlem iptal")
                    return False
                if deneme < retry:
                    await asyncio.sleep(2)
                    continue
                return False
        except asyncio.TimeoutError:
            log.error(f"[ElevenLabs] Timeout (60s) — deneme {deneme+1}/{retry+1}")
            if deneme < retry:
                await asyncio.sleep(2)
            else:
                return False
        except Exception as e:
            log.error(f"[ElevenLabs İstisna] {e}")
            if deneme < retry:
                await asyncio.sleep(2)
            else:
                return False
    return False

# ============================================================
# MOTOR 4 — FFMPEG ALTYAZI
# ============================================================
def ffmpeg_var_mi() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False

def hex_to_ass_color(hex_color: str) -> str:
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 6:
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        return f"&H00{b}{g}{r}"
    return "&H00FFFFFF"

def ffmpeg_altyazi_gom(video_yolu, srt_yolu, cikti_yolu, font_name, font_size, font_color, is_bold, is_shadow, margin_v) -> bool:
    srt_escaped = srt_yolu.replace("\\", "/").replace(":", "\\:")
    ass_color   = hex_to_ass_color(font_color)
    bold_val    = "-1" if is_bold else "0"
    shadow_val  = "2" if is_shadow else "0"
    font_clean  = font_name.replace("'", "").split(',')[0].strip()
    style_str   = (
        f"FontName={font_clean},FontSize={font_size},PrimaryColour={ass_color},"
        f"OutlineColour=&H00000000,Outline=2,Shadow={shadow_val},Bold={bold_val},Alignment=2,MarginV={margin_v}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-threads", "0",          # tüm CPU çekirdeklerini kullan
        "-i", video_yolu,
        "-vf", f"subtitles='{srt_escaped}':force_style='{style_str}'",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-threads", "0",
        "-c:a", "copy", cikti_yolu,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[FFmpeg Altyazı Hata] {result.stderr[-500:]}")
        return False
    return True

# ============================================================
# MOTOR 5 — DUBLAJ TTS (sessizlik-concat yaklaşımı)
# ============================================================
def ses_sure_olc(dosya: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", dosya],
            capture_output=True, text=True, timeout=15
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _sessizlik_olustur(sure: float, cikis: str) -> bool:
    """Verilen sürede sessiz stereo WAV üretir."""
    sure = max(0.05, round(sure, 4))
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(sure), "-c:a", "pcm_s16le", cikis
    ], capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def _mp3_wav_cevir(giris: str, cikis: str) -> bool:
    """MP3 → stereo 44100Hz WAV."""
    r = subprocess.run([
        "ffmpeg", "-y", "-i", giris,
        "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", cikis
    ], capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def _dublaj_track_olustur(ses_listesi: list, video_sure: float,
                           cikis_wav: str, tmp_dir: str) -> bool:
    """
    Segment seslerinden tek bir tam-uzunluk dublaj WAV track'i oluşturur.
    Yöntem: her segmentin başına sessizlik ekle → concat → tek WAV.
    Bu yöntem adelay filter_complex'ten çok daha güvenilirdir.
    """
    ses_listesi = sorted(ses_listesi, key=lambda x: x["baslangic"])
    parcalar     = []
    onceki_bitis = 0.0

    for i, sd in enumerate(ses_listesi):
        bas      = sd["baslangic"]
        ses_sure = ses_sure_olc(sd["dosya"])
        if ses_sure <= 0:
            continue

        # Önceki bitiş ile bu segmentin başı arasındaki boşluğu doldur
        bosluk = bas - onceki_bitis
        if bosluk > 0.02:
            bp = os.path.join(tmp_dir, f"b_{i:04d}.wav")
            if _sessizlik_olustur(bosluk, bp):
                parcalar.append(bp)

        # Sesi WAV'a çevir
        wp = os.path.join(tmp_dir, f"s_{i:04d}.wav")
        if _mp3_wav_cevir(sd["dosya"], wp):
            parcalar.append(wp)
            onceki_bitis = bas + ses_sure
        else:
            onceki_bitis = bas + 0.5

    if not parcalar:
        return False

    # Video sonuna kadar sondaki boşluğu doldur
    kalan = video_sure - onceki_bitis
    if kalan > 0.05:
        ep = os.path.join(tmp_dir, "b_son.wav")
        if _sessizlik_olustur(kalan, ep):
            parcalar.append(ep)

    # Tek parça ise direkt kopyala
    if len(parcalar) == 1:
        shutil.copy(parcalar[0], cikis_wav)
        return True

    # concat ile birleştir — 500'den fazla parça varsa 50'şerlik gruplarla
    GRUP = 400
    if len(parcalar) <= GRUP:
        girdi = []
        for p in parcalar:
            girdi += ["-i", p]
        n  = len(parcalar)
        fc = "".join(f"[{j}:a]" for j in range(n)) + f"concat=n={n}:v=0:a=1[ao]"
        r  = subprocess.run(
            ["ffmpeg", "-y", *girdi,
             "-filter_complex", fc, "-map", "[ao]",
             "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", cikis_wav],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode != 0:
            print(f"[Track Concat Hata] {r.stderr[-600:]}")
            return False
    else:
        # Çok fazla parça — gruplar halinde ara WAV üret, sonra birleştir
        ara_wavler = []
        for g in range(0, len(parcalar), GRUP):
            grup  = parcalar[g:g+GRUP]
            girdi = []
            for p in grup: girdi += ["-i", p]
            n  = len(grup)
            fc = "".join(f"[{j}:a]" for j in range(n)) + f"concat=n={n}:v=0:a=1[ao]"
            ara = os.path.join(tmp_dir, f"ara_{g//GRUP}.wav")
            r   = subprocess.run(
                ["ffmpeg", "-y", *girdi,
                 "-filter_complex", fc, "-map", "[ao]",
                 "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", ara],
                capture_output=True, text=True, timeout=300
            )
            if r.returncode == 0:
                ara_wavler.append(ara)
        # Ara WAV'ları birleştir
        girdi = []
        for a in ara_wavler: girdi += ["-i", a]
        n  = len(ara_wavler)
        fc = "".join(f"[{j}:a]" for j in range(n)) + f"concat=n={n}:v=0:a=1[ao]"
        r  = subprocess.run(
            ["ffmpeg", "-y", *girdi,
             "-filter_complex", fc, "-map", "[ao]",
             "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", cikis_wav],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode != 0:
            print(f"[Track Ara Concat Hata] {r.stderr[-600:]}")
            return False

    return True


def ffmpeg_ses_miksleme(
    video_yolu: str,
    ses_listesi: list,
    cikti_yolu: str,
    orig_vol: float = 0.1,
    dub_vol: float  = 1.0,
    gecici_klasor: str = "",
) -> bool:
    """
    1) Segment seslerinden dublaj WAV track'i oluştur (sessizlik+concat)
    2) Orijinal ses + dublaj track'i mikslayıp video'ya göm
    """
    if not ses_listesi:
        return False

    tmp = gecici_klasor or os.path.dirname(ses_listesi[0]["dosya"])
    video_sure = ses_sure_olc(video_yolu)
    if video_sure <= 0:
        video_sure = 3600.0

    dublaj_wav = os.path.join(tmp, "DUBLAJ_TRACK.wav")
    print(f"[Miks] {len(ses_listesi)} segment → track oluşturuluyor...")

    if not _dublaj_track_olustur(ses_listesi, video_sure, dublaj_wav, tmp):
        print("[Miks Hata] Dublaj track oluşturulamadı")
        return False

    print(f"[Miks] Track OK ({ses_sure_olc(dublaj_wav):.1f}s) — video ile birleştiriliyor...")

    cmd = [
        "ffmpeg", "-y",
        "-threads", "0",          # tüm CPU çekirdekleri
        "-i", video_yolu,
        "-i", dublaj_wav,
        "-filter_complex",
        f"[0:a]volume={orig_vol}[orig];"
        f"[1:a]volume={dub_vol}[dub];"
        f"[orig][dub]amix=inputs=2:duration=first:dropout_transition=1:normalize=0[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-threads", "0",
        "-shortest",
        cikti_yolu,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"[FFmpeg Final Hata] {result.stderr[-1000:]}")
        return False
    print(f"[Miks] ✓ → {cikti_yolu}")
    return True


# ============================================================
# METİN NORMALIZE — TTS öncesi ön işleme
# ============================================================

# İnternet kısaltmaları → okunabilir metin (dile göre)
KISALTMA_SOZLUK = {
    "tr": {
        # Genel Türkçe kısaltmalar
        "dk": "dakika", "sn": "saniye", "saat": "saat",
        "vb": "ve benzeri", "vb.": "ve benzeri",
        "vs": "ve saire", "vs.": "ve saire",
        "örn": "örneğin", "örn.": "örneğin",
        "yani": "yani", "falan": "falan",
        "Türkiye": "Türkiye", "İstanbul": "İstanbul",
        # Sosyal medya / sokak jargonu TR
        "kanka": "kanka", "aga": "ağa", "ya": "ya",
        "amk": "いや", "lan": "lan",
    },
    "en": {
        # İnternet kısaltmaları → açık metin (TTS'in doğru okuyacağı hale)
        "lol": "laughing out loud",
        "lmao": "laughing my ass off",
        "lmfao": "laughing my freaking ass off",
        "rofl": "rolling on the floor laughing",
        "omg": "oh my god",
        "omfg": "oh my freaking god",
        "wtf": "what the f",
        "wth": "what the heck",
        "brb": "be right back",
        "afk": "away from keyboard",
        "ngl": "not gonna lie",
        "fr": "for real",
        "frfr": "for real for real",
        "ong": "on god",
        "nvm": "never mind",
        "idk": "I don't know",
        "idc": "I don't care",
        "imo": "in my opinion",
        "imho": "in my humble opinion",
        "tbh": "to be honest",
        "tbf": "to be fair",
        "fyi": "for your information",
        "asap": "as soon as possible",
        "eta": "estimated time of arrival",
        "tl;dr": "too long didn't read",
        "tldr": "too long didn't read",
        "smh": "shaking my head",
        "smdh": "shaking my damn head",
        "irl": "in real life",
        "dm": "direct message",
        "dms": "direct messages",
        "hmu": "hit me up",
        "fomo": "fear of missing out",
        "yolo": "you only live once",
        "goat": "greatest of all time",
        "w": "win",
        "l": "loss",
        "npc": "non-playable character",
        "pov": "point of view",
        "iykyk": "if you know you know",
        "istg": "I swear to god",
        "rn": "right now",
        "imo": "in my opinion",
        "lowkey": "low key",
        "highkey": "high key",
        "cap": "lie",
        "no cap": "no lie",
        "bussin": "really good",
        "sus": "suspicious",
        "slay": "slay",
        "bet": "bet",
        "vibe": "vibe",
        "based": "based",
        "ratio": "ratio",
        "mid": "mediocre",
        "fire": "fire",
        "lit": "lit",
        "flex": "flex",
        "sic": "sick",
        "w/": "with",
        "w/o": "without",
        "b4": "before",
        "u": "you",
        "r": "are",
        "ur": "your",
        "thx": "thanks",
        "ty": "thank you",
        "np": "no problem",
        "yw": "you're welcome",
        "bc": "because",
        "cuz": "because",
        "gonna": "going to",
        "wanna": "want to",
        "gotta": "got to",
        "kinda": "kind of",
        "sorta": "sort of",
        "dunno": "don't know",
    },
    "de": {
        "bzw": "beziehungsweise",
        "usw": "und so weiter",
        "z.b": "zum Beispiel",
        "zb": "zum Beispiel",
        "ggf": "gegebenenfalls",
        "mfg": "mit freundlichen Grüßen",
        "lg": "liebe Grüße",
        "lol": "lachend",
        "omg": "oh mein Gott",
        "ngl": "ehrlich gesagt",
        "nvm": "vergiss es",
    },
    "fr": {
        "svp": "s'il vous plaît",
        "stp": "s'il te plaît",
        "lol": "mort de rire",
        "mdr": "mort de rire",
        "jsais": "je sais",
        "jsuis": "je suis",
        "ct": "c'était",
        "pk": "pourquoi",
        "pcq": "parce que",
    },
}

# Yaygın yanlış telaffuz düzeltmeleri (alias yaklaşımı)
TELAFFUZ_DUZELT = {
    "en": {
        # Teknik terimler
        "GIF": "jif",
        "API": "A P I",
        "URL": "U R L",
        "SQL": "sequel",
        "nginx": "engine x",
        "Linux": "linnux",
        "data": "dayta",
        "cache": "cash",
        "queue": "cue",
        "meme": "meem",
        "niche": "neesh",
        "GIF": "jif",
        # Para birimleri
        "$": "dollars",
        "€": "euros",
        "£": "pounds",
        "¥": "yen",
        # Yaygın yanlışlar
        "etc": "et cetera",
        "etc.": "et cetera",
        "i.e.": "that is",
        "e.g.": "for example",
    },
    "tr": {
        # Türkçe teknik terimler
        "AI": "yapay zeka",
        "ML": "makine öğrenimi",
        "API": "A P İ",
        "URL": "U R L",
        "Wi-Fi": "vay fay",
        "PDF": "P D F",
        "SMS": "S M S",
        "$": "dolar",
        "€": "euro",
        "£": "sterlin",
        "%": "yüzde",
        "km": "kilometre",
        "kg": "kilogram",
        "m²": "metrekare",
    },
}


def metin_normalize(metin: str, dil: str = "en") -> str:
    """
    TTS'e göndermeden önce metni normalize et:
    1. İnternet kısaltmalarını açık metne çevir
    2. Yaygın telaffuz hatalarını düzelt
    3. Sembol/sayı normalizasyonu
    4. Gereksiz boşlukları temizle
    """
    if not metin:
        return metin

    # Temel temizlik
    metin = metin.strip()

    # Dile özgü kısaltma sözlüğü
    kisaltmalar = KISALTMA_SOZLUK.get(dil, KISALTMA_SOZLUK.get("en", {}))
    duzeltmeler = TELAFFUZ_DUZELT.get(dil, {})

    # Kısaltmaları değiştir (büyük/küçük harf duyarsız, kelime sınırıyla)
    for kisaltma, acilim in kisaltmalar.items():
        pattern = r'\b' + re.escape(kisaltma) + r'\b'
        metin = re.sub(pattern, acilim, metin, flags=re.IGNORECASE)

    # Telaffuz düzeltmeleri (büyük/küçük duyarlı olanlar için)
    for yanlis, dogru in duzeltmeler.items():
        metin = metin.replace(yanlis, dogru)

    # Sayı + birim normalizasyonu
    metin = re.sub(r'(\d+)%', r'\1 percent', metin) if dil == 'en' else re.sub(r'(\d+)%', r'\1 yüzde', metin)
    metin = re.sub(r'\$(\d+)', r'\1 dollars', metin) if dil == 'en' else metin
    metin = re.sub(r'€(\d+)', r'\1 euros', metin) if dil in ('en','de') else metin

    # Gereksiz tekrar boşlukları temizle
    metin = re.sub(r'\s+', ' ', metin).strip()

    return metin


async def elevenlabs_segment_uret(
    metin: str,
    ses_id: str,
    output_path: str,
    hedef_sure: float,
    retry: int = 2,
    dil: str = "en",
) -> bool:
    """
    Segment metni sese çevirir.
    - TTS öncesi metin normalizasyonu uygular (kısaltmalar, jargon)
    - Rate limit ve hatalarda retry
    - Ses hedef süreden uzunsa akıllı sıkıştırma
    """
    # Metin normalize et
    metin_temiz = metin_normalize(metin, dil)
    if metin_temiz != metin:
        log.debug(f"[Normalize] '{metin[:50]}' → '{metin_temiz[:50]}'")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ses_id}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY,
    }
    data = {
        "text": metin_temiz,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.80,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }
    for deneme in range(retry + 1):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=data, headers=headers, timeout=90.0)

            if r.status_code == 429:
                bekle = 6 * (deneme + 1)
                print(f"[ElevenLabs] Rate limit → {bekle}s bekleniyor...")
                await asyncio.sleep(bekle)
                continue

            if r.status_code != 200:
                print(f"[ElevenLabs {r.status_code}] {r.text[:150]}")
                if deneme < retry:
                    await asyncio.sleep(2)
                    continue
                return False

            with open(output_path, "wb") as f:
                f.write(r.content)

            # Süre kontrolü — akıllı hız ayarı
            gercek = ses_sure_olc(output_path)
            if gercek > 0 and hedef_sure > 0:
                oran = gercek / hedef_sure
                print(f"[TTS] üretilen={gercek:.2f}s hedef={hedef_sure:.2f}s oran={oran:.2f}x")

                if oran > 1.25:
                    # atempo max 2.0x — üstünü chain'le (1.5x * 1.5x = 2.25x)
                    oran_sinir = min(oran, 2.0)
                    if oran_sinir > 2.0:
                        # İki aşamalı: oran1 * oran2 = hedef
                        oran1 = min(oran_sinir ** 0.5, 2.0)
                        oran2 = min(oran_sinir / oran1, 2.0)
                        filtre = f"atempo={oran1:.4f},atempo={oran2:.4f}"
                    else:
                        filtre = f"atempo={oran_sinir:.4f}"

                    adj = output_path + "_adj.mp3"
                    r2  = subprocess.run(
                        ["ffmpeg", "-y", "-i", output_path,
                         "-filter:a", filtre,
                         "-c:a", "libmp3lame", "-q:a", "2", adj],
                        capture_output=True, text=True, timeout=60
                    )
                    if r2.returncode == 0:
                        os.replace(adj, output_path)
                        print(f"[TTS] Sıkıştırıldı: {oran_sinir:.2f}x")
                # oran < 1 → kısa üretildi, boşluk olsun — doğal

            return True

        except Exception as e:
            print(f"[ElevenLabs İstisna deneme {deneme+1}] {e}")
            if deneme < retry:
                await asyncio.sleep(3)
    return False

# ============================================================
# SRT YARDIMCILARI
# ============================================================
def saniye_srt_cevir(s: float) -> str:
    return f"{int(s//3600):02d}:{int((s%3600)//60):02d}:{int(s%60):02d},{int((s-int(s))*1000):03d}"

def deepgram_to_srt(dg_response, path: str):
    try:
        utterances = dg_response.results.utterances
    except Exception:
        utterances = None

    # Kelime bazlı zaman + confidence — TikTok modu için JSON kaydet
    kelime_listesi = []
    try:
        words_all = dg_response.results.channels[0].alternatives[0].words
        for w in words_all:
            kelime_listesi.append({
                "word":       w.word,
                "start":      round(getattr(w, 'start', 0), 4),
                "end":        round(getattr(w, 'end', 0), 4),
                "confidence": round(getattr(w, 'confidence', 1.0), 3),
                "speaker":    getattr(w, 'speaker', 0),
            })
    except Exception:
        pass

    if kelime_listesi:
        words_path = path.replace('.srt', '_words.json')
        conf_path  = path.replace('.srt', '_confidence.json')
        try:
            with open(words_path, 'w', encoding='utf-8') as f:
                json.dump(kelime_listesi, f, ensure_ascii=False)
            # Confidence dict (kelime → skor) — geriye dönük uyumluluk
            with open(conf_path, 'w', encoding='utf-8') as f:
                json.dump({w['word'].lower().strip('.,?!;:'): w['confidence'] for w in kelime_listesi}, f, ensure_ascii=False)
        except Exception:
            pass

    if utterances and len(utterances) > 0:
        speakers = set(getattr(u, 'speaker', 0) for u in utterances)
        cok_konusmaci = len(speakers) > 1

        with open(path, "w", encoding="utf-8") as f:
            idx = 1
            for u in utterances:
                speaker    = getattr(u, 'speaker', 0)
                transcript = u.transcript.strip()
                if not transcript:
                    continue
                prefix    = f"[Konuşmacı {speaker}]: " if cok_konusmaci else ""
                kelimeler = transcript.split()
                if len(kelimeler) <= 12:
                    f.write(f"{idx}\n{saniye_srt_cevir(u.start)} --> {saniye_srt_cevir(u.end)}\n{prefix}{transcript}\n\n")
                    idx += 1
                else:
                    sure     = u.end - u.start
                    parcalar = [kelimeler[i:i+8] for i in range(0, len(kelimeler), 8)]
                    for p_idx, parca in enumerate(parcalar):
                        p_start = u.start + (p_idx / len(parcalar)) * sure
                        p_end   = u.start + ((p_idx + 1) / len(parcalar)) * sure
                        f.write(f"{idx}\n{saniye_srt_cevir(p_start)} --> {saniye_srt_cevir(p_end)}\n{prefix}{' '.join(parca)}\n\n")
                        idx += 1
        return

    try:
        words = dg_response.results.channels[0].alternatives[0].words
    except Exception:
        return
    if not words:
        return

    gruplar, mevcut = [], [words[0]]
    for w in words[1:]:
        sessizlik = w.start - mevcut[-1].end
        noktalama = mevcut[-1].word.rstrip().endswith(('.', '?', '!', ','))
        if sessizlik > 0.8 or noktalama or len(mevcut) >= 8:
            gruplar.append(mevcut)
            mevcut = [w]
        else:
            mevcut.append(w)
    if mevcut:
        gruplar.append(mevcut)

    with open(path, "w", encoding="utf-8") as f:
        for i, grup in enumerate(gruplar):
            f.write(f"{i+1}\n{saniye_srt_cevir(grup[0].start)} --> {saniye_srt_cevir(grup[-1].end)}\n{' '.join(w.word for w in grup)}\n\n")

# ============================================================
# ANA İŞLEM MOTORU
# ============================================================
async def islem_motoru(out_file, modul, hedef_dil, ses_id, tmp_in, yazili_metin, kaynak_dil, f_name, f_size, f_color, is_bold, is_shadow, m_v, orig_vol=0.03, dub_vol=1.0):
    b_id   = os.path.splitext(out_file)[0].replace("sonuc_", "")
    gecici = os.path.join(TEMP_DIR, b_id)
    os.makedirs(gecici, exist_ok=True)

    try:
        islem_durumlari[out_file] = {"durum": "Başlatılıyor...", "yuzde": 5}

        # ── Dosya boyutu / süre kontrolü ──
        if tmp_in and os.path.exists(tmp_in):
            gecerli, hata_msg = _dosya_kontrol(tmp_in)
            if not gecerli:
                islem_durumlari[out_file] = {"durum": f"Hata: {hata_msg}", "yuzde": 0}
                return

        # ── API key kontrolleri ──
        if modul in ["desifre", "altyazi", "seslendirme"] and not DEEPGRAM_API_KEY:
            islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji('deepgram_401')}", "yuzde": 0}
            return
        if modul in ["metinden_sese", "seslendirme"] and not ELEVENLABS_API_KEY:
            islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji('elevenlabs_401')}", "yuzde": 0}
            return

        # ses_id boşsa varsayılan
        if not ses_id or ses_id.strip() == "":
            ses_id = "nPczCjzI2devNBz1zQrb"
            log.warning("[Motor] ses_id boş — Brian varsayılan kullanılıyor")

        # ── METİNDEN SESE ──────────────────────────────────
        if modul == "metinden_sese":
            islem_durumlari[out_file] = {"durum": "Metin hazırlanıyor...", "yuzde": 20}

            # Hedef dil varsa önce çevir
            metin_final = yazili_metin or ""
            hedef_dil_upper = hedef_dil.upper() if hedef_dil else ""
            kaynak_dil_upper = kaynak_dil.upper() if kaynak_dil else "TR"

            if hedef_dil_upper and hedef_dil_upper not in ("AUTO", "") and hedef_dil_upper != kaynak_dil_upper and DEEPL_API_KEY:
                try:
                    islem_durumlari[out_file] = {"durum": f"DeepL ile {hedef_dil_upper}'e çevriliyor...", "yuzde": 35}
                    deepl_hedef = DEEPL_DILLER.get(hedef_dil_upper, hedef_dil_upper)
                    async with httpx.AsyncClient() as c:
                        dr = await c.post(
                            f"{_deepl_base_url()}/v2/translate",
                            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
                            json={"text": [metin_final], "target_lang": deepl_hedef},
                            timeout=30.0,
                        )
                        if dr.status_code == 200:
                            metin_final = dr.json()["translations"][0]["text"]
                            log.info(f"[TTS Çeviri] {kaynak_dil_upper}→{hedef_dil_upper}: {metin_final[:80]}")
                        else:
                            log.warning(f"[TTS Çeviri] DeepL {dr.status_code}: {dr.text[:200]}")
                except Exception as e:
                    log.warning(f"[TTS Çeviri Hata] {e}")

            # Normalize et
            metin_final = metin_normalize(metin_final, hedef_dil.lower() if hedef_dil else "tr")

            islem_durumlari[out_file] = {"durum": "ElevenLabs sesi sentezliyor...", "yuzde": 60}
            ok = await elevenlabs_ses_uret(metin_final, ses_id, os.path.join(OUTPUT_DIR, out_file))
            if ok:
                islem_durumlari[out_file] = {"durum": "Tamamlandı", "yuzde": 100}
            else:
                islem_durumlari[out_file] = {
                    "durum": f"Hata: Ses üretilemedi. Railway loglarında detay var. (ses_id={ses_id[:8]}...)",
                    "yuzde": 0
                }
            return

        # ── DEŞİFRE ────────────────────────────────────────
        if modul == "desifre":
            islem_durumlari[out_file] = {"durum": "Ses analiz ediliyor...", "yuzde": 30}
            try:
                dg = await deepgram_desifre_et(tmp_in, kaynak_dil)
            except ValueError as e:
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji(str(e))}", "yuzde": 0}
                return
            islem_durumlari[out_file] = {"durum": "Transkript oluşturuluyor...", "yuzde": 70}
            srt_path = os.path.join(OUTPUT_DIR, os.path.splitext(out_file)[0] + ".srt")
            deepgram_to_srt(dg, srt_path)
            if not os.path.exists(srt_path) or os.path.getsize(srt_path) < 10:
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji('segment_yok')}", "yuzde": 0}
                return

            # Çeviri — hedef dil varsa çevir
            hd = (hedef_dil or "").strip().upper()
            kd = (kaynak_dil or "").strip().upper()
            if hd and hd not in ("", "AUTO") and hd != kd and DEEPL_API_KEY:
                islem_durumlari[out_file] = {"durum": f"DeepL ile {hd}'e çevriliyor...", "yuzde": 85}
                try:
                    with open(srt_path, encoding="utf-8") as f:
                        icerik = f.read()
                    cevrilmis = await srt_paralel_cevir(icerik, hedef_dil)
                    with open(srt_path, "w", encoding="utf-8") as f:
                        f.write(cevrilmis)
                    log.info(f"[Desifre] Çeviri tamamlandı: {kaynak_dil}→{hedef_dil}")
                except Exception as e:
                    log.warning(f"[Desifre] Çeviri hatası: {e} — orijinal transkript kullanılıyor")
            elif hd and not DEEPL_API_KEY:
                log.warning("[Desifre] Hedef dil seçili ama DEEPL_API_KEY eksik")

            islem_durumlari[out_file] = {"durum": "Tamamlandı", "yuzde": 100}
            return

        # ── ALTYAZI ────────────────────────────────────────
        if modul == "altyazi":
            if not ffmpeg_var_mi():
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji('ffmpeg_yok')}", "yuzde": 0}
                return
            islem_durumlari[out_file] = {"durum": "Ses analiz ediliyor...", "yuzde": 20}
            try:
                dg = await deepgram_desifre_et(tmp_in, kaynak_dil)
            except ValueError as e:
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji(str(e))}", "yuzde": 0}
                return

            srt_kaynak = os.path.join(gecici, "kaynak.srt")
            deepgram_to_srt(dg, srt_kaynak)
            srt_final = srt_kaynak

            # Çeviri kontrolü — kaynak "auto" bile olsa hedef dil varsa çevir
            hd = (hedef_dil or "").strip().upper()
            kd = (kaynak_dil or "").strip().upper()
            if hd and hd not in ("", "AUTO") and hd != kd and DEEPL_API_KEY:
                islem_durumlari[out_file] = {"durum": f"DeepL ile {hd}'e çevriliyor...", "yuzde": 50}
                with open(srt_kaynak, encoding="utf-8") as f:
                    icerik = f.read()
                cevrilmis = await srt_paralel_cevir(icerik, hedef_dil)
                srt_final = os.path.join(gecici, f"ceviri_{hedef_dil}.srt")
                with open(srt_final, "w", encoding="utf-8") as f:
                    f.write(cevrilmis)
            elif hd and hd not in ("", "AUTO") and not DEEPL_API_KEY:
                log.warning("[Altyazı] Hedef dil seçili ama DEEPL_API_KEY eksik — çeviri atlandı")

            base = os.path.splitext(out_file)[0]
            shutil.copy(srt_final, os.path.join(OUTPUT_DIR, base + ".srt"))

            if ffmpeg_var_mi():
                islem_durumlari[out_file] = {"durum": "Altyazı videoya gömülüyor...", "yuzde": 75}
                ok = ffmpeg_altyazi_gom(
                    tmp_in, srt_final,
                    os.path.join(OUTPUT_DIR, out_file),
                    f_name, f_size, f_color,
                    is_bold == "true", is_shadow == "true", m_v
                )
                if not ok:
                    islem_durumlari[out_file] = {"durum": "Uyarı: FFmpeg hatası, SRT kaydedildi", "yuzde": 100}
                    return
            else:
                islem_durumlari[out_file] = {"durum": "Uyarı: FFmpeg yok, SRT kaydedildi", "yuzde": 100}
                return

            islem_durumlari[out_file] = {"durum": "Tamamlandı", "yuzde": 100}
            return

        # ── DUBLAJ ─────────────────────────────────────────
        if modul == "seslendirme":

            # 1. Deşifre
            islem_durumlari[out_file] = {"durum": "Konuşmalar analiz ediliyor...", "yuzde": 8}
            try:
                dg = await deepgram_desifre_et(tmp_in, kaynak_dil)
            except ValueError as e:
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji(str(e))}", "yuzde": 0}
                return

            srt_path = os.path.join(OUTPUT_DIR, os.path.splitext(out_file)[0] + ".srt")
            deepgram_to_srt(dg, srt_path)

            with open(srt_path, encoding="utf-8") as f:
                srt_icerik = f.read()
            segmentler = _srt_parse(srt_icerik)

            if not segmentler:
                islem_durumlari[out_file] = {"durum": "Hata: Segment bulunamadı", "yuzde": 0}
                return

            # 2. Kısa segmentleri birleştir (< 0.8s → bir sonrakiyle birleştir)
            # Hem kalite artar hem ElevenLabs karakter israfı azalır
            segmentler = _kisa_seg_birlestir(segmentler, min_sure=0.8)

            # 3. Çeviri — kaynak "auto" bile olsa hedef dil varsa çevir
            hd = (hedef_dil or "").strip().upper()
            kd = (kaynak_dil or "").strip().upper()
            if hd and hd not in ("", "AUTO") and hd != kd and DEEPL_API_KEY:
                islem_durumlari[out_file] = {"durum": f"{hedef_dil} diline çevriliyor...", "yuzde": 15}
                cevrilmis_srt = await srt_paralel_cevir(srt_icerik, hedef_dil)
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(cevrilmis_srt)
                segmentler = _srt_parse(cevrilmis_srt)
                segmentler = _kisa_seg_birlestir(segmentler, min_sure=0.8)
                log.info(f"[Dublaj] Çeviri tamamlandı → {hedef_dil}, {len(segmentler)} segment")
            elif hd and hd not in ("", "AUTO") and not DEEPL_API_KEY:
                log.warning("[Dublaj] Hedef dil seçili ama DEEPL_API_KEY eksik — çeviri atlandı")

            # 4. Karakter sayısı kontrolü — kota uyarısı
            toplam_karakter = sum(len(re.sub(r"\[Konuşmacı \d+\]:\s*", "", s["metin"])) for s in segmentler)
            print(f"[Dublaj] {len(segmentler)} segment, ~{toplam_karakter} karakter kullanılacak")
            islem_durumlari[out_file] = {
                "durum": f"{len(segmentler)} segment, ~{toplam_karakter} karakter — sesler üretiliyor...",
                "yuzde": 18
            }
            speaker_ses_map = {}
            speaker_map_path = os.path.join(OUTPUT_DIR, os.path.splitext(out_file)[0] + "_speaker_map.json")
            if os.path.exists(speaker_map_path):
                try:
                    with open(speaker_map_path, encoding="utf-8") as f:
                        speaker_ses_map = json.load(f)
                    print(f"[Dublaj] Konuşmacı ses haritası: {speaker_ses_map}")
                except Exception:
                    pass

            # 4. Paralel ElevenLabs TTS
            toplam = len(segmentler)
            islem_durumlari[out_file] = {"durum": f"Sesler üretiliyor (0/{toplam})...", "yuzde": 20}

            ses_klasor = os.path.join(gecici, "sesler")
            os.makedirs(ses_klasor, exist_ok=True)

            semaphore  = asyncio.Semaphore(4)
            tamamlanan = [0]

            async def seg_uret_task(seg, idx, tum_segmentler):
                async with semaphore:
                    metin = re.sub(r"\[Konuşmacı \d+\]:\s*", "", seg["metin"]).strip()
                    temiz = re.sub(r"[^\w\s]", "", metin).strip()
                    if not metin or len(temiz) < 2:
                        tamamlanan[0] += 1
                        return None

                    # Konuşmacıya özel ses ID'si varsa onu kullan
                    speaker_no = re.search(r"\[Konuşmacı (\d+)\]", seg["metin"])
                    kullanilacak_ses = ses_id
                    if speaker_no:
                        sp_key = speaker_no.group(1)
                        kullanilacak_ses = speaker_ses_map.get(sp_key, ses_id)

                    # Hedef süre: bir sonraki segmentin başına kadar olan süre
                    # Bu sayede doğal boşluklar korunur
                    sonraki = next((s for s in tum_segmentler if s["no"] > seg["no"]), None)
                    if sonraki:
                        # Mevcut segmentin bitişinden sonraki segmentin başına kadar olan toplam
                        kullanilabilir = sonraki["baslangic"] - seg["baslangic"]
                        # En az kendi süresi, en fazla kullanılabilir alan
                        sure = max(seg["bitis"] - seg["baslangic"],
                                   min(kullanilabilir * 0.85, seg["bitis"] - seg["baslangic"] * 1.3))
                    else:
                        sure = max(1.0, seg["bitis"] - seg["baslangic"])

                    ses_yol = os.path.join(ses_klasor, f"seg_{idx:04d}.mp3")
                    ok = await elevenlabs_segment_uret(metin, kullanilacak_ses, ses_yol, sure, dil=hedef_dil or kaynak_dil or "en")
                    tamamlanan[0] += 1
                    pct = 20 + int((tamamlanan[0] / toplam) * 55)
                    islem_durumlari[out_file] = {
                        "durum": f"Sesler üretiliyor ({tamamlanan[0]}/{toplam})...",
                        "yuzde": pct,
                    }
                    if ok:
                        return {"dosya": ses_yol, "baslangic": seg["baslangic"]}
                    return None

            gorevler  = [seg_uret_task(seg, i, segmentler) for i, seg in enumerate(segmentler)]
            sonuclar  = await asyncio.gather(*gorevler)
            ses_liste = [s for s in sonuclar if s is not None]

            if not ses_liste:
                islem_durumlari[out_file] = {"durum": "Hata: Ses üretilemedi. ElevenLabs API key kontrol edin.", "yuzde": 0}
                return

            # 4. FFmpeg Miksleme
            islem_durumlari[out_file] = {"durum": f"{len(ses_liste)} ses senkronize ediliyor...", "yuzde": 80}
            cikti_tam = os.path.join(OUTPUT_DIR, out_file)

            ok = ffmpeg_ses_miksleme(
                video_yolu=tmp_in,
                ses_listesi=ses_liste,
                cikti_yolu=cikti_tam,
                orig_vol=orig_vol,
                dub_vol=dub_vol,
                gecici_klasor=ses_klasor,
            )

            if not ok:
                islem_durumlari[out_file] = {"durum": "Uyarı: Miksleme hatası, SRT kaydedildi", "yuzde": 100}
                return

            islem_durumlari[out_file] = {"durum": "Tamamlandı", "yuzde": 100}
            return

    except Exception as e:
        print(f"[Sistem Hata] {e}")
        import traceback; traceback.print_exc()
        islem_durumlari[out_file] = {"durum": "Hata: Sistem işleyemedi", "yuzde": 0}
    finally:
        if tmp_in and os.path.exists(tmp_in):
            try: os.remove(tmp_in)
            except: pass
        if os.path.exists(gecici):
            shutil.rmtree(gecici, ignore_errors=True)

# ============================================================
# ENDPOİNTLER
# ============================================================
@app.post("/api/islem/")
async def islem_baslat(
    arka_plan: BackgroundTasks,
    modul: str        = Form(...),
    hedef_dil: str    = Form(""),
    kaynak_dil: str   = Form("tr"),
    ses_id: str       = Form("nPczCjzI2devNBz1zQrb"),
    dosya: UploadFile = File(None),
    yazili_metin: str = Form(""),
    f_name: str       = Form("Arial"),
    f_size: str       = Form("22"),
    f_color: str      = Form("#ffffff"),
    is_bold: str      = Form("true"),
    is_shadow: str    = Form("true"),
    m_v: str          = Form("20"),
    orig_vol: str     = Form("0.03"),   # Konuşmacı ses seviyesi
    dub_vol_param: str = Form("1.0"),   # Dublaj ses seviyesi
):
    b_id   = uuid.uuid4().hex[:8]
    tmp_in = ""

    if modul == "metinden_sese":
        out_file = f"sonuc_{b_id}.mp3"
    elif modul == "desifre":
        out_file = f"sonuc_{b_id}.srt"
    else:
        out_file = f"sonuc_{b_id}.mp4"

    if dosya:
        # Dosya adını güvenli hale getir — boşluk, Türkçe karakter, özel karakter temizle
        import re as _re
        guvenli_ad = dosya.filename or "upload"
        guvenli_ad = _re.sub(r'[^\w.\-]', '_', guvenli_ad.replace(' ', '_'))
        guvenli_ad = guvenli_ad[:80]  # max 80 karakter
        tmp_in = os.path.join(TEMP_DIR, f"orijinal_{b_id}_{guvenli_ad}")
        with open(tmp_in, "wb") as buf:
            shutil.copyfileobj(dosya.file, buf)

    try:
        orig_vol_f = float(orig_vol)
        dub_vol_f  = float(dub_vol_param)
    except ValueError:
        orig_vol_f = 0.03
        dub_vol_f  = 1.0

    arka_plan.add_task(
        islem_motoru, out_file, modul, hedef_dil,
        ses_id, tmp_in, yazili_metin, kaynak_dil,
        f_name, f_size, f_color, is_bold, is_shadow, m_v,
        orig_vol_f, dub_vol_f,
    )
    return JSONResponse({"beklenen_dosya_adi": out_file})


@app.post("/api/cevir/")
async def ceviri_baslat(srt_dosya_adi: str = Form(...), hedef_dil: str = Form(...)):
    kaynak_yol = os.path.join(OUTPUT_DIR, srt_dosya_adi)
    if not os.path.exists(kaynak_yol):
        return JSONResponse({"hata": "SRT bulunamadı"}, status_code=404)
    if not DEEPL_API_KEY:
        return JSONResponse({"hata": "DEEPL_API_KEY eksik"}, status_code=500)
    try:
        with open(kaynak_yol, encoding="utf-8") as f:
            icerik = f.read()
        cevrilmis = await srt_paralel_cevir(icerik, hedef_dil)
        base     = os.path.splitext(srt_dosya_adi)[0]
        yeni_ad  = f"{base}_{hedef_dil.lower()}.srt"
        with open(os.path.join(OUTPUT_DIR, yeni_ad), "w", encoding="utf-8") as f:
            f.write(cevrilmis)
        return JSONResponse({"basari": True, "cevrilmis_dosya_adi": yeni_ad, "segment_sayisi": cevrilmis.count("\n\n")})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.get("/durum/{dosya_adi}")
async def durum_sorgula(dosya_adi: str):
    return JSONResponse(islem_durumlari.get(dosya_adi, {"durum": "Kuyrukta...", "yuzde": 0}))


@app.get("/dinle/{dosya_adi}")
async def dosyayi_dinle(dosya_adi: str):
    for yol in [
        os.path.join(OUTPUT_DIR, dosya_adi),
        os.path.join(OUTPUT_DIR, os.path.splitext(dosya_adi)[0] + ".srt"),
    ]:
        if os.path.exists(yol):
            return FileResponse(yol)
    return JSONResponse({"hata": "Bulunamadı"}, status_code=404)


@app.get("/indir/{dosya_adi}")
async def dosyayi_indir(dosya_adi: str):
    for yol, ad in [
        (os.path.join(OUTPUT_DIR, dosya_adi), dosya_adi),
        (os.path.join(OUTPUT_DIR, os.path.splitext(dosya_adi)[0] + ".srt"), os.path.splitext(dosya_adi)[0] + ".srt"),
    ]:
        if os.path.exists(yol):
            return FileResponse(yol, media_type="application/octet-stream", filename=ad)
    return JSONResponse({"hata": "Bulunamadı"}, status_code=404)


# ============================================================
# SRT PARSE / SERIALIZE
# ============================================================
def _saniye_srt_global(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s//3600):02d}:{int((s%3600)//60):02d}:{int(s%60):02d},{int(round((s-int(s))*1000)):03d}"

def _srt_saniyeye(zaman_str: str) -> float:
    try:
        temiz   = zaman_str.strip().replace(",", ".")
        parcalar = temiz.split(":")
        return float(parcalar[0]) * 3600 + float(parcalar[1]) * 60 + float(parcalar[2])
    except Exception:
        return 0.0

def _srt_parse(icerik: str) -> list:
    bloklar = []
    for blok in icerik.strip().split("\n\n"):
        satirlar = blok.strip().split("\n")
        if len(satirlar) < 3:
            continue
        try:
            no = int(satirlar[0].strip())
        except ValueError:
            continue
        zp = satirlar[1].split(" --> ")
        if len(zp) != 2:
            continue
        bloklar.append({
            "no": no,
            "baslangic": _srt_saniyeye(zp[0]),
            "bitis":     _srt_saniyeye(zp[1]),
            "metin":     "\n".join(satirlar[2:]),
        })
    return bloklar

def _kisa_seg_birlestir(segmentler: list, min_sure: float = 1.2) -> list:
    """
    Ardışık kısa segmentleri birleştirir.
    - min_sure: bu süreden kısa segmentler bir sonrakiyle birleşir
    - Aynı konuşmacı kontrolü: prefix farklıysa birleştirme
    - Max 20 kelime sınırı — çok uzun segment oluşmasın
    """
    if not segmentler:
        return segmentler

    def _speaker(metin):
        m = re.search(r"\[Konuşmacı (\d+)\]", metin)
        return m.group(1) if m else "0"

    sonuc = []
    i = 0
    while i < len(segmentler):
        seg = dict(segmentler[i])
        sure = seg["bitis"] - seg["baslangic"]
        sp   = _speaker(seg["metin"])

        while (sure < min_sure and
               i + 1 < len(segmentler) and
               len(seg["metin"].split()) < 20):
            sonraki  = segmentler[i + 1]
            sp_sonra = _speaker(sonraki["metin"])

            # Farklı konuşmacıysa birleştirme — her biri ayrı seslendirme alacak
            if sp_sonra != sp:
                break

            # Aralarındaki boşluk 2s'den fazlaysa birleştirme
            bosluk = sonraki["baslangic"] - seg["bitis"]
            if bosluk > 2.0:
                break

            seg["metin"] = seg["metin"].rstrip() + " " + re.sub(r"\[Konuşmacı \d+\]:\s*", "", sonraki["metin"]).lstrip()
            seg["bitis"] = sonraki["bitis"]
            sure = seg["bitis"] - seg["baslangic"]
            i += 1

        sonuc.append(seg)
        i += 1

    for idx, s in enumerate(sonuc, start=1):
        s["no"] = idx

    print(f"[Seg Birleştir] {len(segmentler)} → {len(sonuc)} segment (min_sure={min_sure}s)")
    return sonuc


def _srt_serialize(bloklar: list) -> str:
    satirlar = []
    for i, b in enumerate(bloklar, start=1):
        satirlar.append(
            f"{i}\n{_saniye_srt_global(b['baslangic'])} --> {_saniye_srt_global(b['bitis'])}\n{b['metin']}\n"
        )
    return "\n".join(satirlar)


# ============================================================
# CRUD ENDPOİNTLERİ
# ============================================================
@app.post("/api/zaman_guncelle/")
async def zaman_guncelle(
    dosya_adi: str = Form(...), segment_no: int = Form(...),
    yeni_baslangic: float = Form(...), yeni_bitis: float = Form(...),
):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        ok = False
        for b in bloklar:
            if b["no"] == segment_no:
                b["baslangic"] = yeni_baslangic
                b["bitis"]     = yeni_bitis
                ok = True; break
        if not ok:
            return JSONResponse({"hata": "Segment bulunamadı."}, status_code=404)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(_srt_serialize(bloklar))
        return JSONResponse({"basari": True, "segment_no": segment_no,
                             "yeni_zaman": f"{_saniye_srt_global(yeni_baslangic)} --> {_saniye_srt_global(yeni_bitis)}"})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/metin_guncelle/")
async def metin_guncelle(dosya_adi: str = Form(...), segment_no: int = Form(...), yeni_metin: str = Form(...)):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        ok = False
        for b in bloklar:
            if b["no"] == segment_no:
                b["metin"] = yeni_metin.strip(); ok = True; break
        if not ok:
            return JSONResponse({"hata": "Segment bulunamadı."}, status_code=404)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(_srt_serialize(bloklar))
        return JSONResponse({"basari": True})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/segment_birlestir/")
async def segment_birlestir(dosya_adi: str = Form(...), segment_no: int = Form(...)):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        idx = next((i for i, b in enumerate(bloklar) if b["no"] == segment_no), None)
        if idx is None or idx + 1 >= len(bloklar):
            return JSONResponse({"hata": "Sonraki segment yok."}, status_code=404)
        b1, b2 = bloklar[idx], bloklar[idx+1]
        bloklar[idx] = {"no": b1["no"], "baslangic": b1["baslangic"], "bitis": b2["bitis"],
                        "metin": f"{b1['metin']} {b2['metin']}".strip()}
        bloklar.pop(idx+1)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(_srt_serialize(bloklar))
        return JSONResponse({"basari": True, "yeni_segment_sayisi": len(bloklar)})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/segment_bol/")
async def segment_bol(dosya_adi: str = Form(...), segment_no: int = Form(...), kesim_saniye: float = Form(...)):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        idx = next((i for i, b in enumerate(bloklar) if b["no"] == segment_no), None)
        if idx is None:
            return JSONResponse({"hata": "Segment bulunamadı."}, status_code=404)
        b = bloklar[idx]
        if not (b["baslangic"] < kesim_saniye < b["bitis"]):
            return JSONResponse({"hata": "Kesim noktası segmentin dışında."}, status_code=400)
        kelimeler = b["metin"].split()
        yari = max(1, len(kelimeler) // 2)
        bloklar[idx]   = {"no": b["no"],   "baslangic": b["baslangic"], "bitis": kesim_saniye, "metin": " ".join(kelimeler[:yari])}
        bloklar.insert(idx+1, {"no": b["no"]+1, "baslangic": kesim_saniye,   "bitis": b["bitis"],   "metin": " ".join(kelimeler[yari:]) or "..."})
        with open(yol, "w", encoding="utf-8") as f:
            f.write(_srt_serialize(bloklar))
        return JSONResponse({"basari": True, "yeni_segment_sayisi": len(bloklar)})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/segment_sil/")
async def segment_sil(dosya_adi: str = Form(...), segment_no: int = Form(...)):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        onceki = len(bloklar)
        bloklar = [b for b in bloklar if b["no"] != segment_no]
        if len(bloklar) == onceki:
            return JSONResponse({"hata": "Segment bulunamadı."}, status_code=404)
        with open(yol, "w", encoding="utf-8") as f:
            f.write(_srt_serialize(bloklar))
        return JSONResponse({"basari": True, "yeni_segment_sayisi": len(bloklar)})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.get("/api/cakisma_tespit/{dosya_adi}")
async def cakisma_tespit(dosya_adi: str):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        cakismalar = []
        for i in range(len(bloklar) - 1):
            if bloklar[i]["bitis"] > bloklar[i+1]["baslangic"]:
                cakismalar.append({
                    "segment_a": bloklar[i]["no"], "segment_b": bloklar[i+1]["no"],
                    "cakisma_ms": round((bloklar[i]["bitis"] - bloklar[i+1]["baslangic"]) * 1000),
                })
        return JSONResponse({"cakisma_sayisi": len(cakismalar), "cakismalar": cakismalar, "temiz": len(cakismalar) == 0})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/cakisma_duzelt/")
async def cakisma_duzelt(dosya_adi: str = Form(...), bosluk_ms: int = Form(50)):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        n = 0
        bosluk = bosluk_ms / 1000.0
        for i in range(len(bloklar) - 1):
            if bloklar[i]["bitis"] > bloklar[i+1]["baslangic"]:
                bloklar[i]["bitis"] = max(bloklar[i]["baslangic"] + 0.1, bloklar[i+1]["baslangic"] - bosluk)
                n += 1
        with open(yol, "w", encoding="utf-8") as f:
            f.write(_srt_serialize(bloklar))
        return JSONResponse({"basari": True, "duzeltilen_cakisma": n, "yeni_segment_sayisi": len(bloklar)})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.get("/api/waveform/{dosya_adi}")
async def waveform_al(dosya_adi: str, orneklem: int = 200):
    orneklem  = min(max(orneklem, 10), 2000)
    dosya_yolu = None
    for uzanti in ["", ".mp4", ".mp3", ".wav", ".aac"]:
        d = os.path.join(OUTPUT_DIR, dosya_adi if dosya_adi.endswith(uzanti) else dosya_adi + uzanti)
        if os.path.exists(d):
            dosya_yolu = d; break
    if not dosya_yolu:
        return JSONResponse({"hata": "Medya bulunamadı."}, status_code=404)
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": "FFmpeg yok."}, status_code=500)
    try:
        cmd = ["ffmpeg", "-y", "-i", dosya_yolu, "-ac", "1", "-ar", "8000",
               "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        import struct
        ham = result.stdout
        n_total = len(ham) // 2
        if n_total == 0:
            return JSONResponse({"hata": "Ses verisi boş."}, status_code=400)
        chunk = max(1, n_total // orneklem)
        vals  = []
        for i in range(orneklem):
            parca = ham[i*chunk*2:(i+1)*chunk*2]
            if not parca:
                vals.append(0.0); continue
            samples = struct.unpack(f"<{len(parca)//2}h", parca)
            vals.append(round(max(abs(v) for v in samples) / 32768.0, 4))
        return JSONResponse({"dosya": dosya_adi, "orneklem_sayisi": len(vals), "waveform": vals})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


# ============================================================
# SEGMENT YENİDEN SESLENDİR
# ============================================================
@app.post("/api/segment_yeniden_seslendir/")
async def segment_yeniden_seslendir(
    arka_plan: BackgroundTasks,
    video_dosya_adi: str = Form(...),
    srt_dosya_adi:   str = Form(...),
    segment_no: int      = Form(...),
    ses_id: str          = Form(...),
):
    """Tek segmenti yeniden seslendirip mevcut videoya işler."""
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)

    video_yol = os.path.join(OUTPUT_DIR, video_dosya_adi)
    srt_yol   = os.path.join(OUTPUT_DIR, srt_dosya_adi)

    if not os.path.exists(video_yol):
        return JSONResponse({"hata": "Video dosyası bulunamadı."}, status_code=404)
    if not os.path.exists(srt_yol):
        return JSONResponse({"hata": "SRT dosyası bulunamadı."}, status_code=404)

    try:
        with open(srt_yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        seg = next((b for b in bloklar if b["no"] == segment_no), None)
        if not seg:
            return JSONResponse({"hata": f"Segment {segment_no} bulunamadı."}, status_code=404)

        metin = re.sub(r"\[Konuşmacı \d+\]:\s*", "", seg["metin"]).strip()
        if not metin:
            return JSONResponse({"hata": "Segment metni boş."}, status_code=400)

        b_id = uuid.uuid4().hex[:8]
        islem_id = f"redub_{b_id}"
        islem_durumlari[islem_id] = {"durum": "Ses üretiliyor...", "yuzde": 20}

        async def _redub():
            tmp_dir = os.path.join(TEMP_DIR, f"redub_{b_id}")
            os.makedirs(tmp_dir, exist_ok=True)
            try:
                ses_yol = os.path.join(tmp_dir, "seg.mp3")
                sure    = max(0.5, seg["bitis"] - seg["baslangic"])
                ok = await elevenlabs_segment_uret(metin, ses_id, ses_yol, sure)
                if not ok:
                    islem_durumlari[islem_id] = {"durum": "Hata: Ses üretilemedi.", "yuzde": 0}
                    return
                islem_durumlari[islem_id] = {"durum": "Videoya işleniyor...", "yuzde": 60}
                yedek = video_yol.replace(".mp4", f"_yedek_{b_id}.mp4")
                shutil.copy(video_yol, yedek)
                ok2 = ffmpeg_ses_miksleme(
                    video_yolu=yedek,
                    ses_listesi=[{"dosya": ses_yol, "baslangic": seg["baslangic"]}],
                    cikti_yolu=video_yol,
                    orig_vol=orig_vol, dub_vol=dub_vol, gecici_klasor=tmp_dir,
                )
                os.remove(yedek) if ok2 else shutil.copy(yedek, video_yol) or os.remove(yedek)
                islem_durumlari[islem_id] = {
                    "durum": "Tamamlandı" if ok2 else "Hata: FFmpeg başarısız.",
                    "yuzde": 100 if ok2 else 0
                }
            except Exception as e:
                log.error(f"[Redub] {e}")
                islem_durumlari[islem_id] = {"durum": "Hata: İşlem tamamlanamadı.", "yuzde": 0}
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        arka_plan.add_task(_redub)
        return JSONResponse({"islem_id": islem_id, "segment_no": segment_no, "metin": metin})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/ses_klonla/")
async def ses_klonla(
    ses_dosyasi: UploadFile = File(...),
    isim: str = Form("Klonlanan Ses"),
):
    """
    ElevenLabs Instant Voice Cloning API ile ses klonlar.
    Min 30s, max 5 dakika ses dosyası önerilir.
    Free plan desteklemez — Creator+ gerektirir.
    """
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)

    try:
        # Geçici dosyaya kaydet
        b_id = uuid.uuid4().hex[:8]
        ext  = os.path.splitext(ses_dosyasi.filename or "ses.mp3")[1] or ".mp3"
        tmp  = os.path.join(TEMP_DIR, f"clone_{b_id}{ext}")
        with open(tmp, "wb") as f:
            shutil.copyfileobj(ses_dosyasi.file, f)

        boyut_mb = os.path.getsize(tmp) / (1024*1024)
        log.info(f"[Klonlama] {ses_dosyasi.filename} — {boyut_mb:.1f}MB")

        # ElevenLabs Add Voice endpoint
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(tmp, "rb") as f:
                r = await client.post(
                    "https://api.elevenlabs.io/v1/voices/add",
                    headers={"xi-api-key": ELEVENLABS_API_KEY},
                    data={"name": isim, "description": "VoiceFlow Studio klonu"},
                    files={"files": (ses_dosyasi.filename, f, "audio/mpeg")},
                )

        os.remove(tmp)

        if r.status_code == 200:
            voice_id = r.json().get("voice_id")
            log.info(f"[Klonlama] Başarılı → {voice_id}")
            return JSONResponse({
                "basari": True,
                "voice_id": voice_id,
                "isim": isim,
                "mesaj": "Ses başarıyla klonlandı. Artık bu sesi seçebilirsiniz."
            })
        elif r.status_code == 422:
            return JSONResponse({
                "hata": "Ses dosyası çok kısa veya kalitesi düşük. En az 30 saniyelik net ses yükleyin.",
                "detay": r.text[:200]
            }, status_code=422)
        elif r.status_code == 401:
            return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=401)
        else:
            detay = r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:200]
            # Free plan kontrolü
            if "quota" in str(detay).lower() or "limit" in str(detay).lower():
                return JSONResponse({
                    "hata": "Ses klonlama için ElevenLabs Creator planı gereklidir (22$/ay).",
                    "plan_gerekli": True
                }, status_code=402)
            return JSONResponse({"hata": f"ElevenLabs hatası: {detay}"}, status_code=r.status_code)

    except Exception as e:
        log.error(f"[Klonlama Hata] {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.delete("/api/ses_sil/{voice_id}")
async def ses_sil(voice_id: str):
    """Klonlanmış sesi ElevenLabs'tan siler."""
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.delete(
                f"https://api.elevenlabs.io/v1/voices/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
            )
        if r.status_code == 200:
            return JSONResponse({"basari": True})
        return JSONResponse({"hata": r.text[:100]}, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.get("/api/sesler/")
async def sesler_listele():
    """Hesaptaki tüm sesleri listeler (klonlar dahil)."""
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
            )
        if r.status_code == 200:
            sesler = r.json().get("voices", [])
            return JSONResponse({
                "sesler": [
                    {
                        "voice_id": v["voice_id"],
                        "isim": v["name"],
                        "kategori": v.get("category", "premade"),
                        "klonlanmis": v.get("category") == "cloned",
                    }
                    for v in sesler
                ]
            })
        return JSONResponse({"hata": r.text[:100]}, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.get("/api/kota/")
async def kota_kontrol():
    """ElevenLabs karakter kotasını kontrol eder."""
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
            )
        if r.status_code == 200:
            d = r.json()
            kullanilan = d.get("character_count", 0)
            limit      = d.get("character_limit", 10000)
            kalan      = limit - kullanilan
            return JSONResponse({
                "tier": d.get("tier", "free"),
                "kullanilan": kullanilan,
                "limit": limit,
                "kalan": kalan,
                "yuzde": round(kullanilan / limit * 100, 1) if limit else 0,
                "klonlama_destekli": d.get("can_use_instant_voice_cloning", False),
            })
        return JSONResponse({"hata": r.text[:100]}, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)
async def words_al(dosya_adi: str):
    """TikTok modu için kelime bazlı zaman damgalarını döndürür."""
    base = dosya_adi.replace('.srt', '')
    words_path = os.path.join(OUTPUT_DIR, base + "_words.json")
    if not os.path.exists(words_path):
        return JSONResponse({"hata": "Kelime verisi bulunamadı."}, status_code=404)
    with open(words_path, encoding="utf-8") as f:
        return JSONResponse({"words": json.load(f)})


@app.post("/api/kendi_sesiyle_dublaj/")
async def kendi_sesiyle_dublaj(
    video: UploadFile = File(...),
    hedef_dil: str = Form("EN"),
    kaynak_dil: str = Form("auto"),
):
    """
    Tek tıkla: Videonun kendi sesini klonla → hedef dilde konuştur → videoya karıştır.
    Akış: Video → FFmpeg ses çıkar → Deepgram transkript → DeepL çeviri
          → ElevenLabs Instant Clone → klonlanan ses TTS → FFmpeg mix
    """
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": "ElevenLabs API key eksik"}, status_code=500)
    if not DEEPGRAM_API_KEY:
        return JSONResponse({"hata": "Deepgram API key eksik"}, status_code=500)

    b_id = uuid.uuid4().hex[:8]
    gecici = os.path.join(TEMP_DIR, b_id)
    os.makedirs(gecici, exist_ok=True)

    ext = os.path.splitext(video.filename or "video.mp4")[1] or ".mp4"
    video_path = os.path.join(gecici, f"video{ext}")
    ses_path   = os.path.join(gecici, "ses_orj.mp3")
    out_path   = os.path.join(OUTPUT_DIR, f"sonuc_{b_id}.mp4")
    klonlanan_voice_id = None

    try:
        # 1. Videoyu kaydet
        with open(video_path, "wb") as f:
            shutil.copyfileobj(video.file, f)
        log.info(f"[KendiSesi] Video kaydedildi: {os.path.getsize(video_path)//1024}KB")

        # 2. Sesi çıkar (FFmpeg)
        ffmpeg = _ffmpeg_path()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", video_path,
            "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", ses_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(ses_path):
            return JSONResponse({"hata": f"Ses çıkarılamadı: {err.decode()[:200]}"}, status_code=500)
        log.info(f"[KendiSesi] Ses çıkarıldı: {os.path.getsize(ses_path)//1024}KB")

        # 3. Deepgram transkript
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(ses_path, "rb") as f:
                dg_r = await client.post(
                    "https://api.deepgram.com/v1/listen?model=nova-2&detect_language=true&punctuate=true",
                    headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/mpeg"},
                    content=f.read(),
                )
        if dg_r.status_code != 200:
            return JSONResponse({"hata": f"Transkript hatası: {dg_r.text[:200]}"}, status_code=500)

        transkript = dg_r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        if not transkript.strip():
            return JSONResponse({"hata": "Videoda konuşma sesi algılanamadı"}, status_code=400)
        log.info(f"[KendiSesi] Transkript: {transkript[:100]}")

        # 4. DeepL çeviri
        metin_final = transkript
        if DEEPL_API_KEY:
            hedef_deepl = DEEPL_DILLER.get(hedef_dil.upper(), hedef_dil.upper())
            async with httpx.AsyncClient(timeout=30.0) as client:
                dl_r = await client.post(
                    f"{_deepl_base_url()}/v2/translate",
                    headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
                    json={"text": [transkript], "target_lang": hedef_deepl},
                )
            if dl_r.status_code == 200:
                metin_final = dl_r.json()["translations"][0]["text"]
                log.info(f"[KendiSesi] Çeviri: {metin_final[:100]}")

        # 5. Ses klonla (ElevenLabs Instant Clone)
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(ses_path, "rb") as f:
                klon_r = await client.post(
                    "https://api.elevenlabs.io/v1/voices/add",
                    headers={"xi-api-key": ELEVENLABS_API_KEY},
                    data={"name": f"KendiSes_{b_id}", "description": "VoiceFlow otomatik klon"},
                    files={"files": ("ses.mp3", f, "audio/mpeg")},
                )

        if klon_r.status_code != 200:
            # Klonlama başarısız — plan kısıtı olabilir, varsayılan ses kullan
            log.warning(f"[KendiSesi] Klonlama başarısız ({klon_r.status_code}), varsayılan ses kullanılıyor")
            klonlanan_voice_id = "nPczCjzI2devNBz1zQrb"  # Brian
        else:
            klonlanan_voice_id = klon_r.json().get("voice_id")
            log.info(f"[KendiSesi] Ses klonlandı: {klonlanan_voice_id}")

        # 6. Klonlanan sesle TTS üret
        tts_path = os.path.join(gecici, "tts_cikti.mp3")
        async with httpx.AsyncClient(timeout=120.0) as client:
            tts_r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{klonlanan_voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={
                    "text": metin_final,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.85}
                },
            )
        if tts_r.status_code != 200:
            return JSONResponse({"hata": f"TTS hatası: {tts_r.text[:200]}"}, status_code=500)

        with open(tts_path, "wb") as f:
            f.write(tts_r.content)
        log.info(f"[KendiSesi] TTS üretildi: {len(tts_r.content)//1024}KB")

        # 7. FFmpeg — orijinal video + yeni ses karıştır
        proc2 = await asyncio.create_subprocess_exec(
            ffmpeg, "-y",
            "-i", video_path,
            "-i", tts_path,
            "-filter_complex", "[0:a]volume=0.05[orig];[1:a]volume=1.0[dub];[orig][dub]amix=inputs=2:duration=longest[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            out_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err2 = await proc2.communicate()
        if proc2.returncode != 0:
            return JSONResponse({"hata": f"Video birleştirme hatası: {err2.decode()[-300:]}"}, status_code=500)

        log.info(f"[KendiSesi] Tamamlandı: {out_path}")

        # Klonlanan sesi ElevenLabs'tan sil (temizlik)
        if klonlanan_voice_id and klonlanan_voice_id != "nPczCjzI2devNBz1zQrb":
            try:
                async with httpx.AsyncClient() as c:
                    await c.delete(
                        f"https://api.elevenlabs.io/v1/voices/{klonlanan_voice_id}",
                        headers={"xi-api-key": ELEVENLABS_API_KEY}
                    )
            except Exception:
                pass

        return JSONResponse({
            "basari": True,
            "dosya": os.path.basename(out_path),
            "transkript": transkript[:200],
            "ceviri": metin_final[:200],
            "hedef_dil": hedef_dil,
        })

    except Exception as e:
        log.error(f"[KendiSesi Hata] {e}", exc_info=True)
        return JSONResponse({"hata": str(e)}, status_code=500)
    finally:
        # Geçici dosyaları temizle
        try:
            shutil.rmtree(gecici, ignore_errors=True)
        except Exception:
            pass


@app.post("/api/bakiye_kontrol/")
async def bakiye_kontrol(request: Request):
    """
    Kullanıcının işlem için yeterli kredisi var mı kontrol eder.
    Supabase entegrasyonu gelince gerçek bakiye çekilecek.
    Şimdilik: giriş yapılmamışsa serbest, yapılmışsa mock bakiye.
    """
    try:
        body = await request.json()
        modul   = body.get("modul", "desifre")
        user_id = body.get("user_id")
    except Exception:
        modul   = "desifre"
        user_id = None

    # Modül başına kredi maliyeti
    maliyet = {"desifre": 5, "altyazi": 8, "seslendirme": 20, "metinden_sese": 3}.get(modul, 5)

    # Giriş yapılmamışsa demo mod — serbest
    if not user_id:
        return JSONResponse({"yeterli": True, "bakiye": 999, "maliyet": maliyet, "mod": "demo"})

    # TODO: Supabase'den gerçek bakiye çek
    # Şimdilik mock: 15 kredi var gibi davran
    mock_bakiye = 15
    yeterli = mock_bakiye >= maliyet

    return JSONResponse({
        "yeterli": yeterli,
        "bakiye": mock_bakiye,
        "maliyet": maliyet,
        "mod": "supabase_mock"
    })


@app.post("/api/normalize_test/")
async def normalize_test_endpoint(
    metin: str = Form(...),
    dil: str   = Form("en"),
):
    """Metnin TTS öncesi nasıl normalize edileceğini gösterir."""
    normalize_edilmis = metin_normalize(metin, dil)
    farklar = []
    # Hangi kelimeler değişti?
    orijinal_kelimeler = metin.split()
    yeni_kelimeler     = normalize_edilmis.split()
    for i, (o, y) in enumerate(zip(orijinal_kelimeler, yeni_kelimeler)):
        if o.lower() != y.lower():
            farklar.append({"orijinal": o, "normalize": y})
    return JSONResponse({
        "orijinal":         metin,
        "normalize_edilmis": normalize_edilmis,
        "degisen_kelimeler": farklar,
        "dil": dil,
    })


@app.post("/api/url_yukle/")
async def url_yukle(
    url: str = Form(...),
    kalite: str = Form("best"),  # best | 1080 | 720 | 480 | 360 | audio_only
):
    """
    URL üzerinden video indirir (YouTube, Drive, Dropbox, direkt link).
    kalite: best | 1080 | 720 | 480 | 360 | audio_only
    """
    # Kalite → yt-dlp format string
    kalite_fmt = {
        "best":       "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "1080":       "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "720":        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
        "480":        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
        "360":        "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
        "audio_only": "bestaudio[ext=m4a]/bestaudio/best",
    }.get(kalite, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")

    try:
        import yt_dlp
    except ImportError:
        # yt-dlp yoksa direkt HTTP indirmeyi dene
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    return JSONResponse({"hata": f"İndirme başarısız: HTTP {r.status_code}"}, status_code=400)
                ct = r.headers.get("content-type","")
                ext = "mp4" if "video" in ct else "mp3" if "audio" in ct else "mp4"
                b_id = uuid.uuid4().hex[:8]
                dosya_yolu = os.path.join(TEMP_DIR, f"url_{b_id}.{ext}")
                with open(dosya_yolu, "wb") as f:
                    f.write(r.content)
                boyut = os.path.getsize(dosya_yolu) / (1024*1024)
                return JSONResponse({
                    "basari": True,
                    "dosya_yolu": dosya_yolu,
                    "dosya_adi": os.path.basename(dosya_yolu),
                    "baslik": url.split("/")[-1][:50],
                    "boyut_mb": round(boyut, 1),
                    "temp_id": b_id,
                })
        except Exception as e:
            return JSONResponse({"hata": f"İndirme hatası: {str(e)[:200]}"}, status_code=500)

    b_id = uuid.uuid4().hex[:8]
    cikti_sablonu = os.path.join(TEMP_DIR, f"url_{b_id}.%(ext)s")
    ydl_opts = {
        'outtmpl': cikti_sablonu,
        'format': kalite_fmt,
        'noplaylist': True,
        'quiet': True,
        'max_filesize': MAX_DOSYA_MB * 1024 * 1024,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')[:50]
            ext   = info.get('ext', 'mp4')
            dosya_yolu = os.path.join(TEMP_DIR, f"url_{b_id}.{ext}")
            if not os.path.exists(dosya_yolu):
                for f in os.listdir(TEMP_DIR):
                    if f.startswith(f"url_{b_id}"):
                        dosya_yolu = os.path.join(TEMP_DIR, f); break
            if not os.path.exists(dosya_yolu):
                return JSONResponse({"hata": "İndirme tamamlanamadı."}, status_code=500)
            boyut_mb = os.path.getsize(dosya_yolu) / (1024*1024)
            return JSONResponse({
                "basari": True, "dosya_yolu": dosya_yolu,
                "dosya_adi": os.path.basename(dosya_yolu),
                "baslik": title, "boyut_mb": round(boyut_mb, 1), "temp_id": b_id,
            })
    except Exception as e:
        err = str(e)
        if "Private" in err or "unavailable" in err:
            return JSONResponse({"hata": "Video özel veya erişilemiyor."}, status_code=403)
        return JSONResponse({"hata": f"İndirme hatası: {err[:200]}"}, status_code=500)


@app.get("/api/confidence/{dosya_adi}")
async def confidence_al(dosya_adi: str):
    """SRT ile birlikte kaydedilen kelime güven skorlarını döndürür."""
    base = dosya_adi.replace('.srt', '')
    conf_path = os.path.join(OUTPUT_DIR, base + "_confidence.json")
    if not os.path.exists(conf_path):
        return JSONResponse({"hata": "Confidence verisi bulunamadı."}, status_code=404)
    with open(conf_path, encoding="utf-8") as f:
        return JSONResponse(json.load(f))


@app.post("/api/speaker_map/")
async def speaker_map_kaydet(
    dosya_adi: str = Form(...),
    speaker_map: str = Form(...),  # JSON string: {"0": "voice_id_1", "1": "voice_id_2"}
):
    """Her konuşmacı için ses ID'si eşleştirmesini kaydeder."""
    try:
        harita = json.loads(speaker_map)
        map_path = os.path.join(OUTPUT_DIR, dosya_adi.replace('.srt','').replace('.mp4','') + "_speaker_map.json")
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(harita, f, ensure_ascii=False)
        return JSONResponse({"basari": True, "kayit": map_path})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.get("/api/speaker_map/{dosya_adi}")
async def speaker_map_al(dosya_adi: str):
    """Kaydedilmiş konuşmacı ses haritasını döndürür."""
    map_path = os.path.join(OUTPUT_DIR, dosya_adi.replace('.srt','').replace('.mp4','') + "_speaker_map.json")
    if not os.path.exists(map_path):
        return JSONResponse({})
    with open(map_path, encoding="utf-8") as f:
        return JSONResponse(json.load(f))


@app.post("/api/kelime_oneri/")
async def kelime_oneri(
    kelime: str = Form(...),
    baglam: str = Form(""),
    dil: str    = Form("tr"),
):
    """
    Düşük güvenli kelime için:
    1. Önce influencer / sokak jargonu sözlüğüne bak
    2. Fonetik alternatifler üret
    3. Baglam varsa Claude API ile akıllı öneri al
    """
    temiz = kelime.lower().strip('.,?!;:\'"-')

    # ── İNFLUENCER / SOKAK JARGONU SÖZLÜĞÜ ──
    jargon_map = {
        # Türkçe sokak / influencer
        "naber": ["ne haber", "nasılsın", "ne var ne yok"],
        "nbr": ["ne haber", "nasılsın"],
        "kpk": ["kafayı patlattım", "çıldırdım"],
        "ya": ["ya", "yani", "anlıyor musun"],
        "aga": ["ağabey", "arkadaş", "bro"],
        "agam": ["ağabeyim", "dostum"],
        "kanka": ["arkadaş", "dostum", "kardeşim"],
        "knk": ["kanka", "arkadaş"],
        "bro": ["bro", "kardeş", "arkadaş"],
        "reis": ["reis", "patron", "abi"],
        "çakmak": ["çakmak", "yakalamak"],
        "saçmalamak": ["saçmalamak", "abartmak"],
        "efsane": ["efsane", "harika", "muhteşem"],
        "leş": ["berbat", "rezil", "korkunç"],
        "süper": ["süper", "harika", "mükemmel"],
        "acayip": ["acayip", "çok", "inanılmaz"],
        "bi": ["bir", "biraz"],
        "şey": ["şey", "yani", "hmm"],
        "işte": ["işte", "yani"],
        "hani": ["hani", "yani", "bilirsin"],
        "tamam mı": ["tamam mı", "anladın mı", "değil mi"],
        "falan": ["falan", "gibi şeyler", "ve benzeri"],
        "filan": ["filan", "gibi şeyler"],
        "mk": ["mükemmel", "muhteşem"],
        "mq": ["mucize", "harika"],
        "amk": ["ya", "vay be"],
        "aq": ["ah ya", "vay be"],
        "len": ["lan", "hey"],
        "deli": ["deli", "çılgın", "inanılmaz"],
        "çıldırtıcı": ["çıldırtıcı", "inanılmaz derecede güzel"],
        "yok artık": ["inanılmaz", "olmaz böyle şey"],
        "tabi": ["tabii", "elbette", "kesinlikle"],
        "bi ara": ["bir ara", "yakında", "zaman zaman"],
        "sıkıcı": ["sıkıcı", "sıradan", "monoton"],
        "cool": ["havalı", "cool", "şık"],
        "vibe": ["atmosfer", "hava", "enerji"],
        # İngilizce sokak / influencer
        "lowkey": ["low key", "biraz", "gizlice"],
        "highkey": ["high key", "açıkçası", "gerçekten"],
        "ngl": ["not gonna lie", "yalan olmaz"],
        "fr": ["for real", "gerçekten"],
        "frfr": ["for real for real", "gerçekten"],
        "ong": ["on god", "yemin ederim"],
        "cap": ["lie", "yalan"],
        "no cap": ["no lie", "yalan değil"],
        "bussin": ["really good", "çok iyi"],
        "slay": ["slay", "harika görünüyor"],
        "bet": ["okay", "tamam"],
        "fam": ["family", "arkadaş"],
        "bro": ["brother", "arkadaş"],
        "bruh": ["bro", "ya", "yok artık"],
        "goat": ["greatest of all time", "en iyi"],
        "w": ["win", "kazanç"],
        "l": ["loss", "kayıp"],
        "fire": ["amazing", "harika"],
        "lit": ["amazing", "harika"],
        "sus": ["suspicious", "şüpheli"],
        "ratio": ["got ratioed", "beğeni az yorum çok"],
        "mid": ["mediocre", "sıradan"],
        "based": ["based", "gerçekçi"],
        "rent free": ["always on my mind", "aklımdan çıkmıyor"],
        "understood the assignment": ["did great", "işini bildi"],
        "it's giving": ["it seems like", "sanki"],
        "no shot": ["no way", "imkansız"],
        "send it": ["go for it", "yap"],
        "rizz": ["charisma", "karizmatik"],
        "delulu": ["delusional", "hayalperest"],
        "touch grass": ["go outside", "dışarı çık"],
        "main character": ["main character energy", "baş karakter"],
        "era": ["phase", "dönem"],
        "iykyk": ["if you know you know", "bilenler bilir"],
        "istg": ["i swear to god", "yemin ederim"],
        "omg": ["oh my god", "tanrım"],
        "omfg": ["oh my god", "tanrım"],
        "lmao": ["laughing", "güldüm"],
        "lol": ["laughing out loud", "haha"],
        "tbh": ["to be honest", "dürüst olmak gerekirse"],
        "imo": ["in my opinion", "bence"],
        "nvm": ["never mind", "boşver"],
        "idk": ["i don't know", "bilmiyorum"],
        "idc": ["i don't care", "umurumda değil"],
        "smh": ["shaking my head", "hayal kırıklığı"],
        "rn": ["right now", "şu an"],
        "asap": ["as soon as possible", "en kısa sürede"],
        # Ses/müzik/içerik üretici jargonu
        "content": ["içerik", "video"],
        "vlog": ["vlog", "günlük video"],
        "collab": ["iş birliği", "ortak çalışma"],
        "drop": ["yayınla", "çıkar"],
        "trending": ["gündemde", "popüler"],
        "viral": ["viral", "yayıldı"],
        "algorithm": ["algoritma"],
        "engagement": ["etkileşim"],
        "repost": ["paylaş", "yeniden paylaş"],
    }

    # Fonetik dönüşüm (Türkçe için)
    def fonetik_tr(k):
        return (k.replace('ş','sh').replace('ç','ch')
                 .replace('ğ','').replace('ı','i')
                 .replace('ö','oe').replace('ü','ue')
                 .replace('İ','i').replace('Ş','Sh'))

    oneriler = []

    # 1. Jargon sözlüğünde bul
    if temiz in jargon_map:
        oneriler = jargon_map[temiz]
    else:
        # 2. Kısmi eşleşme — başlangıç
        eslesme = [(k,v) for k,v in jargon_map.items() if temiz.startswith(k) or k.startswith(temiz)]
        if eslesme:
            oneriler = eslesme[0][1]

    # 3. Fonetik alternatifler ekle
    fonetik = fonetik_tr(kelime)
    if fonetik != kelime and fonetik not in oneriler:
        oneriler.append(fonetik)

    # 4. Hece bölme alternatifi
    if len(kelime) > 4:
        heceli = "-".join(kelime[i:i+3] for i in range(0, len(kelime), 3)).strip("-")
        if heceli not in oneriler:
            oneriler.append(heceli)

    # 5. Baglam varsa Claude API ile akıllı öneri
    if baglam and len(baglam) > 5 and ELEVENLABS_API_KEY:
        try:
            ai_prompt = f"""Sen bir transkript düzeltme asistanısın.
Vidyoda geçen "{kelime}" kelimesi yanlış tanınmış olabilir.
Bağlam: "{baglam}"
Bu bağlamda "{kelime}" yerine gelebilecek 3 Türkçe kelime öner.
Sadece JSON liste döndür, açıklama yapma: ["öneri1", "öneri2", "öneri3"]"""

            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": os.getenv("ANTHROPIC_API_KEY",""),
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 100,
                        "messages": [{"role":"user","content": ai_prompt}]
                    },
                    timeout=8.0,
                )
                if r.status_code == 200:
                    text = r.json()["content"][0]["text"].strip()
                    import json as _json
                    ai_oneriler = _json.loads(text)
                    for ao in ai_oneriler:
                        if ao not in oneriler:
                            oneriler.insert(0, ao)  # AI önerilerini başa koy
        except Exception as ex:
            log.debug(f"[AI Öneri] {ex}")

    if not oneriler:
        oneriler = [kelime, kelime.capitalize(), kelime.upper()]

    return JSONResponse({
        "kelime": kelime,
        "oneriler": list(dict.fromkeys(oneriler))[:6],  # max 6 öneri, unique
        "jargon": temiz in jargon_map,
    })


@app.get("/api/segmentler/{dosya_adi}")
async def segmentleri_listele(dosya_adi: str):
    yol = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)
    try:
        with open(yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())
        return JSONResponse({
            "dosya": dosya_adi,
            "segment_sayisi": len(bloklar),
            "segmentler": [
                {"no": b["no"], "baslangic": b["baslangic"], "bitis": b["bitis"],
                 "zaman_str": f"{_saniye_srt_global(b['baslangic'])} --> {_saniye_srt_global(b['bitis'])}",
                 "metin": b["metin"], "sure": round(b["bitis"] - b["baslangic"], 3)}
                for b in bloklar
            ]
        })
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.get("/docs-api", response_class=HTMLResponse)
async def docs_page():
    if os.path.exists("api-docs.html"):
        with open("api-docs.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Dokümantasyon bulunamadı.</h1>")


# ============================================================
# SES ÖNİZLEME
# ============================================================
@app.post("/api/ses_onizle/")
async def ses_onizle(
    ses_id: str = Form(...),
    metin: str  = Form("Merhaba, ben VoiceFlow Studio. Bu benim sesim."),
):
    """
    ElevenLabs preview endpoint ile sesin küçük bir örneğini üretir.
    Karakter kotasından düşmez — sadece önizleme.
    """
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)

    # Metni 100 karakterle sınırla — preview için yeterli
    onizleme_metni = metin[:100]

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ses_id}/stream"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY,
    }
    data = {
        "text": onizleme_metni,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
        },
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=data, headers=headers, timeout=30.0)
        if r.status_code == 200:
            # Geçici dosyaya kaydet, serve et
            b_id = uuid.uuid4().hex[:8]
            onizleme_yol = os.path.join(TEMP_DIR, f"onizleme_{b_id}.mp3")
            with open(onizleme_yol, "wb") as f:
                f.write(r.content)
            return FileResponse(onizleme_yol, media_type="audio/mpeg",
                                filename=f"preview_{ses_id}.mp3")
        return JSONResponse({"hata": f"ElevenLabs {r.status_code}: {r.text[:100]}"}, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


# ============================================================
# MAGIC CUT — DOLGU KELİME SİLME
# ============================================================
DOLGU_KELIMELER = {
    "tr": ["hmm", "hm", "şey", "ıı", "ee", "eee", "şeyden", "yani", "işte", "falan",
           "filan", "hani", "neyse", "yok", "ha", "aa", "öf", "uh", "um"],
    "en": ["um", "uh", "hmm", "hm", "like", "you know", "i mean", "basically",
           "literally", "actually", "sort of", "kind of", "right", "okay so"],
    "de": ["äh", "ähm", "hm", "hmm", "naja", "also", "ja", "ne"],
    "fr": ["euh", "eh", "hm", "hmm", "ben", "voilà", "bah"],
}

@app.post("/api/platform_boyutlandir/")
async def platform_boyutlandir(
    dosya_adi: str = Form(...),
    platform: str = Form("tiktok"),  # tiktok | youtube | instagram_post | instagram_story | original
):
    """
    FFmpeg ile videoyu platforma göre boyutlandır.
    - tiktok / instagram_story: 9:16 dikey (1080x1920)
    - youtube: 16:9 yatay (1920x1080)
    - instagram_post: 1:1 kare (1080x1080)
    """
    kaynak = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(kaynak):
        return JSONResponse({"hata": "Dosya bulunamadı"}, status_code=404)

    PLATFORM_AYAR = {
        "tiktok":           {"w": 1080, "h": 1920, "etiket": "TikTok_9x16"},
        "instagram_story":  {"w": 1080, "h": 1920, "etiket": "Instagram_Story"},
        "youtube":          {"w": 1920, "h": 1080, "etiket": "YouTube_16x9"},
        "instagram_post":   {"w": 1080, "h": 1080, "etiket": "Instagram_Post"},
        "shorts":           {"w": 1080, "h": 1920, "etiket": "Shorts_9x16"},
    }

    if platform == "original":
        return JSONResponse({"basari": True, "dosya": dosya_adi, "platform": "original"})

    ayar = PLATFORM_AYAR.get(platform)
    if not ayar:
        return JSONResponse({"hata": f"Bilinmeyen platform: {platform}"}, status_code=400)

    b_id = uuid.uuid4().hex[:8]
    cikti_adi = f"sonuc_{b_id}_{ayar['etiket']}.mp4"
    cikti = os.path.join(OUTPUT_DIR, cikti_adi)

    w, h = ayar["w"], ayar["h"]

    # FFmpeg: scale + crop ile hedef boyuta getir, siyah şerit ekle
    # scale: en büyük kenarı hedefle, crop: tam boyuta getir
    ffmpeg = _ffmpeg_path()
    filtre = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    )

    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-i", kaynak,
        "-vf", filtre,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        cikti,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()

    if proc.returncode != 0:
        log.error(f"[PlatformResize] Hata: {err.decode()[-300:]}")
        return JSONResponse({"hata": "Video boyutlandırılamadı"}, status_code=500)

    log.info(f"[PlatformResize] {platform} → {cikti_adi}")
    return JSONResponse({
        "basari": True,
        "dosya": cikti_adi,
        "platform": platform,
        "boyut": f"{w}x{h}",
        "etiket": ayar["etiket"]
    })


@app.post("/api/klip_kes/")
async def klip_kes(
    dosya_adi: str    = Form(...),
    baslangic: float  = Form(...),
    bitis: float      = Form(...),
):
    """FFmpeg ile videodan belirli bir bölümü keser."""
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": "FFmpeg yüklü değil"}, status_code=500)

    giris = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(giris):
        return JSONResponse({"hata": "Dosya bulunamadı"}, status_code=404)

    b_id  = uuid.uuid4().hex[:8]
    ext   = os.path.splitext(dosya_adi)[1] or ".mp4"
    cikti = f"klip_{b_id}{ext}"
    cikti_yol = os.path.join(OUTPUT_DIR, cikti)

    sure = max(0.5, bitis - baslangic)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(baslangic),
        "-i", giris,
        "-t", str(sure),
        "-c:v", "copy", "-c:a", "aac",
        cikti_yol
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        log.info(f"[Klip Kes] {dosya_adi} {baslangic:.1f}s-{bitis:.1f}s → {cikti}")
        return JSONResponse({"cikti": cikti, "sure": sure})
    log.error(f"[Klip Kes Hata] {result.stderr[-300:]}")
    return JSONResponse({"hata": "Klip kesilemedi"}, status_code=500)


@app.post("/api/magic_cut/")
async def magic_cut(
    srt_dosya_adi: str = Form(...),
    dil: str           = Form("tr"),
    min_confidence: float = Form(0.5),  # Bu skoru altındaki dolgu kelimeleri sil
):
    """
    SRT dosyasındaki düşük güvenli dolgu kelimeleri tespit edip
    o segmentleri işaretler veya siler.
    """
    srt_yol  = os.path.join(OUTPUT_DIR, srt_dosya_adi)
    conf_yol = os.path.join(OUTPUT_DIR, srt_dosya_adi.replace('.srt', '_confidence.json'))

    if not os.path.exists(srt_yol):
        return JSONResponse({"hata": "SRT bulunamadı."}, status_code=404)

    try:
        with open(srt_yol, encoding="utf-8") as f:
            bloklar = _srt_parse(f.read())

        # Confidence verisi varsa kullan
        conf_data = {}
        if os.path.exists(conf_yol):
            with open(conf_yol, encoding="utf-8") as f:
                conf_data = json.load(f)

        dolgu_listesi = DOLGU_KELIMELER.get(dil, DOLGU_KELIMELER["en"])
        silinen = []
        kalan   = []

        for blok in bloklar:
            metin  = re.sub(r"\[Konuşmacı \d+\]:\s*", "", blok["metin"]).strip().lower()
            kelimeler = metin.split()

            # Tek kelimeli segment dolgu kelimesiyse sil
            if len(kelimeler) == 1 and kelimeler[0] in dolgu_listesi:
                silinen.append(blok["no"])
                continue

            # Confidence düşükse ve dolgu kelimesiyse sil
            if len(kelimeler) <= 2:
                conf = min(conf_data.get(k, 1.0) for k in kelimeler)
                if conf < min_confidence and any(k in dolgu_listesi for k in kelimeler):
                    silinen.append(blok["no"])
                    continue

            kalan.append(blok)

        if silinen:
            with open(srt_yol, "w", encoding="utf-8") as f:
                f.write(_srt_serialize(kalan))
            log.info(f"[Magic Cut] {len(silinen)} dolgu silindi, {len(kalan)} segment kaldı")

        return JSONResponse({
            "basari": True,
            "silinen_segment_sayisi": len(silinen),
            "kalan_segment_sayisi": len(kalan),
            "silinen_segmentler": silinen[:20],  # ilk 20
        })
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


# ============================================================
# ARKA PLAN GÜRÜLTÜ GİDERME
# ============================================================
@app.post("/api/gurultu_gider/")
async def gurultu_gider(
    arka_plan: BackgroundTasks,
    dosya_adi: str   = Form(...),   # sonuc_xxx.mp4 veya ses dosyası
    seviye: str      = Form("orta"), # hafif | orta | guclu
):
    """
    FFmpeg anlmdn (Adaptive Non-Local Means Denoiser) filtresi ile
    arka plan gürültüsünü giderir. Dolby gerekmiyor — tamamen ücretsiz.
    """
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": _hata_mesaji("ffmpeg_yok")}, status_code=500)

    giris_yol  = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(giris_yol):
        return JSONResponse({"hata": "Dosya bulunamadı."}, status_code=404)

    # Seviyeye göre filtre parametreleri
    seviye_params = {
        "hafif": "s=7:p=0.01:r=0.001:pdl=0.001",
        "orta":  "s=7:p=0.03:r=0.003:pdl=0.003",
        "guclu": "s=7:p=0.09:r=0.009:pdl=0.009",
    }
    filtre = seviye_params.get(seviye, seviye_params["orta"])

    b_id = uuid.uuid4().hex[:8]
    islem_id = f"gurultu_{b_id}"
    islem_durumlari[islem_id] = {"durum": "Gürültü gideriliyor...", "yuzde": 20}

    # Çıktı dosyası
    ext      = os.path.splitext(dosya_adi)[1]
    cikti_ad = dosya_adi.replace(ext, f"_temiz{ext}")
    cikti_yol = os.path.join(OUTPUT_DIR, cikti_ad)

    async def _denoise():
        try:
            is_video = ext.lower() in [".mp4", ".mov", ".avi", ".mkv"]

            if is_video:
                cmd = [
                    "ffmpeg", "-y", "-i", giris_yol,
                    "-af", f"anlmdn={filtre}",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    cikti_yol
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-i", giris_yol,
                    "-af", f"anlmdn={filtre}",
                    "-c:a", "libmp3lame", "-q:a", "2",
                    cikti_yol
                ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                islem_durumlari[islem_id] = {
                    "durum": "Tamamlandı",
                    "yuzde": 100,
                    "cikti_dosya": cikti_ad,
                }
                log.info(f"[Gürültü Gider] ✓ {cikti_yol}")
            else:
                islem_durumlari[islem_id] = {"durum": "Hata: FFmpeg başarısız.", "yuzde": 0}
                log.error(f"[Gürültü Gider] {result.stderr[-400:]}")
        except Exception as e:
            islem_durumlari[islem_id] = {"durum": f"Hata: {e}", "yuzde": 0}

    arka_plan.add_task(_denoise)
    return JSONResponse({
        "islem_id": islem_id,
        "dosya_adi": dosya_adi,
        "seviye": seviye,
        "beklenen_cikti": cikti_ad,
    })


@app.get("/creator", response_class=HTMLResponse)
async def creator_page():
    if os.path.exists("creator.html"):
        with open("creator.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Creator sayfası bulunamadı.</h1>")


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    if os.path.exists("index.html"):
        with open("index.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Uygulama bulunamadi.</h1>")