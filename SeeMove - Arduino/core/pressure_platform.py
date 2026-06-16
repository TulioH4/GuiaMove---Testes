"""
core/pressure_platform.py
Tipos compartilhados da plataforma de pressao e simulador.
"""

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class SensorData:
    top_left: float
    top_right: float
    bottom_left: float
    bottom_right: float
    timestamp: float = field(default_factory=time.time)

    def total(self) -> float:
        return self.top_left + self.top_right + self.bottom_left + self.bottom_right


class PressurePlatformSimulator:
    def __init__(
        self,
        exercise: str = "squat",
        on_data: Optional[Callable[[SensorData], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        rate_hz: float = 10.0,
    ):
        self.exercise = exercise
        self.on_data = on_data
        self.on_status = on_status
        self._rate = rate_hz
        self._tick = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def connect(self) -> bool:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if self.on_status:
            self.on_status(f"Simulador iniciado ({self.exercise})")
        return True

    def _loop(self):
        interval = 1.0 / self._rate
        while self._running:
            time.sleep(interval)
            self._tick += 1
            tl, tr, bl, br = PATTERNS.get(self.exercise, _stand_pattern)(self._tick)
            if self.on_data:
                self.on_data(
                    SensorData(
                        max(0.0, tl),
                        max(0.0, tr),
                        max(0.0, bl),
                        max(0.0, br),
                    )
                )

    def read(self) -> Optional[SensorData]:
        return None

    def disconnect(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)


def _squat_pattern(t):
    n = lambda s=1.5: random.gauss(0, s)
    return (
        18 + math.sin(t * 0.08) * 6 + n(),
        17 + math.sin(t * 0.08 + 0.3) * 6 + n(),
        15 + math.cos(t * 0.08) * 4 + n(),
        20 + math.sin(t * 0.05) * 5 + n(),
    )


def _balance_pattern(t):
    n = lambda s=2.5: random.gauss(0, s)
    return (
        5 + math.sin(t * 0.12) * 4 + n(),
        30 + math.sin(t * 0.09) * 8 + n(),
        4 + math.cos(t * 0.11) * 3 + n(),
        31 + math.cos(t * 0.07) * 7 + n(),
    )


def _stand_pattern(t):
    n = lambda: random.gauss(0, 0.8)
    return 17 + n(), 18 + n(), 17 + n(), 18 + n()


def _lunge_pattern(t):
    n = lambda s=2.0: random.gauss(0, s)
    return (
        25 + math.sin(t * 0.06) * 10 + n(),
        10 + math.sin(t * 0.06 + math.pi) * 5 + n(),
        15 + math.cos(t * 0.05) * 8 + n(),
        8 + math.cos(t * 0.05 + math.pi) * 4 + n(),
    )


PATTERNS = {
    "squat": _squat_pattern,
    "balance": _balance_pattern,
    "stand": _stand_pattern,
    "lunge": _lunge_pattern,
}
