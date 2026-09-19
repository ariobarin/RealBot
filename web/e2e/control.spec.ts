import { expect, test } from '@playwright/test'

test('connect screen remembers a room and opens the control dashboard', async ({ page }) => {
  await page.goto('/connect')
  await expect(page.getByRole('heading', { name: 'Connect to your bracketbot' })).toBeVisible()
  await page.getByLabel('Demo room ID').fill('hack-room')
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page).toHaveURL('/control/hack-room')
  await expect(page.getByRole('heading', { name: 'hack-room' })).toBeVisible()
  await expect(page.getByTestId('connection-status')).toContainText(/Connecting|Waiting|Reconnecting/)
  await expect(page.getByText('Waiting for camera frames')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop' })).toBeDisabled()
})
