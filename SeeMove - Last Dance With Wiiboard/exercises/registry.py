"""
exercises/registry.py
Registro central de exercícios disponíveis no SeeMove.
"""

from exercises.base import Exercise
from exercises.implementations import (
    SquatExercise,
    UnipodialBalanceExercise,
    StaticPostureExercise,
    LungeExercise,
)


class ExerciseRegistry:
    """Fábrica de exercícios indexados por chave string."""

    _REGISTRY = {
        "squat":   SquatExercise,
        "balance": UnipodialBalanceExercise,
        "stand":   StaticPostureExercise,
        "lunge":   LungeExercise,
    }

    def get(self, key: str) -> Exercise:
        cls = self._REGISTRY.get(key)
        if cls is None:
            available = ", ".join(self._REGISTRY.keys())
            raise ValueError(
                f"Exercício '{key}' não encontrado. Disponíveis: {available}"
            )
        return cls()

    def list_all(self) -> dict:
        return {k: cls().name for k, cls in self._REGISTRY.items()}
