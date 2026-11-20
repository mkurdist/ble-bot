"""
خواندن فایل CSV خروجی ووکامرس (محصولات > خروجی گرفتن).
فقط پردازش فایل است؛ هیچ درخواستی به سایت نمی‌زند.
"""
import csv
import io
import re
from datetime import datetime, timezone
from html import unescape


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _norm(h):
    """نام ستون را برای مقایسه یکدست می‌کند (حروف کوچک، بدون فاصله/نیم‌فاصله/علامت سؤال)."""
    h = (h or "").replace("﻿", "").lower()
    h = h.replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"[\s‌\?؟:_\-]+", "", h)


# نام ستون‌ها در خروجی انگلیسی و فارسی ووکامرس
ALIASES = {
    "id": ["id", "شناسه", "شناسهمحصول", "productid"],
    "type": ["type", "نوع", "نوعمحصول"],
    "name": ["name", "نام", "ناممحصول", "عنوان", "title"],
    "published": ["published", "منتشرشده", "منتشر", "وضعیت", "status"],
    "short": ["shortdescription", "توضیحکوتاه", "توضیحاتکوتاه", "خلاصه"],
    "desc": ["description", "توضیحات", "توضیح"],
    "instock": ["instock", "موجود", "موجودیدرانبار", "وضعیتموجودی"],
    "stock": ["stock", "موجودی", "تعدادموجودی"],
    "sale": ["saleprice", "قیمتحراج", "قیمتفروشویژه", "قیمتتخفیفی"],
    "regular": ["regularprice", "قیمتعادی", "قیمت", "price"],
    "cats": ["categories", "دستهها", "دستهبندیها", "دستهبندی", "دسته"],
    "images": ["images", "تصاویر", "تصویر", "عکسها", "عکس"],
    "link": ["permalink", "link", "url", "لینک", "پیوندیکتا", "آدرس"],
}


def _decode(data):
    for enc in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes", "بله", "instock", "موجود", "publish", "published", "منتشرشده")


def _price(v, unit):
    v = (v or "").strip().replace(",", "").replace("٬", "")
    try:
        n = int(float(v))
        return f"{n:,} {unit}" if n > 0 else None
    except Exception:
        return None


def _categories(v):
    out = []
    for part in (v or "").split(","):
        last = part.split(">")[-1].strip()
        last = strip_html(last)
        if last and last not in out:
            out.append(last)
    return out[:3]


def _first_image(v):
    for part in re.split(r"[,\n]\s*(?=https?://)", (v or "").strip()):
        part = part.strip().split(" ! ")[0].strip()
        if part.startswith("http"):
            return part
    return None


def parse_products_csv(data, site_url, price_unit="تومان"):
    """
    خروجی: (rows, skipped)
      rows: لیست محصولات آماده‌ی ذخیره (هرکدام با فیلد active)
      skipped: لیست پیش‌نویس/ناقص که نباید وارد چرخه شوند (فقط id) برای غیرفعال‌سازی
    """
    text = _decode(data)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    header = next(reader, None)
    if not header:
        raise ValueError("فایل خالی است")

    idx = {}
    normed = [_norm(h) for h in header]
    for key, names in ALIASES.items():
        for n in names:
            if n in normed:
                idx[key] = normed.index(n)
                break
    if "id" not in idx or "name" not in idx:
        raise ValueError("ستون «ID» و «Name» (شناسه و نام) در فایل پیدا نشد. ستون‌های فایل: " + " | ".join(header[:12]))

    def get(row, key):
        i = idx.get(key)
        return row[i] if i is not None and i < len(row) else ""

    rows, inactive_ids, now = [], [], datetime.now(timezone.utc).isoformat()
    order = 0
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        pid_raw = get(row, "id").strip()
        if not pid_raw.isdigit():
            continue
        pid = int(pid_raw)
        if get(row, "type").strip().lower() in ("variation", "تنوع"):
            continue                                   # تنوع‌ها محصول مستقل نیستند
        order += 1
        if "published" in idx and get(row, "published").strip() not in ("1", "true", "منتشر", "منتشرشده"):
            inactive_ids.append(pid)                   # پیش‌نویس / خصوصی
            continue
        if "instock" in idx:
            instock = _truthy(get(row, "instock"))
        else:
            instock = True
        link = get(row, "link").strip() or f"{site_url}/?p={pid}"
        price = _price(get(row, "sale"), price_unit) or _price(get(row, "regular"), price_unit)
        rows.append({
            "id": pid,
            "title": strip_html(get(row, "name")),
            "url": link,
            "image_url": _first_image(get(row, "images")),
            "price": price,
            "short_desc": strip_html(get(row, "short"))[:600],
            "description": strip_html(get(row, "desc"))[:900],
            "categories": _categories(get(row, "cats")),
            "sort_order": order,
            "active": instock,
            "updated_at": now,
        })
    return rows, inactive_ids
