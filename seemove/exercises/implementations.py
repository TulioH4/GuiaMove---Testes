"""
exercises/implementations.py
Três exercícios baseados inteiramente no esqueleto (Kinect + MediaPipe).

Princípios do feedback para deficientes visuais:
  1. Uma instrução por vez — a mais urgente
  2. Linguagem corporal concreta: o usuário sabe exatamente o que fazer
  3. Confirmação positiva quando corrige ("muito bom, continue")
  4. Silêncio quando está correto — não enche a sessão de feedback
  5. Urgência progressiva: instrução leve → reforço → alerta

A prioridade de cada verificação é modelada como uma cadeia de
responsabilidade (`PostureCheck`, ver exercises/base.py): cada exercício
monta `self._checks` na ordem em que os problemas devem ser isolados —
o primeiro check que detecta um desvio "vence" e é o único falado,
evitando misturar várias correções na mesma instrução.

Exercícios disponíveis:
  squat   → Agachamento bipodal
  stand   → Avaliação postural estática
  balance → Equilíbrio unipodal
"""

from exercises.base import Exercise, FeedbackResult, Severity, PostureCheck
from core.skeleton import (
    SkeletonFrame,
    L_HIP, R_HIP, L_KNEE, R_KNEE,
)


# ─── Agachamento ──────────────────────────────────────────────────────────────

class SquatExercise(Exercise):
    """
    Agachamento bipodal — avalia:
      - Valgo de joelho (joelhos cedendo para dentro durante a descida)
      - Inclinação lateral do tronco (compensação)
      - Simetria: diferença de ângulo entre joelho esquerdo e direito

    Parâmetros biomecânicos:
      - Valgo > 5% da largura da imagem → alerta; > 10% → erro
      - Inclinação lateral > 8° → alerta; > 15° → erro
      - Diferença de ângulo entre joelhos > 15° → assimetria
    """
    name          = "Agachamento"
    start_message = (
        "Agachamento. Posicione-se de frente para a câmera, "
        "pés na largura dos ombros. Dobre os joelhos devagar."
    )
    end_message   = "Agachamento concluído. Bom trabalho."
    description   = "Avalia joelhos, tronco e simetria durante a descida."

    VALGUS_WARN  = 5.0    # % largura da imagem
    VALGUS_ERROR = 10.0
    TILT_WARN    = 8.0    # graus
    TILT_ERROR   = 15.0
    ASYM_WARN    = 15.0   # diferença de ângulo entre joelhos

    def __init__(self):
        self._checks = [
            PostureCheck("valgo_joelho", self._check_valgus),
            PostureCheck("inclinacao_tronco", self._check_trunk_tilt),
            PostureCheck("assimetria_joelhos", self._check_knee_asymmetry),
        ]

    def analyze(self, frame: SkeletonFrame) -> FeedbackResult:
        return self.analyze_checks(frame, self._checks)

    # ── Prioridade 1: valgo de joelho (risco de lesão) ────────────────────
    def _check_valgus(self, frame: SkeletonFrame):
        m = frame.metrics
        vl, vr = abs(m.knee_valgus_l), abs(m.knee_valgus_r)
        if max(vl, vr) > self.VALGUS_ERROR:
            side = "esquerdo" if vl > vr else "direito"
            return FeedbackResult(
                f"Seu joelho {side} está cedendo muito para dentro. "
                f"Empurre o joelho para fora, alinhado com o segundo dedo do pé.",
                True, Severity.ERROR,
                f"Valgo joelho {side}: {max(vl,vr):.1f}%"
            )
        if max(vl, vr) > self.VALGUS_WARN:
            side = "esquerdo" if vl > vr else "direito"
            return FeedbackResult(
                f"O joelho {side} está levemente para dentro. "
                f"Abra o joelho um pouco mais.",
                True, Severity.WARN,
                f"Valgo joelho {side}: {max(vl,vr):.1f}%"
            )
        return None

    # ── Prioridade 2: inclinação do tronco ─────────────────────────────────
    def _check_trunk_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        tilt = abs(m.trunk_lean_x)
        if tilt > self.TILT_ERROR:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Seu tronco está inclinando muito para a {side}. "
                f"Centralize os ombros sobre o quadril.",
                True, Severity.ERROR,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°"
            )
        if tilt > self.TILT_WARN:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Tronco levemente para a {side}. "
                f"Tente manter os ombros nivelados.",
                True, Severity.WARN,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°"
            )
        return None

    # ── Prioridade 3: assimetria entre joelhos ─────────────────────────────
    def _check_knee_asymmetry(self, frame: SkeletonFrame):
        m = frame.metrics
        angle_diff = abs(m.knee_angle_l - m.knee_angle_r)
        if angle_diff > self.ASYM_WARN and m.knee_angle_l < 160 and m.knee_angle_r < 160:
            side = "esquerda" if m.knee_angle_l < m.knee_angle_r else "direita"
            return FeedbackResult(
                f"Sua perna {side} está descendo mais que a outra. "
                f"Tente distribuir o agachamento igualmente nas duas pernas.",
                True, Severity.WARN,
                f"Assimetria joelhos: {angle_diff:.1f}°"
            )
        return None


# ─── Postura estática ─────────────────────────────────────────────────────────

class StaticPostureExercise(Exercise):
    """
    Avaliação postural em pé — avalia:
      - Simetria de ombros (assimetria crônica postural)
      - Simetria de quadril (obliquidade pélvica)
      - Inclinação lateral do tronco
      - Verticalidade do tronco (anteriorização ou retificação)

    Parâmetros biomecânicos:
      - Inclinação de ombros > 3° → alerta; > 6° → erro
      - Inclinação de quadril > 4° → alerta; > 8° → erro
      - Inclinação de tronco > 5° → alerta; > 10° → erro
      - Verticalidade do tronco > 10° → desvio postural
    """
    name          = "Postura estática"
    start_message = (
        "Avaliação postural. Fique em pé de forma natural, "
        "olhar no horizonte, braços soltos ao lado do corpo."
    )
    end_message   = "Avaliação postural concluída."
    description   = "Avalia simetria de ombros, quadril e alinhamento do tronco."

    SHOULDER_WARN  = 3.0
    SHOULDER_ERROR = 6.0
    HIP_WARN       = 4.0
    HIP_ERROR      = 8.0
    TRUNK_WARN     = 5.0
    TRUNK_ERROR    = 10.0
    VERTICAL_WARN  = 10.0

    def __init__(self):
        self._checks = [
            PostureCheck("inclinacao_ombros", self._check_shoulder_tilt),
            PostureCheck("inclinacao_quadril", self._check_hip_tilt),
            PostureCheck("inclinacao_tronco", self._check_trunk_tilt),
            PostureCheck("verticalidade_tronco", self._check_torso_vertical),
        ]

    def analyze(self, frame: SkeletonFrame) -> FeedbackResult:
        return self.analyze_checks(frame, self._checks)

    # ── Prioridade 1: inclinação de ombros ─────────────────────────────────
    def _check_shoulder_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        sh = abs(m.shoulder_tilt)
        if sh > self.SHOULDER_ERROR:
            side = "direito" if m.shoulder_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Seu ombro {side} está elevado. "
                f"Relaxe os ombros e deixe-os cair naturalmente.",
                True, Severity.ERROR,
                f"Inclinação ombros: {m.shoulder_tilt:.1f}°"
            )
        if sh > self.SHOULDER_WARN:
            side = "direito" if m.shoulder_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Ombro {side} levemente elevado. "
                f"Solte a tensão dos ombros.",
                True, Severity.WARN,
                f"Inclinação ombros: {m.shoulder_tilt:.1f}°"
            )
        return None

    # ── Prioridade 2: inclinação de quadril ────────────────────────────────
    def _check_hip_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        hp = abs(m.hip_tilt)
        if hp > self.HIP_ERROR:
            side = "direito" if m.hip_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Seu quadril está mais alto do lado {side}. "
                f"Distribua o peso igualmente nos dois pés.",
                True, Severity.ERROR,
                f"Inclinação quadril: {m.hip_tilt:.1f}°"
            )
        if hp > self.HIP_WARN:
            side = "direito" if m.hip_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Quadril levemente inclinado para o lado {side}. "
                f"Tente nivelar o peso entre os dois pés.",
                True, Severity.WARN,
                f"Inclinação quadril: {m.hip_tilt:.1f}°"
            )
        return None

    # ── Prioridade 3: inclinação lateral do tronco ─────────────────────────
    def _check_trunk_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        tl = abs(m.trunk_lean_x)
        if tl > self.TRUNK_ERROR:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Seu tronco está inclinado para a {side}. "
                f"Alinhe a cabeça com a coluna e centralize o peso.",
                True, Severity.ERROR,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°"
            )
        if tl > self.TRUNK_WARN:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Tronco levemente inclinado para a {side}.",
                True, Severity.WARN,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°"
            )
        return None

    # ── Prioridade 4: verticalidade do tronco ──────────────────────────────
    def _check_torso_vertical(self, frame: SkeletonFrame):
        m = frame.metrics
        if m.torso_vertical > self.VERTICAL_WARN:
            return FeedbackResult(
                "Você está inclinado para frente. "
                "Afaste levemente os quadris para trás e alinhe a coluna.",
                True, Severity.WARN,
                f"Verticalidade tronco: {m.torso_vertical:.1f}°"
            )
        return None


# ─── Equilíbrio unipodial ─────────────────────────────────────────────────────

class UnipodialBalanceExercise(Exercise):
    """
    Equilíbrio em uma perna — avalia:
      - Elevação do joelho da perna levantada (deve superar um limiar)
      - Oscilação lateral do tronco (instabilidade)
      - Inclinação do quadril (Trendelenburg — sinal de fraqueza glútea)

    Parâmetros biomecânicos:
      - Joelho elevado (y < quadril y - 0.08) → perna levantada confirmada
      - Inclinação do tronco > 10° → oscilação moderada
      - Inclinação do tronco > 20° → risco de queda
      - Inclinação do quadril > 5° → sinal de Trendelenburg
    """
    name          = "Equilíbrio unipodial"
    start_message = (
        "Equilíbrio em uma perna. Encontre um ponto fixo na sua frente. "
        "Quando estiver pronto, eleve uma perna devagar e mantenha."
    )
    end_message   = "Equilíbrio concluído. Excelente trabalho."
    description   = "Avalia oscilação, inclinação do quadril e estabilidade do tronco."

    KNEE_LIFT_THRESHOLD   = 0.08   # y do joelho deve estar 8% acima do quadril
    TRUNK_SWAY_WARN       = 10.0   # graus
    TRUNK_SWAY_ERROR      = 20.0
    HIP_DROP_WARN         = 5.0    # Trendelenburg
    HIP_DROP_ERROR        = 10.0

    def __init__(self):
        self._checks = [
            PostureCheck("perna_elevada", self._check_leg_raised),
            PostureCheck("risco_queda", self._check_fall_risk),
            PostureCheck("trendelenburg", self._check_hip_drop),
            PostureCheck("oscilacao_tronco", self._check_trunk_sway),
        ]

    def analyze(self, frame: SkeletonFrame) -> FeedbackResult:
        return self.analyze_checks(frame, self._checks)

    def _leg_raised(self, frame: SkeletonFrame) -> bool:
        pts = frame.points
        lh, rh = pts.get(L_HIP), pts.get(R_HIP)
        lk, rk = pts.get(L_KNEE), pts.get(R_KNEE)

        if lh and lk and lh.visible and lk.visible:
            if lh.y - lk.y > self.KNEE_LIFT_THRESHOLD:
                return True
        if rh and rk and rh.visible and rk.visible:
            if rh.y - rk.y > self.KNEE_LIFT_THRESHOLD:
                return True
        return False

    # ── Prioridade 0: confirma que uma perna está de fato elevada ─────────
    def _check_leg_raised(self, frame: SkeletonFrame):
        if not self._leg_raised(frame):
            return FeedbackResult(
                "Eleve uma perna até a altura do quadril e mantenha a posição.",
                True, Severity.WARN,
                "Nenhuma perna detectada elevada."
            )
        return None

    # ── Prioridade 1: risco de queda (oscilação severa) ────────────────────
    def _check_fall_risk(self, frame: SkeletonFrame):
        m = frame.metrics
        tilt = abs(m.trunk_lean_x)
        if tilt > self.TRUNK_SWAY_ERROR:
            return FeedbackResult(
                "Oscilação muito grande. Apoie as duas pernas agora para não cair.",
                True, Severity.ERROR,
                f"Oscilação tronco: {m.trunk_lean_x:.1f}°"
            )
        return None

    # ── Prioridade 2: sinal de Trendelenburg (queda de quadril) ───────────
    def _check_hip_drop(self, frame: SkeletonFrame):
        m = frame.metrics
        hp = abs(m.hip_tilt)
        if hp > self.HIP_DROP_ERROR:
            return FeedbackResult(
                "Seu quadril está caindo para o lado. "
                "Contraia o glúteo da perna de apoio.",
                True, Severity.ERROR,
                f"Queda de quadril: {m.hip_tilt:.1f}°"
            )
        if hp > self.HIP_DROP_WARN:
            return FeedbackResult(
                "Quadril levemente inclinado. "
                "Ative o glúteo e mantenha o quadril nivelado.",
                True, Severity.WARN,
                f"Queda de quadril: {m.hip_tilt:.1f}°"
            )
        return None

    # ── Prioridade 3: oscilação moderada do tronco ─────────────────────────
    def _check_trunk_sway(self, frame: SkeletonFrame):
        m = frame.metrics
        tilt = abs(m.trunk_lean_x)
        if tilt > self.TRUNK_SWAY_WARN:
            return FeedbackResult(
                "Você está oscilando. "
                "Contraia o abdômen e fixe o olhar em um ponto na sua frente.",
                True, Severity.WARN,
                f"Oscilação tronco: {m.trunk_lean_x:.1f}°"
            )
        return None
