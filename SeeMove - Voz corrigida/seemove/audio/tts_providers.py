"""
audio/tts_providers.py
Abstração de motores de TTS (Strategy pattern) — permite trocar entre
síntese local (pyttsx3, offline, sem custo) e vozes de nuvem mais
naturais (Azure Cognitive Services, Google Cloud TTS, ElevenLabs),
mantendo o resto do sistema (TTSEngine, fila, worker thread) inalterado.

Todos os provedores em nuvem retornam áudio como WAV/PCM em bytes, para
que a reprodução seja uniforme (sounddevice + soundfile) independente
do provedor escolhido.

Se um provedor de nuvem não puder ser inicializado (pacote não
instalado, sem chave de API, erro de rede), a fábrica cai para
Pyttsx3Provider automaticamente e loga um aviso — nunca derruba a
aplicação.
"""

import sys
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


# Script Python mínimo executado em cada subprocess de fala (pyttsx3).
# Isolado em subprocess para evitar conflitos COM/SAPI/threading no Windows.
_SPEAK_SCRIPT = """
import sys, pyttsx3
msg, rate, vol, vid = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
e = pyttsx3.init()
e.setProperty('rate', rate)
e.setProperty('volume', vol)
if vid:
    e.setProperty('voice', vid)
else:
    for v in e.getProperty('voices'):
        n = v.name.lower()
        if any(x in n for x in ('pt','brazil','brasil','portuguese')):
            e.setProperty('voice', v.id)
            break
e.say(msg)
e.runAndWait()
"""

_WIN_FLAGS = 0x08000000 if sys.platform == "win32" else 0


class TTSProvider(ABC):
    """Interface comum a todos os motores de síntese de voz."""

    #: True se o provedor reproduz o áudio sozinho (ex.: subprocess pyttsx3);
    #: False se `synthesize()` devolve bytes de áudio para o TTSEngine tocar.
    self_playing: bool = False

    @abstractmethod
    def synthesize(self, text: str, rate: int, volume: float,
                    voice_id: Optional[str]) -> Optional[bytes]:
        """
        Sintetiza `text` e devolve áudio WAV em bytes, ou None em caso de
        falha. Provedores `self_playing=True` tocam o áudio diretamente e
        também devolvem None (não há bytes para o TTSEngine reproduzir).
        """
        raise NotImplementedError

    def list_voices(self) -> List[Dict]:
        return []


class Pyttsx3Provider(TTSProvider):
    """Motor local via SAPI (Windows) — subprocess isolado por fala. Padrão offline."""

    self_playing = True

    def __init__(self):
        try:
            import pyttsx3  # noqa
        except ImportError:
            raise RuntimeError("pyttsx3 não instalado: pip install pyttsx3")

        self._voices: List[Dict] = []
        self._load_voices()

    def _load_voices(self):
        try:
            import pyttsx3
            e = pyttsx3.init()
            self._voices = [{"id": v.id, "name": v.name} for v in e.getProperty("voices")]
            try:
                e.stop()
            except Exception:
                pass
        except Exception as ex:
            print(f"[tts] Erro ao listar vozes pyttsx3: {ex}")

    def synthesize(self, text: str, rate: int, volume: float,
                    voice_id: Optional[str]) -> Optional[bytes]:
        proc = self.speak_interruptible(text, rate, volume, voice_id)
        if proc is None:
            return None
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            print(f"[tts] Timeout ao falar: {text[:40]}")
            proc.kill()
        return None

    def speak_interruptible(self, text: str, rate: int, volume: float,
                              voice_id: Optional[str]) -> Optional[subprocess.Popen]:
        """
        Mesma síntese de `synthesize()`, mas via Popen em vez de
        subprocess.run — devolve o processo para que o chamador (TTSEngine)
        possa guardá-lo e chamar `.terminate()` para interromper a fala em
        andamento (usado por Pausar/Parar).
        """
        try:
            return subprocess.Popen(
                [sys.executable, "-c", _SPEAK_SCRIPT,
                 text, str(rate), str(volume), voice_id or ""],
                creationflags=_WIN_FLAGS,
            )
        except Exception as ex:
            print(f"[tts] Erro subprocess pyttsx3: {ex}")
            return None

    def list_voices(self) -> List[Dict]:
        return self._voices


class AzureTTSProvider(TTSProvider):
    """Vozes neurais Azure Cognitive Services (ex.: pt-BR-FranciscaNeural)."""

    self_playing = False
    _CURATED_VOICES = [
        {"id": "pt-BR-FranciscaNeural", "name": "Francisca (pt-BR, neural)"},
        {"id": "pt-BR-AntonioNeural",   "name": "Antônio (pt-BR, neural)"},
    ]

    def __init__(self, api_key: str, region: str, default_voice: Optional[str] = None):
        try:
            import azure.cognitiveservices.speech as speechsdk  # noqa
        except ImportError:
            raise RuntimeError(
                "azure-cognitiveservices-speech não instalado: "
                "pip install azure-cognitiveservices-speech"
            )
        if not api_key or not region:
            raise RuntimeError("Azure TTS requer api_key e api_region.")

        self._speechsdk = speechsdk
        self._api_key   = api_key
        self._region    = region
        self._default_voice = default_voice or self._CURATED_VOICES[0]["id"]

    def synthesize(self, text: str, rate: int, volume: float,
                    voice_id: Optional[str]) -> Optional[bytes]:
        speechsdk = self._speechsdk
        voice = voice_id or self._default_voice
        # rate (~50-300 wpm) -> taxa relativa de prosódia SSML
        rate_pct = int(((rate - 145) / 145) * 100)
        vol_pct  = int(max(0.0, min(1.0, volume)) * 100)

        ssml = (
            f'<speak version="1.0" xml:lang="pt-BR">'
            f'<voice name="{voice}">'
            f'<prosody rate="{rate_pct:+d}%" volume="{vol_pct}%">{_xml_escape(text)}</prosody>'
            f'</voice></speak>'
        )

        try:
            speech_config = speechsdk.SpeechConfig(subscription=self._api_key, region=self._region)
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
            )
            synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)
            result = synthesizer.speak_ssml_async(ssml).get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return result.audio_data
            print(f"[tts] Azure falhou: {result.reason}")
            return None
        except Exception as ex:
            print(f"[tts] Erro Azure TTS: {ex}")
            return None

    def list_voices(self) -> List[Dict]:
        return list(self._CURATED_VOICES)


class GoogleCloudTTSProvider(TTSProvider):
    """Vozes Google Cloud Text-to-Speech (ex.: pt-BR-Wavenet-A)."""

    self_playing = False
    _CURATED_VOICES = [
        {"id": "pt-BR-Wavenet-A", "name": "Wavenet A (pt-BR)"},
        {"id": "pt-BR-Wavenet-B", "name": "Wavenet B (pt-BR)"},
    ]

    def __init__(self, api_key: Optional[str] = None, default_voice: Optional[str] = None):
        try:
            from google.cloud import texttospeech  # noqa
        except ImportError:
            raise RuntimeError(
                "google-cloud-texttospeech não instalado: "
                "pip install google-cloud-texttospeech"
            )
        # A lib do Google usa GOOGLE_APPLICATION_CREDENTIALS (arquivo de conta de
        # serviço) por padrão; api_key aqui é aceito só para uniformizar a fábrica.
        self._texttospeech = texttospeech
        self._client = texttospeech.TextToSpeechClient()
        self._default_voice = default_voice or self._CURATED_VOICES[0]["id"]

    def synthesize(self, text: str, rate: int, volume: float,
                    voice_id: Optional[str]) -> Optional[bytes]:
        tts = self._texttospeech
        voice_name = voice_id or self._default_voice
        speaking_rate = max(0.25, min(4.0, rate / 145.0))

        try:
            synthesis_input = tts.SynthesisInput(text=text)
            voice = tts.VoiceSelectionParams(language_code="pt-BR", name=voice_name)
            audio_config = tts.AudioConfig(
                audio_encoding=tts.AudioEncoding.LINEAR16,
                speaking_rate=speaking_rate,
                volume_gain_db=max(-96.0, min(16.0, (volume - 1.0) * 16.0)),
            )
            response = self._client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            return response.audio_content
        except Exception as ex:
            print(f"[tts] Erro Google Cloud TTS: {ex}")
            return None

    def list_voices(self) -> List[Dict]:
        return list(self._CURATED_VOICES)


class ElevenLabsTTSProvider(TTSProvider):
    """Vozes ElevenLabs via REST API (modelo multilíngue, suporta pt-BR)."""

    self_playing = False
    _CURATED_VOICES = [
        {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel (multilíngue)"},
    ]
    _API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    def __init__(self, api_key: str, default_voice: Optional[str] = None):
        try:
            import requests  # noqa
        except ImportError:
            raise RuntimeError("requests não instalado: pip install requests")
        if not api_key:
            raise RuntimeError("ElevenLabs TTS requer api_key.")

        self._requests = requests
        self._api_key  = api_key
        self._default_voice = default_voice or self._CURATED_VOICES[0]["id"]

    def synthesize(self, text: str, rate: int, volume: float,
                    voice_id: Optional[str]) -> Optional[bytes]:
        voice = voice_id or self._default_voice
        url = self._API_URL.format(voice_id=voice)
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "output_format": "pcm_16000",
        }
        try:
            resp = self._requests.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.content
        except Exception as ex:
            print(f"[tts] Erro ElevenLabs TTS: {ex}")
            return None

    def list_voices(self) -> List[Dict]:
        return list(self._CURATED_VOICES)


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


# Cache simples em memória para frases repetidas (confirmações, mensagens
# fixas de início/fim) — evita custo/latência de rechamar a API de nuvem.
_SYNTH_CACHE: Dict[tuple, Optional[bytes]] = {}
_SYNTH_CACHE_MAX = 200


def synthesize_cached(provider: TTSProvider, provider_name: str, text: str,
                       rate: int, volume: float, voice_id: Optional[str]) -> Optional[bytes]:
    if provider.self_playing:
        return provider.synthesize(text, rate, volume, voice_id)

    key = (provider_name, voice_id, rate, round(volume, 2), text)
    if key in _SYNTH_CACHE:
        return _SYNTH_CACHE[key]

    audio = provider.synthesize(text, rate, volume, voice_id)
    if audio is not None:
        if len(_SYNTH_CACHE) >= _SYNTH_CACHE_MAX:
            _SYNTH_CACHE.pop(next(iter(_SYNTH_CACHE)))
        _SYNTH_CACHE[key] = audio
    return audio


def create_tts_provider(voice_settings) -> TTSProvider:
    """
    Fábrica: instancia o provedor configurado em `voice_settings.provider`.
    Em qualquer falha (pacote ausente, sem chave, erro de inicialização),
    cai para Pyttsx3Provider e loga um aviso — nunca propaga a exceção.
    """
    name = (voice_settings.provider or "pyttsx3").lower()

    try:
        if name == "azure":
            return AzureTTSProvider(
                api_key=voice_settings.api_key,
                region=voice_settings.api_region,
                default_voice=voice_settings.cloud_voice_id,
            )
        if name == "google":
            return GoogleCloudTTSProvider(
                api_key=voice_settings.api_key,
                default_voice=voice_settings.cloud_voice_id,
            )
        if name == "elevenlabs":
            return ElevenLabsTTSProvider(
                api_key=voice_settings.api_key,
                default_voice=voice_settings.cloud_voice_id,
            )
        if name != "pyttsx3":
            print(f"[tts] Provedor desconhecido '{name}' — usando pyttsx3.")
    except Exception as ex:
        print(f"[tts] Não foi possível iniciar provedor '{name}' ({ex}) — usando pyttsx3.")

    return Pyttsx3Provider()
