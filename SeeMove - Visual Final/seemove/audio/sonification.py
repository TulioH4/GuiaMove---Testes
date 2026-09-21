"""
audio/sonification.py
Sonificação espacial para feedback direcional instintivo.

Em vez de esperar a síntese de voz, o usuário recebe um bipe cuja
frequência e posição estéreo indicam a direção e intensidade do desvio:

  Eixo X (lateral):
    Desvio à direita → bipe mais alto no canal direito
    Desvio à esquerda → bipe mais alto no canal esquerdo

  Eixo Y (ântero-posterior):
    Desvio para frente → tom mais agudo
    Desvio para trás   → tom mais grave

  CoG centralizado → bipe suave e centralizado (confirmação positiva)

Todo o áudio é reproduzido por uma única thread worker (fila serial),
nunca em threads ad-hoc concorrentes: chamar sd.play()/sd.wait() de
várias threads ao mesmo tempo colide no stream padrão do PortAudio e
pode derrubar o processo com um crash nativo (access violation) —
sintoma observado no Windows como "A instrução ... referenciou a
memória ... A memória não pôde ser written."

Requer:
    pip install sounddevice numpy
"""

import math
import queue
import threading
from typing import Optional

try:
    import numpy as np
    import sounddevice as sd
    _AUDIO_AVAILABLE = True
except ImportError:
    _AUDIO_AVAILABLE = False


class SonificationEngine:
    """
    Gerador de tons de feedback espacial (stereo panning + frequência).

    Parâmetros de mapeamento:
        BASE_FREQ  : frequência central (Hz) — tom neutro quando centralizado
        FREQ_RANGE : variação máxima de frequência para desvio no eixo Y
        DURATION   : duração do bipe (segundos)
        SAMPLE_RATE: taxa de amostragem de áudio
    """

    BASE_FREQ = 440.0     # Lá4 — referência musical para postura centralizada
    FREQ_RANGE = 200.0    # ±200 Hz para desvio máximo no eixo Y
    DURATION = 0.18       # segundos — bipe curto e não intrusivo
    SAMPLE_RATE = 44100

    # Nº máximo de pulsos ambiente pendentes na fila — evita acumular
    # áudio atrasado quando o usuário fica muito tempo em WAITING.
    _AMBIENT_QUEUE_LIMIT = 2

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and _AUDIO_AVAILABLE
        if enabled and not _AUDIO_AVAILABLE:
            print("[sonification] sounddevice/numpy não instalados.")
            print("  Execute: pip install sounddevice numpy")

        self._queue: "queue.Queue" = queue.Queue()
        self._pending_ambient = 0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._worker, daemon=True, name="sonification-worker"
            )
            self._thread.start()

    def _worker(self):
        """Única thread que efetivamente toca áudio — serializa todo
        acesso ao sounddevice/PortAudio, evitando chamadas concorrentes."""
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break

            audio, done_event, is_ambient = item
            try:
                sd.play(audio, self.SAMPLE_RATE)
                sd.wait()
            except Exception as ex:
                print(f"[sonification] Erro ao reproduzir áudio: {ex}")
            finally:
                if is_ambient:
                    with self._lock:
                        self._pending_ambient = max(0, self._pending_ambient - 1)
                if done_event is not None:
                    done_event.set()
                self._queue.task_done()

    def _enqueue(self, audio, blocking: bool = False,
                  is_ambient: bool = False) -> bool:
        if audio is None or self._thread is None:
            return False

        if is_ambient:
            with self._lock:
                if self._pending_ambient >= self._AMBIENT_QUEUE_LIMIT:
                    return False  # descarta — evita acumular atraso de fundo
                self._pending_ambient += 1

        done_event = threading.Event() if blocking else None
        self._queue.put((audio, done_event, is_ambient))
        if blocking:
            done_event.wait(timeout=2.0)
        return True

    def _generate_tone(
        self,
        frequency: float,
        pan: float,
        duration: float,
        envelope: str = "hann",
    ) -> Optional["np.ndarray"]:
        """
        Gera um tom senoidal estéreo com panning e envelope de amplitude.

        Args:
            frequency: Frequência em Hz.
            pan: Posição estéreo de -1.0 (esquerda) a +1.0 (direita).
            duration: Duração em segundos.
            envelope: 'hann' (suave) ou 'linear' (simples fade-out).

        Returns:
            Array numpy (samples, 2) para reprodução estéreo.
        """
        if not _AUDIO_AVAILABLE:
            return None

        n_samples = int(self.SAMPLE_RATE * duration)
        t = np.linspace(0, duration, n_samples, endpoint=False)

        # Tom senoidal
        wave = np.sin(2 * math.pi * frequency * t)

        # Envelope para evitar cliques digitais
        if envelope == "hann":
            env = np.hanning(n_samples)
        else:
            env = np.linspace(1, 0, n_samples)
        wave *= env * 0.4  # amplitude máxima de 40%

        # Stereo panning (lei de potência constante)
        angle = (pan + 1.0) / 2.0 * math.pi / 2.0  # 0 a π/2
        left = math.cos(angle)
        right = math.sin(angle)

        stereo = np.column_stack([wave * left, wave * right])
        return stereo.astype(np.float32)

    def play(self, cog_x: float, cog_y: float, blocking: bool = False):
        """
        Toca um bipe de feedback baseado na posição do CoG.

        Args:
            cog_x: Desvio lateral normalizado [-1, +1].
            cog_y: Desvio ântero-posterior normalizado [-1, +1].
            blocking: Se True, aguarda o áudio terminar antes de retornar.
        """
        if not self.enabled:
            return

        magnitude = math.sqrt(cog_x ** 2 + cog_y ** 2)

        if magnitude < 0.05:
            # CoG centralizado — bipe suave de confirmação (meio tom acima)
            frequency = self.BASE_FREQ * 1.06
            pan = 0.0
            duration = self.DURATION * 0.7
        else:
            # Mapeia desvios para frequência e posição estéreo
            frequency = self.BASE_FREQ + cog_y * self.FREQ_RANGE
            frequency = max(200.0, min(900.0, frequency))
            pan = max(-1.0, min(1.0, cog_x * 1.5))  # amplifica desvio lateral
            duration = self.DURATION

        audio = self._generate_tone(frequency, pan, duration)
        self._enqueue(audio, blocking=blocking)

    def play_cue(self, severity: str, pan: float = 0.0, blocking: bool = True) -> float:
        """
        Toca um bipe curto de alerta ligado à severidade (usado pelo
        AudioCoordinator antes de enfileirar a fala, para que bipe e voz
        nunca se sobreponham). Retorna a duração do bipe em segundos.

        Args:
            severity: "ok" | "warn" | "error".
            pan: posição estéreo [-1, +1] (direção do desvio, se conhecida).
            blocking: se True, aguarda o bipe terminar antes de retornar
                      (usado pelo coordinator para sequenciar com a fala).
        """
        if not self.enabled:
            return 0.0

        freq_by_severity = {"ok": 587.0, "warn": 349.0, "error": 261.0}
        dur_by_severity   = {"ok": 0.12, "warn": 0.16, "error": 0.22}
        frequency = freq_by_severity.get(severity, 349.0)
        duration  = dur_by_severity.get(severity, 0.16)

        audio = self._generate_tone(frequency, max(-1.0, min(1.0, pan)), duration)
        self._enqueue(audio, blocking=blocking)
        return duration

    def play_ambient_pulse(self, cog_x: float, cog_y: float):
        """
        Pulso suave usado como sonificação de fundo enquanto o usuário
        está corrigindo a postura (estado WAITING) — mais discreto que
        `play()`, sem intenção de alerta. Enfileirado (não bloqueante);
        descartado silenciosamente se já houver pulsos pendentes, para
        não acumular atraso de áudio.
        """
        if not self.enabled:
            return

        magnitude = math.sqrt(cog_x ** 2 + cog_y ** 2)
        frequency = self.BASE_FREQ + cog_y * self.FREQ_RANGE * 0.5
        frequency = max(250.0, min(750.0, frequency))
        pan = max(-1.0, min(1.0, cog_x * 1.2))
        duration = self.DURATION * (0.5 if magnitude < 0.05 else 0.8)

        audio = self._generate_tone(frequency, pan, duration)
        if audio is not None:
            # Amplitude reduzida — pulso de fundo, não deve competir com a fala
            audio = (audio * 0.5).astype(np.float32)
        self._enqueue(audio, blocking=False, is_ambient=True)

    def play_sequence(self, pattern: str):
        """
        Sequências de bipes para comunicação de estado.

        Padrões disponíveis:
            'start'   — 3 bipes ascendentes (início de exercício)
            'success' — 2 bipes agudos (meta atingida)
            'warning' — 1 bipe grave longo (atenção)
            'end'     — sequência descendente (fim de sessão)
        """
        if not self.enabled:
            return

        sequences = {
            "start":   [(440, 0.12), (550, 0.12), (660, 0.15)],
            "success": [(660, 0.15), (880, 0.20)],
            "warning": [(220, 0.40)],
            "end":     [(660, 0.12), (550, 0.12), (440, 0.12), (330, 0.20)],
        }

        tones = sequences.get(pattern, [])

        def _play_seq():
            import time
            for freq, dur in tones:
                audio = self._generate_tone(freq, 0.0, dur)
                # blocking=True serializa naturalmente através da fila
                # única do worker — cada tom só toca depois do anterior.
                self._enqueue(audio, blocking=True)
                time.sleep(0.05)

        t = threading.Thread(target=_play_seq, daemon=True)
        t.start()

    def clear_queue(self):
        """Descarta tons pendentes (usado por Pausar/Parar) sem derrubar
        a worker thread — a fila volta a aceitar novos tons normalmente.
        Também corta na hora o tom que já estiver tocando: sem o
        sd.stop(), só a fila era drenada e um bipe em andamento (até
        ~0.2s) continuava até o fim, diferente do TTSEngine.stop_all(),
        que já cortava a fala imediatamente — a promessa de silêncio
        instantâneo ao Pausar/Parar não valia igualmente para os dois."""
        with self._lock:
            self._pending_ambient = 0
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        if _AUDIO_AVAILABLE:
            try:
                sd.stop()
            except Exception:
                pass

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=2.0)
