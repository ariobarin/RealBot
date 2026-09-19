"""bb-relay: cloud TCP tunnel that carries the policy gRPC stream between the
robot and the GPU policy server without an SSH tunnel.

Two ends, deployed on different machines:
  - bb_relay.client : robot-side  — binds a local port; ``policy_client`` dials it
  - bb_relay.server : GPU-side     — forwards bytes to the local policy_server

Together with the ``policy_client`` package this is how inference is deployed
over the cloud relay instead of a direct SSH tunnel:

    from bb_relay import run_tunnel, lookup_session
    run_tunnel(session_id, lookup_session(api_key, session_id), api_key)
    # ...then point policy_client at localhost:8080

Re-exports are lazy (PEP 562): importing ``bb_relay`` does not pull in grpc, and
``python -m bb_relay.client`` / ``bb_relay.server`` run without a double-import warning.
"""

import importlib

# attribute name -> submodule it lives in
_LAZY = {
    "run_tunnel": "client",
    "lookup_session": "client",
    "pick_session_interactively": "client",
    "wait_for_gpu_side": "client",
    "run_one_session": "server",
    "wait_for_next_session": "server",
}

__all__ = list(_LAZY)


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f".{mod}", __name__), name)


def __dir__():
    return sorted(__all__)
