"""
core/session.py
Loop principal — driven por callbacks do hardware/simulador.
Feedback de áudio com cooldown inteligente:
  - Só fala quando a mensagem muda
  - Mínimo 4s entre falas (evita poluição sonora)
  - Mensagens de erro têm cooldown reduzido (2s)
"""

import time
import threading
from core.balance_board import SensorData
from core.cog import calculate_cog, CoGHistory, CoGStats
from audio.tts_engine import TTSEngine
from audio.sonification import SonificationEngine
from exercises.base import Exercise
from reports.reporter import SessionReporter
from config.settings import Settings


class Session:
    def __init__(self, board, tts, sonification, exercise, settings, reporter,
                 web_push=None, web_tts=None, web_status=None):
        self.board = board
        self.tts = tts
        self.sonification = sonification
        self.exercise = exercise
        self.settings = settings
        self.reporter = reporter
        self.web_push = web_push
        self.web_tts = web_tts
        self.web_status = web_status

        self.history = CoGHistory(max_size=300)
        self.stats = CoGStats()

        self._last_tts_time = 0.0
        self._last_feedback_msg = ""
        self._last_severity = "ok"
        self._session_start = time.time()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def _on_data(self, data: SensorData):
        """Callback chamado pelo hardware/simulador a cada frame."""
        with self._lock:
            # --- FREIO ABSOLUTO (10 FPS) ---
            now = time.time()
            if not hasattr(self, '_last_update_time'):
                self._last_update_time = 0.0
                
            if now - self._last_update_time < 0.1:
                return  # Descarta o excesso de pacotes (salva a memória RAM)
            self._last_update_time = now
            # -------------------------------
            
            # Se o peso for muito baixo, os cálculos de COG ficam instáveis.
            if data.total() < 10.0:  
                return # Ignora completamente para não travar a matemática

            cog = calculate_cog(data, threshold=self.settings.threshold)
            if cog is None:
                return

            self.history.push(cog)
            self.stats.update(cog)
            self.reporter.record(data, cog)

            smoothed = self.history.smoothed(window=self.settings.smoothing_window)
            sx, sy = smoothed if smoothed else (cog.x, cog.y)

            feedback = self.exercise.analyze(sx, sy, cog.total_kg)
            summary = self.reporter.summary()

            # Dashboard
            if self.web_push:
                try:
                    self.web_push(data, cog, feedback, summary)
                except Exception:
                    pass

            # Áudio — lógica de cooldown inteligente
            self._maybe_speak(feedback)

            # Log terminal
            elapsed = int(time.time() - self._session_start)
            m, s = divmod(elapsed, 60)
            status = "OK" if cog.is_centered else "CORRIGIR"
            print(f"  {m:02d}:{s:02d}   {cog.x:+.3f}  {cog.y:+.3f}  "
                  f"{cog.total_kg:6.1f}kg  {status}  {feedback.message}")

    def _maybe_speak(self, feedback):
        """
        Emite áudio apenas quando necessário:
        - Mudança de mensagem
        - Cooldown respeitado (4s normal, 2s para erros)
        """
        if not feedback.should_speak:
            # Se voltou ao estado ok e a última mensagem foi uma correção,
            # confirma apenas uma vez
            if (self._last_severity != "ok" and feedback.severity == "ok"
                    and time.time() - self._last_tts_time > 2.0):
                self._speak("Certo.", "ok")
            return

        now = time.time()
        cooldown = 2.0 if feedback.severity == "error" else 4.0
        msg_changed = feedback.message != self._last_feedback_msg
        time_ok = (now - self._last_tts_time) >= cooldown

        if msg_changed or time_ok:
            self._speak(feedback.message, feedback.severity)

    def _speak(self, message: str, severity: str):
        now = time.time()
        if self.settings.tts_enabled:
            self.tts.speak(message)
        if self.settings.sonification_enabled:
            self.sonification.play(
                self._last_feedback_msg != "ok",  # usa severidade como sinal
                0.0
            )
        if self.web_tts:
            try:
                self.web_tts(message, severity)
            except Exception:
                pass
        self._last_tts_time = now
        self._last_feedback_msg = message
        self._last_severity = severity

    def run(self):
        """Conecta callbacks e bloqueia até KeyboardInterrupt."""
        self.board.on_data = self._on_data
        if self.web_status:
            self.board.on_status = lambda msg: self.web_status(msg)

        print(f"\n  {'TEMPO':>5}  {'X':>7}  {'Y':>7}  {'TOTAL':>8}  STATUS")
        print("  " + "-" * 52)

        # Para hardware baseado em thread (on_data callback), apenas aguarda
        # Para modo polling legado, lê em loop
        if hasattr(self.board, '_thread'):
            self._stop_event.wait()  # desbloqueia via stop()
        else:
            while not self._stop_event.is_set():
                data = self.board.read()
                if data:
                    self._on_data(data)

    def stop(self):
        self._stop_event.set()
