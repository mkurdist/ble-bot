"""
ربات بله - پست‌گذاری خودکار محصولات فروشگاه در کانال
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
def pick_product():
    r = sb("GET", T, {"select": "*", "active": "eq.true"})
    items = r.json() if r.ok else []
    if not r.ok:
        log.error("خواندن محصولات ناموفق: %s", r.text[:200])
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

# ------------------------- ارسال یک اسلات (همه‌ی پلتفرم‌ها با هم) -------------------------
def _do_post(slot_key):
    """اسلات را قفل می‌کند (ضد پست دوبل)، یک محصول انتخاب می‌کند و به همه‌ی پلتفرم‌های فعال می‌فرستد."""
    if not PLATFORMS:
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
    body = ai_body(p)                       # یک بار تولید متن برای همه‌ی پلتفرم‌ها
    try:
        img = prepare_image(p["image_url"])
    except Exception as e:
        log.warning("دانلود عکس ناموفق: %s", e)
        img = None
    results = {}
    for plat in PLATFORMS:
        try:
            results[plat.name] = bool(plat.send_post(p, build_caption(p, body, plat), img))
        except Exception as e:
            log.warning("%s: %s", plat.name, e)
            results[plat.name] = False
    summary = " ".join(f"{k}:{'✅' if v else '❌'}" for k, v in results.items())
    if any(results.values()):
        try:   # بعد از ارسال موفق، خطای دیتابیس نباید باعث ارسال دوباره شود
            sb("PATCH", T, {"product_id": f"eq.{p['product_id']}"},
               {"posted_count": p["posted_count"] + 1, "last_posted_at": datetime.now(timezone.utc).isoformat()})
            sb("PATCH", "post_slots", {"slot": f"eq.{slot_key}"}, {"product_id": p["product_id"]})
        except Exception as e:
            log.error("ثبت نتیجه در دیتابیس ناموفق (پست ارسال شده): %s", e)
        return f"ارسال شد: {p['name']}\n{summary}"
    sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})   # اسلات آزاد شود تا دوباره تلاش شود
    return f"ارسال ناموفق ({summary})"

_post_lock = threading.Lock()   # پست دستی و زمان‌بندی هم‌زمان یک محصول را دوبار انتخاب نکنند

def do_post(slot_key):
    with _post_lock:
        try:
            return _do_post(slot_key)
        except Exception as e:
            log.exception("do_post: %s", e)
            try:
                sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})   # اسلات سوخته نشود
            except Exception:
                pass
            return "ارسال ناموفق (خطای داخلی)"

# ------------------------- زمان‌بند (ترد پس‌زمینه) -------------------------
_retry_after = {}
def scheduler_loop():
    time.sleep(5)
    while True:
        try:
            now = datetime.now(TZ)
            for t in POST_TIMES:
                hh, mm = map(int, t.split(":"))
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due <= now <= due + timedelta(minutes=CATCHUP_MINUTES):
                    slot = f"{now:%Y-%m-%d}_{t}"
                    if _retry_after.get(slot, 0) > time.time():
                        continue
                    res = do_post(slot)
                    if res.startswith(("ارسال ناموفق", "خطای دیتابیس", "محصول آماده‌ای نیست", "هیچ پلتفرمی")):
                        _retry_after[slot] = time.time() + 900     # ۱۵ دقیقه صبر بعد از شکست
                    if res != "قبلاً انجام شده":
                        log.info("اسلات %s: %s", t, res)
        except Exception as e:
            log.exception("scheduler: %s", e)
        time.sleep(60)

# ------------------------- پنل ادمین (یکسان در بله / تلگرام / روبیکا) -------------------------
_waiting = {}   # (پلتفرم, شناسه‌ی کاربر) -> "add"

def status_text():
    r = sb("GET", T, {"select": "posted_count", "active": "eq.true"})
    items = r.json() if r.ok else []
    if not items:
        return "محصول فعالی در دیتابیس نیست." if r.ok else f"خطای دیتابیس: {r.text[:200]}"
    mn = min(i["posted_count"] for i in items)
    left = sum(1 for i in items if i["posted_count"] == mn)
    return (f"محصولات فعال: {len(items)}\nدور فعلی چرخه: {mn + 1}\n"
            f"باقی‌مانده تا پایان این دور: {left}\nساعت‌های پست: {', '.join(POST_TIMES)}\n"
            f"پست می‌شود در: {', '.join(x.name for x in PLATFORMS) or '-'}")

def add_links(plat, chat, text):
    links = re.findall(r"https?://[^\s]+", text)[:20]
    if not links:
        plat.send_text(chat, "لینکی پیدا نشد. لینک کامل محصول را بفرستید.")
        return
    for link in links:
        try:
            row = fetch_by_link(link)
            ex = sb("GET", T, {"product_id": f"eq.{row['product_id']}", "select": "product_id"})
            existed = ex.ok and bool(ex.json())
            upsert_rows([row])
            warn = "" if row.get("image_url") else "\n⚠️ عکس ندارد و پست نمی‌شود تا عکس اضافه شود."
            state = "" if row["active"] else "\n⚠️ در سایت ناموجود است و غیرفعال ثبت شد."
            plat.send_text(chat, f"{'🔄 قبلاً بود، به‌روز شد' if existed else '✅ اضافه شد'}: {row['name']}{warn}{state}")
        except Exception as e:
            plat.send_text(chat, f"❌ ناموفق: {link}\n{e}")
        time.sleep(1)

def set_active(plat, chat, text, flag):
    links = re.findall(r"https?://[^\s]+", text)
    if not links:
        plat.send_text(chat, "لینک محصول را کنار دستور بنویسید.")
        return
    row = find_in_db(links[0])
    if not row:
        plat.send_text(chat, "این محصول در دیتابیس نیست؛ ابتدا با لینک اضافه‌اش کنید.")
        return
    sb("PATCH", T, {"product_id": f"eq.{row['product_id']}"}, {"active": flag})
    plat.send_text(chat, f"{'✅ فعال شد' if flag else '⏸️ از چرخه خارج شد'}: {row['name']}")

def run_action(plat, uid, chat, action):
    if action == "add":
        _waiting[(plat.name, uid)] = "add"
        plat.send_text(chat, "لینک محصول را بفرستید (می‌توانید چند لینک را زیر هم بفرستید).")
    elif action == "status":
        plat.send_text(chat, status_text())
    elif action == "post":
        plat.send_text(chat, do_post(f"manual_{int(time.time())}"))

def handle_event(plat, kind, uid, chat, data):
    """ورودی یکسان برای هر سه پلتفرم: kind = message | button"""
    uid = str(uid)
    if kind == "message":
        text = (data or "").strip()
        cmd = text.split(" ")[0].split("@")[0].lower()
        if cmd == "/myid":
            plat.send_text(chat, f"شناسه‌ی شما در {plat.name}: {uid}\nشناسه‌ی این چت: {chat}")
            return
    if uid not in plat.admins:
        return
    if kind == "button":
        run_action(plat, uid, chat, data)
        return
    if cmd in ("/start", "/menu", "/help"):
        plat.send_menu(chat)
    elif cmd == "/status":
        run_action(plat, uid, chat, "status")
    elif cmd == "/post":
        run_action(plat, uid, chat, "post")
    elif cmd in ("/off", "/on"):
        try:
            set_active(plat, chat, text, cmd == "/on")
        except Exception as e:
            plat.send_text(chat, f"❌ {e}")
    elif _waiting.get((plat.name, uid)) == "add" or SITE_URL.split("//")[-1] in text:
        _waiting.pop((plat.name, uid), None)
        add_links(plat, chat, text)

def _run_poll(plat):
    while True:
        try:
            plat.poll(handle_event)
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
    threading.Thread(target=scheduler_loop, daemon=True).start()
    for plat in ALL_PLATFORMS:
        threading.Thread(target=_run_poll, args=(plat,), daemon=True).start()

start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
