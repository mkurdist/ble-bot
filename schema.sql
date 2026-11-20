-- این کد را یک‌بار در Supabase > SQL Editor اجرا کنید
create table if not exists products (
  id bigint primary key,
  title text not null,
  url text not null,
  image_url text,
  price text,
  short_desc text,
  description text,
  categories text[] default '{}',
  sort_order int default 0,
  active boolean default true,
  posted_count int default 0,
  last_posted_at timestamptz,
  updated_at timestamptz default now()
);

create table if not exists post_slots (
  slot text primary key,
  product_id bigint,
  created_at timestamptz default now()
);

create table if not exists kv (
  k text primary key,
  v text
);

-- فقط سرور (service_role) دسترسی دارد
alter table products enable row level security;
alter table post_slots enable row level security;
alter table kv enable row level security;
