"""
core/session.py
Loop principal do SeeMove — baseado inteiramente no Kinect + MediaPipe.
Sem sensores de pressão.

Pipeline em 3 estágios, cada um na sua própria thread, ligados por filas
limitadas (drop-oldest — nunca acumula atraso):

  1. Captura + MediaPipe   → KinectTracker._loop (inalterado)
     chama Session._enqueue_frame, que só empurra o frame numa fila —
     nunca espera análise nem rede, então a câmera nunca trava.
  2. Análise biomecânica   → Session._analysis_loop (thread nova)
     roda exercise.analyze + a máquina de estados de feedback (que
     dispara áudio) + o reporter, e empurra um snapshot pronto numa
     segunda fila.
  3. Broadcast Socket.IO   → Session._broadcast_loop (thread nova)
     consome a fila de snapshots e chama web_push, limitado a no
     máximo 30 pacotes por segundo.

Máquina de estados de feedback:
  STOPPED     → mudo por padrão — nenhuma análise nem áudio até "Iniciar"
  SETUP       → Modo Configuração: calibrando o enquadramento (ver CalibrationManager)
  PAUSED      → mudo temporariamente (Pausar/Retomar)
  BRIEFING    → explicando o movimento antes de monitorar (start_message)
  IDLE        → monitorando silenciosamente (postura OK)
  INSTRUCTING → desvio detectado, instrução emitida, aguardando correção
  WAITING     → janela de silêncio (5s) para o usuário corrigir
  CONFIRMING  → corrigiu — emite confirmação positiva uma vez
  REINFORCING → não corrigiu — reforça com dica adicional
"""

import queue
import time
import threading
from enum import Enum
from typing import Optional

from core.kinect_tracker import KinectTracker
from core.skeleton import SkeletonFrame
from core.calibration_manager import CalibrationManager
from exercises.base import Exercise, FeedbackResult, Severity
from reports.reporter import SessionReporter
from config.settings import Settings
from audio.tts_engine import TTSEngine
from audio.sonification import SonificationEngine
from audio.coordinator import AudioCoordinator


class FeedbackState(Enum):
    STOPPED     = "stopped"
    SETUP       = "setup"
    PAUSED      = "paused"
    BRIEFING    = "briefing"
    IDLE        = "idle"
    INSTRUCTING = "instructing"
    WAITING     = "waiting"
    CONFIRMING  = "confirming"
    REINFORCING = "reinforcing"


CONFIRMATIONS = [
    "Isso, muito bom.",
    "Perfeito, continue assim.",
    "Ótimo, está correto.",
    "Muito bem.",
    "Excelente.",
]

_WAITING_EXERCISE_MSG = FeedbackResult("Aguardando início do exercício.", False, Severity.OK, "")
_PAUSED_MSG           = FeedbackResult("Pausado.", False, Severity.OK, "")


def _put_drop_oldest(q: "queue.Queue", item):
    """queue.put não bloqueante — descarta o item mais antigo se a fila
    estiver cheia, em vez de acumular atraso (produtor nunca espera)."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


class Session:
    WAIT_WINDOW_S  = 5.0
    CONFIRM_FRAMES = 8       # frames OK consecutivos para confirmar
    REINFORCE_MAX  = 2       # máximo de reforços antes de pausa longa
    BRIEFING_TIMEOUT_S = 15.0
    BROADCAST_MAX_FPS  = 30

    def __init__(self,
                 tracker: KinectTracker,
                 tts: TTSEngine,
                 sonification: SonificationEngine,
                 exercise: Exercise,
                 settings: Settings,
                 reporter: SessionReporter,
                 web_push=None):
        self.tracker      = tracker
        self.tts          = tts
        self.sonification = sonification
        self.exercise     = exercise
        self.settings     = settings
        self.reporter     = reporter
        self.web_push     = web_push

        self.audio = AudioCoordinator(
            tts, sonification, settings.voice, settings.sonification_enabled
        )
        self.calibration = CalibrationManager(self.audio, on_success=self._on_calibration_success)

        # Muda por padrão — só sai de STOPPED quando start_exercise() é
        # chamado explicitamente pelo botão "Iniciar" do dashboard/CLI.
        self._state           = FeedbackState.STOPPED
        self._pre_pause_state = FeedbackState.STOPPED
        self._state_since     = time.time()
        self._last_msg        = ""
        self._ok_frames       = 0
        self._reinforce_count = 0
        self._confirm_idx     = 0
        self._session_start   = time.time()
        # RLock (não Lock): _on_calibration_success() é chamado de dentro de
        # calibration.feed(), que roda dentro de _process_frame() já com o
        # lock adquirido — precisa reentrar na mesma thread pra poder mudar
        # de estado (voltar a STOPPED ou seguir pro start_exercise pendente).
        self._lock             = threading.RLock()
        self._stop_event       = threading.Event()
        self._calibration_done   = False
        self._pending_exercise: Optional[Exercise] = None

        self._frame_queue:  "queue.Queue" = queue.Queue(maxsize=2)
        self._render_queue: "queue.Queue" = queue.Queue(maxsize=2)
        self._analysis_thread:  Optional[threading.Thread] = None
        self._broadcast_thread: Optional[threading.Thread] = None

        # Sinal sonoro de repetição contabilizada/rejeitada (exercícios de
        # repetição, ex. agachamento) — lê `exercise.repetitions`/
        # `rejected_reps`/`last_rep_cadence`, atributos públicos que já
        # existem no Exercise, em vez de exigir um canal de eventos novo
        # entre Exercise e AudioCoordinator.
        self._last_rep_count      = 0
        self._last_rejected_reps  = 0

    # ── Início / Pausa / Parada de exercício ────────────────────────────────

    def set_exercise(self, exercise: Exercise):
        """
        Só troca o exercício ativo, sem entrar em BRIEFING — usado quando o
        usuário apenas seleciona um exercício na aba (antes de clicar
        'Iniciar'). A troca em si (rebind de referência) já é atômica sem
        lock, mas todo o resto do estado da Session segue a disciplina de
        só ler/escrever sob self._lock — deixar essa escrita de fora era a
        única exceção inconsistente, sem motivo real pra ser.
        """
        with self._lock:
            self.exercise = exercise
            self._last_rep_count     = 0
            self._last_rejected_reps = 0

    def start_exercise(self, exercise: Exercise):
        """
        Troca o exercício ativo e entra na fase BRIEFING: explica o
        movimento por completo (exercise.start_message) antes de começar
        a monitorar/corrigir — evita que uma correção seja falada por
        cima da explicação inicial.

        Gatilho autônomo do Modo Configuração: se o enquadramento ainda
        não foi validado nesta sessão, redireciona para start_setup() em
        vez de ir direto pro BRIEFING — a calibração acontece primeiro e,
        ao suceder, este mesmo exercício é retomado automaticamente
        (_on_calibration_success), sem exigir nenhum clique extra.
        """
        with self._lock:
            already_calibrated = self._calibration_done
        if not already_calibrated:
            self.start_setup(pending_exercise=exercise)
            return

        with self._lock:
            self.exercise         = exercise
            self._ok_frames       = 0
            self._reinforce_count = 0
            self._last_rep_count     = 0
            self._last_rejected_reps = 0
            self._set_state(FeedbackState.BRIEFING)

        # speak_now() já loga essa fala no painel visual sozinho
        # (AudioCoordinator.log_push) — não precisa duplicar aqui.
        self.audio.speak_now(exercise.start_message)

        threading.Thread(target=self._finish_briefing, daemon=True).start()

    def _finish_briefing(self):
        self.audio.wait_speech_done(timeout=self.BRIEFING_TIMEOUT_S)
        with self._lock:
            if self._state == FeedbackState.BRIEFING:
                self._set_state(FeedbackState.IDLE)

    # ── Modo Configuração (calibração de enquadramento) ─────────────────────

    def start_setup(self, pending_exercise: Optional[Exercise] = None):
        """
        Inicia o Modo Configuração — calibra o enquadramento antes de
        liberar qualquer exercício. `pending_exercise` é setado quando
        start_exercise() redireciona pra cá automaticamente (o exercício
        pedido é retomado sozinho após a calibração); None quando disparado
        manualmente pelo botão dedicado "Iniciar configuração".
        """
        with self._lock:
            self._pending_exercise = pending_exercise
            self._set_state(FeedbackState.SETUP)
        self.calibration.start()
        self.audio.speak_now(
            "Vamos calibrar o enquadramento. Fique de corpo inteiro em "
            "frente à câmera."
        )

    def _on_calibration_success(self):
        """Callback do CalibrationManager — chamado de dentro de
        calibration.feed(), já rodando na thread de análise com o lock
        (reentrante) adquirido."""
        with self._lock:
            self._calibration_done = True
            pending = self._pending_exercise
            self._pending_exercise = None
        if pending is not None:
            self.start_exercise(pending)
        else:
            with self._lock:
                self._set_state(FeedbackState.STOPPED)

    def pause_exercise(self):
        """Muda a análise/áudio imediatamente, sem perder o exercício ativo."""
        with self._lock:
            if self._state in (FeedbackState.STOPPED, FeedbackState.PAUSED):
                return
            self._pre_pause_state = self._state
            self._set_state(FeedbackState.PAUSED)
        self.audio.stop_all()

    def resume_exercise(self):
        """Retoma a análise a partir de um ciclo limpo (IDLE)."""
        with self._lock:
            if self._state != FeedbackState.PAUSED:
                return
            self._ok_frames       = 0
            self._reinforce_count = 0
            self._set_state(FeedbackState.IDLE)

    def stop_exercise(self):
        """Silencia tudo e volta ao estado mudo inicial — precisa clicar
        'Iniciar' de novo para retomar. Também aborta uma calibração
        pendente, se houver."""
        with self._lock:
            self._set_state(FeedbackState.STOPPED)
            self._ok_frames       = 0
            self._reinforce_count = 0
            self._pending_exercise = None
        self.audio.stop_all()

    # ── Estágio 1: captura → fila (rápido, nunca bloqueia a câmera) ────────

    def _enqueue_frame(self, frame: SkeletonFrame):
        _put_drop_oldest(self._frame_queue, frame)

    # ── Estágio 2: análise biomecânica + máquina de estados ────────────────

    def _analysis_loop(self):
        while not self._stop_event.is_set():
            try:
                frame = self._frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process_frame(frame)

    def _process_frame(self, frame: SkeletonFrame):
        now = time.time()
        with self._lock:
            state = self._state
            if state == FeedbackState.STOPPED:
                result = _WAITING_EXERCISE_MSG
            elif state == FeedbackState.PAUSED:
                result = _PAUSED_MSG
            elif state == FeedbackState.SETUP:
                framing = self.calibration.feed(frame)
                result = FeedbackResult(
                    framing.message or "Calibrando enquadramento...",
                    False,
                    Severity.OK if framing.ok else Severity.WARN,
                    framing.issue.value,
                )
            else:
                result = self.exercise.analyze(frame)
                self._check_rep_signal()

            summary = self.reporter.record_skeleton(frame, result)
            self._tick(frame, result)

            # Log terminal — inclui result.detail (fase/ângulo/repetições
            # pro agachamento) pra dar pra diagnosticar pelo terminal sem
            # precisar instrumentar nada na hora.
            elapsed = int(now - self._session_start)
            m, s = divmod(elapsed, 60)
            det  = "✓" if frame.detected else "✗"
            conf = f"{frame.metrics.confidence:.0f}%" if frame.detected else "—"
            sev  = result.severity.value
            extra = f"  ({result.detail})" if result.detail else ""
            print(f"  {m:02d}:{s:02d}  [{det}] conf={conf}  "
                  f"state={self._state.value:<12}  [{sev}] {result.message[:60]}{extra}")

        if self.web_push:
            _put_drop_oldest(self._render_queue, (frame, result, summary))

    def _check_rep_signal(self):
        """Sinal sonoro quando o Exercise ativo contabiliza (ou rejeita)
        uma repetição — lido via `exercise.repetitions`/`rejected_reps`/
        `last_rep_cadence`, atributos públicos que já existem (não todo
        Exercise tem, daí o getattr com default: StaticPostureExercise/
        UnipodialBalanceExercise não contam repetição, vira no-op pra eles).

        `repetitions` só sobe com cadência boa; um ciclo completo mas
        rápido/devagar demais vai pra `rejected_reps` em vez de contar —
        aqui só decidimos o SINAL certo pra cada caso, sem duplicar essa
        regra (ela mora inteira no Exercise).

        Chime, e fala só quando REJEITA: um bipe curto de sucesso não
        compete com a máquina de correção de postura em _tick() (que
        continua sendo a única coisa que decide o que é falado no caminho
        normal) — mas quando a repetição não conta, a pessoa precisa saber
        o motivo, senão fica sem entender por que o número não subiu.
        """
        rep_count = getattr(self.exercise, "repetitions", None)
        rejected  = getattr(self.exercise, "rejected_reps", None)
        if rep_count is None:
            return

        if rep_count != self._last_rep_count:
            self._last_rep_count = rep_count
            self.audio.chime("success")

        if rejected is not None and rejected != self._last_rejected_reps:
            self._last_rejected_reps = rejected
            self.audio.chime("warning")
            cadence = getattr(self.exercise, "last_rep_cadence", None)
            dica = {
                "rapida_demais": "Não contou. Desça e suba com mais controle.",
                "lenta_demais":  "Não contou. Tente manter um ritmo mais contínuo.",
            }.get(cadence, "Não contou — cadência irregular.")
            self.audio.speak_now(dica, Severity.WARN)

    # ── Estágio 3: broadcast Socket.IO, limitado a BROADCAST_MAX_FPS ───────

    def _broadcast_loop(self):
        min_interval = 1.0 / self.BROADCAST_MAX_FPS
        last_emit = 0.0
        while not self._stop_event.is_set():
            try:
                frame, result, summary = self._render_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            wait = min_interval - (time.time() - last_emit)
            if wait > 0:
                time.sleep(wait)
            last_emit = time.time()

            try:
                self.web_push(frame, result, summary)
            except Exception:
                pass

    # ── Máquina de estados ────────────────────────────────────────────────

    def _tick(self, frame: SkeletonFrame, result: FeedbackResult):
        if self._state in (FeedbackState.STOPPED, FeedbackState.PAUSED,
                            FeedbackState.BRIEFING, FeedbackState.SETUP):
            # mudo pra máquina de correção de exercício: aguardando início,
            # pausado, explicando o movimento, ou calibrando enquadramento
            # (a CalibrationManager já cuida do próprio áudio nesse caso)
            return

        ok      = result.severity == Severity.OK
        elapsed = time.time() - self._state_since
        direction = self._direction_hint(frame)

        if self._state == FeedbackState.IDLE:
            if not ok:
                self._set_state(FeedbackState.INSTRUCTING)
                self._last_msg        = result.message
                self._reinforce_count = 0
                self._ok_frames       = 0
                self.audio.emit(result.message, result.severity, direction)

        elif self._state == FeedbackState.INSTRUCTING:
            self._set_state(FeedbackState.WAITING)

        elif self._state == FeedbackState.WAITING:
            if ok:
                self._ok_frames += 1
                if self._ok_frames >= self.CONFIRM_FRAMES:
                    self._set_state(FeedbackState.CONFIRMING)
                    if self.settings.voice.confirm_on_correction:
                        msg = CONFIRMATIONS[self._confirm_idx % len(CONFIRMATIONS)]
                        self._confirm_idx += 1
                        self.audio.emit(msg, Severity.OK, bypass_cooldown=True)
            else:
                self._ok_frames = 0
                self.audio.ambient_tick(direction, 0.0)
                if elapsed >= self.WAIT_WINDOW_S:
                    self._set_state(FeedbackState.REINFORCING)
                    self._reinforce_count += 1
                    if self._reinforce_count <= self.REINFORCE_MAX:
                        self._last_msg = result.message
                        self.audio.emit(result.message, result.severity, direction)
                    else:
                        self.audio.emit(
                            "Tudo bem, descanse um momento e tente de novo.",
                            Severity.WARN, bypass_cooldown=True
                        )
                        self._reinforce_count = 0

        elif self._state == FeedbackState.CONFIRMING:
            self._set_state(FeedbackState.IDLE)
            self._ok_frames = 0

        elif self._state == FeedbackState.REINFORCING:
            self._set_state(FeedbackState.WAITING)
            self._ok_frames = 0

    def _direction_hint(self, frame: SkeletonFrame) -> float:
        """Direção aproximada do desvio para posicionamento estéreo do bipe."""
        m = frame.metrics
        raw = m.trunk_lean_x if m.trunk_lean_x else (m.knee_valgus_l - m.knee_valgus_r)
        return max(-1.0, min(1.0, raw / 15.0))

    def _set_state(self, s: FeedbackState):
        self._state       = s
        self._state_since = time.time()

    # ── Run / Stop ────────────────────────────────────────────────────────

    def run(self):
        self.tracker.on_frame = self._enqueue_frame
        self._analysis_thread = threading.Thread(
            target=self._analysis_loop, daemon=True, name="session-analysis"
        )
        self._broadcast_thread = threading.Thread(
            target=self._broadcast_loop, daemon=True, name="session-broadcast"
        )
        self._analysis_thread.start()
        self._broadcast_thread.start()

        print(f"\n  {'TEMPO':>5}  DET  CONF    ESTADO          FEEDBACK")
        print("  " + "─" * 65)
        self._stop_event.wait()

    def stop(self):
        self._stop_event.set()
        for t in (self._analysis_thread, self._broadcast_thread):
            if t and t.is_alive():
                t.join(timeout=2.0)
