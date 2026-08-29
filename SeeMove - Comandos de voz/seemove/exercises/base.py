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
    # Quantas vezes o limiar de ALERTA da própria checagem o desvio está
    # (1.0 = bem em cima do limiar, 2.0 = o dobro da tolerância). Sempre
    # normalizado pelo limiar de WARN, mesmo em resultados ERROR — isso
    # deixa magnitudes de métricas diferentes (graus de inclinação, % de
    # valgo) comparáveis entre si. Usado só para desempate entre checagens
    # concorrentes em analyze_checks(); não é falado nem aparece na UI.
    magnitude:    float = 0.0


OK_RESULT = FeedbackResult("", False, Severity.OK)

_SEVERITY_RANK = {Severity.ERROR: 2, Severity.WARN: 1, Severity.OK: 0}


def worst_feedback(results: List[FeedbackResult]) -> FeedbackResult:
    """Escolhe o pior entre vários FeedbackResult: maior severidade
    primeiro, depois maior magnitude. Usado por analyze_checks() (vários
    candidatos do MESMO frame) e por filtros temporais que avaliam uma
    janela de frames recentes (ver SquatExercise._analyze_posture_with_
    temporal_filter) — mesma regra de desempate nos dois casos."""
    return max(results, key=lambda r: (_SEVERITY_RANK[r.severity], r.magnitude))


@dataclass
class PostureCheck:
    """
    Uma verificação de postura de um exercício.

    `check(frame)` retorna um FeedbackResult se detectou o problema que
    esta checagem cobre (com `magnitude` preenchido), ou None se está tudo
    certo nesse aspecto. Ao contrário de uma Chain of Responsibility
    clássica, a ORDEM da lista em `Exercise._checks` não determina mais
    prioridade — `analyze_checks()` roda todas e escolhe a mais grave (ver
    lá para o motivo). A ordem só importa para `gates` (ver
    `analyze_checks`), onde continua sendo Chain of Responsibility de
    verdade: o primeiro que disparar vence incondicionalmente.
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
                        checks: List[PostureCheck],
                        gates: Optional[List[PostureCheck]] = None) -> FeedbackResult:
        """
        Avalia as checagens de postura de um exercício e isola UMA única
        mensagem — nunca fala duas correções ao mesmo tempo.

        `gates`: checagens estruturais em ORDEM FIXA, avaliadas antes de
        tudo — a primeira que disparar vence incondicionalmente (Chain of
        Responsibility de verdade). Fazem sentido como gate quando, se
        falharem, qualquer feedback biomecânico das outras checagens seria
        prematuro ou sem sentido (ex.: "perna não está elevada" no
        equilíbrio unipodal — não faz sentido avaliar oscilação de tronco
        antes disso).

        `checks`: TODAS são avaliadas a cada frame (não para na primeira
        que disparar). Se mais de uma disparar no mesmo frame, vence a
        OBJETIVAMENTE mais grave: primeiro por severidade (erro > alerta),
        depois por `magnitude` (quantas vezes o próprio limiar de alerta o
        desvio está — normalizado, então dá pra comparar métricas
        diferentes, tipo graus de inclinação de tronco vs. % de valgo de
        joelho, de forma justa).

        Antes disso era "primeiro da lista que passar do limiar vence",
        ordem escolhida manualmente por checagem — o que na prática
        significava que uma checagem com limiar sensível cadastrada cedo
        na lista (ex.: inclinação de ombro, WARN a partir de só 3°) quase
        sempre "roubava" a fala de problemas muito piores cadastrados
        depois, mesmo quando o desvio real deles era muito maior.
        """
        err = self._require_detection(frame)
        if err:
            return err

        for gate in (gates or []):
            result = gate.check(frame)
            if result is not None:
                return result

        candidates: List[FeedbackResult] = []
        for posture_check in checks:
            result = posture_check.check(frame)
            if result is not None:
                candidates.append(result)

        if not candidates:
            return OK_RESULT

        return worst_feedback(candidates)
