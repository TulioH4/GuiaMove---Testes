"""
SeeMove — ponto de entrada (versão Kinect + MediaPipe).

Sem sensores de pressão, sem Wii Balance Board, sem Arduino.

Uso:
    python main.py                        # câmera padrão (índice 0)
    python main.py --camera 1             # Kinect em outra porta
    python main.py --exercise squat       # squat | stand | balance
    python main.py --no-depth             # desativa profundidade (só RGB)
    python main.py --no-web               # só terminal, sem dashboard
    python main.py --no-tts               # sem voz
    python main.py --no-sonification      # sem bipes
    python main.py --complexity 2         # modelo mais pesado e preciso
    python main.py --report relatorio.csv # salva CSV ao encerrar
"""

import argparse
import sys
import time

from core.kinect_tracker import KinectTracker
from core.session import Session
from audio.tts_engine import TTSEngine
from audio.sonification import SonificationEngine
from exercises.registry import ExerciseRegistry
from reports.reporter import SessionReporter
from config.settings import Settings, VoiceSettings, KinectSettings


def parse_args():
    p = argparse.ArgumentParser(
        description="SeeMove — monitoramento postural via Kinect + MediaPipe"
    )
    p.add_argument("--camera",     type=int,   default=0,
                   help="Índice da câmera/Kinect (padrão: 0)")
    p.add_argument("--exercise",   default="squat",
                   choices=["squat", "stand", "balance"],
                   help="Exercício inicial (padrão: squat)")
    p.add_argument("--complexity", type=int,   default=1, choices=[0, 1, 2],
                   help="Complexidade do modelo MediaPipe: 0=rápido, 1=padrão, 2=preciso")
    p.add_argument("--rate",       type=float, default=10.0,
                   help="Taxa de análise em FPS (padrão: 10)")
    p.add_argument("--no-depth",   action="store_true",
                   help="Desativa captura de profundidade do Kinect")
    p.add_argument("--no-tts",     action="store_true",
                   help="Desativa síntese de voz")
    p.add_argument("--no-sonification", action="store_true",
                   help="Desativa bipes de feedback")
    p.add_argument("--no-web",     action="store_true",
                   help="Sem dashboard web (modo terminal)")
    p.add_argument("--port",       type=int,   default=5000,
                   help="Porta do servidor web (padrão: 5000)")
    p.add_argument("--tts-rate",   type=int,   default=145,
                   help="Velocidade da voz em palavras/minuto (padrão: 145)")
    p.add_argument("--tts-volume", type=float, default=1.0,
                   help="Volume da voz 0.0–1.0 (padrão: 1.0)")
    p.add_argument("--tts-provider", default="pyttsx3",
                   choices=["pyttsx3", "azure", "google", "elevenlabs"],
                   help="Motor de síntese de voz (padrão: pyttsx3, offline)")
    p.add_argument("--tts-api-key", type=str, default=None,
                   help="Chave de API do provedor de voz em nuvem "
                        "(ou defina a variável de ambiente SEEMOVE_TTS_API_KEY)")
    p.add_argument("--tts-api-region", type=str, default=None,
                   help="Região do provedor (necessário para Azure, ex.: brazilsouth)")
    p.add_argument("--no-smoothing", action="store_true",
                   help="Desativa suavização (One Euro Filter) dos landmarks")
    p.add_argument("--report",     type=str,   default=None,
                   metavar="ARQUIVO",
                   help="Salva relatório CSV ao encerrar")
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 55)
    print("  SeeMove — Monitoramento postural")
    print("  Kinect + MediaPipe Pose")
    print("=" * 55)

    # ── Configurações ────────────────────────────────────────────────────
    settings = Settings(
        voice=VoiceSettings(
            enabled=not args.no_tts,
            rate=args.tts_rate,
            volume=args.tts_volume,
            provider=args.tts_provider,
            api_key=args.tts_api_key,       # None mantém o padrão de env var
            api_region=args.tts_api_region,
        ),
        kinect=KinectSettings(
            camera_index=args.camera,
            rate_hz=args.rate,
            model_complexity=args.complexity,
            use_depth=not args.no_depth,
            smoothing_enabled=not args.no_smoothing,
        ),
        sonification_enabled=not args.no_sonification,
    )
    # api_key/region de CLI têm prioridade só se informados — senão o
    # default_factory da dataclass já pega das variáveis de ambiente.
    if args.tts_api_key is None:
        settings.voice.api_key = VoiceSettings().api_key
    if args.tts_api_region is None:
        settings.voice.api_region = VoiceSettings().api_region

    # ── Kinect Tracker ───────────────────────────────────────────────────
    tracker = KinectTracker(
        camera_index=settings.kinect.camera_index,
        rate_hz=settings.kinect.rate_hz,
        model_complexity=settings.kinect.model_complexity,
        min_detection_confidence=settings.kinect.min_detection_confidence,
        min_tracking_confidence=settings.kinect.min_tracking_confidence,
        use_depth=settings.kinect.use_depth,
        smoothing_enabled=settings.kinect.smoothing_enabled,
        smoothing_min_cutoff=settings.kinect.smoothing_min_cutoff,
        smoothing_beta=settings.kinect.smoothing_beta,
    )

    # ── Áudio ────────────────────────────────────────────────────────────
    tts = TTSEngine(
        enabled=settings.voice.enabled,
        rate=settings.voice.rate,
        volume=settings.voice.volume,
        voice_settings=settings.voice,
    )
    sonification = SonificationEngine(enabled=settings.sonification_enabled)

    # ── Exercício ────────────────────────────────────────────────────────
    registry = ExerciseRegistry()
    exercise = registry.get(args.exercise)
    print(f"\n[exercício] {exercise.name}")
    print(f"[câmera]    índice {args.camera}")
    print(f"[modelo]    complexidade {args.complexity}")
    print(f"[depth]     {'ativo (Kinect IR)' if settings.kinect.use_depth else 'desativado'}")

    # ── Reporter ─────────────────────────────────────────────────────────
    reporter = SessionReporter()
    reporter.set_exercise(exercise.name)

    # ── Sessão ───────────────────────────────────────────────────────────
    session = Session(
        tracker=tracker,
        tts=tts,
        sonification=sonification,
        exercise=exercise,
        settings=settings,
        reporter=reporter,
    )

    # ── Dashboard web ────────────────────────────────────────────────────
    if not args.no_web:
        try:
            from web.server import start, inject_refs
            inject_refs(
                session=session,
                tracker=tracker,
                reporter=reporter,
                settings=settings,
                tts=tts,
                registry=registry,
            )
            # Liga callbacks do web push à sessão
            from web.server import push_frame, push_tts_log, push_audio_event
            session.web_push = push_frame
            # Com o dashboard ativo, o áudio passa a ser reproduzido no
            # browser (Web Speech API + Web Audio API) — zero latência de
            # backend e cancelamento instantâneo. O backend não toca mais
            # áudio local nesse modo; --no-web continua usando TTSEngine/
            # SonificationEngine locais normalmente.
            session.audio.remote_push = push_audio_event
            # Toda fala que passa por AudioCoordinator.emit()/speak_now()
            # (correções, confirmações, briefing, calibração de
            # enquadramento) alimenta o painel "Log de feedback de áudio"
            # automaticamente a partir daqui — não precisa de fiação manual
            # espalhada pela Session.
            session.audio.log_push = push_tts_log
            # Liga status do tracker ao dashboard
            tracker.on_status = lambda msg, conn: \
                __import__("web.server", fromlist=["push_tracker_status"]) \
                .push_tracker_status(msg, conn)
            start(port=args.port, open_browser=True)
        except ImportError as e:
            print(f"[web] Dependência faltando: {e}")
            print("  Execute: pip install flask flask-socketio")

    # ── Inicia tracker ────────────────────────────────────────────────────
    print(f"\n[sessão] Iniciando. Ctrl+C para encerrar.\n")
    if not tracker.connect():
        print("[erro] Não foi possível abrir a câmera.")
        sys.exit(1)

    # Não fala o start_message automaticamente — o usuário escolhe o
    # exercício e inicia pelo dashboard. A fala automática atrapalhava
    # usuários que ainda estavam se posicionando.

    try:
        session.run()
    except KeyboardInterrupt:
        print("\n\n[sessão] Encerrada pelo usuário.")
    finally:
        tracker.disconnect()
        tts.speak("Sessão encerrada.")

        s = reporter.summary()
        print(f"\n{'─'*40}")
        print(f"  Duração            : {s['duration_str']}")
        print(f"  Postura correta    : {s['ok_pct']}% do tempo")
        print(f"  Correções emitidas : {s['corrections']}")
        print(f"  Confiança média    : {s['mean_confidence']}%")
        print(f"{'─'*40}")

        if args.report:
            reporter.save_csv(args.report)
            print(f"  Relatório CSV      : {args.report}")

        print("\nAté logo!")


if __name__ == "__main__":
    main()
