"""Robot-authoritative runtime primitives for RealBot."""

from .agent import EdgeAgent
from .motion import MotionCoordinator, MotionMode

__all__ = ["EdgeAgent", "MotionCoordinator", "MotionMode"]
