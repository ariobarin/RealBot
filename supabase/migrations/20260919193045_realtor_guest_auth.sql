create type public.organization_role as enum ('owner', 'realtor', 'viewer');

create table public.profiles (
  user_id uuid primary key references auth.users (id) on delete cascade,
  display_name text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.organizations (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(trim(name)) between 1 and 160),
  created_at timestamptz not null default now()
);

create table public.organization_members (
  organization_id uuid not null references public.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  role public.organization_role not null,
  created_at timestamptz not null default now(),
  primary key (organization_id, user_id)
);

create index organization_members_user_id_idx
  on public.organization_members (user_id);

create table public.spaces (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  name text not null check (length(trim(name)) between 1 and 200),
  relay_room_id text not null unique check (length(trim(relay_room_id)) between 1 and 200),
  created_by uuid not null references auth.users (id) on delete restrict,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index spaces_organization_id_idx on public.spaces (organization_id);

create schema if not exists private;

revoke all on schema private from public, anon, authenticated;
grant usage on schema private to authenticated, service_role;

create table private.tour_invitations (
  id uuid primary key default gen_random_uuid(),
  space_id uuid not null references public.spaces (id) on delete cascade,
  created_by uuid not null references auth.users (id) on delete restrict,
  token_hash bytea not null unique,
  secret_hash text,
  permissions text[] not null default array['view']::text[],
  expires_at timestamptz not null,
  max_uses integer not null default 1 check (max_uses > 0),
  use_count integer not null default 0 check (use_count >= 0 and use_count <= max_uses),
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  check (
    cardinality(permissions) > 0
    and permissions <@ array['view', 'control']::text[]
  )
);

create index tour_invitations_space_id_idx
  on private.tour_invitations (space_id);
create index tour_invitations_expires_at_idx
  on private.tour_invitations (expires_at);

create table private.guest_sessions (
  id uuid primary key default gen_random_uuid(),
  invitation_id uuid not null references private.tour_invitations (id) on delete cascade,
  session_token_hash bytea not null unique,
  display_name text not null check (length(trim(display_name)) between 1 and 120),
  contact_email text,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create index guest_sessions_invitation_id_idx
  on private.guest_sessions (invitation_id);
create index guest_sessions_expires_at_idx
  on private.guest_sessions (expires_at);

create table private.socket_tickets (
  id uuid primary key default gen_random_uuid(),
  token_hash bytea not null unique,
  principal_type text not null check (principal_type in ('realtor', 'guest')),
  principal_user_id uuid references auth.users (id) on delete cascade,
  guest_session_id uuid references private.guest_sessions (id) on delete cascade,
  space_id uuid not null references public.spaces (id) on delete cascade,
  connection_type text not null check (connection_type in ('control', 'video')),
  permissions text[] not null,
  expires_at timestamptz not null,
  consumed_at timestamptz,
  created_at timestamptz not null default now(),
  check (
    cardinality(permissions) > 0
    and permissions <@ array['view', 'control']::text[]
  ),
  check (
    (principal_type = 'realtor' and principal_user_id is not null and guest_session_id is null)
    or
    (principal_type = 'guest' and principal_user_id is null and guest_session_id is not null)
  )
);

create index socket_tickets_expires_at_idx
  on private.socket_tickets (expires_at);
create index socket_tickets_space_id_idx
  on private.socket_tickets (space_id);

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  new_organization_id uuid;
  new_organization_name text;
begin
  insert into public.profiles (user_id, display_name)
  values (
    new.id,
    coalesce(nullif(trim(new.raw_user_meta_data ->> 'display_name'), ''), split_part(new.email, '@', 1), '')
  )
  on conflict (user_id) do nothing;

  if coalesce(new.raw_user_meta_data ->> 'account_type', '') = 'realtor_owner' then
    new_organization_name := left(
      coalesce(
        nullif(trim(new.raw_user_meta_data ->> 'organization_name'), ''),
        nullif(concat(split_part(new.email, '@', 1), '''s organization'), '''s organization'),
        'My organization'
      ),
      160
    );

    insert into public.organizations (name)
    values (new_organization_name)
    returning id into new_organization_id;

    insert into public.organization_members (organization_id, user_id, role)
    values (new_organization_id, new.id, 'owner');
  end if;

  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

revoke all on function public.handle_new_user() from public;

-- A hosted Auth user can exist before this first application migration is
-- deployed. Backfill those users so an early signup is not stranded without
-- the profile and owner membership normally created by the trigger above.
insert into public.profiles (user_id, display_name)
select
  users.id,
  coalesce(
    nullif(trim(users.raw_user_meta_data ->> 'display_name'), ''),
    split_part(users.email, '@', 1),
    ''
  )
from auth.users as users
on conflict (user_id) do nothing;

do $$
declare
  existing_user record;
  new_organization_id uuid;
  new_organization_name text;
begin
  for existing_user in
    select users.id, users.email, users.raw_user_meta_data
    from auth.users as users
    where coalesce(users.raw_user_meta_data ->> 'account_type', '') = 'realtor_owner'
      and not exists (
        select 1
        from public.organization_members
        where organization_members.user_id = users.id
      )
  loop
    new_organization_name := left(
      coalesce(
        nullif(trim(existing_user.raw_user_meta_data ->> 'organization_name'), ''),
        nullif(concat(split_part(existing_user.email, '@', 1), '''s organization'), '''s organization'),
        'My organization'
      ),
      160
    );

    insert into public.organizations (name)
    values (new_organization_name)
    returning id into new_organization_id;

    insert into public.organization_members (organization_id, user_id, role)
    values (new_organization_id, existing_user.id, 'owner');
  end loop;
end;
$$;

create or replace function private.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = statement_timestamp();
  return new;
end;
$$;

revoke all on function private.set_updated_at() from public;

create trigger profiles_set_updated_at
  before update on public.profiles
  for each row execute procedure private.set_updated_at();

create trigger spaces_set_updated_at
  before update on public.spaces
  for each row execute procedure private.set_updated_at();

create or replace function private.is_organization_member(target_organization_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.organization_members
    where organization_id = target_organization_id
      and user_id = (select auth.uid())
  );
$$;

create or replace function private.has_organization_role(
  target_organization_id uuid,
  allowed_roles public.organization_role[]
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.organization_members
    where organization_id = target_organization_id
      and user_id = (select auth.uid())
      and role = any (allowed_roles)
  );
$$;

revoke all on function private.is_organization_member(uuid) from public;
revoke all on function private.has_organization_role(uuid, public.organization_role[]) from public;
grant execute on function private.is_organization_member(uuid) to authenticated;
grant execute on function private.has_organization_role(uuid, public.organization_role[]) to authenticated;

alter table public.profiles enable row level security;
alter table public.organizations enable row level security;
alter table public.organization_members enable row level security;
alter table public.spaces enable row level security;
alter table private.tour_invitations enable row level security;
alter table private.guest_sessions enable row level security;
alter table private.socket_tickets enable row level security;

create policy profiles_select_self
  on public.profiles
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy profiles_update_self
  on public.profiles
  for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy organizations_select_member
  on public.organizations
  for select
  to authenticated
  using ((select private.is_organization_member(id)));

create policy organization_members_select_member
  on public.organization_members
  for select
  to authenticated
  using ((select private.is_organization_member(organization_id)));

create policy spaces_select_member
  on public.spaces
  for select
  to authenticated
  using ((select private.is_organization_member(organization_id)));

create policy spaces_insert_operator
  on public.spaces
  for insert
  to authenticated
  with check (
    created_by = (select auth.uid())
    and (select private.has_organization_role(
      organization_id,
      array['owner', 'realtor']::public.organization_role[]
    ))
  );

create policy spaces_update_operator
  on public.spaces
  for update
  to authenticated
  using ((select private.has_organization_role(
    organization_id,
    array['owner', 'realtor']::public.organization_role[]
  )))
  with check ((select private.has_organization_role(
    organization_id,
    array['owner', 'realtor']::public.organization_role[]
  )));

create policy spaces_delete_operator
  on public.spaces
  for delete
  to authenticated
  using ((select private.has_organization_role(
    organization_id,
    array['owner', 'realtor']::public.organization_role[]
  )));

revoke all on table public.profiles from anon, authenticated;
revoke all on table public.organizations from anon, authenticated;
revoke all on table public.organization_members from anon, authenticated;
revoke all on table public.spaces from anon, authenticated;

grant select on table public.profiles to authenticated;
grant update (display_name) on table public.profiles to authenticated;
grant select on table public.organizations to authenticated;
grant select on table public.organization_members to authenticated;
grant select, insert, delete on table public.spaces to authenticated;
grant update (name, relay_room_id) on table public.spaces to authenticated;

revoke all on all tables in schema private from anon, authenticated;
revoke all on all sequences in schema private from anon, authenticated;
grant all privileges on all tables in schema private to service_role;
grant usage, select on all sequences in schema private to service_role;

alter default privileges in schema private
  revoke all on tables from anon, authenticated;
alter default privileges in schema private
  grant all privileges on tables to service_role;
