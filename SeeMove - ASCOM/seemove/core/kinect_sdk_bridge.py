"""
core/kinect_sdk_bridge.py
Cliente Python do executável nativo native/kinect_bridge/kinect_color_bridge.exe.

Por quê: o driver oficial "Kinect for Windows" do Kinect v1 não expõe a
câmera como um dispositivo de captura genérico (DirectShow/Media
Foundation) — só a API NUI (Kinect10.dll) tem acesso aos streams de cor
e profundidade. O executável nativo fala NUI e publica cada stream numa
área de memória compartilhada própria; esta classe só lê essa memória,
sem nenhum binding direto com a DLL do Kinect.
"""

import mmap
import os
import struct
import subprocess
import threading
import time
from typing import Optional

import numpy as np

FRAME_WIDTH  = 640
FRAME_HEIGHT = 480
HEADER_BYTES = 16  # frame_counter (u64) + width (u32) + height (u32)

COLOR_SHM_NAME  = "GuiaMoveKinectColor"
COLOR_FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 4  # BGRA
COLOR_TOTAL_BYTES = HEADER_BYTES + COLOR_FRAME_BYTES

DEPTH_SHM_NAME  = "GuiaMoveKinectDepth"
DEPTH_FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 2  # uint16, mm
DEPTH_TOTAL_BYTES = HEADER_BYTES + DEPTH_FRAME_BYTES

CONTROL_SHM_NAME = "GuiaMoveKinectControl"
# requested_angle, command_seq, current_angle, last_applied_seq (LONG = int32)
_CONTROL_STRUCT = struct.Struct("<iiii")
CONTROL_TOTAL_BYTES = _CONTROL_STRUCT.size

TILT_MIN_DEGREES = -27
TILT_MAX_DEGREES = 27

BRIDGE_EXE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "native", "kinect_bridge", "kinect_color_bridge.exe",
)


class KinectSDKBridge:
    def __init__(self, log=print):
        self._log = log
        self._proc: Optional[subprocess.Popen] = None
        self._mm_color: Optional[mmap.mmap] = None
        self._mm_depth: Optional[mmap.mmap] = None
        self._mm_control: Optional[mmap.mmap] = None
        self._last_color_counter = -1
        self._last_depth_counter = -1
        self._tilt_seq = 0
        self._dead = False
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self, timeout: float = 10.0) -> bool:
        if not os.path.exists(BRIDGE_EXE):
            self._log(
                f"Bridge do Kinect SDK não encontrada em {BRIDGE_EXE} "
                f"(rode native/kinect_bridge/build.bat primeiro)."
            )
            return False

        # Se uma sessão anterior morreu de forma abrupta (crash, encerrada
        # pelo Gerenciador de Tarefas), o processo filho da bridge pode ter
        # ficado órfão segurando o Kinect (a API NUI só permite um processo
        # por vez — a próxima tentativa falharia com E_NUI_DEVICE_IN_USE).
        # Mata qualquer instância anterior antes de abrir uma nova.
        subprocess.run(
            ["taskkill", "/F", "/IM", os.path.basename(BRIDGE_EXE)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        self._proc = subprocess.Popen(
            [BRIDGE_EXE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Drena o stderr continuamente numa thread própria. Sem isso, o
        # pipe só era lido quando o processo já tinha morrido — se a
        # bridge logasse o suficiente enquanto viva (ex.: erros repetidos
        # do motor de inclinação), o buffer do pipe enchia e o write()
        # dela bloqueava, travando a bridge inteira (é um loop só, cor/
        # profundidade/motor tudo junto).
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self._log("Bridge do Kinect SDK encerrou de imediato.")
                return False
            try:
                self._mm_color = mmap.mmap(-1, COLOR_TOTAL_BYTES, tagname=COLOR_SHM_NAME)
                self._mm_depth = mmap.mmap(-1, DEPTH_TOTAL_BYTES, tagname=DEPTH_SHM_NAME)
                self._mm_control = mmap.mmap(-1, CONTROL_TOTAL_BYTES, tagname=CONTROL_SHM_NAME)
                self._tilt_seq = 0
                return True
            except OSError:
                time.sleep(0.2)

        self._log("Timeout esperando a bridge do Kinect SDK inicializar.")
        self.stop()
        return False

    def _drain_stderr(self):
        if not self._proc or not self._proc.stderr:
            return
        try:
            for line in self._proc.stderr:
                line = line.rstrip()
                if line:
                    self._log(f"[kinect-bridge] {line}")
        except (ValueError, OSError):
            pass  # pipe fechado durante stop() -- normal ao encerrar

    def _check_alive(self) -> bool:
        if self._dead:
            return False
        if self._proc is not None and self._proc.poll() is not None:
            self._dead = True
            self._log("Bridge do Kinect SDK encerrou inesperadamente.")
            return False
        return True

    def read(self) -> Optional[np.ndarray]:
        """Retorna o frame de cor mais recente em BGR (numpy), ou None se
        não houver frame novo desde a última leitura."""
        if self._mm_color is None or not self._check_alive():
            return None

        self._mm_color.seek(0)
        counter = struct.unpack("<Q", self._mm_color.read(8))[0]
        if counter == 0 or counter == self._last_color_counter:
            return None
        self._last_color_counter = counter

        self._mm_color.seek(HEADER_BYTES)
        raw = self._mm_color.read(COLOR_FRAME_BYTES)
        bgra = np.frombuffer(raw, dtype=np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH, 4)
        return np.ascontiguousarray(bgra[:, :, :3])  # descarta o canal X/alpha

    def read_depth(self) -> Optional[np.ndarray]:
        """Retorna o frame de profundidade mais recente (numpy uint16,
        milímetros reais do sensor IR), ou None se não houver frame novo."""
        if self._mm_depth is None or not self._check_alive():
            return None

        self._mm_depth.seek(0)
        counter = struct.unpack("<Q", self._mm_depth.read(8))[0]
        if counter == 0 or counter == self._last_depth_counter:
            return None
        self._last_depth_counter = counter

        self._mm_depth.seek(HEADER_BYTES)
        raw = self._mm_depth.read(DEPTH_FRAME_BYTES)
        return np.frombuffer(raw, dtype=np.uint16).reshape(FRAME_HEIGHT, FRAME_WIDTH).copy()

    def set_tilt(self, angle_degrees: int) -> bool:
        """
        Pede pro motor do Kinect inclinar pra um ângulo (-27 a 27 graus).
        Não bloqueia — a bridge aplica no próprio ritmo, respeitando o
        cooldown do motor (comandos rápidos demais são coalescidos: só o
        último ângulo pedido antes do cooldown liberar é realmente aplicado).
        Retorna False se a bridge não estiver rodando.
        """
        if self._mm_control is None or not self._check_alive():
            return False
        angle_degrees = max(TILT_MIN_DEGREES, min(TILT_MAX_DEGREES, int(angle_degrees)))
        self._tilt_seq += 1
        self._mm_control.seek(0)
        self._mm_control.write(_CONTROL_STRUCT.pack(angle_degrees, self._tilt_seq, 0, 0))
        return True

    def get_tilt_status(self) -> Optional[dict]:
        """Retorna {'current_angle', 'requested_angle', 'pending'} ou None
        se a bridge não estiver rodando. 'pending' indica se o último
        comando enviado ainda não foi aplicado (motor girando/na fila)."""
        if self._mm_control is None or not self._check_alive():
            return None
        self._mm_control.seek(0)
        requested, _seq, current, last_applied = _CONTROL_STRUCT.unpack(self._mm_control.read(CONTROL_TOTAL_BYTES))
        return {
            "current_angle": current,
            "requested_angle": requested,
            "pending": last_applied != self._tilt_seq,
        }

    def stop(self):
        for attr in ("_mm_color", "_mm_depth", "_mm_control"):
            mm = getattr(self, attr)
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
