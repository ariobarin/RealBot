import { expect, test } from '@playwright/test'
import { authenticateRealtor } from './auth'

test.beforeEach(async ({ page }) => authenticateRealtor(page))

test('saved room opens the shared controls with existing action recorder data', async ({ page }) => {
  let failed = false
  await page.route('**/api/action-points/bracketbot-scan/actions', (route) =>
    route.fulfill({
      status: failed ? 503 : 200,
      json: {
        recording: { state: 'idle', message: 'Show a close thumbs-up to begin.', speech_status: 'listening' },
        landmarks: [
          {
            id: 'fixture-1',
            action_id: 'light_switch',
            action: { display_name: 'Light switch' },
            map_revision: 3,
          },
        ],
      },
    }),
  )
  await page.route('**/api/action-points/bracketbot-scan/stream', (route) => route.abort())
  await page.goto('/realtor')
  await page.getByTestId('add-card').click()
  await expect(page.getByRole('heading', { name: 'Set up a scanned room' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start scan' })).toHaveCount(0)
  await page.getByRole('link', { name: 'Bracketbot Scan Set up action points' }).click()
  await expect(page).toHaveURL('/realtor/control/bracketbot-scan')
  await expect(page.getByRole('heading', { name: 'Set up action points' })).toBeVisible()
  await page.locator('summary', { hasText: 'Action points' }).click()
  await expect(page.getByText('Show a close thumbs-up to begin.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Recorded points (1)' })).toBeVisible()
  await expect(page.getByText('Light switch', { exact: true })).toBeVisible()
  failed = true
  await expect(page.getByRole('alert')).toContainText('recorder is unavailable')
  await expect(page.getByRole('heading', { name: 'Recorded points (1)' })).toHaveCount(0)
  failed = false
  await expect(page.getByRole('heading', { name: 'Recorded points (1)' })).toBeVisible()
  await page.getByRole('link', { name: 'Preview as user' }).click()
  await expect(page.locator('summary', { hasText: 'Action points' })).toHaveCount(0)
})

test('escape returns to saved spaces without a simulated scan', async ({ page }) => {
  await page.goto('/onboard')
  await page.keyboard.press('Escape')
  await expect(page).toHaveURL('/realtor')
})
