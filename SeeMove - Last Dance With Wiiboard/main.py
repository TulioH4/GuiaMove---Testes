"""
SeeMove — ponto de entrada.

Uso:
    python main.py                    # simulação + dashboard
    python main.py --hardware         # Balance Board real (conecta pela UI)
    python main.py --no-web           # só terminal
    python main.py --exercise squat
    python main.py --report saida.csv
"""

import argparse
import sys
import time
from core.session import Session
from core.balance_board import BalanceBoardSimulator, BalanceBoardHardware
from audio.tts_engine import TTSEngine
from audio.sonification import SonificationEngine
from exercises.registry import ExerciseRegistry
from reports.reporter import SessionReporter
from config.settings import Settings


def parse_args():
    p = argparse.ArgumentParser(description="SeeMove")
    p.add_argument("--hardware", action="store_true",
                   help="Habilita modo hardware (conexão feita pela UI ou automaticamente)")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--exercise", default="squat",
                   choices=["squat","balance","stand","lunge"])
    p.add_argument("--threshold", type=float, default=0.15)
    p.add_argument("--no-tts", action="store_true")
    p.add_argument("--no-sonification", action="store_true")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--report", type=str, default=None)
    return p.parse_args()


def list_bluetooth():
    try:
        import bluetooth
        print("Buscando dispositivos Bluetooth...")
        devices = bluetooth.discover_devices(lookup_names=True, duration=8)
        for addr, name in devices:
            tag = " ← Balance Board" if ("Nintendo" in name or "RVL" in name) else ""
            print(f"  {addr}  {name}{tag}")
    except ImportError:
        print("PyBluez não instalado. Execute: pip install PyBluez")
    sys.exit(0)


def main():
    args = parse_args()
    if args.list_devices:
        list_bluetooth()

    settings = Settings(
        threshold=args.threshold,
        tts_enabled=not args.no_tts,
        sonification_enabled=not args.no_sonification,
    )

    print("=" * 50)
    print("  SeeMove — Monitoramento postural")
    print("=" * 50)

    # Board — hardware ou simulador
    if args.hardware:
        print("\n[modo] Hardware — conexão via UI ou automática.")
        board = BalanceBoardHardware()
    else:
        print(f"\n[modo] Simulação ({args.exercise}).")
        board = BalanceBoardSimulator(exercise=args.exercise)

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
            print("[erro] Não foi possível conectar ao Balance Board.")
            sys.exit(1)
    else:
        print("[hardware] Clique em 'Conectar Balance Board' no dashboard.")

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
