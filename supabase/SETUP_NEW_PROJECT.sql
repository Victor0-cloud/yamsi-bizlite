-- Draft migration for a NEW Supabase project. Review before applying.
-- No business amounts or operational balances are seeded.
begin;
create table public.biz_tenants (
 id uuid primary key default gen_random_uuid(), name text not null
);
create table public.biz_businesses (
 tenant_id uuid not null references public.biz_tenants(id),
 id text not null, name text not null,
 business_type text not null check (business_type in ('water_factory','poultry_farm','retail_store')),
 primary key (tenant_id,id)
);
create table public.biz_branches (
 tenant_id uuid not null, business_id text not null, id text not null,
 name text not null, timezone text not null default 'Africa/Lagos',
 primary key(tenant_id,business_id,id),
 foreign key(tenant_id,business_id) references public.biz_businesses(tenant_id,id)
);
create table public.biz_memberships (
 tenant_id uuid not null, business_id text not null, branch_id text not null,
 user_id uuid not null references auth.users(id),
 role text not null check(role in ('owner','admin','manager','worker','driver')),
 primary key(tenant_id,business_id,branch_id,user_id),
 foreign key(tenant_id,business_id,branch_id) references public.biz_branches(tenant_id,business_id,id)
);
create table public.biz_employees (
 tenant_id uuid not null references public.biz_tenants(id), id uuid not null default gen_random_uuid(),
 display_name text not null, active boolean not null default true,
 primary key(tenant_id,id)
);
create table public.biz_assignments (
 tenant_id uuid not null, employee_id uuid not null,
 business_id text not null, branch_id text not null, role text not null,
 primary key(tenant_id,employee_id,business_id,branch_id,role),
 foreign key(tenant_id,employee_id) references public.biz_employees(tenant_id,id),
 foreign key(tenant_id,business_id,branch_id) references public.biz_branches(tenant_id,business_id,id)
);
-- Compensation belongs to an employee once; allocations are separate.
create table public.biz_compensation (
 tenant_id uuid not null, id uuid not null default gen_random_uuid(),
 employee_id uuid not null, effective_from date not null,
 kind text not null check(kind in ('monthly','per_good_bag','per_collected_bag')),
 amount_kobo bigint not null check(amount_kobo>=0),
 primary key(tenant_id,id),
 unique(tenant_id,employee_id,kind,effective_from),
 foreign key(tenant_id,employee_id) references public.biz_employees(tenant_id,id)
);
create table public.biz_setting_versions (
 tenant_id uuid not null, business_id text not null, branch_id text not null,
 key text not null, effective_from timestamptz not null, value jsonb not null,
 recorded_at timestamptz not null default now(),
 primary key(tenant_id,business_id,branch_id,key,effective_from),
 foreign key(tenant_id,business_id,branch_id) references public.biz_branches(tenant_id,business_id,id)
);
create table public.biz_message_inbox (
 id uuid primary key default gen_random_uuid(),
 provider text not null check(provider in ('whatsapp','telegram')),
 provider_account text not null, provider_event_id text not null,
 received_at timestamptz not null default now(),
 status text not null default 'received' check(status in ('received','processing','processed','failed')),
 payload jsonb not null,
 unique(provider,provider_account,provider_event_id)
);
-- Confirmed messages remain submissions, not ledger postings.
create table public.biz_submissions (
 tenant_id uuid not null, business_id text not null, branch_id text not null,
 id uuid not null default gen_random_uuid(), employee_id uuid not null,
 inbox_id uuid references public.biz_message_inbox(id),
 idempotency_key text not null, kind text not null, payload jsonb not null,
 status text not null default 'draft' check(status in ('draft','confirmed','rejected')),
 created_at timestamptz not null default now(),
 primary key(tenant_id,business_id,branch_id,id),
 unique(tenant_id,business_id,branch_id,idempotency_key),
 foreign key(tenant_id,employee_id) references public.biz_employees(tenant_id,id),
 foreign key(tenant_id,business_id,branch_id) references public.biz_branches(tenant_id,business_id,id)
);
create index biz_membership_user on public.biz_memberships(user_id);
create index biz_submissions_review on public.biz_submissions(tenant_id,business_id,branch_id,status,created_at);
-- Fail closed: clients cannot mutate foundation tables directly.
alter table public.biz_tenants enable row level security;
alter table public.biz_businesses enable row level security;
alter table public.biz_branches enable row level security;
alter table public.biz_memberships enable row level security;
alter table public.biz_employees enable row level security;
alter table public.biz_assignments enable row level security;
alter table public.biz_compensation enable row level security;
alter table public.biz_setting_versions enable row level security;
alter table public.biz_message_inbox enable row level security;
alter table public.biz_submissions enable row level security;
revoke all on public.biz_tenants, public.biz_businesses, public.biz_branches,
 public.biz_memberships, public.biz_employees, public.biz_assignments,
 public.biz_compensation, public.biz_setting_versions, public.biz_message_inbox,
 public.biz_submissions from anon, authenticated;
grant select on public.biz_memberships, public.biz_businesses, public.biz_branches to authenticated;
create policy own_membership on public.biz_memberships for select to authenticated using(user_id=auth.uid());
create policy member_business on public.biz_businesses for select to authenticated using(
 exists(select 1 from public.biz_memberships m where m.user_id=auth.uid() and m.tenant_id=biz_businesses.tenant_id and m.business_id=biz_businesses.id));
create policy member_branch on public.biz_branches for select to authenticated using(
 exists(select 1 from public.biz_memberships m where m.user_id=auth.uid() and m.tenant_id=biz_branches.tenant_id and m.business_id=biz_branches.business_id and m.branch_id=biz_branches.id));

insert into public.biz_tenants(id,name) values ('00000000-0000-0000-0000-000000000001','YAMSI BizLite');
insert into public.biz_businesses(tenant_id,id,name,business_type) values
('00000000-0000-0000-0000-000000000001','amose_table_water','AMOSE Table Water','water_factory'),
('00000000-0000-0000-0000-000000000001','nughe_farms','Nughe Farms','poultry_farm'),
('00000000-0000-0000-0000-000000000001','phone_center','Phone Center','retail_store');
insert into public.biz_branches(tenant_id,business_id,id,name) values
('00000000-0000-0000-0000-000000000001','amose_table_water','asaba','Asaba'),
('00000000-0000-0000-0000-000000000001','amose_table_water','warri','Warri'),
('00000000-0000-0000-0000-000000000001','nughe_farms','warri','Warri'),
('00000000-0000-0000-0000-000000000001','phone_center','warri','Warri');
-- No salaries, opening balances or transactions are inserted without effective dates.
commit;
select b.name as business, r.name as branch
from public.biz_businesses b join public.biz_branches r
on r.tenant_id=b.tenant_id and r.business_id=b.id order by b.name,r.name;
