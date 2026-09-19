"""
config/settings.py
Configurações globais do SeeMove — versão Kinect+MediaPipe.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VoiceSettings:
    """Configurações de voz ajustáveis pelo usuário via dashboard."""
    enabled: bool = True
    rate: int = 145           # palavras por minuto (50–300)
    volume: float = 1.0       # 0.0 – 1.0
    voice_id: Optional[str] = None   # None = auto-seleciona pt-BR
    cooldown_ok_s: float = 4.0       # silêncio entre falas quando OK
    cooldown_warn_s: float = 3.0     # entre correções
    cooldown_error_s: float = 1.5    # urgente
    confirm_on_correction: bool = True  # fala "muito bem" ao corrigir

    # Provedor de TTS: "pyttsx3" (local, padrão) | "azure" | "google" | "elevenlabs"
    provider: str = "pyttsx3"
    # Chave de API do provedor em nuvem — nunca hard-code; use variável de ambiente
    # SEEMOVE_TTS_API_KEY se não for passada explicitamente.
    api_key: Optional[str] = field(
        default_factory=lambda: os.environ.get("SEEMOVE_TTS_API_KEY")
    )
    api_region: Optional[str] = field(
        default_factory=lambda: os.environ.get("SEEMOVE_TTS_API_REGION")
    )  # usado pelo Azure (ex.: "brazilsouth")
    cloud_voice_id: Optional[str] = None  # nome/ID da voz no provedor de nuvem


@dataclass
class KinectSettings:
    camera_index: int = 0
    width: int = 640
    height: int = 480
    rate_hz: float = 10.0
    model_complexity: int = 1      # 0=leve, 1=padrão, 2=pesado
    min_detection_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    use_depth: bool = True         # usa profundidade quando disponível

    # Suavização de landmarks (One Euro Filter) — reduz jitter/falsos positivos
    smoothing_enabled: bool = True
    smoothing_min_cutoff: float = 1.0
    smoothing_beta: float = 0.3


@dataclass
class Settings:
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    kinect: KinectSettings = field(default_factory=KinectSettings)
    sonification_enabled: bool = True

    def apply_voice(self, tts_engine) -> None:
        """Aplica as configurações de voz ao TTSEngine em tempo real."""
        v = self.voice
        tts_engine.enabled = v.enabled
        tts_engine.set_rate(v.rate)
        tts_engine.set_volume(v.volume)
