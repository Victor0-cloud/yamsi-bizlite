-- Stage 2: WhatsApp inbox processing support.
-- Additive only. Does NOT alter biz_submissions primary key, NOT NULL
-- requirements, or any existing business/accounting data.
begin;

-- Maps a messaging-provider sender identity to a known employee.
-- Never guesses: absence of a row here means the sender is unresolved.
create table public.biz_sender_identities (
 tenant_id uuid not null references public.biz_tenants(id),
 provider text not null check(provider in ('whatsapp','telegram')),
 provider_sender text not null,
 employee_id uuid not null,
 created_at timestamptz not null default now(),
 primary key(provider, provider_sender),
 foreign key(tenant_id, employee_id) references public.biz_employees(tenant_id, id)
);
alter table public.biz_sender_identities enable row level security;
revoke all on public.biz_sender_identities from anon, authenticated;

-- Widen biz_message_inbox.status to allow 'unmatched': the webhook event
-- was received and is valid, but sender/business/branch could not be
-- safely resolved. Distinct from 'failed' (a processing error).
do $$
declare
 c text;
begin
 select conname into c
 from pg_constraint
 where conrelid = 'public.biz_message_inbox'::regclass
   and contype = 'c'
   and pg_get_constraintdef(oid) ilike '%status%received%processing%processed%failed%';
 if c is not null then
   execute format('alter table public.biz_message_inbox drop constraint %I', c);
 end if;
end $$;
alter table public.biz_message_inbox
 add constraint biz_message_inbox_status_check
 check (status in ('received','processing','processed','failed','unmatched'));

-- Guarantee the same inbox event can never produce more than one submission,
-- independent of the idempotency_key value chosen by the processor.
alter table public.biz_submissions
 add constraint biz_submissions_inbox_id_unique unique (inbox_id);

-- Grants required by the new internal processing endpoint (service_role
-- only; anon/authenticated privileges are unchanged).
grant select on public.biz_sender_identities to service_role;
grant select on public.biz_employees to service_role;
grant select on public.biz_assignments to service_role;
grant select, insert on public.biz_submissions to service_role;
grant update on public.biz_message_inbox to service_role;

commit;
