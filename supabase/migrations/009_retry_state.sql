-- Stage 007C: retry/dead-letter tracking for inbox processing, outbound
-- WhatsApp delivery, and media retrieval failures.
--
-- One generic table, not three ad hoc ones -- each subject type is a typed,
-- FK-enforced, mutually-exclusive reference (same pattern already used by
-- biz_evidence and biz_reminders). This is distinct from biz_reminders:
-- biz_reminders is a business-facing nudge to a HUMAN ("send the photo");
-- biz_retry_state is an infrastructure retry for a TECHNICAL failure
-- (a Supabase write, a WhatsApp API call, a media download). They track
-- different things and are not overlapping concepts.
--
-- Schema only. No retry cadence VALUES are seeded -- max_attempts and
-- next_attempt_at are only ever populated by application code that first
-- confirms an owner-configured public.biz_setting_versions policy exists
-- (retry_engine.get_retry_policy); with no configured policy, a row is
-- still created (so the failure is tracked) but stays state='pending' with
-- no schedule -- automatic retry scheduling stays disabled.
begin;

create table public.biz_retry_state (
 tenant_id uuid not null references public.biz_tenants(id),
 id uuid primary key default gen_random_uuid(),
 subject_type text not null check(subject_type in ('inbox_processing','outbound_message','media_retrieval')),
 inbox_id uuid references public.biz_message_inbox(id),
 outbound_message_id uuid references public.biz_outbound_messages(id),
 evidence_id uuid references public.biz_evidence(id),
 attempt_count integer not null default 0,
 last_attempt_at timestamptz,
 next_attempt_at timestamptz,
 max_attempts integer,
 state text not null default 'pending'
   check(state in ('pending','scheduled','exhausted','dead_letter','resolved')),
 last_error text,
 created_at timestamptz not null default now(),
 unique(inbox_id),
 unique(outbound_message_id),
 unique(evidence_id),
 check (
   (subject_type = 'inbox_processing' and inbox_id is not null and outbound_message_id is null and evidence_id is null)
   or (subject_type = 'outbound_message' and outbound_message_id is not null and inbox_id is null and evidence_id is null)
   or (subject_type = 'media_retrieval' and evidence_id is not null and inbox_id is null and outbound_message_id is null)
 )
);

create index biz_retry_state_next_attempt on public.biz_retry_state(next_attempt_at) where state = 'scheduled';

alter table public.biz_retry_state enable row level security;
revoke all on public.biz_retry_state from anon, authenticated;
grant select, insert, update on public.biz_retry_state to service_role;

commit;
