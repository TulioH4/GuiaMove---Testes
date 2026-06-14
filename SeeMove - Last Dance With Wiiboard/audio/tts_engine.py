"""
audio/tts_engine.py
Motor de síntese de voz (Text-to-Speech) para feedback corretivo.

Usa pyttsx3 — biblioteca offline que aproveita as vozes nativas do sistema:
  - Windows: Microsoft SAPI (vozes do Windows, incluindo pt-BR se instaladas)
  - Linux: espeak-ng
  - macOS: NSSpeechSynthesizer

Instalação:
    pip install pyttsx3

Para verificar vozes disponíveis em português:
    python -c "import pyttsx3; e=pyttsx3.init(); [print(v.id,v.name) for v in e.getProperty('voices')]"
"""

import threading
import queue
from typing import Optional


class TTSEngine:
    """
    Motor TTS com fila assíncrona para não bloquear o loop de sensores.

    O feedback de áudio é enfileirado e executado em thread separada,
    garantindo que a leitura dos sensores continue ininterrupta.
    """

    def __init__(
        self,
        enabled: bool = True,
        rate: int = 145,
        volume: float = 1.0,
        voice_lang: str = "pt",
    ):
        self.enabled = enabled
        self._rate = rate
        self._volume = volume
        self._voice_lang = voice_lang
        self._engine = None
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        if enabled:
            self._init_engine()
            self._start_worker()

    def _init_engine(self):
        """Inicializa o engine pyttsx3 e seleciona voz em português se disponível."""
        try:
            import pyttsx3
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", self._rate)
            self._engine.setProperty("volume", self._volume)
            self._select_voice()
        except ImportError:
            print("[tts] pyttsx3 não instalado. Execute: pip install pyttsx3")
            self.enabled = False
        except Exception as e:
            print(f"[tts] Erro ao inicializar: {e}")
            self.enabled = False

    def _select_voice(self):
        """Seleciona a primeira voz disponível no idioma configurado."""
        if not self._engine:
            return
        voices = self._engine.getProperty("voices")
        for voice in voices:
            if self._voice_lang.lower() in voice.id.lower() or \
               self._voice_lang.lower() in voice.name.lower():
                self._engine.setProperty("voice", voice.id)
                print(f"[tts] Voz selecionada: {voice.name}")
                return
        print(f"[tts] Voz em '{self._voice_lang}' não encontrada. Usando voz padrão.")
        print("[tts] Instale vozes pt-BR em: Configurações > Hora e idioma > Fala")

    def _start_worker(self):
        """Inicia a thread de execução do TTS."""
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self):
        """Thread que consome a fila de mensagens e executa o TTS."""
        while self._running:
            try:
                message = self._queue.get(timeout=0.5)
                if message is None:
                    break
                if self._engine:
                    self._engine.say(message)
                    self._engine.runAndWait()
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[tts] Erro na execução: {e}")

    def speak(self, message: str, priority: bool = False):
        """
        Enfileira uma mensagem para síntese de voz.

        Args:
            message: Texto a ser falado.
            priority: Se True, limpa a fila antes de enfileirar
                      (usa para mensagens urgentes de segurança).
        """
        if not self.enabled or not message.strip():
            return

        if priority:
            # Esvazia fila para falar imediatamente
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break

        self._queue.put(message)

    def speak_now(self, message: str):
        """Fala imediatamente, descartando mensagens pendentes."""
        self.speak(message, priority=True)

    def set_rate(self, rate: int):
        """Ajusta velocidade da voz (palavras por minuto). Padrão: 145."""
        self._rate = rate
        if self._engine:
            self._engine.setProperty("rate", rate)

    def set_volume(self, volume: float):
        """Ajusta volume (0.0 a 1.0)."""
        self._volume = max(0.0, min(1.0, volume))
        if self._engine:
            self._engine.setProperty("volume", self._volume)

    def stop(self):
        """Encerra a thread de TTS."""
        self._running = False
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=2.0)

    def __del__(self):
        self.stop()
