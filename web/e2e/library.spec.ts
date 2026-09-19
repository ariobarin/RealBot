import { expect, test } from '@playwright/test'
import { authenticateRealtor } from './auth'

test.beforeEach(async ({ page }) => authenticateRealtor(page))

test('library shows the preset SLAM map and an add card', async ({ page }) => {
  await page.goto('/realtor')
  await expect(page.getByRole('heading', { name: 'Your spaces' })).toBeVisible()

  const cards = page.getByTestId('map-card')
  await expect(cards).toHaveCount(4)
  await expect(cards.first()).toContainText('Small House')
  await expect(cards.nth(1)).toContainText('TurtleBot3 Sandbox')
  await expect(cards.first().getByRole('img')).toHaveAttribute('alt', /SLAM floor plan/)

  await expect(page.getByTestId('add-card')).toHaveAttribute('href', '/onboard')
  await expect(page.getByRole('button', { name: 'Add a space' })).toBeVisible()
})

test('map card navigates to the map view', async ({ page }) => {
  await page.goto('/realtor')
  await page.getByTestId('map-card').first().click()
  await expect(page).toHaveURL('/map/small-house')
  await expect(page.getByRole('heading', { name: 'Small House' })).toBeVisible()
  await page.getByRole('link', { name: /Your spaces/ }).click()
  await expect(page).toHaveURL('/realtor')
})

test('onboarding is reachable by URL', async ({ page }) => {
  await page.goto('/onboard')
  await expect(page.getByRole('heading', { name: 'Add a space' })).toBeVisible()
})
