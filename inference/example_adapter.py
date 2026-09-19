"""Template for plugging your own robot into the policy-inference client.

Like a .env.example: copy this to ``<yourrobot>_adapter.py``, implement every
member below, and point live_inference at it. No working code lives here — only
the contract the policy client requires.

The policy client (policy_client) drives ANY object exposing the six members
below; the formal definition is the ``Robot`` protocol in
policy_client/async_inference/robot_client.py. See bracketbot_adapter.py for a
real implementation.

Data conventions to match (so the policy sees the shape the model trained on):
  - observation_features / action_features: dicts naming each signal.
      * scalar joints  -> float
      * camera frames  -> (height, width, channels) tuple
  - get_observation(): returns {feature_name: value}; joint values are floats in
    the policy's normalized range, camera values are raw encoded-JPEG bytes.
  - send_action(action): action is {joint_name: float} in that same normalized
    range; map it back to your hardware units and command the robot.

This module is plain functions (a module satisfies the protocol). You may equally
expose the same six members from a class instance.
"""

# --- The shape of every signal the policy reads / writes ---------------------

#: e.g. {"joint_0": float, ..., "head": (480, 640, 3)}
observation_features: dict = {}

#: e.g. {"joint_0": float, ...}  (must match the action space the policy outputs)
action_features: dict = {}


# --- Lifecycle ----------------------------------------------------------------

def connect() -> None:
    """Open hardware/streams and enable actuators. Called once before inference.

    Install a SIGINT/SIGTERM handler here if you need a safe stop (e.g. disable
    torque) on Ctrl-C.
    """
    raise NotImplementedError


def disconnect() -> None:
    """Disable actuators and release hardware/streams. Called once at shutdown."""
    raise NotImplementedError


# --- Per-step I/O -------------------------------------------------------------

def get_observation() -> dict:
    """Return the latest observation as {feature_name: value} matching
    observation_features (joint floats + raw JPEG bytes per camera)."""
    raise NotImplementedError


def send_action(action: dict) -> dict:
    """Execute one action ({joint_name: float} matching action_features) on the
    hardware, in the policy's normalized range. Return the action performed."""
    raise NotImplementedError
