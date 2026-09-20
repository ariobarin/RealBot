import { test, expect } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    sessionStorage.setItem('realbot-session', JSON.stringify({ role: 'visitor', roomId: 'demo-bot' })),
  )
})

test('visitor live view fails closed before a real session is available', async ({ page }) => {
  await page.goto('/user/demo-bot/live')
  await expect(page.getByRole('heading', { name: 'Explore with the robot' })).toBeVisible()
  await expect(page.getByText('Waiting for the robot camera')).toBeVisible()
  await expect(page.getByLabel('Live robot camera; click clear floor to choose a destination')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeDisabled()
  await page.getByLabel('LiveKit URL').fill('ws://unsafe.example')
  await page.getByLabel('Robot identity').fill('bot')
  await page.getByLabel('Controller token').fill('not-a-real-token')
  await page.getByRole('button', { name: 'Connect', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('secure wss://')
  await page.screenshot({ path: 'test-results/live-drive-disconnected.png', fullPage: true })
})

test('private demo session file fills the form without connecting or persisting tokens', async ({ page }) => {
  await page.goto('/user/demo-bot/live')
  await page.getByLabel('Load demo session').setInputFiles({
    name: 'browser.session.json',
    mimeType: 'application/json',
    buffer: Buffer.from(
      JSON.stringify({
        version: 1,
        kind: 'browser',
        url: 'wss://test.invalid',
        token: 'ui-fixture-token',
        robotIdentity: 'robot',
      }),
    ),
  })
  await expect(page.getByLabel('LiveKit URL')).toHaveValue('wss://test.invalid')
  await expect(page.getByLabel('Robot identity')).toHaveValue('robot')
  await expect(page.getByLabel('Controller token')).toHaveValue('ui-fixture-token')
  await expect(page.getByLabel('Live robot camera; click clear floor to choose a destination')).toHaveCount(0)
  expect(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage]))).not.toContain(
    'ui-fixture-token',
  )
  await page.getByLabel('Load demo session').setInputFiles({
    name: 'invalid.session.json',
    mimeType: 'application/json',
    buffer: Buffer.from('{}'),
  })
  await expect(page.getByRole('alert')).toContainText('Could not load')
})

test('visitor clicks the live camera directly and can Stop (UI fixture, no robot connection)', async ({ page }) => {
  // Replace only the UI client boundary. Wire protocol behavior is covered by
  // liveDriveClient.test.ts; this checks actual DOM sizing, clicks and buttons.
  await page.route('**/src/lib/liveDriveClient.ts', (route) =>
    route.fulfill({
      contentType: 'application/javascript',
      body: `export class LiveDriveClient {
      constructor(view, drive) { this.view = view; this.drive = drive; window.__driveClicks = []; }
      async connect() {
        this.view({connection:'connected',robotOnline:true,telemetryFresh:false,camera:{attach(el){el.style.width='640px';el.style.height='480px';},detach(){}}});
        this.drive({available:true,canCapture:true,canClick:true,busy:false,status:'UI TEST FIXTURE — not robot video'});
      }
      clickDestination(u,v) { window.__driveClicks.push({u,v}); this.drive({available:true,canCapture:false,canClick:false,busy:true,status:'Moving (UI fixture)'}); }
      stop() { this.drive({available:true,canCapture:true,canClick:true,busy:false,status:'Stopped (UI fixture)'}); }
      disconnect() {}
    }`,
    }),
  )
  await page.goto('/user/demo-bot/live')
  await page.getByLabel('LiveKit URL').fill('wss://test.invalid')
  await page.getByLabel('Robot identity').fill('bot')
  await page.getByLabel('Controller token').fill('ui-test-only')
  await page.getByRole('button', { name: 'Connect', exact: true }).click()
  const video = page.getByLabel('Live robot camera; click clear floor to choose a destination')
  await expect(video).toBeVisible()
  const bounds = (await video.boundingBox())!
  await video.click({ position: { x: bounds.width * 0.25, y: bounds.height * 0.75 } })
  const clicks = await page.evaluate(
    () => (window as unknown as { __driveClicks: Array<{ u: number; v: number }> }).__driveClicks,
  )
  expect(clicks).toHaveLength(1)
  expect(clicks[0].u).toBeCloseTo(0.25, 2)
  expect(clicks[0].v).toBeCloseTo(0.75, 2)
  await expect(page.getByLabel('Selected destination')).toBeVisible()
  await page.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(page.getByText('Stopped (UI fixture)')).toBeVisible()
})
