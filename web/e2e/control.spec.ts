import { expect, test } from '@playwright/test'

test('connect screen remembers a room and opens the control dashboard', async ({ page }) => {
  await page.goto('/connect')
  await expect(page.getByRole('heading', { name: 'Connect to your bracketbot' })).toBeVisible()
  await page.getByLabel('Demo room ID').fill('hack-room')
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page).toHaveURL('/user/hack-room')
  await expect(page.getByRole('heading', { name: 'Control your bracketbot' })).toBeVisible()
  await expect(page.getByTestId('connection-status')).toContainText(/Connecting|Waiting|Reconnecting/)
  await expect(page.getByText('Waiting for camera frames')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop' })).toBeDisabled()
  await expect(page.getByRole('heading', { name: 'Command activity' })).toHaveCount(0)
})

test('realtor dashboard can enter the exact user view', async ({ page }) => {
  await page.goto('/realtor/connect')
  await expect(page.getByText('Realtor access')).toBeVisible()
  await page.getByLabel('Demo room ID').fill('listing-room')
  await page.getByRole('button', { name: 'Connect' }).click()

  await expect(page).toHaveURL('/realtor/control/listing-room')
  await expect(page.getByRole('heading', { name: 'listing-room' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Command activity' })).toBeVisible()

  await page.getByRole('link', { name: 'Preview as user' }).click()
  await expect(page).toHaveURL('/user/listing-room?preview=realtor')
  await expect(page.getByRole('heading', { name: 'Control your bracketbot' })).toBeVisible()
  await expect(page.getByText('this is the exact experience a user sees.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Command activity' })).toHaveCount(0)
})
