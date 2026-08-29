"""
audio/coordinator.py
Facade que coordena voz e sonificação para que nunca se sobreponham de
forma confusa, aplica os cooldowns configuráveis por severidade
(VoiceSettings.cooldown_ok_s/warn_s/error_s), e decide ONDE o áudio é
de fato reproduzido:

  - Sink local (padrão, modo --no-web): TTSEngine (pyttsx3/nuvem) +
    SonificationEngine tocando no próprio backend via sounddevice.
  - Sink remoto (dashboard web ativo): nenhum áudio toca no backend —
    o coordinator só empacota eventos semânticos (audio_speak/audio_cue/
    audio_ambient/audio_stop) e os entrega via `remote_push(evento, payload)`
    para o navegador tocar via Web Speech API + Web Audio API, com
    latência zero e cancelamento instantâneo (impossível de garantir com
    pyttsx3/sounddevice no backend).

`remote_push` e `log_push` seguem o mesmo padrão de injeção já usado por
Session.web_push (setado externamente por main.py/web/server.py depois
que a Session já existe) — quando são None, o comportamento local de
sempre continua intacto.

`log_push(message, severity)` é chamado toda vez que uma mensagem é
efetivamente falada (local ou remota) — é o único lugar que alimenta o
painel "Log de feedback de áudio" do dashboard, então qualquer fala que
passe por emit()/speak_now() aparece lá automaticamente, sem cada
chamador precisar lembrar de logar manualmente.
"""
import threading
import time
from typing import Callable, Optional

from exercises.base import Severity

# palavras por minuto usado como referência para converter VoiceSettings.rate
# em rate relativo (1.0 = normal) para SpeechSynthesisUtterance.rate no browser
_REFERENCE_WPM = 145.0


class AudioCoordinator:
    def __init__(self, tts, sonification, voice_settings, sonification_enabled: bool = True,
                 remote_push: Optional[Callable[[str, dict], None]] = None,
                 log_push: Optional[Callable[[str, str], None]] = None):
        self.tts = tts
        self.sonification = sonification
        self.voice_settings = voice_settings
        self.sonification_enabled = sonification_enabled
        self.remote_push = remote_push  # setável depois — session.audio.remote_push = fn
        self.log_push = log_push        # idem — session.audio.log_push = fn

        self._last_emit_ts = {
            Severity.OK: 0.0,
            Severity.WARN: 0.0,
            Severity.ERROR: 0.0,
        }
        self._ambient_lock = threading.Lock()
        self._ambient_active = False
        self._last_ambient_ts = 0.0
        self.AMBIENT_INTERVAL_S = 1.2
        self._remote_speech_done_ts = 0.0

    def _cooldown_for(self, severity: Severity) -> float:
        v = self.voice_settings
        return {
            Severity.OK: v.cooldown_ok_s,
            Severity.WARN: v.cooldown_warn_s,
            Severity.ERROR: v.cooldown_error_s,
        }.get(severity, v.cooldown_warn_s)

    def _is_remote(self) -> bool:
        return self.remote_push is not None

    def _log(self, message: str, severity_value: str):
        if self.log_push:
            try:
                self.log_push(message, severity_value)
            except Exception:
                pass

    def _push_speak(self, message: str, cancel: bool):
        rate_ratio = max(0.5, min(2.5, self.voice_settings.rate / _REFERENCE_WPM))
        self.remote_push("audio_speak", {
            "text": message,
            "rate": rate_ratio,
            "volume": self.voice_settings.volume,
            "cancel": cancel,
        })
        # Estimativa de duração da fala (~ rate wpm) — usada só para
        # wait_speech_done() saber quando o briefing provavelmente terminou,
        # já que o browser não devolve um evento de "terminei de falar".
        words = max(1, len(message.split()))
        duration = max(0.8, words / (self.voice_settings.rate / 60.0))
        self._remote_speech_done_ts = time.time() + duration

    def emit(self, message: str, severity: Severity, direction_hint: float = 0.0,
              bypass_cooldown: bool = False) -> bool:
        """
        Emite feedback (bipe + fala) respeitando o cooldown da severidade.
        Retorna True se de fato emitiu, False se suprimido pelo cooldown.
        """
        if not message:
            return False

        now = time.time()
        if not bypass_cooldown:
            elapsed = now - self._last_emit_ts[severity]
            if elapsed < self._cooldown_for(severity):
                return False
        self._last_emit_ts[severity] = now

        self.stop_ambient()

        if self._is_remote():
            if self.sonification_enabled and severity != Severity.OK:
                self.remote_push("audio_cue", {"severity": severity.value, "pan": direction_hint})
            if self.voice_settings.enabled:
                self._push_speak(message, cancel=True)
                self._log(message, severity.value)
            return True

        if self.sonification_enabled and severity != Severity.OK:
            self.sonification.play_cue(severity.value, pan=direction_hint, blocking=True)

        if self.voice_settings.enabled:
            self.tts.speak(message)
            self._log(message, severity.value)

        return True

    def speak_now(self, message: str, severity: Severity = Severity.OK):
        """Bypassa fila/cooldown — usado para alertas críticos (ex.: perda de câmera)
        e para falas fora do ciclo normal de correção (briefing, calibração de
        enquadramento). `severity` só afeta a cor da entrada no log visual."""
        if not message:
            return
        self.stop_ambient()
        if not self.voice_settings.enabled:
            return
        if self._is_remote():
            self._push_speak(message, cancel=True)
        else:
            self.tts.speak_now(message)
        self._log(message, severity.value)

    def wait_speech_done(self, timeout: float = 15.0):
        """Bloqueia até a fala terminar (ou timeout) — usado na fase de briefing."""
        deadline = time.time() + timeout
        if self._is_remote():
            while time.time() < deadline:
                if time.time() >= self._remote_speech_done_ts:
                    return
                time.sleep(0.1)
            return
        while time.time() < deadline:
            if self.tts.queue_empty():
                return
            time.sleep(0.1)

    def ambient_tick(self, direction_x: float, direction_y: float):
        """
        Pulso suave de sonificação de fundo — chamado repetidamente pela
        Session enquanto está em WAITING (aguardando o usuário corrigir).
        Não compete com fala: é sempre menos intrusivo que emit().

        Auto-throttled para no máximo 1 pulso a cada AMBIENT_INTERVAL_S,
        mesmo que a Session chame isso a cada frame (~10 Hz) — evita
        encher a fila de áudio de sonificação sem necessidade.
        """
        if not self.sonification_enabled:
            return

        now = time.time()
        with self._ambient_lock:
            if now - self._last_ambient_ts < self.AMBIENT_INTERVAL_S:
                return
            self._last_ambient_ts = now
            self._ambient_active = True

        if self._is_remote():
            self.remote_push("audio_ambient", {"x": direction_x, "y": direction_y})
        else:
            self.sonification.play_ambient_pulse(direction_x, direction_y)

    def stop_ambient(self):
        with self._ambient_lock:
            self._ambient_active = False

    def chime(self, pattern: str = "success"):
        """
        Sinal sonoro curto de comunicação de estado (ex.: calibração de
        enquadramento concluída) — usa SonificationEngine.play_sequence()
        em modo local (já existia, nunca tinha sido ligada a nada) ou
        manda um evento pro browser tocar via Web Audio API em modo remoto.
        """
        if not self.sonification_enabled:
            return
        if self._is_remote():
            self.remote_push("audio_chime", {"pattern": pattern})
        else:
            self.sonification.play_sequence(pattern)

    def stop_all(self):
        """
        Interrompe qualquer áudio em andamento/pendente imediatamente —
        usado por Pausar/Parar. Em modo remoto, manda o browser cancelar
        speechSynthesis e osciladores ativos; em modo local, drena as
        filas e termina o subprocess de fala em andamento.
        """
        self.stop_ambient()
        if self._is_remote():
            self.remote_push("audio_stop", {})
            self._remote_speech_done_ts = 0.0
            return

        self.tts.stop_all()
        self.sonification.clear_queue()
