"""
ربات شهر سفال v3 - پست‌گذاری خودکار محصولات + پنل مدیریت کامل در تلگرام
- منبع محصولات: جدول products در Supabase (product_id, name, slug, permalink, image_url)
- هیچ همگام‌سازی خودکاری با سایت وجود ندارد؛ محصول جدید فقط با ارسال لینک به ربات اضافه می‌شود
- مقصدها: بله، تلگرام و روبیکا (هرکدام که توکن و مقصدش تنظیم شود)، همه با یک متن و یک محصول در هر اسلات
- متن پست: Gemini با چرخش بین چند کلید
"""
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlparse, unquote
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ------------------------- تنظیمات (از Environment Variables) -------------------------
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"متغیر محیطی {name} تنظیم نشده است")
    return v

BALE_TOKEN = env("BALE_TOKEN", required=True)
BALE_TARGET = env("BALE_TARGET") or env("CHANNEL_ID")    # مقصد پست در بله: @channel یا شناسه‌ی عددی (اختیاری)
SB_URL = env("SUPABASE_URL", required=True).rstrip("/")
SB_KEY = env("SUPABASE_KEY", required=True)
GEMINI_KEYS = [k.strip() for k in env("GEMINI_KEYS", "").split(",") if k.strip()]
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
SITE_URL = env("SITE_URL", "https://citysofal.ir").rstrip("/")
ADMIN_USERNAME = env("ADMIN_USERNAME", "").lstrip("@")
ADMIN_PHONE = env("ADMIN_PHONE", "")
POST_TIMES = [t.strip() for t in env("POST_TIMES", "10:00,13:00,16:00,19:00,21:00").split(",") if t.strip()]
RUBIKA_LINK_BUTTON = env("RUBIKA_LINK_BUTTON", "true").lower() == "true"   # دکمه‌ی شیشه‌ای روبیکا؛ اگر خطا داد خودکار بدون دکمه می‌فرستد
ORDER_MODE = env("ORDER_MODE", "random")               # random | sequential
CATCHUP_MINUTES = int(env("CATCHUP_MINUTES", "180"))   # اگر سرور خواب بود تا چند دقیقه دیرتر هم پست بزند
TZ = ZoneInfo(env("TZ_NAME", "Asia/Tehran"))
UA = {"User-Agent": "Mozilla/5.0 (compatible; CitySofalBot/1.0)"}
T = "products"                                         # نام جدول محصولات

# توکن‌ها و کلیدها هرگز در لاگ دیده نشوند (پیام خطای requests شامل آدرس با توکن است)
_SECRETS = [x for x in (BALE_TOKEN, env("TELEGRAM_TOKEN"), env("RUBIKA_TOKEN"), SB_KEY, env("SETUP_KEY"), *GEMINI_KEYS)
            if x and len(x) >= 6]

class _Redact(logging.Filter):
    @staticmethod
    def _scrub(text):
        for x in _SECRETS:
            text = text.replace(x, "***")
        return text
    def filter(self, record):
        record.msg, record.args = self._scrub(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = self._scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True

for _h in logging.getLogger().handlers:
    _h.addFilter(_Redact())

def _valid_time(t):
    try:
        hh, mm = map(int, t.split(":"))
        return 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        return False

_bad = [t for t in POST_TIMES if not _valid_time(t)]
if _bad:
    log.error("ساعت نامعتبر در POST_TIMES نادیده گرفته شد: %s (قالب درست: 10:00)", _bad)
POST_TIMES = [t for t in POST_TIMES if _valid_time(t)]

STYLE_HINTS = [
    "لحن گرم و صمیمی، تأکید بر زیبایی و حس خانه",
    "تأکید بر کاربرد و استفاده‌ی روزمره",
    "به‌عنوان یک ایده‌ی هدیه‌ی خاص",
    "تأکید بر چیدمان و دکوراسیون خانه",
    "کوتاه، شاعرانه و دلنشین",
]

# ------------------------- Supabase (REST ساده) -------------------------
def sb(method, table, params=None, body=None, prefer=None):
    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    return requests.request(method, f"{SB_URL}/rest/v1/{table}", headers=headers, params=params,
                            data=json.dumps(body) if body is not None else None, timeout=30)

# ------------------------- افزودن محصول با لینک -------------------------
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", unescape(s)).strip()

def parse_product_link(link):
    """لینک باید از همین سایت و به شکل /product/نام-محصول/ باشد؛ برمی‌گرداند (لینک تمیز، slug)."""
    link = link.strip().rstrip(".,)»،؛؟!")
    u = urlparse(link)
    strip_www = lambda h: h.lower().removeprefix("www.")
    if strip_www(u.netloc) != strip_www(urlparse(SITE_URL).netloc):
        raise RuntimeError("لینک باید از سایت فروشگاه باشد")
    parts = [x for x in u.path.split("/") if x]
    if len(parts) < 2 or parts[-2] != "product":
        raise RuntimeError("این لینک، لینک یک محصول نیست (باید شکل /product/نام-محصول/ باشد)")
    return link, unquote(parts[-1])

def find_in_db(link):
    """محصول را بدون درخواست به سایت از دیتابیس پیدا می‌کند (با slug یا permalink)."""
    link, slug = parse_product_link(link)
    for col, val in (("slug", slug), ("permalink", link), ("permalink", link.rstrip("/") + "/")):
        r = sb("GET", T, {col: f"eq.{val}", "select": "*", "limit": 1})
        if r.ok and r.json():
            return r.json()[0]
    return None

def fetch_by_link(link):
    """اول از API سایت (۱ درخواست سبک)، اگر نشد از دیتابیس، آخرین راه تگ‌های og صفحه."""
    link, slug = parse_product_link(link)
    try:
        r = requests.get(f"{SITE_URL}/wp-json/wc/store/v1/products",
                         params={"slug": slug}, headers=UA, timeout=30)
        if r.status_code == 200 and r.json():
            p = r.json()[0]
            imgs = p.get("images") or []
            return {"product_id": p["id"], "name": strip_html(p.get("name")), "slug": p.get("slug") or slug,
                    "permalink": p.get("permalink") or link,
                    "image_url": imgs[0]["src"] if imgs else None,
                    "active": bool(p.get("is_in_stock", True))}
    except Exception as e:
        log.warning("Store API در دسترس نیست: %s", e)
    row = find_in_db(link)               # جلوگیری از ساخت ردیف تکراری با شناسه‌ی جعلی
    if row:
        return {k: row.get(k) for k in ("product_id", "name", "slug", "permalink", "image_url", "active")}
    r = requests.get(link, headers=UA, timeout=30)
    r.raise_for_status()
    def og(name):
        m = re.search(r'<meta[^>]+property=["\']og:%s["\'][^>]+content=["\']([^"\']+)' % name, r.text)
        return unescape(m.group(1)).strip() if m else None
    title = og("title")
    if not title:
        raise RuntimeError("اطلاعات محصول از لینک خوانده نشد")
    title = re.sub(r"\s*[-–|]\s*شهر سفال\s*$", "", title)
    return {"product_id": zlib.crc32(link.encode()) & 0x7FFFFFFF, "name": title, "slug": slug,
            "permalink": link, "image_url": og("image"), "active": True}

def upsert_rows(rows):
    # فقط ستون‌های موجود در rows به‌روز می‌شوند؛ posted_count و mention_count دست‌نخورده می‌مانند
    r = sb("POST", T, {"on_conflict": "product_id"}, rows, "resolution=merge-duplicates")
    if not r.ok:
        raise RuntimeError(f"ذخیره در Supabase ناموفق: {r.text[:200]}")

# ------------------------- انتخاب محصول بعدی (چرخه) -------------------------
def fetch_all(table, params, page=1000):
    out, off = [], 0
    while True:
        r = sb("GET", table, dict(params, limit=page, offset=off))
        if not r.ok:
            return None, r
        rows = r.json() or []
        out += rows
        if len(rows) < page:
            return out, r
        off += page

def pick_product():
    items, r = fetch_all(T, {"select": "*", "active": "eq.true", "order": "product_id.asc"})
    if items is None:
        log.error("خواندن محصولات ناموفق: %s", r.text[:200])
        items = []
    items = [p for p in items if p.get("image_url") and p.get("permalink")]
    if not items:
        return None
    min_count = min(p["posted_count"] for p in items)      # فقط از کم‌پستی‌ها انتخاب می‌شود => چرخه‌ی بدون تکرار
    pool = [p for p in items if p["posted_count"] == min_count]
    if ORDER_MODE == "sequential":
        return min(pool, key=lambda p: p["product_id"])
    return random.choice(pool)

# ------------------------- Gemini -------------------------
_key_idx = 0
GEMINI_ENDPOINTS = [   # کلیدهای AIza... روی اولی و کلیدهای AQ... (Vertex Express) روی دومی کار می‌کنند
    "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
    "https://aiplatform.googleapis.com/v1/publishers/google/models/{m}:generateContent",
]
_key_ep = {}   # شماره‌ی کلید -> آدرسی که برایش جواب داده

def gemini_text(prompt):
    global _key_idx
    if not GEMINI_KEYS:
        return None
    n = len(GEMINI_KEYS)
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 1.0, "maxOutputTokens": 1200}}
    for i in range(n):
        idx = (_key_idx + i) % n
        key = GEMINI_KEYS[idx]
        eps = [_key_ep[idx]] if idx in _key_ep else range(len(GEMINI_ENDPOINTS))
        for e in eps:
            try:
                r = requests.post(GEMINI_ENDPOINTS[e].format(m=GEMINI_MODEL),
                                  headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                  json=body, timeout=40)
                if r.status_code == 200:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                    if txt:
                        _key_ep[idx] = e
                        _key_idx = (idx + 1) % n           # کلید بعدی برای نوبت بعد
                        return txt
                log.warning("gemini key#%d ep%d -> HTTP %s %s", idx, e, r.status_code, r.text[:160].replace("\n", " "))
                if r.status_code in (429, 500, 503):
                    break                                    # این کلید فعلاً محدود است؛ کلید بعدی
            except Exception as ex:
                log.warning("gemini key#%d ep%d error: %s", idx, e, ex)
    return None

FALLBACK_BODY = "یک انتخاب زیبا برای خانه‌ی شما ✨\nجزئیات و تصاویر بیشتر را در سایت ببینید."

def clean_ai_text(raw):
    """فقط متن بین <<< و >>> قبول می‌شود؛ هر چیز دیگر (فکر کردن مدل، توضیح، انگلیسی) دور ریخته می‌شود."""
    m = re.search(r"<<<(.*?)>>>", raw or "", re.S)
    if not m:
        return None
    txt = re.sub(r"[*`\[\]<>#]", "", m.group(1)).strip()
    letters = re.findall(r"[A-Za-z\u0600-\u06FF]", txt)
    persian = re.findall(r"[\u0600-\u06FF]", txt)
    if len(txt) < 15 or not letters or len(persian) / len(letters) < 0.85:   # بیش از ۱۵٪ حروف لاتین = مشکوک
        return None
    return txt[:350]

def ai_body(p):
    prompt = f"""تو کپی‌رایتر کانال فروشگاه «شهر سفال» (سفال، سرامیک، گلدان، دکوری) هستی.
برای محصول زیر یک متن کوتاه فارسی برای پست کانال بنویس.

قوانین:
- ۲ تا ۳ خط کوتاه، حداکثر ۲۵۰ کاراکتر، لحن: {random.choice(STYLE_HINTS)}
- فقط به زبان فارسی بنویس؛ هیچ کلمه‌ی انگلیسی ننویس.
- فقط از اطلاعات زیر استفاده کن؛ ابعاد، جنس، ضمانت، تضمین یا ویژگی‌ای که از نام محصول معلوم نیست را ننویس.
- قیمت، شماره تماس، لینک و هشتگ ننویس.
- از ستاره، کروشه و بک‌تیک استفاده نکن. حداکثر ۲ ایموجی مرتبط.
- با «سلام» یا «به نظر من» شروع نکن.
- استدلال، توضیح و پیش‌نویس ننویس. فقط متن نهایی پست را بین <<< و >>> بگذار، مثل: <<<متن پست>>>

نام محصول: {p['name']}
نام انگلیسی (از آدرس، فقط برای درک بهتر): {(p.get('slug') or '').replace('-', ' ')}"""
    for _ in range(2):                    # دو تلاش (کلید بعدی)، بعد متن پیش‌فرض
        txt = clean_ai_text(gemini_text(prompt))
        if txt:
            return txt
        log.warning("خروجی Gemini نامعتبر بود؛ دوباره تلاش می‌شود")
    return FALLBACK_BODY

def build_caption(p, body, plat):
    lines = [f"✅ {p['name']}", "", body, "", "🌐 برای مشاهده قیمت و سفارش وارد سایت شوید"]
    if plat.admin:
        lines.append(f"💬 سفارش: @{plat.admin}")
    if ADMIN_PHONE:
        lines.append(f"📞 {ADMIN_PHONE}")
    return "\n".join(lines)[:1000]

# ------------------------- پلتفرم‌ها (بله / تلگرام / روبیکا) -------------------------
def prepare_image(url):
    """تصویر را دانلود و به JPEG سبک تبدیل می‌کند (webp/png سنگین مشکل‌ساز است)."""
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(r.content))
        if im.mode in ("RGBA", "LA", "P"):                 # پس‌زمینه‌ی شفاف PNG => سفید
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return r.content

MENU_LABELS = {"add": "➕ افزودن محصول (با لینک)", "status": "📊 وضعیت چرخه", "post": "📤 ارسال فوری"}
MENU_HELP = "چه کاری انجام بدم؟"
START_TS = time.time()

class Platform:
    name = ""
    def __init__(self, token, target, admin, admins):
        self.token, self.target = token, target
        self.admin = (admin or "").lstrip("@")
        self.admins = admins                      # مجموعه‌ی شناسه‌های ادمین (رشته)
    def admin_url(self):
        return None
    def send_post(self, p, caption, img):
        raise NotImplementedError
    def send_text(self, chat, text):
        raise NotImplementedError
    def send_menu(self, chat, text=MENU_HELP):
        raise NotImplementedError
    def poll(self, handler):
        raise NotImplementedError

class TgLike(Platform):
    """بله و تلگرام ساختار Bot API یکسان دارند (sendPhoto + دکمه‌ی inline با url)."""
    api = ""
    def keyboard(self, p):
        rows = [[{"text": "مشاهده در سایت", "url": p["permalink"]}]]
        if self.admin:
            rows.append([{"text": "ارسال پیام", "url": self.admin_url()}])
        return {"inline_keyboard": rows}
    def send_post(self, p, caption, img):
        if img:
            try:
                r = requests.post(f"{self.api}/sendPhoto",
                                  data={"chat_id": self.target, "caption": caption,
                                        "reply_markup": json.dumps(self.keyboard(p), ensure_ascii=False)},
                                  files={"photo": ("product.jpg", img, "image/jpeg")}, timeout=90)
                j = r.json()
                if j.get("ok"):
                    return True
                log.warning("%s sendPhoto خطا: %s", self.name, j)
            except Exception as e:
                log.warning("%s sendPhoto exception: %s", self.name, e)
        r = requests.post(f"{self.api}/sendMessage",
                          json={"chat_id": self.target, "text": f"{caption}\n\n🔗 {p['permalink']}"}, timeout=30)
        ok = r.json().get("ok", False)
        if not ok:
            log.error("%s sendMessage هم خطا داد: %s", self.name, r.text[:300])
        return ok
    def send_text(self, chat, text):
        try:
            requests.post(f"{self.api}/sendMessage", json={"chat_id": chat, "text": text}, timeout=20)
        except Exception as e:
            log.warning("%s send_text: %s", self.name, e)
    def send_menu(self, chat, text=MENU_HELP):
        kb = {"inline_keyboard": [
            [{"text": MENU_LABELS["add"], "callback_data": "add"}],
            [{"text": MENU_LABELS["status"], "callback_data": "status"},
             {"text": MENU_LABELS["post"], "callback_data": "post"}]]}
        try:
            requests.post(f"{self.api}/sendMessage", json={"chat_id": chat, "text": text, "reply_markup": kb}, timeout=20)
        except Exception as e:
            log.warning("%s menu: %s", self.name, e)
    def _updates(self, offset, timeout):
        r = requests.get(f"{self.api}/getUpdates", params={"offset": offset, "timeout": timeout}, timeout=timeout + 15)
        return r.json().get("result", [])
    def _process(self, u, handler):
        if u.get("callback_query"):
            cb = u["callback_query"]
            try:
                requests.post(f"{self.api}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=15)
            except Exception:
                pass
            handler(self, "button", cb["from"]["id"], cb["message"]["chat"]["id"], cb.get("data"))
        elif u.get("message"):
            m = u["message"]
            handler(self, "message", (m.get("from") or {}).get("id"), m["chat"]["id"], m.get("text") or "")
    def poll(self, handler):
        try:
            requests.post(f"{self.api}/deleteWebhook", timeout=15)
        except Exception:
            pass
        offset = 0
        try:                                   # پیام‌های قدیمیِ مانده در صف دوباره اجرا نشوند
            for _ in range(30):
                ups = self._updates(offset, 0)
                if not ups:
                    break
                offset = ups[-1]["update_id"] + 1
        except Exception as e:
            log.warning("%s drain: %s", self.name, e)
        while True:
            try:
                for u in self._updates(offset, 25):
                    offset = u["update_id"] + 1
                    try:
                        self._process(u, handler)
                    except Exception as e:
                        log.warning("%s handler: %s", self.name, e)
            except Exception as e:
                log.warning("%s poll: %s", self.name, e)
                time.sleep(5)

class Bale(TgLike):
    name = "bale"
    def __init__(self, *a):
        super().__init__(*a)
        self.api = f"https://tapi.bale.ai/bot{self.token}"
    def admin_url(self):
        return f"https://ble.ir/{self.admin}"

class Telegram(TgLike):
    name = "telegram"
    def __init__(self, *a):
        super().__init__(*a)
        self.api = f"https://api.telegram.org/bot{self.token}"
    def admin_url(self):
        return f"https://t.me/{self.admin}"
    def _process(self, u, handler):
        handler(self, u)                      # پنل v3 خودش آپدیت خام را تفسیر می‌کند

class Rubika(Platform):
    """روبیکا: ارسال = requestSendFile -> آپلود -> sendFile. پنل ادمین با دکمه‌های پایین چت (ChatKeypad) و getUpdates."""
    name = "rubika"
    def __init__(self, *a):
        super().__init__(*a)
        self._seen = {}                       # آپدیت‌های پردازش‌شده (ضد اجرای تکراری)
    def _call(self, method, payload):
        r = requests.post(f"https://botapi.rubika.ir/v3/{self.token}/{method}", json=payload, timeout=40)
        return r.json()
    @staticmethod
    def _ok(j):
        return str(j.get("status", "")).upper() == "OK"
    def admin_url(self):
        return f"https://rubika.ir/{self.admin}"
    def _inline(self, p):
        """دکمه‌های شیشه‌ای زیر پست: مشاهده در سایت + ارسال پیام (Button از نوع Link)."""
        def link(bid, label, url):
            return {"id": bid, "type": "Link", "button_text": label,
                    "button_link": {"type": "url", "link_url": url}}
        rows = [{"buttons": [link("site", "مشاهده در سایت", p["permalink"])]}]
        if self.admin:
            rows.append({"buttons": [link("msg", "ارسال پیام", self.admin_url())]})
        return {"rows": rows}
    def send_post(self, p, caption, img):
        text = f"{caption}\n\n🔗 {p['permalink']}"
        kp = self._inline(p) if RUBIKA_LINK_BUTTON else None
        if img:
            try:
                j = self._call("requestSendFile", {"type": "Image"})
                upload_url = (j.get("data") or j).get("upload_url")
                up = requests.post(upload_url, files={"file": ("product.jpg", img, "image/jpeg")}, timeout=90).json()
                file_id = (up.get("data") or up).get("file_id")
                if file_id:
                    payload = {"chat_id": self.target, "file_id": file_id, "text": text}
                    if kp:
                        j = self._call("sendFile", dict(payload, inline_keypad=kp))
                        if self._ok(j):
                            return True
                        log.warning("rubika sendFile با دکمه خطا داد، بدون دکمه تلاش می‌شود: %s", j)
                    j = self._call("sendFile", payload)
                    if self._ok(j):
                        return True
                    log.warning("rubika sendFile خطا: %s", j)
            except Exception as e:
                log.warning("rubika file exception: %s", e)
        j = self._call("sendMessage", {"chat_id": self.target, "text": text, "inline_keypad": kp} if kp
                       else {"chat_id": self.target, "text": text})
        if not self._ok(j) and kp:
            j = self._call("sendMessage", {"chat_id": self.target, "text": text})
        if not self._ok(j):
            log.error("rubika sendMessage خطا: %s", j)
        return self._ok(j)
    def send_text(self, chat, text):
        try:
            self._call("sendMessage", {"chat_id": chat, "text": text})
        except Exception as e:
            log.warning("rubika send_text: %s", e)
    def send_menu(self, chat, text=MENU_HELP):
        keypad = {"rows": [
            {"buttons": [{"id": "add", "type": "Simple", "button_text": MENU_LABELS["add"]}]},
            {"buttons": [{"id": "status", "type": "Simple", "button_text": MENU_LABELS["status"]},
                         {"id": "post", "type": "Simple", "button_text": MENU_LABELS["post"]}]}],
            "resize_keyboard": True, "one_time_keyboard": False}
        try:
            self._call("sendMessage", {"chat_id": chat, "text": text,
                                       "chat_keypad": keypad, "chat_keypad_type": "New"})
        except Exception as e:
            log.warning("rubika menu: %s", e)
    def _process(self, u, handler):
        h = hashlib.md5(json.dumps(u, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if h in self._seen:
            return
        self._seen[h] = 1
        if len(self._seen) > 2000:
            self._seen.pop(next(iter(self._seen)))
        t, chat = u.get("type"), u.get("chat_id")
        if t == "StartedBot":
            handler(self, "message", chat, chat, "/start")
            return
        if t != "NewMessage":
            return
        nm = u.get("new_message") or {}
        if nm.get("sender_type") != "User":          # پیام‌های خود بات را نادیده بگیر
            return
        try:
            if int(nm.get("time") or 0) < START_TS - 30:   # پیام‌های قدیمی قبل از راه‌اندازی
                return
        except Exception:
            pass
        uid = nm.get("sender_id") or chat
        text = (nm.get("text") or "").strip()
        bid = (nm.get("aux_data") or {}).get("button_id")
        by_label = {v: k for k, v in MENU_LABELS.items()}
        if bid in MENU_LABELS:
            handler(self, "button", uid, chat, bid)
        elif text in by_label:
            handler(self, "button", uid, chat, by_label[text])
        else:
            handler(self, "message", uid, chat, text)
    def poll(self, handler):
        offset = None
        def fetch():
            payload = {"limit": 50}
            if offset:
                payload["offset_id"] = offset
            d = self._call("getUpdates", payload)
            return d.get("data") or d
        try:                                          # صف قدیمی را رد کن
            for _ in range(20):
                d = fetch()
                if d.get("next_offset_id"):
                    offset = d["next_offset_id"]
                if not d.get("updates"):
                    break
        except Exception as e:
            log.warning("rubika drain: %s", e)
        while True:
            try:
                d = fetch()
                for u in d.get("updates") or []:
                    try:
                        self._process(u, handler)
                    except Exception as e:
                        log.warning("rubika handler: %s", e)
                if d.get("next_offset_id"):
                    offset = d["next_offset_id"]
            except Exception as e:
                log.warning("rubika poll: %s", e)
            time.sleep(3)

def _ids(*keys):
    s = set()
    for k in keys:
        for x in env(k, "").split(","):
            if x.strip():
                s.add(x.strip())
    return s

# آیدی ادمین ثبت سفارش در هر پلتفرم (در هر پلتفرم فرق دارد)
DEFAULT_ADMIN_USERNAMES = {"BALE": "city_sofal", "RUBIKA": "Citysofal", "TELEGRAM": "Aso_babsor"}

def build_platforms():
    def adm(key):
        # اولویت: متغیر ADMIN_USERNAME_<پلتفرم> در رندر > آیدی پیش‌فرض همین پلتفرم > ADMIN_USERNAME عمومی
        return env(f"ADMIN_USERNAME_{key}", "") or DEFAULT_ADMIN_USERNAMES.get(key, "") or ADMIN_USERNAME
    out = [Bale(BALE_TOKEN, BALE_TARGET, adm("BALE"), _ids("ADMIN_USER_IDS_BALE", "ADMIN_USER_IDS"))]
    if env("TELEGRAM_TOKEN"):
        out.append(Telegram(env("TELEGRAM_TOKEN"), env("TELEGRAM_TARGET"), adm("TELEGRAM"), _ids("ADMIN_USER_IDS_TELEGRAM")))
    if env("RUBIKA_TOKEN"):
        out.append(Rubika(env("RUBIKA_TOKEN"), env("RUBIKA_TARGET"), adm("RUBIKA"), _ids("ADMIN_USER_IDS_RUBIKA")))
    return out

ALL_PLATFORMS = build_platforms()                       # برای پنل ادمین (توکن دارند)
PLATFORMS = [p for p in ALL_PLATFORMS if p.target]      # برای پست‌گذاری (مقصد دارند)


# =====================================================================
#                              نسخه ۳
# =====================================================================
from collections import Counter
from html import escape as _esc

def safe(x):
    """متن خطا بدون توکن/کلید (پیام خطای requests شامل آدرس با توکن است)."""
    return _Redact._scrub(str(x if x is not None else ""))

def esc(x):
    return _esc(safe(x), quote=False)

def utcnow():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()

# ------------------------- تاریخ شمسی و اعداد فارسی -------------------------
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
def fa(x):
    return str(x).translate(_FA)
def en(x):
    return str(x).translate(_EN)

J_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]   # بر اساس weekday() پایتون

def g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd

def j2g(jy, jm, jd):
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    days += ((jm - 1) * 31) if jm < 7 else (((jm - 7) * 30) + 186)
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    feb = 29 if ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0) else 28
    sal = [0, 31, feb, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > sal[gm]:
        gd -= sal[gm]
        gm += 1
    return gy, gm, gd

def to_local(v):
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(TZ)

def jdate(v, weekday=False):
    d = to_local(v)
    jy, jm, jd = g2j(d.year, d.month, d.day)
    s = f"{fa(jd)} {J_MONTHS[jm - 1]}"
    return f"{WEEKDAYS[d.weekday()]} {s}" if weekday else s

def fmt_dt(v):
    if not v:
        return "—"
    try:
        d = to_local(v)
        jy, jm, jd = g2j(d.year, d.month, d.day)
        return fa(f"{jy}/{jm:02d}/{jd:02d} {d:%H:%M}")
    except Exception:
        return str(v)[:16]

def parse_when(text, now=None):
    """زمان دلخواه را می‌خواند. نمونه‌ها: 18:30 | 1405/09/10 18:30 | 09/10 18:30 | 2026-12-01 18:30 | +45 (دقیقه) | +2h"""
    now = now or datetime.now(TZ)
    s = re.sub(r"\s+", " ", en(text).replace("ساعت", " ").replace("،", " ").replace("ـ", "")).strip()
    m = re.fullmatch(r"\+(\d{1,4})\s*(m|h)?", s.lower())
    if m:
        n = int(m.group(1))
        return now + (timedelta(hours=n) if m.group(2) == "h" else timedelta(minutes=n))
    mt = re.search(r"(\d{1,2}):(\d{2})\s*$", s)
    hh, mm = (int(mt.group(1)), int(mt.group(2))) if mt else (10, 0)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError("ساعت نامعتبر است (مثال: 18:30)")
    date_part = s[:mt.start()].strip() if mt else s
    if not date_part:
        d = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return d if d > now else d + timedelta(days=1)
    m = re.fullmatch(r"(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{1,2})", date_part)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a >= 1700:                                  # میلادی
            gy, gm, gd = a, b, c
        else:                                          # شمسی
            jy = a + 1400 if a < 100 else a
            gy, gm, gd = j2g(jy, b, c)
            if g2j(gy, gm, gd) != (jy, b, c):
                raise ValueError("این تاریخ شمسی وجود ندارد")
    else:
        m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})", date_part)
        if not m:
            raise ValueError("قالب زمان را متوجه نشدم. مثال: 1405/09/10 18:30")
        jy = g2j(now.year, now.month, now.day)[0]
        jm_, jd_ = int(m.group(1)), int(m.group(2))
        gy, gm, gd = j2g(jy, jm_, jd_)
        if g2j(gy, gm, gd) != (jy, jm_, jd_):
            raise ValueError("این تاریخ شمسی وجود ندارد")
    try:
        return datetime(gy, gm, gd, hh, mm, tzinfo=TZ)
    except ValueError:
        raise ValueError("این تاریخ وجود ندارد")

# ------------------------- تنظیمات ذخیره‌شده (جدول kv) -------------------------
_cfg = {"t": 0.0, "d": {}}
def cfg_all():
    if time.time() - _cfg["t"] < 20:
        return _cfg["d"]
    try:
        r = sb("GET", "kv", {"select": "k,v"})
        if r.ok:
            _cfg["d"] = {x["k"]: x["v"] for x in r.json()}
    except Exception as e:
        log.warning("cfg read: %s", e)
    _cfg["t"] = time.time()
    return _cfg["d"]

def cfg(k, default=None):
    v = cfg_all().get(k)
    return default if v is None else v

def cfg_set(k, v):
    r = sb("POST", "kv", {"on_conflict": "k"}, [{"k": k, "v": str(v)}], "resolution=merge-duplicates")
    if not r.ok:
        raise RuntimeError("ذخیره تنظیمات ناموفق: " + r.text[:150])
    _cfg["d"][k] = str(v)

def cfg_del(k):
    sb("DELETE", "kv", {"k": f"eq.{k}"})
    _cfg["d"].pop(k, None)

def plat_enabled(name):
    return cfg(f"cfg_en_{name}", "1") == "1"

def active_platforms():
    return [p for p in PLATFORMS if plat_enabled(p.name)]

def norm_time(t):
    hh, mm = map(int, t.split(":"))
    return f"{hh:02d}:{mm:02d}"

def post_times():
    raw = cfg("cfg_post_times")
    if raw:
        ts = sorted({norm_time(t) for t in raw.split(",") if _valid_time(t.strip())})
        if ts:
            return ts
    return sorted({norm_time(t) for t in POST_TIMES})

def queue_gap():
    try:
        return max(0, int(cfg("cfg_queue_gap", "10")))
    except Exception:
        return 10

def gen_times(n, start="10:00", end="21:00"):
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    if n <= 1:
        return [norm_time(start)]
    step = (e - s) / (n - 1)
    out = []
    for i in range(n):
        m = int(round((s + step * i) / 5.0)) * 5
        out.append(f"{m // 60:02d}:{m % 60:02d}")
    return sorted(set(out))

def skip_past_slots(times):
    """ساعت‌های امروز که گذشته‌اند «انجام‌شده» ثبت می‌شوند تا با تغییر تنظیمات، پست‌های گذشته یک‌جا ارسال نشوند."""
    now = datetime.now(TZ)
    for t in times:
        hh, mm = map(int, t.split(":"))
        if now.replace(hour=hh, minute=mm, second=0, microsecond=0) <= now:
            sb("POST", "post_slots", None, [{"slot": f"{now:%Y-%m-%d}_{t}"}], "return=minimal")

# ------------------------- بررسی دیتابیس، گزارش عملیات، اعلان به ادمین‌ها -------------------------
SCHEMA_OK = True
SCHEMA_ERR = ""

def schema_check():
    global SCHEMA_OK, SCHEMA_ERR
    probes = [("post_queue", "id,scheduled_at,image_override,started_at,attempts,last_error,caption_override"),
              ("post_history", "id,batch,source,error"), ("admin_log", "id,admin_id,action,detail")]
    bad = []
    for table, cols in probes:
        try:
            r = sb("GET", table, {"select": cols, "limit": 1})
            if not r.ok:
                bad.append(table)
        except Exception:
            bad.append(table)
    SCHEMA_OK = not bad
    SCHEMA_ERR = ", ".join(bad)
    if bad:
        log.error("دیتابیس به‌روز نیست؛ بخش «نسخه ۳» فایل schema.sql را اجرا کنید. جدول‌های مشکل‌دار: %s", SCHEMA_ERR)
    return SCHEMA_OK

def alog(uid, action, detail=""):
    try:
        sb("POST", "admin_log", None, [{"admin_id": str(uid), "action": action, "detail": str(detail)[:300]}], "return=minimal")
    except Exception as e:
        log.warning("admin_log: %s", e)

def tg_platform():
    return next((p for p in ALL_PLATFORMS if isinstance(p, Telegram)), None)

def notify_admins(text, rows=None):
    tgp = tg_platform()
    if not tgp:
        return
    for uid in tgp.admins:
        kw = {"chat_id": uid, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if rows:
            kw["reply_markup"] = {"inline_keyboard": rows}
        try:
            requests.post(f"{tgp.api}/sendMessage", json=kw, timeout=20)
        except Exception as e:
            log.warning("notify: %s", e)

# ------------------------- تصویر -------------------------
def _to_jpeg(content):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(content))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return content

def load_image(src):
    """آدرس http یا عکس ارسال‌شده در تلگرام (tg:<file_id>) را به JPEG تبدیل می‌کند."""
    if not src:
        return None
    if src.startswith("tg:"):
        tgp = tg_platform()
        j = requests.get(f"{tgp.api}/getFile", params={"file_id": src[3:]}, timeout=30).json()
        path = j["result"]["file_path"]
        r = requests.get(f"https://api.telegram.org/file/bot{tgp.token}/{path}", timeout=60)
        r.raise_for_status()
        return _to_jpeg(r.content)
    return prepare_image(src)

# ------------------------- متن‌های هوش مصنوعی (یک متن یا چند متن) -------------------------
def ai_variants(p, n=3):
    """n متن متفاوت با لحن‌های مختلف؛ اگر Gemini کمتر داد، تک‌به‌تک تکمیل می‌شود."""
    styles = random.sample(STYLE_HINTS, min(n, len(STYLE_HINTS)))
    rules = "\n".join(f"{i + 1}) لحن: {s}" for i, s in enumerate(styles))
    prompt = f"""تو کپی‌رایتر کانال فروشگاه «شهر سفال» (سفال، سرامیک، گلدان، دکوری) هستی.
برای محصول زیر {len(styles)} متن کاملاً متفاوت و کوتاه فارسی برای پست کانال بنویس.

قوانین هر متن:
- ۲ تا ۳ خط کوتاه، حداکثر ۲۵۰ کاراکتر.
- فقط فارسی؛ هیچ کلمه‌ی انگلیسی ننویس.
- فقط از اطلاعات زیر استفاده کن؛ ابعاد، جنس، ضمانت یا ویژگی‌ای که از نام محصول معلوم نیست را ننویس.
- قیمت، شماره تماس، لینک و هشتگ ننویس. از ستاره، کروشه و بک‌تیک استفاده نکن. حداکثر ۲ ایموجی.
- با «سلام» یا «به نظر من» شروع نکن.
- استدلال و توضیح ننویس. هر متن نهایی را جداگانه بین <<< و >>> بگذار.

لحن‌ها:
{rules}

نام محصول: {p['name']}
نام انگلیسی (از آدرس): {(p.get('slug') or '').replace('-', ' ')}"""
    out = []
    raw = gemini_text(prompt) or ""
    for chunk in re.findall(r"<<<.*?>>>", raw, re.S):
        t = clean_ai_text(chunk)
        if t and t not in out:
            out.append(t)
    tries = 0
    while len(out) < n and tries < 3:
        tries += 1
        t = clean_ai_text(gemini_text(prompt.replace(f"{len(styles)} متن", "یک متن", 1)))
        if t and t not in out:
            out.append(t)
    return out[:n]

def ai_best_body(p):
    """۳ متن می‌نویسد، یک بار از Gemini می‌خواهد بهترین را انتخاب کند و همان را برمی‌گرداند."""
    vs = ai_variants(p, 3)
    if len(vs) < 2:
        return vs[0] if vs else ai_body(p)
    listing = "\n\n".join(f"{i + 1}) {t}" for i, t in enumerate(vs))
    prompt = f"""این {len(vs)} متن برای پست کانال فروشگاه سفال و سرامیک نوشته شده است. محصول: {p['name']}
بهترین متن را انتخاب کن: فارسی روان و طبیعی، جذاب، کوتاه، و بدون ادعای ساختگی (ابعاد، جنس، ضمانت).
فقط شماره‌ی بهترین متن را بین <<< و >>> بنویس، مثل: <<<2>>>

{listing}"""
    try:
        m = re.search(r"<<<\s*([1-9])\s*>>>", en(gemini_text(prompt) or ""))
        idx = int(m.group(1)) - 1 if m else 0
        return vs[idx] if idx < len(vs) else vs[0]
    except Exception as e:
        log.warning("ai_best_body: %s", e)
        return vs[0]

def smart_body(p):
    """متن پست: اگر «انتخاب خودکار بهترین متن» روشن باشد ۳ متن + انتخاب، وگرنه یک متن مثل قبل."""
    if cfg("cfg_ai_best", "0") == "1":
        return ai_best_body(p)
    return ai_body(p)

# ------------------------- انتشار -------------------------
_last_pub = [0.0]

def mark_published():
    _last_pub[0] = time.time()
    try:
        cfg_set("cfg_last_publish", int(_last_pub[0]))
    except Exception:
        pass

def last_publish_ts():
    if not _last_pub[0]:
        try:
            _last_pub[0] = float(cfg_all().get("cfg_last_publish") or 0)
        except Exception:
            pass
    return _last_pub[0]

def hist(batch, queue_id, p, platform, ok, source, err=None):
    row = {"queue_id": queue_id, "product_id": p["product_id"], "product_name": p.get("name"),
           "platform": platform, "success": bool(ok), "batch": batch, "source": source, "error": (err or "")[:200] or None}
    try:
        r = sb("POST", "post_history", None, [row], "return=minimal")
        if not r.ok:   # دیتابیس هنوز به‌روز نشده: حداقل ستون‌های قدیمی
            for k in ("batch", "source", "error"):
                row.pop(k, None)
            sb("POST", "post_history", None, [row], "return=minimal")
    except Exception as e:
        log.warning("history: %s", e)

def publish(p, body, img, plats, done=None, on_result=None, batch="", source="", queue_id=None):
    """به هر مقصد (که قبلاً موفق نشده) می‌فرستد. نتیجه: {پلتفرم: True/False}"""
    results = dict(done or {})
    for plat in plats:
        if results.get(plat.name):
            continue                                   # قبلاً موفق بوده؛ تکراری نفرست
        err = None
        try:
            ok = bool(plat.send_post(p, build_caption(p, body, plat), img))
        except Exception as e:
            ok, err = False, safe(e)
            log.warning("%s: %s", plat.name, e)
        results[plat.name] = ok
        hist(batch, queue_id, p, plat.name, ok, source, err)
        if on_result:
            on_result(plat.name, ok, results)
    return results

# ------------------------- ارسال یک اسلات چرخه‌ای (همان رفتار قبلی) -------------------------
def _do_post(slot_key):
    plats = active_platforms()
    if not plats:
        return "هیچ پلتفرمی فعال نیست (توکن و مقصد را تنظیم کنید)"
    r = sb("POST", "post_slots", None, [{"slot": slot_key}], "return=minimal")
    if r.status_code == 409 or (not r.ok and "duplicate" in r.text.lower()):
        return "قبلاً انجام شده"
    if not r.ok:
        log.error("قفل اسلات ناموفق: %s", r.text[:200])
        return "خطای دیتابیس"
    p = pick_product()
    if not p:
        sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})
        return "محصول آماده‌ای نیست"
    body = smart_body(p)
    try:
        img = prepare_image(p["image_url"])
    except Exception as e:
        log.warning("دانلود عکس ناموفق: %s", e)
        img = None
    source = "manual" if slot_key.startswith("manual_") else "slot"
    results = publish(p, body, img, plats, batch=f"s:{slot_key}", source=source)
    summary = " ".join(f"{k}:{'✅' if v else '❌'}" for k, v in results.items())
    if any(results.values()):
        try:
            sb("PATCH", T, {"product_id": f"eq.{p['product_id']}"},
               {"posted_count": int(p.get("posted_count") or 0) + 1, "last_posted_at": iso(utcnow())})
            sb("PATCH", "post_slots", {"slot": f"eq.{slot_key}"}, {"product_id": p["product_id"]})
            mark_published()
        except Exception as e:
            log.error("ثبت نتیجه در دیتابیس ناموفق (پست ارسال شده): %s", e)
        return f"ارسال شد: {p['name']}\n{summary}"
    sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})
    return f"ارسال ناموفق ({summary})"

_post_lock = threading.Lock()

def do_post(slot_key):
    with _post_lock:
        try:
            return _do_post(slot_key)
        except Exception as e:
            log.exception("do_post: %s", e)
            try:
                sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})
            except Exception:
                pass
            return "ارسال ناموفق (خطای داخلی)"

# ------------------------- صف انتشار -------------------------
def get_item(qid):
    r = sb("GET", "post_queue", {"id": f"eq.{qid}", "select": "*", "limit": 1})
    return r.json()[0] if r.ok and r.json() else None

def get_product(pid):
    r = sb("GET", T, {"product_id": f"eq.{pid}", "select": "*", "limit": 1})
    return r.json()[0] if r.ok and r.json() else None

def product_names(pids):
    pids = sorted({x for x in pids if x is not None})
    if not pids:
        return {}
    r = sb("GET", T, {"select": "product_id,name", "product_id": "in.(" + ",".join(map(str, pids)) + ")"})
    return {x["product_id"]: x.get("name") or f"#{x['product_id']}" for x in (r.json() if r.ok else [])}

def enqueue(pid, copy_from=None):
    """محصول را به صف اضافه می‌کند. اگر همین محصول قبلاً در صف منتظر است، همان را برمی‌گرداند. (qid, تازه_بود)"""
    ex = sb("GET", "post_queue", {"product_id": f"eq.{pid}", "status": "in.(pending,processing)", "select": "id", "limit": 1})
    if ex.ok and ex.json():
        return ex.json()[0]["id"], False
    p = get_product(pid)
    if not p:
        raise RuntimeError("محصول پیدا نشد")
    if not p.get("image_url") or not p.get("permalink"):
        raise RuntimeError("این محصول تصویر یا لینک ندارد؛ اول از «مدیریت محصول» تصویر/لینک را کامل کنید.")
    row = {"product_id": pid}
    if copy_from:
        for k in ("caption_override", "image_override"):
            if copy_from.get(k):
                row[k] = copy_from[k]
    q = sb("POST", "post_queue", None, [row], "return=representation")
    if not q.ok or not q.json():
        raise RuntimeError("افزودن به صف ناموفق: " + q.text[:160])
    return q.json()[0]["id"], True

def recover_stale():
    """موردی که بیش از ۱۰ دقیقه «در حال انتشار» مانده (مثلاً ریستارت سرور) به صف برمی‌گردد؛ مقصدهای موفق دوباره ارسال نمی‌شوند."""
    cut = iso(utcnow() - timedelta(minutes=10))
    r = sb("PATCH", "post_queue", {"status": "eq.processing", "started_at": f"lt.{cut}"},
           {"status": "pending", "last_error": "پس از توقف سرور به صف برگشت"}, "return=representation")
    if r.ok and r.json():
        log.warning("بازیابی موردهای گیرکرده در صف: %s", [x["id"] for x in r.json()])

def publish_queue_item(qid, notify=True):
    """یک مورد pending را قفل می‌کند و منتشر می‌کند. برمی‌گرداند (وضعیت، نتیجه، پیام)."""
    item = get_item(qid)
    if not item or item["status"] != "pending":
        return None, {}, "این مورد دیگر در انتظار انتشار نیست"
    claim = sb("PATCH", "post_queue", {"id": f"eq.{qid}", "status": "eq.pending"},
               {"status": "processing", "started_at": iso(utcnow()), "attempts": int(item.get("attempts") or 0) + 1,
                "last_error": None}, "return=representation")
    if not claim.ok or not claim.json():
        return None, {}, "این مورد هم‌اکنون توسط فرایند دیگری در حال انتشار است"
    def finish(state, results, err=None):
        sb("PATCH", "post_queue", {"id": f"eq.{qid}"},
           {"status": state, "platform_status": results, "last_error": err, "completed_at": iso(utcnow())})
    try:
        p = get_product(item["product_id"])
        if not p:
            finish("failed", {}, "محصول از دیتابیس حذف شده است")
            return "failed", {}, "محصول حذف شده است"
        plats = active_platforms()
        if not plats:
            finish("failed", item.get("platform_status") or {}, "هیچ پلتفرم فعالی وجود ندارد")
            return "failed", {}, "هیچ پلتفرم فعالی وجود ندارد"
        prior = item.get("platform_status") or {}
        body = item.get("caption_override") or smart_body(p)
        if not item.get("caption_override"):       # متن ثبت شود تا تلاش مجدد همان متن را بفرستد
            sb("PATCH", "post_queue", {"id": f"eq.{qid}"}, {"caption_override": body})
        img = None
        for src in (item.get("image_override"), p.get("image_url")):
            if not src:
                continue
            try:
                img = load_image(src)
                break
            except Exception as e:
                log.warning("queue image %s: %s", qid, e)
        def save(name, ok, results):               # بعد از هر مقصد ذخیره می‌شود (ضد ارسال تکراری)
            sb("PATCH", "post_queue", {"id": f"eq.{qid}"}, {"platform_status": results})
        results = publish(p, body, img, plats, done=prior, on_result=save, batch=f"q:{qid}", source="queue", queue_id=qid)
        first_success = not any(prior.values())
        if any(results.values()) and first_success:
            try:
                sb("PATCH", T, {"product_id": f"eq.{p['product_id']}"},
                   {"posted_count": int(p.get("posted_count") or 0) + 1, "last_posted_at": iso(utcnow())})
            except Exception as e:
                log.warning("queue update product: %s", e)
        if any(results.values()):
            mark_published()
        failed = [x.name for x in plats if not results.get(x.name)]
        state = "failed" if failed else "done"
        finish(state, results, ("ناموفق در: " + "، ".join(failed)) if failed else None)
        summary = " ".join(f"{k}:{'✅' if v else '❌'}" for k, v in results.items())
        log.info("queue item %s -> %s %s", qid, state, results)
        if notify and (failed or item.get("scheduled_at")):
            head = "⚠️ انتشار ناقص/ناموفق" if failed else "✅ انتشار زمان‌بندی‌شده انجام شد"
            rows = [[{"text": "🔁 تلاش مجدد", "callback_data": f"qr:{qid}"}, {"text": "📄 جزئیات", "callback_data": f"qd:{qid}"}]] if failed else None
            notify_admins(f"{head}\n<b>{esc(p.get('name'))}</b>\n{summary}", rows)
        return state, results, f"{p.get('name')}\n{summary}"
    except Exception as e:
        log.exception("queue worker %s: %s", qid, e)
        try:
            finish("failed", item.get("platform_status") or {}, safe(e)[:200])
        except Exception:
            pass
        return "failed", {}, f"خطای داخلی: {safe(e)}"

_queue_lock = threading.Lock()

def process_queue_once():
    if not PLATFORMS or not _queue_lock.acquire(blocking=False):
        return
    try:
        recover_stale()
        r = sb("GET", "post_queue", {"select": "id,scheduled_at", "status": "eq.pending",
                                      "or": f"(scheduled_at.is.null,scheduled_at.lte.{iso(utcnow())})",
                                      "order": "scheduled_at.asc.nullsfirst,created_at.asc", "limit": 10})
        if not r.ok or not r.json():
            return
        gap_ok = time.time() - last_publish_ts() >= queue_gap() * 60
        for it in r.json():
            if it.get("scheduled_at") or gap_ok:       # فاصله‌ی بین پست‌ها فقط برای موردهای بدون زمان اختصاصی
                publish_queue_item(it["id"])
                break
    except Exception as e:
        log.exception("queue loop: %s", e)
    finally:
        _queue_lock.release()

# ------------------------- زمان‌بند (ترد پس‌زمینه) -------------------------
_retry_after = {}
_notified = set()
def scheduler_loop():
    time.sleep(5)
    while True:
        try:
            now = datetime.now(TZ)
            process_queue_once()
            for t in post_times():
                hh, mm = map(int, t.split(":"))
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due <= now <= due + timedelta(minutes=CATCHUP_MINUTES):
                    slot = f"{now:%Y-%m-%d}_{t}"
                    if _retry_after.get(slot, 0) > time.time():
                        continue
                    res = do_post(slot)
                    if res.startswith(("ارسال ناموفق", "خطای دیتابیس", "محصول آماده‌ای نیست", "هیچ پلتفرمی")):
                        _retry_after[slot] = time.time() + 900
                        if res.startswith("ارسال ناموفق") and slot not in _notified:
                            _notified.add(slot)
                            notify_admins(f"⚠️ پست ساعت {fa(t)} ارسال نشد:\n{esc(res)}")
                    if res != "قبلاً انجام شده":
                        log.info("اسلات %s: %s", t, res)
        except Exception as e:
            log.exception("scheduler: %s", e)
        time.sleep(60)

# =====================================================================
#                 پنل مدیریت تلگرام (فقط ادمین‌ها)
# =====================================================================
PAGE = 8
STATUS_FA = {"pending": "⏳ در انتظار", "processing": "⚙️ در حال انتشار", "done": "✅ منتشر شد",
             "failed": "❌ ناموفق", "cancelled": "🚫 لغو شد"}
_state = {}       # uid -> {"mode":..., "ts":..., ...}   ورودیِ منتظر از ادمین
_view = {}        # uid -> {"q": عبارت جست‌وجو}
_variants = {}    # qid -> [متن۱، متن۲، متن۳]

def B(text, data):
    return {"text": text, "callback_data": data}

def tgcall(plat, method, **kw):
    try:
        return requests.post(f"{plat.api}/{method}", json=kw, timeout=40).json()
    except Exception as e:
        log.warning("tg %s: %s", method, e)
        return {}

def show(plat, chat, mid, text, rows):
    """پیام را ویرایش می‌کند (اگر mid باشد) وگرنه پیام تازه می‌فرستد."""
    if len(text) > 3900:
        text = text[:3900].rsplit("\n", 1)[0] + "\n…"
    kw = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
          "reply_markup": {"inline_keyboard": rows or []}}
    if mid:
        j = tgcall(plat, "editMessageText", message_id=mid, **kw)
        if j.get("ok") or "not modified" in str(j.get("description", "")):
            return
    j = tgcall(plat, "sendMessage", **kw)
    if not j.get("ok"):
        log.warning("tg show: %s", j)

def count_rows(table, params):
    r = sb("GET", table, dict(params, select="*", limit=1), prefer="count=exact")
    try:
        return int(r.headers.get("Content-Range", "*/0").split("/")[-1])
    except Exception:
        return 0

def _total(r):
    try:
        return int(r.headers.get("Content-Range", "*/0").split("/")[-1])
    except Exception:
        return 0

def nav(prefix, page, total, per=PAGE):
    row = []
    if page > 0:
        row.append(B("◀️ قبلی", f"{prefix}:{page - 1}"))
    if (page + 1) * per < total:
        row.append(B("بعدی ▶️", f"{prefix}:{page + 1}"))
    return row

HOME = [B("🏠 منوی اصلی", "m")]

# ------------------------- منوی اصلی -------------------------
def screen_menu(plat, chat, mid, note=""):
    warn = f"\n\n⚠️ دیتابیس به‌روز نیست ({esc(SCHEMA_ERR)}). بخش «نسخه ۳» فایل schema.sql را در Supabase اجرا کنید." if not SCHEMA_OK else ""
    rows = [[B("➕ افزودن محصول", "add"), B("🛍 محصولات", "pl:a:0")],
            [B("🗂 صف انتشار", "ql:p:0"), B("🗓 تقویم انتشار", "cal:0")],
            [B("📤 ارسال فوری", "pn"), B("📊 وضعیت چرخه", "st")],
            [B("📈 آمار و گزارش", "stat:7"), B("📜 تاریخچه", "hist:a:0")],
            [B("⚙️ تنظیمات", "set"), B("📋 گزارش عملیات", "log")]]
    show(plat, chat, mid, (note + "\n\n" if note else "") + "🏺 <b>پنل مدیریت شهر سفال</b>\nچه کاری انجام بدم؟" + warn, rows)

# ------------------------- وضعیت -------------------------
def screen_status(plat, chat, mid):
    text = esc(status_text())
    q = sb("GET", "post_queue", {"select": "status", "status": "in.(pending,processing,failed)", "limit": 1000})
    c = Counter(x["status"] for x in (q.json() if q.ok else []))
    text += f"\n\n🗂 صف: {fa(c['pending'])} در انتظار · {fa(c['processing'])} در حال انتشار · {fa(c['failed'])} ناموفق"
    nx = sb("GET", "post_queue", {"select": "scheduled_at", "status": "eq.pending", "scheduled_at": f"gte.{iso(utcnow())}",
                                   "order": "scheduled_at.asc", "limit": 1})
    if nx.ok and nx.json():
        text += f"\n⏱ نزدیک‌ترین انتشار زمان‌بندی‌شده: {fmt_dt(nx.json()[0]['scheduled_at'])}"
    show(plat, chat, mid, text, [[B("🔄 تازه‌سازی", "st"), B("🗂 صف", "ql:p:0")], HOME])

def status_text():
    items, r = fetch_all(T, {"select": "posted_count", "active": "eq.true", "order": "product_id.asc"})
    if not items:
        return "محصول فعالی در دیتابیس نیست." if items is not None else f"خطای دیتابیس: {r.text[:200]}"
    mn = min(i["posted_count"] for i in items)
    left = sum(1 for i in items if i["posted_count"] == mn)
    times = post_times()
    return (f"محصولات فعال: {fa(len(items))}\nدور فعلی چرخه: {fa(mn + 1)}\n"
            f"باقی‌مانده تا پایان این دور: {fa(left)}\nپست خودکار روزانه: {fa(len(times))} عدد\nساعت‌ها: {fa(', '.join(times))}\n"
            f"مقصدهای فعال: {', '.join(x.name for x in active_platforms()) or '-'}")

# ------------------------- محصولات -------------------------
def clean_q(s):
    return re.sub(r"[,()*%\\]", " ", s).strip()[:40]

def screen_products(plat, chat, mid, uid, f="a", page=0):
    params = {"select": "product_id,name,active", "order": "name.asc", "limit": PAGE, "offset": page * PAGE}
    q = (_view.get(uid) or {}).get("q")
    if f == "o":
        params["active"] = "eq.true"
    elif f == "x":
        params["active"] = "eq.false"
    elif f == "s" and q:
        params["name"] = f"ilike.*{clean_q(q)}*"
    r = sb("GET", T, params, prefer="count=exact")
    if not r.ok:
        show(plat, chat, mid, "خطای دریافت محصولات: " + esc(r.text[:150]), [HOME])
        return
    items, total = r.json() or [], _total(r)
    mark = lambda k, t: ("• " + t) if f == k else t
    rows = [[B(("✅ " if x.get("active") else "⏸ ") + str(x.get("name") or "محصول")[:45], f"pd:{x['product_id']}")] for x in items]
    n = nav(f"pl:{f}", page, total)
    if n:
        rows.append(n)
    rows.append([B(mark("a", "همه"), "pl:a:0"), B(mark("o", "فعال"), "pl:o:0"), B(mark("x", "غیرفعال"), "pl:x:0"), B("🔍 جستجو", "psr")])
    rows.append([B("➕ افزودن با لینک", "add"), B("🏠", "m")])
    title = f"🛍 <b>محصولات</b> — {fa(total)} مورد"
    if f == "s" and q:
        title += f"\n🔍 جستجو: «{esc(q)}»"
    if not items:
        title += "\nموردی پیدا نشد."
    elif total > PAGE:
        title += f" · صفحه {fa(page + 1)} از {fa((total + PAGE - 1) // PAGE)}"
    show(plat, chat, mid, title, rows)

def screen_product(plat, chat, mid, pid, note=""):
    p = get_product(pid)
    if not p:
        show(plat, chat, mid, "محصول پیدا نشد.", [[B("🛍 محصولات", "pl:a:0")], HOME])
        return
    text = (f"{note + chr(10) + chr(10) if note else ''}🛍 <b>{esc(p.get('name'))}</b>\n"
            f"شناسه: {fa(p['product_id'])}\nوضعیت: {'✅ فعال (در چرخه)' if p.get('active') else '⏸ غیرفعال'}\n"
            f"تعداد انتشار: {fa(p.get('posted_count') or 0)} · آخرین انتشار: {fmt_dt(p.get('last_posted_at'))}\n"
            f"🖼 تصویر: {'دارد' if p.get('image_url') else '⚠️ ندارد (پست نمی‌شود)'}\n🔗 {esc(p.get('permalink') or '—')}")
    rows = [[B("📤 افزودن به صف", f"pq:{pid}"), B("⏸ غیرفعال" if p.get("active") else "▶️ فعال‌سازی", f"pt:{pid}")],
            [B("✏️ نام", f"pe:{pid}:n"), B("🖼 تصویر", f"pe:{pid}:i"), B("🔗 لینک", f"pe:{pid}:l")],
            [B("🗑 حذف محصول", f"pdel:{pid}")], [B("⬅️ محصولات", "pl:a:0"), B("🏠", "m")]]
    show(plat, chat, mid, text, rows)

def delete_product(pid, uid):
    p = get_product(pid)
    sb("PATCH", "post_queue", {"product_id": f"eq.{pid}", "status": "in.(pending,failed)"}, {"status": "cancelled"})
    r = sb("DELETE", T, {"product_id": f"eq.{pid}"})
    if not r.ok:
        raise RuntimeError("حذف ناموفق: " + r.text[:150])
    alog(uid, "حذف محصول", f"{pid} {p.get('name') if p else ''}")

# ------------------------- صف انتشار -------------------------
def item_status(it):
    ps = it.get("platform_status") or {}
    if it["status"] == "failed" and any(ps.values()):
        return "⚠️ ناقص (بخشی ارسال شد)"
    return STATUS_FA.get(it["status"], it["status"])

QF = {"p": ("در انتظار", "in.(pending,processing)"), "f": ("ناموفق", "eq.failed"),
      "d": ("انجام‌شده", "eq.done"), "c": ("لغوشده", "eq.cancelled")}

def screen_queue(plat, chat, mid, f="p", page=0):
    desc = f in ("d", "c")
    r = sb("GET", "post_queue", {"select": "id,product_id,status,scheduled_at,platform_status", "status": QF[f][1],
                                  "order": "created_at.desc" if desc else "created_at.asc", "limit": PAGE, "offset": page * PAGE},
           prefer="count=exact")
    if not r.ok:
        show(plat, chat, mid, "خطای صف: " + esc(r.text[:150]), [HOME])
        return
    items, total = r.json() or [], _total(r)
    names = product_names([x["product_id"] for x in items])
    rows = []
    for x in items:
        when = f" 🕒{fmt_dt(x['scheduled_at'])[5:]}" if x.get("scheduled_at") and f == "p" else ""
        rows.append([B(f"{STATUS_FA.get(x['status'], '')[:1]} {names.get(x['product_id'], x['product_id'])}"[:48] + when, f"qd:{x['id']}")])
    n = nav(f"ql:{f}", page, total)
    if n:
        rows.append(n)
    rows.append([B(("• " if f == k else "") + v[0], f"ql:{k}:0") for k, v in QF.items()])
    rows.append([B("🛍 افزودن از محصولات", "pl:a:0"), B("🗓 تقویم", "cal:0")])
    rows.append(HOME)
    text = f"🗂 <b>صف انتشار — {QF[f][0]}</b> ({fa(total)} مورد)"
    if not items:
        text += "\nموردی نیست."
    show(plat, chat, mid, text, rows)

def screen_qitem(plat, chat, mid, qid, note=""):
    it = get_item(qid)
    if not it:
        show(plat, chat, mid, "مورد صف پیدا نشد.", [[B("🗂 صف", "ql:p:0")], HOME])
        return
    p = get_product(it["product_id"]) or {}
    ps = it.get("platform_status") or {}
    lines = [note] if note else []
    lines += [f"🗂 <b>مورد صف #{fa(qid)}</b>", f"🛍 {esc(p.get('name') or 'محصول حذف شده')}", f"وضعیت: {item_status(it)}",
              f"زمان انتشار: {fmt_dt(it['scheduled_at']) if it.get('scheduled_at') else 'به‌ترتیب صف (FIFO)'}"]
    if it.get("caption_override"):
        lines.append(f"✏️ متن: {esc(it['caption_override'][:180])}")
    else:
        lines.append("متن: خودکار با هوش مصنوعی (هنگام انتشار)")
    lines.append(f"🖼 تصویر: {'سفارشی' if it.get('image_override') else 'تصویر محصول'}")
    if ps:
        lines.append("نتیجه: " + " ".join(f"{k}:{'✅' if v else '❌'}" for k, v in ps.items()))
    if it.get("last_error"):
        lines.append(f"خطا: {esc(it['last_error'])}")
    if it.get("attempts"):
        lines.append(f"تعداد تلاش: {fa(it['attempts'])}")
    st = it["status"]
    rows = []
    if st in ("pending", "failed"):
        rows.append([B("✏️ ویرایش متن", f"qe:{qid}"), B("🤖 ۳ متن AI", f"qa:{qid}")])
        rows.append([B("🖼 تغییر تصویر", f"qi:{qid}"), B("👁 پیش‌نمایش", f"qp:{qid}")])
    if st == "pending":
        rows.append([B("🕒 زمان‌بندی", f"qs:{qid}"), B("🚀 انتشار همین حالا", f"qn:{qid}")])
    if st == "failed":
        rows.append([B("🔁 تلاش مجدد (فقط مقصدهای ناموفق)", f"qr:{qid}")])
    if st in ("pending", "failed"):
        rows.append([B("🚫 لغو", f"qx:{qid}")])
    if st in ("done", "cancelled"):
        rows.append([B("📤 انتشار دوباره (مورد جدید)", f"qc:{qid}")])
    rows.append([B("⬅️ صف", "ql:p:0"), B("🏠", "m")])
    show(plat, chat, mid, "\n".join(lines), rows)

def screen_schedule(plat, chat, mid, qid):
    rows = [[B("⏱ ۳۰ دقیقه دیگر", f"qs:{qid}:m30"), B("⏱ ۱ ساعت دیگر", f"qs:{qid}:h1")],
            [B("🌙 امشب ۲۰:۰۰", f"qs:{qid}:t20"), B("🌅 فردا ۱۰:۰۰", f"qs:{qid}:f10")],
            [B("✍️ تاریخ و ساعت دلخواه", f"qs:{qid}:c")],
            [B("❌ حذف زمان‌بندی (به‌ترتیب صف)", f"qs:{qid}:x")], [B("⬅️ بازگشت", f"qd:{qid}")]]
    show(plat, chat, mid, "🕒 <b>زمان انتشار این محصول</b>\nیکی از گزینه‌ها را بزنید یا زمان دلخواه بنویسید.\n"
         "مثال: <code>1405/09/10 18:30</code> یا <code>18:30</code> یا <code>+45</code> (۴۵ دقیقه دیگر)", rows)

def preset_time(code):
    now = datetime.now(TZ)
    if code == "m30":
        return now + timedelta(minutes=30)
    if code == "h1":
        return now + timedelta(hours=1)
    if code == "t20":
        d = now.replace(hour=20, minute=0, second=0, microsecond=0)
        return d if d > now else d + timedelta(days=1)
    if code == "f10":
        return (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    raise ValueError("گزینه نامعتبر")

def set_schedule(qid, dt, uid):
    r = sb("PATCH", "post_queue", {"id": f"eq.{qid}", "status": "in.(pending,failed)"},
           {"scheduled_at": iso(dt) if dt else None, "status": "pending"}, "return=representation")
    if not r.ok or not r.json():
        raise RuntimeError("این مورد قابل زمان‌بندی نیست")
    alog(uid, "زمان‌بندی", f"صف {qid}: {fmt_dt(dt) if dt else 'حذف زمان'}")

def preview(plat, chat, qid):
    it = get_item(qid)
    p = get_product(it["product_id"]) if it else None
    if not p:
        return tgcall(plat, "sendMessage", chat_id=chat, text="مورد یا محصول پیدا نشد.")
    body = it.get("caption_override") or "(متن این پست هنگام انتشار با هوش مصنوعی نوشته می‌شود)"
    cap = build_caption(p, body, plat)
    try:
        img = load_image(it.get("image_override") or p.get("image_url"))
    except Exception as e:
        img = None
        cap += f"\n\n⚠️ دانلود تصویر ناموفق: {e}"
    if img:
        requests.post(f"{plat.api}/sendPhoto", data={"chat_id": chat, "caption": cap},
                      files={"photo": ("p.jpg", img, "image/jpeg")}, timeout=60)
    else:
        tgcall(plat, "sendMessage", chat_id=chat, text=cap)

def publish_bg(plat, chat, qid):
    def run():
        state, results, msg = publish_queue_item(qid, notify=False)
        icon = {"done": "✅ منتشر شد", "failed": "⚠️ ناقص/ناموفق"}.get(state, "ℹ️")
        rows = [[B("📄 جزئیات", f"qd:{qid}")]] + ([[B("🔁 تلاش مجدد", f"qr:{qid}")]] if state == "failed" else [])
        show(plat, chat, None, f"{icon}\n{esc(msg)}", rows)
    threading.Thread(target=run, daemon=True).start()

# ------------------------- تقویم انتشار -------------------------
def screen_calendar(plat, chat, mid, week=0):
    week = max(-4, min(week, 8))
    start = (datetime.now(TZ) + timedelta(days=7 * week)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    r = sb("GET", "post_queue", {"select": "id,product_id,status,scheduled_at",
                                  "and": f"(scheduled_at.gte.{iso(start)},scheduled_at.lt.{iso(end)})",
                                  "status": "neq.cancelled", "order": "scheduled_at.asc", "limit": 100})
    items = r.json() if r.ok else []
    names = product_names([x["product_id"] for x in items])
    by_day = {}
    for x in items:
        by_day.setdefault(to_local(x["scheduled_at"]).date(), []).append(x)
    times, today = post_times(), datetime.now(TZ).date()
    lines = [f"🗓 <b>تقویم انتشار</b>\nپست خودکار چرخه‌ای هر روز: {fa(len(times))} عدد ({fa(', '.join(times))})", ""]
    for i in range(7):
        d = (start + timedelta(days=i)).date()
        mark = " ← امروز" if d == today else ""
        lines.append(f"<b>{jdate(datetime(d.year, d.month, d.day, tzinfo=TZ), weekday=True)}</b>{mark}")
        day_items = by_day.get(d, [])
        if not day_items:
            lines.append("   —")
        for x in day_items:
            em = STATUS_FA.get(x["status"], "")[:1]
            lines.append(f"   {em} {fa(to_local(x['scheduled_at']).strftime('%H:%M'))} — {esc(names.get(x['product_id'], x['product_id']))}")
    rows = [[B("◀️ هفته قبل", f"cal:{week - 1}"), B("هفته بعد ▶️", f"cal:{week + 1}")]]
    if week != 0:
        rows.append([B("📍 این هفته", "cal:0")])
    rows.append([B("🗂 صف", "ql:p:0"), B("🏠", "m")])
    show(plat, chat, mid, "\n".join(lines), rows)

# ------------------------- آمار و نمودار -------------------------
def screen_stats(plat, chat, mid, days=7):
    days = 30 if days >= 30 else 7
    since = iso(utcnow() - timedelta(days=days))
    r = sb("GET", "post_history", {"select": "created_at,platform,success,product_id,product_name,queue_id,batch",
                                    "created_at": f"gte.{since}", "order": "created_at.desc", "limit": 3000})
    rows_h = r.json() if r.ok else []
    posts, per_plat, top = {}, {}, Counter()
    for h in rows_h:
        key = h.get("batch") or (f"q{h['queue_id']}" if h.get("queue_id") else f"{h.get('product_id')}@{str(h.get('created_at'))[:16]}")
        d = to_local(h["created_at"]).date()
        cur = posts.setdefault(key, {"day": d, "ok": False, "name": h.get("product_name")})
        cur["ok"] = cur["ok"] or bool(h.get("success"))
        pp = per_plat.setdefault(h["platform"], [0, 0])
        pp[0] += 1
        pp[1] += 1 if h.get("success") else 0
    for v in posts.values():
        if v["ok"]:
            top[v["name"]] += 1
    daily = Counter(v["day"] for v in posts.values() if v["ok"])
    today = datetime.now(TZ).date()
    mx = max(daily.values()) if daily else 1
    chart = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        jy, jm, jd = g2j(d.year, d.month, d.day)
        n = daily.get(d, 0)
        chart.append(f"{jm:02d}/{jd:02d} {'█' * max(1 if n else 0, round(n / mx * 14))} {n}")
    ok_posts = sum(1 for v in posts.values() if v["ok"])
    bad_posts = len(posts) - ok_posts
    total_p = count_rows(T, {})
    act_p = count_rows(T, {"active": "eq.true"})
    new_p = count_rows(T, {"active": "eq.true", "posted_count": "eq.0"})
    lines = [f"📈 <b>گزارش {fa(days)} روز اخیر</b>",
             f"پست‌های موفق: {fa(ok_posts)} · پست‌های کاملاً ناموفق: {fa(bad_posts)}",
             f"محصولات: {fa(total_p)} (فعال {fa(act_p)}، هنوز منتشرنشده {fa(new_p)})", "", "<b>موفقیت هر مقصد</b>"]
    for name, (tot, ok) in sorted(per_plat.items()):
        lines.append(f"• {esc(name)}: {fa(ok)} از {fa(tot)} ({fa(round(ok * 100 / tot) if tot else 0)}٪)")
    if not per_plat:
        lines.append("• هنوز داده‌ای ثبت نشده")
    if top:
        lines += ["", "<b>پرانتشارترین‌ها</b>"] + [f"• {esc(n)} — {fa(c)} بار" for n, c in top.most_common(5)]
    lines += ["", "<b>نمودار روزانه (ماه/روز شمسی)</b>", "<pre>" + "\n".join(chart) + "</pre>"]
    rows = [[B(("• " if days == 7 else "") + "۷ روز", "stat:7"), B(("• " if days == 30 else "") + "۳۰ روز", "stat:30")],
            [B("📜 تاریخچه", "hist:a:0"), B("🏠", "m")]]
    show(plat, chat, mid, "\n".join(lines), rows)

def screen_history(plat, chat, mid, f="a", page=0):
    params = {"select": "product_name,platform,success,created_at,error", "order": "created_at.desc", "limit": 10, "offset": page * 10}
    if f == "f":
        params["success"] = "eq.false"
    r = sb("GET", "post_history", params, prefer="count=exact")
    if not r.ok:
        show(plat, chat, mid, "تاریخچه آماده نیست: " + esc(r.text[:150]), [HOME])
        return
    items, total = r.json() or [], _total(r)
    lines = [f"📜 <b>تاریخچه انتشار{' — فقط ناموفق' if f == 'f' else ''}</b> ({fa(total)})"]
    for h in items:
        err = f" — {esc(h['error'])}" if h.get("error") else ""
        lines.append(f"{'✅' if h.get('success') else '❌'} {esc(h.get('product_name'))} | {esc(h.get('platform'))} | {fmt_dt(h.get('created_at'))}{err}")
    if not items:
        lines.append("موردی نیست.")
    rows = []
    n = nav(f"hist:{f}", page, total, 10)
    if n:
        rows.append(n)
    rows.append([B(("• " if f == "a" else "") + "همه", "hist:a:0"), B(("• " if f == "f" else "") + "ناموفق‌ها", "hist:f:0"), B("🏠", "m")])
    show(plat, chat, mid, "\n".join(lines), rows)

def screen_log(plat, chat, mid):
    r = sb("GET", "admin_log", {"select": "admin_id,action,detail,created_at", "order": "created_at.desc", "limit": 15})
    if not r.ok:
        show(plat, chat, mid, "گزارش عملیات آماده نیست (schema.sql را اجرا کنید): " + esc(r.text[:120]), [HOME])
        return
    lines = ["📋 <b>آخرین عملیات مدیریتی</b>"]
    for x in r.json() or []:
        lines.append(f"• {fmt_dt(x['created_at'])} — {esc(x['action'])}" + (f": {esc(x['detail'])}" if x.get("detail") else "") + f" (ادمین {esc(x['admin_id'])})")
    if len(lines) == 1:
        lines.append("هنوز موردی ثبت نشده.")
    show(plat, chat, mid, "\n".join(lines), [[B("🔄 تازه‌سازی", "log"), B("🏠", "m")]])

# ------------------------- تنظیمات -------------------------
def screen_settings(plat, chat, mid):
    times = post_times()
    custom = bool(cfg("cfg_post_times"))
    lines = ["⚙️ <b>تنظیمات انتشار</b>", "", "<b>مقصدها</b> (برای هر مقصد می‌توانید انتشار را خاموش/روشن کنید)"]
    rows = []
    for p in PLATFORMS:
        on = plat_enabled(p.name)
        lines.append(f"• {p.name}: {'✅ روشن' if on else '⏸ خاموش'}")
        rows.append([B(f"{'⏸ خاموش‌کردن' if on else '▶️ روشن‌کردن'} {p.name}", f"sp:{p.name}")])
    lines += ["", f"<b>پست خودکار روزانه:</b> {fa(len(times))} عدد — {fa(', '.join(times))}" + ("" if custom else " (پیش‌فرض سرور)"),
              f"<b>فاصله‌ی حداقل بین پست‌های صف:</b> {fa(queue_gap())} دقیقه"]
    best = cfg("cfg_ai_best", "0") == "1"
    lines.append(f"<b>انتخاب خودکار بهترین متن:</b> {'✅ روشن (۳ متن می‌نویسد و بهترین را خودش انتخاب می‌کند)' if best else '⏸ خاموش (یک متن)'}")
    rows.append([B("🤖 انتخاب خودکار بهترین متن: " + ("خاموش‌کردن" if best else "روشن‌کردن"), "sai")])
    rows.append([B("🔢 تعداد و ساعت پست روزانه", "stm"), B("⏲ فاصله‌ی بین پست‌ها", "stg")])
    rows.append(HOME)
    show(plat, chat, mid, "\n".join(lines), rows)

def screen_times(plat, chat, mid, note=""):
    times = post_times()
    rows = [[B(f"{fa(n)} پست", f"stn:{n}") for n in (3, 4, 5)], [B(f"{fa(n)} پست", f"stn:{n}") for n in (6, 8, 10)],
            [B("✍️ ساعت‌های دلخواه", "stt")], [B("↩️ برگشت به پیش‌فرض سرور", "stdef")], [B("⬅️ تنظیمات", "set")]]
    show(plat, chat, mid, f"{note + chr(10) + chr(10) if note else ''}🔢 <b>پست خودکار روزانه</b>\nالان: {fa(len(times))} عدد — {fa(', '.join(times))}\n\n"
         "با دکمه‌ها، پست‌ها به‌طور مساوی بین ۱۰:۰۰ تا ۲۱:۰۰ پخش می‌شوند.\nبرای ساعت دقیق، «ساعت‌های دلخواه» را بزنید.\n"
         "ساعت‌های گذشته‌ی امروز دوباره پست نمی‌شوند.", rows)

def screen_gap(plat, chat, mid):
    rows = [[B(("• " if queue_gap() == n else "") + f"{fa(n)} دقیقه", f"stg:{n}") for n in (0, 5, 10)],
            [B(("• " if queue_gap() == n else "") + f"{fa(n)} دقیقه", f"stg:{n}") for n in (20, 30, 60)], [B("⬅️ تنظیمات", "set")]]
    show(plat, chat, mid, f"⏲ <b>فاصله‌ی بین پست‌های صف</b>\nالان: {fa(queue_gap())} دقیقه\n"
         "بعد از هر انتشار، مورد بعدیِ صف حداقل این‌قدر صبر می‌کند. موردهای دارای زمان اختصاصی دقیقاً سر ساعتشان منتشر می‌شوند.", rows)

def parse_times_input(text):
    s = en(text).strip()
    m = re.fullmatch(r"(\d{1,2})", s)
    if m:
        return gen_times(int(m.group(1)))
    m = re.fullmatch(r"(\d{1,2})\s+(\d{1,2}:\d{2})\s*[-–تا]+\s*(\d{1,2}:\d{2})", s)
    if m:
        return gen_times(int(m.group(1)), norm_time(m.group(2)), norm_time(m.group(3)))
    ts = [t for t in re.split(r"[,،\s]+", s) if t]
    if not ts or not all(re.fullmatch(r"\d{1,2}:\d{2}", t) and _valid_time(t) for t in ts):
        raise ValueError("قالب نامعتبر. مثال: 10:00,13:00,16:00  یا  6  یا  6 09:00-22:00")
    return sorted({norm_time(t) for t in ts})

def save_times(times, uid):
    if not 1 <= len(times) <= 24:
        raise ValueError("تعداد پست روزانه باید بین ۱ تا ۲۴ باشد")
    cfg_set("cfg_post_times", ",".join(times))
    skip_past_slots(times)
    alog(uid, "تغییر ساعت‌های پست روزانه", ",".join(times))

# ------------------------- افزودن محصول با لینک -------------------------
def add_links(plat, chat, text):
    links = re.findall(r"https?://[^\s]+", text)[:20]
    if not links:
        show(plat, chat, None, "لینکی پیدا نشد. لینک کامل محصول را بفرستید.", [HOME])
        return
    for link in links:
        try:
            row = fetch_by_link(link)
            ex = sb("GET", T, {"product_id": f"eq.{row['product_id']}", "select": "product_id"})
            existed = ex.ok and bool(ex.json())
            upsert_rows([row])
            warn = "" if row.get("image_url") else "\n⚠️ عکس ندارد و پست نمی‌شود تا عکس اضافه شود."
            state = "" if row["active"] else "\n⚠️ در سایت ناموجود است و غیرفعال ثبت شد."
            show(plat, chat, None, f"{'🔄 قبلاً بود، به‌روز شد' if existed else '✅ اضافه شد'}: <b>{esc(row['name'])}</b>{warn}{state}",
                 [[B("🛍 مدیریت محصول", f"pd:{row['product_id']}"), B("📤 افزودن به صف", f"pq:{row['product_id']}")]])
        except Exception as e:
            show(plat, chat, None, f"❌ ناموفق: {esc(link)}\n{esc(e)}", [HOME])
        time.sleep(1)

def set_active(plat, chat, text, flag):
    links = re.findall(r"https?://[^\s]+", text)
    if not links:
        show(plat, chat, None, "لینک محصول را کنار دستور بنویسید.", [HOME])
        return
    row = find_in_db(links[0])
    if not row:
        show(plat, chat, None, "این محصول در دیتابیس نیست؛ ابتدا با لینک اضافه‌اش کنید.", [HOME])
        return
    sb("PATCH", T, {"product_id": f"eq.{row['product_id']}"}, {"active": flag})
    show(plat, chat, None, f"{'✅ فعال شد' if flag else '⏸️ از چرخه خارج شد'}: {esc(row['name'])}", [[B("🛍 مدیریت", f"pd:{row['product_id']}")]])

# ------------------------- ورودی متنی/عکس ادمین (حالت‌های انتظار) -------------------------
def ask(uid, mode, plat, chat, mid, prompt, back, **extra):
    _state[uid] = dict(mode=mode, ts=time.time(), mid=mid, **extra)
    show(plat, chat, mid, prompt, [[B("❌ انصراف", back)]])

def drop_msg(plat, chat, m):
    try:
        tgcall(plat, "deleteMessage", chat_id=chat, message_id=m["message_id"])
    except Exception:
        pass

def handle_input(plat, uid, chat, m, text, st):
    mode, mid = st["mode"], st.get("mid")
    if mode == "add":
        _state.pop(uid, None)
        add_links(plat, chat, text)
    elif mode == "search":
        _state.pop(uid, None)
        _view[uid] = {"q": text}
        drop_msg(plat, chat, m)
        screen_products(plat, chat, mid, uid, "s", 0)
    elif mode in ("pname", "pimg", "plink"):
        pid = st["pid"]
        if mode == "pname":
            if len(text) < 3:
                raise ValueError("نام خیلی کوتاه است")
            patch, what = {"name": text[:200]}, "نام"
        elif mode == "pimg":
            if not re.match(r"https?://", text):
                raise ValueError("آدرس تصویر باید با http شروع شود")
            patch, what = {"image_url": text}, "تصویر"
        else:
            link, slug = parse_product_link(text)
            patch, what = {"permalink": link, "slug": slug}, "لینک"
        r = sb("PATCH", T, {"product_id": f"eq.{pid}"}, patch)
        if not r.ok:
            raise RuntimeError(r.text[:150])
        _state.pop(uid, None)
        drop_msg(plat, chat, m)
        alog(uid, f"ویرایش {what} محصول", f"{pid}: {text[:80]}")
        screen_product(plat, chat, mid, pid, f"✅ {what} ذخیره شد.")
    elif mode == "cap":
        qid = st["qid"]
        if text.lower() == "/reset":
            val = None
        elif 5 <= len(text) <= 600:
            val = text
        else:
            raise ValueError("متن باید بین ۵ تا ۶۰۰ کاراکتر باشد")
        r = sb("PATCH", "post_queue", {"id": f"eq.{qid}", "status": "in.(pending,failed)"}, {"caption_override": val}, "return=representation")
        if not r.ok or not r.json():
            raise RuntimeError("این مورد دیگر قابل ویرایش نیست")
        _state.pop(uid, None)
        drop_msg(plat, chat, m)
        alog(uid, "ویرایش متن پست", f"صف {qid}")
        screen_qitem(plat, chat, mid, qid, "✅ متن ذخیره شد." if val else "↩️ متن به حالت خودکار برگشت.")
    elif mode == "img":
        qid = st["qid"]
        val = None
        if m.get("photo"):
            val = "tg:" + m["photo"][-1]["file_id"]
        elif re.match(r"https?://", text):
            val = text
        elif text.lower() != "/reset":
            raise ValueError("یک عکس یا آدرس تصویر (http...) بفرستید. برای برگشت به تصویر محصول: /reset")
        r = sb("PATCH", "post_queue", {"id": f"eq.{qid}", "status": "in.(pending,failed)"}, {"image_override": val}, "return=representation")
        if not r.ok or not r.json():
            raise RuntimeError("این مورد دیگر قابل ویرایش نیست")
        _state.pop(uid, None)
        drop_msg(plat, chat, m)
        alog(uid, "تغییر تصویر پست", f"صف {qid}")
        screen_qitem(plat, chat, mid, qid, "✅ تصویر ذخیره شد." if val else "↩️ تصویر به حالت پیش‌فرض برگشت.")
    elif mode == "sched":
        qid = st["qid"]
        dt = parse_when(text)
        if dt < datetime.now(TZ) - timedelta(minutes=1):
            raise ValueError("این زمان گذشته است")
        set_schedule(qid, dt, uid)
        _state.pop(uid, None)
        drop_msg(plat, chat, m)
        screen_qitem(plat, chat, mid, qid, f"🕒 زمان‌بندی شد: {fmt_dt(dt)}")
    elif mode == "times":
        times = parse_times_input(text)
        save_times(times, uid)
        _state.pop(uid, None)
        drop_msg(plat, chat, m)
        screen_times(plat, chat, mid, "✅ ذخیره شد.")

# ------------------------- مسیریابی دکمه‌ها -------------------------
def route_button(plat, uid, chat, mid, data):
    a = data.split(":")
    c = a[0]
    if c not in ("x",):
        _state.pop(uid, None)
    if c == "m":
        screen_menu(plat, chat, mid)
    elif c == "st":
        screen_status(plat, chat, mid)
    elif c == "pn":
        show(plat, chat, mid, "⏳ در حال انتخاب محصول و ارسال… (چند ثانیه صبر کنید)", [])
        res = do_post(f"manual_{int(time.time())}")
        alog(uid, "ارسال فوری", res.split("\n")[0])
        show(plat, chat, mid, "📤 " + esc(res), [[B("📜 تاریخچه", "hist:a:0"), B("🏠", "m")]])
    elif c == "add":
        ask(uid, "add", plat, chat, mid, "➕ لینک محصول را بفرستید (چند لینک را می‌توانید زیر هم بفرستید).", "m")
    elif c == "pl":
        screen_products(plat, chat, mid, uid, a[1], int(a[2]))
    elif c == "psr":
        ask(uid, "search", plat, chat, mid, "🔍 بخشی از نام محصول را بنویسید:", "pl:a:0")
    elif c == "pd":
        screen_product(plat, chat, mid, int(a[1]))
    elif c == "pt":
        p = get_product(int(a[1]))
        if p:
            sb("PATCH", T, {"product_id": f"eq.{a[1]}"}, {"active": not p.get("active")})
            alog(uid, "فعال/غیرفعال محصول", f"{a[1]} → {'غیرفعال' if p.get('active') else 'فعال'}")
        screen_product(plat, chat, mid, int(a[1]))
    elif c == "pe":
        pid, kind = int(a[1]), a[2]
        prompts = {"n": ("pname", "✏️ نام جدید محصول را بفرستید:"),
                   "i": ("pimg", "🖼 آدرس مستقیم تصویر جدید (http…) را بفرستید:"),
                   "l": ("plink", "🔗 لینک جدید محصول در سایت را بفرستید:")}
        mode, prompt = prompts[kind]
        ask(uid, mode, plat, chat, mid, prompt, f"pd:{pid}", pid=pid)
    elif c == "pdel":
        p = get_product(int(a[1])) or {}
        show(plat, chat, mid, f"🗑 «<b>{esc(p.get('name'))}</b>» برای همیشه از دیتابیس حذف شود؟\nموردهای منتظر آن در صف هم لغو می‌شوند. تاریخچه‌ی انتشار می‌ماند.",
             [[B("✅ بله، حذف شود", f"pdy:{a[1]}"), B("↩️ نه", f"pd:{a[1]}")]])
    elif c == "pdy":
        delete_product(int(a[1]), uid)
        screen_products(plat, chat, mid, uid, "a", 0)
    elif c == "pq":
        qid, fresh = enqueue(int(a[1]))
        alog(uid, "افزودن به صف", f"محصول {a[1]} → صف {qid}")
        screen_qitem(plat, chat, mid, qid, "🟢 به صف اضافه شد." if fresh else "ℹ️ این محصول از قبل در صف منتظر است (تکراری اضافه نشد).")
    elif c == "ql":
        screen_queue(plat, chat, mid, a[1], int(a[2]))
    elif c == "qd":
        screen_qitem(plat, chat, mid, int(a[1]))
    elif c == "qx":
        r = sb("PATCH", "post_queue", {"id": f"eq.{a[1]}", "status": "in.(pending,failed)"}, {"status": "cancelled"}, "return=representation")
        if r.ok and r.json():
            alog(uid, "لغو مورد صف", a[1])
            screen_qitem(plat, chat, mid, int(a[1]), "🚫 لغو شد.")
        else:
            screen_qitem(plat, chat, mid, int(a[1]), "⚠️ لغو ممکن نشد (احتمالاً همین حالا در حال انتشار است یا تمام شده).")
    elif c == "qr":
        qid = int(a[1])
        r = sb("PATCH", "post_queue", {"id": f"eq.{qid}", "status": "eq.failed"}, {"status": "pending", "last_error": None}, "return=representation")
        if not (r.ok and r.json()):
            screen_qitem(plat, chat, mid, qid, "⚠️ این مورد ناموفق نیست یا قبلاً دوباره شروع شده است.")
        else:
            alog(uid, "تلاش مجدد", f"صف {qid}")
            screen_qitem(plat, chat, mid, qid, "🔁 تلاش مجدد شروع شد؛ نتیجه همین‌جا اعلام می‌شود.")
            publish_bg(plat, chat, qid)
    elif c == "qe":
        ask(uid, "cap", plat, chat, mid, "✏️ متن توضیح پست را بفرستید (۵ تا ۶۰۰ کاراکتر).\nاین متن بین نام محصول و دعوت به سایت قرار می‌گیرد.\nبرای برگشت به متن خودکار: /reset", f"qd:{a[1]}", qid=int(a[1]))
    elif c == "qi":
        ask(uid, "img", plat, chat, mid, "🖼 عکس جدید را همین‌جا بفرستید، یا آدرس تصویر (http…) بنویسید.\nبرای برگشت به تصویر محصول: /reset", f"qd:{a[1]}", qid=int(a[1]))
    elif c == "qa":
        qid = int(a[1])
        it, p = get_item(qid), None
        p = get_product(it["product_id"]) if it else None
        if not p:
            screen_qitem(plat, chat, mid, qid, "⚠️ محصول پیدا نشد.")
            return
        show(plat, chat, mid, "🤖 در حال نوشتن ۳ متن پیشنهادی…", [])
        vs = ai_variants(p, 3)
        if not vs:
            screen_qitem(plat, chat, mid, qid, "⚠️ هوش مصنوعی الان جواب نداد؛ کمی بعد دوباره امتحان کنید.")
            return
        _variants[qid] = vs
        text = f"🤖 <b>متن‌های پیشنهادی برای {esc(p['name'])}</b>\n\n" + "\n\n".join(f"<b>{fa(i + 1)})</b> {esc(t)}" for i, t in enumerate(vs))
        rows = [[B(f"✅ انتخاب {fa(i + 1)}", f"qv:{qid}:{i}") for i in range(len(vs))],
                [B("🔄 ۳ متن دیگر", f"qa:{qid}"), B("⬅️ بازگشت", f"qd:{qid}")]]
        show(plat, chat, mid, text, rows)
    elif c == "qv":
        qid, i = int(a[1]), int(a[2])
        vs = _variants.get(qid)
        if not vs or i >= len(vs):
            screen_qitem(plat, chat, mid, qid, "⚠️ این پیشنهادها منقضی شده‌اند؛ دوباره «۳ متن AI» را بزنید.")
            return
        r = sb("PATCH", "post_queue", {"id": f"eq.{qid}", "status": "in.(pending,failed)"}, {"caption_override": vs[i]}, "return=representation")
        if r.ok and r.json():
            alog(uid, "انتخاب متن AI", f"صف {qid} متن {i + 1}")
            screen_qitem(plat, chat, mid, qid, f"✅ متن {fa(i + 1)} انتخاب شد.")
        else:
            screen_qitem(plat, chat, mid, qid, "⚠️ این مورد دیگر قابل ویرایش نیست.")
    elif c == "qs":
        qid = int(a[1])
        if len(a) == 2:
            screen_schedule(plat, chat, mid, qid)
        elif a[2] == "c":
            ask(uid, "sched", plat, chat, mid, "✍️ تاریخ و ساعت را بنویسید:\n<code>1405/09/10 18:30</code>\n<code>18:30</code> (امروز/فردا)\n<code>+45</code> (۴۵ دقیقه دیگر) · <code>+2h</code> (۲ ساعت دیگر)\n<code>2026-12-01 18:30</code>", f"qs:{qid}", qid=qid)
        elif a[2] == "x":
            set_schedule(qid, None, uid)
            screen_qitem(plat, chat, mid, qid, "↩️ زمان‌بندی حذف شد؛ به‌ترتیب صف منتشر می‌شود.")
        else:
            dt = preset_time(a[2])
            set_schedule(qid, dt, uid)
            screen_qitem(plat, chat, mid, qid, f"🕒 زمان‌بندی شد: {fmt_dt(dt)}")
    elif c == "qn":
        qid = int(a[1])
        alog(uid, "انتشار فوری صف", str(qid))
        show(plat, chat, mid, "🚀 انتشار شروع شد؛ نتیجه را همین‌جا می‌فرستم…", [])
        publish_bg(plat, chat, qid)
    elif c == "qp":
        preview(plat, chat, int(a[1]))
    elif c == "qc":
        it = get_item(int(a[1]))
        if it:
            qid, fresh = enqueue(it["product_id"], copy_from=it)
            alog(uid, "انتشار دوباره", f"صف {a[1]} → {qid}")
            screen_qitem(plat, chat, mid, qid, "🟢 مورد جدید ساخته شد." if fresh else "ℹ️ این محصول از قبل در صف منتظر است.")
    elif c == "cal":
        screen_calendar(plat, chat, mid, int(a[1]))
    elif c == "stat":
        screen_stats(plat, chat, mid, int(a[1]))
    elif c == "hist":
        screen_history(plat, chat, mid, a[1], int(a[2]))
    elif c == "log":
        screen_log(plat, chat, mid)
    elif c == "set":
        screen_settings(plat, chat, mid)
    elif c == "sp":
        cfg_set(f"cfg_en_{a[1]}", "0" if plat_enabled(a[1]) else "1")
        alog(uid, "تغییر مقصد انتشار", f"{a[1]} → {'روشن' if plat_enabled(a[1]) else 'خاموش'}")
        screen_settings(plat, chat, mid)
    elif c == "sai":
        new = "0" if cfg("cfg_ai_best", "0") == "1" else "1"
        cfg_set("cfg_ai_best", new)
        alog(uid, "انتخاب خودکار بهترین متن", "روشن" if new == "1" else "خاموش")
        screen_settings(plat, chat, mid)
    elif c == "stm":
        screen_times(plat, chat, mid)
    elif c == "stn":
        save_times(gen_times(int(a[1])), uid)
        screen_times(plat, chat, mid, "✅ ذخیره شد.")
    elif c == "stt":
        ask(uid, "times", plat, chat, mid, "✍️ ساعت‌ها را بنویسید:\n<code>10:00,13:00,16:00,19:00,21:00</code>\nیا فقط تعداد: <code>6</code>\nیا تعداد با بازه: <code>6 09:00-22:00</code>", "stm")
    elif c == "stdef":
        cfg_del("cfg_post_times")
        alog(uid, "برگشت ساعت‌ها به پیش‌فرض", "")
        screen_times(plat, chat, mid, "↩️ به پیش‌فرض سرور برگشت.")
    elif c == "stg":
        if len(a) == 1:
            screen_gap(plat, chat, mid)
        else:
            cfg_set("cfg_queue_gap", int(a[1]))
            alog(uid, "فاصله بین پست‌های صف", f"{a[1]} دقیقه")
            screen_gap(plat, chat, mid)
    elif c == "x":
        _state.pop(uid, None)
        screen_menu(plat, chat, mid)
    else:
        screen_menu(plat, chat, mid)

# ------------------------- ورودی کلی تلگرام -------------------------
def handle_update(plat, u):
    if u.get("callback_query"):
        cb = u["callback_query"]
        uid, chat, mid = str(cb["from"]["id"]), cb["message"]["chat"]["id"], cb["message"]["message_id"]
        tgcall(plat, "answerCallbackQuery", callback_query_id=cb["id"])
        if uid not in plat.admins:
            return
        try:
            route_button(plat, uid, chat, mid, cb.get("data") or "")
        except Exception as e:
            log.exception("button %s: %s", cb.get("data"), e)
            show(plat, chat, None, f"❌ خطا: {esc(e)}", [HOME])
        return
    m = u.get("message")
    if not m:
        return
    uid, chat = str((m.get("from") or {}).get("id")), m["chat"]["id"]
    text = (m.get("text") or m.get("caption") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd == "/myid":
        plat.send_text(chat, f"شناسه‌ی شما: {uid}\nشناسه‌ی این چت: {chat}")
        return
    if uid not in plat.admins:
        return
    try:
        st = _state.get(uid)
        if st and time.time() - st["ts"] > 900:
            _state.pop(uid, None)
            st = None
        if cmd == "/cancel":
            _state.pop(uid, None)
            screen_menu(plat, chat, None, "انصراف داده شد.")
        elif cmd in ("/start", "/menu", "/help"):
            _state.pop(uid, None)
            screen_menu(plat, chat, None)
        elif cmd in ("/status", "/post", "/products", "/queue", "/history", "/calendar", "/stats", "/settings", "/log"):
            _state.pop(uid, None)
            route_button(plat, uid, chat, None, {"/status": "st", "/post": "pn", "/products": "pl:a:0", "/queue": "ql:p:0",
                                                 "/history": "hist:a:0", "/calendar": "cal:0", "/stats": "stat:7",
                                                 "/settings": "set", "/log": "log"}[cmd])
        elif cmd in ("/cancelqueue", "/retryqueue"):
            parts = text.split()
            if len(parts) < 2 or not parts[1].isdigit():
                show(plat, chat, None, f"شناسه‌ی صف را کنار دستور بنویسید. مثال: {cmd} 12", [HOME])
            else:
                route_button(plat, uid, chat, None, ("qx:" if cmd == "/cancelqueue" else "qr:") + parts[1])
        elif cmd in ("/off", "/on"):
            set_active(plat, chat, text, cmd == "/on")
        elif st and (text or m.get("photo")):
            try:
                handle_input(plat, uid, chat, m, text, st)
            except (ValueError, RuntimeError) as e:          # ورودی اشتباه: حالت انتظار می‌ماند
                show(plat, chat, None, f"⚠️ {esc(e)}\nدوباره بفرستید یا /cancel را بزنید.", [])
        elif SITE_URL.split("//")[-1].removeprefix("www.") in text:
            add_links(plat, chat, text)
        else:
            screen_menu(plat, chat, None)
    except Exception as e:
        log.exception("message: %s", e)
        show(plat, chat, None, f"❌ خطا: {esc(e)}", [HOME])

def _run_poll(plat):
    while True:
        try:
            plat.poll(handle_update)
        except Exception as e:
            log.exception("poll %s: %s", plat.name, e)
            time.sleep(10)

# ------------------------- وب‌سرور (برای UptimeRobot) -------------------------
app = Flask(__name__)

@app.get("/")
def home():
    return "ok"

@app.get("/health")
def health():
    return jsonify(status="up", time=datetime.now(TZ).isoformat())


@app.get("/rubika-id")
def rubika_id():
    """صفحه‌ی موقت برای پیدا کردن chat_id روبیکا. فقط با SETUP_KEY کار می‌کند؛ بعد از استفاده SETUP_KEY را حذف کنید."""
    from flask import request, Response
    key = env("SETUP_KEY")
    if not key or not hmac.compare_digest(request.args.get("key", ""), key):
        return "forbidden", 403
    tok = env("RUBIKA_TOKEN")
    if not tok:
        return Response("RUBIKA_TOKEN تنظیم نشده", 400, mimetype="text/plain; charset=utf-8")
    try:
        r = requests.post(f"https://botapi.rubika.ir/v3/{tok}/getUpdates", json={"limit": 50}, timeout=30)
        data = r.json()
    except Exception as e:
        return Response(f"خطا: {e}", 500, mimetype="text/plain; charset=utf-8")
    ups = (data.get("data") or data).get("updates") or []
    out, seen = [], set()
    for u in ups:
        cid = u.get("chat_id") or (u.get("new_message") or {}).get("chat_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        txt = ((u.get("new_message") or {}).get("text") or "")[:40]
        out.append(f"chat_id: {cid}   (نوع رویداد: {u.get('type')}  متن: {txt})")
    if not out:
        out = ["چیزی پیدا نشد. اول در روبیکا یک پیام بفرستید (به ربات یا داخل کانال) و صفحه را دوباره باز کنید.",
               "پاسخ خام روبیکا:", json.dumps(data, ensure_ascii=False)[:1500]]
    return Response("\n".join(out), mimetype="text/plain; charset=utf-8")

_started = False
def start_background():
    global _started
    if _started:
        return
    _started = True
    schema_check()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    tgp = tg_platform()
    if tgp:
        if not tgp.admins:
            log.warning("ADMIN_USER_IDS_TELEGRAM خالی است؛ هیچ‌کس نمی‌تواند پنل را استفاده کند.")
        threading.Thread(target=_run_poll, args=(tgp,), daemon=True).start()
    else:
        log.warning("پنل مدیریتی فعال نیست: TELEGRAM_TOKEN تنظیم نشده است. ارسال زمان‌بندی‌شده همچنان کار می‌کند.")

start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
