begin;

create extension if not exists pgtap with schema extensions;

select plan(14);

insert into auth.users (id, email)
values
  ('10000000-0000-0000-0000-000000000001', 'owner@example.test'),
  ('10000000-0000-0000-0000-000000000002', 'viewer@example.test'),
  ('10000000-0000-0000-0000-000000000003', 'outsider@example.test');

insert into public.organizations (id, name)
values ('20000000-0000-0000-0000-000000000001', 'Test Realty');

insert into public.organization_members (organization_id, user_id, role)
values
  (
    '20000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    'owner'
  ),
  (
    '20000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000002',
    'viewer'
  );

insert into public.spaces (id, organization_id, name, relay_room_id, created_by)
values (
  '30000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001',
  'Test Space',
  'test-room',
  '10000000-0000-0000-0000-000000000001'
);

select is(
  (select count(*) from public.profiles),
  3::bigint,
  'creating an auth user creates a profile'
);

insert into auth.users (id, email, raw_user_meta_data)
values (
  '10000000-0000-0000-0000-000000000004',
  'self-signup@example.test',
  '{"account_type":"realtor_owner","display_name":"Test Agent","organization_name":"New Realty"}'
);

select is(
  (
    select display_name
    from public.profiles
    where user_id = '10000000-0000-0000-0000-000000000004'
  ),
  'Test Agent',
  'self-signup creates the realtor profile'
);

select is(
  (
    select organizations.name
    from public.organization_members
    join public.organizations on organizations.id = organization_members.organization_id
    where organization_members.user_id = '10000000-0000-0000-0000-000000000004'
      and organization_members.role = 'owner'
  ),
  'New Realty',
  'self-signup creates an owned organization'
);

set local role authenticated;
select set_config(
  'request.jwt.claims',
  '{"sub":"10000000-0000-0000-0000-000000000001","role":"authenticated"}',
  true
);

select is(
  (select count(*) from public.organizations),
  1::bigint,
  'an owner can read their organization'
);

select is(
  (select count(*) from public.spaces),
  1::bigint,
  'an owner can read their organization spaces'
);

select lives_ok(
  $$
    insert into public.spaces (organization_id, name, relay_room_id, created_by)
    values (
      '20000000-0000-0000-0000-000000000001',
      'Owner Space',
      'owner-room',
      '10000000-0000-0000-0000-000000000001'
    )
  $$,
  'an owner can create a space'
);

select lives_ok(
  $$
    update public.spaces
    set name = 'Renamed Test Space'
    where id = '30000000-0000-0000-0000-000000000001'
  $$,
  'an owner can update an allowed space field'
);

select throws_ok(
  $$
    update public.spaces
    set created_by = '10000000-0000-0000-0000-000000000002'
    where id = '30000000-0000-0000-0000-000000000001'
  $$,
  '42501',
  null,
  'an owner cannot rewrite a space creator'
);

select ok(
  (
    select updated_at > created_at
    from public.spaces
    where id = '30000000-0000-0000-0000-000000000001'
  ),
  'updating a space advances its updated timestamp'
);

select set_config(
  'request.jwt.claims',
  '{"sub":"10000000-0000-0000-0000-000000000002","role":"authenticated"}',
  true
);

select is(
  (select count(*) from public.spaces),
  2::bigint,
  'a viewer can read organization spaces'
);

select throws_ok(
  $$
    insert into public.spaces (organization_id, name, relay_room_id, created_by)
    values (
      '20000000-0000-0000-0000-000000000001',
      'Viewer Space',
      'viewer-room',
      '10000000-0000-0000-0000-000000000002'
    )
  $$,
  '42501',
  null,
  'a viewer cannot create a space'
);

select set_config(
  'request.jwt.claims',
  '{"sub":"10000000-0000-0000-0000-000000000003","role":"authenticated"}',
  true
);

select is(
  (select count(*) from public.organizations),
  0::bigint,
  'an outsider cannot read organizations'
);

select is(
  (select count(*) from public.spaces),
  0::bigint,
  'an outsider cannot read spaces'
);

select is(
  (select count(*) from public.organization_members),
  0::bigint,
  'an outsider cannot enumerate organization membership'
);

reset role;

select * from finish();
rollback;
