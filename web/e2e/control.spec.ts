import { expect, test } from '@playwright/test'

async function installRobotSocket(page: import('@playwright/test').Page, acquired = true) {
  await page.addInitScript((leaseAcquired) => {
    const commands: unknown[] = []
    Object.defineProperty(window, '__realbotCommands', { value: commands })

    class RobotSocket {
      static readonly OPEN = 1
      readonly OPEN = 1
      readyState = RobotSocket.OPEN
      binaryType = 'blob'
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      private readonly video: boolean

      constructor(url: string) {
        this.video = url.endsWith('/video')
        window.setTimeout(() => {
          this.onopen?.()
          if (!this.video) {
            this.onmessage?.({ data: JSON.stringify({ type: 'presence', online: true }) })
            this.onmessage?.({
              data: JSON.stringify({ type: 'control_lease', acquired: leaseAcquired }),
            })
          }
        }, 0)
      }

      send(raw: string) {
        const command = JSON.parse(raw) as { type: string; commandId?: string }
        if (command.type === 'heartbeat') return
        commands.push(command)
        window.setTimeout(
          () =>
            this.onmessage?.({
              data: JSON.stringify({
                type: 'command_status',
                commandId: command.commandId,
                status: 'succeeded',
              }),
            }),
          0,
        )
      }

      close() {}
    }

    Object.defineProperty(window, 'WebSocket', { value: RobotSocket })
  }, acquired)
}

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
  await expect(page.getByRole('button', { name: 'Free cam' })).toBeDisabled()
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
  await expect(page.getByRole('heading', { name: 'listing-room' })).toBeVisible({ timeout: 10_000 })
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

test('visitor can run a bounded left-hand free-cam session', async ({ page }) => {
  await installRobotSocket(page)
  await page.goto('/')
  await page.getByRole('button', { name: 'Join tour' }).click()
  await expect(page.getByTestId('connection-status')).toContainText('Robot online')

  await page.getByRole('button', { name: 'Free cam' }).click()
  const panel = page.getByTestId('free-cam-panel')
  await expect(panel).toBeVisible()
  await panel.getByRole('button', { name: 'Start free cam' }).click()
  await expect(panel).toContainText('Left-hand camera')

  await panel.getByRole('button', { name: 'Look right' }).click()
  await expect(panel).toContainText('pan 5°')
  const commands = await page.evaluate(
    () =>
      (
        window as unknown as Window & {
          __realbotCommands: Array<{ action: string; payload: Record<string, unknown> }>
        }
      ).__realbotCommands,
  )
  const actions = commands.map((command) => command.action)
  expect(actions).toContain('free_cam_start')
  expect(actions).toContain('free_cam_pose')
  expect(commands.find((command) => command.action === 'free_cam_start')?.payload).not.toHaveProperty('hand')

  await panel.getByRole('button', { name: 'Exit free cam' }).click()
  await expect(panel).toHaveCount(0)
})

test('view-only visitor cannot issue motion but can still stop', async ({ page }) => {
  await installRobotSocket(page, false)
  await page.goto('/')
  await page.getByRole('button', { name: 'Join tour' }).click()
  await expect(page.getByTestId('control-lease')).toContainText('View only')
  await expect(page.getByRole('button', { name: 'Free cam' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Use action' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Stop' })).toBeEnabled()
  await page.getByRole('button', { name: 'Stop' }).click()
  const commands = await page.evaluate(
    () =>
      (
        window as unknown as Window & {
          __realbotCommands: Array<{ action: string }>
        }
      ).__realbotCommands,
  )
  expect(commands.map((command) => command.action)).toEqual(['stop'])
})
