"""
GuiaMove — ponto de entrada (versão Kinect + MediaPipe).

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

from core.kinect_tracker import KinectTracker
from core.session import Session
from audio.tts_engine import TTSEngine
from audio.sonification import SonificationEngine
from exercises.registry import ExerciseRegistry
from reports.reporter import SessionReporter
from config.settings import Settings, VoiceSettings, KinectSettings


def parse_args():
    p = argparse.ArgumentParser(
        description="GuiaMove — monitoramento postural via Kinect + MediaPipe"
    )
    p.add_argument("--camera",     type=int,   default=0,
                   help="Índice da câmera/Kinect (padrão: 0)")
    p.add_argument("--no-camera",  action="store_true",
                   help="Roda sem câmera — dashboard, voz e configuração funcionam "
                        "normalmente, só não há captura/análise de pose")
    p.add_argument("--exercise",   default="squat",
                   choices=["squat", "stand", "balance"],
                   help="Exercício inicial (padrão: squat)")
    p.add_argument("--complexity", type=int,   default=1, choices=[0, 1, 2],
                   help="Complexidade do modelo MediaPipe (0/1/2). ATENÇÃO: só tem efeito "
                        "na API antiga (mediapipe < 0.10); com a API Tasks (a instalada "
                        "hoje) o modelo 'lite' é fixo e este valor é ignorado")
    p.add_argument("--rate",       type=float, default=30.0,
                   help="Taxa máxima de captura/boneco em FPS (padrão: 30; a análise de "
                        "exercício continua limitada a ~10 Hz na Session)")
    p.add_argument("--no-depth",   action="store_true",
                   help="Desativa captura de profundidade do Kinect")
    p.add_argument("--kinect-sdk", action="store_true",
                   help="Captura o RGB do Kinect v1 pela API oficial do SDK (NUI) "
                        "em vez de cv2.VideoCapture — necessário quando o driver "
                        "'Kinect for Windows' não expõe a câmera como webcam "
                        "genérica. Requer native/kinect_bridge/kinect_color_bridge.exe "
                        "compilado (ver native/kinect_bridge/build.bat)")
    p.add_argument("--kinect-sdk-depth", action="store_true",
                   help="Experimental: usa a profundidade real do infravermelho "
                        "(via --kinect-sdk) pra enriquecer quadril/joelho/tornozelo. "
                        "Requer --kinect-sdk. Testado como instável a >3m de "
                        "distância — só ative se o Kinect estiver mais perto (~1,5-2,5m)")
    p.add_argument("--no-browser-open", action="store_true",
                   help="Não abre o navegador sozinho ao iniciar (uso interno/testes — "
                        "evita abrir uma aba extra fora de controle a cada execução)")
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
    # Com a saída redirecionada (arquivo/pipe) o Windows usa a codepage do sistema (cp1252), que não tem
    # "─", "✓" nem "✗" dos logs — um print desses derrubava a sessão. Troca o caractere por "?" em vez de quebrar.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = parse_args()

    print("=" * 55)
    print("  GuiaMove — Monitoramento postural")
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
        use_sdk_bridge=args.kinect_sdk,
        use_sdk_depth=args.kinect_sdk_depth,
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
    print(f"[depth]     {'ativo (Kinect IR)' if tracker.use_depth else 'desativado'}")

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
    # A câmera que para de mandar imagem (ou não abre) precisa ser FALADA, não só aparecer no painel.
    tracker.on_camera_problem = session.camera_problem
    tracker.on_camera_ok      = session.camera_ok

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
            start(port=args.port, open_browser=not args.no_browser_open)
        except ImportError as e:
            print(f"[web] Dependência faltando: {e}")
            print("  Execute: pip install flask flask-socketio")
        except RuntimeError as e:
            # Porta já em uso (outra instância aberta) — ver web.server.start()
            print(f"[erro] {e}")
            sys.exit(1)

    # ── Inicia tracker ────────────────────────────────────────────────────
    print("\n[sessão] Iniciando. Ctrl+C para encerrar.\n")
    if args.no_camera:
        print("[câmera] Desativada (--no-camera) — dashboard/voz/configuração "
              "seguem funcionando, sem captura nem análise de pose.")
    elif not tracker.connect():
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
        # Encerra as threads de análise/broadcast antes de desconectar a
        # câmera — session.stop() existia mas nunca era chamado (as
        # threads são daemon e morriam sozinhas ao sair do processo, sem
        # um join real). Sem impacto visível até aqui, mas é o encerramento
        # correto: dá pras filas internas esvaziarem e às threads saírem
        # do loop de forma limpa em vez de serem simplesmente cortadas.
        session.stop()
        tracker.disconnect()
        # session.audio (não tts.speak() direto), pra respeitar o mesmo
        # roteamento do resto do app: no modo dashboard, sai pelo navegador
        # (Web Speech API) via remote_push; tts.speak() direto sempre saía
        # pela voz local do servidor, inconsistente com tudo mais.
        session.audio.speak_now("Sessão encerrada.")

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
