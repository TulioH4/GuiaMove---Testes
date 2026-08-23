"""
core/kinect_tracker.py
Captura de pose via Kinect/webcam + MediaPipe.

Compatível com MediaPipe >= 0.10 (nova API Tasks)
e MediaPipe < 0.10 (API antiga solutions) — detecta automaticamente.

Profundidade via freenect (opcional, só com Kinect v1 físico).
"""

import base64
import threading
import time
from typing import Callable, Dict, List, Optional

from core.skeleton import (
    Point3D, SkeletonFrame, compute_metrics,
    L_SHOULDER, R_SHOULDER, L_HIP, R_HIP,
    L_KNEE, R_KNEE, L_ANKLE, R_ANKLE,
)
from core.filters import LandmarkSmoother

try:
    import freenect
    _FREENECT_OK = True
except ImportError:
    _FREENECT_OK = False

# Nomes dos 33 landmarks MediaPipe (índice → nome)
_LM_NAMES = [
    "nose","left_eye_inner","left_eye","left_eye_outer",
    "right_eye_inner","right_eye","right_eye_outer","left_ear","right_ear",
    "mouth_left","mouth_right","left_shoulder","right_shoulder",
    "left_elbow","right_elbow","left_wrist","right_wrist",
    "left_pinky","right_pinky","left_index","right_index",
    "left_thumb","right_thumb","left_hip","right_hip",
    "left_knee","right_knee","left_ankle","right_ankle",
    "left_heel","right_heel","left_foot_index","right_foot_index",
]


class KinectTracker:
    JPEG_QUALITY = 60

    def __init__(self,
                 camera_index: int = 0,
                 on_frame:  Optional[Callable[[SkeletonFrame], None]] = None,
                 on_status: Optional[Callable[[str, bool], None]] = None,
                 rate_hz: float = 10.0,
                 model_complexity: int = 1,
                 min_detection_confidence: float = 0.5,
                 min_tracking_confidence: float = 0.5,
                 use_depth: bool = True,
                 smoothing_enabled: bool = True,
                 smoothing_min_cutoff: float = 1.0,
                 smoothing_beta: float = 0.3):

        self.camera_index        = camera_index
        self.on_frame            = on_frame
        self.on_status           = on_status
        self.rate_hz             = rate_hz
        self.model_complexity    = model_complexity
        self.min_det_conf        = min_detection_confidence
        self.min_trk_conf        = min_tracking_confidence
        self.use_depth           = use_depth and _FREENECT_OK

        self._smoother: Optional[LandmarkSmoother] = (
            LandmarkSmoother(smoothing_min_cutoff, smoothing_beta)
            if smoothing_enabled else None
        )

        self._running  = False
        self._thread:  Optional[threading.Thread] = None
        self._cap      = None
        self._pose     = None        # instância do detector
        self._api      = "new"       # "new" = Tasks API, "old" = solutions API
        self._depth_frame   = None
        self._depth_lock    = threading.Lock()

    def _log(self, msg: str, connected: bool = False):
        print(f"[kinect] {msg}")
        if self.on_status:
            self.on_status(msg, connected)

    # ── Conexão ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if self._running:
            return True
        try:
            import cv2
        except ImportError:
            self._log("opencv-python não instalado: pip install opencv-python")
            return False

        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def reset_smoothing(self):
        """
        Descarta o estado acumulado do filtro de suavização (One Euro
        Filter). Útil quando as primeiras leituras da sessão saíram ruins
        (pessoa ainda se posicionando, oclusão) e o filtro ficou "preso"
        arrastando esse viés — força a próxima leitura de cada landmark a
        começar do zero, sem precisar reconectar a câmera.
        """
        if self._smoother:
            self._smoother.reset()
            self._log("Suavização de pose resetada.", self._running)

    def disconnect(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        self._release()
        self._log("Desconectado.", False)

    def _release(self):
        if self._pose:
            try:
                self._pose.close()
            except Exception:
                pass
            self._pose = None
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    # ── Profundidade ──────────────────────────────────────────────────────

    def _start_depth(self):
        if not self.use_depth:
            return
        t = threading.Thread(target=self._depth_loop, daemon=True)
        t.start()

    def _depth_loop(self):
        self._log("Iniciando profundidade Kinect IR...", True)
        while self._running:
            try:
                frame, _ = freenect.sync_get_depth()
                with self._depth_lock:
                    self._depth_frame = frame
            except Exception as e:
                self._log(f"Depth error: {e} — desativando.", True)
                self.use_depth = False
                break
            time.sleep(1.0 / 15)

    def _sample_depth(self, x_norm: float, y_norm: float) -> Optional[float]:
        with self._depth_lock:
            d = self._depth_frame
        if d is None:
            return None
        h, w = d.shape
        px = max(2, min(w-3, int(x_norm * w)))
        py = max(2, min(h-3, int(y_norm * h)))
        patch = d[py-2:py+3, px-2:px+3]
        valid = patch[(patch > 0) & (patch < 10000)]
        return float(valid.mean()) if valid.size > 0 else None

    # ── MediaPipe — carregamento com detecção de API ───────────────────────

    def _load_mediapipe(self):
        """
        Tenta carregar MediaPipe na nova API (Tasks, >= 0.10).
        Se não encontrar, cai para a API antiga (solutions, < 0.10).
        """
        import mediapipe as mp

        # Tenta nova API primeiro
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision
            import urllib.request, os, tempfile

            model_path = os.path.join(
                os.path.dirname(__file__), "pose_landmarker.task"
            )
            if not os.path.exists(model_path):
                self._log("Baixando modelo pose_landmarker.task (~7MB)...", True)
                url = ("https://storage.googleapis.com/mediapipe-models/"
                       "pose_landmarker/pose_landmarker_lite/float16/1/"
                       "pose_landmarker_lite.task")
                urllib.request.urlretrieve(url, model_path)
                self._log("Modelo baixado.", True)

            base_opts = mp_python.BaseOptions(model_asset_path=model_path)
            opts = mp_vision.PoseLandmarkerOptions(
                base_options=base_opts,
                output_segmentation_masks=False,
                num_poses=1,
                min_pose_detection_confidence=self.min_det_conf,
                min_pose_presence_confidence=self.min_det_conf,
                min_tracking_confidence=self.min_trk_conf,
            )
            self._pose = mp_vision.PoseLandmarker.create_from_options(opts)
            self._api  = "new"
            self._mp   = mp
            self._mp_vision = mp_vision
            self._log("MediaPipe Tasks API carregado.", True)
            return True

        except Exception as e:
            self._log(f"Tasks API indisponível ({e}) — tentando solutions API...", True)

        # Fallback: API antiga (solutions)
        try:
            pose_api = mp.solutions.pose
            drawing  = mp.solutions.drawing_utils
            styles   = mp.solutions.drawing_styles
            self._pose = pose_api.Pose(
                static_image_mode=False,
                model_complexity=self.model_complexity,
                smooth_landmarks=True,
                enable_segmentation=False,
                min_detection_confidence=self.min_det_conf,
                min_tracking_confidence=self.min_trk_conf,
            )
            self._api      = "old"
            self._mp       = mp
            self._pose_api = pose_api
            self._drawing  = drawing
            self._styles   = styles
            self._log("MediaPipe solutions API carregado.", True)
            return True

        except Exception as e:
            self._log(f"MediaPipe falhou completamente: {e}", False)
            return False

    # ── Loop principal ────────────────────────────────────────────────────

    def _loop(self):
        import cv2

        self._log(f"Abrindo câmera (índice {self.camera_index})...", False)

        # Tenta DirectShow primeiro (Windows)
        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            self._running = False
            self._log("Câmera não encontrada.", False)
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        self._cap = cap
        self._log("Câmera aberta. Carregando MediaPipe...", True)

        self._start_depth()

        if not self._load_mediapipe():
            self._running = False
            return

        delay    = 1.0 / max(1.0, self.rate_hz)
        fail_cnt = 0

        while self._running:
            ok, bgr = cap.read()
            if not ok:
                fail_cnt += 1
                if fail_cnt > 30:
                    self._log("Câmera parou de enviar frames.", True)
                    fail_cnt = 0
                    # Notifica a sessão mesmo sem frame novo da câmera, para
                    # que o watchdog de enquadramento perceba a perda.
                    if self.on_frame:
                        self.on_frame(SkeletonFrame(detected=False, timestamp=time.time()))
                time.sleep(0.05)
                continue

            fail_cnt = 0
            bgr = cv2.flip(bgr, 1)

            if self._api == "new":
                skel = self._process_new(bgr)
            else:
                skel = self._process_old(bgr)

            skel.image_b64 = self._encode(bgr)

            if self.on_frame:
                self.on_frame(skel)

            time.sleep(delay)

        self._release()

    # ── Processamento — nova API (Tasks) ──────────────────────────────────

    def _process_new(self, bgr) -> SkeletonFrame:
        import cv2
        import mediapipe as mp

        skel = SkeletonFrame(detected=False, timestamp=time.time())
        rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result   = self._pose.detect(mp_image)

        if not result.pose_landmarks or not result.pose_landmarks[0]:
            return skel

        lms    = result.pose_landmarks[0]
        points = {}
        raw    = []

        for i, lm in enumerate(lms):
            points[i] = Point3D(x=lm.x, y=lm.y, z=lm.z, visibility=lm.presence)
            raw.append({
                "name": _LM_NAMES[i] if i < len(_LM_NAMES) else str(i),
                "x": round(lm.x, 4), "y": round(lm.y, 4),
                "z": round(lm.z, 4), "visibility": round(lm.presence, 3),
            })

        # Desenha esqueleto no BGR
        if result.pose_landmarks:
            from mediapipe.tasks.python.components.containers import landmark as lm_module
            # Desenha manualmente com cv2 (Tasks API não tem drawing_utils nativo)
            self._draw_skeleton_manual(bgr, lms, (0, 255, 140))

        if self._smoother:
            points = self._smoother.smooth(points, skel.timestamp)
        self._enrich_depth(points)
        skel.points        = points
        skel.metrics       = compute_metrics(points)
        skel.detected      = True
        skel.raw_landmarks = raw
        return skel

    def _draw_skeleton_manual(self, bgr, landmarks, color):
        """Desenha o esqueleto manualmente para a nova API Tasks."""
        import cv2
        h, w = bgr.shape[:2]
        BONES = [
            (11,12),(11,23),(12,24),(23,24),
            (11,13),(13,15),(12,14),(14,16),
            (23,25),(25,27),(24,26),(26,28),
            (27,29),(28,30),(29,31),(30,32),
        ]
        pts = [(int(lm.x*w), int(lm.y*h)) for lm in landmarks]
        for a, b in BONES:
            if a < len(pts) and b < len(pts):
                cv2.line(bgr, pts[a], pts[b], color, 2, cv2.LINE_AA)
        for i, (x, y) in enumerate(pts):
            r = 5 if i in (25,26,27,28) else 3
            cv2.circle(bgr, (x, y), r, color, -1, cv2.LINE_AA)

    # ── Processamento — API antiga (solutions) ────────────────────────────

    def _process_old(self, bgr) -> SkeletonFrame:
        import cv2

        skel = SkeletonFrame(detected=False, timestamp=time.time())
        rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        result = self._pose.process(rgb)
        rgb.flags.writeable = True

        if not result.pose_landmarks:
            return skel

        lms    = result.pose_landmarks.landmark
        points = {}
        raw    = []

        for i, lm in enumerate(lms):
            points[i] = Point3D(x=lm.x, y=lm.y, z=lm.z, visibility=lm.visibility)
            raw.append({
                "name": _LM_NAMES[i] if i < len(_LM_NAMES) else str(i),
                "x": round(lm.x, 4), "y": round(lm.y, 4),
                "z": round(lm.z, 4), "visibility": round(lm.visibility, 3),
            })

        # Desenha esqueleto com utilitários nativos
        self._drawing.draw_landmarks(
            bgr,
            result.pose_landmarks,
            self._pose_api.POSE_CONNECTIONS,
            landmark_drawing_spec=self._styles.get_default_pose_landmarks_style(),
        )

        if self._smoother:
            points = self._smoother.smooth(points, skel.timestamp)
        self._enrich_depth(points)
        skel.points        = points
        skel.metrics       = compute_metrics(points)
        skel.detected      = True
        skel.raw_landmarks = raw
        return skel

    # ── Utilidades ────────────────────────────────────────────────────────

    def _enrich_depth(self, points: Dict[int, Point3D]):
        """Substitui Z estimado pelo MediaPipe por profundidade real do Kinect."""
        if not self.use_depth:
            return
        for idx in (L_KNEE, R_KNEE, L_ANKLE, R_ANKLE, L_HIP, R_HIP):
            p = points.get(idx)
            if p and p.visible:
                depth_mm = self._sample_depth(p.x, p.y)
                if depth_mm:
                    points[idx] = Point3D(
                        x=p.x, y=p.y,
                        z=-(depth_mm / 1000.0),
                        visibility=p.visibility,
                    )

    def _encode(self, bgr) -> Optional[str]:
        import cv2
        h, w = bgr.shape[:2]
        tw = 424
        if w > tw:
            bgr = cv2.resize(bgr, (tw, int(h*tw/w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(buf).decode("ascii")
