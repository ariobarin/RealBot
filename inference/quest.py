"""Quest-driven control on top of inference, all from the headset.

Used in place of run_client by live_inference / vlm_inference. The policy drives the arms; headset and keyboard
actions interrupt it without ever stopping it -- inference is only PAUSED, so nothing re-homes or parks:

  - X (left_a): freeze the arms and jog them along the recent command history -- push the left stick
    back/down to go backward in time, up/forward to go forward again; click X again to resume from that pose.
  - LEFT-stick click: freeze the arms in place and hand them to quest_teleop (drive it normally, X to
    engage); click the RIGHT stick to take the arms back and resume the policy from where they are.
  - Keyboard (terminal): press S to start inference and S again to pause it; press H to ramp the arms
    home and pause. Inference starts PAUSED, so the arms hold at home until you press S -- nothing
    moves on its own.

Each transition buzzes both controllers so you feel where you are without watching the terminal: one tap
entering rewind, two taps leaving it; one long buzz handing off to teleop, two taps taking the arms back.

Both run inline on the control loop, so the policy is paused (not stopped) while they do, then picks up
right where the arms ended up. The handoff hands arm.ctrl/arm.torque to quest_teleop and back; the arms
stay energized across it because quest.py keeps holding the frozen pose until quest_teleop has booted and
is waiting for the writers, so they change hands with the arms already up (no unheld gap, no daemon change).
"""

import collections
import os
import re
import signal
import subprocess
import sys
import threading
import time

import numpy as np
from bbos import Reader, Writer, Type

from policy_client.async_inference.robot_client import RobotClient

APPS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How much arm-command history to keep, in seconds.
WINDOW_S = 30

# Left stick scrubs the arms live through the log: how far you push = how far back.
# Small deadzone just to ignore the stick's resting jitter (tune on hardware).
REWIND_DEADZONE = 0.05

# Cap how far the arms move per scrub tick (joint units) so a big/fast push eases in instead of
# jerking. ~0.3 turns/s at 30 fps; tune on hardware -- lower = slower and smoother.
SCRUB_MAX_STEP = 0.01

# Stick magnitude that counts as "all the way". A real stick rarely reads a clean 1.0, so anything
# past this saturates to a full push -- guarantees the oldest sample is reachable, not just short of it.
REWIND_FULL_PUSH = 0.9

# Rewind jog: how many logged samples the playhead travels per tick at full push. The arm still eases
# toward the playhead pose capped by SCRUB_MAX_STEP, so this just scales how fast the jog scans the
# frozen log with how hard you push. Tune on hardware.
REWIND_JOG_STEP = 3.0

# quest_teleop prints this line the instant it's booted and reaching for the arm writers; we release
# the arms right then instead of waiting out a timer. Must match READY_LINE in quest_teleop/main.py.
READY_LINE = "QUEST_TELEOP_READY"

# Safety cap on the freeze: if quest_teleop hasn't signalled ready within this long it's assumed dead,
# so we abort the handoff and resume inference rather than release the arms to nobody. Normal handoff
# releases as soon as it's up (usually well under this), so this only bounds the failure case.
HANDOFF_BOOT_S = 45.0


def _reap(p):
    """SIGKILL the process group, then wait until every member exits -- `uv run` spawns the python
    child that holds arm.ctrl, and p.wait() only reaps uv, so the resume would otherwise race a
    still-dying child for arm.ctrl."""
    try:
        os.killpg(p.pid, signal.SIGKILL)   # p.pid == group leader (start_new_session)
    except ProcessLookupError:
        return
    p.wait()
    while True:
        try:
            os.killpg(p.pid, 0)
            time.sleep(0.02)
        except ProcessLookupError:
            return


def _banner(text):
    # big all-caps state banner on the inference terminal (INFERENCE / REWIND / TELEOP / ...)
    bar = "=" * 64
    print(f"\n{bar}\n{text.upper().center(64)}\n{bar}\n", flush=True)


# Haptic cues on each mode change, felt on both controllers. A "tap" is one short buzz; the daemon
# forwards one packet per write, so a multi-tap is just packets spaced by a silent gap. Patterns:
# enter rewind = one tap, leave rewind = two taps; enter teleop = one long buzz, back to inference = two taps.
HAPTIC_FREQ = 160.0     # Hz; matches the quest daemon's default pulse.
HAPTIC_AMP = 0.6        # 0..1 strength.
TAP_DUR = 0.08          # a short tap, seconds.
LONG_DUR = 0.35         # the single long buzz for entering teleop.
HAPTIC_GAP = 0.12       # silence between taps so a two-tap reads as two, not one.


def _buzz(pulses):
    """Fire a short haptic pattern on both controllers. pulses = list of (amp, dur) seconds, played
    in order with a silent gap between them (one tap, two taps, one long buzz...). Opens the haptic
    writer only for the moment of the buzz, so quest_teleop can own it the rest of the time -- we
    only ever buzz at transitions, when teleop isn't reaching for it. Never crashes the run: if the
    channel is held elsewhere or the write fails, the cue is just skipped."""
    try:
        w = Writer("quest.haptic", Type("quest_haptic"), keeptime=False)
    except RuntimeError:
        return
    try:
        with w:
            for amp, dur in pulses:
                with w.buf() as b:
                    b["hand"] = 2             # both controllers
                    b["frequency"] = HAPTIC_FREQ
                    b["amplitude"] = amp
                    b["duration"] = dur
                time.sleep(dur + HAPTIC_GAP)  # let the daemon forward + the buzz play before the next
    except Exception:
        pass


class Quest:
    def __init__(self, config, robot, driver=None, eval_dataset=None):
        # config = RobotClientConfig (fps, checkpoint, ...); robot = the adapter that owns the arm
        # writers; driver = optional per-run task hook (VLM only). eval_dataset = "{runId}_{step}" to
        # record inference episodes under eval_rollouts/, or None when the checkpoint isn't a logged run.
        self.config, self.robot, self.driver = config, robot, driver
        self._eval_dataset = eval_dataset
        # Rolling log of recent arm commands, one per control tick -- a tick is one pass of the
        # policy loop, ~fps times a second. Oldest fall off; maxlen = fps * seconds = ticks kept.
        self.ctrl_log = collections.deque(maxlen=int(config.fps * WINDOW_S))

    def inference(self):
        """Run the policy. X rewinds the arms through the command history; a left-stick click hands
        them to quest_teleop and back. Both happen inline (see _on_tick), so inference is paused for
        them, never stopped -- it resumes right where the arms are."""
        c = RobotClient(self.config, self.robot)
        if not c.start():
            return
        # background thread: fetches action chunks from the policy server into the client's queue
        threading.Thread(target=c.receive_actions, daemon=True).start()
        stop = threading.Event()
        self.ctrl_log.clear()
        self._pending = None              # "rewind"/"handoff" latched by the watcher thread, run by on_tick
        self._busy = False                # an action is running -> the watcher won't latch a new one
        self._recording = False           # eval episode open? toggled by S when the checkpoint is a logged run
        # dataset.flag writer, only when this checkpoint is a logged eval run -- lets S record episodes to
        # eval_rollouts/{runId}_{step}. Released to quest_teleop during a handoff (single-writer), retaken after.
        self._ds = (Writer("dataset.flag", Type("dataset_flag")).__enter__()
                    if self._eval_dataset else None)
        # Watch the left controls (X + left-stick click) on their own steady 50Hz reader, so a single
        # click always registers -- on_tick only samples at the inference loop's rate, which can lag.
        threading.Thread(target=self._watch_left, args=(c,), daemon=True).start()
        # Keyboard controls on their own raw-stdin reader: S start/pause, H home. On a real terminal only.
        threading.Thread(target=self._watch_keys, args=(c,), daemon=True).start()
        # Start paused only when a keyboard is present to start it (S). A non-tty run (service/pipe) has
        # no way to unpause, so it keeps the old auto-start behavior -- the policy drives immediately.
        c.paused = sys.stdin.isatty()
        _banner("INFERENCE PAUSED -- press S to start" if c.paused else "INFERENCE")
        if self._eval_dataset:
            print(f"[eval] recording armed -> eval_rollouts/{self._eval_dataset} (S records / stops)", flush=True)
        # Open the topics we read each tick (a Reader is a live subscription; the `with` auto-closes
        # them when the run ends). keeptime=False = latest value on demand, we do our own timing.
        # rl/rr = the two arm commands (to log); rq = the controllers (X, the sticks, the clicks).
        with Reader("arm_left.ctrl", keeptime=False) as rl, \
             Reader("arm_right.ctrl", keeptime=False) as rr, \
             Reader("quest.controllers", keeptime=False) as rq:
            # on_tick is a hook the control loop calls once every tick, on the loop's own thread --
            # in step with the policy's arm writes. We handle both headset actions there so they run
            # inline, pausing the policy while they do (it can't write arm.ctrl at the same time).
            c.on_tick = lambda: self._on_tick(c, rl, rr, rq)
            # VLM only: a background thread that swaps the task prompt as the run goes (stop ends it).
            if self.driver:
                threading.Thread(target=self.driver, args=(c, stop), daemon=True).start()
            # Run the policy. Blocks here (logging / rewind / handoff all happen via on_tick) until
            # Ctrl-C.
            try:
                c.control_loop()
            except KeyboardInterrupt:
                pass
            stop.set()                        # tell the driver thread to quit
            c.stop()                          # stop the client
        if self._ds is not None:              # close any open episode, then release dataset.flag
            self._rec_stop()
            self._ds.__exit__(None, None, None)
            self._ds = None
        self.ctrl_log.clear()                 # empty the log on the way out (readers close with the block)

    def run(self):
        self.inference()

    def _on_tick(self, client, rl, rr, rq):
        """Runs once per control tick. If the left-control watcher latched an action (X -> rewind,
        left-stick -> handoff), run it here on the control-loop thread so it can't race the policy's arm
        writes; the blocking handler is what pauses the policy. Any other tick just logs the command."""
        action, self._pending = self._pending, None
        if action:
            self._busy = True                 # tell the watcher not to latch another while this runs
            try:
                if action == "rewind":
                    self._rewind_session(client, rq)
                elif action == "handoff":
                    self._handoff(client)
                elif action == "home":
                    self._home(client)
                elif action == "toggle":
                    self._toggle_pause(client)
            finally:
                self._busy = False
            return
        if not client.paused:                 # don't fill the rewind log with the held pose while paused
            self._log_ctrl(rl, rr)

    def _watch_left(self, client):
        """Latch left-control clicks on their own 50Hz reader (X -> rewind, left-stick -> handoff), so a
        single press is never missed when the inference loop ticks slowly. on_tick runs the latched
        action; we don't latch another while one is running (busy) or one is already pending."""
        with Reader("quest.controllers", keeptime=False) as r:
            prev_x = prev_click = False
            while client.running:
                if r.ready():
                    d = r.data
                    x, click = bool(d["left_a"]), bool(d["left_thumbstick_click"])
                    if self._pending is None and not self._busy:
                        if x and not prev_x:
                            self._pending = "rewind"
                        elif click and not prev_click:
                            self._pending = "handoff"
                    prev_x, prev_click = x, click
                time.sleep(0.02)

    def _watch_keys(self, client):
        """Latch keyboard commands from a raw (cbreak) stdin, so a single keypress needs no Enter: S
        toggles inference start/pause, H homes the arms and pauses. Uses the same _pending the headset
        watcher does, so on_tick runs the action on the control-loop thread. Skipped when stdin isn't a
        terminal (piped/service run), so it never touches a non-interactive process."""
        if not sys.stdin.isatty():
            return
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while client.running:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1).lower()
                    if self._pending is None and not self._busy:
                        if ch == "s":
                            self._pending = "toggle"
                        elif ch == "h":
                            self._pending = "home"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)   # restore the terminal on the way out

    def _toggle_pause(self, client):
        """S: start the policy if paused, else pause and hold. Paused = the arms stay put with torque on,
        so nothing moves until S is pressed again."""
        if client.paused:
            client.paused = False                    # arms held in place while paused -> queued actions still fit
            _banner("INFERENCE RUNNING")
            self._rec_start()                        # S also starts an eval episode (if this run is logged)
        else:
            client.paused = True
            self._freeze()                           # hold in place, torque on
            self._rec_stop()                         # ...and stops it
            _banner("INFERENCE PAUSED")

    def _rec_start(self):
        """Open an eval episode: one dataset.flag toggle under eval_rollouts/{runId}_{step}. No-op if this
        run isn't recording (checkpoint not logged) or an episode is already open. The daemon's flag reader
        is new-data-gated, so one write = one flip -- no reset pulse needed."""
        if self._ds is None or self._recording:
            return
        with self._ds.buf() as b:
            b["prefix"] = b"eval_rollouts"
            b["name"] = self._eval_dataset.encode()
            b["text"] = (getattr(self.config, "task", "") or "").encode()
            b["toggle_episode"] = True
            b["drop_episode"] = False
        self._recording = True
        print(f"[eval] RECORDING episode -> eval_rollouts/{self._eval_dataset}", flush=True)

    def _rec_stop(self):
        """Close the current eval episode (another toggle). No-op if nothing is recording."""
        if self._ds is None or not self._recording:
            return
        with self._ds.buf() as b:
            b["prefix"] = b"eval_rollouts"
            b["name"] = self._eval_dataset.encode()
            b["toggle_episode"] = True
            b["drop_episode"] = False
        self._recording = False
        print("[eval] episode stopped", flush=True)

    def _home(self, client):
        """H: pause the policy, ramp both arms to their home pose (torque stays on), and hold there.
        Press S to resume the policy from home. Runs on the control-loop thread, so it can't race the
        policy's arm writes -- and the policy is paused throughout anyway."""
        _banner("HOMING")
        client.paused = True
        self.robot.home()                            # adapter ramps both arms to cfg.home, blocking, torque on
        with client.action_queue_lock:               # arm moved -> drop stale actions so resume re-plans from home
            client.action_queue.queue.clear()
        _banner("INFERENCE PAUSED")

    def _log_ctrl(self, rl, rr):
        """Append this tick's arm command to the log."""
        if rl.ready() and rr.ready():
            self.ctrl_log.append((np.array(rl.data["pos"], np.float32),
                                  np.array(rr.data["pos"], np.float32)))

    def _rewind_session(self, client, rq):
        """Started by an X click: freeze the arms in place, then jog them along the frozen history with
        the left stick -- push back/down to travel backward in time, up/forward to travel forward again,
        both bounded by the frozen log; harder push = faster, neutral holds. Click X again to resume
        inference from wherever the arms ended up, or click the left stick to hand that pose straight to teleop.

        Runs on the control-loop thread (from on_tick), so the policy is paused the whole time. Torque
        is already on from inference, so re-commanding the held pose every tick is what freezes the arms.
        We reuse the adapter's ctrl writers -- never open our own, never touch the adapter."""
        writers = getattr(self.robot, "_writers", {})
        log = list(self.ctrl_log)                     # freeze the history so it can't shift mid-session
        if len(log) < 2 or "ctrl_l" not in writers:
            return                                    # nothing logged yet, or not connected -> do nothing
        wl, wr = writers["ctrl_l"], writers["ctrl_r"]
        _banner("REWIND MODE ACTIVATED")
        _buzz([(HAPTIC_AMP, TAP_DUR)])                  # entered rewind: one tap
        cmd_l, cmd_r = log[-1]                         # commanded pose; starts frozen at the current pose
        i = float(len(log) - 1)                        # playhead into the frozen log; starts at the newest
        prev_x = True                                 # X is down now (we entered on its click)
        prev_click = False                            # left-stick not clicked yet this session
        to_handoff = False                            # set if the user clicks left-stick -> go to teleop
        while client.running:
            if rq.ready():
                d = rq.data
                if bool(d["left_a"]) and not prev_x:  # X clicked again -> resume inference from here
                    break
                click = bool(d["left_thumbstick_click"])
                if click and not prev_click:          # left-stick click -> hand straight to teleop from here
                    to_handoff = True
                    break
                prev_x, prev_click = bool(d["left_a"]), click
                fwd = -float(d["left_thumbstick"][1])  # stick up = forward in time (+), down/back = older (-)
                if abs(fwd) > REWIND_DEADZONE:         # past the deadzone -> jog the playhead; neutral -> hold
                    # how hard you push sets the jog speed (deadzone..full -> 0..1); the sign sets the way
                    frac = min((abs(fwd) - REWIND_DEADZONE) / (REWIND_FULL_PUSH - REWIND_DEADZONE), 1.0)
                    i = float(np.clip(i + np.sign(fwd) * frac * REWIND_JOG_STEP, 0, len(log) - 1))
                target_l, target_r = log[round(i)]
                # ease the command toward the playhead pose, capped per tick so it can't jerk
                cmd_l = cmd_l + np.clip(target_l - cmd_l, -SCRUB_MAX_STEP, SCRUB_MAX_STEP)
                cmd_r = cmd_r + np.clip(target_r - cmd_r, -SCRUB_MAX_STEP, SCRUB_MAX_STEP)
                # command the (jogged-or-held) pose every tick, so the arms stay put between pushes
                if wl.ready():
                    wl["pos"] = cmd_l.astype(np.float32)
                if wr.ready():
                    wr["pos"] = cmd_r.astype(np.float32)
            time.sleep(1.0 / self.config.fps)         # re-read the stick once per control tick
        if to_handoff:                                # left-stick in rewind -> teleop takes the rewound pose
            self._handoff(client)                     # does its own TELEOP banner + long buzz, resumes on right-click
            return
        _banner("INFERENCE")
        _buzz([(HAPTIC_AMP, TAP_DUR), (HAPTIC_AMP, TAP_DUR)])   # back to inference: two taps
        # drop the policy's stale queued commands so it re-plans from the frozen pose, then resume
        with client.action_queue_lock:
            client.action_queue.queue.clear()

    def _handoff(self, client):
        """Started by a left-stick click: freeze the arms in place, hand them to quest_teleop, take
        them back on the right stick, then resume the policy. Runs on the control-loop thread (from
        on_tick), so the policy is paused the whole time and resumes right where the arms are."""
        _banner("TELEOP DAGGER")
        _buzz([(HAPTIC_AMP, LONG_DUR)])       # entering teleop: one long buzz (before teleop grabs the haptic)
        client.paused = True                  # explicit pause: policy stops driving + stops queuing actions
        self._freeze()                        # hold the current pose, torque on -- arms up while we hold arm.ctrl
        # Recording uses dataset.flag too (single-writer) -- close the episode and hand the flag to
        # quest_teleop so it can record dagger; we take it back after the handoff.
        was_recording = self._recording
        self._rec_stop()
        if self._ds is not None:
            self._ds.__exit__(None, None, None)
            self._ds = None
        # Capture quest_teleop's output on a pipe (off the inference terminal, so it stays quiet and
        # clearly reads as paused) and watch it for the ready line. --no-startup-home: hold pose at load.
        # --dataset names any episodes it records dagger__<ckpt>__<step> under teleop's normal prefix,
        # so DAgger takes land in the Quest Teleop table; only the name changes, upload is untouched.
        p = subprocess.Popen(["uv", "run", "quest_teleop/main.py", "--no-startup-home",
                              "--dataset", self._dagger_name()],
                             cwd=APPS, start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ready = threading.Event()
        tail = collections.deque(maxlen=20)   # keep recent lines to show if it dies before it's ready
        def drain():                          # read the pipe to EOF so quest_teleop never blocks on it
            for line in p.stdout:
                tail.append(line.rstrip())
                if READY_LINE in line:
                    ready.set()
            p.stdout.close()
        threading.Thread(target=drain, daemon=True).start()
        # Keep holding the frozen arms until quest_teleop signals it's booted and reaching for the
        # writers, then release -- it grabs them with the arms already up, no unheld gap.
        released = False
        if self._wait_ready(p, ready):
            self._release_arm_writers()       # hand over; quest_teleop's writer_wait grabs immediately
            released = True
            self._wait("right_thumbstick_click", lambda: p.poll() is None)   # drive until the right stick
        else:
            print(f"[handoff] quest_teleop didn't come up in {HANDOFF_BOOT_S:.0f}s -- staying in "
                  f"inference. last output:", flush=True)
            for line in tail:
                print(f"    {line}", flush=True)
        _reap(p)
        if released:
            self._reopen_arm_writers()        # take arm.ctrl/arm.torque back (still ours if we never released)
        if self._eval_dataset:                # take dataset.flag back (quest_teleop is reaped, so it's free)
            self._ds = Writer("dataset.flag", Type("dataset_flag")).__enter__()
        self._freeze()                        # hold where teleop left the arms
        with client.action_queue_lock:        # drop stale actions so the policy re-plans from here
            client.action_queue.queue.clear()
        client.paused = False                 # resume the policy from the frozen pose
        if was_recording:
            self._rec_start()                 # resume recording a fresh episode if it was on before
        _banner("INFERENCE")
        _buzz([(HAPTIC_AMP, TAP_DUR), (HAPTIC_AMP, TAP_DUR)])   # back to inference: two taps (teleop freed the haptic)

    def _wait_ready(self, p, ready):
        """Wait for quest_teleop's ready line (booted, reaching for the arms). True once it comes;
        False if it exits first or doesn't show up within HANDOFF_BOOT_S (assume it died)."""
        deadline = time.monotonic() + HANDOFF_BOOT_S
        while time.monotonic() < deadline:
            if ready.is_set():                # got the ready line
                return True
            if p.poll() is not None:          # quest_teleop exited before it was ready
                return False
            time.sleep(0.05)
        return False

    def _freeze(self):
        """Hold both arms where they are, with torque on -- two writers: arm.ctrl (command the joints
        to stay put) + arm.torque (keep them energized). This is what stops them from dropping."""
        for side, arm in (("l", "arm_left"), ("r", "arm_right")):
            # read where the arm actually is now (arm.state), then command it to stay there
            with Reader(f"{arm}.state", keeptime=False) as r:
                while not r.ready():
                    time.sleep(0.005)                            # wait for the first reading
                pos = np.array(r.data["pos"], np.float32)
            self.robot._writers[f"ctrl_{side}"]["pos"] = pos     # command = current pose (hold)
            with self.robot._writers[f"torque_{side}"].buf() as b:
                b["enable"][:] = np.ones(len(pos), np.bool_)     # torque on for every joint

    def _release_arm_writers(self):
        """Close the adapter's arm.ctrl / arm.torque writers so quest_teleop can open them."""
        for key in ("ctrl_l", "ctrl_r", "torque_l", "torque_r"):
            self.robot._writers[key].__exit__(None, None, None)

    def _reopen_arm_writers(self):
        """Re-open the arm writers we released, so the policy can drive again after teleop."""
        w = self.robot._writers
        w["ctrl_l"]   = Writer("arm_left.ctrl",    Type("arm_ctrl")).__enter__()
        w["ctrl_r"]   = Writer("arm_right.ctrl",   Type("arm_ctrl")).__enter__()
        w["torque_l"] = Writer("arm_left.torque",  Type("arm_torque")).__enter__()
        w["torque_r"] = Writer("arm_right.torque", Type("arm_torque")).__enter__()

    def _dagger_name(self):
        """Dataset name for DAgger recordings: dagger__<ckpt>__<step>. From a checkpoint path like
        '.../<name>/checkpoints/<step>/pretrained_model' -> 'dagger__<name>__<step>' (name, not the path)."""
        p = (getattr(self.config, "pretrained_name_or_path", "") or os.environ.get("CHECKPOINT", "")).rstrip("/")
        # Drop the trailing "pretrained_model" dir AND a "_pretrained_model" the step dir may carry -- some
        # checkpoints name the step dir "<step>_pretrained_model" with a pretrained_model/ inside it.
        for suf in ("/pretrained_model", "_pretrained_model"):
            if p.endswith(suf):
                p = p[: -len(suf)]
        # last two path parts = <name> and <step> (the "checkpoints" dir between them is dropped)
        parts = [re.sub(r"[^A-Za-z0-9._-]", "-", s) for s in p.split("/") if s and s != "checkpoints"]
        return "dagger__" + "__".join(parts[-2:] or ["unknown"])

    def _wait(self, field, alive):
        """Return True on a rising-edge click of `field`; False if alive() goes false first."""
        with Reader("quest.controllers", keeptime=False) as r:
            prev = False
            while alive():
                if r.ready():
                    down = bool(r.data[field])
                    if down and not prev:
                        return True
                    prev = down
                time.sleep(0.02)
        return False
