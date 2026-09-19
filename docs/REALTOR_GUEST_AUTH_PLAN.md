# Realtor and guest access plan

## Status

This document records the implementation decision for browser users of RealBot.
It replaces the earlier open question about which authentication provider to use.

- Realtor identities use **Supabase Auth**.
- Realtor authorization data uses **Supabase Postgres** with Row Level Security
  where the browser reads application data directly.
- Visitors do not create accounts. They receive an expiring invitation link from
  a realtor and exchange it for a tour-scoped guest session.
- Browser WebSocket connections use short-lived, single-use socket tickets
  issued after the server authorizes a realtor or guest session.
- Robot authentication is outside the scope of this plan. Existing robot relay
  connections are not changed by this work.

This is an implementation plan, not a claim that every boundary is already
deployed. The realtor frontend integration is present but is not operational
until the migration and hosted Auth configuration are applied. Visitor access
and browser relay sockets remain hackathon-only paths.

## Implementation status

The first realtor-authentication slice is present in the repository but still
requires the migration and hosted Auth configuration to be applied:

- the Supabase CLI configuration and versioned schema migration exist under
  `supabase/`;
- public organization, membership, profile, and space tables have explicit
  grants and RLS policies;
- invitation secrets, guest sessions, and socket tickets live in an unexposed
  `private` schema;
- the frontend lets a landlord/realtor create an email/password account and
  requires email confirmation;
- new realtor signups atomically create a profile, an organization, and an
  owner membership; subsequent sign-in still requires a membership row;
- the old demo realtor login is available only in the explicit Playwright test
  environment.

Accountless visitor-link exchange, invitation management UI/API, guest session
cookies, and browser socket-ticket enforcement are not implemented yet. The
current visitor room-code flow and relay remain a hackathon-only path.

## Why Supabase

RealBot needs more than a login screen. It needs relational records for realtor
membership, properties or spaces, tour invitations, expiration, revocation, and
visitor permissions. Supabase keeps managed authentication and that relational
authorization data together. A provider focused only on identity would still
require another database and synchronization between the two systems.

The decision is deliberately limited to realtor identity and browser access. It
does not select a media platform, message broker, robot credential format, or a
broader device-management architecture.

## Security boundary

Frontend route guards are navigation conveniences, not authorization. Every API
operation and browser WebSocket connection must be authorized by the server.

The following values are not authorization credentials:

- a client-selected role;
- a value in `localStorage` or `sessionStorage`;
- a route such as `/realtor/...`;
- a room ID;
- a visitor-supplied name or email address.

Do not place passwords, service-role keys, invitation tokens, or long-lived
access tokens in `VITE_` environment variables. Vite exposes those values to the
browser bundle. The Supabase project URL and publishable key are intentionally
public; server credentials remain server-only.

## Roles and authorization

The initial application roles are:

| Role          | Purpose                                                      |
| ------------- | ------------------------------------------------------------ |
| `owner`       | Manage an organization, realtor invitations, and membership. |
| `realtor`     | Manage accessible spaces and create or operate tours.        |
| `viewer`      | Optional read-only staff access.                             |
| guest session | Access one invitation's tour with its explicit permissions.  |

Application roles and organization membership are server-managed. Do not accept
authorization roles from user-editable Supabase user metadata. Database rows are
the source of truth so revocation can take effect without waiting for a JWT to
expire.

## Proposed data model

Names may change in migrations, but the relationships and security boundaries
should remain recognizable.

```text
auth.users                         managed by Supabase Auth

profiles
  user_id                          -> auth.users.id
  display_name

organizations
  id
  name

organization_members
  organization_id                 -> organizations.id
  user_id                          -> auth.users.id
  role                             owner | realtor | viewer

spaces
  id
  organization_id                 -> organizations.id
  name
  relay_room_id                    current relay lookup; not a credential

private.tour_invitations
  id
  space_id                         -> spaces.id
  created_by                       -> auth.users.id
  token_hash                       never store the link token in plaintext
  secret_hash                      nullable
  permissions                      view/control capabilities
  expires_at
  max_uses
  use_count
  revoked_at                       nullable

private.guest_sessions
  id
  invitation_id                    -> tour_invitations.id
  session_token_hash               never store the guest credential in plaintext
  display_name
  contact_email                    nullable and unverified unless OTP-verified
  expires_at
  revoked_at                       nullable

private.socket_tickets
  id
  token_hash                       never store the socket ticket in plaintext
  principal_type                   realtor | guest
  principal_user_id                nullable; set for a realtor
  guest_session_id                 nullable; set for a guest
  space_id
  connection_type                  control | video
  permissions
  expires_at
  consumed_at                      nullable
```

Enable RLS on every browser-accessible application table. At minimum:

- organization members can read only their organizations;
- realtors can create invitations only for spaces they can access;
- only an owner or the invitation creator can revoke an invitation;
- guests cannot query invitation or membership tables directly;
- Supabase service-role access is server-only and never shipped to the browser.

## Realtor flow

1. A landlord or realtor creates an account and names their organization.
2. The Auth user-creation trigger creates that user's profile, organization,
   and `owner` membership. Visitors never use this signup path.
3. Supabase handles verified email, password storage, sign-in, password reset,
   session refresh, and logout.
4. After sign-in, the application loads organization membership from the
   database. A valid Supabase session without membership does not grant realtor
   access.
5. Sensitive control-session issuance may require a recent login or an `aal2`
   MFA session. Read-only account and space views may remain available at
   `aal1`.
6. The relay verifies the Supabase access token and current database membership
   before issuing a socket ticket.

Email/password is the initial universal sign-in method. Google or Microsoft OIDC
can be added without changing the authorization model. TOTP MFA should be added
before treating remote robot control as production-ready.

## Visitor invitation flow

Visitors never become Supabase users and do not receive a durable RealBot
account.

1. A realtor creates an invitation for one space and selects its expiration,
   maximum uses, permissions, and optional secret/PIN requirement.
2. The server creates at least 256 bits of cryptographically random token data,
   stores only its hash, and returns a link such as `/tour/<token>` once.
3. The visitor opens the link and supplies a display name. Contact email is
   optional unless the product flow explicitly requires it. An entered address
   is unverified contact metadata unless an email OTP is completed.
4. If configured, the visitor also enters the separately shared secret or PIN.
   A secret sent in the same message as the link is not an independent security
   factor.
5. The server rate-limits attempts, validates the token hash, secret hash,
   expiration, revocation state, and use limit, then creates a guest session.
6. The client removes the raw invitation token from the address bar after the
   exchange and navigates to a token-free tour route.
7. The guest session can access only its selected space and explicit
   permissions. It ends on expiration, realtor revocation, or tour termination.

The guest's name and optional email may be attached to the temporary tour
session for display and a minimal audit trail, but they do not create an account.
Expired guest data should be purged on a documented retention schedule.

Prefer a generated PIN or multi-word secret over a realtor-chosen common word.
Store it using a password-hashing function, never plaintext, and apply per-link
and per-IP attempt limits.

## Browser WebSocket authorization

The browser WebSocket API cannot attach an ordinary `Authorization` header.
Do not put a Supabase access token or reusable invitation token into a WebSocket
URL, where infrastructure logs and browser history may expose it.

Use a ticket exchange:

1. The browser authenticates an HTTPS request using either its Supabase realtor
   session or its guest session.
2. `POST /v1/socket-tickets` checks current membership or invitation state and
   returns a random, single-use ticket valid for roughly 30 to 60 seconds.
3. The browser opens the control or video socket with that ticket.
4. The relay consumes the ticket before adding the browser to a room. A ticket
   is bound to one space, connection type, and permission set.
5. Reconnection obtains a new ticket. Revoked or expired principals cannot mint
   another one.

This applies to browser-facing `/ws/client/...` connections only. Robot-facing
WebSocket authentication is not part of this implementation.

## Suggested HTTP surface

Exact paths may change, but implementation should preserve these operations:

```text
POST   /v1/tour-invitations              create an invitation as a realtor
GET    /v1/tour-invitations              list manageable invitations
POST   /v1/tour-invitations/{id}/revoke  revoke an invitation and guest sessions
POST   /v1/guest-sessions/exchange       exchange link data and visitor fields
DELETE /v1/guest-sessions/current        leave a tour
POST   /v1/socket-tickets                mint one scoped, single-use ticket
```

Return generic errors from the public exchange endpoint so callers cannot
distinguish nonexistent, expired, revoked, or secret-protected invitations.

## Configuration

Browser configuration:

```text
VITE_SUPABASE_URL=
VITE_SUPABASE_PUBLISHABLE_KEY=
VITE_RELAY_WS_URL=
```

Server-only configuration should include the Supabase issuer/JWKS information
and any database or service credentials required by the chosen server library.
Exact variable names should be defined when the relay's deployment environment
is selected. No server secret may use a `VITE_` prefix.

Remove `VITE_REALTOR_EMAIL` and `VITE_REALTOR_PASSWORD` after the migration.

## Implementation sequence

1. Add versioned database migrations, RLS policies, and seed data for local
   development.
2. Add the Supabase client and replace the demo realtor comparison with real
   session state, loading state, error handling, recovery, and logout.
3. Add membership-backed realtor route loading. Keep route guards for UX, but
   rely on server and RLS checks for enforcement.
4. Add invitation creation, listing, revocation, and the `/tour/:token` visitor
   exchange flow.
5. Add guest-session validation and socket-ticket issuance to the relay or an
   adjacent API service.
6. Require socket tickets on browser control and video connections, leaving
   robot endpoints unchanged.
7. Replace room-ID entry in the visitor UI with invitation landing and session
   states.
8. Remove demo credentials and update end-to-end tests to use isolated Supabase
   test users or a documented local auth test double.
9. Add scheduled cleanup for expired invitations, guest sessions, and consumed
   tickets.

## Required tests

- A valid realtor with membership can load only authorized spaces.
- A signed-in user without membership cannot enter the realtor application.
- Browser-modified roles or storage do not grant access.
- An invitation works before expiration and fails after expiration or
  revocation.
- A secret-protected invitation is rate-limited and never stores the secret in
  plaintext.
- A guest cannot access another invitation's space or broaden its permissions.
- A consumed or expired socket ticket cannot be replayed.
- Revocation prevents new socket tickets and closes active guest connections.
- Realtor logout clears application state and prevents ticket issuance.
- Existing robot simulator and robot relay endpoints continue to work without
  adopting this browser-auth model.

## Deferred decisions

The following do not block the first implementation but must be selected before
production launch:

- the exact retention period for guest name, email, and audit events;
- whether visitor email is optional, required but unverified, or OTP-verified
  for particular invitation types;
- the deployment component that owns the HTTPS invitation and ticket endpoints;
- final session lifetimes and whether MFA is required for all realtor access or
  only control-ticket issuance;
- transactional email delivery and branding;
- privacy-policy, consent, account deletion, and support procedures.
