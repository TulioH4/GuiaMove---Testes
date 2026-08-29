"""
web/server.py
Servidor Flask + Socket.IO do SeeMove — versão Kinect + MediaPipe.

Rotas HTTP:
  GET  /                          Dashboard principal (boneco 3D embutido)
  GET  /api/state                 Estado completo da sessão (JSON)

  POST /api/tracker/connect       Inicia captura (câmera / Kinect)
  POST /api/tracker/disconnect    Para captura
  GET  /api/tracker/status        Status atual do tracker
  POST /api/tracker/reset_pose    Reseta a suavização + o boneco 3D dos clientes

  GET  /api/exercises             Lista exercícios disponíveis
  POST /api/exercise/<key>        Troca exercício ativo
  POST /api/exercise/<key>/start  Inicia (gatilho estrito — sai de STOPPED)
  POST /api/exercise/pause        Pausa análise/áudio (mantém o exercício)
  POST /api/exercise/resume       Retoma a partir de um ciclo limpo
  POST /api/exercise/stop         Para e volta ao estado mudo (STOPPED)

  POST /api/setup/start           Inicia o Modo Configuração (calibração de enquadramento)

  GET  /api/voice/settings        Configurações atuais de voz
  POST /api/voice/settings        Atualiza configurações de voz
  GET  /api/voice/voices          Lista vozes disponíveis no sistema

  GET  /api/report/html           Relatório HTML (abre no navegador)
  GET  /api/report/csv            Download CSV
  GET  /api/report/json           JSON completo

Eventos Socket.IO emitidos:
  frame           — dados do frame analisado (métricas + imagem)
  tts_log         — entrada de log de voz (texto, para o log visual)
  audio_speak     — {text, rate, volume, cancel} — o browser fala via Web Speech API
  audio_cue       — {severity, pan} — bipe curto via Web Audio API
  audio_ambient   — {x, y} — pulso suave de fundo via Web Audio API
  audio_stop      — cancela speechSynthesis + osciladores ativos no browser
  audio_chime     — {pattern} — sinal sonoro curto (ex.: calibração concluída)
  tracker_status  — mudança de status do tracker (conectado/desconectado)
  exercise_changed — confirmação de troca de exercício
  pose_reset      — pede para o browser zerar a pose do boneco 3D na hora
"""

import io
import secrets
import threading
import time
import webbrowser

from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO

app = Flask(__name__, template_folder="templates", static_folder="static")
# Chave aleatória por processo — nada aqui usa sessão/cookie assinado hoje,
# mas uma chave hardcoded e versionada no repositório é um hábito ruim de
# carregar adiante caso isso mude no futuro.
app.config["SECRET_KEY"] = secrets.token_hex(32)
# Sem cors_allowed_origins="*": o dashboard só conecta ao Socket.IO a
# partir da própria origem que o serviu (127.0.0.1:<porta>) — não há
# nenhum caso de uso legítimo de conexão cross-origin aqui, e "*" aceitava
# WebSocket de qualquer site caso o servidor um dia deixe de ser
# localhost-only.
socketio = SocketIO(app, async_mode="threading")

# ── Referências injetadas por main.py ────────────────────────────────────────
_session  = None
_tracker  = None
_reporter = None
_settings = None
_tts      = None
_registry = None

# ── Estado compartilhado ─────────────────────────────────────────────────────
_state = {
    # Tracker
    "tracker_connected": False,
    "tracker_status":    "Aguardando conexão",
    "tracker_source":    "Kinect",

    # Pose
    "detected":          False,
    "confidence":        0.0,
    "frame_count":       0,
    "image_b64":         None,
    "raw_landmarks":     [],

    # Métricas biomecânicas
    "shoulder_tilt":     0.0,
    "hip_tilt":          0.0,
    "trunk_lean_x":      0.0,
    "knee_valgus_l":     0.0,
    "knee_valgus_r":     0.0,
    "knee_angle_l":      180.0,
    "knee_angle_r":      180.0,
    "torso_vertical":    0.0,
    "depth_l_knee":      0.0,
    "depth_r_knee":      0.0,

    # Feedback atual
    "feedback":          "Aguardando...",
    "severity":          "ok",
    "session_state":     "stopped",   # stopped | paused | running (ver _session_state_for_ui)

    # Sessão
    "exercise":          "squat",
    "duration_str":      "00:00",
    "duration_s":        0,
    "ok_pct":            0.0,
    "corrections":       0,
    "mean_confidence":   0.0,

    # Repetições (exercícios de repetição, ex. agachamento) — None quando
    # o exercício ativo não conta repetição (postura estática, equilíbrio)
    "repetitions":       0,
    "rejected_reps":     0,
    "last_rep_cadence":  None,

    # TTS log
    "tts_log":           [],

    # Voz
    "voice_enabled":     True,
    "voice_rate":        145,
    "voice_volume":      1.0,
}
_lock = threading.Lock()


def inject_refs(session, tracker, reporter, settings, tts, registry):
    """Chamado por main.py para registrar os objetos vivos."""
    global _session, _tracker, _reporter, _settings, _tts, _registry
    _session  = session
    _tracker  = tracker
    _reporter = reporter
    _settings = settings
    _tts      = tts
    _registry = registry

    with _lock:
        _state["voice_enabled"] = settings.voice.enabled
        _state["voice_rate"]    = settings.voice.rate
        _state["voice_volume"]  = settings.voice.volume
        _state["exercise"]      = session.exercise.__class__.__name__ \
                                    .lower().replace("exercise", "")


# ── Callbacks chamados pela Session / Tracker ─────────────────────────────────

def _session_state_for_ui() -> str:
    """
    Colapsa o FeedbackState interno da Session (STOPPED/PAUSED/BRIEFING/
    IDLE/INSTRUCTING/WAITING/CONFIRMING/REINFORCING) nos 3 estados que o
    dashboard precisa distinguir para os botões Iniciar/Pausar/Parar.
    """
    if _session is None:
        return "stopped"
    with _session._lock:
        name = _session._state.name
    if name == "STOPPED":
        return "stopped"
    if name == "PAUSED":
        return "paused"
    if name == "SETUP":
        return "setup"
    return "running"


def push_frame(frame, result, summary):
    """
    Chamado pela Session a cada frame analisado.
    frame   : SkeletonFrame
    result  : FeedbackResult | None
    summary : dict | None
    """
    m = frame.metrics

    with _lock:
        _state["session_state"]  = _session_state_for_ui()
        _state["detected"]       = frame.detected
        _state["confidence"]     = m.confidence
        _state["frame_count"]   += 1
        _state["image_b64"]      = frame.image_b64
        _state["raw_landmarks"]  = frame.raw_landmarks

        _state["shoulder_tilt"]  = m.shoulder_tilt
        _state["hip_tilt"]       = m.hip_tilt
        _state["trunk_lean_x"]   = m.trunk_lean_x
        _state["knee_valgus_l"]  = m.knee_valgus_l
        _state["knee_valgus_r"]  = m.knee_valgus_r
        _state["knee_angle_l"]   = m.knee_angle_l
        _state["knee_angle_r"]   = m.knee_angle_r
        _state["torso_vertical"] = m.torso_vertical
        _state["depth_l_knee"]   = m.depth_l_knee
        _state["depth_r_knee"]   = m.depth_r_knee

        if result is not None:
            _state["feedback"] = result.message
            _state["severity"] = result.severity.value

        # Repetições — atributos públicos do Exercise ativo (nem todo
        # exercício tem; getattr com default 0/None cobre os que não têm).
        if _session is not None:
            ex = _session.exercise
            _state["repetitions"]      = getattr(ex, "repetitions", 0)
            _state["rejected_reps"]    = getattr(ex, "rejected_reps", 0)
            _state["last_rep_cadence"] = getattr(ex, "last_rep_cadence", None)

        if summary is not None:
            _state["duration_str"]    = summary.get("duration_str", "00:00")
            _state["duration_s"]      = summary.get("duration_s", 0)
            _state["ok_pct"]          = summary.get("ok_pct", 0.0)
            _state["corrections"]     = summary.get("corrections", 0)
            _state["mean_confidence"] = summary.get("mean_confidence", 0.0)

        snap = dict(_state)

    # Não serializa a imagem no evento de frame principal — envia separado
    # para não sobrecarregar clientes que não exibem vídeo
    snap_no_img = {k: v for k, v in snap.items() if k != "image_b64"}
    socketio.emit("frame", snap_no_img)

    # Imagem em evento separado (pode ser ignorado por clientes leves)
    if frame.image_b64:
        socketio.emit("frame_image", {"img": frame.image_b64})


def push_tts_log(message: str, severity: str = "ok"):
    """Chamado pela Session ao emitir feedback de voz."""
    with _lock:
        entry = {
            "time": time.strftime("%M:%S"),
            "msg":  message,
            "sev":  severity,
        }
        _state["tts_log"] = ([entry] + _state["tts_log"])[:40]
    socketio.emit("tts_log", entry)


def push_audio_event(event: str, payload: dict):
    """
    Chamado pelo AudioCoordinator (sink remoto) para entregar um evento de
    áudio ao browser — este processo Python nunca toca áudio nesse modo.
    """
    socketio.emit(event, payload)


def push_tracker_status(msg: str, connected: bool):
    """Chamado pelo KinectTracker ao mudar de estado."""
    with _lock:
        _state["tracker_connected"] = connected
        _state["tracker_status"]    = msg
    socketio.emit("tracker_status", {"msg": msg, "connected": connected})


# ── Rotas principais ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    with _lock:
        s = dict(_state)
        s.pop("image_b64", None)   # não inclui imagem no estado geral
    return jsonify(s)


# ── Tracker ───────────────────────────────────────────────────────────────────

@app.route("/api/tracker/connect", methods=["POST"])
def tracker_connect():
    if _tracker is None:
        return jsonify({"error": "sessão não inicializada"}), 500

    data         = request.get_json(silent=True) or {}
    camera_index = int(data.get("camera_index", 0))
    use_depth    = bool(data.get("use_depth", True))

    # Atualiza parâmetros se fornecidos
    _tracker.camera_index = camera_index
    _tracker.use_depth    = use_depth

    def _do():
        push_tracker_status("Abrindo câmera...", False)
        ok = _tracker.connect()
        if not ok:
            push_tracker_status(
                "Câmera não encontrada. Verifique o índice e a conexão USB.", False
            )

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/tracker/disconnect", methods=["POST"])
def tracker_disconnect():
    if _tracker:
        _tracker.disconnect()
        push_tracker_status("Câmera desconectada.", False)
    return jsonify({"ok": True})


@app.route("/api/tracker/status")
def tracker_status():
    with _lock:
        return jsonify({
            "connected": _state["tracker_connected"],
            "status":    _state["tracker_status"],
            "source":    _state["tracker_source"],
        })


@app.route("/api/tracker/reset_pose", methods=["POST"])
def reset_pose():
    """
    Descarta o estado acumulado do filtro de suavização (útil quando a
    leitura inicial saiu ruim e o esqueleto/boneco 3D ficou "preso" numa
    pose errada) e avisa todos os clientes conectados para zerarem a pose
    do boneco 3D imediatamente, em vez de esperar o Slerp corrigir sozinho.
    """
    if _tracker is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    _tracker.reset_smoothing()
    socketio.emit("pose_reset", {})
    return jsonify({"ok": True})


# ── Exercícios ────────────────────────────────────────────────────────────────

@app.route("/api/exercises")
def list_exercises():
    if _registry is None:
        return jsonify({})
    return jsonify(_registry.list_all())


@app.route("/api/exercise/<key>", methods=["POST"])
def set_exercise(key):
    if _registry is None or _session is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    try:
        ex = _registry.get(key)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    _session.set_exercise(ex)
    _reporter.set_exercise(ex.name)

    with _lock:
        _state["exercise"] = key

    # Anuncia o exercício apenas se o usuário clicar em "Iniciar exercício"
    # via botão dedicado — não fala automaticamente ao trocar de aba
    socketio.emit("exercise_changed", {"key": key, "name": ex.name})
    return jsonify({"ok": True, "name": ex.name})


@app.route("/api/exercise/<key>/start", methods=["POST"])
def start_exercise(key):
    """
    Inicia o exercício com narração de voz.
    Chamado pelo botão 'Iniciar' no dashboard — separado da seleção
    de exercício para não falar automaticamente ao trocar de aba.
    """
    if _registry is None or _session is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    try:
        ex = _registry.get(key)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    _reporter.set_exercise(ex.name)

    with _lock:
        _state["exercise"] = key

    # Session.start_exercise() entra na fase de instrução (BRIEFING) e só
    # começa a monitorar/corrigir depois que a explicação for falada por
    # completo — evita que uma correção seja falada por cima dela.
    _session.start_exercise(ex)

    socketio.emit("exercise_changed", {"key": key, "name": ex.name})
    return jsonify({"ok": True, "name": ex.name})


@app.route("/api/exercise/pause", methods=["POST"])
def pause_exercise_route():
    """Pausa análise e áudio imediatamente, sem perder o exercício ativo."""
    if _session is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    _session.pause_exercise()
    socketio.emit("session_state", {"state": "paused"})
    return jsonify({"ok": True})


@app.route("/api/exercise/resume", methods=["POST"])
def resume_exercise_route():
    """Retoma a análise a partir de um ciclo limpo."""
    if _session is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    _session.resume_exercise()
    socketio.emit("session_state", {"state": "resumed"})
    return jsonify({"ok": True})


@app.route("/api/exercise/stop", methods=["POST"])
def stop_exercise_route():
    """Silencia tudo e volta ao estado mudo — precisa clicar Iniciar de novo."""
    if _session is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    _session.stop_exercise()
    socketio.emit("session_state", {"state": "stopped"})
    return jsonify({"ok": True})


# ── Modo Configuração (calibração de enquadramento) ────────────────────────────

@app.route("/api/setup/start", methods=["POST"])
def setup_start():
    """
    Botão dedicado 'Iniciar configuração' — calibra o enquadramento de
    corpo inteiro de forma autônoma antes de liberar qualquer exercício
    (ver core/calibration_manager.py). Ao clicar 'Iniciar <exercício>' sem
    ter calibrado ainda, a própria Session já redireciona pra cá sozinha —
    esta rota é só o caminho manual/explícito.
    """
    if _session is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    _session.start_setup()
    socketio.emit("session_state", {"state": "setup"})
    return jsonify({"ok": True})


# ── Configurações de voz ──────────────────────────────────────────────────────

@app.route("/api/voice/settings", methods=["GET"])
def get_voice_settings():
    if _settings is None:
        return jsonify({}), 500
    v = _settings.voice
    return jsonify({
        "enabled":               v.enabled,
        "rate":                  v.rate,
        "volume":                v.volume,
        "voice_id":              v.voice_id,
        "cooldown_ok_s":         v.cooldown_ok_s,
        "cooldown_warn_s":       v.cooldown_warn_s,
        "cooldown_error_s":      v.cooldown_error_s,
        "confirm_on_correction": v.confirm_on_correction,
    })


@app.route("/api/voice/settings", methods=["POST"])
def set_voice_settings():
    """
    Atualiza configurações de voz em tempo real.
    Aceita qualquer subconjunto dos campos — não precisa enviar todos.
    """
    if _settings is None or _tts is None:
        return jsonify({"error": "sessão não inicializada"}), 500

    data = request.get_json(silent=True) or {}
    v    = _settings.voice

    if "enabled" in data:
        v.enabled       = bool(data["enabled"])
        _tts.enabled    = v.enabled
    if "rate" in data:
        v.rate = max(50, min(300, int(data["rate"])))
        _tts.set_rate(v.rate)
    if "volume" in data:
        v.volume = max(0.0, min(1.0, float(data["volume"])))
        _tts.set_volume(v.volume)
    if "voice_id" in data and data["voice_id"]:
        v.voice_id = data["voice_id"]
        _tts.set_voice(v.voice_id)
    if "cooldown_ok_s" in data:
        v.cooldown_ok_s = max(1.0, float(data["cooldown_ok_s"]))
    if "cooldown_warn_s" in data:
        v.cooldown_warn_s = max(1.0, float(data["cooldown_warn_s"]))
    if "cooldown_error_s" in data:
        v.cooldown_error_s = max(0.5, float(data["cooldown_error_s"]))
    if "confirm_on_correction" in data:
        v.confirm_on_correction = bool(data["confirm_on_correction"])

    # Propaga para o estado do dashboard
    with _lock:
        _state["voice_enabled"] = v.enabled
        _state["voice_rate"]    = v.rate
        _state["voice_volume"]  = v.volume

    socketio.emit("voice_settings_changed", {
        "enabled": v.enabled,
        "rate":    v.rate,
        "volume":  v.volume,
    })

    # Fala uma frase de teste se voz foi ativada ou velocidade alterada.
    # Precisa passar pelo AudioCoordinator (não _tts.speak() direto) para
    # respeitar o sink remoto quando o dashboard está ativo — senão a
    # confirmação sai sempre pela mesma voz pyttsx3 do servidor, dando a
    # falsa impressão de que a troca de voz no navegador não fez nada.
    if v.enabled and _session is not None and \
       ("rate" in data or "volume" in data or "voice_id" in data):
        _session.audio.speak_now("Configuração de voz atualizada.")

    return jsonify({"ok": True})


@app.route("/api/voice/test", methods=["POST"])
def test_voice():
    """
    Fala uma frase de teste para o usuário verificar as configurações.
    Passa pelo AudioCoordinator (não direto no TTSEngine) para respeitar o
    sink remoto quando o dashboard está ativo (senão a fala de teste saía
    pelo backend em vez do navegador) e para aparecer no log visual.
    """
    if _session is None:
        return jsonify({"error": "sessão não inicializada"}), 500
    data = request.get_json(silent=True) or {}
    msg  = data.get("message", "Teste de voz do SeeMove.")
    _session.audio.speak_now(msg)
    return jsonify({"ok": True})


@app.route("/api/voice/voices")
def list_voices():
    """Lista as vozes disponíveis no sistema (para o seletor do dashboard)."""
    if _tts is None:
        return jsonify({"voices": []})
    voices = _tts.get_voices()
    # Simplifica para o frontend
    simplified = [
        {
            "id":   v.get("id", ""),
            "name": v.get("name", "Desconhecida"),
        }
        for v in voices
    ]
    return jsonify({"voices": simplified})


# ── Relatórios ────────────────────────────────────────────────────────────────

@app.route("/api/report/html")
def report_html():
    if _reporter is None or not _reporter._records:
        return "Nenhum dado de sessão disponível ainda.", 404
    html = _reporter.generate_html_report()
    return Response(
        html, mimetype="text/html",
        headers={"Content-Disposition": "inline"}
    )


@app.route("/api/report/csv")
def report_csv():
    if _reporter is None or not _reporter._records:
        return "Nenhum dado disponível.", 404
    buf = io.StringIO()
    import csv
    fields = [
        "timestamp", "detected", "confidence",
        "shoulder_tilt", "hip_tilt", "trunk_lean_x",
        "knee_valgus_l", "knee_valgus_r",
        "knee_angle_l",  "knee_angle_r",
        "torso_vertical", "feedback", "severity",
    ]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in _reporter._records:
        w.writerow({
            "timestamp":      r.timestamp,
            "detected":       int(r.detected),
            "confidence":     r.confidence,
            "shoulder_tilt":  r.shoulder_tilt,
            "hip_tilt":       r.hip_tilt,
            "trunk_lean_x":   r.trunk_lean_x,
            "knee_valgus_l":  r.knee_valgus_l,
            "knee_valgus_r":  r.knee_valgus_r,
            "knee_angle_l":   r.knee_angle_l,
            "knee_angle_r":   r.knee_angle_r,
            "torso_vertical": r.torso_vertical,
            "feedback":       r.feedback,
            "severity":       r.severity,
        })
    fname = f"seemove_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


@app.route("/api/report/json")
def report_json():
    if _reporter is None:
        return jsonify({"error": "sem dados"}), 404
    return jsonify({
        "summary":  _reporter.summary(),
        "exercise": _reporter._exercise_name,
    })


# ── Socket.IO ─────────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    """Envia estado completo ao cliente ao conectar."""
    with _lock:
        snap = dict(_state)
        snap.pop("image_b64", None)
    socketio.emit("frame", snap)
    socketio.emit("tracker_status", {
        "msg":       snap["tracker_status"],
        "connected": snap["tracker_connected"],
    })


@socketio.on("request_image")
def on_request_image():
    """Cliente solicita o frame de imagem atual."""
    with _lock:
        img = _state.get("image_b64")
    if img:
        socketio.emit("frame_image", {"img": img})


# ── Inicialização ─────────────────────────────────────────────────────────────

def start(port: int = 5000, open_browser: bool = True):
    def _run():
        socketio.run(
            app, host="127.0.0.1", port=port,
            allow_unsafe_werkzeug=True,
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.5)

    url = f"http://127.0.0.1:{port}"
    print(f"[web] Dashboard: {url}")
    if open_browser:
        webbrowser.open(url)

    return t
