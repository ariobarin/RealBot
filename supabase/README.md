# RealBot Supabase development

The repository uses Supabase Auth for landlord/realtor accounts and Supabase
Postgres for organization membership and spaces. A realtor can create an
account and becomes the owner of a new organization after email confirmation.
Visitors are not Supabase users. The accountless visitor invitation endpoints
and browser socket tickets remain a later implementation slice.

The versioned schema is in `migrations/`. Do not make untracked production
schema changes in the Dashboard.

## Local stack

Install and start a Docker-compatible container runtime, then run from the
repository root:

```sh
supabase start
supabase db reset
supabase status
```

Local Studio is available at `http://localhost:54323`. `supabase status` prints
the local project URL and publishable key; use them in `web/.env.local` when
developing entirely against the local stack. Mailpit captures local Auth email.

Stop the stack without deleting its data with:

```sh
supabase stop
```

## Hosted development project

Log in and link this checkout to the hosted development project:

```sh
supabase login
supabase link --project-ref zyxlasrmpvfmtgcjfqfs
supabase migration list
supabase db push --dry-run
supabase db push
```

Link and push only after verifying that the selected project is the intended
development project. Never run `supabase db reset --linked` against production.

The hosted Auth settings must match `config.toml`:

- public email signup enabled for landlord/realtor account creation;
- anonymous sign-in disabled;
- email confirmation enabled;
- secure password changes enabled;
- minimum password length 12;
- lower- and uppercase letters, digits, and symbols required;
- Site URL and allowed redirects set for the deployed frontend.

## Create the first owner

1. Confirm the hosted **Site URL** and allowed redirect URLs point to the
   frontend.
2. Open the frontend, choose **Realtor**, then **Create a realtor account**.
3. Enter the realtor and organization details, then confirm the email.

The Auth trigger creates the `profiles`, `organizations`, and owner
`organization_members` rows atomically. A successfully authenticated user
without an organization membership is intentionally denied access to the
realtor application. Visitor links never call Supabase signup and never create
rows in `auth.users`.

The first migration also backfills realtor-owner accounts created before the
migration was deployed. After pushing it, an already-confirmed early account
does not need to be deleted or recreated.

## Keys

The browser receives only:

```text
VITE_SUPABASE_URL
VITE_SUPABASE_PUBLISHABLE_KEY
```

Never place a Supabase secret/service-role key in `web/`, a `VITE_` variable,
Git, browser code, or client-visible deployment configuration.
