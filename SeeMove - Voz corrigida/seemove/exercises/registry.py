"""exercises/registry.py"""
from exercises.base import Exercise
from exercises.implementations import (
    SquatExercise,
    StaticPostureExercise,
    UnipodialBalanceExercise,
)

class ExerciseRegistry:
    _REGISTRY = {
        "squat":   SquatExercise,
        "stand":   StaticPostureExercise,
        "balance": UnipodialBalanceExercise,
    }

    def get(self, key: str) -> Exercise:
        cls = self._REGISTRY.get(key)
        if cls is None:
            available = ", ".join(self._REGISTRY.keys())
            raise ValueError(f"Exercício '{key}' não encontrado. Disponíveis: {available}")
        return cls()

    def list_all(self) -> dict:
        return {k: cls().name for k, cls in self._REGISTRY.items()}
