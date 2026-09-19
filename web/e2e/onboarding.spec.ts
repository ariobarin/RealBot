import { expect, test } from '@playwright/test'
import { authenticateRealtor } from './auth'

test.beforeEach(async ({ page }) => authenticateRealtor(page))

test('add card opens onboarding; pairing enables Start after ~2 s; Start scans then opens the preset map', async ({
  page,
}) => {
  await page.goto('/realtor')
  await page.getByTestId('add-card').click()
  await expect(page).toHaveURL('/onboard')

  await expect(page.getByTestId('step-card')).toHaveCount(3)
  await expect(page.getByText('Looking on this network…')).toBeVisible()
  await expect(page.getByTestId('start-button')).toBeDisabled()

  await page.waitForTimeout(1500)
  await expect(page.getByTestId('start-button')).toBeDisabled()

  await expect(page.getByTestId('start-button')).toBeEnabled({ timeout: 1500 })
  await expect(page.getByText(/Found bracketbot/)).toBeVisible()

  await page.getByTestId('start-button').click()
  await expect(page.getByTestId('scanning')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Mapping the space' })).toBeVisible()
  await expect(page).toHaveURL('/map/small-house', { timeout: 12_000 })
  await expect(page.getByTestId('map-view')).toHaveAttribute('data-status', 'ready', { timeout: 10_000 })
  await expect(page.getByRole('heading', { name: 'Small House' })).toBeVisible()
})

test('escape returns to the library and pairing restarts on re-entry', async ({ page }) => {
  await page.goto('/onboard')
  await page.keyboard.press('Escape')
  await expect(page).toHaveURL('/realtor')

  await page.getByTestId('add-card').click()
  await expect(page.getByTestId('pairing')).toHaveAttribute('data-phase', 'pairing')
  await expect(page.getByTestId('start-button')).toBeEnabled({ timeout: 3000 })
})
