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
DEEPGRAM_API_KEY    = os.getenv("DEEPGRAM_API_KEY")
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY")
DEEPL_API_KEY       = os.getenv("DEEPL_API_KEY")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
STRIPE_SECRET_KEY   = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY      = os.getenv("RESEND_API_KEY", "")
RESEND_FROM         = os.getenv("RESEND_FROM", "Lumnex <onboarding@resend.dev>")
SUPABASE_URL        = os.getenv("SUPABASE_URL", "https://biqsljanevkxrgpdxard.supabase.co")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# Stripe fiyat ID'leri — Stripe Dashboard'dan alınacak
STRIPE_PRICES = {
    "creator":  os.getenv("STRIPE_PRICE_CREATOR",  ""),   # $14/mo
    "studio":   os.getenv("STRIPE_PRICE_STUDIO",   ""),   # $34/mo
    "business": os.getenv("STRIPE_PRICE_BUSINESS", ""),   # $89/mo
}

# ── Loglama ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voiceflow")

# ── Dosya limitleri ──
MAX_DOSYA_MB   = int(os.getenv("MAX_DOSYA_MB", "500"))    # 500MB varsayılan
MAX_SURE_DAKIKA = int(os.getenv("MAX_SURE_DAKIKA", "30")) # 30 dakika

app = FastAPI(title="Lumnex API", version="1.0.0")

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
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

@app.get("/", response_class=HTMLResponse)
async def root():
    landing = "landing.html" if os.path.exists("landing.html") else "index.html"
    if os.path.exists(landing):
        with open(landing, encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers=_NO_CACHE_HEADERS)
    return HTMLResponse("<h1>Lumnex API</h1><p>index.html bulunamadı.</p>")

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
        "versiyon": "2.0.0",
    }


@app.post("/api/kayit_email/")
async def kayit_email(
    arka_plan: BackgroundTasks,
    email: str = Form(...),
    isim: str  = Form(""),
):
    """
    Kullanıcı kayıt olunca:
    1. Hoşgeldin emaili anında gönder
    2. Gün 2 + Gün 5 hatırlatmalarını arka planda zamanla
    """
    if not email or "@" not in email:
        return JSONResponse({"hata": "Geçerli email gerekli"}, status_code=400)

    # Hoşgeldin emailini hemen gönder
    s = EMAIL_SABLONLAR["hosgeldin"]
    basari = await resend_email_gonder(email, s["konu"], s["html"], isim)

    # Hatırlatma zamanlayıcısını arka planda başlat
    arka_plan.add_task(hatirlatma_zamanlayici, email, isim)

    return JSONResponse({
        "basari": basari,
        "mesaj": "Hoşgeldin emaili gönderildi, hatırlatmalar zamanlandı"
    })


@app.post("/api/email_test/")
async def email_test(
    email: str = Form(...),
    sablon: str = Form("hosgeldin"),
):
    """Test amaçlı — belirtilen şablonu gönderir."""
    if sablon not in EMAIL_SABLONLAR:
        return JSONResponse({"hata": f"Şablon bulunamadı: {sablon}"}, status_code=404)
    s = EMAIL_SABLONLAR[sablon]
    basari = await resend_email_gonder(email, s["konu"], s["html"], "Test Kullanıcı")
    return JSONResponse({"basari": basari, "sablon": sablon})


# ── Email Endpoints ────────────────────────────────────────────

@app.on_event("startup")
async def baslangic_kontrolu():
    """Uygulama başlarken key kontrolü yap, eksikleri logla."""
    log.info("=" * 50)
    log.info("Lumnex başlatılıyor...")
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
            deepgram.listen.rest.v("1").transcribe_file, payload, options,
            timeout=httpx.Timeout(300.0, connect=15.0)   # 5 dk timeout — büyük dosyalar için
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

async def _deepl_chunk_cevir(client: httpx.AsyncClient, satirlar: list, hedef_dil: str,
                              placeholders: dict = None) -> list:
    deepl_hedef = DEEPL_DILLER.get(hedef_dil.upper(), hedef_dil.upper())
    try:
        r = await client.post(
            f"{_deepl_base_url()}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            json={"text": satirlar, "target_lang": deepl_hedef, "preserve_formatting": True},
            timeout=30.0,
        )
        r.raise_for_status()
        sonuc = [t["text"] for t in r.json()["translations"]]
        if placeholders:
            sonuc = _deepl_placeholder_geri_al(sonuc, placeholders)
        return sonuc
    except Exception as e:
        print(f"[DeepL Chunk Hata] {e}")
        # Placeholder'ları geri al (hata durumunda da)
        if placeholders:
            satirlar = _deepl_placeholder_geri_al(satirlar, placeholders)
        return satirlar

async def deepl_paralel_cevir_listesi(metin_listesi: list, hedef_dil: str) -> list:
    if not metin_listesi:
        return []
    # Glossary terimlerini DeepL'den önce koru
    glossary = _glossary_yukle()
    korunmus, placeholders = _deepl_glossary_koru(metin_listesi, glossary)
    if placeholders:
        print(f"[DeepL Glossary] {len(placeholders)} terim korunuyor → çeviri sonrası geri alınacak")
    # 100'den az segment → tek API çağrısı (en hızlı)
    if len(korunmus) <= 100:
        async with httpx.AsyncClient() as client:
            sonuc = await _deepl_chunk_cevir(client, korunmus, hedef_dil, placeholders)
        return sonuc
    # 100+ segment → 100'lük batch'ler halinde paralel
    CHUNK = 100
    chunks = [korunmus[i:i+CHUNK] for i in range(0, len(korunmus), CHUNK)]
    async with httpx.AsyncClient() as client:
        sonuclar = await asyncio.gather(*[_deepl_chunk_cevir(client, c, hedef_dil, placeholders) for c in chunks])
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
# EMAIL — RESEND ENTEGRASYONu
# ============================================================

EMAIL_SABLONLAR = {
"hosgeldin": {
    "konu": "Lumnex'ya Hoş Geldin! 🎬",
    "html": """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#09090b;font-family:'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:580px;margin:0 auto;padding:40px 24px;">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:36px;">
    <div style="width:10px;height:10px;border-radius:50%;background:#6366f1;"></div>
    <span style="font-size:18px;font-weight:800;color:#fafafa;">Lumnex</span>
  </div>
  <div style="background:linear-gradient(135deg,rgba(99,102,241,.15),rgba(168,85,247,.1));border:1px solid rgba(99,102,241,.3);border-radius:20px;padding:32px;margin-bottom:24px;text-align:center;">
    <div style="font-size:48px;margin-bottom:12px;">🎬</div>
    <h1 style="color:#fafafa;font-size:24px;font-weight:800;margin:0 0 10px;">Hoş Geldin, {isim}!</h1>
    <p style="color:#a1a1aa;font-size:15px;margin:0;line-height:1.6;">Videonuzu dakikalar içinde 100+ dile açmaya hazır mısın?</p>
  </div>
  <div style="background:#18181b;border:1px solid rgba(34,197,94,.3);border-radius:14px;padding:20px;margin-bottom:20px;">
    <span style="font-size:28px;">🎁</span>
    <div style="font-size:15px;font-weight:700;color:#fafafa;margin:8px 0 4px;">Sana 10 Ücretsiz Dakika Hediye!</div>
    <div style="font-size:13px;color:#71717a;">Kayıt bonusu olarak hesabına 10 dakika eklendi. Hemen kullanmaya başla.</div>
  </div>
  <div style="text-align:center;margin-bottom:32px;">
    <a href="https://lumnex-production-395d.up.railway.app/app" style="display:inline-block;background:#6366f1;color:#fff;text-decoration:none;padding:14px 36px;border-radius:12px;font-size:15px;font-weight:700;">Editörü Aç →</a>
  </div>
  <div style="border-top:1px solid #27272a;padding-top:20px;text-align:center;">
    <p style="font-size:12px;color:#52525b;margin:0;">© 2026 Lumnex</p>
  </div>
</div>
</body></html>"""
},
"hatirlat_2gun": {
    "konu": "5 dakikan seni bekliyor 👀",
    "html": """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#09090b;font-family:'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:580px;margin:0 auto;padding:40px 24px;">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:36px;">
    <div style="width:10px;height:10px;border-radius:50%;background:#6366f1;"></div>
    <span style="font-size:18px;font-weight:800;color:#fafafa;">Lumnex</span>
  </div>
  <h1 style="color:#fafafa;font-size:22px;font-weight:800;margin:0 0 14px;">Hey {isim}, henüz ilk videonu yapmadın 👀</h1>
  <p style="color:#a1a1aa;font-size:15px;line-height:1.7;margin:0 0 24px;">Rakiplerin bugün TikTok'ta viral oluyor. Sen de 5 dakikada Türkçe videonun İngilizce versiyonunu çıkarabilirsin.</p>
  <div style="background:rgba(234,179,8,.08);border:1px solid rgba(234,179,8,.2);border-radius:12px;padding:16px;margin-bottom:24px;">
    <span style="font-size:20px;">⏰</span>
    <span style="font-size:14px;color:#eab308;font-weight:600;margin-left:8px;">Ücretsiz 10 dakikan hâlâ duruyor, kullanmadan gitmesin!</span>
  </div>
  <div style="background:#111113;border:1px solid #27272a;border-radius:14px;padding:20px;margin-bottom:24px;">
    <div style="font-size:13px;color:#71717a;margin-bottom:12px;">Bu hafta kullanıcılar ne yaptı:</div>
    <div style="font-size:13px;color:#a1a1aa;line-height:2;">
      🇹🇷→🇺🇸 Türkçe ders videosunu İngilizceye çevirdi — <strong style="color:#22c55e;">12K izlenme</strong><br>
      🎵 Podcast transkripti alıp blog yazısına çevirdi<br>
      📱 Hormozi stili altyazıyla Reels'i viral oldu
    </div>
  </div>
  <div style="text-align:center;margin-bottom:32px;">
    <a href="https://lumnex-production-395d.up.railway.app/app" style="display:inline-block;background:linear-gradient(135deg,#ec4899,#a855f7);color:#fff;text-decoration:none;padding:14px 36px;border-radius:12px;font-size:15px;font-weight:700;">Şimdi Dene →</a>
  </div>
  <div style="border-top:1px solid #27272a;padding-top:20px;text-align:center;">
    <p style="font-size:12px;color:#52525b;margin:0;">© 2026 Lumnex · <a href="#" style="color:#52525b;">Aboneliği iptal et</a></p>
  </div>
</div>
</body></html>"""
},
"hatirlat_5gun": {
    "konu": "Son şans: Ücretsiz dakikaların sona eriyor ⚡",
    "html": """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#09090b;font-family:'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:580px;margin:0 auto;padding:40px 24px;">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:36px;">
    <div style="width:10px;height:10px;border-radius:50%;background:#6366f1;"></div>
    <span style="font-size:18px;font-weight:800;color:#fafafa;">Lumnex</span>
  </div>
  <div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:14px;padding:24px;margin-bottom:24px;text-align:center;">
    <div style="font-size:36px;margin-bottom:8px;">⚡</div>
    <div style="font-size:18px;font-weight:800;color:#fafafa;margin-bottom:6px;">Ücretsiz dakikaların sona eriyor</div>
    <div style="font-size:13px;color:#a1a1aa;">Hesabındaki 10 ücretsiz dakika bu ay biter — kullanmazsan kaybolur.</div>
  </div>
  <div style="background:#111113;border:1px solid #27272a;border-radius:14px;padding:20px;margin-bottom:24px;">
    <div style="font-size:13px;font-weight:700;color:#a1a1aa;margin-bottom:14px;">Kaçırdıkların:</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <div style="background:#18181b;border-radius:10px;padding:12px;"><div style="font-size:18px;">🎙️</div><div style="font-size:12px;font-weight:700;color:#fafafa;">Ses Klonlama</div><div style="font-size:11px;color:#71717a;">Kendi sesinle 100+ dil</div></div>
      <div style="background:#18181b;border-radius:10px;padding:12px;"><div style="font-size:18px;">🔥</div><div style="font-size:12px;font-weight:700;color:#fafafa;">Hormozi Altyazı</div><div style="font-size:11px;color:#71717a;">Viral içerik için</div></div>
      <div style="background:#18181b;border-radius:10px;padding:12px;"><div style="font-size:18px;">✂️</div><div style="font-size:12px;font-weight:700;color:#fafafa;">Magic Cut</div><div style="font-size:11px;color:#71717a;">Dolgu sesler otomatik silinir</div></div>
      <div style="background:#18181b;border-radius:10px;padding:12px;"><div style="font-size:18px;">📱</div><div style="font-size:12px;font-weight:700;color:#fafafa;">Platform Boyutu</div><div style="font-size:11px;color:#71717a;">TikTok/YouTube/IG otomatik</div></div>
    </div>
  </div>
  <div style="text-align:center;margin-bottom:16px;">
    <a href="https://lumnex-production-395d.up.railway.app/app" style="display:inline-block;background:#6366f1;color:#fff;text-decoration:none;padding:14px 36px;border-radius:12px;font-size:15px;font-weight:700;">Son Kez Dene →</a>
  </div>
  <div style="text-align:center;margin-bottom:32px;">
    <a href="https://lumnex-production-395d.up.railway.app/#pricing" style="font-size:13px;color:#71717a;text-decoration:none;">Ya da Creator planına geç ($9/ay) →</a>
  </div>
  <div style="border-top:1px solid #27272a;padding-top:20px;text-align:center;">
    <p style="font-size:12px;color:#52525b;margin:0;">© 2026 Lumnex · <a href="#" style="color:#52525b;">Aboneliği iptal et</a></p>
  </div>
</div>
</body></html>"""
}
}


async def resend_email_gonder(to_email: str, konu: str, html: str, isim: str = "") -> bool:
    """Resend API ile email gönderir."""
    if not RESEND_API_KEY:
        log.warning("[Resend] API key eksik")
        return False
    html_final = html.replace("{isim}", isim or to_email.split("@")[0].capitalize())
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": RESEND_FROM,
                    "to": [to_email],
                    "subject": konu,
                    "html": html_final
                }
            )
        if r.status_code in (200, 201):
            log.info(f"[Resend] ✓ {konu} → {to_email}")
            return True
        else:
            log.error(f"[Resend] {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log.error(f"[Resend] Hata: {e}")
        return False


async def hatirlatma_zamanlayici(email: str, isim: str):
    """Kayıt sonrası gün 2 ve gün 5 hatırlatma emaillerini gönderir."""
    # Gün 2 — 48 saat sonra
    await asyncio.sleep(48 * 3600)
    s2 = EMAIL_SABLONLAR["hatirlat_2gun"]
    await resend_email_gonder(email, s2["konu"], s2["html"], isim)
    log.info(f"[Email] Gün 2 hatırlatması gönderildi → {email}")

    # Gün 5 — 72 saat daha bekle (toplamda 5 gün)
    await asyncio.sleep(72 * 3600)
    s5 = EMAIL_SABLONLAR["hatirlat_5gun"]
    await resend_email_gonder(email, s5["konu"], s5["html"], isim)
    log.info(f"[Email] Gün 5 hatırlatması gönderildi → {email}")


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
async def elevenlabs_ses_uret(metin: str, ses_id: str, output_path: str, retry: int = 2, style: float = 0.0, stability: float = 0.5) -> bool:
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
        "voice_settings": {
            "stability": max(0.0, min(1.0, stability if stability != 0.5 else 0.35)),
            "similarity_boost": 0.85,
            "style": max(0.0, min(1.0, style if style != 0.0 else 0.25)),
            "use_speaker_boost": True,
        },
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
            elif r.status_code == 402:
                log.error(f"[ElevenLabs 402] Bu ses Free plan'da kullanılamaz (paid_plan_required). Starter plan gerekli.")
                return False
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

def _ffmpeg_libass_var_mi() -> bool:
    """FFmpeg'in libass (subtitles filter) destekleyip desteklemediğini kontrol eder."""
    try:
        r = subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True)
        return "subtitles" in r.stdout or "subtitles" in r.stderr
    except Exception:
        return False

def ffmpeg_altyazi_gom(video_yolu, srt_yolu, cikti_yolu, font_name, font_size, font_color, is_bold, is_shadow, margin_v) -> bool:
    # Yöntem 1: libass ile burn-in (en iyi görüntü kalitesi)
    if _ffmpeg_libass_var_mi():
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
            "ffmpeg", "-y", "-threads", "0",
            "-i", video_yolu,
            "-vf", f"subtitles='{srt_escaped}':force_style='{style_str}'",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-threads", "0", "-c:a", "copy", cikti_yolu,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        log.warning(f"[FFmpeg] libass burn başarısız, soft subtitle deneniyor: {result.stderr[-200:]}")

    # Yöntem 2: soft subtitle (mov_text) — libass gerektirmez, tarayıcı overlay ile gösterilir
    cmd2 = [
        "ffmpeg", "-y",
        "-i", video_yolu,
        "-i", srt_yolu,
        "-c:v", "copy", "-c:a", "copy",
        "-c:s", "mov_text",
        "-map", "0:v", "-map", "0:a?", "-map", "1:s",
        cikti_yolu,
    ]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    if result2.returncode == 0:
        log.info("[FFmpeg] Soft subtitle (mov_text) başarılı")
        return True

    # Yöntem 3: Sesi ve videoyu direkt kopyala (hiç subtitle yok, SRT ayrı)
    cmd3 = ["ffmpeg", "-y", "-i", video_yolu, "-c:v", "copy", "-c:a", "copy", cikti_yolu]
    result3 = subprocess.run(cmd3, capture_output=True, text=True)
    if result3.returncode == 0:
        log.info("[FFmpeg] Video kopyalandı (subtitle ayrı SRT olarak mevcut)")
        return True

    log.error(f"[FFmpeg Altyazı Hata] Tüm yöntemler başarısız: {result3.stderr[-300:]}")
    return False

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


# ============================================================
# SUPABASE YARDIMCI FONKSİYONLAR
# ============================================================
def _sb_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

async def sb_profil_getir(user_id: str) -> dict | None:
    """Kullanıcının profil verisini Supabase'den çeker."""
    if not SUPABASE_SERVICE_KEY or not user_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/profiles",
                params={"id": f"eq.{user_id}", "select": "id,email,plan,kullanim_dakika,ay_baslangic,stripe_customer_id"},
                headers=_sb_headers(),
            )
            rows = r.json()
            return rows[0] if rows else None
    except Exception as e:
        log.warning(f"[Supabase] profil_getir hata: {e}")
        return None

async def sb_plan_guncelle(email: str, plan: str, customer_id: str = "", sub_id: str = "") -> bool:
    """Kullanıcının planını e-posta ile günceller (Stripe webhook'tan çağrılır)."""
    if not SUPABASE_SERVICE_KEY or not email:
        return False
    try:
        payload: dict = {"plan": plan, "updated_at": "now()"}
        if customer_id:
            payload["stripe_customer_id"] = customer_id
        if sub_id:
            payload["stripe_subscription_id"] = sub_id
        if plan == "lite":
            payload["kullanim_dakika"] = 0  # downgrade'de sıfırla
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.patch(
                f"{SUPABASE_URL}/rest/v1/profiles",
                params={"email": f"eq.{email}"},
                headers=_sb_headers(),
                json=payload,
            )
        log.info(f"[Supabase] Plan güncellendi email={email} plan={plan} status={r.status_code}")
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"[Supabase] plan_guncelle hata: {e}")
        return False

async def sb_kullanim_ekle(user_id: str, dakika: float) -> bool:
    """İşlem sonrası kullanılan dakikayı Supabase'e yazar. Aylık reset de kontrol eder."""
    if not SUPABASE_SERVICE_KEY or not user_id or dakika <= 0:
        return False
    try:
        profil = await sb_profil_getir(user_id)
        if not profil:
            return False
        import datetime
        bugun = datetime.date.today()
        ay_bas_str = profil.get("ay_baslangic") or str(bugun)
        ay_bas = datetime.date.fromisoformat(ay_bas_str[:10])
        # Yeni ay başladıysa sıfırla
        if bugun.year != ay_bas.year or bugun.month != ay_bas.month:
            mevcut = 0.0
            yeni_ay_bas = bugun.replace(day=1).isoformat()
        else:
            mevcut = float(profil.get("kullanim_dakika") or 0)
            yeni_ay_bas = ay_bas_str[:10]
        yeni_toplam = round(mevcut + dakika, 2)
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.patch(
                f"{SUPABASE_URL}/rest/v1/profiles",
                params={"id": f"eq.{user_id}"},
                headers=_sb_headers(),
                json={"kullanim_dakika": yeni_toplam, "ay_baslangic": yeni_ay_bas, "updated_at": "now()"},
            )
        log.info(f"[Supabase] Kullanım eklendi user={user_id[:8]} +{dakika:.1f}dk toplam={yeni_toplam:.1f}dk")
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"[Supabase] kullanim_ekle hata: {e}")
        return False


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


FILLER_PATTERN = re.compile(
    r'\b(ı+h*|e+h*|e{2,}|ı{2,}|mm+|hmm+|hm+|uhh*|umm*|şey+|hani|yani yani|işte işte|falan filan)\b',
    re.IGNORECASE | re.UNICODE
)

GLOSSARY_PATH = os.path.join(os.getenv("DATA_DIR", "/app/data"), "glossary.json")

def _glossary_yukle() -> list:
    try:
        if os.path.exists(GLOSSARY_PATH):
            with open(GLOSSARY_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _glossary_uygula(metin: str) -> str:
    """Kullanıcı glossary'sindeki terimleri metne uygular."""
    for madde in _glossary_yukle():
        kaynak = madde.get("kaynak", "").strip()
        hedef  = madde.get("hedef", "").strip()
        if kaynak and hedef:
            metin = re.sub(r'\b' + re.escape(kaynak) + r'\b', hedef, metin, flags=re.IGNORECASE)
    return metin

def _deepl_glossary_koru(satirlar: list, glossary: list) -> tuple:
    """DeepL çevirisinden önce glossary kaynak terimlerini token ile koru.
    Döndürür: (korunmuş_satirlar, {token: hedef_terim} haritası)
    """
    if not glossary:
        return satirlar, {}
    placeholders: dict = {}
    sonuc = []
    for satir in satirlar:
        for i, madde in enumerate(glossary):
            kaynak = madde.get("kaynak", "").strip()
            hedef  = madde.get("hedef",  "").strip()
            if kaynak and hedef:
                token = f"LMNX{i:03d}GL"
                placeholders[token] = hedef
                satir = re.sub(r'(?<![A-Za-z])' + re.escape(kaynak) + r'(?![A-Za-z])',
                               token, satir, flags=re.IGNORECASE)
        sonuc.append(satir)
    return sonuc, placeholders

def _deepl_placeholder_geri_al(satirlar: list, placeholders: dict) -> list:
    """Token'ları hedef terimlerle değiştir."""
    if not placeholders:
        return satirlar
    sonuc = []
    for satir in satirlar:
        for token, hedef in placeholders.items():
            satir = satir.replace(token, hedef)
        sonuc.append(satir)
    return sonuc


def _tts_sessizlik_kirp(audio_path: str) -> bool:
    """TTS çıktısından baş/son sessizliği kırpar — gerçek konuşma süresini ölçmek için."""
    adj = audio_path + "_trim.mp3"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-af", "silenceremove=start_periods=1:start_silence=0.08:start_threshold=-42dB:"
                    "stop_periods=1:stop_silence=0.08:stop_threshold=-42dB",
             "-c:a", "libmp3lame", "-q:a", "2", adj],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0 and os.path.exists(adj) and os.path.getsize(adj) > 500:
            os.replace(adj, audio_path)
            return True
    except Exception:
        pass
    if os.path.exists(adj):
        try: os.remove(adj)
        except: pass
    return False


async def gemini_srt_slang_normalize(srt_icerik: str, kaynak_dil: str = "tr") -> str:
    """DeepL çevirisinden önce SRT içindeki argo/konuşma dilini standartlaştırır."""
    if not GEMINI_API_KEY:
        return srt_icerik
    bloklar = []
    for blok in srt_icerik.strip().split("\n\n"):
        s = blok.strip().split("\n")
        if len(s) >= 3:
            bloklar.append({"num": s[0], "zaman": s[1], "metin": "\n".join(s[2:])})
    if not bloklar:
        return srt_icerik

    BATCH = 25
    tum_metinler = [b["metin"] for b in bloklar]
    normalize_edilmis = []

    for i in range(0, len(tum_metinler), BATCH):
        batch = tum_metinler[i:i+BATCH]
        numarali = "\n".join(f"{j+1}. {m}" for j, m in enumerate(batch))
        prompt = (
            f"Normalize these {len(batch)} transcript lines. Language: {kaynak_dil}.\n"
            f"Fix ALL of these:\n"
            f"1. Internet slang, abbreviations, filler words (um, uh, şey, hani, eee, ıı)\n"
            f"2. Mixed languages — translate any foreign words inline to {kaynak_dil}\n"
            f"3. Regional dialect → standard spoken language\n"
            f"4. Mis-transcribed technical terms: fix brand names (GitHub→GitHub, OAuth→OAuth), "
            f"programming terms (API, SQL, URL, JSON, CSS, HTML), product names that ASR may mangle\n"
            f"5. Keep the original meaning. Do NOT add or remove sentences.\n"
            f"Return ONLY numbered list: 1. ... 2. ... (same language, no explanations)\n\n{numarali}"
        )
        try:
            async with httpx.AsyncClient(timeout=12.0) as c:
                r = await c.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                    json={"contents":[{"parts":[{"text":prompt}]}],
                          "generationConfig":{"maxOutputTokens":2000,"temperature":0.1}}
                )
            if r.status_code == 200:
                cevap = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                parsed = []
                for satir in cevap.split("\n"):
                    m = re.match(r'^\d+\.\s*(.*)', satir.strip())
                    if m: parsed.append(m.group(1).strip())
                if len(parsed) == len(batch):
                    normalize_edilmis.extend(parsed)
                    continue
        except Exception as e:
            log.debug(f"[SlangNorm] batch {i} hata: {e}")
        normalize_edilmis.extend(batch)

    satirlar = []
    for i, blok in enumerate(bloklar):
        metin = normalize_edilmis[i] if i < len(normalize_edilmis) else blok["metin"]
        satirlar.extend([blok["num"], blok["zaman"], metin, ""])
    return "\n".join(satirlar)


async def gemini_metin_kisalt(metin: str, hedef_sure: float, dil: str = "en") -> str:
    """
    Hedef süreye sığacak şekilde metni kısaltır (timing overflow önleme).
    Anlamı korur, gereksiz kelimeleri ve ifadeleri çıkarır.
    Sadece metin açıkça çok uzunsa çağrılır.
    """
    if not GEMINI_API_KEY or not metin or hedef_sure <= 0:
        return metin
    # Yaklaşık hedef kelime sayısı: ortalama 2.4 kelime/sn konuşma hızı
    hedef_kelime = max(3, int(hedef_sure * 2.4))
    mevcut_kelime = len(metin.split())
    if mevcut_kelime <= hedef_kelime + 3:
        return metin  # Zaten yeterince kısa
    dil_adi = {"tr": "Turkish", "en": "English", "de": "German", "fr": "French",
               "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian"}.get(dil, "the original language")
    prompt = (
        f"Shorten this dubbed speech segment to fit within {hedef_sure:.1f} seconds "
        f"(target: ~{hedef_kelime} words). Current: {mevcut_kelime} words.\n"
        f"Language: {dil_adi}.\n"
        f"Rules:\n"
        f"- Remove redundant phrases, filler, and the least important details\n"
        f"- KEEP the core meaning and key information\n"
        f"- Do NOT change the language or translate\n"
        f"- Return ONLY the shortened text, no explanations\n\n"
        f"Text: {metin}"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": 300, "temperature": 0.1}}
            )
        if r.status_code == 200:
            kisaltilmis = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if kisaltilmis and len(kisaltilmis) > 3:
                log.debug(f"[GeminiKisalt] '{metin[:40]}' → '{kisaltilmis[:40]}' (hedef={hedef_sure:.1f}s)")
                return kisaltilmis
    except Exception as e:
        log.debug(f"[GeminiKisalt] fallback: {e}")
    return metin


async def gemini_jargon_temizle(metin: str, dil: str = "tr") -> str:
    """
    Gemini ile transkript segmentini temizler:
    - Dolgu seslerini (ıı, ee, şey, hmm, um, uh) kaldırır
    - Sokak jargonu ve internet kısaltmalarını TTS'in anlayacağı hale getirir
    - Karışık dili (TR içinde EN kelimeler vs.) hedef dilde normalize eder
    Başarısız olursa regex ile temel temizlik yapıp orijinali döndürür.
    """
    if not GEMINI_API_KEY or not metin or not metin.strip():
        return _glossary_uygula(FILLER_PATTERN.sub('', metin).strip())

    # Glossary'yi önce uygula
    metin = _glossary_uygula(metin)

    # Glossary maddelerini Gemini prompt'una ekle
    glossary = _glossary_yukle()
    glossary_satir = ""
    if glossary:
        maddeler = "; ".join(f'"{g["kaynak"]}"→"{g["hedef"]}"' for g in glossary[:20])
        glossary_satir = f"6. Apply these custom glossary terms: {maddeler}\n"

    dil_adi = {"tr":"Türkçe","en":"English","de":"Deutsch","fr":"Français","it":"Italiano"}.get(dil,"the target language")
    prompt = (
        f"You are a transcript cleaner for a TTS dubbing system. Clean the following transcript segment:\n"
        f"1. Remove filler sounds (ıı, ee, eee, hmm, um, uh, şey, hani, etc.) completely\n"
        f"2. Expand internet slang, street/colloquial language, abbreviations to full spoken words\n"
        f"3. Convert all content to natural spoken {dil_adi} — translate any foreign words inline\n"
        f"4. Keep the meaning. Do NOT summarize. Do NOT add anything.\n"
        f"5. Return ONLY the cleaned text, nothing else.\n"
        f"{glossary_satir}\n"
        f"Segment: {metin}"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"maxOutputTokens":400,"temperature":0.1}}
            )
        if r.status_code == 200:
            temiz = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if temiz and len(temiz) > 2:
                return temiz
    except Exception as e:
        log.debug(f"[GeminiClean] fallback: {e}")
    # Fallback: regex ile dolgu temizle
    return FILLER_PATTERN.sub('', metin).strip() or metin


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
    style: float = 0.0,
    stability: float = 0.55,
) -> bool:
    """
    Segment metni sese çevirir.
    - TTS öncesi metin normalizasyonu uygular (kısaltmalar, jargon)
    - Rate limit ve hatalarda retry
    - Ses hedef süreden uzunsa akıllı sıkıştırma
    """
    # Metin normalize et
    # 1. Gemini ile jargon + dolgu temizle (8s timeout, hata durumunda fallback)
    metin_gemini = await gemini_jargon_temizle(metin, dil)
    # 2. Kural tabanlı normalizasyon
    metin_temiz = metin_normalize(metin_gemini, dil)
    if metin_temiz != metin:
        log.debug(f"[Normalize] '{metin[:50]}' → '{metin_temiz[:50]}'")

    # 3. Timing overflow önleme: çeviri orijinalden çok uzunsa Gemini ile kısalt
    # Karakter/saniye tahmini: ortalama ~13 char/s konuşma (dile göre değişir)
    if hedef_sure > 0:
        CPS = {"tr": 14, "en": 13, "de": 12, "fr": 13, "es": 13, "it": 13, "pt": 13, "ru": 12}.get(dil, 13)
        tahmin_sure = len(metin_temiz) / CPS
        if tahmin_sure > hedef_sure * 1.3:  # %30'dan fazla taşma riski var
            log.info(f"[GeminiKisalt] Taşma riski: tahmin={tahmin_sure:.1f}s hedef={hedef_sure:.1f}s — kısaltılıyor")
            metin_temiz = await gemini_metin_kisalt(metin_temiz, hedef_sure, dil)

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
            "stability": max(0.0, min(1.0, stability if stability != 0.55 else 0.35)),
            "similarity_boost": 0.85,
            "style": max(0.0, min(1.0, style if style != 0.0 else 0.25)),
            "use_speaker_boost": True,
        },
    }
    for deneme in range(retry + 1):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=data, headers=headers, timeout=90.0)

            log.info(f"[ElevenLabs] HTTP {r.status_code} ses_id={ses_id[:8]} metin='{metin_temiz[:30]}'")

            if r.status_code == 429:
                bekle = 6 * (deneme + 1)
                log.warning(f"[ElevenLabs] Rate limit → {bekle}s bekleniyor...")
                await asyncio.sleep(bekle)
                continue

            if r.status_code == 402:
                log.error(f"[ElevenLabs 402] ses_id={ses_id} — Bu ses Free plan'da kullanılamaz (paid_plan_required).")
                return False
            if r.status_code in (401, 403):
                log.error(f"[ElevenLabs {r.status_code}] API key geçersiz — ses_id={ses_id}, yanıt: {r.text[:200]}")
                return False

            if r.status_code == 422:
                hata = r.text[:300]
                log.error(f"[ElevenLabs 422] {hata}")
                # Plan kısıtı varsa direkt çık, retry yapma
                if "professional_voice" in hata or "starter" in hata or "free" in hata.lower():
                    return False
                if deneme < retry:
                    await asyncio.sleep(2)
                    continue
                return False

            if r.status_code != 200:
                hata = r.text[:300]
                log.error(f"[ElevenLabs {r.status_code}] {hata}")
                # Kota/plan hatası — retry yapma
                if any(k in hata.lower() for k in ["quota", "limit", "insufficient", "upgrade", "plan"]):
                    return False
                if deneme < retry:
                    await asyncio.sleep(2)
                    continue
                return False

            with open(output_path, "wb") as f:
                f.write(r.content)

            # 1. Baş/son sessizliği kırp → gerçek konuşma süresi ölç
            _tts_sessizlik_kirp(output_path)

            gercek = ses_sure_olc(output_path)
            if gercek > 0 and hedef_sure > 0:
                oran = gercek / hedef_sure
                log.debug(f"[TTS] üretilen={gercek:.2f}s hedef={hedef_sure:.2f}s oran={oran:.2f}x")

                # 2. Doğal hız sınırı: max 1.35x — Gemini kısaltma sonrası hafif taşma için tolerans
                MAX_ATEMPO = 1.35
                if oran > 1.10:  # %10'dan fazla uzunsa hafifçe sıkıştır
                    oran_sinir = min(oran, MAX_ATEMPO)
                    filtre = f"atempo={oran_sinir:.4f}"
                    adj = output_path + "_adj.mp3"
                    r2 = subprocess.run(
                        ["ffmpeg", "-y", "-i", output_path,
                         "-filter:a", filtre,
                         "-c:a", "libmp3lame", "-q:a", "2", adj],
                        capture_output=True, text=True, timeout=60
                    )
                    if r2.returncode == 0:
                        os.replace(adj, output_path)
                        log.debug(f"[TTS] Sıkıştırıldı: {oran_sinir:.2f}x (hedef={hedef_sure:.1f}s)")
                    elif os.path.exists(adj):
                        try: os.remove(adj)
                        except: pass
                # oran > MAX_ATEMPO: zorlama, doğal hızda bırak (hafif taşma kabul edilebilir)
                # oran < 1: kısa → boşluk olsun, doğal

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
async def islem_motoru(out_file, modul, hedef_dil, ses_id, tmp_in, yazili_metin, kaynak_dil, f_name, f_size, f_color, is_bold, is_shadow, m_v, orig_vol=0.03, dub_vol=1.0, style=0.0, stability=0.55, user_id=""):
    import time as _time
    b_id   = os.path.splitext(out_file)[0].replace("sonuc_", "")
    gecici = os.path.join(TEMP_DIR, b_id)
    _t0    = _time.time()

    log.info(f"══ MOTOR BAŞLADI ══ id={b_id} modul={modul} ses={ses_id[:12]} dil={kaynak_dil}→{hedef_dil} style={style} stability={stability}")

    try:
        os.makedirs(gecici, exist_ok=True)
    except Exception as e:
        log.error(f"[Motor] Geçici klasör oluşturulamadı: {gecici} — {e}")
        islem_durumlari[out_file] = {"durum": f"Hata: Temp dizin oluşturulamadı — {e}", "yuzde": 0}
        return

    try:
        islem_durumlari[out_file] = {"durum": "Başlatılıyor...", "yuzde": 5}

        # ── Dosya boyutu / süre kontrolü ──
        if tmp_in:
            if not os.path.exists(tmp_in):
                log.error(f"[Motor] Yüklenen dosya bulunamadı: {tmp_in}")
                islem_durumlari[out_file] = {"durum": "Hata: Yüklenen dosya sunucuda bulunamadı. Tekrar yükleyin.", "yuzde": 0}
                return
            boyut_mb = os.path.getsize(tmp_in) / 1024 / 1024
            log.info(f"[Motor] Dosya: {os.path.basename(tmp_in)} ({boyut_mb:.1f} MB)")
            gecerli, hata_msg = _dosya_kontrol(tmp_in)
            if not gecerli:
                log.warning(f"[Motor] Dosya kontrolü başarısız: {hata_msg}")
                islem_durumlari[out_file] = {"durum": f"Hata: {hata_msg}", "yuzde": 0}
                return
            log.info(f"[Motor] Dosya kontrolü OK ({boyut_mb:.1f} MB)")

        # ── API key kontrolleri ──
        if modul in ["desifre", "altyazi", "seslendirme"] and not DEEPGRAM_API_KEY:
            log.error("[Motor] DEEPGRAM_API_KEY eksik!")
            islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji('deepgram_401')}", "yuzde": 0}
            return
        if modul in ["metinden_sese", "seslendirme"] and not ELEVENLABS_API_KEY:
            log.error("[Motor] ELEVENLABS_API_KEY eksik!")
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
            ok = await elevenlabs_ses_uret(metin_final, ses_id, os.path.join(OUTPUT_DIR, out_file), style=style, stability=stability)
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
            log.info(f"[Desifre] Deepgram başlıyor: {os.path.basename(tmp_in)} dil={kaynak_dil}")
            try:
                dg = await deepgram_desifre_et(tmp_in, kaynak_dil)
            except ValueError as e:
                log.error(f"[Desifre] Deepgram HATA: {e}")
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji(str(e))}", "yuzde": 0}
                return
            except Exception as e:
                log.error(f"[Desifre] Deepgram beklenmedik HATA: {e}")
                islem_durumlari[out_file] = {"durum": f"Hata: Deepgram yanıt vermedi — {e}", "yuzde": 0}
                return
            log.info(f"[Desifre] Deepgram OK → transkript oluşturuluyor")
            islem_durumlari[out_file] = {"durum": "Transkript oluşturuluyor...", "yuzde": 70}
            srt_path = os.path.join(OUTPUT_DIR, os.path.splitext(out_file)[0] + ".srt")
            deepgram_to_srt(dg, srt_path)
            if not os.path.exists(srt_path) or os.path.getsize(srt_path) < 10:
                log.error(f"[Desifre] SRT boş veya oluşturulamadı: {srt_path}")
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji('segment_yok')}", "yuzde": 0}
                return
            log.info(f"[Desifre] SRT oluşturuldu ({os.path.getsize(srt_path)} byte)")

            # Teknik terim / slang normalize (Gemini) — transkript kalitesini iyileştirir
            if GEMINI_API_KEY:
                islem_durumlari[out_file] = {"durum": "Transkript iyileştiriliyor (teknik terimler)...", "yuzde": 75}
                try:
                    with open(srt_path, encoding="utf-8") as f:
                        ham_srt = f.read()
                    temizlenmis = await gemini_srt_slang_normalize(ham_srt, kaynak_dil or "tr")
                    with open(srt_path, "w", encoding="utf-8") as f:
                        f.write(temizlenmis)
                    log.info("[Desifre] Gemini normalize tamamlandı")
                except Exception as e:
                    log.warning(f"[Desifre] Gemini normalize hata (orijinal kullanılıyor): {e}")

            # Çeviri — hedef dil varsa çevir
            hd = (hedef_dil or "").strip().upper()
            kd = (kaynak_dil or "").strip().upper()
            if hd and hd not in ("", "AUTO") and DEEPL_API_KEY:
                islem_durumlari[out_file] = {"durum": f"DeepL ile {hd}'e çevriliyor...", "yuzde": 85}
                try:
                    with open(srt_path, encoding="utf-8") as f:
                        icerik = f.read()
                    log.info(f"[Desifre] DeepL çeviri başlıyor: {kd}→{hd} ({len(icerik)} karakter)")
                    cevrilmis = await srt_paralel_cevir(icerik, hedef_dil)
                    with open(srt_path, "w", encoding="utf-8") as f:
                        f.write(cevrilmis)
                    log.info(f"[Desifre] Çeviri tamamlandı: {kaynak_dil}→{hedef_dil}")
                except Exception as e:
                    log.warning(f"[Desifre] Çeviri hatası: {e} — orijinal transkript kullanılıyor")
            elif hd and not DEEPL_API_KEY:
                log.warning("[Desifre] Hedef dil seçili ama DEEPL_API_KEY eksik")

            elapsed = _time.time() - _t0
            log.info(f"[Desifre] ✓ TAMAMLANDI — süre={elapsed:.1f}s")
            islem_durumlari[out_file] = {"durum": "Tamamlandı", "yuzde": 100}
            return

        # ── ALTYAZI ────────────────────────────────────────
        if modul == "altyazi":
            if not ffmpeg_var_mi():
                log.error("[Altyazı] FFmpeg bulunamadı!")
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji('ffmpeg_yok')}", "yuzde": 0}
                return
            islem_durumlari[out_file] = {"durum": "Ses analiz ediliyor...", "yuzde": 20}
            log.info(f"[Altyazı] Deepgram başlıyor: {os.path.basename(tmp_in)} dil={kaynak_dil}")
            try:
                dg = await deepgram_desifre_et(tmp_in, kaynak_dil)
            except ValueError as e:
                log.error(f"[Altyazı] Deepgram HATA: {e}")
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji(str(e))}", "yuzde": 0}
                return
            except Exception as e:
                log.error(f"[Altyazı] Deepgram beklenmedik HATA: {e}")
                islem_durumlari[out_file] = {"durum": f"Hata: Deepgram yanıt vermedi — {e}", "yuzde": 0}
                return
            log.info(f"[Altyazı] Deepgram OK")

            srt_kaynak = os.path.join(gecici, "kaynak.srt")
            deepgram_to_srt(dg, srt_kaynak)
            srt_final = srt_kaynak

            # Çeviri kontrolü — kaynak "auto" bile olsa hedef dil varsa çevir
            hd = (hedef_dil or "").strip().upper()
            kd = (kaynak_dil or "").strip().upper()
            if hd and hd not in ("", "AUTO") and DEEPL_API_KEY:
                islem_durumlari[out_file] = {"durum": f"DeepL ile {hd}'e çevriliyor...", "yuzde": 50}
                try:
                    with open(srt_kaynak, encoding="utf-8") as f:
                        icerik = f.read()
                    log.info(f"[Altyazı] DeepL çeviri başlıyor: {kd}→{hd} ({len(icerik)} karakter)")
                    # Slang/argo normalize et (DeepL öncesi)
                    islem_durumlari[out_file] = {"durum": "Argo/slang normalize ediliyor...", "yuzde": 48}
                    icerik = await gemini_srt_slang_normalize(icerik, kaynak_dil or "tr")
                    cevrilmis = await srt_paralel_cevir(icerik, hedef_dil)
                    srt_final = os.path.join(gecici, f"ceviri_{hedef_dil}.srt")
                    with open(srt_final, "w", encoding="utf-8") as f:
                        f.write(cevrilmis)
                    log.info(f"[Altyazı] Çeviri tamamlandı: {kd}→{hd}")
                except Exception as e:
                    log.error(f"[Altyazı] Çeviri hatası: {e} — orijinal SRT kullanılıyor")
            elif hd and hd not in ("", "AUTO") and not DEEPL_API_KEY:
                log.warning("[Altyazı] Hedef dil seçili ama DEEPL_API_KEY eksik — çeviri atlandı")

            base = os.path.splitext(out_file)[0]
            shutil.copy(srt_final, os.path.join(OUTPUT_DIR, base + ".srt"))
            # words dosyasını OUTPUT_DIR'e kopyala (TikTok kelime timestamp'leri için)
            words_src = srt_kaynak.replace('.srt', '_words.json')
            if os.path.exists(words_src):
                shutil.copy(words_src, os.path.join(OUTPUT_DIR, base + "_words.json"))

            # Preview: orijinal videoyu kopyala (burn etme — frontend live overlay gösterecek)
            islem_durumlari[out_file] = {"durum": "Video hazırlanıyor...", "yuzde": 75}
            cikti_mp4 = os.path.join(OUTPUT_DIR, out_file)
            try:
                shutil.copy(tmp_in, cikti_mp4)
                log.info(f"[Altyazı] Orijinal video kopyalandı (burn-free preview)")
            except Exception as e:
                log.error(f"[Altyazı] Video kopyalama HATA: {e}")
                islem_durumlari[out_file] = {"durum": f"Hata: Video kopyalanamadı — {e}", "yuzde": 0}
                return

            elapsed = _time.time() - _t0
            log.info(f"[Altyazı] ✓ TAMAMLANDI — süre={elapsed:.1f}s")
            islem_durumlari[out_file] = {"durum": "Tamamlandı", "yuzde": 100}
            return

        # ── DUBLAJ ─────────────────────────────────────────
        if modul == "seslendirme":

            # 1. Deşifre
            islem_durumlari[out_file] = {"durum": "Konuşmalar analiz ediliyor...", "yuzde": 8}
            log.info(f"[Dublaj] Deepgram başlıyor: {os.path.basename(tmp_in)} dil={kaynak_dil}")
            try:
                dg = await deepgram_desifre_et(tmp_in, kaynak_dil)
            except ValueError as e:
                log.error(f"[Dublaj] Deepgram HATA: {e}")
                islem_durumlari[out_file] = {"durum": f"Hata: {_hata_mesaji(str(e))}", "yuzde": 0}
                return
            except Exception as e:
                log.error(f"[Dublaj] Deepgram beklenmedik HATA: {e}")
                islem_durumlari[out_file] = {"durum": f"Hata: Deepgram yanıt vermedi — {e}", "yuzde": 0}
                return
            log.info(f"[Dublaj] Deepgram OK → SRT oluşturuluyor")

            srt_path = os.path.join(OUTPUT_DIR, os.path.splitext(out_file)[0] + ".srt")
            deepgram_to_srt(dg, srt_path)

            with open(srt_path, encoding="utf-8") as f:
                srt_icerik = f.read()
            segmentler = _srt_parse(srt_icerik)

            if not segmentler:
                log.error(f"[Dublaj] SRT parse sonucu boş: {srt_path}")
                islem_durumlari[out_file] = {"durum": "Hata: Segment bulunamadı", "yuzde": 0}
                return
            log.info(f"[Dublaj] {len(segmentler)} segment parse edildi")

            # 2. Kısa segmentleri birleştir (< 0.8s → bir sonrakiyle birleştir)
            # Hem kalite artar hem ElevenLabs karakter israfı azalır
            segmentler = _kisa_seg_birlestir(segmentler, min_sure=0.8)

            # 3. Çeviri — kaynak "auto" bile olsa hedef dil varsa çevir
            hd = (hedef_dil or "").strip().upper()
            kd = (kaynak_dil or "").strip().upper()
            if hd and hd not in ("", "AUTO") and DEEPL_API_KEY:
                islem_durumlari[out_file] = {"durum": f"{hedef_dil} diline çevriliyor...", "yuzde": 15}
                log.info(f"[Dublaj] DeepL çeviri başlıyor: {kd}→{hd}")
                try:
                    srt_normalize = await gemini_srt_slang_normalize(srt_icerik, kaynak_dil or "tr")
                    cevrilmis_srt = await srt_paralel_cevir(srt_normalize, hedef_dil)
                    with open(srt_path, "w", encoding="utf-8") as f:
                        f.write(cevrilmis_srt)
                    segmentler = _srt_parse(cevrilmis_srt)
                    segmentler = _kisa_seg_birlestir(segmentler, min_sure=0.8)
                    log.info(f"[Dublaj] Çeviri tamamlandı → {hedef_dil}, {len(segmentler)} segment")
                except Exception as e:
                    log.error(f"[Dublaj] Çeviri hatası: {e} — orijinal segmentler kullanılıyor")
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
            log.info(f"[Dublaj] {toplam} segment bulundu, ses_id={ses_id[:12]}, style={style}, stability={stability}")
            islem_durumlari[out_file] = {"durum": f"Sesler üretiliyor (0/{toplam})...", "yuzde": 20}

            ses_klasor = os.path.join(gecici, "sesler")
            os.makedirs(ses_klasor, exist_ok=True)
            log.info(f"[Dublaj] Ses klasörü: {ses_klasor}")

            semaphore   = asyncio.Semaphore(4)
            tamamlanan  = [0]
            atlanan     = [0]
            hata_kodlari = []

            async def seg_uret_task(seg, idx, tum_segmentler):
                async with semaphore:
                    metin = re.sub(r"\[Konuşmacı \d+\]:\s*", "", seg["metin"]).strip()
                    temiz = re.sub(r"[^\w\s]", "", metin).strip()
                    if not metin or len(temiz) < 2:
                        log.info(f"[Dublaj seg {idx}] Çok kısa metin, atlandı: '{seg['metin'][:30]}'")
                        tamamlanan[0] += 1
                        atlanan[0] += 1
                        return None

                    # Konuşmacıya özel ses ID'si ve ayarları
                    speaker_no = re.search(r"\[Konuşmacı (\d+)\]", seg["metin"])
                    kullanilacak_ses = ses_id
                    sp_stability = stability
                    sp_style     = style
                    if speaker_no:
                        sp_key = speaker_no.group(1)
                        sp_val = speaker_ses_map.get(sp_key)
                        if isinstance(sp_val, dict):
                            kullanilacak_ses = sp_val.get("voice_id", ses_id)
                            sp_stability     = float(sp_val.get("stability", stability))
                            sp_style         = float(sp_val.get("style", style))
                        elif isinstance(sp_val, str):
                            kullanilacak_ses = sp_val

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
                    log.info(f"[Dublaj seg {idx}] '{metin[:40]}' → {kullanilacak_ses[:8]} sure={sure:.2f}s stab={sp_stability}")
                    ok = await elevenlabs_segment_uret(metin, kullanilacak_ses, ses_yol, sure, dil=hedef_dil or kaynak_dil or "en", style=sp_style, stability=sp_stability)
                    tamamlanan[0] += 1
                    pct = 20 + int((tamamlanan[0] / toplam) * 55)
                    islem_durumlari[out_file] = {
                        "durum": f"Sesler üretiliyor ({tamamlanan[0]}/{toplam})...",
                        "yuzde": pct,
                    }
                    if ok:
                        log.info(f"[Dublaj seg {idx}] OK ✓")
                        return {"dosya": ses_yol, "baslangic": seg["baslangic"]}
                    log.error(f"[Dublaj seg {idx}] BAŞARISIZ — metin='{metin[:30]}'")
                    return None

            gorevler  = [seg_uret_task(seg, i, segmentler) for i, seg in enumerate(segmentler)]
            sonuclar  = await asyncio.gather(*gorevler)
            ses_liste = [s for s in sonuclar if s is not None]

            log.info(f"[Dublaj] Toplam={toplam} Atlanan={atlanan[0]} Üretilen={len(ses_liste)}")
            if not ses_liste:
                if atlanan[0] == toplam:
                    neden = "Videodaki tüm konuşma segmentleri çok kısa veya boş. Daha uzun konuşma içeren bir video deneyin."
                else:
                    neden = (
                        f"Seçili ses: {ses_id[:12]}... — "
                        "ElevenLabs API hatası. Olası nedenler: "
                        "1) Türkçe/DE/FR/IT sesler Starter plan gerektirir — İngilizce ses seçin. "
                        "2) Railway ELEVENLABS_API_KEY hatalı. "
                        "3) Kota dolmuş. Railway loglarını kontrol edin."
                    )
                islem_durumlari[out_file] = {"durum": f"Hata: {neden}", "yuzde": 0}
                return

            # 4. FFmpeg Miksleme
            islem_durumlari[out_file] = {"durum": f"{len(ses_liste)} ses senkronize ediliyor...", "yuzde": 80}
            cikti_tam = os.path.join(OUTPUT_DIR, out_file)
            log.info(f"[Dublaj] FFmpeg miksleme başlıyor: {len(ses_liste)} ses → {os.path.basename(cikti_tam)}")
            try:
                ok = ffmpeg_ses_miksleme(
                    video_yolu=tmp_in,
                    ses_listesi=ses_liste,
                    cikti_yolu=cikti_tam,
                    orig_vol=orig_vol,
                    dub_vol=dub_vol,
                    gecici_klasor=ses_klasor,
                )
            except Exception as e:
                log.error(f"[Dublaj] FFmpeg miksleme HATA: {e}")
                import traceback; log.error(traceback.format_exc())
                islem_durumlari[out_file] = {"durum": f"Hata: FFmpeg miksleme başarısız — {e}", "yuzde": 0}
                return

            if not ok:
                log.error(f"[Dublaj] FFmpeg miksleme başarısız (False döndü) — cikti={cikti_tam}")
                islem_durumlari[out_file] = {"durum": "Uyarı: Miksleme hatası, SRT kaydedildi", "yuzde": 100}
                return

            elapsed = _time.time() - _t0
            log.info(f"[Dublaj] ✓ TAMAMLANDI — süre={elapsed:.1f}s çıktı={os.path.basename(cikti_tam)}")
            islem_durumlari[out_file] = {"durum": "Tamamlandı", "yuzde": 100}
            return

    except Exception as e:
        import traceback
        log.error(f"[Motor FATAL] {e}\n{traceback.format_exc()}")
        islem_durumlari[out_file] = {"durum": f"Hata: Sistem işleyemedi — {e}", "yuzde": 0}
    finally:
        # Başarılıysa kullanım dakikasını Supabase'e kaydet
        try:
            durum = islem_durumlari.get(out_file, {})
            if durum.get("yuzde") == 100 and user_id:
                cikti_tam = os.path.join(OUTPUT_DIR, out_file)
                sure_sn = ses_sure_olc(cikti_tam) if os.path.exists(cikti_tam) else 0.0
                if sure_sn <= 0 and tmp_in and os.path.exists(tmp_in):
                    sure_sn = ses_sure_olc(tmp_in)
                sure_dk = round(sure_sn / 60, 2) if sure_sn > 0 else 1.0
                islem_durumlari[out_file]["sure_dakika"] = sure_dk
                await sb_kullanim_ekle(user_id, sure_dk)
        except Exception as _eu:
            log.warning(f"[Motor] Kullanım kaydı hatası: {_eu}")
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
    orig_vol: str     = Form("0.03"),
    dub_vol_param: str = Form("1.0"),
    style: str        = Form("0.0"),      # Duygu yoğunluğu (0.0-1.0)
    stability: str    = Form("0.55"),     # Kararlılık (düşük = daha duygusal)
    user_id: str      = Form(""),         # Supabase user UUID (boş = demo mod)
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
        orig_vol_f  = float(orig_vol)
        dub_vol_f   = float(dub_vol_param)
        style_f     = max(0.0, min(1.0, float(style)))
        stability_f = max(0.0, min(1.0, float(stability)))
    except ValueError:
        orig_vol_f  = 0.03
        dub_vol_f   = 1.0
        style_f     = 0.0
        stability_f = 0.55

    arka_plan.add_task(
        islem_motoru, out_file, modul, hedef_dil,
        ses_id, tmp_in, yazili_metin, kaynak_dil,
        f_name, f_size, f_color, is_bold, is_shadow, m_v,
        orig_vol_f, dub_vol_f, style_f, stability_f,
        user_id.strip() or "",
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
            ext = os.path.splitext(yol)[1].lower()
            mime = {".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
                    ".srt": "text/plain", ".vtt": "text/vtt", ".ass": "text/plain",
                    ".json": "application/json"}.get(ext)
            return FileResponse(yol, media_type=mime) if mime else FileResponse(yol)
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
                    data={"name": isim, "description": "Lumnex klonu"},
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


@app.post("/api/video_ses_klonla/")
async def video_ses_klonla(
    video_dosyasi: UploadFile = File(...),
    isim: str = Form("Video Sesi"),
):
    """
    Yüklenen video/ses dosyasından sesi çıkarır (ffmpeg) ve
    ElevenLabs Instant Voice Cloning ile klonlar.
    Kullanıcı videodaki kendi sesini veya konuşmacı sesini klonlamak için kullanır.
    """
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": _hata_mesaji("ffmpeg_yok")}, status_code=500)

    b_id = uuid.uuid4().hex[:8]
    ext  = os.path.splitext(video_dosyasi.filename or "video.mp4")[1] or ".mp4"
    tmp_video = os.path.join(TEMP_DIR, f"vsk_{b_id}{ext}")
    tmp_ses   = os.path.join(TEMP_DIR, f"vsk_{b_id}.mp3")

    try:
        with open(tmp_video, "wb") as f:
            shutil.copyfileobj(video_dosyasi.file, f)

        # 1. Sesi çıkar (ilk 5 dakika yeterli)
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", tmp_video,
            "-t", "300",          # max 5 dakika
            "-q:a", "2",          # yüksek kalite
            "-vn",                # video yok
            tmp_ses
        ]
        result = await asyncio.to_thread(
            subprocess.run, ffmpeg_cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0 or not os.path.exists(tmp_ses):
            return JSONResponse({"hata": "Videodan ses çıkarılamadı. Lütfen video dosyasını kontrol edin."}, status_code=500)

        boyut_mb = os.path.getsize(tmp_ses) / (1024*1024)
        log.info(f"[VideoSesKlonla] Ses çıkarıldı: {boyut_mb:.1f}MB → ElevenLabs'a gönderiliyor")

        # 2. ElevenLabs'a klonla
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(tmp_ses, "rb") as f:
                r = await client.post(
                    "https://api.elevenlabs.io/v1/voices/add",
                    headers={"xi-api-key": ELEVENLABS_API_KEY},
                    data={"name": isim, "description": "Lumnex — Video sesi klonu"},
                    files={"files": (os.path.basename(tmp_ses), f, "audio/mpeg")},
                )

        # Temizlik
        for f in [tmp_video, tmp_ses]:
            try: os.remove(f)
            except Exception: pass

        if r.status_code == 200:
            voice_id = r.json().get("voice_id")
            log.info(f"[VideoSesKlonla] Başarılı → {voice_id}")
            return JSONResponse({"basari": True, "voice_id": voice_id, "isim": isim})
        elif r.status_code == 422:
            return JSONResponse({"hata": "Ses çok kısa veya kalitesi düşük. Daha uzun konuşma içeren bir video deneyin."}, status_code=422)
        else:
            detay = r.json() if "application/json" in r.headers.get("content-type","") else r.text[:200]
            if "quota" in str(detay).lower() or "limit" in str(detay).lower():
                return JSONResponse({"hata": "Ses klonlama için ElevenLabs Creator planı gereklidir ($22/ay).", "plan_gerekli": True}, status_code=402)
            return JSONResponse({"hata": f"ElevenLabs hatası: {detay}"}, status_code=r.status_code)

    except Exception as e:
        for f in [tmp_video, tmp_ses]:
            try: os.remove(f)
            except Exception: pass
        log.error(f"[VideoSesKlonla] {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/speaker_auto_klonla/")
async def speaker_auto_klonla(
    video_dosyasi: UploadFile = File(...),
    dosya_adi:     str = Form(""),
    kaynak_dil:    str = Form("tr"),
):
    """
    Videodaki her konuşmacıyı otomatik tespit et, seslerini ayır ve ElevenLabs ile klonla.
    Döndürür: { "0": {"voice_id": "...", "isim": "Konuşmacı 0"}, "1": {...} }
    Klonlanan sesler speaker_map'e otomatik kaydedilir.
    """
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("elevenlabs_401")}, status_code=500)
    if not DEEPGRAM_API_KEY:
        return JSONResponse({"hata": _hata_mesaji("deepgram_401")}, status_code=500)
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": _hata_mesaji("ffmpeg_yok")}, status_code=500)

    b_id = uuid.uuid4().hex[:8]
    ext  = os.path.splitext(video_dosyasi.filename or "video.mp4")[1] or ".mp4"
    tmp_video = os.path.join(TEMP_DIR, f"sak_{b_id}{ext}")
    tmp_audio = os.path.join(TEMP_DIR, f"sak_{b_id}.mp3")
    tmp_files = [tmp_video, tmp_audio]

    try:
        # 1. Video kaydet
        with open(tmp_video, "wb") as f:
            shutil.copyfileobj(video_dosyasi.file, f)

        # 2. Sesi çıkar
        r = await asyncio.to_thread(subprocess.run, [
            "ffmpeg", "-y", "-i", tmp_video, "-t", "600",
            "-q:a", "2", "-vn", tmp_audio
        ], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(tmp_audio):
            return JSONResponse({"hata": "Videodan ses çıkarılamadı."}, status_code=500)

        # 3. Deepgram diarize
        response = await deepgram_desifre_et(tmp_audio, kaynak_dil)
        words = response.results.channels[0].alternatives[0].words or []

        # 4. Konuşmacı → zaman aralığı haritası
        speaker_intervals: dict = {}
        for w in words:
            sp = str(getattr(w, "speaker", 0) or 0)
            t_start = float(getattr(w, "start", 0) or 0)
            t_end   = float(getattr(w, "end",   0) or 0)
            if t_end > t_start:
                speaker_intervals.setdefault(sp, []).append((t_start, t_end))

        if not speaker_intervals:
            return JSONResponse({"hata": "Konuşmacı tespit edilemedi. Diarization verisiz döndü."}, status_code=400)

        log.info(f"[SpeakerAutoKlon] {len(speaker_intervals)} konuşmacı tespit edildi: {list(speaker_intervals.keys())}")

        # 5. Her konuşmacı için ses segmentlerini birleştir ve klonla
        speaker_map_sonuc: dict = {}
        async with httpx.AsyncClient(timeout=120.0) as client:
            for sp_id, intervals in speaker_intervals.items():
                # FFmpeg filter_complex ile segmentleri birleştir
                # Minimum 5 saniye ses gerekli (ElevenLabs için)
                toplam_sure = sum(e - s for s, e in intervals)
                if toplam_sure < 3.0:
                    log.warning(f"[SpeakerAutoKlon] Konuşmacı {sp_id} çok az konuştu ({toplam_sure:.1f}s), atlanıyor")
                    continue

                tmp_sp = os.path.join(TEMP_DIR, f"sak_{b_id}_sp{sp_id}.mp3")
                tmp_files.append(tmp_sp)

                # Segmentleri trim + concat ile çıkar
                filter_parts = []
                for i, (s, e) in enumerate(intervals[:30]):  # max 30 segment
                    filter_parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
                concat_inputs = "".join(f"[a{i}]" for i in range(len(filter_parts)))
                filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={len(filter_parts)}:v=0:a=1[outa]"

                ffmpeg_r = await asyncio.to_thread(subprocess.run, [
                    "ffmpeg", "-y", "-i", tmp_audio,
                    "-filter_complex", filter_complex,
                    "-map", "[outa]",
                    "-q:a", "2",
                    tmp_sp
                ], capture_output=True, text=True, timeout=120)

                if ffmpeg_r.returncode != 0 or not os.path.exists(tmp_sp):
                    log.warning(f"[SpeakerAutoKlon] Konuşmacı {sp_id} ses çıkarma başarısız: {ffmpeg_r.stderr[-200:]}")
                    continue

                # ElevenLabs'a klonla
                isim = f"Konuşmacı {sp_id}"
                with open(tmp_sp, "rb") as audio_f:
                    el_r = await client.post(
                        "https://api.elevenlabs.io/v1/voices/add",
                        headers={"xi-api-key": ELEVENLABS_API_KEY},
                        data={"name": isim, "description": f"Lumnex — Auto klonlanan konuşmacı {sp_id}"},
                        files={"files": (f"speaker_{sp_id}.mp3", audio_f, "audio/mpeg")},
                    )

                if el_r.status_code == 200:
                    voice_id = el_r.json().get("voice_id")
                    speaker_map_sonuc[sp_id] = {"voice_id": voice_id, "isim": isim, "sure": round(toplam_sure, 1)}
                    log.info(f"[SpeakerAutoKlon] Konuşmacı {sp_id} klonlandı → {voice_id}")
                elif el_r.status_code == 402:
                    return JSONResponse({
                        "hata": "Ses klonlama için ElevenLabs Creator planı gereklidir.",
                        "plan_gerekli": True,
                        "kismi": speaker_map_sonuc
                    }, status_code=402)
                else:
                    log.warning(f"[SpeakerAutoKlon] EL hata sp{sp_id}: {el_r.status_code} {el_r.text[:100]}")

        # 6. Speaker map'i kaydet (eğer dosya_adi verilmişse)
        if dosya_adi and speaker_map_sonuc:
            map_path = os.path.join(OUTPUT_DIR, dosya_adi.replace(".srt","").replace(".mp4","") + "_speaker_map.json")
            harita = {sp: v["voice_id"] for sp, v in speaker_map_sonuc.items()}
            with open(map_path, "w", encoding="utf-8") as f:
                json.dump(harita, f, ensure_ascii=False)

        return JSONResponse({
            "basari": True,
            "speaker_sayisi": len(speaker_map_sonuc),
            "speaker_map": speaker_map_sonuc,
        })

    except Exception as e:
        log.error(f"[SpeakerAutoKlon] {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)
    finally:
        for f in tmp_files:
            try: os.remove(f)
            except Exception: pass


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
@app.get("/api/words/{dosya_adi}")
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
                    data={"name": f"KendiSes_{b_id}", "description": "Lumnex otomatik klon"},
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

    profil = await sb_profil_getir(user_id)
    if not profil:
        # Supabase bağlantısı yoksa serbest geç
        return JSONResponse({"yeterli": True, "bakiye": 999, "maliyet": maliyet, "mod": "demo"})

    import datetime
    plan = profil.get("plan", "lite")
    limitler = {"lite": 10, "creator": 75, "studio": 200, "business": 600}
    limit = limitler.get(plan, 10)

    # Aylık reset kontrolü
    bugun = datetime.date.today()
    ay_bas_str = profil.get("ay_baslangic") or str(bugun)
    ay_bas = datetime.date.fromisoformat(ay_bas_str[:10])
    if bugun.year != ay_bas.year or bugun.month != ay_bas.month:
        kullanim = 0.0  # Yeni ay, sıfırdan say
    else:
        kullanim = float(profil.get("kullanim_dakika") or 0)

    kalan = max(0.0, limit - kullanim)
    yeterli = kalan >= maliyet

    return JSONResponse({
        "yeterli": yeterli,
        "bakiye": round(kalan, 1),
        "kullanim": round(kullanim, 1),
        "limit": limit,
        "plan": plan,
        "maliyet": maliyet,
        "mod": "supabase",
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


# ============================================================
# GLOSSARY — Özel Terim Sözlüğü
# ============================================================

@app.get("/api/glossary/")
async def glossary_al():
    return JSONResponse(_glossary_yukle())

@app.post("/api/glossary/")
async def glossary_kaydet(maddeler: str = Form(...)):
    """maddeler: JSON list — [{"kaynak":"naber","hedef":"nasılsın"}, ...]"""
    try:
        liste = json.loads(maddeler)
        if not isinstance(liste, list):
            return JSONResponse({"hata": "Liste bekleniyor"}, status_code=400)
        os.makedirs(os.path.dirname(GLOSSARY_PATH), exist_ok=True)
        with open(GLOSSARY_PATH, "w", encoding="utf-8") as f:
            json.dump(liste, f, ensure_ascii=False, indent=2)
        return JSONResponse({"basari": True, "adet": len(liste)})
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


# ============================================================
# SRT BURN-IN — Altyazı Yakma
# ============================================================

@app.post("/api/altyazi_gom/")
async def altyazi_gom(
    video_adi: str = Form(...),
    srt_adi:   str = Form(...),
):
    """SRT'yi videoya ffmpeg ile gömer (hard subtitle)."""
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": "FFmpeg yüklü değil"}, status_code=500)
    video_yol = os.path.join(OUTPUT_DIR, video_adi)
    srt_yol   = os.path.join(OUTPUT_DIR, srt_adi)
    if not os.path.exists(video_yol):
        return JSONResponse({"hata": "Video bulunamadı"}, status_code=404)
    if not os.path.exists(srt_yol):
        return JSONResponse({"hata": "SRT bulunamadı"}, status_code=404)

    b_id  = uuid.uuid4().hex[:8]
    cikti = f"burned_{b_id}.mp4"
    cikti_yol = os.path.join(OUTPUT_DIR, cikti)
    # SRT path'i ffmpeg için escape et
    srt_escaped = srt_yol.replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y", "-i", video_yol,
        "-vf", f"subtitles='{srt_escaped}':force_style='Fontsize=18,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Bold=1'",
        "-c:a", "copy", "-preset", "fast", cikti_yol
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            return JSONResponse({"cikti": cikti})
        log.error(f"[AltyaziGom] {result.stderr[-400:]}")
        return JSONResponse({"hata": "Altyazı gömilemedi"}, status_code=500)
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/kelime_cevir/")
async def kelime_cevir(
    kelime: str = Form(...),
    baglam: str = Form(""),
    kaynak_dil: str = Form("TR"),
    hedef_dil: str  = Form("EN"),
):
    """Bir kelimeyi DeepL ile çevirir + Gemini ile eş anlamlılar üretir."""
    results = {}
    # 1. DeepL çevirisi
    if DEEPL_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    _deepl_base_url() + "/v2/translate",
                    headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}", "Content-Type": "application/json"},
                    json={"text": [kelime], "source_lang": kaynak_dil.upper()[:2], "target_lang": hedef_dil.upper()[:2]},
                )
            if r.status_code == 200:
                results["deepl"] = r.json()["translations"][0]["text"]
        except Exception as e:
            log.warning(f"[kelime_cevir deepl] {e}")
    # 2. Gemini ile eş anlamlı + alternatifler
    if GEMINI_API_KEY:
        try:
            prompt = (
                f"Give 4 alternative words/synonyms for the word '{kelime}' in the context: '{baglam[:100]}'. "
                f"Target language: {hedef_dil}. "
                "Return ONLY a JSON array like [\"word1\",\"word2\",\"word3\",\"word4\"]. No explanation."
            )
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                    json={"contents":[{"parts":[{"text":prompt}]}],
                          "generationConfig":{"maxOutputTokens":100,"temperature":0.5}}
                )
            if r.status_code == 200:
                raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                import re as _re
                m = _re.search(r'\[.*?\]', raw, _re.S)
                if m:
                    results["alternatifler"] = json.loads(m.group())
        except Exception as e:
            log.warning(f"[kelime_cevir gemini] {e}")
    return JSONResponse(results)


@app.get("/api/voice_preview_url/")
async def voice_preview_url(ses_id: str):
    """ElevenLabs'tan sesin preview URL'ini getirir (TTS kredisi harcamaz)."""
    if not ELEVENLABS_API_KEY:
        return JSONResponse({"hata": "ElevenLabs key eksik"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"https://api.elevenlabs.io/v1/voices/{ses_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY}
            )
        if r.status_code == 200:
            data = r.json()
            preview = data.get("preview_url") or data.get("samples", [{}])[0].get("url") if data.get("samples") else None
            if preview:
                return JSONResponse({"url": preview})
        return JSONResponse({"hata": "preview_url yok"}, status_code=404)
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)


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

    # 5. Bağlam varsa Gemini ile akıllı öneri
    if baglam and len(baglam) > 5 and GEMINI_API_KEY:
        try:
            ai_prompt = (
                f'Transkript düzeltme asistanısın. "{kelime}" kelimesi yanlış tanınmış olabilir.\n'
                f'Bağlam: "{baglam}"\n'
                f'Bu bağlamda "{kelime}" yerine gelebilecek 3 kelime öner.\n'
                f'Sadece JSON liste döndür: ["öneri1", "öneri2", "öneri3"]'
            )
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                    json={"contents":[{"parts":[{"text":ai_prompt}]}],
                          "generationConfig":{"maxOutputTokens":80,"temperature":0.2}}
                )
            if r.status_code == 200:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                m = re.search(r'\[.*?\]', text, re.DOTALL)
                if m:
                    ai_oneriler = json.loads(m.group())
                    for ao in ai_oneriler:
                        if ao not in oneriler:
                            oneriler.insert(0, ao)
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
    metin: str  = Form("Merhaba, ben Lumnex. Bu benim sesim."),
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
            "stability": 0.35,
            "similarity_boost": 0.85,
            "style": 0.25,
            "use_speaker_boost": True,
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


@app.post("/api/viral_analiz/")
async def viral_analiz(transkript: str = Form(...), dil: str = Form("tr")):
    """Gemini ile en viral 5 anı tespit et."""
    if not GEMINI_API_KEY:
        return JSONResponse({"hata": "Gemini API key eksik"}, status_code=500)
    sistem = (
        "Sen viral video içerik uzmanısın. Sana bir video transkripti veriliyor. "
        "En viral, en ilgi çekici, izleyiciyi en çok tutacak 5 anı bul. "
        "Her an için: başlangıç saniyesi (start), bitiş saniyesi (end), "
        "viral skor 0-100 (score), kısa açıklama (reason) ver. "
        "SADECE geçerli JSON döndür. Format:\n"
        "[{\"start\":10.5,\"end\":45.2,\"score\":92,\"reason\":\"Güçlü hook, izleyiciyi yakalar\"}, ...]"
    )
    prompt = f"Transkript (dil: {dil}):\n\n{transkript[:6000]}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": sistem + "\n\n" + prompt}]}],
                      "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}},
            )
        data = r.json()
        metin = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        # JSON bloğu ayıkla
        import re as _re
        m = _re.search(r'\[.*\]', metin, _re.DOTALL)
        if not m:
            return JSONResponse({"hata": "Gemini JSON döndürmedi"}, status_code=500)
        sonuclar = json.loads(m.group())
        return JSONResponse({"anlar": sonuclar[:5]})
    except Exception as e:
        log.error(f"[viral_analiz] HATA: {e}")
        return JSONResponse({"hata": str(e)[:200]}, status_code=500)


@app.post("/api/klip_kes/")
async def klip_kes(
    dosya_adi:  str   = Form(...),
    baslangic:  float = Form(...),
    bitis:      float = Form(...),
    shorts_916: bool  = Form(False),  # True → 9:16 crop (Shorts/Reels/TikTok)
):
    """FFmpeg ile videodan belirli bir bölümü keser. shorts_916=True → 9:16 crop."""
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": "FFmpeg yüklü değil"}, status_code=500)

    giris = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(giris):
        return JSONResponse({"hata": "Dosya bulunamadı"}, status_code=404)

    b_id  = uuid.uuid4().hex[:8]
    sufiks = "_shorts" if shorts_916 else ""
    cikti  = f"klip{sufiks}_{b_id}.mp4"
    cikti_yol = os.path.join(OUTPUT_DIR, cikti)

    sure = max(0.5, bitis - baslangic)

    if shorts_916:
        # 9:16 crop: merkezi kes, en küçük boyutu baz al
        vf = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(baslangic), "-i", giris, "-t", str(sure),
            "-vf", vf, "-c:a", "aac", "-preset", "fast", cikti_yol
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(baslangic), "-i", giris, "-t", str(sure),
            "-c:v", "copy", "-c:a", "aac", cikti_yol
        ]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=180
    )
    if result.returncode == 0:
        log.info(f"[Klip Kes] {dosya_adi} {baslangic:.1f}s-{bitis:.1f}s shorts={shorts_916} → {cikti}")
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
# SILENCE CUT — Sessizlik Kaldırma (ffmpeg silencedetect)
# ============================================================
@app.post("/api/silence_cut/")
async def silence_cut(
    dosya_adi: str    = Form(...),    # outputs/ içindeki mp4 dosyası
    min_sure: float   = Form(0.5),   # minimum sessizlik süresi (saniye)
    esik_db: float    = Form(-35),   # sessizlik eşiği (dB), -35 = orta
):
    """
    Video/ses dosyasındaki sessizlik bloklarını ffmpeg silencedetect ile bulup
    o bölümleri video ve SRT'den keser.
    """
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": _hata_mesaji("ffmpeg_yok")}, status_code=500)

    giris = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.exists(giris):
        return JSONResponse({"hata": "Dosya bulunamadı."}, status_code=404)

    try:
        # 1. Sessizlikleri tespit et
        detect_cmd = [
            "ffmpeg", "-i", giris,
            "-af", f"silencedetect=noise={esik_db}dB:d={min_sure}",
            "-f", "null", "-"
        ]
        result = await asyncio.to_thread(
            subprocess.run, detect_cmd, capture_output=True, text=True, timeout=120
        )
        output = result.stderr

        # 2. Sessizlik aralıklarını parse et
        starts  = [float(m.group(1)) for m in re.finditer(r"silence_start: (\S+)", output)]
        ends    = [float(m.group(1)) for m in re.finditer(r"silence_end: (\S+)", output)]

        if not starts:
            return JSONResponse({"basari": True, "mesaj": "Sessizlik bulunamadı.", "silinen_sure": 0, "yeni_dosya": dosya_adi})

        # 3. Video süresini al
        probe = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", giris],
            capture_output=True, text=True, timeout=30
        )
        try:
            toplam_sure = float(probe.stdout.strip())
        except Exception:
            toplam_sure = 9999.0

        # 4. Tutulacak segmentleri hesapla
        silence_aralik = list(zip(starts, ends + [starts[-1] if len(ends) < len(starts) else ends[-1]]))
        tutulacak = []
        onceki = 0.0
        for s_start, s_end in zip(starts, ends if ends else starts):
            if s_start > onceki + 0.05:
                tutulacak.append((onceki, s_start))
            onceki = s_end
        if onceki < toplam_sure - 0.05:
            tutulacak.append((onceki, toplam_sure))

        if not tutulacak:
            return JSONResponse({"hata": "Tüm video sessiz görünüyor."}, status_code=400)

        # 5. ffmpeg concat filter ile sessiz kısımları kes
        b_id = uuid.uuid4().hex[:8]
        cikti = os.path.join(OUTPUT_DIR, f"silence_cut_{b_id}.mp4")

        # filter_complex ile her segmenti kes ve birleştir
        filter_parts = []
        for i, (t_start, t_end) in enumerate(tutulacak):
            filter_parts.append(f"[0:v]trim={t_start:.3f}:{t_end:.3f},setpts=PTS-STARTPTS[v{i}]")
            filter_parts.append(f"[0:a]atrim={t_start:.3f}:{t_end:.3f},asetpts=PTS-STARTPTS[a{i}]")

        n = len(tutulacak)
        v_concat = "".join(f"[v{i}]" for i in range(n))
        a_concat = "".join(f"[a{i}]" for i in range(n))
        filter_parts.append(f"{v_concat}concat=n={n}:v=1:a=0[vout]")
        filter_parts.append(f"{a_concat}concat=n={n}:v=0:a=1[aout]")
        filter_str = ";".join(filter_parts)

        cut_cmd = [
            "ffmpeg", "-y", "-i", giris,
            "-filter_complex", filter_str,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-c:a", "aac", "-preset", "fast",
            cikti
        ]
        cut_result = await asyncio.to_thread(
            subprocess.run, cut_cmd, capture_output=True, text=True, timeout=600
        )

        if cut_result.returncode != 0:
            log.error(f"[SilenceCut] ffmpeg hata: {cut_result.stderr[-500:]}")
            return JSONResponse({"hata": "Video kesilemedi. Lütfen tekrar deneyin."}, status_code=500)

        silinen_sure = sum(e - s for s, e in zip(starts, ends if ends else starts))
        yeni_adi = os.path.basename(cikti)
        log.info(f"[SilenceCut] {len(tutulacak)} segment korundu, {silinen_sure:.1f}s sessizlik silindi → {yeni_adi}")

        return JSONResponse({
            "basari": True,
            "yeni_dosya": yeni_adi,
            "silinen_sure": round(silinen_sure, 1),
            "kalan_segment_sayisi": len(tutulacak),
            "mesaj": f"{silinen_sure:.1f}s sessizlik silindi, {len(tutulacak)} bölüm birleştirildi"
        })

    except Exception as e:
        log.error(f"[SilenceCut] {e}")
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
            return HTMLResponse(f.read(), headers=_NO_CACHE_HEADERS)
    return HTMLResponse("<h1>Uygulama bulunamadi.</h1>")


# ============================================================
# LEGAL PAGES
# ============================================================
@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    if os.path.exists("privacy.html"):
        with open("privacy.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Privacy Policy not found.</h1>")

@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    if os.path.exists("terms.html"):
        with open("terms.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Terms of Service not found.</h1>")


# ============================================================
# AI ASSISTANT — Gemini 2.0 Flash
# ============================================================
@app.post("/api/ai/")
@app.post("/api/ai_asistan/")
async def ai_asistan(sorgu: str = Form(...), dil: str = Form("en")):
    if not GEMINI_API_KEY:
        return JSONResponse({"hata": "Gemini API key not configured."}, status_code=500)
    if not sorgu or len(sorgu.strip()) < 3:
        return JSONResponse({"hata": "Query too short."}, status_code=400)

    dil_adi = {
        "tr": "Turkish", "en": "English", "de": "German", "fr": "French",
        "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
        "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ar": "Arabic",
    }.get(dil, "English")
    system_prompt = (
        "You are the Lumnex AI assistant. Help users with video dubbing, subtitles, "
        "transcription, text-to-speech and content creation. Give step-by-step instructions for workflows.\n\n"
        "FEATURES:\n"
        "- Transcript (Deşifre mode): Upload video → auto-transcribe with speaker detection\n"
        "- Subtitles (Altyazı mode): Add TikTok/Hormozi viral word-by-word subtitles\n"
        "- Dubbing (Seslendirme mode): Translate + dub to 60+ languages. Voice cloning available.\n"
        "- Text-to-Speech (Metinden Sese mode): Type text → studio-quality speech with 50+ voices\n"
        "- Magic Cut: Removes filler words (um, uh, hmm, şey, eee) from transcript automatically\n"
        "- Silence Cut: Removes silent gaps from video with one click\n"
        "- Voice Cloning: Record 30s+ of your voice → clone it → dub in any language as yourself\n"
        "- Use Video's Own Voice: Extract voice from uploaded video → instant clone for dubbing\n\n"
        "COMMON WORKFLOWS (give NUMBERED STEPS):\n"
        "- TikTok video with subtitles: 1.Upload video 2.Select Altyazı mode 3.Set source lang 4.Click Start 5.After done, click Magic subtitle style 6.Download\n"
        "- Dub video to English: 1.Upload video 2.Select Seslendirme mode 3.Source=Turkish Target=English 4.Pick a voice 5.Click Start\n"
        "- Podcast transcript: 1.Upload audio 2.Select Deşifre mode 3.Click Start 4.Edit segments 5.Download SRT or TXT\n"
        "- Clone own voice: 1.Go to Seslendirme mode 2.Click 'Kendi Sesimle Dublaj' 3.Record 30s+ 4.System clones voice 5.Dub to any language\n"
        "- Remove fillers: 1.Get transcript first 2.Click Magic Cut button in toolbar 3.Done\n\n"
        "PLANS: Lite $0/10min · Creator $9/75min · Studio $19/200min · Business $49/600min\n\n"
        f"IMPORTANT: Always reply in {dil_adi}. Use numbered steps for workflow questions. Use emoji. Be concise."
    )

    # System context'i kullanıcı mesajının başına ekle — daha uyumlu yaklaşım
    tam_sorgu = f"{system_prompt}\n\n---\nUser: {sorgu}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                json={
                    "contents": [{"parts": [{"text": tam_sorgu}]}],
                    "generationConfig": {"maxOutputTokens": 700, "temperature": 0.7}
                }
            )
        if r.status_code == 200:
            data = r.json()
            candidates = data.get("candidates", [])
            if not candidates:
                pf = data.get("promptFeedback", {})
                reason = pf.get("blockReason", "NO_CANDIDATES")
                log.error(f"[Gemini AI Asistan] Yanıt engellendi: {reason}")
                return JSONResponse({"hata": f"Response blocked: {reason}"}, status_code=500)
            text = candidates[0]["content"]["parts"][0]["text"]
            return JSONResponse({"yanit": text})
        else:
            log.error(f"[Gemini AI Asistan] HTTP {r.status_code}: {r.text[:300]}")
            return JSONResponse({"hata": f"Gemini error {r.status_code}: {r.text[:120]}"}, status_code=500)
    except Exception as e:
        log.error(f"[Gemini AI Asistan] {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)


# ============================================================
# AI TEXT EDITOR — TTS metin düzeltme/kısaltma/çeviri
# ============================================================
@app.post("/api/ai_text/")
async def ai_text(prompt: str = Form(...)):
    if not GEMINI_API_KEY:
        return JSONResponse({"hata": "Gemini API key not configured."}, status_code=500)
    if not prompt or len(prompt.strip()) < 5:
        return JSONResponse({"hata": "Prompt too short."}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent?key={GEMINI_API_KEY}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.4}
                }
            )
        if r.status_code == 200:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return JSONResponse({"yanit": text})
        else:
            return JSONResponse({"hata": f"Gemini error {r.status_code}"}, status_code=500)
    except Exception as e:
        log.error(f"[AI Text] {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)


# ============================================================
# MY FILES — Output dosyalarını listele + yeniden adlandır
# ============================================================
_NAMES_FILE = os.path.join(OUTPUT_DIR, "_names.json")

def _names_oku() -> dict:
    try:
        with open(_NAMES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _names_yaz(names: dict):
    try:
        with open(_NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"[Names] Yazma hatası: {e}")


@app.post("/api/altyazi_gom/")
async def altyazi_gom_endpoint(
    video_adi: str = Form(...),   # ciktilar/ altındaki mp4 adı (orijinal)
    srt_adi:   str = Form(...),   # ciktilar/ altındaki srt adı
    f_name:    str = Form("Arial"),
    f_size:    str = Form("22"),
    f_color:   str = Form("#ffffff"),
    is_bold:   str = Form("true"),
    is_shadow: str = Form("true"),
    m_v:       str = Form("20"),
):
    """Orijinal video + SRT'yi kullanıcının seçtiği ayarlarla burn eder ve indirme URL'si döner."""
    video_yolu = os.path.join(OUTPUT_DIR, video_adi)
    srt_yolu   = os.path.join(OUTPUT_DIR, srt_adi)
    if not os.path.isfile(video_yolu):
        return JSONResponse({"hata": "Video bulunamadı"}, status_code=404)
    if not os.path.isfile(srt_yolu):
        return JSONResponse({"hata": "SRT bulunamadı"}, status_code=404)
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": "FFmpeg sunucuda bulunamadı"}, status_code=500)

    base    = os.path.splitext(video_adi)[0]
    cikti_ad = f"{base}_burned.mp4"
    cikti_yolu = os.path.join(OUTPUT_DIR, cikti_ad)
    try:
        ok = ffmpeg_altyazi_gom(
            video_yolu, srt_yolu, cikti_yolu,
            f_name, f_size, f_color,
            is_bold == "true", is_shadow == "true", m_v
        )
    except Exception as e:
        return JSONResponse({"hata": str(e)}, status_code=500)

    if not ok:
        return JSONResponse({"hata": "FFmpeg altyazı gömme başarısız"}, status_code=500)

    log.info(f"[BurnSub] ✓ {cikti_ad}")
    return JSONResponse({"url": f"/ciktilar/{cikti_ad}", "dosya_adi": cikti_ad})


@app.get("/api/dosyalar/")
async def dosyalari_listele():
    try:
        names = _names_oku()
        dosyalar = []
        for fname in sorted(os.listdir(OUTPUT_DIR), key=lambda f: os.path.getmtime(os.path.join(OUTPUT_DIR, f)), reverse=True):
            fpath = os.path.join(OUTPUT_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".mp4", ".mp3", ".srt", ".vtt", ".txt", ".ass"):
                continue
            stat = os.stat(fpath)
            dosyalar.append({
                "ad":         fname,
                "goster_ad":  names.get(fname, ""),   # kullanıcının verdiği ad (boşsa fname kullanılır)
                "boyut":      round(stat.st_size / (1024*1024), 2),
                "tarih":      int(stat.st_mtime * 1000),
                "tur":        ext.lstrip("."),
                "url":        f"/ciktilar/{fname}",
            })
        return JSONResponse({"dosyalar": dosyalar[:50]})
    except Exception as e:
        return JSONResponse({"dosyalar": [], "hata": str(e)})


@app.post("/api/dosya_adlandir/")
async def dosya_adlandir(dosya_adi: str = Form(...), yeni_ad: str = Form(...)):
    """Dosyaya kullanıcı dostu bir görüntü adı atar (fiziksel dosyayı değiştirmez)."""
    yeni_ad = yeni_ad.strip()[:120]
    if not yeni_ad:
        return JSONResponse({"hata": "Ad boş olamaz"}, status_code=400)
    # Güvenlik: sadece bilinen çıktı dosyalarına izin ver
    fpath = os.path.join(OUTPUT_DIR, dosya_adi)
    if not os.path.isfile(fpath):
        return JSONResponse({"hata": "Dosya bulunamadı"}, status_code=404)
    names = _names_oku()
    names[dosya_adi] = yeni_ad
    _names_yaz(names)
    log.info(f"[Names] {dosya_adi} → '{yeni_ad}'")
    return JSONResponse({"ok": True, "ad": yeni_ad})


# ============================================================
# STRIPE — Ödeme & Webhook
# ============================================================
@app.post("/api/stripe/checkout/")
async def stripe_checkout(
    plan: str = Form(...),
    email: str = Form(""),
    user_id: str = Form(""),
    success_url: str = Form(""),
    cancel_url: str = Form(""),
):
    if not STRIPE_SECRET_KEY:
        return JSONResponse({"hata": "Stripe not configured. Add STRIPE_SECRET_KEY to environment."}, status_code=500)

    price_id = STRIPE_PRICES.get(plan.lower())
    if not price_id:
        return JSONResponse({"hata": f"Unknown plan: {plan}. Valid: creator, studio, business"}, status_code=400)

    import stripe as _stripe
    _stripe.api_key = STRIPE_SECRET_KEY

    # Default URLs
    base = success_url or "https://lumnex.ai"
    ok_url  = f"{base}/app?checkout=success&plan={plan}"
    ko_url  = f"{base}/app?checkout=cancel"

    try:
        params = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": ok_url,
            "cancel_url":  ko_url,
            "allow_promotion_codes": True,
            "billing_address_collection": "auto",
            "metadata": {"plan": plan, "user_id": user_id},
        }
        if email:
            params["customer_email"] = email

        session = _stripe.checkout.Session.create(**params)
        log.info(f"[Stripe] Checkout session: {session.id} plan={plan} email={email}")
        return JSONResponse({"checkout_url": session.url, "session_id": session.id})
    except Exception as e:
        log.error(f"[Stripe] Checkout error: {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/stripe/webhook/")
async def stripe_webhook(request: Request):
    import stripe as _stripe
    _stripe.api_key = STRIPE_SECRET_KEY

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = _stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        log.error(f"[Stripe Webhook] Signature error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    etype = event["type"]
    log.info(f"[Stripe Webhook] {etype}")

    if etype == "checkout.session.completed":
        session     = event["data"]["object"]
        email       = session.get("customer_email") or session.get("customer_details", {}).get("email", "")
        plan        = session.get("metadata", {}).get("plan", "creator")
        customer_id = session.get("customer", "")
        sub_id      = session.get("subscription", "")
        log.info(f"[Stripe] Payment OK — email={email} plan={plan} customer={customer_id}")
        await sb_plan_guncelle(email, plan, customer_id, sub_id)

    elif etype in ("customer.subscription.deleted", "customer.subscription.paused"):
        import stripe as _stripe2
        sub = event["data"]["object"]
        customer_id = sub.get("customer", "")
        log.info(f"[Stripe] Subscription cancelled — customer={customer_id}")
        # Müşteri e-postasını Stripe'dan çek
        if customer_id and STRIPE_SECRET_KEY:
            try:
                _stripe2.api_key = STRIPE_SECRET_KEY
                cust = _stripe2.Customer.retrieve(customer_id)
                email = cust.get("email", "")
                if email:
                    await sb_plan_guncelle(email, "lite", customer_id)
            except Exception as _se:
                log.error(f"[Stripe] Müşteri bilgisi alınamadı: {_se}")

    return JSONResponse({"received": True})

@app.get("/api/profil/")
async def profil_getir(user_id: str):
    """Kullanıcının güncel plan ve kullanım bilgisini döner."""
    if not user_id:
        return JSONResponse({"hata": "user_id gerekli"}, status_code=400)
    profil = await sb_profil_getir(user_id)
    if not profil:
        return JSONResponse({"hata": "Profil bulunamadı"}, status_code=404)

    import datetime
    plan = profil.get("plan", "lite")
    limitler = {"lite": 10, "creator": 75, "studio": 200, "business": 600}
    limit = limitler.get(plan, 10)

    bugun = datetime.date.today()
    ay_bas_str = profil.get("ay_baslangic") or str(bugun)
    ay_bas = datetime.date.fromisoformat(ay_bas_str[:10])
    if bugun.year != ay_bas.year or bugun.month != ay_bas.month:
        kullanim = 0.0
    else:
        kullanim = float(profil.get("kullanim_dakika") or 0)

    return JSONResponse({
        "plan": plan,
        "kullanim_dakika": round(kullanim, 1),
        "limit_dakika": limit,
        "kalan_dakika": round(max(0, limit - kullanim), 1),
        "email": profil.get("email", ""),
    })


@app.post("/api/kullanim_kaydet/")
async def kullanim_kaydet(user_id: str = Form(...), dosya_adi: str = Form(""), dakika: str = Form("0")):
    """İşlem sonrası kullanılan dakikayı Supabase'e yazar."""
    if not user_id:
        return JSONResponse({"ok": False, "sebep": "user_id eksik"})

    dk = 0.0
    # Önce dosyadan süre hesapla
    if dosya_adi:
        cikti_yolu = os.path.join(OUTPUT_DIR, dosya_adi)
        if os.path.exists(cikti_yolu):
            sure_sn = ses_sure_olc(cikti_yolu)
            dk = round(sure_sn / 60, 2) if sure_sn > 0 else 0.0

    # Dosya yoksa frontend'den gelen değeri kullan
    if dk <= 0:
        try:
            dk = float(dakika)
        except ValueError:
            dk = 0.0

    if dk <= 0:
        return JSONResponse({"ok": False, "sebep": "süre hesaplanamadı"})

    ok = await sb_kullanim_ekle(user_id, dk)
    return JSONResponse({"ok": ok, "eklenen_dakika": dk})


# ============================================================
# PUBLIC API — REST API v1 (API key auth)
# ============================================================

API_KEYS_PATH = os.path.join(os.getenv("DATA_DIR", "/app/data"), "api_keys.json")

def _api_keys_yukle() -> dict:
    try:
        if os.path.exists(API_KEYS_PATH):
            with open(API_KEYS_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _api_keys_kaydet(data: dict):
    os.makedirs(os.path.dirname(API_KEYS_PATH), exist_ok=True)
    with open(API_KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _api_key_dogrula(authorization: str = None) -> dict | None:
    """Authorization: Bearer <key> başlığından key al ve doğrula."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    key = authorization[7:].strip()
    keys = _api_keys_yukle()
    meta = keys.get(key)
    if meta and meta.get("aktif", True):
        return meta
    return None

async def _api_auth(request: Request) -> dict:
    """FastAPI dependency — API key zorunlu."""
    auth = request.headers.get("Authorization", "")
    meta = _api_key_dogrula(auth)
    if not meta:
        raise HTTPException(status_code=401, detail="Geçersiz veya eksik API key. Authorization: Bearer <key> başlığı gerekli.")
    return meta


# -- API Key yönetimi --

@app.post("/api/v1/keys/")
async def api_key_olustur(
    isim:    str = Form(...),
    user_id: str = Form(...),
):
    """Yeni API key oluşturur. user_id ile kullanıcıya bağlı."""
    import secrets, datetime
    key = "lmnx_" + secrets.token_urlsafe(32)
    keys = _api_keys_yukle()
    keys[key] = {
        "isim":       isim,
        "user_id":    user_id,
        "olusturuldu": datetime.datetime.utcnow().isoformat(),
        "istek_sayisi": 0,
        "aktif":      True,
    }
    _api_keys_kaydet(keys)
    log.info(f"[API] Yeni key oluşturuldu: {isim} ({user_id})")
    return JSONResponse({"api_key": key, "isim": isim})


@app.get("/api/v1/keys/")
async def api_key_listele(user_id: str):
    """Kullanıcının API key'lerini listeler (key'in son 8 karakteri gösterilir)."""
    keys = _api_keys_yukle()
    sonuc = []
    for k, v in keys.items():
        if v.get("user_id") == user_id:
            sonuc.append({
                "key_on":      k[:8] + "..." + k[-8:],
                "isim":        v.get("isim"),
                "olusturuldu": v.get("olusturuldu"),
                "istek_sayisi":v.get("istek_sayisi", 0),
                "aktif":       v.get("aktif", True),
            })
    return JSONResponse(sonuc)


@app.delete("/api/v1/keys/{key}")
async def api_key_iptal(key: str, user_id: str):
    """API key'i iptal eder (siler değil, devre dışı bırakır)."""
    keys = _api_keys_yukle()
    if key not in keys or keys[key].get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Key bulunamadı.")
    keys[key]["aktif"] = False
    _api_keys_kaydet(keys)
    return JSONResponse({"basari": True})


# -- API v1 endpointleri --

@app.get("/api/v1/status")
async def api_v1_status(request: Request):
    """API key geçerliliğini kontrol eder ve kota bilgisini döner."""
    meta = await _api_auth(request)
    return JSONResponse({
        "basari":       True,
        "isim":         meta.get("isim"),
        "user_id":      meta.get("user_id"),
        "istek_sayisi": meta.get("istek_sayisi", 0),
        "api_versiyonu": "v1",
        "ozellikler":   ["transcribe", "translate", "process"],
    })


@app.post("/api/v1/transcribe")
async def api_v1_transcribe(
    request:      Request,
    video:        UploadFile = File(...),
    kaynak_dil:   str = Form("tr"),
    hedef_dil:    str = Form(""),
    format:       str = Form("srt"),   # srt | json | txt
):
    """
    Video/ses dosyasını deşifre eder, isteğe bağlı çevirir.
    Döndürür: { "srt": "...", "txt": "...", "dil": "tr" }
    Kimlik doğrulama: Authorization: Bearer <api_key>
    """
    meta = await _api_auth(request)

    b_id = uuid.uuid4().hex[:8]
    ext  = os.path.splitext(video.filename or "input.mp4")[1] or ".mp4"
    tmp_in  = os.path.join(TEMP_DIR, f"apiv1_{b_id}{ext}")
    tmp_mp3 = os.path.join(TEMP_DIR, f"apiv1_{b_id}.mp3")

    try:
        with open(tmp_in, "wb") as f:
            shutil.copyfileobj(video.file, f)

        # Ses çıkar
        await asyncio.to_thread(subprocess.run, [
            "ffmpeg", "-y", "-i", tmp_in, "-q:a", "2", "-vn", tmp_mp3
        ], capture_output=True, timeout=120)

        if not os.path.exists(tmp_mp3):
            raise ValueError("Ses çıkarılamadı")

        # Deepgram
        dg_response = await deepgram_desifre_et(tmp_mp3, kaynak_dil)
        tmp_srt = os.path.join(TEMP_DIR, f"apiv1_{b_id}.srt")
        deepgram_to_srt(dg_response, tmp_srt)
        with open(tmp_srt, encoding="utf-8") as f:
            srt_icerik = f.read()
        try: os.remove(tmp_srt)
        except Exception: pass

        # Opsiyonel çeviri
        if hedef_dil and hedef_dil.upper() != kaynak_dil.upper() and DEEPL_API_KEY:
            srt_icerik = await srt_paralel_cevir(srt_icerik, hedef_dil)

        # Format
        if format == "txt":
            lines = [l for l in srt_icerik.splitlines() if l.strip() and "-->" not in l and not l.strip().isdigit()]
            cikti = "\n".join(lines)
        elif format == "json":
            bloklar = []
            for blok in srt_icerik.strip().split("\n\n"):
                s = blok.strip().split("\n")
                if len(s) >= 3:
                    bloklar.append({"index": s[0], "zaman": s[1], "metin": "\n".join(s[2:])})
            cikti = json.dumps(bloklar, ensure_ascii=False)
        else:
            cikti = srt_icerik

        # İstek sayacını artır
        keys = _api_keys_yukle()
        auth_key = request.headers.get("Authorization", "")[7:].strip()
        if auth_key in keys:
            keys[auth_key]["istek_sayisi"] = keys[auth_key].get("istek_sayisi", 0) + 1
            _api_keys_kaydet(keys)

        return JSONResponse({
            "basari":     True,
            "format":     format,
            "kaynak_dil": kaynak_dil,
            "hedef_dil":  hedef_dil or kaynak_dil,
            "icerik":     cikti,
        })

    except Exception as e:
        log.error(f"[API v1 /transcribe] {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for f in [tmp_in, tmp_mp3]:
            try: os.remove(f)
            except Exception: pass


@app.post("/api/v1/translate")
async def api_v1_translate(
    request:    Request,
    icerik:     str = Form(...),   # SRT veya düz metin
    hedef_dil:  str = Form(...),
    format:     str = Form("srt"),
):
    """
    SRT veya düz metni DeepL + Glossary ile çevirir.
    Kimlik doğrulama: Authorization: Bearer <api_key>
    """
    await _api_auth(request)
    try:
        if format == "srt":
            sonuc = await srt_paralel_cevir(icerik, hedef_dil)
        else:
            # Düz metin
            satirlar = [icerik]
            cevrilen = await deepl_paralel_cevir_listesi(satirlar, hedef_dil)
            sonuc = cevrilen[0] if cevrilen else icerik

        return JSONResponse({"basari": True, "hedef_dil": hedef_dil, "icerik": sonuc})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════════════════════════════
# GOOGLE DRIVE OAUTH
# ════════════════════════════════════════════════════════════════

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "")

# state → {user_id, ts}  /  token_key → {access_token, ts}
_gdrive_state: dict = {}
_gdrive_tokens: dict = {}


@app.get("/api/gdrive/auth/")
async def gdrive_auth(user_id: str = ""):
    if not GOOGLE_CLIENT_ID:
        return JSONResponse({"hata": "GOOGLE_CLIENT_ID env var eksik"}, status_code=500)
    from urllib.parse import urlencode
    from fastapi.responses import RedirectResponse

    state = uuid.uuid4().hex
    _gdrive_state[state] = {"user_id": user_id, "ts": __import__("time").time()}

    redirect = GOOGLE_REDIRECT_URI or "https://voiceflow-studio-production-eebc.up.railway.app/api/gdrive/callback/"
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/drive.readonly",
        "access_type": "offline",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@app.get("/api/gdrive/callback/")
async def gdrive_callback(code: str = "", state: str = "", error: str = ""):
    from fastapi.responses import RedirectResponse

    if error:
        return RedirectResponse(f"/app?gdrive_error={error}")

    stored = _gdrive_state.pop(state, {})
    redirect = GOOGLE_REDIRECT_URI or "https://voiceflow-studio-production-eebc.up.railway.app/api/gdrive/callback/"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": redirect,
                    "grant_type": "authorization_code",
                },
                timeout=15.0,
            )
            r.raise_for_status()
            tok = r.json()
    except Exception as e:
        log.error(f"[GDrive] Token exchange hatası: {e}")
        return RedirectResponse("/app?gdrive_error=token_failed")

    token_key = uuid.uuid4().hex
    _gdrive_tokens[token_key] = {
        "access_token": tok.get("access_token", ""),
        "user_id": stored.get("user_id", ""),
        "ts": __import__("time").time(),
    }
    log.info(f"[GDrive] OAuth başarılı — token_key={token_key[:8]}...")
    return RedirectResponse(f"/app?gdrive_token={token_key}")


@app.get("/api/gdrive/files/")
async def gdrive_files(token: str):
    stored = _gdrive_tokens.get(token)
    if not stored:
        return JSONResponse({"hata": "Geçersiz veya süresi dolmuş token"}, status_code=401)

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                headers={"Authorization": f"Bearer {stored['access_token']}"},
                params={
                    "q": "mimeType contains 'video/' and trashed=false",
                    "fields": "files(id,name,size,mimeType,modifiedTime)",
                    "orderBy": "modifiedTime desc",
                    "pageSize": "50",
                },
                timeout=15.0,
            )
            if r.status_code == 401:
                return JSONResponse({"hata": "Google oturumu sona erdi. Tekrar giriş yapın."}, status_code=401)
            r.raise_for_status()
            return JSONResponse(r.json())
    except Exception as e:
        log.error(f"[GDrive] Dosya listesi hatası: {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)


@app.post("/api/gdrive/import/")
async def gdrive_import(
    token: str = Form(...),
    file_id: str = Form(...),
    file_name: str = Form(...),
):
    """Google Drive'dan video indir → ciktilar klasörüne kaydet."""
    stored = _gdrive_tokens.get(token)
    if not stored:
        return JSONResponse({"hata": "Geçersiz token"}, status_code=401)

    ext = os.path.splitext(file_name)[1] or ".mp4"
    b_id = uuid.uuid4().hex[:8]
    cikti_adi = f"gdrive_{b_id}{ext}"
    cikti_yol = os.path.join(OUTPUT_DIR, cikti_adi)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
            async with client.stream(
                "GET",
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                headers={"Authorization": f"Bearer {stored['access_token']}"},
                params={"alt": "media"},
            ) as r:
                r.raise_for_status()
                with open(cikti_yol, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        f.write(chunk)

        boyut_mb = os.path.getsize(cikti_yol) / (1024 * 1024)
        log.info(f"[GDrive] İndirildi: {file_name} → {cikti_adi} ({boyut_mb:.1f}MB)")
        return JSONResponse({
            "basari": True,
            "dosya_adi": cikti_adi,
            "boyut_mb": round(boyut_mb, 1),
            "orijinal_ad": file_name,
        })
    except Exception as e:
        if os.path.exists(cikti_yol):
            os.unlink(cikti_yol)
        log.error(f"[GDrive] İndirme hatası: {e}")
        return JSONResponse({"hata": f"İndirme başarısız: {e}"}, status_code=500)


# ════════════════════════════════════════════════════════════════
# TRANSCRIPT EDITOR — Video Bölgesi Kes
# ════════════════════════════════════════════════════════════════

@app.post("/api/transcript_kes/")
async def transcript_kes(
    video_dosya_adi: str = Form(...),
    kes_baslangic: float = Form(...),
    kes_bitis: float = Form(...),
):
    """
    Maestra-style: Video'dan [kes_baslangic, kes_bitis] aralığını sil.
    [0 → kes_baslangic] + [kes_bitis → son] concat eder.
    """
    if not ffmpeg_var_mi():
        return JSONResponse({"hata": "FFmpeg yüklü değil"}, status_code=500)

    giris = os.path.join(OUTPUT_DIR, video_dosya_adi)
    if not os.path.exists(giris):
        return JSONResponse({"hata": "Video bulunamadı"}, status_code=404)

    b_id = uuid.uuid4().hex[:8]
    tmp = os.path.join(TEMP_DIR, f"tkes_{b_id}")
    os.makedirs(tmp, exist_ok=True)

    p1 = os.path.join(tmp, "p1.mp4")
    p2 = os.path.join(tmp, "p2.mp4")
    liste = os.path.join(tmp, "list.txt")
    cikti_adi = f"edited_{b_id}.mp4"
    cikti_yol = os.path.join(OUTPUT_DIR, cikti_adi)

    encode = ["-c:v", "libx264", "-c:a", "aac", "-preset", "fast"]

    try:
        parcalar = []

        # Parça 1: 0 → kes_baslangic
        if kes_baslangic > 0.1:
            r1 = await asyncio.to_thread(
                subprocess.run,
                ["ffmpeg", "-y", "-i", giris, "-t", str(kes_baslangic)] + encode + [p1],
                capture_output=True, text=True, timeout=300,
            )
            if r1.returncode == 0:
                parcalar.append(p1)

        # Parça 2: kes_bitis → son
        r2 = await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-ss", str(kes_bitis), "-i", giris] + encode + [p2],
            capture_output=True, text=True, timeout=300,
        )
        if r2.returncode == 0 and os.path.getsize(p2) > 1000:
            parcalar.append(p2)

        if not parcalar:
            return JSONResponse({"hata": "Kesilecek parça oluşturulamadı"}, status_code=500)

        if len(parcalar) == 1:
            shutil.copy(parcalar[0], cikti_yol)
        else:
            with open(liste, "w") as f:
                for p in parcalar:
                    f.write(f"file '{p}'\n")
            r3 = await asyncio.to_thread(
                subprocess.run,
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", liste, "-c", "copy", cikti_yol],
                capture_output=True, text=True, timeout=300,
            )
            if r3.returncode != 0:
                return JSONResponse({"hata": "Concat başarısız: " + r3.stderr[-200:]}, status_code=500)

        log.info(f"[Transcript Kes] {video_dosya_adi} [{kes_baslangic:.2f}→{kes_bitis:.2f}s] → {cikti_adi}")
        return JSONResponse({"basari": True, "cikti": cikti_adi})

    except Exception as e:
        log.error(f"[Transcript Kes Hata] {e}")
        return JSONResponse({"hata": str(e)}, status_code=500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
