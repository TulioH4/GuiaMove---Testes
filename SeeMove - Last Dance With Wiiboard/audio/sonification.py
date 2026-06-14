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

Requer:
    pip install sounddevice numpy
"""

import math
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

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and _AUDIO_AVAILABLE
        if enabled and not _AUDIO_AVAILABLE:
            print("[sonification] sounddevice/numpy não instalados.")
            print("  Execute: pip install sounddevice numpy")

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
        if audio is None:
            return

        if blocking:
            sd.play(audio, self.SAMPLE_RATE)
            sd.wait()
        else:
            # Toca em thread separada para não bloquear o loop
            t = threading.Thread(
                target=lambda: (sd.play(audio, self.SAMPLE_RATE), sd.wait()),
                daemon=True,
            )
            t.start()

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
                if audio is not None:
                    sd.play(audio, self.SAMPLE_RATE)
                    sd.wait()
                time.sleep(0.05)

        t = threading.Thread(target=_play_seq, daemon=True)
        t.start()
