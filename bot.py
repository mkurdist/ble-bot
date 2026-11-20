"""
ربات بله - پست‌گذاری خودکار محصولات فروشگاه در کانال
- منبع محصولات: فایل CSV خروجی پنل ووکامرس (ارسال به ربات در بله) + افزودن تکی با لینک
- هیچ سینک خودکاری با سایت وجود ندارد. تنها درخواست به سایت: وقتی ادمین «لینک محصول» می‌دهد (۱ درخواست).
- ذخیره‌سازی: Supabase (چرخه‌ی پست، جلوگیری از تکرار، اسلات‌ها)
- متن پست: Gemini با چرخش بین چند کلید
"""
import hashlib
import io
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

from csv_import import parse_products_csv, strip_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


# ------------------------- تنظیمات (Environment Variables رندر) -------------------------
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"متغیر محیطی {name} تنظیم نشده است")
    return v


BOT_TOKEN = env("BOT_TOKEN", required=True)
CHANNEL_ID = env("CHANNEL_ID", required=True)          # مثل @mychannel
SB_URL = env("SUPABASE_URL", required=True).rstrip("/")
SB_KEY = env("SUPABASE_KEY", required=True)
# چند کلید را با کاما (یا خط جدید) جدا کنید
GEMINI_KEYS = [k.strip() for k in re.split(r"[,\s]+", env("GEMINI_API_KEY", "")) if k.strip()]
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MAX_ATTEMPTS = int(env("GEMINI_MAX_ATTEMPTS", str(max(len(GEMINI_KEYS), 1))) or 1)
SITE_URL = env("SITE_URL", "https://citysofal.ir").rstrip("/")
SITE_HOST = urlparse(SITE_URL).netloc.lower().removeprefix("www.")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", env("ADMIN_ID", "")) if x.strip().isdigit()}
ADMIN_USERNAME = env("ADMIN_USERNAME", "").lstrip("@")   # آیدی بله برای چاپ در پست (اختیاری)
ADMIN_PHONE = env("ADMIN_PHONE", "")
POST_TIMES = [t.strip() for t in env("POST_TIMES", "10:00,13:00,16:00,19:00,21:00").split(",") if t.strip()]
ORDER_MODE = env("ORDER_MODE", "random")               # random | sequential
SHOW_PRICE = env("SHOW_PRICE", "false").lower() == "true"
PRICE_UNIT = env("PRICE_UNIT", "تومان")
CATCHUP_MINUTES = int(env("CATCHUP_MINUTES", "180"))   # اگر سرور خواب بود تا چند دقیقه دیرتر هم پست بزند
TZ = ZoneInfo(env("TZ_NAME", "Asia/Tehran"))
BALE_API = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
BALE_FILE = f"https://tapi.bale.ai/file/bot{BOT_TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; CitySofalBot/2.0)"}

STYLE_HINTS = [
    "لحن گرم و صمیمی، تأکید بر زیبایی و حس خانه",
    "تأکید بر کاربرد و استفاده‌ی روزمره",
    "به‌عنوان یک ایده‌ی هدیه‌ی خاص",
    "تأکید بر دست‌ساز بودن و اصالت (فقط اگر در مشخصات محصول آمده)",
    "تأکید بر چیدمان و دکوراسیون خانه",
    "کوتاه، شاعرانه و دلنشین",
]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


# ------------------------- Supabase (REST ساده) -------------------------
def sb(method, table, params=None, body=None, prefer=None):
    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    return requests.request(method, f"{SB_URL}/rest/v1/{table}", headers=headers,
                            params=params, data=json.dumps(body) if body is not None else None, timeout=30)


def kv_get(k):
    r = sb("GET", "kv", {"k": f"eq.{k}", "select": "v"})
    if r.ok and r.json():
        return r.json()[0]["v"]
    return None


def kv_set(k, v):
    sb("POST", "kv", {"on_conflict": "k"}, [{"k": k, "v": str(v)}], "resolution=merge-duplicates")


def kv_del(k):
    sb("DELETE", "kv", {"k": f"eq.{k}"})


def current_cycle_min():
    """کمترین تعداد پستِ محصولات فعال = دور فعلی چرخه. محصول تازه باید با همین عدد وارد شود."""
    r = sb("GET", "products", {"select": "posted_count", "active": "eq.true"})
    items = r.json() if r.ok else []
    return min((i["posted_count"] for i in items), default=0)


def existing_ids():
    ids, offset = set(), 0
    while True:
        r = sb("GET", "products", {"select": "id", "limit": 1000, "offset": offset})
        chunk = r.json() if r.ok else []
        ids |= {x["id"] for x in chunk}
        if len(chunk) < 1000:
            return ids
        offset += 1000


def save_rows(rows):
    """ذخیره‌ی محصولات: جدیدها با posted_count دور فعلی، موجودها بدون دست‌زدن به شمارنده. خروجی: (جدید، به‌روز)."""
    if not rows:
        return 0, 0
    have = existing_ids()
    base = current_cycle_min()
    new = [dict(r, posted_count=base) for r in rows if r["id"] not in have]
    upd = [r for r in rows if r["id"] in have]
    for batch in (new, upd):
        for i in range(0, len(batch), 100):
            r = sb("POST", "products", {"on_conflict": "id"}, batch[i:i + 100], "resolution=merge-duplicates")
            if not r.ok:
                raise RuntimeError(f"ذخیره در Supabase ناموفق: {r.text[:200]}")
    return len(new), len(upd)


# ------------------------- ایمپورت فایل CSV -------------------------
def import_csv_bytes(data):
    rows, drafts = parse_products_csv(data, SITE_URL, PRICE_UNIT)
    if not rows and not drafts:
        raise ValueError("هیچ محصولی در فایل پیدا نشد")
    new, upd = save_rows(rows)
    if drafts:
        sb("PATCH", "products", {"id": f"in.({','.join(map(str, drafts))})"}, {"active": False})
    off = sum(1 for r in rows if not r["active"])
    noimg = sum(1 for r in rows if r["active"] and not r["image_url"])
    msg = f"✅ فایل وارد شد\nجدید: {new}\nبه‌روزرسانی‌شده: {upd}"
    if off:
        msg += f"\nناموجود (کنار گذاشته شد): {off}"
    if drafts:
        msg += f"\nپیش‌نویس/منتشرنشده: {len(drafts)}"
    if noimg:
        msg += f"\n⚠️ بدون عکس (پست نمی‌شوند): {noimg}"
    return msg


# ------------------------- افزودن تکی با لینک -------------------------
def _is_site_link(u):
    try:
        host = urlparse(u).netloc.lower().removeprefix("www.")
        return host == SITE_HOST
    except Exception:
        return False


def find_links(text):
    return [u.rstrip(").,،") for u in re.findall(r"https?://[^\s]+", text or "") if _is_site_link(u.rstrip(").,،"))]


def row_from_store_api(p):
    imgs = p.get("images") or []
    prices = p.get("prices") or {}
    price = None
    try:
        minor = int(prices.get("currency_minor_unit", 0))
        val = int(prices["price"]) / (10 ** minor)
        if prices.get("currency_code") == "IRR":
            val /= 10
        price = f"{int(val):,} {PRICE_UNIT}" if val > 0 else None
    except Exception:
        pass
    return {
        "id": p["id"],
        "title": strip_html(p.get("name")),
        "url": p.get("permalink"),
        "image_url": imgs[0]["src"] if imgs else None,
        "price": price,
        "short_desc": strip_html(p.get("short_description"))[:600],
        "description": strip_html(p.get("description"))[:900],
        "categories": [strip_html(c["name"]) for c in (p.get("categories") or [])][:3],
        "active": bool(p.get("is_in_stock", True)),
        "updated_at": utcnow(),
    }


def _meta(html, prop):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)["\']' % re.escape(prop), html, re.I) \
        or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop), html, re.I)
    return strip_html(m.group(1)) if m else None


def row_from_html(url, html):
    """فالبک: اگر API ووکامرس جواب نداد، از متاتگ‌های صفحه‌ی همان یک محصول می‌خوانیم."""
    title = _meta(html, "og:title")
    if not title:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        title = strip_html(m.group(1)) if m else None
    if not title:
        raise RuntimeError("عنوان محصول از صفحه خوانده نشد")
    title = re.split(r"\s+[-–|–]\s+", title)[0].strip() or title
    m = re.search(r"postid-(\d+)", html) or re.search(r"[?&]p=(\d+)", html)
    pid = int(m.group(1)) if m else -int(hashlib.sha1(url.encode()).hexdigest()[:12], 16)
    cats = [strip_html(c) for c in re.findall(r'rel=["\']tag["\'][^>]*>([^<]+)</a>', html)][:3]
    return {
        "id": pid,
        "title": title,
        "url": _meta(html, "og:url") or url,
        "image_url": _meta(html, "og:image"),
        "price": None,
        "short_desc": (_meta(html, "og:description") or "")[:600],
        "description": "",
        "categories": cats,
        "active": True,
        "updated_at": utcnow(),
    }


def fetch_product_from_link(link):
    """یک درخواست سبک به سایت برای همان یک محصول (فقط وقتی ادمین لینک می‌دهد)."""
    u = urlparse(link)
    q = parse_qs(u.query)
    base = f"{SITE_URL}/wp-json/wc/store/v1/products"
    attempts = []
    if "p" in q and q["p"][0].isdigit():
        attempts.append({"include": q["p"][0]})
    else:
        seg = [s for s in u.path.split("/") if s]
        if seg:
            slug_raw = seg[-1]
            attempts.append(f"slug={slug_raw.lower()}")           # شکل انکودشده (ذخیره‌ی وردپرس)
            attempts.append({"slug": unquote(slug_raw)})            # شکل فارسی
    for params in attempts:
        try:
            r = requests.get(base, params=params, headers=UA, timeout=30)
            if r.status_code == 200 and r.json():
                return row_from_store_api(r.json()[0])
        except Exception as e:
            log.warning("store api: %s", e)
    r = requests.get(link, headers=UA, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"صفحه باز نشد (HTTP {r.status_code})")
    return row_from_html(link, r.text)


def add_from_links(links):
    out = []
    for link in links[:10]:
        try:
            row = fetch_product_from_link(link)
            new, _ = save_rows([row])
            note = "" if row.get("image_url") else " ⚠️ بدون عکس (پست نمی‌شود)"
            out.append(f"{'➕ اضافه شد' if new else '🔄 از قبل بود، به‌روز شد'}: {row['title']}{note}")
        except Exception as e:
            out.append(f"❌ {link}\n   {e}")
        time.sleep(1)
    return "\n".join(out)


def remove_by_input(text):
    """حذف (غیرفعال‌سازی) با لینک، شناسه‌ی عددی یا بخشی از نام."""
    text = text.strip()
    links = find_links(text)
    if links:
        try:
            pid = fetch_product_from_link(links[0])["id"]
        except Exception as e:
            return f"❌ {e}"
    elif text.isdigit():
        pid = int(text)
    else:
        r = sb("GET", "products", {"select": "id,title", "title": f"ilike.*{text}*", "active": "eq.true", "limit": 6})
        found = r.json() if r.ok else []
        if not found:
            return "محصولی با این نام پیدا نشد."
        if len(found) > 1:
            return "چند محصول پیدا شد، شناسه‌ی عددی یکی را بفرستید:\n" + "\n".join(f"{x['id']} — {x['title']}" for x in found)
        pid = found[0]["id"]
    r = sb("PATCH", "products", {"id": f"eq.{pid}"}, {"active": False}, "return=representation")
    done = r.json() if r.ok else []
    return f"🗑 از چرخه خارج شد: {done[0]['title']}" if done else "این محصول در دیتابیس نبود."


# ------------------------- انتخاب محصول بعدی (چرخه) -------------------------
def pick_product():
    r = sb("GET", "products", {"select": "*", "active": "eq.true"})
    items = r.json() if r.ok else []
    items = [p for p in items if p.get("image_url")]
    if not items:
        return None
    min_count = min(p["posted_count"] for p in items)      # فقط از کم‌پستی‌ها => چرخه‌ی بدون تکرار
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
    cfg = {"temperature": 1.0, "maxOutputTokens": 1024}
    if "2.5-flash" in GEMINI_MODEL:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}       # بدون «فکر کردن» تا سهمیه‌ی توکن متن را نخورد
    for attempt in range(max(GEMINI_MAX_ATTEMPTS, 1)):
        key_no = (_key_idx + attempt) % n
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEYS[key_no], "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
                timeout=40)
            if r.status_code == 200:
                cands = r.json().get("candidates") or [{}]
                parts = (cands[0].get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts).strip()
                if txt:
                    _key_idx = (key_no + 1) % n         # کلید بعدی برای نوبت بعد
                    return txt
                log.warning("gemini key#%d -> پاسخ خالی", key_no)
            else:
                log.warning("gemini key#%d -> HTTP %s", key_no, r.status_code)
        except Exception as e:
            log.warning("gemini error: %s", e)
        time.sleep(1)
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


def bale_msg(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        requests.post(f"{BALE_API}/sendMessage", json=payload, timeout=20)
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
        sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})
        return "محصول آماده‌ای نیست"
    if bale_send_post(p, build_caption(p)):
        sb("PATCH", "products", {"id": f"eq.{p['id']}"},
           {"posted_count": p["posted_count"] + 1, "last_posted_at": utcnow()})
        sb("PATCH", "post_slots", {"slot": f"eq.{slot_key}"}, {"product_id": p["id"]})
        return f"ارسال شد: {p['title']}"
    sb("DELETE", "post_slots", {"slot": f"eq.{slot_key}"})   # اسلات آزاد شود تا دوباره تلاش شود
    return "ارسال ناموفق"


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
                    if res != "قبلاً انجام شده":
                        log.info("اسلات %s: %s", t, res)
                    if res in ("ارسال ناموفق", "خطای دیتابیس", "محصول آماده‌ای نیست"):
                        _retry_after[slot] = time.time() + 600      # ۱۰ دقیقه بعد دوباره
        except Exception as e:
            log.exception("scheduler: %s", e)
        time.sleep(60)


# ------------------------- پنل ادمین در بله -------------------------
MENU = {"inline_keyboard": [
    [{"text": "➕ افزودن محصول (با لینک)", "callback_data": "add"}],
    [{"text": "📥 وارد کردن فایل محصولات", "callback_data": "csv"}],
    [{"text": "📤 پست فوری", "callback_data": "post"}, {"text": "📊 وضعیت", "callback_data": "status"}],
    [{"text": "🗑 حذف محصول از چرخه", "callback_data": "remove"}],
]}


def status_text():
    items = sb("GET", "products", {"select": "posted_count,image_url", "active": "eq.true"}).json()
    if not items:
        return "محصولی در دیتابیس نیست. فایل CSV محصولات را برای ربات بفرستید."
    mn = min(i["posted_count"] for i in items)
    left = sum(1 for i in items if i["posted_count"] == mn)
    noimg = sum(1 for i in items if not i.get("image_url"))
    s = (f"📊 محصولات فعال: {len(items)}\nدور فعلی چرخه: {mn + 1}\n"
         f"باقی‌مانده تا پایان این دور: {left}\nساعت‌های پست: {', '.join(POST_TIMES)}")
    if noimg:
        s += f"\n⚠️ بدون عکس: {noimg}"
    return s


def download_bale_file(file_id):
    r = requests.get(f"{BALE_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    if not r.get("ok"):
        raise RuntimeError(f"getFile خطا داد: {r}")
    path = r["result"]["file_path"]
    d = requests.get(f"{BALE_FILE}/{path}", timeout=90)
    d.raise_for_status()
    return d.content


def handle_action(chat, uid, action):
    if action == "add":
        kv_set(f"state_{uid}", "add")
        bale_msg(chat, "🔗 لینک محصول را بفرستید (چند لینک را هم می‌توانید در یک پیام بفرستید).\nبرای لغو: /cancel")
    elif action == "csv":
        bale_msg(chat, "📥 فایل CSV خروجی محصولات ووکامرس را همین‌جا به‌صورت فایل (Document) بفرستید.\n"
                       "محصولات تکراری فقط به‌روز می‌شوند و شمارنده‌ی پستشان صفر نمی‌شود.")
    elif action == "remove":
        kv_set(f"state_{uid}", "remove")
        bale_msg(chat, "🗑 لینک محصول، شناسه‌ی عددی یا بخشی از نام آن را بفرستید.\nبرای لغو: /cancel")
    elif action == "status":
        bale_msg(chat, status_text(), MENU)
    elif action == "post":
        bale_msg(chat, do_post(f"manual_{int(time.time())}"), MENU)
    elif action == "menu":
        bale_msg(chat, "منوی مدیریت:", MENU)


def handle_message(m):
    uid = (m.get("from") or {}).get("id")
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    if text.lower().startswith("/myid"):                   # برای پیدا کردن شناسه‌ی عددی خودتان (مقدار ADMIN_ID)
        bale_msg(chat, f"شناسه‌ی عددی شما: {uid}")
        return
    if uid not in ADMIN_IDS:
        return

    # فایل CSV
    doc = m.get("document")
    if doc:
        name = (doc.get("file_name") or "").lower()
        if not name.endswith(".csv"):
            bale_msg(chat, "فقط فایل CSV پذیرفته می‌شود. در پنل ووکامرس: محصولات > خروجی گرفتن (Export) و فرمت CSV.")
            return
        bale_msg(chat, "⏳ در حال خواندن فایل...")
        try:
            bale_msg(chat, import_csv_bytes(download_bale_file(doc["file_id"])), MENU)
        except Exception as e:
            log.exception("import")
            bale_msg(chat, f"❌ خطا در وارد کردن فایل: {e}")
        return

    if not text:
        return
    cmd = text.split()[0].lower() if text.startswith("/") else None
    if cmd in ("/start", "/help", "/menu"):
        handle_action(chat, uid, "menu")
    elif cmd == "/cancel":
        kv_del(f"state_{uid}")
        bale_msg(chat, "لغو شد.", MENU)
    elif cmd in ("/status", "/post", "/add", "/remove"):
        handle_action(chat, uid, cmd[1:])
    elif cmd:
        handle_action(chat, uid, "menu")
    else:
        state = kv_get(f"state_{uid}")
        links = find_links(text)
        if state == "remove":
            kv_del(f"state_{uid}")
            bale_msg(chat, remove_by_input(text), MENU)
        elif links:                                        # لینک بفرستید، با دکمه یا بدون دکمه، اضافه می‌شود
            kv_del(f"state_{uid}")
            bale_msg(chat, "⏳ در حال افزودن...")
            bale_msg(chat, add_from_links(links), MENU)
        elif state == "add":
            bale_msg(chat, "لینکی از سایت پیدا نشد. لینک کامل محصول را بفرستید یا /cancel بزنید.")
        else:
            handle_action(chat, uid, "menu")


def handle_callback(cb):
    uid = (cb.get("from") or {}).get("id")
    try:
        requests.post(f"{BALE_API}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=10)
    except Exception:
        pass
    if uid not in ADMIN_IDS:
        return
    chat = (cb.get("message") or {}).get("chat", {}).get("id") or uid
    handle_action(chat, uid, cb.get("data", ""))


def poll_loop():
    if not ADMIN_IDS:
        log.warning("ADMIN_ID تنظیم نشده؛ پنل ادمین غیرفعال است (پست‌گذاری زمان‌بندی‌شده کار می‌کند).")
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
                except Exception:
                    log.exception("handler")
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
