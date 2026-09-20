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
  await expect(page.getByRole('button', { name: 'Choose destination' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeDisabled()
  await page.getByLabel('LiveKit URL').fill('ws://unsafe.example')
  await page.getByLabel('Robot identity').fill('bot')
  await page.getByLabel('Controller token').fill('not-a-real-token')
  await page.getByRole('button', { name: 'Connect', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('secure wss://')
  await page.screenshot({ path: 'test-results/live-drive-disconnected.png', fullPage: true })
})

test('visitor can select a captured image and Stop (UI fixture, no robot connection)', async ({ page }) => {
  // Replace only the UI client boundary. Wire protocol behavior is covered by
  // liveDriveClient.test.ts; this checks actual DOM sizing, clicks and buttons.
  await page.route('**/src/lib/liveDriveClient.ts', (route) =>
    route.fulfill({
      contentType: 'application/javascript',
      body: `export class LiveDriveClient {
      constructor(view, drive) { this.view = view; this.drive = drive; window.__driveClicks = []; }
      async connect() {
        this.view({connection:'connected',robotOnline:true,telemetryFresh:false,camera:{attach(){},detach(){}}});
        this.drive({available:true,canCapture:true,busy:false,status:'UI TEST FIXTURE — not robot video'});
      }
      chooseDestination() {
        const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"><rect width="640" height="480" fill="#ddd"/><text x="40" y="240">UI TEST FIXTURE — not robot video</text></svg>';
        this.drive({available:true,canCapture:false,busy:false,status:'Select test pixel',capture:{id:'test',image:'data:image/svg+xml,'+encodeURIComponent(svg),expires:Infinity}});
      }
      move(u,v) { window.__driveClicks.push({u,v}); this.drive({available:true,canCapture:false,busy:true,status:'Moving (UI fixture)'}); }
      stop() { this.drive({available:true,canCapture:true,busy:false,status:'Stopped (UI fixture)'}); }
      disconnect() {}
    }`,
    }),
  )
  await page.goto('/user/demo-bot/live')
  await page.getByLabel('LiveKit URL').fill('wss://test.invalid')
  await page.getByLabel('Robot identity').fill('bot')
  await page.getByLabel('Controller token').fill('ui-test-only')
  await page.getByRole('button', { name: 'Connect', exact: true }).click()
  await page.getByRole('button', { name: 'Choose destination' }).click()
  const image = page.getByAltText('Captured camera view: click a clear floor destination')
  await expect(image).toBeVisible()
  const bounds = (await image.boundingBox())!
  await image.click({ position: { x: bounds.width * 0.25, y: bounds.height * 0.75 } })
  const clicks = await page.evaluate(
    () => (window as unknown as { __driveClicks: Array<{ u: number; v: number }> }).__driveClicks,
  )
  expect(clicks).toHaveLength(1)
  expect(clicks[0].u).toBeCloseTo(0.25, 2)
  expect(clicks[0].v).toBeCloseTo(0.75, 2)
  await expect(image).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Choose destination' })).toBeDisabled()
  await page.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(page.getByText('Stopped (UI fixture)')).toBeVisible()
})
