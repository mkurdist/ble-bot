"""
ربات بله - پست‌گذاری خودکار محصولات فروشگاه (ووکامرس) در کانال
- منبع محصولات: فایل CSV خروجی پنل (یک‌بار) + افزودن تکی با لینک؛ هیچ همگام‌سازی خودکاری با سایت نیست
- ذخیره‌سازی: Supabase (چرخه‌ی پست، جلوگیری از تکرار، لاگ اسلات‌ها)
- متن پست: Gemini با چرخش بین چند کلید
"""
import csv
import io
import json
import logging
import os
import random
import re
import threading
import time
import zlib
from datetime import datetime, timedelta
from html import unescape
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
CHANNEL_ID = env("CHANNEL_ID", required=True)          # مثل @mychannel
SB_URL = env("SUPABASE_URL", required=True).rstrip("/")
SB_KEY = env("SUPABASE_KEY", required=True)
GEMINI_KEYS = [k.strip() for k in env("GEMINI_KEYS", "").split(",") if k.strip()]
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
SITE_URL = env("SITE_URL", "https://citysofal.ir").rstrip("/")
ADMIN_USERNAME = env("ADMIN_USERNAME", "").lstrip("@")
ADMIN_PHONE = env("ADMIN_PHONE", "")
ADMIN_USER_IDS = {int(x) for x in env("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()}
POST_TIMES = [t.strip() for t in env("POST_TIMES", "10:00,13:00,16:00,19:00,21:00").split(",") if t.strip()]
ORDER_MODE = env("ORDER_MODE", "random")               # random | sequential
SHOW_PRICE = env("SHOW_PRICE", "false").lower() == "true"
CATCHUP_MINUTES = int(env("CATCHUP_MINUTES", "180"))   # اگر سرور خواب بود تا چند دقیقه دیرتر هم پست بزند
TZ = ZoneInfo(env("TZ_NAME", "Asia/Tehran"))
BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; CitySofalBot/1.0)"}

STYLE_HINTS = [
    "لحن گرم و صمیمی، تأکید بر زیبایی و حس خانه",
    "تأکید بر کاربرد و استفاده‌ی روزمره",
    "به‌عنوان یک ایده‌ی هدیه‌ی خاص",
    "تأکید بر دست‌ساز بودن و اصالت (فقط اگر در مشخصات محصول آمده)",
    "تأکید بر چیدمان و دکوراسیون خانه",
    "کوتاه، شاعرانه و دلنشین",
]

# ------------------------- Supabase (REST ساده) -------------------------
def sb(method, table, params=None, body=None, prefer=None):
    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    r = requests.request(method, f"{SB_URL}/rest/v1/{table}", headers=headers,
                         params=params, data=json.dumps(body) if body is not None else None, timeout=30)
    return r

def kv_get(k):
    r = sb("GET", "kv", {"k": f"eq.{k}", "select": "v"})
    if r.ok and r.json():
        return r.json()[0]["v"]
    return None

def kv_set(k, v):
    sb("POST", "kv", {"on_conflict": "k"}, [{"k": k, "v": str(v)}], "resolution=merge-duplicates")

# ------------------------- ورود محصولات (CSV خروجی پنل + افزودن با لینک) -------------------------
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", unescape(s)).strip()

def fmt_num_price(x):
    try:
        return f"{int(float(str(x).replace(',', '').strip())):,} تومان"
    except Exception:
        return None

def fmt_price(prices):
    try:
        minor = int(prices.get("currency_minor_unit", 0))
        val = int(prices["price"]) / (10 ** minor)
        if prices.get("currency_code") == "IRR":
            val /= 10
        return f"{int(val):,} تومان"
    except Exception:
        return None

def row_from_api(p):
    imgs = p.get("images") or []
    return {
        "id": p["id"],
        "title": strip_html(p.get("name")),
        "url": p.get("permalink"),
        "image_url": imgs[0]["src"] if imgs else None,
        "price": fmt_price(p.get("prices") or {}),
        "short_desc": strip_html(p.get("short_description"))[:600],
        "description": strip_html(p.get("description"))[:900],
        "categories": [strip_html(c["name"]) for c in (p.get("categories") or [])][:3],
        "sort_order": int(time.time()) % 2_000_000_000,
        "active": bool(p.get("is_in_stock", True)),
    }

def fetch_by_link(link):
    """فقط همان یک محصول را از سایت می‌گیرد (۱ درخواست سبک)."""
    from urllib.parse import urlparse, unquote
    link = link.strip().rstrip(".,)»")
    slug = unquote(urlparse(link).path.strip("/").split("/")[-1])
    if slug:
        r = requests.get(f"{SITE_URL}/wp-json/wc/store/v1/products",
                         params={"slug": slug}, headers=UA, timeout=30)
        if r.status_code == 200 and r.json():
            return row_from_api(r.json()[0])
    # فالبک: خواندن تگ‌های og از خود صفحه
    r = requests.get(link, headers=UA, timeout=30)
    r.raise_for_status()
    def og(name):
        m = re.search(r'<meta[^>]+property=["\']og:%s["\'][^>]+content=["\']([^"\']+)' % name, r.text)
        return unescape(m.group(1)).strip() if m else None
    title, image = og("title"), og("image")
    if not title:
        raise RuntimeError("اطلاعات محصول از لینک خوانده نشد")
    title = re.sub(r"\s*[-–|]\s*شهر سفال\s*$", "", title)
    return {"id": zlib.crc32(link.encode()), "title": title, "url": link, "image_url": image,
            "price": None, "short_desc": (og("description") or "")[:600], "description": "",
            "categories": [], "sort_order": int(time.time()) % 2_000_000_000, "active": True}

def upsert_rows(rows):
    # فقط ستون‌های موجود در rows به‌روز می‌شوند؛ posted_count و last_posted_at دست‌نخورده می‌مانند
    for i in range(0, len(rows), 100):
        r = sb("POST", "products", {"on_conflict": "id"}, rows[i:i + 100], "resolution=merge-duplicates")
        if not r.ok:
            raise RuntimeError(f"ذخیره در Supabase ناموفق: {r.text[:200]}")

_ALIASES = {
    "id": ["id", "شناسه"],
    "type": ["type", "نوع"],
    "name": ["name", "نام"],
    "published": ["published", "منتشر شده", "منتشرشده"],
    "instock": ["in stock?", "in stock", "موجود؟", "موجود", "وضعیت موجودی"],
    "short": ["short description", "توضیحات کوتاه"],
    "desc": ["description", "توضیحات"],
    "regular": ["regular price", "قیمت عادی"],
    "sale": ["sale price", "قیمت فروش ویژه", "قیمت حراج"],
    "cats": ["categories", "دسته‌ها", "دسته ها", "دسته‌بندی‌ها", "دسته بندی ها"],
    "images": ["images", "تصاویر", "تصویر"],
    "link": ["permalink", "پیوند یکتا", "لینک"],
}

def _norm(h):
    return (h or "").replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ").strip().lower().replace("  ", " ")

def import_csv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = {}
    for h in (reader.fieldnames or []):
        for key, names in _ALIASES.items():
            if key not in cols and _norm(h) in [_norm(n) for n in names]:
                cols[key] = h
    for need in ("id", "name", "images"):
        if need not in cols:
            raise RuntimeError("ستون «%s» در فایل پیدا نشد. ستون‌های فایل: %s" % (need, " | ".join(reader.fieldnames or [])[:300]))
    g = lambda row, k: (row.get(cols[k]) or "").strip() if k in cols else ""
    rows, skipped = [], 0
    for i, row in enumerate(reader, 1):
        if g(row, "type").lower() in ("variation", "تنوع"):
            skipped += 1
            continue
        pid = g(row, "id")
        if not pid.isdigit() or not g(row, "name"):
            skipped += 1
            continue
        if "published" in cols and g(row, "published") not in ("1", ""):
            skipped += 1                                   # پیش‌نویس/خصوصی
            continue
        img = (re.split(r"[,\n]\s*", g(row, "images")) or [""])[0].strip()
        stock = g(row, "instock")
        cats = [c.split(">")[-1].strip() for c in g(row, "cats").split(",") if c.strip()][:3]
        rows.append({
            "id": int(pid), "title": strip_html(g(row, "name")),
            "url": g(row, "link") or f"{SITE_URL}/?p={pid}",
            "image_url": img or None,
            "price": fmt_num_price(g(row, "sale") or g(row, "regular")),
            "short_desc": strip_html(g(row, "short"))[:600],
            "description": strip_html(g(row, "desc"))[:900],
            "categories": cats, "sort_order": i,
            "active": stock not in ("0", "outofstock", "ناموجود"),
        })
    if not rows:
        raise RuntimeError("هیچ محصولی در فایل پیدا نشد")
    upsert_rows(rows)
    no_img = sum(1 for r in rows if not r["image_url"])
    return len(rows), skipped, no_img

# ------------------------- انتخاب محصول بعدی (چرخه) -------------------------
def pick_product():
    r = sb("GET", "products", {"select": "*", "active": "eq.true"})
    items = r.json() if r.ok else []
    items = [p for p in items if p.get("image_url")]
    if not items:
        return None
    min_count = min(p["posted_count"] for p in items)      # فقط از کم‌پستی‌ها انتخاب می‌شود => چرخه‌ی بدون تکرار
    pool = [p for p in items if p["posted_count"] == min_count]
    if ORDER_MODE == "sequential":
        return min(pool, key=lambda p: p["sort_order"])
    return random.choice(pool)

# ------------------------- Gemini -------------------------
_key_idx = 0
def gemini_text(prompt):
    global _key_idx
    if not GEMINI_KEYS:
        return None
    n = len(GEMINI_KEYS)
    for i in range(n):
        key = GEMINI_KEYS[(_key_idx + i) % n]
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 1.0, "maxOutputTokens": 400}},
                timeout=40)
            if r.status_code == 200:
                _key_idx = (_key_idx + i + 1) % n       # کلید بعدی برای نوبت بعد
                parts = r.json()["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts).strip()
            log.warning("gemini key#%d -> HTTP %s", (_key_idx + i) % n, r.status_code)
        except Exception as e:
            log.warning("gemini error: %s", e)
    return None

def ai_body(p):
    prompt = f"""تو کپی‌رایتر کانال فروشگاه «شهر سفال» (سفال، سرامیک، گلدان، دکوری) هستی.
برای محصول زیر یک متن کوتاه فارسی برای پست کانال بنویس.

قوانین:
- ۲ تا ۳ خط کوتاه، حداکثر ۲۵۰ کاراکتر، لحن: {random.choice(STYLE_HINTS)}
- فقط از اطلاعات زیر استفاده کن؛ ابعاد، جنس، ضمانت یا ویژگی‌ای که نیامده را اختراع نکن.
- قیمت، شماره تماس، لینک و هشتگ ننویس.
- از ستاره، کروشه و بک‌تیک استفاده نکن. حداکثر ۲ ایموجی مرتبط.
- با «سلام» یا «به نظر من» شروع نکن. فقط خود متن را بده، بدون توضیح اضافه.

نام محصول: {p['title']}
دسته: {', '.join(p.get('categories') or [])}
توضیح کوتاه: {p.get('short_desc') or '-'}
توضیحات: {(p.get('description') or '-')[:600]}"""
    txt = gemini_text(prompt)
    if not txt:
        return "یک انتخاب زیبا برای خانه‌ی شما ✨\nجزئیات و تصاویر بیشتر را در سایت ببینید."
    txt = re.sub(r"[*`\[\]]", "", txt).strip()
    return txt[:350]

def build_caption(p):
    lines = [f"✅ {p['title']}", "", ai_body(p), ""]
    if SHOW_PRICE and p.get("price"):
        lines.append(f"🏷️ {p['price']}")
    else:
        lines.append("🌐 برای مشاهده قیمت و سفارش وارد سایت شوید")
    if ADMIN_USERNAME:
        lines.append(f"💬 سفارش: @{ADMIN_USERNAME}")
    if ADMIN_PHONE:
        lines.append(f"📞 {ADMIN_PHONE}")
    tags = " ".join("#" + re.sub(r"[\s‌\-]+", "_", c) for c in (p.get("categories") or [])[:2])
    if tags:
        lines += ["", tags]
    return "\n".join(lines)[:1000]

def build_keyboard(p):
    rows = [[{"text": "مشاهده در سایت", "url": p["url"]}]]
    if ADMIN_USERNAME:
        rows.append([{"text": "ارسال پیام", "url": f"https://ble.ir/{ADMIN_USERNAME}"}])
    return {"inline_keyboard": rows}

# ------------------------- ارسال به بله -------------------------
def prepare_image(url):
    """تصویر را دانلود و به JPEG سبک تبدیل می‌کند (webp و حجم بالا مشکل‌ساز است)."""
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return r.content

def bale_send_post(p, caption):
    kb = json.dumps(build_keyboard(p), ensure_ascii=False)
    try:
        img = prepare_image(p["image_url"])
        r = requests.post(f"{BALE_API}/sendPhoto",
                          data={"chat_id": CHANNEL_ID, "caption": caption, "reply_markup": kb},
                          files={"photo": ("product.jpg", img, "image/jpeg")}, timeout=90)
        j = r.json()
        if j.get("ok"):
            return True
        log.warning("sendPhoto خطا: %s", j)
    except Exception as e:
        log.warning("sendPhoto exception: %s", e)
    # فالبک: متن + لینک (اگر دکمه یا عکس مشکل داشت)
    text = f"{caption}\n\n🔗 {p['url']}"
    r = requests.post(f"{BALE_API}/sendMessage", json={"chat_id": CHANNEL_ID, "text": text}, timeout=30)
    ok = r.json().get("ok", False)
    if not ok:
        log.error("sendMessage هم خطا داد: %s", r.text[:300])
    return ok

def bale_msg(chat_id, text):
    try:
        requests.post(f"{BALE_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=20)
    except Exception as e:
        log.warning("bale_msg: %s", e)

# ------------------------- ارسال یک اسلات -------------------------
def do_post(slot_key):
    """اسلات را در Supabase قفل می‌کند (جلوگیری از پست دوبل) و یک محصول می‌فرستد."""
    r = sb("POST", "post_slots", None, [{"slot": slot_key}], "return=minimal")
    if r.status_code == 409 or (not r.ok and "duplicate" in r.text.lower()):
        return "قبلاً انجام شده"
    if not r.ok:
        log.error("قفل اسلات ناموفق: %s", r.text[:200])
        return "خطای دیتابیس"
    p = pick_product()
    if not p:
        return "محصول آماده‌ای نیست"
    if bale_send_post(p, build_caption(p)):
        sb("PATCH", "products", {"id": f"eq.{p['id']}"},
           {"posted_count": p["posted_count"] + 1, "last_posted_at": datetime.utcnow().isoformat() + "Z"})
        sb("PATCH", "post_slots", {"slot": f"eq.{slot_key}"}, {"product_id": p["id"]})
        return f"ارسال شد: {p['title']}"
    sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})   # اسلات آزاد شود تا دوباره تلاش شود
    return "ارسال ناموفق"

# ------------------------- زمان‌بند (ترد پس‌زمینه) -------------------------
def scheduler_loop():
    time.sleep(5)
    while True:
        try:
            now = datetime.now(TZ)
            for t in POST_TIMES:
                hh, mm = map(int, t.split(":"))
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due <= now <= due + timedelta(minutes=CATCHUP_MINUTES):
                    res = do_post(f"{now:%Y-%m-%d}_{t}")
                    if res != "قبلاً انجام شده":
                        log.info("اسلات %s: %s", t, res)
        except Exception as e:
            log.exception("scheduler: %s", e)
        time.sleep(60)

# ------------------------- پنل ادمین در بله -------------------------
MENU = {"inline_keyboard": [
    [{"text": "➕ افزودن محصول (با لینک)", "callback_data": "add"}],
    [{"text": "📥 ورود فایل CSV محصولات", "callback_data": "csv"}],
    [{"text": "📊 وضعیت چرخه", "callback_data": "status"}, {"text": "📤 ارسال فوری", "callback_data": "post"}],
]}
_waiting = {}   # user_id -> "add" | "csv"

def status_text():
    items = sb("GET", "products", {"select": "posted_count", "active": "eq.true"}).json()
    if not items:
        return "هنوز محصولی در دیتابیس نیست. فایل CSV را وارد کنید یا لینک محصول بفرستید."
    mn = min(i["posted_count"] for i in items)
    left = sum(1 for i in items if i["posted_count"] == mn)
    return (f"محصولات فعال: {len(items)}\nدور فعلی چرخه: {mn + 1}\n"
            f"باقی‌مانده تا پایان این دور: {left}\nساعت‌های پست: {', '.join(POST_TIMES)}")

def bale_menu(chat, text="چه کاری انجام بدم؟"):
    try:
        requests.post(f"{BALE_API}/sendMessage",
                      json={"chat_id": chat, "text": text, "reply_markup": MENU}, timeout=20)
    except Exception as e:
        log.warning("menu: %s", e)

def add_links(chat, text):
    links = re.findall(r"https?://[^\s]+", text)[:20]
    if not links:
        bale_msg(chat, "لینکی پیدا نشد. لینک کامل محصول را بفرستید.")
        return
    for link in links:
        try:
            row = fetch_by_link(link)
            upsert_rows([row])
            warn = "" if row.get("image_url") else "\n⚠️ عکس ندارد و پست نمی‌شود تا عکس اضافه کنید."
            state = "" if row["active"] else "\n⚠️ در سایت ناموجود است و غیرفعال ثبت شد."
            bale_msg(chat, f"✅ اضافه شد: {row['title']}{warn}{state}")
        except Exception as e:
            bale_msg(chat, f"❌ ناموفق: {link}\n{e}")
        time.sleep(1)

def set_active(chat, text, flag):
    links = re.findall(r"https?://[^\s]+", text)
    if not links:
        bale_msg(chat, "لینک محصول را کنار دستور بنویسید.")
        return
    row = fetch_by_link(links[0])
    sb("PATCH", "products", {"id": f"eq.{row['id']}"}, {"active": flag})
    bale_msg(chat, f"{'✅ فعال شد' if flag else '⏸️ از چرخه خارج شد'}: {row['title']}")

def handle_csv(chat, doc):
    try:
        fi = requests.get(f"{BALE_API}/getFile", params={"file_id": doc["file_id"]}, timeout=30).json()
        path = fi["result"]["file_path"]
        data = requests.get(f"https://tapi.bale.ai/file/bot{BALE_TOKEN}/{path}", timeout=60).content
        n, skipped, no_img = import_csv(data)
        msg = f"✅ {n} محصول وارد/به‌روز شد. ({skipped} ردیف نادیده گرفته شد)"
        if no_img:
            msg += f"\n⚠️ {no_img} محصول بدون عکس‌اند و پست نمی‌شوند."
        bale_msg(chat, msg)
    except Exception as e:
        bale_msg(chat, f"❌ خطا در ورود فایل: {e}")

def handle_callback(cb):
    uid, data = cb["from"]["id"], cb.get("data")
    chat = cb["message"]["chat"]["id"]
    requests.post(f"{BALE_API}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=15)
    if uid not in ADMIN_USER_IDS:
        return
    if data == "add":
        _waiting[uid] = "add"
        bale_msg(chat, "لینک محصول را بفرستید (می‌توانید چند لینک را زیر هم بفرستید).")
    elif data == "csv":
        _waiting[uid] = "csv"
        bale_msg(chat, "فایل CSV خروجی محصولات را به‌صورت فایل (Document) بفرستید.")
    elif data == "status":
        bale_msg(chat, status_text())
    elif data == "post":
        bale_msg(chat, do_post(f"manual_{int(time.time())}"))

def handle_message(m):
    uid = (m.get("from") or {}).get("id")
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    if text.split(" ")[0].lower() == "/myid":
        bale_msg(chat, f"شناسه‌ی عددی شما: {uid}")
        return
    if uid not in ADMIN_USER_IDS:
        return
    if m.get("document"):
        if _waiting.pop(uid, None) == "csv" or (m["document"].get("file_name") or "").lower().endswith(".csv"):
            handle_csv(chat, m["document"])
        return
    cmd = text.split(" ")[0].lower()
    if cmd in ("/start", "/menu", "/help"):
        bale_menu(chat)
    elif cmd == "/status":
        bale_msg(chat, status_text())
    elif cmd == "/post":
        bale_msg(chat, do_post(f"manual_{int(time.time())}"))
    elif cmd in ("/off", "/on"):
        try:
            set_active(chat, text, cmd == "/on")
        except Exception as e:
            bale_msg(chat, f"❌ {e}")
    elif _waiting.get(uid) == "add" or SITE_URL.split("//")[-1] in text:
        _waiting.pop(uid, None)
        add_links(chat, text)

def poll_loop():
    try:
        requests.post(f"{BALE_API}/deleteWebhook", timeout=15)
    except Exception:
        pass
    offset = 0
    while True:
        try:
            r = requests.get(f"{BALE_API}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=40)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                try:
                    if u.get("callback_query"):
                        handle_callback(u["callback_query"])
                    elif u.get("message"):
                        handle_message(u["message"])
                except Exception as e:
                    log.warning("handler: %s", e)
        except Exception as e:
            log.warning("poll: %s", e)
            time.sleep(5)

# ------------------------- وب‌سرور (برای UptimeRobot) -------------------------
app = Flask(__name__)

@app.get("/")
def home():
    return "ok"

@app.get("/health")
def health():
    return jsonify(status="up", time=datetime.now(TZ).isoformat())

_started = False
def start_background():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()

start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
