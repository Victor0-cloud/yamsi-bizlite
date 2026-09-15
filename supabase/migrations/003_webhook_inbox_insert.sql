-- Private backend only. Raw events cannot become accounting transactions here.
begin;
grant usage on schema public to service_role;
grant insert, select on public.biz_message_inbox to service_role;
commit;
