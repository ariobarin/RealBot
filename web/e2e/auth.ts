import type { Page } from '@playwright/test'

export async function authenticateRealtor(page: Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem(
      'realbot-session',
      JSON.stringify({
        role: 'realtor',
        userId: 'test-realtor',
        email: 'realtor@realbot.demo',
        organizationId: 'test-organization',
        membershipRole: 'realtor',
      }),
    )
  })
}
