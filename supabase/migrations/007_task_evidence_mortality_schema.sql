-- Stage 007: task engine, evidence tracking, mortality incidents, reminder
-- scheduling state, and a sensitive-action approval gate.
--
-- Schema only. No data rows are inserted. No business-rule VALUES (evidence
-- policy, reminder cadence, escalation thresholds) are seeded here -- those
-- live in application-layer defaults (clearly marked as local placeholders)
-- until the owner supplies real values for a future biz_setting_versions
-- seed migration.
begin;

create table public.biz_tasks (
 tenant_id uuid not null references public.biz_tenants(id),
 id uuid primary key default gen_random_uuid(),
 business_id text not null,
 branch_id text not null,
 assigned_employee_id uuid,
 created_by text not null,
 source text not null check(source in ('inbound_message','rule_engine','owner','manual')),
 related_inbox_id uuid references public.biz_message_inbox(id),
 related_submission_id uuid,
 task_type text not null,
 title text not null,
 instructions text,
 status text not null default 'pending'
   check(status in ('pending','acknowledged','in_progress','completed','overdue','cancelled')),
 priority text not null default 'normal' check(priority in ('low','normal','high','urgent')),
 due_at timestamptz,
 requires_evidence boolean not null default false,
 completed_at timestamptz,
 completed_by uuid,
 -- Stage 007C correction: a generic idempotency key for tasks that have no
 -- related_submission_id to key off (e.g. a "missing daily report"
 -- follow-up, where by definition no submission exists yet). NULL for
 -- every task that already has a natural key via related_submission_id.
 dedupe_key text,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 foreign key(tenant_id,assigned_employee_id) references public.biz_employees(tenant_id,id),
 foreign key(tenant_id,completed_by) references public.biz_employees(tenant_id,id),
 foreign key(tenant_id,business_id,branch_id) references public.biz_branches(tenant_id,business_id,id),
 foreign key(tenant_id,business_id,branch_id,related_submission_id)
   references public.biz_submissions(tenant_id,business_id,branch_id,id),
 unique(related_submission_id, task_type)
);
create unique index biz_tasks_dedupe_key_unique on public.biz_tasks(dedupe_key) where dedupe_key is not null;

create table public.biz_mortality_incidents (
 tenant_id uuid not null references public.biz_tenants(id),
 id uuid primary key default gen_random_uuid(),
 business_id text not null,
 branch_id text not null,
 submission_id uuid not null,
 reported_by uuid not null,
 mortality_count integer not null check(mortality_count > 0),
 occurred_on date,
 suspected_cause text,
 cause_source text check(cause_source in ('staff_reported')),
 follow_up_status text not null default 'pending'
   check(follow_up_status in ('pending','bird_care_reviewed','escalated','resolved')),
 follow_up_task_id uuid references public.biz_tasks(id),
 escalated_to_owner boolean not null default false,
 escalated_at timestamptz,
 notes text,
 created_at timestamptz not null default now(),
 foreign key(tenant_id,business_id,branch_id) references public.biz_branches(tenant_id,business_id,id),
 foreign key(tenant_id,business_id,branch_id,submission_id)
   references public.biz_submissions(tenant_id,business_id,branch_id,id),
 foreign key(tenant_id,reported_by) references public.biz_employees(tenant_id,id),
 unique(submission_id)
);

create table public.biz_evidence (
 tenant_id uuid not null references public.biz_tenants(id),
 id uuid primary key default gen_random_uuid(),
 subject_type text not null check(subject_type in ('submission','task','mortality_incident')),
 -- Stage 007C correction: who this evidence requirement is FOR. Without
 -- this, an incoming image could only be scoped to (tenant,business,branch)
 -- -- too coarse to safely disambiguate when an employee has more than one
 -- open requirement at once (see evidence_store.find_open_requirement).
 employee_id uuid,
 submission_business_id text,
 submission_branch_id text,
 submission_id uuid,
 task_id uuid references public.biz_tasks(id),
 mortality_incident_id uuid references public.biz_mortality_incidents(id),
 provider text not null check(provider in ('whatsapp','telegram')),
 provider_media_id text,
 mime_type text,
 caption text,
 inbox_id uuid references public.biz_message_inbox(id),
 storage_path text,
 status text not null default 'required' check(status in ('required','received','missing','reviewed')),
 captured_at timestamptz,
 reviewed_by uuid,
 reviewed_at timestamptz,
 notes text,
 created_at timestamptz not null default now(),
 foreign key(tenant_id,employee_id) references public.biz_employees(tenant_id,id),
 foreign key(tenant_id,submission_business_id,submission_branch_id,submission_id)
   references public.biz_submissions(tenant_id,business_id,branch_id,id),
 foreign key(tenant_id,reviewed_by) references public.biz_employees(tenant_id,id),
 unique(provider, provider_media_id),
 check (
   (subject_type = 'submission' and submission_id is not null and task_id is null and mortality_incident_id is null)
   or (subject_type = 'task' and task_id is not null and submission_id is null and mortality_incident_id is null)
   or (subject_type = 'mortality_incident' and mortality_incident_id is not null and submission_id is null and task_id is null)
 )
);

create table public.biz_reminders (
 tenant_id uuid not null references public.biz_tenants(id),
 id uuid primary key default gen_random_uuid(),
 subject_type text not null check(subject_type in ('task','evidence')),
 task_id uuid references public.biz_tasks(id),
 evidence_id uuid references public.biz_evidence(id),
 reminder_state text not null default 'scheduled'
   check(reminder_state in ('scheduled','sent','escalated','cancelled','done')),
 attempt_count integer not null default 0,
 max_attempts integer not null default 3,
 last_sent_at timestamptz,
 next_due_at timestamptz,
 created_at timestamptz not null default now(),
 unique(task_id),
 unique(evidence_id),
 check (
   (subject_type = 'task' and task_id is not null and evidence_id is null)
   or (subject_type = 'evidence' and evidence_id is not null and task_id is null)
 )
);

create table public.biz_approval_requests (
 tenant_id uuid not null references public.biz_tenants(id),
 id uuid primary key default gen_random_uuid(),
 action_type text not null check(action_type in (
   'fire_staff','hire_staff','salary_change','disciplinary_action','major_purchase',
   'transfer_money','price_change','compensation_change','loan','payment',
   'ownership_change','other_sensitive')),
 requested_by text not null,
 subject_description text not null,
 payload jsonb not null default '{}'::jsonb,
 status text not null default 'pending' check(status in ('pending','approved','rejected')),
 decided_by uuid,
 decided_at timestamptz,
 decision_notes text,
 created_at timestamptz not null default now(),
 foreign key(tenant_id,decided_by) references public.biz_employees(tenant_id,id)
);

create index biz_tasks_business_status on public.biz_tasks(tenant_id,business_id,branch_id,status);
create index biz_evidence_status on public.biz_evidence(status);
create index biz_mortality_business on public.biz_mortality_incidents(tenant_id,business_id,branch_id);
create index biz_reminders_due on public.biz_reminders(next_due_at) where reminder_state = 'scheduled';

alter table public.biz_tasks enable row level security;
alter table public.biz_mortality_incidents enable row level security;
alter table public.biz_evidence enable row level security;
alter table public.biz_reminders enable row level security;
alter table public.biz_approval_requests enable row level security;
revoke all on public.biz_tasks, public.biz_mortality_incidents, public.biz_evidence,
 public.biz_reminders, public.biz_approval_requests from anon, authenticated;

grant select, insert, update on public.biz_tasks to service_role;
grant select, insert, update on public.biz_mortality_incidents to service_role;
grant select, insert, update on public.biz_evidence to service_role;
grant select, insert, update on public.biz_reminders to service_role;
grant select, insert, update on public.biz_approval_requests to service_role;

commit;
