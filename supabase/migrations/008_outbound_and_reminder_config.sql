-- Stage 007B: automatic-processing error tracking, outbound message
-- auditability, and removing fabricated reminder/escalation defaults from
-- the schema's own constraints.
--
-- Additive/relaxing only. No data rows are seeded, no business-policy
-- VALUES (reminder cadence, escalation hours) are inserted -- those must
-- come from an owner-confirmed public.biz_setting_versions row, added in a
-- future migration once the owner supplies real numbers.
begin;

-- Per-row processing error, distinct from the coarse 'failed' status --
-- lets automatic processing explain *why* a row failed without guessing.
alter table public.biz_message_inbox add column if not exists processing_error text;

-- biz_reminders.max_attempts previously defaulted to 3 -- an implicit,
-- unconfirmed business policy baked into the schema. Reminders created
-- without an owner-confirmed policy must be able to say "not configured"
-- rather than silently getting a fabricated cadence.
alter table public.biz_reminders alter column max_attempts drop default;
alter table public.biz_reminders alter column max_attempts drop not null;

create table public.biz_outbound_messages (
 tenant_id uuid not null references public.biz_tenants(id),
 id uuid primary key default gen_random_uuid(),
 business_id text not null,
 branch_id text not null,
 recipient_employee_id uuid not null,
 provider text not null check(provider in ('whatsapp','telegram')),
 provider_sender text,
 -- Stage 007C correction: the business's own WhatsApp phone_number_id,
 -- snapshotted at queue time (reused from the triggering inbox row's
 -- provider_account) so the dispatch worker never needs to re-derive it
 -- later via a task/inbox lookup, and so it stays correct even if the
 -- business's active number changes between queueing and dispatch.
 provider_account text,
 related_task_id uuid not null references public.biz_tasks(id),
 message_type text not null,
 message_text text not null,
 status text not null default 'queued'
   -- 'sending' is a short-lived claim state: the dispatch worker sets it
   -- via a conditional UPDATE ... WHERE status='queued' so two concurrent
   -- workers can never both send the same message.
   check(status in ('queued','sending','sent','delivered','failed','skipped_no_identity')),
 idempotency_key text not null,
 queued_at timestamptz not null default now(),
 sent_at timestamptz,
 provider_message_id text,
 failure_reason text,
 foreign key(tenant_id,recipient_employee_id) references public.biz_employees(tenant_id,id),
 foreign key(tenant_id,business_id,branch_id) references public.biz_branches(tenant_id,business_id,id),
 unique(idempotency_key)
);

create index biz_outbound_messages_status on public.biz_outbound_messages(status);

alter table public.biz_outbound_messages enable row level security;
revoke all on public.biz_outbound_messages from anon, authenticated;
grant select, insert, update on public.biz_outbound_messages to service_role;

commit;
