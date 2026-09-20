import { expect, test } from '@playwright/test'
import { authenticateRealtor } from './auth'

test.beforeEach(async ({ page }) => authenticateRealtor(page))

test('realtor selects availability and copies a placeholder visitor link', async ({ context, page }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/map/small-house/viewing')

  await expect(page.getByRole('heading', { name: 'Open for viewing' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Small House' })).toBeVisible()

  const time = page.getByRole('button', { name: '9:00 AM' })
  await time.click()
  await expect(time).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('1 time selected')).toBeVisible()

  await page.getByRole('button', { name: 'Create viewing link' }).click()
  await expect(page.getByRole('heading', { name: 'Your viewing link is ready' })).toBeVisible()

  const visitorLink = page.getByRole('textbox', { name: 'Visitor link' })
  await expect(visitorLink).toHaveValue(/\?tour=small-house&invite=preview$/)
  await page.getByRole('button', { name: 'Copy link' }).click()
  await expect(page.getByRole('button', { name: 'Copied' })).toBeVisible()
  await expect
    .poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toContain('?tour=small-house&invite=preview')
})
