/** Public listings with a bracketbot on site. Static for the prototype — the relay
 *  will eventually publish this list from live rooms. */
export interface OpenTour {
  id: string
  roomId: string
  name: string
  neighbourhood: string
  /** ISO time of the next tour when not live. */
  nextTourAt?: string
  live: boolean
  host?: string
  watching?: number
  /** Index into the plan thumbnail variants. */
  plan: 0 | 1 | 2 | 3
}

export const OPEN_TOURS: OpenTour[] = [
  {
    id: 'noe-2bed',
    roomId: 'demo-bot',
    name: 'Sunny 2-bed',
    neighbourhood: 'Noe Valley',
    live: true,
    host: 'Dana',
    watching: 3,
    plan: 0,
  },
  {
    id: 'mission-loft',
    roomId: 'demo-bot',
    name: 'Loft',
    neighbourhood: 'The Mission',
    live: true,
    plan: 1,
  },
  {
    id: 'alamo-victorian',
    roomId: 'demo-bot',
    name: 'Victorian',
    neighbourhood: 'Alamo Square',
    live: false,
    nextTourAt: '2026-09-21T13:30:00',
    plan: 2,
  },
  {
    id: 'dolores-studio',
    roomId: 'demo-bot',
    name: 'Studio',
    neighbourhood: 'Dolores Park',
    live: false,
    nextTourAt: '2026-09-22T17:00:00',
    plan: 3,
  },
]

/** Every kind of place a bracketbot can walk. Order is the on-screen order. */
export const PLACE_WORDS = [
  'apartments',
  'houses',
  'lofts',
  'condos',
  'townhomes',
  'studios',
  'penthouses',
  'bungalows',
  'cottages',
  'cabins',
  'villas',
  'duplexes',
  'brownstones',
  'farmhouses',
  'new builds',
  'open houses',
  'offices',
  'storefronts',
  'showrooms',
  'galleries',
  'venues',
  'warehouses',
  'rentals',
]
