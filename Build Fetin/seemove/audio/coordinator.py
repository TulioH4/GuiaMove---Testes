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

# Duração estimada de uma fala no navegador — SOBRESTIMADA de propósito. É um TETO: o navegador confirma o fim real
# ("audio_done" → speech_finished()) e isso só ENCURTA. Uma estimativa curta demais faz quem pergunta "ainda está
# falando?" (calibração, fim do briefing) achar que acabou e cortar a fala no meio: "Seus pés não aparecem…" cortava
# "Vamos calibrar o enquadramento…" porque a conta por nº de palavras ignorava a latência da voz e as pausas entre frases.
# Medido no navegador com a voz Microsoft Maria (pt-BR, ritmo 1,0): 0,10 a 0,15 s por caractere no total; a abertura da
# calibração (74 caracteres) leva 7,5 s com a página parada e 11 s com o painel recebendo quadros e desenhando o boneco
# (o navegador atrasa o início de cada frase); "Série completa, 5 repetições. Bom trabalho!" leva 6,4 s. A conta por nº
# de palavras dava 5,0 s e 2,9 s. As constantes abaixo ficam acima de todos os tempos medidos, inclusive sob carga.
_SPEECH_S_PER_CHAR = 0.14    # tempo por caractere no ritmo normal
_SPEECH_STARTUP_S  = 1.0     # do envio até a voz começar (cancelamento, latência da voz)
_SPEECH_SENTENCE_S = 0.6     # pausa/sobra por frase
_SPEECH_COMMA_S    = 0.3     # pausa por vírgula
_SPEECH_DIGIT_S    = 0.4     # cada dígito é falado como palavra ("5" → "cinco")


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
        # Id da última fala enviada ao navegador — ele devolve "audio_done"
        # com o id quando a fala TERMINA de verdade (ver speech_finished()).
        self._speech_id = 0

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
        now = time.time()
        self._speech_id += 1
        self.remote_push("audio_speak", {
            "id": self._speech_id,
            "text": message,
            "rate": rate_ratio,
            "volume": self.voice_settings.volume,
            "cancel": cancel,
        })
        # Teto de duração usado por wait_speech_done()/is_speaking() enquanto o navegador não confirma o fim real
        # (speech_finished()); sem nenhum navegador aberto, é a única fonte. Uma fala que ENTRA NA FILA
        # (cancel=False) só começa quando a atual terminar, então a duração se soma à que já estava pendente.
        start = now if cancel else max(now, self._remote_speech_done_ts)
        self._remote_speech_done_ts = start + self._estimate_speech_s(message, rate_ratio)

    @staticmethod
    def _estimate_speech_s(message: str, rate_ratio: float) -> float:
        frases = max(1, sum(message.count(c) for c in ".!?"))
        virgulas = message.count(",")
        digitos = sum(ch.isdigit() for ch in message)
        return (max(1.0, len(message) * _SPEECH_S_PER_CHAR / rate_ratio) + _SPEECH_STARTUP_S
                + _SPEECH_SENTENCE_S * frases + _SPEECH_COMMA_S * virgulas + _SPEECH_DIGIT_S * digitos)

    # Fala pendente (em curso + na fila) acima disto: uma mensagem NÃO urgente nova é descartada em vez de entrar
    # atrasada demais na fila (a correção seria dita depois de a pessoa já ter mudado de movimento).
    MAX_BACKLOG_S = 30.0

    def _backlog_s(self) -> float:
        if not self._is_remote():
            return 0.0
        return max(0.0, self._remote_speech_done_ts - time.time())

    def speech_finished(self, speech_id: int):
        """O navegador avisa que terminou (ou teve cortada) a fala `speech_id`.
        Só a ÚLTIMA fala enviada importa — o fim de uma anterior, cancelada
        por uma mais nova, não diz nada sobre a atual. Só ENCURTA a
        estimativa, nunca a estende."""
        if speech_id == self._speech_id:
            self._remote_speech_done_ts = min(self._remote_speech_done_ts, time.time())

    def emit(self, message: str, severity: Severity, direction_hint: float = 0.0,
              bypass_cooldown: bool = False, cancel: bool = False) -> bool:
        """
        Emite feedback (bipe + fala) respeitando o cooldown da severidade.
        Retorna True se de fato emitiu, False se suprimido pelo cooldown.

        `cancel=False` (padrão; modo remoto/navegador) faz a fala entrar na fila do
        speechSynthesis em vez de cortar o que já está tocando. Antes só a
        confirmação positiva era assim; correções e reforços cortavam a fala em curso
        (uma repetição explicada pela metade, o "Bom trabalho" da série) e quem não
        enxerga perdia a frase. Se já há fala demais pendente (MAX_BACKLOG_S), a
        mensagem é descartada: a máquina de estados repete a correção depois.
        """
        if not message:
            return False
        if self._backlog_s() > self.MAX_BACKLOG_S:
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
                self._push_speak(message, cancel=cancel)
                self._log(message, severity.value)
            return True

        if self.sonification_enabled and severity != Severity.OK:
            self.sonification.play_cue(severity.value, pan=direction_hint, blocking=True)

        if self.voice_settings.enabled:
            self.tts.speak(message)
            self._log(message, severity.value)

        return True

    def speak_now(self, message: str, severity: Severity = Severity.OK, interrupt: bool = True):
        """Bypassa o cooldown — usado para alertas críticos (ex.: perda de câmera), respostas a uma
        ação do usuário e falas fora do ciclo normal de correção (briefing, calibração).
        `interrupt=True` (padrão) corta a fala em curso; `interrupt=False` entra na fila e fala DEPOIS dela
        (avisos que não podem atropelar a fala em andamento: instruções da calibração, resultado de
        repetição) e é descartada se já há fala demais pendente. `severity` só afeta a cor no log visual."""
        if not message:
            return
        self.stop_ambient()
        if not self.voice_settings.enabled:
            return
        if not interrupt and self._backlog_s() > self.MAX_BACKLOG_S:
            return
        if self._is_remote():
            self._push_speak(message, cancel=interrupt)
        elif interrupt:
            self.tts.speak_now(message)
        else:
            self.tts.speak(message)
        self._log(message, severity.value)

    def is_speaking(self) -> bool:
        """Estimativa de "ainda estou falando algo" — usado por quem repete
        um aviso periodicamente (ex.: CalibrationManager) pra nunca cortar
        uma fala em andamento no meio, só porque o cooldown entre avisos
        já passou. Em modo remoto o navegador não devolve um evento de "já
        terminei de falar", então isso é uma estimativa por duração (ver
        _push_speak); em modo local, a fila do TTSEngine é a fonte real."""
        if self._is_remote():
            return time.time() < self._remote_speech_done_ts
        return not self.tts.queue_empty()

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
