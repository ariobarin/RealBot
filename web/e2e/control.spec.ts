import { expect, test } from '@playwright/test'

test('visitor portal opens a tour and keeps realtor pages inaccessible', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Choose how you’re joining' })).toBeVisible()
  await page.getByLabel('Tour access code').fill('hack-room')
  await page.getByRole('button', { name: 'Join tour' }).click()
  await expect(page).toHaveURL('/user/hack-room')
  await expect(page.getByRole('heading', { name: 'Control your bracketbot' })).toBeVisible()
  await expect(page.getByTestId('connection-status')).toContainText(/Connecting|Waiting|Reconnecting/)
  await expect(page.getByText('Waiting for camera frames')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop' })).toBeDisabled()
  await expect(page.getByRole('heading', { name: 'Command activity' })).toHaveCount(0)
  await expect(page.getByText('SLAM telemetry')).toHaveCount(0)
  await expect(page.getByTestId('visitor-minimap')).toBeVisible()
  await expect(page.getByLabel('Robot location and planned route')).toContainText('Floor map')
  const [pageHeight, viewportHeight] = await page.evaluate(() => [
    document.documentElement.scrollHeight,
    window.innerHeight,
  ])
  expect(pageHeight).toBeLessThanOrEqual(viewportHeight)

  await page.goto('/realtor')
  await expect(page).toHaveURL('/user/hack-room')
  await expect(page.getByRole('link', { name: /Realtor dashboard/ })).toHaveCount(0)
})

test('realtor dashboard can enter the exact user view', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('tab', { name: 'Realtor' }).click()
  await page.getByLabel('Email').fill('realtor@realbot.demo')
  await page.getByLabel('Password').fill('demo')
  await page.getByRole('button', { name: 'Open realtor portal' }).click()
  await expect(page).toHaveURL('/realtor')

  await page.evaluate(() => localStorage.setItem('realbot-room', 'listing-room'))
  await page.getByRole('button', { name: 'Open robot dashboard' }).click()

  await expect(page).toHaveURL('/realtor/control/listing-room')
  await expect(page.getByRole('heading', { name: 'listing-room' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Command activity' })).toBeVisible()
  await expect(page.getByText('SLAM telemetry')).toBeVisible()

  await page.getByRole('link', { name: 'Preview as user' }).click()
  await expect(page).toHaveURL('/user/listing-room')
  await expect(page.getByRole('heading', { name: 'Control your bracketbot' })).toBeVisible()
  await expect(page.getByText('this is the exact experience a user sees.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Command activity' })).toHaveCount(0)
  await expect(page.getByTestId('visitor-minimap')).toBeVisible()

  await page.getByRole('link', { name: 'Exit preview' }).click()
  await expect(page).toHaveURL('/realtor/control/listing-room')
})

test('invalid realtor credentials stay on the portal', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('tab', { name: 'Realtor' }).click()
  await page.getByLabel('Password').fill('wrong')
  await page.getByRole('button', { name: 'Open realtor portal' }).click()
  await expect(page.getByRole('alert')).toContainText('does not match')
  await expect(page).toHaveURL('/')
})
