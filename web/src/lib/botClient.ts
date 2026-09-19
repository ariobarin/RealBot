/** Command payloads mirror the robot's nav server (nav/main.py) so the mock can be swapped for a WebSocket client. */
export type BotCommand =
  | { type: 'add_wp'; x: number; y: number; h?: number }
  | { type: 'start' }
  | { type: 'stop' }
  | { type: 'clear' }
  | { type: 'wipe_map' }

export interface BotClient {
  sendCommand: (cmd: BotCommand) => void
}

export interface SentCommand {
  id: number
  at: number
  cmd: BotCommand
}

type Listener = (sent: SentCommand) => void

export class MockBotClient implements BotClient {
  private seq = 0
  private listeners = new Set<Listener>()
  readonly log: SentCommand[] = []

  sendCommand(cmd: BotCommand) {
    const sent: SentCommand = { id: ++this.seq, at: Date.now(), cmd }
    this.log.push(sent)
    console.info('[bot ->]', JSON.stringify(cmd))
    for (const l of this.listeners) l(sent)
  }

  subscribe(l: Listener) {
    this.listeners.add(l)
    return () => {
      this.listeners.delete(l)
    }
  }
}

export const botClient = new MockBotClient()
