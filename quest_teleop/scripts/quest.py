"""The Quest controllers: frame data, button edges, the two menus, haptics."""
import time
import contextlib

import numpy as np

from bbos import Reader, Writer, Type


# --- Menus -----------------------------------------------------------------------------------
MENU_NORMAL = 0
MENU_SELECT = 1

# Only X and B change meaning between menus.
MENU_EVENTS = {
    MENU_NORMAL: {"x": "teleop", "b": "home"},
    MENU_SELECT: {"x": "no_headset", "b": "slow"},
}

TRIG_TH = 0.5              # trigger press threshold, 0..1
TOGGLE_HOLD = 2.0          # both triggers held this long -> MENU_SELECT
HOLD_SEC = 1.0             # left-stick hold to lock the height plane
EPISODE_DROP_HOLD_S = 2.0  # hold A this long to drop the episode instead of toggling it


# --- Haptics ---------------------------------------------------------------------------------
# Queued with timestamps and sent one per tick, so gaps survive (the daemon coalesces
# same-tick writes).
HAPTIC_SHORT = {"freq": 160.0, "amp": 0.85, "dur": 0.15}
HAPTIC_LONG = {"freq": 160.0, "amp": 0.85, "dur": 0.45}
HAPTIC_GAP = 0.2
HAPTIC_GAP_LONG = 0.45
# Purr: a rumble that grows with how far you've drifted from where you engaged -> a felt cue
# to come back and use the grip as a clutch.
PURR_FREQ = 160.0
PURR_DUR = 0.12
PURR_AMP_MIN = 0.0
PURR_AMP_MAX = 0.18
PURR_FULL_DRIFT = 0.15  # drift (m) at PURR_AMP_MAX; smoothstep in between
PURR_THRESH = 0.15      # squeeze above which precision/purr engages
PURR_INTERVAL = 0.1     # < dur, so consecutive pulses overlap


class _NullHaptics:
    """Stand-in when quest.haptic is held elsewhere: buf() writes go nowhere."""
    class _Buf:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def __setitem__(self, key, val):
            pass
    def buf(self):
        return _NullHaptics._Buf()


@contextlib.contextmanager
def optional_haptics():
    """The quest.haptic Writer, or a no-op stand-in if another process holds it."""
    try:
        w = Writer("quest.haptic", Type("quest_haptic"), keeptime=False)
    except RuntimeError as e:
        print(f"WARNING: quest.haptic already in use ({e}); running WITHOUT haptic feedback.", flush=True)
        yield _NullHaptics()
        return
    with w:
        yield w


class Quest:
    """The controllers as one object: latest frame, menu, button events, haptics."""

    def __init__(self):
        self.menu = MENU_NORMAL
        # Triggers and squeezes hold between frames; the gripper runs every iteration.
        self.T_head = np.eye(4)
        self.left_pose = np.zeros(7)
        self.right_pose = np.zeros(7)
        self.left_trigger = 0.0
        self.right_trigger = 0.0
        self.left_squeeze = 0.0
        self.right_squeeze = 0.0
        self.left_thumbstick = np.zeros(2)
        self.right_thumbstick = np.zeros(2)
        self.left_stick_click = False
        self.right_stick_click = False
        self.left_a = False      # X
        self.left_b = False      # Y
        self.right_a = False     # A
        self.right_b = False     # B
        self._link_up = True
        self._prev_left_a = False
        self._prev_left_b = False
        self._prev_right_a = False
        self._prev_right_b = False
        self._a_press_time = 0.0
        self._a_consumed = False    # this A hold already dropped
        self._b_consumed = False    # this B press already selected
        self._combo_start = None
        self._armed_buzzed = False
        self._stick_hold_start = None
        self._stick_hold_captured = False
        self._queue = []
        self._next_purr_t = 0.0
        self._purr_toggle = 0       # alternates hands while both grips are held

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._r_link = self._stack.enter_context(Reader("quest.link", Type("quest_link")))
        self._r = self._stack.enter_context(Reader("quest.controllers"))
        self._w_hap = self._stack.enter_context(optional_haptics())
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)

    # --- Frame ---------------------------------------------------------------------------
    def poll(self):
        """Latch a fresh frame. False if the daemon published nothing new."""
        if not self._r.ready():
            return False
        d = self._r.data
        self.T_head = np.asarray(d['T_head'], dtype=np.float64)
        self.left_pose = np.asarray(d['left_pose'], dtype=np.float64)
        self.right_pose = np.asarray(d['right_pose'], dtype=np.float64)
        self.left_trigger = float(d['left_trigger'])
        self.right_trigger = float(d['right_trigger'])
        self.left_squeeze = float(d['left_squeeze'])
        self.right_squeeze = float(d['right_squeeze'])
        self.left_thumbstick = d['left_thumbstick']
        self.right_thumbstick = d['right_thumbstick']
        self.left_stick_click = bool(d['left_thumbstick_click'])
        self.right_stick_click = bool(d['right_thumbstick_click'])
        self.left_a = bool(d['left_a'])
        self.left_b = bool(d['left_b'])
        self.right_a = bool(d['right_a'])
        self.right_b = bool(d['right_b'])
        return True

    def link_edge(self):
        """Headset link liveness: the new state on a change, else None."""
        if not self._r_link.ready():
            return None
        up = bool(self._r_link.data["connected"])
        if up == self._link_up:
            return None
        self._link_up = up
        return up

    # --- Buttons -------------------------------------------------------------------------
    def events(self, no_headset):
        """This frame's presses, in evaluation order — the menu resolves before B and X read it."""
        out = []

        # Y: the app decides dehome vs a second-press ESTOP.
        if self.left_b and not self._prev_left_b:
            out.append("dehome")
        self._prev_left_b = self.left_b

        if self.right_a and not self._prev_right_a:
            self._a_press_time = time.monotonic()
            self._a_consumed = False
        if self.right_a and not self._a_consumed and (time.monotonic() - self._a_press_time) >= EPISODE_DROP_HOLD_S:
            self._a_consumed = True
            out.append("episode_drop")
        if self._prev_right_a and not self.right_a and not self._a_consumed:
            out.append("episode_toggle")
        self._prev_right_a = self.right_a

        # The anchor owns the height in no-headset mode, so the stick is dead there.
        if self.left_stick_click and not no_headset:
            if self._stick_hold_start is None:
                self._stick_hold_start = time.monotonic()
                self._stick_hold_captured = False
            if not self._stick_hold_captured and time.monotonic() - self._stick_hold_start >= HOLD_SEC:
                self._stick_hold_captured = True
                out.append("height_lock")
        else:
            self._stick_hold_start = None
            self._stick_hold_captured = False

        self._update_menu()

        # B selects on the press, homes on the release; a press that selected is consumed.
        if self.right_b and not self._prev_right_b:
            self._b_consumed = False
            if self.menu == MENU_SELECT:
                self._b_consumed = True
                out.append(MENU_EVENTS[MENU_SELECT]["b"])
        if self._prev_right_b and not self.right_b:
            if not self._b_consumed:
                out.append(MENU_EVENTS[MENU_NORMAL]["b"])
            self._b_consumed = False
        self._prev_right_b = self.right_b

        if self.left_a and not self._prev_left_a:
            out.append(MENU_EVENTS[self.menu]["x"])
        self._prev_left_a = self.left_a

        return out

    def _update_menu(self):
        """Both triggers held TOGGLE_HOLD s arms MENU_SELECT; releasing one drops it."""
        if self.left_trigger > TRIG_TH and self.right_trigger > TRIG_TH:
            if self._combo_start is None:
                self._combo_start = time.monotonic()
            if self.menu == MENU_NORMAL and time.monotonic() - self._combo_start >= TOGGLE_HOLD:
                self.menu = MENU_SELECT
                if not self._armed_buzzed:
                    self._armed_buzzed = True
                    self.buzz(HAPTIC_LONG)
                    print(f"MODE SELECT - B = slow, X = no-headset "
                          f"({TOGGLE_HOLD:.0f}s hold)", flush=True)
        else:
            self._combo_start = None
            self.menu = MENU_NORMAL
            self._armed_buzzed = False

    # --- Haptics -------------------------------------------------------------------------
    def buzz(self, pattern, delay=0.0):
        """Queue a pulse ``delay`` seconds out."""
        self._queue.append((time.time() + delay, pattern))

    def send_haptics(self, teleop_active, drift_left, drift_right):
        """One command per tick: a due pulse, else the purr, scaled by each hand's drift (m)."""
        now = time.time()
        if self._queue:
            self._queue.sort(key=lambda x: x[0])
            if self._queue[0][0] <= now:
                _, p = self._queue.pop(0)
                with self._w_hap.buf() as hb:
                    hb["hand"] = 2; hb["frequency"] = p["freq"]; hb["amplitude"] = p["amp"]; hb["duration"] = p["dur"]
                return
        if teleop_active and now >= self._next_purr_t:
            cand = []
            if self.left_squeeze > PURR_THRESH:
                d = min(drift_left / PURR_FULL_DRIFT, 1.0)
                cand.append((0, PURR_AMP_MIN + (PURR_AMP_MAX - PURR_AMP_MIN) * d * d * (3.0 - 2.0 * d)))
            if self.right_squeeze > PURR_THRESH:
                d = min(drift_right / PURR_FULL_DRIFT, 1.0)
                cand.append((1, PURR_AMP_MIN + (PURR_AMP_MAX - PURR_AMP_MIN) * d * d * (3.0 - 2.0 * d)))
            if cand:
                hand, amp = cand[self._purr_toggle % len(cand)]
                self._purr_toggle += 1
                with self._w_hap.buf() as hb:
                    hb["hand"] = hand; hb["frequency"] = PURR_FREQ; hb["amplitude"] = amp; hb["duration"] = PURR_DUR
                self._next_purr_t = now + PURR_INTERVAL
