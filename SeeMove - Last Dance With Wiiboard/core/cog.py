"""
core/cog.py
Cálculo do Centro de Gravidade (CoG) a partir dos 4 sensores de pressão.

O CoG é normalizado no intervalo [-1, +1] para cada eixo:
  X: -1 = todo peso à esquerda, +1 = todo peso à direita
  Y: -1 = todo peso atrás,      +1 = todo peso à frente

Além do CoG instantâneo, o módulo fornece:
  - CoGHistory: buffer circular para histórico temporal
  - CoGStats: estatísticas da sessão (média, desvio padrão, percentil centralizado)
"""

import math
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from core.balance_board import SensorData


@dataclass
class CoGReading:
    """Resultado do cálculo de CoG para um instante."""
    x: float
    y: float
    total_kg: float
    timestamp: float
    magnitude: float = field(init=False)
    is_centered: bool = field(init=False)

    THRESHOLD: float = 0.15

    def __post_init__(self):
        self.magnitude = math.sqrt(self.x ** 2 + self.y ** 2)
        self.is_centered = self.magnitude <= self.THRESHOLD

    def stability_pct(self) -> int:
        """Percentual de estabilidade: 100% = centro exato, 0% = máximo desvio."""
        return max(0, round((1.0 - self.magnitude) * 100))

    def quadrant(self) -> str:
        """Retorna o quadrante do CoG em texto."""
        if abs(self.x) <= self.THRESHOLD and abs(self.y) <= self.THRESHOLD:
            return "centro"
        h = "direita" if self.x > 0 else "esquerda"
        v = "frente" if self.y > 0 else "trás"
        return f"{v}-{h}"


def calculate_cog(data: SensorData, threshold: float = 0.15) -> Optional[CoGReading]:
    """
    Calcula o Centro de Gravidade a partir de uma leitura dos sensores.

    Fórmulas:
        total = TL + TR + BL + BR
        X = ((TR + BR) - (TL + BL)) / total
        Y = ((TL + TR) - (BL + BR)) / total

    Retorna None se o peso total for insuficiente (< 5 kg),
    evitando divisão por zero e leituras espúrias.
    """
    tl, tr, bl, br = data.top_left, data.top_right, data.bottom_left, data.bottom_right
    total = tl + tr + bl + br

    if total < 5.0:
        return None

    x = ((tr + br) - (tl + bl)) / total
    y = ((tl + tr) - (bl + br)) / total

    # Clampeia para [-1, 1] por segurança numérica
    x = max(-1.0, min(1.0, x))
    y = max(-1.0, min(1.0, y))

    reading = CoGReading.__new__(CoGReading)
    reading.x = round(x, 4)
    reading.y = round(y, 4)
    reading.total_kg = round(total, 2)
    reading.timestamp = data.timestamp
    reading.THRESHOLD = threshold
    reading.__post_init__()
    return reading


class CoGHistory:
    """
    Buffer circular para histórico temporal do CoG.
    Útil para suavização, detecção de tendências e visualização.
    """

    def __init__(self, max_size: int = 300):
        self.max_size = max_size
        self._buffer: deque = deque(maxlen=max_size)

    def push(self, reading: CoGReading):
        self._buffer.append(reading)

    def __len__(self):
        return len(self._buffer)

    def last_n(self, n: int) -> List[CoGReading]:
        """Retorna os últimos N readings."""
        buf = list(self._buffer)
        return buf[-n:] if len(buf) >= n else buf

    def smoothed(self, window: int = 5) -> Optional[Tuple[float, float]]:
        """
        Média móvel dos últimos N readings.
        Reduz ruído dos sensores para feedback mais estável.
        """
        recent = self.last_n(window)
        if not recent:
            return None
        avg_x = sum(r.x for r in recent) / len(recent)
        avg_y = sum(r.y for r in recent) / len(recent)
        return round(avg_x, 4), round(avg_y, 4)

    def trend(self, window: int = 10) -> Optional[Tuple[float, float]]:
        """
        Detecta tendência de desvio (inclinação linear simples).
        Retorna (dx, dy) — positivo indica desvio crescente naquela direção.
        """
        recent = self.last_n(window)
        if len(recent) < 3:
            return None
        n = len(recent)
        xs = [r.x for r in recent]
        ys = [r.y for r in recent]
        # Regressão linear mínimos quadrados
        ix = list(range(n))
        mean_i = sum(ix) / n
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((i - mean_i) ** 2 for i in ix) or 1
        slope_x = sum((i - mean_i) * (x - mean_x) for i, x in zip(ix, xs)) / denom
        slope_y = sum((i - mean_i) * (y - mean_y) for i, y in zip(ix, ys)) / denom
        return round(slope_x, 4), round(slope_y, 4)

    def x_series(self) -> List[float]:
        return [r.x for r in self._buffer]

    def y_series(self) -> List[float]:
        return [r.y for r in self._buffer]

    def timestamps(self) -> List[float]:
        return [r.timestamp for r in self._buffer]


class CoGStats:
    """
    Estatísticas acumuladas da sessão para geração de relatórios.
    Atualiza incrementalmente sem armazenar todos os readings.
    """

    def __init__(self):
        self.count: int = 0
        self.centered_count: int = 0
        self._sum_x: float = 0.0
        self._sum_y: float = 0.0
        self._sum_x2: float = 0.0
        self._sum_y2: float = 0.0
        self._sum_mag: float = 0.0
        self.max_x: float = 0.0
        self.max_y: float = 0.0
        self.max_mag: float = 0.0

    def update(self, reading: CoGReading):
        self.count += 1
        if reading.is_centered:
            self.centered_count += 1
        self._sum_x += reading.x
        self._sum_y += reading.y
        self._sum_x2 += reading.x ** 2
        self._sum_y2 += reading.y ** 2
        self._sum_mag += reading.magnitude
        self.max_x = max(self.max_x, abs(reading.x))
        self.max_y = max(self.max_y, abs(reading.y))
        self.max_mag = max(self.max_mag, reading.magnitude)

    @property
    def mean_x(self) -> float:
        return round(self._sum_x / self.count, 4) if self.count else 0.0

    @property
    def mean_y(self) -> float:
        return round(self._sum_y / self.count, 4) if self.count else 0.0

    @property
    def std_x(self) -> float:
        if self.count < 2:
            return 0.0
        variance = (self._sum_x2 - self._sum_x ** 2 / self.count) / (self.count - 1)
        return round(math.sqrt(max(0, variance)), 4)

    @property
    def std_y(self) -> float:
        if self.count < 2:
            return 0.0
        variance = (self._sum_y2 - self._sum_y ** 2 / self.count) / (self.count - 1)
        return round(math.sqrt(max(0, variance)), 4)

    @property
    def centered_pct(self) -> float:
        return round(self.centered_count / self.count * 100, 1) if self.count else 0.0

    @property
    def mean_magnitude(self) -> float:
        return round(self._sum_mag / self.count, 4) if self.count else 0.0

    def to_dict(self) -> dict:
        return {
            "total_readings": self.count,
            "centered_pct": self.centered_pct,
            "mean_x": self.mean_x,
            "mean_y": self.mean_y,
            "std_x": self.std_x,
            "std_y": self.std_y,
            "max_x": round(self.max_x, 4),
            "max_y": round(self.max_y, 4),
            "mean_magnitude": self.mean_magnitude,
            "max_magnitude": round(self.max_mag, 4),
        }
