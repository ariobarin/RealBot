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
  /** The SLAM preset behind this listing (an id in public/maps/_index.json). */
  mapId: string
  /** Where the robot is on the plan, as a fraction of the thumbnail's width/height. */
  robot: [number, number]
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
    mapId: 'small-house',
    robot: [0.62, 0.48],
  },
  {
    id: 'mission-loft',
    roomId: 'demo-bot',
    name: 'Loft',
    neighbourhood: 'The Mission',
    live: true,
    mapId: 'bookstore',
    robot: [0.3, 0.7],
  },
  {
    id: 'alamo-victorian',
    roomId: 'demo-bot',
    name: 'Victorian',
    neighbourhood: 'Alamo Square',
    live: false,
    nextTourAt: '2026-09-21T13:30:00',
    mapId: 'tb3-house',
    robot: [0.5, 0.55],
  },
  {
    id: 'dolores-studio',
    roomId: 'demo-bot',
    name: 'Studio',
    neighbourhood: 'Dolores Park',
    live: false,
    nextTourAt: '2026-09-22T17:00:00',
    mapId: 'tb3-sandbox',
    robot: [0.5, 0.5],
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
