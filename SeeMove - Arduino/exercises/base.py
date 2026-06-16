"""
exercises/base.py
Classe base para exercícios e estrutura de feedback.

Cada exercício herda de Exercise e implementa analyze(),
que recebe o CoG suavizado e retorna um FeedbackResult com
a mensagem e se deve acionar TTS/sonificação.
"""

from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class FeedbackResult:
    """Resultado da análise biomecânica de um frame."""
    message: str
    should_speak: bool
    severity: str  # 'ok' | 'warn' | 'error'
    cog_x: float = 0.0
    cog_y: float = 0.0


class Exercise(ABC):
    """Interface base para todos os exercícios do SeeMove."""

    name: str = "Exercício"
    start_message: str = "Exercício iniciado."
    end_message: str = "Exercício concluído."

    @abstractmethod
    def analyze(self, cog_x: float, cog_y: float, total_kg: float) -> FeedbackResult:
        """
        Analisa a postura e retorna feedback.

        Args:
            cog_x: Desvio lateral CoG [-1, +1]
            cog_y: Desvio ântero-posterior CoG [-1, +1]
            total_kg: Peso total sobre a plataforma

        Returns:
            FeedbackResult com mensagem e metadados
        """
        pass

    def _lateral_instruction(self, x: float, threshold: float) -> str:
        """Gera instrução lateral padronizada."""
        if x > threshold * 2:
            return "Peso excessivo na perna direita, centralize"
        if x > threshold:
            return "Transfira levemente o peso para a esquerda"
        if x < -threshold * 2:
            return "Peso excessivo na perna esquerda, centralize"
        if x < -threshold:
            return "Transfira levemente o peso para a direita"
        return ""

    def _anteroposterior_instruction(self, y: float, threshold: float) -> str:
        """Gera instrução ântero-posterior padronizada."""
        if y > threshold * 2:
            return "Recue o quadril, você está muito à frente"
        if y > threshold:
            return "Recue levemente o quadril"
        if y < -threshold * 2:
            return "Avance o peso, você está muito atrás"
        if y < -threshold:
            return "Avance o peso para a frente dos pés"
        return ""
