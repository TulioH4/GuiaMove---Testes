"""
exercises/implementations.py
Três exercícios baseados inteiramente no esqueleto (Kinect + MediaPipe).

Princípios do feedback para deficientes visuais:
  1. Uma instrução por vez — a mais urgente
  2. Linguagem corporal concreta: o usuário sabe exatamente o que fazer
  3. Confirmação positiva quando corrige ("muito bom, continue")
  4. Silêncio quando está correto — não enche a sessão de feedback
  5. Urgência progressiva: instrução leve → reforço → alerta

Cada exercício monta `self._checks` (`PostureCheck`, ver exercises/base.py)
— TODAS são avaliadas a cada frame, e a mais grave (por severidade, depois
por magnitude do desvio) é a única falada, evitando misturar várias
correções na mesma instrução. A ordem da lista não importa mais para
prioridade (só serve como identificador estável de cada checagem); quando
um problema estrutural precisa bloquear todo o resto incondicionalmente
(ex.: "nenhuma perna elevada" no equilíbrio unipodal), ele entra como
`gates` — aí sim Chain of Responsibility de verdade, em ordem fixa.

Exercícios disponíveis:
  squat   → Agachamento bipodal
  stand   → Avaliação postural estática
  balance → Equilíbrio unipodal
"""

import time
from collections import deque
from typing import Optional

from exercises.base import Exercise, FeedbackResult, Severity, PostureCheck, worst_feedback
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

    # ── Fases do movimento (ver bloco de métodos mais abaixo) ──────────────
    # Ângulo do joelho: 180° = perna estendida, menor = mais flexionado.
    # Valores iniciais — calibrar com testes reais (distância/altura da
    # câmera, amplitude individual etc. mudam a leitura do MediaPipe).
    STATE_STANDING   = "em_pe"
    STATE_DESCENDING = "descendo"
    STATE_BOTTOM     = "fundo"
    STATE_ASCENDING  = "subindo"

    STAND_EXIT_ANGLE    = 168.0   # acima disso, considera "quase em pé"
    DESCENT_ENTER_ANGLE = 160.0   # abaixo disso, considera "começou a descer"
    BOTTOM_ANGLE        = 115.0   # profundidade mínima aceitável

    STATE_CONFIRM_FRAMES  = 4     # frames consecutivos p/ confirmar troca de fase
    MOVEMENT_TIMEOUT_S    = 8.0   # timeout de SEGURANÇA — não é limite de cadência
    POSTURE_CONFIRM_FRAMES = 3    # frames consecutivos p/ confirmar erro postural

    # Janela de cadência aceitável para o ciclo INTEIRO da repetição
    # (descida + fundo + subida) — valores iniciais, calibrar com testes
    # reais. Diferente de MOVEMENT_TIMEOUT_S (que só evita ficar preso
    # indefinidamente): aqui a repetição É contabilizada de qualquer jeito
    # (chegou à profundidade e voltou), só marca se o RITMO foi bom ou não.
    MIN_REP_DURATION_S = 1.2   # mais rápido que isso: descida/subida bruscas
    MAX_REP_DURATION_S = 6.0   # mais lento que isso (mas dentro do timeout): perdeu continuidade

    def __init__(self):
        self._checks = [
            PostureCheck("valgo_joelho", self._check_valgus),
            PostureCheck("inclinacao_tronco", self._check_trunk_tilt),
            PostureCheck("assimetria_joelhos", self._check_knee_asymmetry),
        ]

        # Estado da máquina de fases do movimento + contagem de repetições.
        self._movement_state      = self.STATE_STANDING
        self._candidate_state     = None
        self._candidate_frames    = 0
        self._movement_started_at = None

        # `repetitions` só conta ciclos com cadência "boa" — é o número
        # oficial da sessão. Um ciclo completo (chegou à profundidade e
        # voltou) com cadência ruim NÃO soma aqui, vai pra
        # `rejected_reps` — Session usa os dois pra decidir o sinal
        # sonoro certo (sucesso vs. "não contou, tente de novo").
        self.repetitions   = 0
        self.rejected_reps = 0

        # Cadência da última repetição concluída — None até a primeira,
        # depois "boa" | "rapida_demais" | "lenta_demais". Session lê este
        # atributo público (junto com `repetitions`) pra decidir que sinal
        # sonoro tocar, sem precisar que o Exercise fale sozinho (mantém a
        # separação: Exercise só analisa, Session decide o que soa).
        self.last_rep_cadence: Optional[str] = None
        self.last_rep_duration_s: float = 0.0

        # _last_raw_angle: fallback de _average_knee_angle (último ângulo
        # bruto válido). _smoothed_angle: estado do filtro exponencial de
        # _smooth_angle. São DUAS variáveis separadas de propósito — usar
        # uma só faz a suavização virar no-op (ela lia o valor que a média
        # bruta tinha acabado de sobrescrever no mesmo frame).
        self._last_raw_angle = 180.0
        self._smoothed_angle = 180.0

        # Confirmação por múltiplos frames do erro postural — janela dos
        # últimos POSTURE_CONFIRM_FRAMES resultados, decide por MAIORIA
        # (não por sequência idêntica e ininterrupta — ver
        # _analyze_posture_with_temporal_filter).
        self._posture_window = deque(maxlen=self.POSTURE_CONFIRM_FRAMES)

    def analyze(self, frame: SkeletonFrame) -> FeedbackResult:
        err = self._require_detection(frame)
        if err:
            self._movement_state = self.STATE_STANDING
            self._candidate_state, self._candidate_frames = None, 0
            self._movement_started_at = None
            return err

        angle = self._smooth_angle(self._average_knee_angle(frame))
        repetition_completed = self._update_movement_state(angle)
        detail = (
            f"fase={self._movement_state};repeticoes={self.repetitions};"
            f"rejeitadas={self.rejected_reps};angulo_joelho={angle:.1f};"
            f"cadencia={self.last_rep_cadence}"
        )

        if repetition_completed:
            msg = ("Repetição concluída." if self.last_rep_cadence == "boa"
                   else "Não contou — cadência irregular. Tente de novo.")
            return FeedbackResult(msg, False, Severity.OK, detail)

        # Durante a fase de transição não queremos que qualquer oscilação
        # pequena vire "erro" — analyze_checks() já escolhe o pior problema
        # entre os três; a espera de POSTURE_CONFIRM_FRAMES só atrasa a
        # CONFIRMAÇÃO, não muda qual problema seria reportado.
        posture_result = self._analyze_posture_with_temporal_filter(frame)
        if posture_result.severity != Severity.OK:
            return FeedbackResult(
                posture_result.message, posture_result.should_speak,
                posture_result.severity, f"{detail};{posture_result.detail}"
            )
        return FeedbackResult("Certo.", False, Severity.OK, detail)

    # Checagens abaixo não têm mais prioridade fixa entre si — todas são
    # avaliadas a cada frame, e analyze_checks() escolhe a mais grave pela
    # magnitude real do desvio (ver exercises/base.py). Os nomes
    # "Prioridade N" ficam só como identificadores estáveis (usados em
    # PostureCheck.name para depuração), não como ordem de execução.
    def _check_valgus(self, frame: SkeletonFrame):
        m = frame.metrics
        vl, vr = abs(m.knee_valgus_l), abs(m.knee_valgus_r)
        worst = max(vl, vr)
        mag = worst / self.VALGUS_WARN
        if worst > self.VALGUS_ERROR:
            side = "esquerdo" if vl > vr else "direito"
            return FeedbackResult(
                f"Seu joelho {side} está cedendo muito para dentro. "
                f"Empurre o joelho para fora, alinhado com o segundo dedo do pé.",
                True, Severity.ERROR,
                f"Valgo joelho {side}: {worst:.1f}%", magnitude=mag
            )
        if worst > self.VALGUS_WARN:
            side = "esquerdo" if vl > vr else "direito"
            return FeedbackResult(
                f"O joelho {side} está levemente para dentro. "
                f"Abra o joelho um pouco mais.",
                True, Severity.WARN,
                f"Valgo joelho {side}: {worst:.1f}%", magnitude=mag
            )
        return None

    def _check_trunk_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        tilt = abs(m.trunk_lean_x)
        mag = tilt / self.TILT_WARN
        if tilt > self.TILT_ERROR:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Seu tronco está inclinando muito para a {side}. "
                f"Centralize os ombros sobre o quadril.",
                True, Severity.ERROR,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°", magnitude=mag
            )
        if tilt > self.TILT_WARN:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Tronco levemente para a {side}. "
                f"Tente manter os ombros nivelados.",
                True, Severity.WARN,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°", magnitude=mag
            )
        return None

    def _check_knee_asymmetry(self, frame: SkeletonFrame):
        m = frame.metrics
        angle_diff = abs(m.knee_angle_l - m.knee_angle_r)
        if angle_diff > self.ASYM_WARN and m.knee_angle_l < 160 and m.knee_angle_r < 160:
            side = "esquerda" if m.knee_angle_l < m.knee_angle_r else "direita"
            return FeedbackResult(
                f"Sua perna {side} está descendo mais que a outra. "
                f"Tente distribuir o agachamento igualmente nas duas pernas.",
                True, Severity.WARN,
                f"Assimetria joelhos: {angle_diff:.1f}°",
                magnitude=angle_diff / self.ASYM_WARN
            )
        return None

    # ── Fases do movimento + contagem de repetições ────────────────────────
    #
    # Um agachamento é um MOVIMENTO CONTÍNUO, não uma sequência de frames
    # independentes — sem isso, uma oscilação normal durante a descida podia
    # ser lida como erro isolado, e não existia contagem de repetições.
    # Histerese (limiares diferentes pra entrar/sair de cada fase) +
    # confirmação por múltiplos frames evita que ruído do MediaPipe bem na
    # borda de um limiar faça o estado "piscar".

    def _average_knee_angle(self, frame: SkeletonFrame) -> float:
        """Ângulo médio dos dois joelhos — mais estável que usar só um
        lado. Se nenhum dos dois for válido neste frame (perda momentânea
        de detecção), usa o último ângulo bruto válido."""
        left, right = frame.metrics.knee_angle_l, frame.metrics.knee_angle_r
        valid = [a for a in (left, right) if 0.0 < a <= 180.0]
        if not valid:
            return self._last_raw_angle
        angle = sum(valid) / len(valid)
        self._last_raw_angle = angle
        return angle

    def _smooth_angle(self, current_angle: float) -> float:
        """Suavização exponencial (EMA) simples. Usa self._smoothed_angle
        (só escrita aqui) como estado anterior — nunca a mesma variável que
        _average_knee_angle usa de fallback, senão a suavização vira no-op
        (lendo um valor que acabou de ser sobrescrito com o ângulo bruto
        do próprio frame atual)."""
        ALPHA = 0.35
        self._smoothed_angle = ALPHA * current_angle + (1.0 - ALPHA) * self._smoothed_angle
        return self._smoothed_angle

    def _request_state(self, new_state: str) -> bool:
        """Só confirma a troca de fase após STATE_CONFIRM_FRAMES frames
        CONSECUTIVOS pedindo o mesmo estado nesse candidato."""
        if new_state == self._movement_state:
            self._candidate_state, self._candidate_frames = None, 0
            return False
        if new_state != self._candidate_state:
            self._candidate_state, self._candidate_frames = new_state, 1
            return False
        self._candidate_frames += 1
        if self._candidate_frames < self.STATE_CONFIRM_FRAMES:
            return False
        self._movement_state = new_state
        self._candidate_state, self._candidate_frames = None, 0
        return True

    def _update_movement_state(self, angle: float) -> bool:
        """Avança a máquina de fases com o ângulo já suavizado. Retorna
        True só no frame exato em que uma repetição é concluída — exige a
        sequência inteira EM_PÉ→DESCENDO→FUNDO→SUBINDO→EM_PÉ; um
        agachamento raso que volta no meio (ver ramo DESCENDING) não
        conta."""
        now = time.monotonic()

        if self._movement_started_at is not None and \
           now - self._movement_started_at > self.MOVEMENT_TIMEOUT_S:
            # Timeout de SEGURANÇA — não valida cadência (cada pessoa tem
            # seu ritmo), só evita que o estado fique preso indefinidamente
            # se o movimento for interrompido no meio. Não conta repetição.
            self._movement_state = self.STATE_STANDING
            self._candidate_state, self._candidate_frames = None, 0
            self._movement_started_at = None

        if self._movement_state == self.STATE_STANDING:
            if angle < self.DESCENT_ENTER_ANGLE:
                if self._request_state(self.STATE_DESCENDING):
                    self._movement_started_at = now
            else:
                self._candidate_state, self._candidate_frames = None, 0
            return False

        if self._movement_state == self.STATE_DESCENDING:
            if angle <= self.BOTTOM_ANGLE:
                self._request_state(self.STATE_BOTTOM)
            elif angle > self.STAND_EXIT_ANGLE:
                # Voltou a ficar em pé sem chegar ao fundo — agachamento
                # raso, aborta sem contar repetição.
                self._movement_state = self.STATE_STANDING
                self._candidate_state, self._candidate_frames = None, 0
                self._movement_started_at = None
            return False

        if self._movement_state == self.STATE_BOTTOM:
            if angle > self.BOTTOM_ANGLE + 5.0:
                self._request_state(self.STATE_ASCENDING)
            return False

        if self._movement_state == self.STATE_ASCENDING:
            if angle >= self.STAND_EXIT_ANGLE and self._request_state(self.STATE_STANDING):
                # Duração do ciclo INTEIRO (descida+fundo+subida) — não é
                # limite rígido por pessoa, só classifica o ritmo.
                # self._movement_started_at foi setado no instante em que
                # a descida foi confirmada (STANDING→DESCENDING), então
                # ainda reflete o início real do ciclo aqui, antes de ser
                # zerado logo abaixo.
                self.last_rep_duration_s = now - self._movement_started_at
                if self.last_rep_duration_s < self.MIN_REP_DURATION_S:
                    self.last_rep_cadence = "rapida_demais"
                elif self.last_rep_duration_s > self.MAX_REP_DURATION_S:
                    self.last_rep_cadence = "lenta_demais"
                else:
                    self.last_rep_cadence = "boa"

                # Cadência ruim NÃO conta como repetição válida — a pessoa
                # completou o ciclo (profundidade e sequência corretas),
                # mas rápido/devagar demais pra valer. Ainda assim retorna
                # True: a fase do movimento resetou de verdade, e o
                # usuário precisa ser avisado de que aquela não contou.
                if self.last_rep_cadence == "boa":
                    self.repetitions += 1
                else:
                    self.rejected_reps += 1

                self._movement_started_at = None
                return True
            return False

        return False

    def _analyze_posture_with_temporal_filter(self, frame: SkeletonFrame) -> FeedbackResult:
        """Confirma um problema postural por MAIORIA entre os últimos
        POSTURE_CONFIRM_FRAMES resultados de analyze_checks() (que já
        escolhe o mais grave por frame — nunca volta a testar cada
        checagem isolada em ordem fixa, senão desfaz a priorização por
        gravidade/magnitude).

        Primeira versão exigia uma sequência IDÊNTICA e ININTERRUPTA (N
        frames seguidos com a mesma mensagem exata) — na prática, quase
        nunca confirmava nada: valgo/inclinação perto do limiar alternam
        de frame a frame por ruído do MediaPipe (ora "joelho esquerdo" ora
        "direito", ou um frame isolado caindo de volta a OK), e qualquer
        troca reiniciava a contagem do zero. Votação por maioria numa
        janela deslizante tolera esse ruído: 1 frame fora do padrão não
        desfaz a confirmação, seja ele um falso "OK" isolado no meio de um
        problema real, seja um falso positivo isolado no meio de uma
        postura correta.
        """
        result = self.analyze_checks(frame, self._checks)
        self._posture_window.append(result)

        # Janela ainda não encheu (início do exercício/pós-reset) — não dá
        # pra votar ainda, mesmo que este frame já tenha um problema.
        if len(self._posture_window) < self.POSTURE_CONFIRM_FRAMES:
            return result if result.severity == Severity.OK else FeedbackResult("", False, Severity.OK, result.detail)

        non_ok = [r for r in self._posture_window if r.severity != Severity.OK]
        if len(non_ok) * 2 <= len(self._posture_window):
            # Minoria (ou nenhum) dos frames recentes com problema — ainda
            # não há evidência suficiente de que é persistente.
            return result if result.severity == Severity.OK else FeedbackResult("", False, Severity.OK, result.detail)

        return worst_feedback(non_ok)


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
    VERTICAL_ERROR = 20.0

    def __init__(self):
        self._checks = [
            PostureCheck("inclinacao_ombros", self._check_shoulder_tilt),
            PostureCheck("inclinacao_quadril", self._check_hip_tilt),
            PostureCheck("inclinacao_tronco", self._check_trunk_tilt),
            PostureCheck("verticalidade_tronco", self._check_torso_vertical),
        ]

    def analyze(self, frame: SkeletonFrame) -> FeedbackResult:
        return self.analyze_checks(frame, self._checks)

    # Checagens abaixo não têm mais prioridade fixa entre si — todas são
    # avaliadas a cada frame, e analyze_checks() escolhe a mais grave pela
    # magnitude real do desvio (ver exercises/base.py). Antes, "ombro"
    # sempre vencia por vir primeiro na lista e ter o limiar mais sensível
    # (3°) — mesmo quando o problema real era um tronco muito mais
    # inclinado, que nunca chegava a ser avaliado naquele frame.
    def _check_shoulder_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        sh = abs(m.shoulder_tilt)
        mag = sh / self.SHOULDER_WARN
        if sh > self.SHOULDER_ERROR:
            side = "direito" if m.shoulder_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Seu ombro {side} está elevado. "
                f"Relaxe os ombros e deixe-os cair naturalmente.",
                True, Severity.ERROR,
                f"Inclinação ombros: {m.shoulder_tilt:.1f}°", magnitude=mag
            )
        if sh > self.SHOULDER_WARN:
            side = "direito" if m.shoulder_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Ombro {side} levemente elevado. "
                f"Solte a tensão dos ombros.",
                True, Severity.WARN,
                f"Inclinação ombros: {m.shoulder_tilt:.1f}°", magnitude=mag
            )
        return None

    def _check_hip_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        hp = abs(m.hip_tilt)
        mag = hp / self.HIP_WARN
        if hp > self.HIP_ERROR:
            side = "direito" if m.hip_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Seu quadril está mais alto do lado {side}. "
                f"Distribua o peso igualmente nos dois pés.",
                True, Severity.ERROR,
                f"Inclinação quadril: {m.hip_tilt:.1f}°", magnitude=mag
            )
        if hp > self.HIP_WARN:
            side = "direito" if m.hip_tilt > 0 else "esquerdo"
            return FeedbackResult(
                f"Quadril levemente inclinado para o lado {side}. "
                f"Tente nivelar o peso entre os dois pés.",
                True, Severity.WARN,
                f"Inclinação quadril: {m.hip_tilt:.1f}°", magnitude=mag
            )
        return None

    def _check_trunk_tilt(self, frame: SkeletonFrame):
        m = frame.metrics
        tl = abs(m.trunk_lean_x)
        mag = tl / self.TRUNK_WARN
        if tl > self.TRUNK_ERROR:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Seu tronco está inclinado para a {side}. "
                f"Alinhe a cabeça com a coluna e centralize o peso.",
                True, Severity.ERROR,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°", magnitude=mag
            )
        if tl > self.TRUNK_WARN:
            side = "direita" if m.trunk_lean_x > 0 else "esquerda"
            return FeedbackResult(
                f"Tronco levemente inclinado para a {side}.",
                True, Severity.WARN,
                f"Inclinação tronco: {m.trunk_lean_x:.1f}°", magnitude=mag
            )
        return None

    def _check_torso_vertical(self, frame: SkeletonFrame):
        m = frame.metrics
        # Antes só existia o tier WARN — por mais inclinado que o tronco
        # ficasse, essa checagem nunca escalava para ERROR e nunca
        # competia de igual pra igual com outras checagens em ERROR (mesmo
        # sendo, na prática, o desvio mais grave que a pessoa podia ter).
        if m.torso_vertical > self.VERTICAL_ERROR:
            return FeedbackResult(
                "Você está bem inclinado para frente. Afaste os quadris "
                "para trás com cuidado e alinhe a coluna, sem forçar.",
                True, Severity.ERROR,
                f"Verticalidade tronco: {m.torso_vertical:.1f}°",
                magnitude=m.torso_vertical / self.VERTICAL_WARN
            )
        if m.torso_vertical > self.VERTICAL_WARN:
            return FeedbackResult(
                "Você está inclinado para frente. "
                "Afaste levemente os quadris para trás e alinhe a coluna.",
                True, Severity.WARN,
                f"Verticalidade tronco: {m.torso_vertical:.1f}°",
                magnitude=m.torso_vertical / self.VERTICAL_WARN
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
        # perna_elevada continua sendo Chain of Responsibility de verdade
        # (gate): se nenhuma perna está elevada, nenhum outro feedback
        # biomecânico faz sentido ainda — não tem "gravidade" pra comparar
        # com oscilação/Trendelenburg, é um pré-requisito estrutural.
        self._gates = [
            PostureCheck("perna_elevada", self._check_leg_raised),
        ]
        # As demais são avaliadas todas a cada frame e a mais grave vence
        # por magnitude (ver exercises/base.py) — risco_queda e
        # oscilacao_tronco leem a mesma métrica (trunk_lean_x) em limiares
        # diferentes; podem disparar juntas no mesmo frame, e nesse caso
        # risco_queda (ERROR) vence por severidade, como deve ser.
        self._checks = [
            PostureCheck("risco_queda", self._check_fall_risk),
            PostureCheck("trendelenburg", self._check_hip_drop),
            PostureCheck("oscilacao_tronco", self._check_trunk_sway),
        ]

    def analyze(self, frame: SkeletonFrame) -> FeedbackResult:
        return self.analyze_checks(frame, self._checks, gates=self._gates)

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

    def _check_leg_raised(self, frame: SkeletonFrame):
        if not self._leg_raised(frame):
            return FeedbackResult(
                "Eleve uma perna até a altura do quadril e mantenha a posição.",
                True, Severity.WARN,
                "Nenhuma perna detectada elevada."
            )
        return None

    def _check_fall_risk(self, frame: SkeletonFrame):
        m = frame.metrics
        tilt = abs(m.trunk_lean_x)
        if tilt > self.TRUNK_SWAY_ERROR:
            return FeedbackResult(
                "Oscilação muito grande. Apoie as duas pernas agora para não cair.",
                True, Severity.ERROR,
                f"Oscilação tronco: {m.trunk_lean_x:.1f}°",
                magnitude=tilt / self.TRUNK_SWAY_WARN
            )
        return None

    def _check_hip_drop(self, frame: SkeletonFrame):
        m = frame.metrics
        hp = abs(m.hip_tilt)
        mag = hp / self.HIP_DROP_WARN
        if hp > self.HIP_DROP_ERROR:
            return FeedbackResult(
                "Seu quadril está caindo para o lado. "
                "Contraia o glúteo da perna de apoio.",
                True, Severity.ERROR,
                f"Queda de quadril: {m.hip_tilt:.1f}°", magnitude=mag
            )
        if hp > self.HIP_DROP_WARN:
            return FeedbackResult(
                "Quadril levemente inclinado. "
                "Ative o glúteo e mantenha o quadril nivelado.",
                True, Severity.WARN,
                f"Queda de quadril: {m.hip_tilt:.1f}°", magnitude=mag
            )
        return None

    def _check_trunk_sway(self, frame: SkeletonFrame):
        m = frame.metrics
        tilt = abs(m.trunk_lean_x)
        if tilt > self.TRUNK_SWAY_WARN:
            return FeedbackResult(
                "Você está oscilando. "
                "Contraia o abdômen e fixe o olhar em um ponto na sua frente.",
                True, Severity.WARN,
                f"Oscilação tronco: {m.trunk_lean_x:.1f}°",
                magnitude=tilt / self.TRUNK_SWAY_WARN
            )
        return None
