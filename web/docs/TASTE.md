# RealBot taste doc

The one page every UI change should be checked against. Vibe: **Airbnb** — warm, airy,
rounded, friendly, and animated in a way that feels physical (springs, not linear tweens).
Chill, never loud. Motion is generous but always in service of _where did that come from /
where did it go_.

Tokens live in `src/styles/tokens.css` and are mirrored into Tailwind via `@theme` in
`src/index.css`. Use the Tailwind names (`bg-brand`, `text-ink-2`, `rounded-2xl`, `shadow-md`)
— never hard-code hex in components.

## 1. Colour

| Token          | Value     | Use                                        |
| -------------- | --------- | ------------------------------------------ |
| `--bg`         | `#FFFFFF` | page                                       |
| `--bg-soft`    | `#F7F7F7` | skeletons, hover fills, thumbnails' well   |
| `--ink`        | `#222222` | primary text, outlines                     |
| `--ink-2`      | `#6A6A6A` | secondary text                             |
| `--ink-3`      | `#B0B0B0` | tertiary text, meta                        |
| `--line`       | `#EBEBEB` | hairlines, dashed borders                  |
| `--brand`      | `#FF385C` | the one accent: CTAs, active states, robot |
| `--brand-2`    | `#E00B41` | brand hover / pressed                      |
| `--brand-soft` | `#FFF1F4` | brand tints, pills                         |
| `--ok`         | `#008A05` | success pills                              |

Map palette: floor `--map-floor #F3EFE8` (warm paper), walls `--map-wall #2B2B2B`, grid lines
`--map-grid #E6E2DB`, unknown = page background. Robot and interactables use `--brand`.

Rules: one accent colour on screen at a time. Text is always `ink`/`ink-2`/`ink-3`, never
grey-on-grey below 4.5:1. Brand is for _action_, not decoration.

## 2. Type

Figtree (Airbnb Cereal is proprietary; Figtree at 700–800 with `-0.035em` tracking on titles is
the closest free fit). Loaded from Google Fonts; falls back to system-ui.

| Role          | Size / weight                          |
| ------------- | -------------------------------------- |
| Page title    | 30–36 px / 700, tracking-tight         |
| Section title | 22 px / 600                            |
| Card title    | 15 px / 600                            |
| Body          | 15–16 px / 400, `ink-2` when secondary |
| Meta / pills  | 12–13 px / 500                         |

Copy voice: short, warm, second person. "Your spaces", "Add a space", "Pair a bracketbot and
scan". No exclamation marks. Sentence case everywhere.

## 3. Shape, space, depth

- Radii: `8 / 12 / 16 / 24 / pill`. Cards and thumbnails are `rounded-2xl` (16). Buttons are pills.
- Spacing scale is Tailwind's 4 px grid. Card grids: `gap-x-6 gap-y-10`. Page gutters `px-6 sm:px-10`.
- Shadows: `sm` at rest, `md` on hover, `lg` for floating panels. Never a border _and_ a shadow.
- Grids use `repeat(auto-fill, minmax(260px, 1fr))` so any item count lays out well.

## 4. Motion

Principles

1. **Everything that changes state animates.** Appear, disappear, move, or morph — never pop.
2. **Springs for things you touch, eases for things that just appear.** `spring(300, 24)` for
   hover/tap/morph; `cubic-bezier(.2,.8,.2,1)` 250 ms for fades and rises.
3. **Shared layout over crossfade.** If A _becomes_ B (card → page, orb → button), use a Framer
   `layoutId` so the eye follows it.
4. **Small distances.** Entrances rise ≤ 16 px. Hover lifts ≤ 2 px and scales ≤ 1.03.
5. **Stagger, but cap it.** Lists stagger children ≤ 60 ms each, and the total spread never
   exceeds ~600 ms regardless of count (`staggerList()` in `lib/motion.ts`).
6. **Respect `prefers-reduced-motion`.** Global CSS clamps durations to 1 ms; Framer springs
   should be swapped for instant tweens via `useReducedMotion()` when meaningful.
7. **Don't animate the data.** Maps, grids and coordinates are truth — reveal them elegantly
   (fade, rise, sweep) but never distort them.

Durations: `fast 150` (colour, opacity), `base 250` (most), `slow 450` (page-level, morphs).

Catalogue of shared pieces

| Thing                  | Motion                                                                     |
| ---------------------- | -------------------------------------------------------------------------- |
| Page enter / exit      | fade + 8 px rise in 300 ms; exit fade + 8 px up in 180 ms (`pageVariants`) |
| Card grid              | staggered `fadeUp`                                                         |
| Card hover             | lift 2 px, shadow sm→md, thumbnail scale 1.03 over 300 ms                  |
| Add card hover         | `+` rotates 90° with spring, dashed border → brand, fill → brand-soft      |
| Button                 | hover scale 1.02, tap 0.97, spring                                         |
| Skeleton               | 1.6 s shimmer                                                              |
| (Sprint 3) pairing orb | radar rings; morphs into Start via `layoutId`                              |
| (Sprint 4) map reveal  | camera dolly 900 ms, walls rise radially over 700 ms                       |

## 5. Interaction

- Anything clickable has a visible hover _and_ a `:focus-visible` ring (2 px ink, 3 px offset).
- Placeholder / not-yet-wired controls still look real but say so (`title="Coming soon"`,
  `cursor-default`). Never a dead click with no affordance.
- Loading: skeletons that match the final layout, not spinners, for content. Spinners only inside buttons.
- Errors: a friendly sentence, the real reason underneath in `ink-2`, and one retry action.
- Empty states: an illustration or the primary object's silhouette, one line of copy, one CTA.

## 6. Accessibility floor

Lighthouse a11y ≥ 95. Every image has an alt that says what it _is_ ("SLAM floor plan of Small
House"). Every icon-only control has `aria-label`. Colour is never the only status signal (pills
carry text). Keyboard order follows visual order.

## 5. Icons

The personality lives in the icons (`src/components/icons/`), the way Airbnb's nav does it: the
page is still, the icons are alive. Every hero icon is a small object on a 96-unit grid with a
front face and a darker side face, one light source top-left, a contact shadow underneath, and
exactly one `--brand` part. Each has a **quiet idle loop** (≤ 3 px of travel) and **one bigger
move on hover**, triggered by an `.icon-hover` ancestor:

| Icon          | Idle                        | Hover                            | Where                          |
| ------------- | --------------------------- | -------------------------------- | ------------------------------ |
| `HouseIcon`   | tree sways                  | door swings open, chimney smokes | Manage spaces tab, empty state |
| `MapPinIcon`  | pin hovers, route flows     | pin jumps, lands with a squash   | Tours tab                      |
| `RobotIcon`   | lens blinks, antenna pulses | wheels spin, body leans forward  | Go live, pairing, status       |
| `KeyRingIcon` | hangs and swings            | big swing, tag jingles           | Realtor sign in                |

Small UI glyphs (arrows, chevrons, close) stay lucide. The Tours hero also has the one large
motion on that page: `WordCarousel`, a three-row picker wheel of place types (0.9 s hold, 0.32 s
move, six tints, a window 1.5 rows tall). Nothing else on the page should compete with it.
