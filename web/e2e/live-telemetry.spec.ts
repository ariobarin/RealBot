import { test, expect } from '@playwright/test'
import { authenticateRealtor } from './auth'

test('realtor can open the read-only live view and sees unavailable values before joining', async ({
  page,
}) => {
  await authenticateRealtor(page)
  await page.goto('/realtor/live/demo-bot')
  await expect(page.getByRole('heading', { name: 'Live robot view' })).toBeVisible()
  await expect(page.getByText('Session: disconnected · Robot: Offline · Telemetry: Waiting')).toBeVisible()
  await expect(page.getByText('Waiting for head camera')).toBeVisible()
  await expect(page.getByRole('button', { name: /move|stop|free cam/i })).toHaveCount(0)
  await page.getByLabel('LiveKit URL').fill('ws://unsafe.example')
  await page.getByLabel('Robot identity').fill('bracketbot-test')
  await page.getByLabel('Viewer token').fill('test-only')
  await page.getByRole('button', { name: 'Connect', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('secure wss://')
  await page.screenshot({ path: 'test-results/live-telemetry.png', fullPage: true })
})

test('visitor cannot access the realtor live view', async ({ page }) => {
  await page.addInitScript(() =>
    sessionStorage.setItem('realbot-session', JSON.stringify({ role: 'visitor', roomId: 'demo-bot' })),
  )
  await page.goto('/realtor/live/demo-bot')
  await expect(page).toHaveURL(/\/signin$/)
  await expect(page.getByRole('heading', { name: 'Live robot view' })).toHaveCount(0)
})
