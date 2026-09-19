import { expect, test } from '@playwright/test'

test('library shows the preset SLAM map and an add card', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Your spaces' })).toBeVisible()

  const cards = page.getByTestId('map-card')
  await expect(cards).toHaveCount(1)
  await expect(cards.first()).toContainText('Small House')
  await expect(cards.first().getByRole('img')).toHaveAttribute('alt', /SLAM floor plan/)

  await expect(page.getByTestId('add-card')).toHaveAttribute('href', '/onboard')
})

test('map card navigates to the map view', async ({ page }) => {
  await page.goto('/')
  await page.getByTestId('map-card').first().click()
  await expect(page).toHaveURL('/map/small-house')
  await expect(page.getByRole('heading', { name: 'Small House' })).toBeVisible()
  await page.getByRole('link', { name: /Your spaces/ }).click()
  await expect(page).toHaveURL('/')
})

test('onboarding is reachable by URL', async ({ page }) => {
  await page.goto('/onboard')
  await expect(page.getByRole('heading', { name: /Scan a space/ })).toBeVisible()
})
