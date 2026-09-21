-- Stage 2 fix: one provider account may serve many scopes.
--
-- Forward-only migration (applied migrations are never edited). Schema +
-- code only: no data rows are seeded, deleted, or altered; no
-- credentials or phone numbers are stored here.
--
-- Root cause: biz_provider_accounts carried unique
-- (tenant_id, provider, provider_account), so a shared account such as
-- the Telegram bot could be registered for only one branch. Registering
-- it for a second branch failed with a duplicate-key violation, and
-- without a registry row the submission/outbound guards rejected that
-- branch's rows (fail closed). Routing everywhere else already keys on
-- (business_id, branch_id), so the tenant-wide uniqueness was the only
-- blocker.
--
-- This migration drops only the tenant-wide uniqueness rule. The scoped
-- rule unique (tenant_id, business_id, branch_id, provider,
-- provider_account) -- the same key both provider-account foreign keys
-- reference -- is preserved untouched, so every existing row keeps its
-- exact-scope authorization and isolation is unchanged: a row still
-- authorizes exactly one scope, and cross-scope use is still refused.
-- _amose_provider_tenant_authorized is recreated with existence
-- semantics (EXISTS instead of SELECT ... INTO) so pre-scope checks
-- stay correct when several scopes share one account.
begin;

-- ---------------------------------------------------------------------------
-- 1. Replace tenant-wide uniqueness with scoped uniqueness.
-- ---------------------------------------------------------------------------
alter table public.biz_provider_accounts
  drop constraint if exists
  biz_provider_accounts_tenant_id_provider_provider_account_key;

comment on table public.biz_provider_accounts is
  'Authoritative provider-account registry. Only registered, enabled accounts may send or receive for their scope. One account may serve many scopes; each (tenant, business, branch, provider, account) row authorizes exactly one scope. Disable (never delete) to revoke.';

-- ---------------------------------------------------------------------------
-- 2. Tenant-level authorization helper with existence semantics: the
-- account must be registered and enabled for the tenant in any scope.
-- Used only by the outbound guard for pre-scope rows; never guesses a
-- business or branch. EXISTS (not SELECT ... INTO) so several scopes
-- sharing one account cannot raise a too-many-rows error.
-- ---------------------------------------------------------------------------
create or replace function public._amose_provider_tenant_authorized(
  p_tenant_id uuid,
  p_provider text,
  p_provider_account text
)
returns boolean
language plpgsql
stable
security definer
set search_path = pg_catalog
as $func$
declare
  v_ok boolean := false;
begin
  if p_tenant_id is null
      or nullif(btrim(p_provider), '') is null
      or nullif(btrim(p_provider_account), '') is null then
    return false;
  end if;
  select exists (
    select 1
    from public.biz_provider_accounts a
    where a.tenant_id = p_tenant_id
      and a.provider = p_provider
      and a.provider_account = p_provider_account
      and a.enabled = true)
  into v_ok;
  return coalesce(v_ok, false);
end;
$func$;

revoke execute on function public._amose_provider_tenant_authorized(uuid, text, text)
  from public, anon, authenticated, service_role;

commit;
