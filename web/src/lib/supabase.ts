import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL?.trim()
const supabasePublishableKey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY?.trim()

// The demo credentials must never be usable from a production bundle, even if
// a deployment accidentally carries the Playwright-only environment variable.
export const isAuthTestMode = import.meta.env.DEV && import.meta.env.VITE_AUTH_MODE === 'test'

export const supabaseConfigurationError =
  !supabaseUrl ||
  !supabasePublishableKey ||
  supabasePublishableKey === 'replace_with_supabase_publishable_key'
    ? 'Supabase is not configured. Set VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY.'
    : null

export const supabase =
  !isAuthTestMode && !supabaseConfigurationError
    ? createClient(supabaseUrl!, supabasePublishableKey!, {
        auth: {
          autoRefreshToken: true,
          detectSessionInUrl: true,
          persistSession: true,
        },
      })
    : null
