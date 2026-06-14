"""
web/server.py
Servidor Flask + Socket.IO do SeeMove.

Rotas:
  GET  /                      Dashboard principal
  GET  /api/state             Estado atual da sessão (JSON)
  POST /api/exercise/<name>   Troca exercício ativo
  POST /api/hardware/connect  Inicia conexão com Balance Board real
  POST /api/hardware/disconnect Desconecta hardware
  GET  /api/report/html       Gera e retorna relatório HTML completo
  GET  /api/report/csv        Gera e retorna CSV para download
  GET  /api/report/json       Retorna JSON completo da sessão
"""

import threading
import webbrowser
import time
import io
from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "seemove-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Referências injetadas pelo main.py
_session_ref = None
_reporter_ref = None
_board_ref = None
_settings_ref = None
_kinect_ref = None

_state = {
    "connected": False,
    "hw_mode": False,
    "hw_status": "Desconectado",
    "exercise": "squat",
    "cog_x": 0.0, "cog_y": 0.0,
    "total_kg": 0.0,
    "tl": 0.0, "tr": 0.0, "bl": 0.0, "br": 0.0,
    "magnitude": 0.0,
    "stability_pct": 100,
    "is_centered": True,
    "feedback": "Aguardando...",
    "severity": "ok",
    "duration_s": 0,
    "centered_pct": 0.0,
    "corrections": 0,
    "tts_log": [],
    "has_data": False,
}
_kinect_state = {
    "connected": False,
    "status": "Desconectado",
    "source": "Kinect RGB",
    "has_pose": False,
    "image": None,
    "landmarks": [],
    "frame_count": 0,
    "metrics": {
        "confidence_pct": 0,
        "shoulder_tilt": 0.0,
        "hip_tilt": 0.0,
        "left_knee_y": None,
        "right_knee_y": None,
    },
    "timestamp": 0,
}
_lock = threading.Lock()


def inject(session, reporter, board, settings):
    """Chamado pelo main.py para registrar referências dos objetos vivos."""
    global _session_ref, _reporter_ref, _board_ref, _settings_ref
    _session_ref = session
    _reporter_ref = reporter
    _board_ref = board
    _settings_ref = settings


def push_state(sensor_data, cog, feedback_result, summary):
    with _lock:
        _state.update({
            "has_data": True,
            "cog_x": round(cog.x, 4),
            "cog_y": round(cog.y, 4),
            "total_kg": round(cog.total_kg, 2),
            "tl": round(sensor_data.top_left, 1),
            "tr": round(sensor_data.top_right, 1),
            "bl": round(sensor_data.bottom_left, 1),
            "br": round(sensor_data.bottom_right, 1),
            "magnitude": round(cog.magnitude, 4),
            "stability_pct": cog.stability_pct(),
            "is_centered": cog.is_centered,
            "feedback": feedback_result.message,
            "severity": feedback_result.severity,
            "duration_s": summary.get("duration_s", 0),
            "centered_pct": summary.get("centered_pct", 0.0),
            "corrections": summary.get("corrections", 0),
        })
        snap = dict(_state)
    socketio.emit("frame", snap)


def push_tts(message: str, severity: str = "ok"):
    with _lock:
        entry = {"time": time.strftime("%M:%S"), "msg": message, "sev": severity}
        _state["tts_log"] = ([entry] + _state["tts_log"])[:30]
    socketio.emit("tts", entry)


def push_hw_status(msg: str, connected: bool = False):
    with _lock:
        _state["hw_status"] = msg
        _state["connected"] = connected
    socketio.emit("hw_status", {"msg": msg, "connected": connected})


def push_kinect_frame(frame):
    with _lock:
        _kinect_state.update({
            "connected": frame.connected,
            "status": frame.status,
            "has_pose": frame.has_pose,
            "image": frame.image,
            "landmarks": frame.landmarks,
            "frame_count": frame.frame_count,
            "metrics": frame.metrics,
            "timestamp": frame.timestamp,
        })
        snap = dict(_kinect_state)
    socketio.emit("kinect_frame", snap)


def push_kinect_status(msg: str, connected: bool = False):
    with _lock:
        _kinect_state["status"] = msg
        _kinect_state["connected"] = connected
        snap = dict(_kinect_state)
    socketio.emit("kinect_status", {"msg": msg, "connected": connected})
    socketio.emit("kinect_frame", snap)


# ── Rotas ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(_state)


@app.route("/api/exercise/<name>", methods=["POST"])
def set_exercise(name):
    valid = ["squat", "balance", "stand", "lunge"]
    if name not in valid:
        return jsonify({"error": "exercício inválido"}), 400
    with _lock:
        _state["exercise"] = name
    if _session_ref:
        from exercises.registry import ExerciseRegistry
        try:
            ex = ExerciseRegistry().get(name)
            _session_ref.exercise = ex
            if _reporter_ref:
                _reporter_ref.set_exercise(ex.name)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    socketio.emit("exercise_changed", {"exercise": name})
    return jsonify({"ok": True})


@app.route("/api/hardware/connect", methods=["POST"])
def hw_connect():
    """Inicia conexão com o Balance Board real em thread separada."""
    if _board_ref is None:
        return jsonify({"error": "sessão não inicializada"}), 500

    def _do_connect():
        push_hw_status("Buscando Balance Board... pressione o botão vermelho.", False)
        ok = _board_ref.connect()
        if ok:
            push_hw_status("Balance Board conectado!", True)
            socketio.emit("hw_connected", {})
        else:
            push_hw_status("Não foi possível conectar. Verifique o Bluetooth.", False)
            socketio.emit("hw_error", {"msg": "Conexão falhou"})

    t = threading.Thread(target=_do_connect, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Conectando..."})


@app.route("/api/hardware/disconnect", methods=["POST"])
def hw_disconnect():
    if _board_ref:
        _board_ref.disconnect()
        push_hw_status("Desconectado.", False)
    return jsonify({"ok": True})


@app.route("/api/kinect/state")
def kinect_state():
    with _lock:
        return jsonify(_kinect_state)


@app.route("/api/kinect/connect", methods=["POST"])
def kinect_connect():
    """Inicia captura RGB + MediaPipe usando Kinect v1 ou webcam comum."""
    global _kinect_ref
    if _kinect_ref is not None:
        with _lock:
            is_connected = _kinect_state.get("connected", False)
        if is_connected:
            return jsonify({"ok": True, "msg": "Kinect ja inicializado."})
        try:
            _kinect_ref.disconnect()
        except Exception:
            pass
        _kinect_ref = None

    data = request.get_json(silent=True) or {}
    camera_index = int(data.get("camera_index", 0))
    source = data.get("source", "kinect")
    source_name = "Webcam do computador" if source == "webcam" else "Kinect RGB"

    try:
        from core.kinect_tracker import KinectPoseTracker
    except Exception as e:
        push_kinect_status(f"Modulo Kinect indisponivel: {e}", False)
        return jsonify({"error": str(e)}), 500

    tracker = KinectPoseTracker(
        camera_index=camera_index,
        source_name=source_name,
        on_frame=push_kinect_frame,
        on_status=push_kinect_status,
    )
    with _lock:
        _kinect_state["source"] = source_name
    _kinect_ref = tracker

    def _do_connect():
        ok = tracker.connect()
        if not ok:
            global _kinect_ref
            _kinect_ref = None

    t = threading.Thread(target=_do_connect, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Conectando Kinect..."})


@app.route("/api/kinect/disconnect", methods=["POST"])
def kinect_disconnect():
    global _kinect_ref
    if _kinect_ref:
        _kinect_ref.disconnect()
        _kinect_ref = None
    push_kinect_status("Desconectado.", False)
    return jsonify({"ok": True})


@app.route("/api/report/html")
def report_html():
    if _reporter_ref is None:
        return "Sessão não iniciada.", 404
    html = _reporter_ref.generate_html_report()
    return Response(html, mimetype="text/html",
                    headers={"Content-Disposition": "inline"})


@app.route("/api/report/csv")
def report_csv():
    if _reporter_ref is None:
        return "Sessão não iniciada.", 404
    buf = io.StringIO()
    import csv
    records = _reporter_ref._records
    if not records:
        return "Sem dados ainda.", 404
    fields = ["timestamp","tl_kg","tr_kg","bl_kg","br_kg","total_kg",
              "cog_x","cog_y","magnitude","is_centered","stability_pct",
              "feedback","severity"]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in records:
        w.writerow({"timestamp": r.timestamp, "tl_kg": r.tl, "tr_kg": r.tr,
                    "bl_kg": r.bl, "br_kg": r.br, "total_kg": r.total_kg,
                    "cog_x": r.cog_x, "cog_y": r.cog_y, "magnitude": r.magnitude,
                    "is_centered": int(r.is_centered), "stability_pct": r.stability_pct,
                    "feedback": r.feedback, "severity": r.severity})
    fname = f"seemove_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/api/report/json")
def report_json():
    if _reporter_ref is None:
        return jsonify({"error": "sem dados"}), 404
    import json
    s = _reporter_ref.summary()
    records = [{"t": r.timestamp, "x": r.cog_x, "y": r.cog_y,
                "mag": r.magnitude, "stab": r.stability_pct, "ok": r.is_centered}
               for r in _reporter_ref._records]
    return jsonify({"summary": s, "records": records})


@socketio.on("connect")
def on_connect():
    with _lock:
        socketio.emit("frame", dict(_state))
        socketio.emit("kinect_frame", dict(_kinect_state))


def start(host="127.0.0.1", port=5000, open_browser=True):
    def _run():
        socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.5)
    if open_browser:
        webbrowser.open(f"http://{host}:{port}")
    print(f"[web] Dashboard: http://{host}:{port}")
    return t
