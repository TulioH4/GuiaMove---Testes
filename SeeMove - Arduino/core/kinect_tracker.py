"""
core/kinect_tracker.py
Captura pose corporal usando a camera RGB do Kinect v1 via OpenCV + MediaPipe.

Este modulo nao usa profundidade nem SDK do Kinect. Para o SeeMove, o Kinect
entra como uma camera USB comum, e o MediaPipe estima as juntas do corpo.
"""

import base64
import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class KinectFrame:
    connected: bool = False
    status: str = "Desconectado"
    has_pose: bool = False
    image: Optional[str] = None
    landmarks: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    frame_count: int = 0
    timestamp: float = field(default_factory=time.time)


class KinectPoseTracker:
    """Tracker de esqueleto baseado em RGB, com callback por frame."""

    def __init__(
        self,
        camera_index: int = 0,
        source_name: str = "Kinect RGB",
        on_frame: Optional[Callable[[KinectFrame], None]] = None,
        on_status: Optional[Callable[[str, bool], None]] = None,
        width: int = 640,
        height: int = 480,
        rate_hz: float = 6.0,
    ):
        self.camera_index = camera_index
        self.source_name = source_name
        self.on_frame = on_frame
        self.on_status = on_status
        self.width = width
        self.height = height
        self.rate_hz = rate_hz

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cap = None
        self._pose = None
        self._pose_api = None
        self._drawing = None
        self._styles = None
        self._pose_future = None
        self._pose_error = None
        self._executor = None
        self._frame_count = 0

    def _status(self, message: str, connected: bool):
        print(f"[kinect] {message}")
        if self.on_status:
            self.on_status(message, connected)

    def connect(self) -> bool:
        if self._running:
            return True

        try:
            import cv2  # noqa: F401
        except ImportError as e:
            self._status(
                "OpenCV ausente. Instale: pip install opencv-python",
                False,
            )
            return False

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def disconnect(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._release()
        self._status("Kinect desconectado.", False)

    def _release(self):
        if self._pose:
            try:
                self._pose.close()
            except Exception:
                pass
            self._pose = None
            self._pose_api = None
            self._drawing = None
            self._styles = None
        if self._executor:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._executor = None
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _loop(self):
        import cv2

        self._status(f"Abrindo camera: {self.source_name} (indice {self.camera_index})...", False)
        self._cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not self._cap.isOpened():
            self._cap = cv2.VideoCapture(self.camera_index)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        if not self._cap.isOpened():
            self._running = False
            self._status(
                "Camera nao encontrada. Verifique o adaptador USB/fonte e o indice da camera.",
                False,
            )
            return

        self._status(f"{self.source_name} aberta. Preview ativo.", True)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._pose_future = self._executor.submit(self._create_pose)

        self._status(
            f"{self.source_name}: camera ativa, carregando MediaPipe em segundo plano.",
            True,
        )
        delay = 1.0 / max(1.0, self.rate_hz)

        failed_reads = 0
        last_read_warning = 0.0

        while self._running:
            ok, frame = self._cap.read()
            if not ok:
                failed_reads += 1
                now = time.time()
                if failed_reads >= 10 and now - last_read_warning > 2.0:
                    last_read_warning = now
                    self._status(
                        f"{self.source_name} abriu, mas nao esta entregando frames. Tente outro indice.",
                        True,
                    )
                time.sleep(0.05)
                continue

            failed_reads = 0
            self._frame_count += 1
            frame = cv2.flip(frame, 1)
            frame = self._resize_for_dashboard(frame)

            landmarks = []
            metrics = {"confidence_pct": 0, "shoulder_tilt": 0.0, "hip_tilt": 0.0}
            has_pose = False
            pose_ready = self._poll_pose_ready()

            if pose_ready:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = self._pose.process(rgb)
                has_pose = result.pose_landmarks is not None

                if has_pose:
                    self._drawing.draw_landmarks(
                        frame,
                        result.pose_landmarks,
                        self._pose_api.POSE_CONNECTIONS,
                        landmark_drawing_spec=self._styles.get_default_pose_landmarks_style(),
                    )
                    landmarks = self._serialize_landmarks(result.pose_landmarks.landmark)
                    metrics = self._metrics(landmarks)

            if self._pose_error:
                status = f"{self.source_name}: camera ativa, MediaPipe falhou ({self._pose_error})"
            elif not pose_ready:
                status = f"{self.source_name}: camera ativa, carregando MediaPipe"
            elif has_pose:
                status = f"{self.source_name}: pose detectada"
            else:
                status = f"{self.source_name}: aguardando corpo no enquadramento"

            image = self._encode_frame(frame)
            payload = KinectFrame(
                connected=True,
                status=status,
                has_pose=has_pose,
                image=image,
                landmarks=landmarks,
                metrics=metrics,
                frame_count=self._frame_count,
            )
            if self.on_frame:
                self.on_frame(payload)
            time.sleep(delay)

        self._release()

    def _create_pose(self):
        import mediapipe as mp

        try:
            pose_api = mp.solutions.pose
            drawing = mp.solutions.drawing_utils
            styles = mp.solutions.drawing_styles
        except AttributeError:
            pose_api, drawing, styles = self._load_mediapipe_solutions_fallback()

        pose = pose_api.Pose(
            static_image_mode=False,
            model_complexity=0,
            smooth_landmarks=True,
            enable_segmentation=False,
            min_detection_confidence=0.45,
            min_tracking_confidence=0.45,
        )
        return pose, pose_api, drawing, styles

    def _load_mediapipe_solutions_fallback(self):
        import importlib

        try:
            pose_api = importlib.import_module("mediapipe.python.solutions.pose")
            drawing = importlib.import_module("mediapipe.python.solutions.drawing_utils")
            styles = importlib.import_module("mediapipe.python.solutions.drawing_styles")
            return pose_api, drawing, styles
        except Exception as e:
            raise RuntimeError(
                "o pacote mediapipe instalado nao expoe a API Pose esperada. "
                "Tente reinstalar com: pip uninstall mediapipe && pip install mediapipe"
            ) from e

    def _poll_pose_ready(self) -> bool:
        if self._pose:
            return True
        if self._pose_error:
            return False
        if not self._pose_future:
            return False
        if not self._pose_future.done():
            return False
        try:
            self._pose, self._pose_api, self._drawing, self._styles = self._pose_future.result()
            self._status(f"{self.source_name}: MediaPipe pronto.", True)
            return True
        except Exception as e:
            self._pose_error = str(e)
            self._status(f"Falha ao inicializar MediaPipe: {e}", True)
            return False

    def _serialize_landmarks(self, raw_landmarks) -> list[dict]:
        names = [
            "nose", "left_eye_inner", "left_eye", "left_eye_outer",
            "right_eye_inner", "right_eye", "right_eye_outer", "left_ear",
            "right_ear", "mouth_left", "mouth_right", "left_shoulder",
            "right_shoulder", "left_elbow", "right_elbow", "left_wrist",
            "right_wrist", "left_pinky", "right_pinky", "left_index",
            "right_index", "left_thumb", "right_thumb", "left_hip",
            "right_hip", "left_knee", "right_knee", "left_ankle",
            "right_ankle", "left_heel", "right_heel", "left_foot_index",
            "right_foot_index",
        ]
        return [
            {
                "name": names[i] if i < len(names) else str(i),
                "x": round(lm.x, 4),
                "y": round(lm.y, 4),
                "z": round(lm.z, 4),
                "visibility": round(lm.visibility, 4),
            }
            for i, lm in enumerate(raw_landmarks)
        ]

    def _metrics(self, landmarks: list[dict]) -> dict:
        by_name = {lm["name"]: lm for lm in landmarks}

        def tilt(left_name: str, right_name: str) -> float:
            left = by_name.get(left_name)
            right = by_name.get(right_name)
            if not left or not right:
                return 0.0
            return round((right["y"] - left["y"]) * 100.0, 2)

        visible = [lm["visibility"] for lm in landmarks]
        confidence = round(sum(visible) / max(1, len(visible)) * 100)

        return {
            "confidence_pct": confidence,
            "shoulder_tilt": tilt("left_shoulder", "right_shoulder"),
            "hip_tilt": tilt("left_hip", "right_hip"),
            "left_knee_y": by_name.get("left_knee", {}).get("y"),
            "right_knee_y": by_name.get("right_knee", {}).get("y"),
        }

    def _encode_frame(self, frame) -> Optional[str]:
        import cv2

        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 58])
        if not ok:
            return None
        data = base64.b64encode(buffer).decode("ascii")
        return f"data:image/jpeg;base64,{data}"

    def _resize_for_dashboard(self, frame):
        import cv2

        h, w = frame.shape[:2]
        target_w = 424
        if w <= target_w:
            return frame
        target_h = int(h * (target_w / w))
        return cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
