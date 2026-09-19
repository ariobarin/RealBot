"""Live halt/home + task controls for a RobotClient.

Kept out of ``policy_client`` so robot_client.py stays byte-identical with the
copy in bb-ml; the client exposes only generic hooks.
"""

import sys
import threading

from policy_client.async_inference.robot_client import dbg_quiet


class LiveControls:
    """Halt+home / resume, applied on the control-loop thread via ``on_tick`` so
    ``robot.home()`` never races ``send_action``. Call ``toggle_home()`` from anywhere.
    """

    def __init__(self, client):
        self.client = client
        self._toggle = threading.Event()
        client.on_tick = self._tick

    @property
    def paused(self) -> bool:
        return self.client.paused

    def toggle_home(self) -> None:
        """Signal the control loop to halt+home (or resume). Thread-safe."""
        self._toggle.set()

    def set_task(self, task: str) -> None:
        """Swap the prompt sent with future observations. Thread-safe."""
        self.client.task = task

    def _tick(self) -> None:
        if self._toggle.is_set():
            self._handle_toggle()

    def _handle_toggle(self) -> None:
        """Drops queued (stale) actions on both transitions."""
        self._toggle.clear()
        client = self.client
        if not client.paused:
            # Halt: pause FIRST so receive_actions starts discarding chunks, then
            # drop whatever is already queued and home the arms.
            client.paused = True
            client.clear_action_queue()
            client.logger.info("Halt — stopping server actions and homing arms")
            if hasattr(client.robot, "home"):
                client.robot.home()
            client.logger.info("Homed — paused; press 'h' to resume")
        else:
            # Resume: clear the queue while still paused (the receiver is still
            # dropping chunks), then re-arm must_go and unpause LAST, so no stale
            # in-flight chunk can repopulate the queue before we restart from a
            # fresh observation.
            client.clear_action_queue()
            client.must_go.set()
            client.paused = False
            client.logger.info("Resuming inference")


def _key_listener(client, controls: LiveControls) -> None:
    """Read single keystrokes from the TTY and drive live controls:
      h  halt + home the arms (press again to resume)
      t  type a new task / language prompt
    No-op if stdin is not a TTY."""
    import atexit
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    # Runs as a daemon thread, so its finally may not fire when the process exits
    # via the SIGINT handler; atexit guarantees the terminal is restored to cooked
    # mode (otherwise the shell is left with echo/line-buffering off).
    atexit.register(termios.tcsetattr, fd, termios.TCSADRAIN, old)
    try:
        tty.setcbreak(fd)
        while client.running:
            try:
                if select.select([sys.stdin], [], [], 0.2)[0]:
                    ch = sys.stdin.read(1)
                    if ch == "h":
                        controls.toggle_home()
                    elif ch == "t":
                        dbg_quiet.set()  # mute action/state spam while typing
                        termios.tcsetattr(fd, termios.TCSADRAIN, old)
                        try:
                            new_task = input(
                                f"\n[task] current: {client.task!r}\n[task] new prompt: "
                            ).strip()
                        finally:
                            tty.setcbreak(fd)
                            dbg_quiet.clear()
                        if new_task:
                            controls.set_task(new_task)
                            print(f"[task] now: {new_task!r}", flush=True)
            except Exception as e:  # noqa: BLE001 — one bad keystroke must not kill live controls
                client.logger.error(f"key listener error (continuing): {e}")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def attach(client, keyboard: bool = True) -> LiveControls:
    """Wire live controls onto ``client``; start the TTY listener if attached."""
    controls = LiveControls(client)
    if keyboard and sys.stdin.isatty():
        threading.Thread(target=_key_listener, args=(client, controls), daemon=True).start()
        client.logger.info("Live keys: 'h' halt+home / resume, 't' change task prompt")
    return controls
