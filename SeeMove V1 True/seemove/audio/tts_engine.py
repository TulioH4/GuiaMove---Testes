"""
audio/tts_engine.py
Motor TTS com provedor plugável (Strategy pattern via audio/tts_providers.py).

Por padrão usa pyttsx3 (offline, subprocess isolado por fala — evita
conflitos COM/SAPI/threading no Windows). Pode ser configurado para usar
vozes de nuvem (Azure, Google, ElevenLabs) mantendo a mesma API pública
(speak, speak_now, set_rate, set_volume, set_voice, get_voices, stop)
usada por core/session.py e web/server.py.

Se o provedor de nuvem falhar em tempo de execução (rede, chave inválida),
a fala específica cai automaticamente para um Pyttsx3Provider de reserva
em vez de derrubar a sessão.
"""
import threading
import queue
from typing import Optional, List, Dict

from audio.tts_providers import (
    TTSProvider, Pyttsx3Provider, create_tts_provider, synthesize_cached,
)

try:
    import numpy as np
    import soundfile as sf
    import sounddevice as sd
    _PLAYBACK_AVAILABLE = True
except ImportError:
    _PLAYBACK_AVAILABLE = False


class TTSEngine:
    def __init__(self, enabled: bool = True, rate: int = 145,
                 volume: float = 1.0, voice_lang: str = "pt",
                 voice_settings=None):
        """
        voice_settings: config.settings.VoiceSettings opcional — se
        fornecido, define o provedor (pyttsx3/azure/google/elevenlabs) e
        credenciais. Sem ele, usa sempre pyttsx3 (comportamento anterior).
        """
        self.enabled     = enabled
        self._rate       = rate
        self._volume     = volume
        self._voice_lang = voice_lang
        self._voice_id:  Optional[str] = None
        self._voices:    List[Dict]    = []
        self._queue:     queue.Queue   = queue.Queue()
        self._thread:    Optional[threading.Thread] = None
        self._running    = False
        self._ready      = threading.Event()

        self._voice_settings   = voice_settings
        self._provider_name    = (voice_settings.provider if voice_settings else "pyttsx3")
        self._provider: Optional[TTSProvider] = None
        self._fallback: Optional[TTSProvider] = None
        self._current_proc = None   # subprocess.Popen em andamento (pyttsx3), p/ stop_all()
        self._proc_lock = threading.Lock()

        if enabled:
            self._load_provider_bg()
            self._ready.wait(timeout=5.0)
            self._start_worker()

    def _load_provider_bg(self):
        """Carrega o provedor (e lista de vozes) em background sem bloquear o __init__."""
        def _load():
            try:
                if self._voice_settings is not None:
                    self._provider = create_tts_provider(self._voice_settings)
                else:
                    self._provider = Pyttsx3Provider()
                self._voices = self._provider.list_voices()
                for v in self._voices:
                    n = v["name"].lower()
                    if any(x in n for x in ("pt", "brazil", "brasil", "portuguese", "franc", "anton")):
                        self._voice_id = v["id"]
                        print(f"[tts] Voz padrão: {v['name']}")
                        break
            except Exception as ex:
                print(f"[tts] Erro ao iniciar provedor de TTS: {ex} — desativando voz.")
                self.enabled = False
            finally:
                self._ready.set()

        threading.Thread(target=_load, daemon=True, name="tts-voices").start()

    def _get_fallback(self) -> Optional[TTSProvider]:
        if self._fallback is None:
            try:
                self._fallback = Pyttsx3Provider()
            except Exception:
                self._fallback = None
        return self._fallback

    def _start_worker(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._worker, daemon=True, name="tts-worker"
        )
        self._thread.start()

    def _worker(self):
        while self._running:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                break

            # Comandos de configuração (tuplas)
            if isinstance(item, tuple):
                cmd, val = item
                if cmd == "__rate__":
                    self._rate = val
                elif cmd == "__volume__":
                    self._volume = val
                elif cmd == "__voice__":
                    self._voice_id = val
                self._queue.task_done()
                continue

            # Mensagem
            if self.enabled and item.strip() and self._provider is not None:
                self._speak_now_sync(item)

            self._queue.task_done()

    def _speak_now_sync(self, text: str):
        """Sintetiza (com fallback) e reproduz — chamado dentro do worker."""
        provider = self._provider

        # Provedores self_playing (pyttsx3) usam Popen interruptível para
        # que stop_all() consiga cortar a fala em andamento.
        if provider is not None and provider.self_playing and \
           hasattr(provider, "speak_interruptible"):
            proc = provider.speak_interruptible(text, self._rate, self._volume, self._voice_id)
            if proc is not None:
                with self._proc_lock:
                    self._current_proc = proc
                try:
                    proc.wait(timeout=20)
                except Exception:
                    pass
                finally:
                    with self._proc_lock:
                        if self._current_proc is proc:
                            self._current_proc = None
            return

        try:
            audio = synthesize_cached(
                provider, self._provider_name, text,
                self._rate, self._volume, self._voice_id,
            )
        except Exception as ex:
            print(f"[tts] Erro no provedor '{self._provider_name}': {ex} — usando fallback local.")
            audio, provider = None, None

        if provider is not None and provider.self_playing:
            return  # o provedor já tocou o áudio sozinho (fallback sem speak_interruptible)

        if audio is None:
            # Provedor de nuvem falhou (ou lançou exceção) — fallback local
            fb = self._get_fallback()
            if fb is not None:
                fb.synthesize(text, self._rate, self._volume, None)
            return

        self._play_audio(audio)

    def _play_audio(self, audio: bytes):
        if not _PLAYBACK_AVAILABLE:
            print("[tts] sounddevice/soundfile não instalados — não é possível "
                  "reproduzir áudio de provedores em nuvem. "
                  "Execute: pip install sounddevice soundfile")
            return
        try:
            import io
            data, samplerate = sf.read(io.BytesIO(audio), dtype="float32")
            sd.play(data, samplerate)
            sd.wait()
        except Exception as ex:
            print(f"[tts] Erro ao reproduzir áudio: {ex}")

    # ── API pública ───────────────────────────────────────────────────────

    def speak(self, message: str, priority: bool = False):
        if not self.enabled or not message or not message.strip():
            return
        if priority:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        self._queue.put(message)

    def speak_now(self, message: str):
        self.speak(message, priority=True)

    def set_rate(self, rate: int):
        self._rate = max(50, min(300, rate))
        self._queue.put(("__rate__", self._rate))

    def set_volume(self, volume: float):
        self._volume = max(0.0, min(1.0, volume))
        self._queue.put(("__volume__", self._volume))

    def set_voice(self, voice_id: str):
        self._voice_id = voice_id
        self._queue.put(("__voice__", voice_id))

    def get_voices(self) -> List[Dict]:
        return self._voices

    def queue_empty(self) -> bool:
        return self._queue.empty()

    def stop_all(self):
        """
        Interrompe imediatamente qualquer fala em andamento e descarta a
        fila pendente — usado por Pausar/Parar. Não derruba a worker
        thread (diferente de stop()); o engine continua pronto para
        falar de novo em seguida.
        """
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass

        with self._proc_lock:
            proc = self._current_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

        if _PLAYBACK_AVAILABLE:
            try:
                sd.stop()  # corta qualquer áudio de nuvem em reprodução
            except Exception:
                pass

    def stop(self):
        self._running = False
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
