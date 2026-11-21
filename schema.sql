-- ربات بله از جدول موجود products استفاده می‌کند. فقط سه ستون به آن اضافه می‌شود (بدون تغییر دادهٔ فعلی)
alter table products add column if not exists posted_count int not null default 0;
alter table products add column if not exists last_posted_at timestamptz;
alter table products add column if not exists active boolean not null default true;

-- جدول‌های مخصوص ربات بله (اگر قبلاً ساخته‌اید، اجرای دوباره مشکلی ندارد)
create table if not exists post_slots (
  slot text primary key,
  product_id bigint,
  created_at timestamptz default now()
);
create table if not exists kv (k text primary key, v text);
alter table post_slots enable row level security;
alter table kv enable row level security;
