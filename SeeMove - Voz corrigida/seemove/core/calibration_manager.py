"""
core/calibration_manager.py
Modo Configuração (Setup/Calibration) — garante de forma autônoma que o
usuário está de corpo inteiro no enquadramento da câmera antes de liberar
qualquer exercício, com orientação por áudio em pt-BR.

Mesmo espírito da cadeia de checagens já usada em exercises/base.py
(Exercise.analyze_checks): uma lista ordenada de verificações, a primeira
que falha isola o problema mais grave — evita misturar várias instruções
("seus pés não aparecem" + "incline a câmera" + "vá pra direita") na
mesma fala, o que seria confuso para quem não vê a tela.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

from core.skeleton import (
    SkeletonFrame, Point3D, MIN_VIS,
    NOSE, L_EAR, R_EAR,
    L_SHOULDER, R_SHOULDER, L_HIP, R_HIP,
    L_WRIST, R_WRIST,
    L_ANKLE, R_ANKLE,
)
from exercises.base import Severity


class FramingIssue(str, Enum):
    NONE      = "none"
    NO_PERSON = "no_person"
    HEAD_CUT  = "head_cut"
    FEET_CUT  = "feet_cut"
    HANDS_CUT = "hands_cut"
    TOO_LEFT  = "too_left"
    TOO_RIGHT = "too_right"


@dataclass
class FramingResult:
    ok:      bool
    issue:   FramingIssue
    message: str


# Distância normalizada (fração da largura/altura da imagem) considerada
# "perto demais da borda" — pega tanto o landmark já perdido pelo MediaPipe
# (visibility baixa) quanto o que ainda está visível mas prestes a sair,
# o que permite avisar o usuário um pouco antes do membro sumir de vez.
EDGE_MARGIN   = 0.06
CENTER_MARGIN = 0.16   # ±16% em torno do centro (0.5) é considerado centralizado


def _cut_or_missing(p: Optional[Point3D], *, near_top=False, near_bottom=False,
                     near_left=False, near_right=False) -> bool:
    """Landmark ausente (baixa confiança) ou perto demais de uma borda."""
    if p is None or p.visibility < MIN_VIS:
        return True
    if near_top and p.y < EDGE_MARGIN:
        return True
    if near_bottom and p.y > 1.0 - EDGE_MARGIN:
        return True
    if near_left and p.x < EDGE_MARGIN:
        return True
    if near_right and p.x > 1.0 - EDGE_MARGIN:
        return True
    return False


@dataclass
class _FramingCheck:
    name:  str
    check: Callable[[SkeletonFrame], Optional[FramingResult]]


def _check_no_person(frame: SkeletonFrame) -> Optional[FramingResult]:
    if not frame.detected or frame.metrics.confidence < 40.0:
        return FramingResult(
            False, FramingIssue.NO_PERSON,
            "Nenhuma pessoa detectada. Posicione-se em frente à câmera."
        )
    return None


def _check_head(frame: SkeletonFrame) -> Optional[FramingResult]:
    pts = frame.points
    head = [pts.get(NOSE), pts.get(L_EAR), pts.get(R_EAR)]
    cut = sum(1 for p in head if _cut_or_missing(p, near_top=True))
    if cut >= 2:  # maioria dos landmarks da cabeça ausente/no topo
        return FramingResult(
            False, FramingIssue.HEAD_CUT,
            "Sua cabeça está cortada. Incline a câmera um pouco para cima."
        )
    return None


def _check_feet(frame: SkeletonFrame) -> Optional[FramingResult]:
    pts = frame.points
    la, ra = pts.get(L_ANKLE), pts.get(R_ANKLE)
    ankles_cut = (
        _cut_or_missing(la, near_bottom=True) and
        _cut_or_missing(ra, near_bottom=True)
    )
    if ankles_cut:
        return FramingResult(
            False, FramingIssue.FEET_CUT,
            "Seus pés não aparecem. Dê um passo para trás ou incline a "
            "câmera um pouco para baixo."
        )
    return None


def _check_hands(frame: SkeletonFrame) -> Optional[FramingResult]:
    pts = frame.points
    lw, rw = pts.get(L_WRIST), pts.get(R_WRIST)
    hands_cut = (
        _cut_or_missing(lw, near_left=True, near_right=True) and
        _cut_or_missing(rw, near_left=True, near_right=True)
    )
    if hands_cut:
        return FramingResult(
            False, FramingIssue.HANDS_CUT,
            "Suas mãos não aparecem. Afaste-se um pouco da câmera."
        )
    return None


def _check_centering(frame: SkeletonFrame) -> Optional[FramingResult]:
    pts = frame.points
    ls, rs = pts.get(L_SHOULDER), pts.get(R_SHOULDER)
    lh, rh = pts.get(L_HIP), pts.get(R_HIP)

    if ls and rs and ls.visible and rs.visible:
        center_x = (ls.x + rs.x) / 2
    elif lh and rh and lh.visible and rh.visible:
        center_x = (lh.x + rh.x) / 2
    else:
        return None  # sem referência de tronco — outros checks já cobriram isso

    # O frame já vem espelhado (modo selfie) desde a captura — a direção na
    # tela já corresponde à direção real do usuário, sem precisar inverter.
    if center_x < 0.5 - CENTER_MARGIN:
        return FramingResult(
            False, FramingIssue.TOO_LEFT,
            "Dê um passo para a sua direita."
        )
    if center_x > 0.5 + CENTER_MARGIN:
        return FramingResult(
            False, FramingIssue.TOO_RIGHT,
            "Dê um passo para a sua esquerda."
        )
    return None


_CHECKS: List[_FramingCheck] = [
    _FramingCheck("no_person", _check_no_person),
    _FramingCheck("head",      _check_head),
    _FramingCheck("feet",      _check_feet),
    _FramingCheck("hands",     _check_hands),
    _FramingCheck("centering", _check_centering),
]


def check_framing(frame: SkeletonFrame) -> FramingResult:
    """Roda a cadeia de checagens de enquadramento em ordem de prioridade."""
    for c in _CHECKS:
        result = c.check(frame)
        if result is not None:
            return result
    return FramingResult(True, FramingIssue.NONE, "")


class CalibrationManager:
    """
    Orquestra o Modo Configuração: exige STABILITY_S segundos contínuos de
    enquadramento correto antes de confirmar sucesso, e nunca fala duas
    instruções em sequência rápida (INSTRUCTION_COOLDOWN_S entre falas).

    Reaproveita o AudioCoordinator já existente — speak_now()/chime() só
    enfileiram na fila assíncrona de TTS/sonificação (ou mandam um evento
    pro browser em modo web), então feed() nunca bloqueia a thread de
    análise nem, por extensão, a captura de vídeo do OpenCV.
    """

    STABILITY_S           = 3.0
    INSTRUCTION_COOLDOWN_S = 4.5

    def __init__(self, audio, on_success: Callable[[], None]):
        self.audio = audio
        self.on_success = on_success
        self._ok_since: Optional[float] = None
        self._last_instruction_ts = 0.0
        self._done = False

    def start(self):
        """Reinicia o ciclo — chamado toda vez que o Modo Configuração começa."""
        self._ok_since = None
        self._last_instruction_ts = 0.0
        self._done = False

    def feed(self, frame: SkeletonFrame) -> FramingResult:
        if self._done:
            return FramingResult(True, FramingIssue.NONE, "")

        result = check_framing(frame)
        now = time.time()

        if result.ok:
            if self._ok_since is None:
                self._ok_since = now
            elif now - self._ok_since >= self.STABILITY_S:
                self._done = True
                self.audio.chime("success")
                self.audio.speak_now(
                    "Enquadramento perfeito! Você já pode iniciar o "
                    "exercício por comando de voz.",
                    Severity.OK,
                )
                self.on_success()
            return result

        self._ok_since = None
        if now - self._last_instruction_ts >= self.INSTRUCTION_COOLDOWN_S:
            self._last_instruction_ts = now
            self.audio.speak_now(result.message, Severity.WARN)
        return result
