-- Branch-clarification outbound support for multi-branch WhatsApp senders.
--
-- message_processor never guesses a branch: when a sender holds several
-- biz_assignments business/branch pairs, a report without a usable explicit
-- "<branch>:" prefix queues exactly one task-less 'branch_clarification'
-- row (idempotent on the inbox event) instead of submitting. Two existing
-- constraints block that row, so this migration widens them minimally:
--   1. The task-less message family gains 'branch_clarification'
--      (related_task_id stays NULL exactly for that family).
--   2. business_id/branch_id become nullable, but ONLY for clarification
--      rows -- a new CHECK pins NULL scope to that family, so every other
--      message keeps full branch scope (there is no branch to record on a
--      clarification by definition).
-- Additive only. Existing rows already satisfy both CHECKs (review rows and
-- task-bound rows all carry scope), and the dispatch worker sends any
-- status='queued' row with a provider_sender, so no worker change is needed.
begin;

alter table public.biz_outbound_messages
  drop constraint biz_outbound_messages_review_task_check;
alter table public.biz_outbound_messages
  add constraint biz_outbound_messages_review_task_check
  check ((message_type in ('review_confirmed', 'review_rejected',
    'review_request', 'branch_clarification'))
    = (related_task_id is null));

alter table public.biz_outbound_messages
  alter column business_id drop not null;
alter table public.biz_outbound_messages
  alter column branch_id drop not null;

alter table public.biz_outbound_messages
  add constraint biz_outbound_messages_clarification_scope_check
  check (
    (
      message_type = 'branch_clarification'
      and business_id is null
      and branch_id is null
    )
    or
    (
      message_type <> 'branch_clarification'
      and business_id is not null
      and branch_id is not null
    )
  );

commit;
