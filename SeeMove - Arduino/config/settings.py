"""
config/settings.py
Configurações globais do sistema SeeMove.
"""

from dataclasses import dataclass


@dataclass
class Settings:
    """Parâmetros configuráveis do sistema."""

    # Limiar de desvio do CoG para considerar postura desviada
    threshold: float = 0.15

    # Número de frames para suavização do CoG (média móvel)
    smoothing_window: int = 5

    # Intervalo mínimo entre falas TTS (segundos)
    tts_cooldown_s: float = 3.0

    # Ativa/desativa subsistemas de áudio
    tts_enabled: bool = True
    sonification_enabled: bool = True

    # Velocidade de fala TTS (palavras por minuto)
    tts_rate: int = 145

    # Taxa de amostragem dos sensores (Hz) — afeta loop do simulador
    sample_rate_hz: float = 10.0

    # Peso mínimo sobre a plataforma para iniciar análise (kg)
    min_weight_kg: float = 5.0
