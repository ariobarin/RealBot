import { expect, test, type Page } from '@playwright/test'

const hasWebGL = (page: Page) =>
  page.evaluate(() => {
    const c = document.createElement('canvas')
    return !!(c.getContext('webgl2') || c.getContext('webgl'))
  })

test('map view loads the SLAM grid with HUD, legend and view controls', async ({ page }) => {
  await page.goto('/map/small-house')
  await expect(page.getByTestId('map-view')).toHaveAttribute('data-status', 'ready', { timeout: 10_000 })
  await expect(page.getByRole('heading', { name: 'Small House' })).toBeVisible()
  await expect(page.getByText('158 m²')).toBeVisible()
  await expect(page.getByText('SLAM · 5 cm')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Dictate interactables' })).toBeDisabled()

  const toggle = page.getByTestId('mode-toggle')
  await expect(toggle.getByRole('radio', { name: '3D' })).toHaveAttribute('aria-checked', 'true')
  await toggle.getByRole('radio', { name: '2D' }).click()
  await expect(toggle.getByRole('radio', { name: '2D' })).toHaveAttribute('aria-checked', 'true')
  await page.getByTestId('reset-view').click()

  await page.getByRole('link', { name: /Your spaces/ }).click()
  await expect(page).toHaveURL('/')
})

test('clicking the floor sends an add_wp command to the mock bot', async ({ page }) => {
  await page.goto('/map/small-house')
  await expect(page.getByTestId('map-view')).toHaveAttribute('data-status', 'ready', { timeout: 10_000 })
  test.skip(!(await hasWebGL(page)), 'WebGL unavailable in this browser')

  await page.getByTestId('mode-toggle').getByRole('radio', { name: '2D' }).click()
  await page.waitForTimeout(1500) // camera fly-to settles
  const box = await page.locator('canvas').boundingBox()
  if (!box) throw new Error('no canvas')
  await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30)

  const toast = page.getByTestId('command-toast')
  await expect(toast).toBeVisible()
  await expect(toast).toContainText('"type":"add_wp"')
  await expect(toast).toContainText('"x":')
  await expect(page.getByTestId('clear-waypoints')).toBeVisible()
  await page.getByTestId('clear-waypoints').click()
  await expect(page.getByTestId('clear-waypoints')).toHaveCount(0)
})

test('unknown map id shows an error with a way back', async ({ page }) => {
  await page.goto('/map/nope')
  await expect(page.getByRole('alert')).toContainText('No space with id "nope"')
  await page.getByRole('button', { name: 'Back to your spaces' }).click()
  await expect(page).toHaveURL('/')
})

test('without WebGL the scene falls back to the flat scan', async ({ page }) => {
  await page.goto('/map/small-house')
  await expect(page.getByTestId('map-view')).toHaveAttribute('data-status', 'ready', { timeout: 10_000 })
  if (await hasWebGL(page)) {
    await expect(page.locator('canvas')).toBeVisible()
  } else {
    await expect(page.getByTestId('scene-fallback').getByRole('img')).toBeVisible()
  }
})
