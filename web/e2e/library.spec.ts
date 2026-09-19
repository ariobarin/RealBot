import { expect, test } from '@playwright/test'

test('library shows the preset SLAM map and an inert add card', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Your spaces' })).toBeVisible()

  const cards = page.getByTestId('map-card')
  await expect(cards).toHaveCount(1)
  await expect(cards.first()).toContainText('Small House')
  await expect(cards.first().getByRole('img')).toHaveAttribute('alt', /SLAM floor plan/)

  await page.getByTestId('add-card').click()
  await expect(page).toHaveURL('/')
})

test('map card navigates to the map placeholder', async ({ page }) => {
  await page.goto('/')
  await page.getByTestId('map-card').first().click()
  await expect(page).toHaveURL('/map/small-house')
  await expect(page.getByRole('heading', { name: 'Map view' })).toBeVisible()
  await page.getByRole('link', { name: /Back to your spaces/ }).click()
  await expect(page).toHaveURL('/')
})

test('onboard placeholder is reachable by URL', async ({ page }) => {
  await page.goto('/onboard')
  await expect(page.getByRole('heading', { name: 'Onboarding & pairing' })).toBeVisible()
})
