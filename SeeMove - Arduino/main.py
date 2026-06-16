"""
SeeMove — ponto de entrada.

Uso:
    python main.py                    # simulação + dashboard
    python main.py --hardware         # Arduino real (conecta pela UI)
    python main.py --no-web           # só terminal
    python main.py --exercise squat
    python main.py --report saida.csv
"""

import argparse
import sys
from core.session import Session
from core.pressure_platform import PressurePlatformSimulator
from core.arduino_board import ArduinoBoardHardware, list_serial_ports
from audio.tts_engine import TTSEngine
from audio.sonification import SonificationEngine
from exercises.registry import ExerciseRegistry
from reports.reporter import SessionReporter
from config.settings import Settings


def parse_args():
    p = argparse.ArgumentParser(description="SeeMove")
    p.add_argument("--hardware", action="store_true",
                   help="Habilita modo hardware Arduino (conexão feita pela UI ou automaticamente)")
    p.add_argument("--port", default="auto",
                   help="Porta serial do Arduino (ex.: COM3, COM4; padrão: auto)")
    p.add_argument("--baud", type=int, default=9600,
                   help="Baud rate da serial do Arduino (padrão: 9600)")
    p.add_argument("--list-ports", action="store_true")
    p.add_argument("--exercise", default="squat",
                   choices=["squat","balance","stand","lunge"])
    p.add_argument("--threshold", type=float, default=0.15)
    p.add_argument("--no-tts", action="store_true")
    p.add_argument("--no-sonification", action="store_true")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--report", type=str, default=None)
    return p.parse_args()


def list_arduino_ports():
    ports = list_serial_ports()
    if not ports:
        print("Nenhuma porta serial encontrada ou pyserial não está instalado.")
        print("Se necessário, execute: pip install pyserial")
    else:
        print("Portas seriais encontradas:")
        for port in ports:
            print(f"  {port}")
    sys.exit(0)


def main():
    args = parse_args()
    if args.list_ports:
        list_arduino_ports()

    settings = Settings(
        threshold=args.threshold,
        tts_enabled=not args.no_tts,
        sonification_enabled=not args.no_sonification,
    )

    print("=" * 50)
    print("  SeeMove — Monitoramento postural")
    print("=" * 50)

    # Plataforma — Arduino ou simulador
    if args.hardware:
        print(f"\n[modo] Hardware — Arduino em {args.port} @ {args.baud} bps.")
        board = ArduinoBoardHardware(port=args.port, baudrate=args.baud)
    else:
        print(f"\n[modo] Simulação ({args.exercise}).")
        board = PressurePlatformSimulator(exercise=args.exercise)

    tts = TTSEngine(enabled=settings.tts_enabled)
    sonification = SonificationEngine(enabled=settings.sonification_enabled)

    registry = ExerciseRegistry()
    exercise = registry.get(args.exercise)
    print(f"[exercício] {exercise.name}")

    reporter = SessionReporter()
    reporter.set_exercise(exercise.name)

    # Inicializa sessão (sem conectar ainda no modo hardware)
    web_push = None
    web_tts = None

    session = Session(
        board=board, tts=tts, sonification=sonification,
        exercise=exercise, settings=settings, reporter=reporter,
        web_push=None, web_tts=None,
    )

    # Dashboard web
    if not args.no_web:
        try:
            from web.server import start, push_state, push_tts, push_hw_status, inject
            # Injeta referências para as rotas da API
            inject(session, reporter, board, settings)
            # Liga callbacks de push ao servidor
            session.web_push = push_state
            session.web_tts = push_tts
            session.web_status = lambda msg: push_hw_status(msg, getattr(board, "_connected", False))
            board.on_status = session.web_status
            start(open_browser=True)
        except ImportError as e:
            print(f"[web] Dependência faltando: {e}")
            print("  Execute: pip install flask flask-socketio")

    # No modo simulação, conecta imediatamente
    # No modo hardware, a conexão é feita pelo botão na UI (ou aqui se --hardware sem web)
    if not args.hardware:
        if not board.connect():
            print("[erro] Falha ao iniciar simulador.")
            sys.exit(1)
    elif args.no_web:
        # Sem UI: conecta diretamente pelo terminal
        if not board.connect():
            print("[erro] Não foi possível conectar ao hardware.")
            sys.exit(1)
    else:
        print("[hardware] Clique em 'Conectar plataforma' no dashboard.")

    tts.speak(exercise.start_message)
    print(f"\n[sessão] Rodando. Ctrl+C para encerrar.\n")

    try:
        session.run()
    except KeyboardInterrupt:
        print("\n\n[sessão] Encerrada.")
    finally:
        board.disconnect()
        s = reporter.summary()
        print(f"\n--- Resumo ---")
        print(f"  Duração      : {s['duration_str']}")
        print(f"  Centralizado : {s['centered_pct']}%")
        print(f"  Correções    : {s['corrections']}")

        if args.report:
            reporter.save_csv(args.report)
            print(f"  Relatório    : {args.report}")

        tts.speak("Sessão encerrada.")
        print("Até logo!")


if __name__ == "__main__":
    main()
