import { expect, test } from '@playwright/test'

test('add card opens onboarding; pairing flips to Start after ~2 s; Start opens the preset map', async ({
  page,
}) => {
  await page.goto('/')
  await page.getByTestId('add-card').click()
  await expect(page).toHaveURL('/onboard')

  await expect(page.getByTestId('step-card')).toHaveCount(4)
  await expect(page.getByText('Pairing with your bracketbot…')).toBeVisible()
  await expect(page.getByTestId('start-button')).toHaveCount(0)

  await page.waitForTimeout(1500)
  await expect(page.getByTestId('start-button')).toHaveCount(0)

  await expect(page.getByTestId('start-button')).toBeVisible({ timeout: 1500 })
  await expect(page.getByTestId('start-button')).toBeEnabled()
  await expect(page.getByTestId('step-card').nth(3)).toHaveAttribute('data-active', 'true')

  await page.getByTestId('start-button').click()
  await expect(page).toHaveURL('/map/small-house')
  await expect(page.getByRole('heading', { name: 'Small House' })).toBeVisible()
})

test('escape returns to the library and pairing restarts on re-entry', async ({ page }) => {
  await page.goto('/onboard')
  await page.keyboard.press('Escape')
  await expect(page).toHaveURL('/')

  await page.getByTestId('add-card').click()
  await expect(page.getByTestId('pairing')).toHaveAttribute('data-phase', 'pairing')
  await expect(page.getByTestId('start-button')).toBeVisible({ timeout: 3000 })
})
