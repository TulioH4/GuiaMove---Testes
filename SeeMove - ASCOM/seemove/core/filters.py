"""
core/filters.py
One Euro Filter para suavizar coordenadas de landmarks do MediaPipe.

Reduz jitter (tremulação) nos ângulos calculados a partir dos landmarks,
evitando falsos positivos de correção causados por ruído de detecção
frame-a-frame, sem introduzir lag perceptível em movimentos rápidos.

Referência: Casiez, Roussel, Vogel — "1€ Filter: A Simple Speed-based
Low-pass Filter for Noisy Input in Interactive Systems" (CHI 2012).
"""

import math
import threading
from typing import Dict, Optional

from core.skeleton import Point3D, MIN_VIS


class _LowPassFilter:
    def __init__(self):
        self._y: Optional[float] = None

    def filter(self, x: float, alpha: float) -> float:
        if self._y is None:
            self._y = x
        else:
            self._y = alpha * x + (1.0 - alpha) * self._y
        return self._y

    def reset(self):
        self._y = None


class OneEuroFilter:
    """Filtro de passa-baixa adaptativo para um único valor escalar ao longo do tempo."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.3, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta       = beta
        self.d_cutoff   = d_cutoff

        self._x_filter = _LowPassFilter()
        self._dx_filter = _LowPassFilter()
        self._last_t: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def filter(self, x: float, t: float) -> float:
        if self._last_t is None:
            self._last_t = t
            self._x_filter.filter(x, 1.0)
            self._dx_filter.filter(0.0, 1.0)
            return x

        dt = max(1e-6, t - self._last_t)
        self._last_t = t

        prev_x = self._x_filter._y if self._x_filter._y is not None else x
        dx = (x - prev_x) / dt
        edx = self._dx_filter.filter(dx, self._alpha(self.d_cutoff, dt))

        cutoff = self.min_cutoff + self.beta * abs(edx)
        return self._x_filter.filter(x, self._alpha(cutoff, dt))

    def reset(self):
        self._x_filter.reset()
        self._dx_filter.reset()
        self._last_t = None


class LandmarkSmoother:
    """
    Aplica um OneEuroFilter independente para x, y e z de cada landmark.

    Landmarks com baixa visibilidade (< MIN_VIS) não são suavizados nem
    usados para atualizar o filtro — evita que ruído de uma detecção
    perdida contamine o estado do filtro, e evita um "salto" quando a
    detecção volta (o filtro retoma de onde parou, não do zero).

    Efeito colateral disso: se as primeiras leituras (sessão recém-aberta,
    pessoa ainda se posicionando) forem ruins, o filtro "adota" esse estado
    inicial e o corrige lentamente por natureza (é um passa-baixa) — na
    prática pode parecer que o esqueleto "travou" numa pose errada. `reset()`
    descarta todo o estado acumulado para forçar uma leitura do zero.
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.3):
        self.min_cutoff = min_cutoff
        self.beta        = beta
        self._filters: Dict[int, Dict[str, OneEuroFilter]] = {}
        self._lock = threading.Lock()

    def _get(self, idx: int) -> Dict[str, OneEuroFilter]:
        f = self._filters.get(idx)
        if f is None:
            f = {
                "x": OneEuroFilter(self.min_cutoff, self.beta),
                "y": OneEuroFilter(self.min_cutoff, self.beta),
                "z": OneEuroFilter(self.min_cutoff, self.beta),
            }
            self._filters[idx] = f
        return f

    def smooth(self, points: Dict[int, Point3D], timestamp: float) -> Dict[int, Point3D]:
        out: Dict[int, Point3D] = {}
        with self._lock:
            for idx, p in points.items():
                if p.visibility < MIN_VIS:
                    out[idx] = p
                    continue
                f = self._get(idx)
                out[idx] = Point3D(
                    x=f["x"].filter(p.x, timestamp),
                    y=f["y"].filter(p.y, timestamp),
                    z=f["z"].filter(p.z, timestamp),
                    visibility=p.visibility,
                )
        return out

    def reset(self):
        """Descarta todo o estado acumulado — a próxima leitura de cada
        landmark começa do zero, sem carregar nenhum viés de frames antigos."""
        with self._lock:
            self._filters.clear()
