import { expect, test } from '@playwright/test'
import { authenticateRealtor } from './auth'

test.beforeEach(async ({ page }) => authenticateRealtor(page))

test('library shows the scanned and preset SLAM maps with an add card', async ({ page }) => {
  await page.goto('/realtor')
  await expect(page.getByRole('heading', { name: 'Your spaces' })).toBeVisible()

  const cards = page.getByTestId('map-card')
  await expect(cards).toHaveCount(3)
  await expect(cards.first()).toContainText('Bracketbot Scan')
  await expect(cards.first()).toContainText('3D scan')
  await expect(cards.nth(1)).toContainText('Small House')
  await expect(cards.nth(2)).toContainText('TurtleBot3 Sandbox')
  await expect(cards.first().getByRole('img')).toHaveAttribute('alt', /SLAM floor plan/)

  await expect(page.getByTestId('add-card')).toHaveAttribute('href', '/onboard')
  await expect(page.getByRole('button', { name: 'Preview user view' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open robot dashboard' })).toBeVisible()
})

test('map card navigates to the map view', async ({ page }) => {
  await page.goto('/realtor')
  await page.getByTestId('map-card').first().click()
  await expect(page).toHaveURL('/map/bracketbot-scan')
  await expect(page.getByRole('heading', { name: 'Bracketbot Scan' })).toBeVisible()
  await page.getByRole('link', { name: /Your spaces/ }).click()
  await expect(page).toHaveURL('/realtor')
})

test('onboarding is reachable by URL', async ({ page }) => {
  await page.goto('/onboard')
  await expect(page.getByRole('heading', { name: /Scan a space/ })).toBeVisible()
})
