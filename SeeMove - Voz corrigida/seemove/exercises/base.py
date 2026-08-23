"""
exercises/base.py
Base para exercícios — análise baseada em SkeletonFrame (Kinect + MediaPipe).
Sem sensores de pressão.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

from core.skeleton import SkeletonFrame, SkeletonMetrics


class Severity(str, Enum):
    OK    = "ok"
    WARN  = "warn"
    ERROR = "error"


@dataclass
class FeedbackResult:
    """Resultado da análise de um frame de exercício."""
    message:      str
    should_speak: bool
    severity:     Severity
    detail:       str = ""    # informação técnica para o dashboard (não falada)


OK_RESULT = FeedbackResult("", False, Severity.OK)


@dataclass
class PostureCheck:
    """
    Um elo na cadeia de verificação de um exercício (Chain of Responsibility).

    `check(frame)` retorna um FeedbackResult se detectou o problema que
    esta checagem cobre, ou None se está tudo certo nesse aspecto — nesse
    caso a cadeia segue para o próximo check. A ordem da lista em
    `Exercise._checks` é a ordem de prioridade: o primeiro check que
    retorna um resultado não-OK "vence" e isola o erro mais grave, sem
    misturar várias instruções na mesma fala.
    """
    name: str
    check: Callable[[SkeletonFrame], Optional[FeedbackResult]]


class Exercise(ABC):
    name:          str = "Exercício"
    start_message: str = "Exercício iniciado."
    end_message:   str = "Exercício concluído."
    description:   str = ""

    @abstractmethod
    def analyze(self, frame: SkeletonFrame) -> FeedbackResult:
        """
        Analisa o frame do esqueleto e retorna feedback instrucional.

        Args:
            frame: Frame completo com landmarks e métricas pré-calculadas.

        Returns:
            FeedbackResult — mensagem para o usuário + metadados.
        """
        pass

    def _require_detection(self, frame: SkeletonFrame,
                           min_confidence: float = 40.0) -> Optional[FeedbackResult]:
        """Retorna feedback se o corpo não for detectado com confiança suficiente."""
        if not frame.detected:
            return FeedbackResult(
                "Posicione-se em frente à câmera.",
                True, Severity.WARN,
                "Nenhum esqueleto detectado."
            )
        if frame.metrics.confidence < min_confidence:
            return FeedbackResult(
                "Afaste-se um pouco da câmera para eu ver seu corpo inteiro.",
                True, Severity.WARN,
                f"Confiança baixa: {frame.metrics.confidence:.0f}%"
            )
        return None

    def analyze_checks(self, frame: SkeletonFrame,
                        checks: List[PostureCheck]) -> FeedbackResult:
        """
        Executa a cadeia de checagens em ordem de prioridade e retorna o
        primeiro resultado não-OK — isola o erro mais grave em vez de
        acumular várias correções na mesma instrução.
        """
        err = self._require_detection(frame)
        if err:
            return err

        for posture_check in checks:
            result = posture_check.check(frame)
            if result is not None:
                return result

        return OK_RESULT
