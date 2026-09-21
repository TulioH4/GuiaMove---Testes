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
    Point3D, SkeletonFrame, compute_metrics, MIN_VIS,
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

# O MediaPipe continua devolvendo visibility/presence ~0.85+ para landmarks
# que ele EXTRAPOLA pra fora da imagem (medido em gravações reais: ~90% dos
# pontos fora do quadro passavam do limiar de 0.45) — é o "fantasma" de
# quando a pessoa está muito perto ou sai de quadro pela borda. Por isso a
# confiança cai linearmente com a distância que o ponto está FORA da imagem
# (0 quando passa de EDGE_FADE, em fração da imagem).
EDGE_FADE = 0.08


def _edge_gated(x: float, y: float, vis: float) -> float:
    outside = max(0.0, -x, x - 1.0, -y, y - 1.0)
    if outside <= 0.0:
        return vis
    return vis * max(0.0, 1.0 - outside / EDGE_FADE)


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
                 smoothing_min_cutoff: float = 2.0,
                 smoothing_beta: float = 1.5,
                 use_sdk_bridge: bool = False,
                 use_sdk_depth: bool = False):

        self.camera_index        = camera_index
        self.on_frame            = on_frame
        self.on_status           = on_status
        self.rate_hz             = rate_hz
        self.model_complexity    = model_complexity
        self.min_det_conf        = min_detection_confidence
        self.min_trk_conf        = min_tracking_confidence
        # Kinect v1: o driver oficial "Kinect for Windows" não expõe a
        # câmera como dispositivo de captura genérico, então
        # cv2.VideoCapture nunca a enxerga. Com use_sdk_bridge=True, a
        # captura vem do executável nativo em native/kinect_bridge/, que
        # fala a API NUI oficial e publica os frames numa memória
        # compartilhada (ver core/kinect_sdk_bridge.py).
        self.use_sdk_bridge      = use_sdk_bridge
        # A bridge também sabe ler profundidade real via NUI, mas testado
        # ao vivo o retorno de infravermelho no joelho falha com frequência
        # nesse ambiente/distância — o gate por perna (só enriquece se
        # quadril+joelho+tornozelo lerem bem juntos) quase nunca fecha, e
        # nos poucos frames em que fecha no meio de um movimento, o salto
        # de escala entre "profundidade real" e "estimativa do MediaPipe"
        # do frame anterior cria um tranco visível no boneco 3D — pior que
        # não ter profundidade nenhuma. Por ora, profundidade enriquecida
        # continua só via freenect (mais estável nos testes que já existiam
        # antes); o Z_DAMPING reduzido em dashboard.html já resolve o
        # problema original sozinho, sem essa instabilidade.
        # use_sdk_depth é um opt-in explícito separado, pra testar a
        # profundidade da bridge (ex.: com o Kinect mais perto da pessoa,
        # onde o retorno do infravermelho é bem melhor) sem reativar isso
        # por padrão até ficar comprovadamente estável.
        # `depth_capable`: o que a configuração de execução PERMITE (freenect
        # instalado, ou --kinect-sdk + --kinect-sdk-depth). `use_depth` é o
        # pedido do momento — o servidor o altera pelo botão "Conectar" do
        # dashboard, cujo checkbox vem marcado por padrão; sem esse teto isso
        # religava a profundidade instável mesmo sem --kinect-sdk-depth.
        self.depth_capable       = _FREENECT_OK or (use_sdk_bridge and use_sdk_depth)
        self.use_depth           = use_depth and self.depth_capable

        self._smoother: Optional[LandmarkSmoother] = (
            LandmarkSmoother(smoothing_min_cutoff, smoothing_beta)
            if smoothing_enabled else None
        )

        self._running  = False
        self._thread:  Optional[threading.Thread] = None
        self._cap      = None
        self._sdk_bridge = None      # KinectSDKBridge quando use_sdk_bridge=True
        self._pose     = None        # instância do detector
        self._api      = "new"       # "new" = Tasks API, "old" = solutions API
        # Tasks API roda em modo VIDEO (rastreia a pessoa de um quadro pro
        # próximo em vez de re-detectar do zero a cada um): medido nas
        # gravações do projeto, metade do custo por quadro (~10 ms contra
        # ~21 ms), ~40-50% menos jitter, bem menos "perdeu/achou" a cada
        # poucos quadros e menos trocas esquerda/direita. O modo VIDEO exige
        # timestamps estritamente crescentes (ms).
        self._model_path      = None
        self._mp_python       = None
        self._mp_vision       = None
        self._last_ts_ms      = -1
        self._pose_reset_req  = False   # pedido de re-detecção (setado por outra thread)
        self._depth_frame   = None
        self._depth_lock    = threading.Lock()
        self._depth_thread: Optional[threading.Thread] = None

    def _log(self, msg: str, connected: bool = False):
        print(f"[kinect] {msg}")
        if self.on_status:
            self.on_status(msg, connected)

    def _bridge_log(self, msg: str):
        """Log da ponte nativa: só PROBLEMAS DA CÂMERA viram status do painel.
        As linhas informativas da ponte ("Memoria compartilhada pronta...")
        chegam de forma assíncrona (thread do stderr) e, tratadas como status
        "desconectado", pintavam um banner amarelo de aviso com a câmera
        funcionando normalmente; e erros só do motor de inclinação (que não
        afetam a imagem) também não podem sinalizar câmera com defeito."""
        low = msg.lower()
        benign = "elevation" in low or "motor" in low
        problem = (not benign) and any(
            w in low for w in ("falhou", "erro", "error", "encerrou", "timeout", "não encontrada"))
        if problem:
            self._log(msg, False)
        else:
            print(f"[kinect] {msg}")

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
        # O detector em modo VIDEO também "gruda" na pessoa que já vinha
        # rastreando — se travou numa pose errada (ou em outra pessoa), só
        # zerar o filtro não basta: pede pra thread de captura recriá-lo (o
        # objeto do MediaPipe só pode ser usado por ela).
        self._pose_reset_req = True
        self._log("Suavização de pose resetada.", self._running)

    # ── Motor de inclinação (só no modo --kinect-sdk) ──────────────────────

    def set_tilt(self, angle_degrees: int) -> bool:
        """Inclina o Kinect (-27 a 27 graus). Só funciona com o motor
        físico do Kinect v1 pela ponte do SDK — sem efeito em webcam
        comum ou quando a ponte não está rodando."""
        if self._sdk_bridge:
            return self._sdk_bridge.set_tilt(angle_degrees)
        return False

    def get_tilt_status(self) -> Optional[dict]:
        if self._sdk_bridge:
            return self._sdk_bridge.get_tilt_status()
        return None

    def disconnect(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._depth_thread:
            self._depth_thread.join(timeout=1.0)
        self._release()
        self._log("Desconectado.", False)

    def _release(self):
        if self._sdk_bridge:
            self._sdk_bridge.stop()
            self._sdk_bridge = None
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
        self._depth_thread = threading.Thread(target=self._depth_loop, daemon=True)
        self._depth_thread.start()

    def _depth_loop(self):
        if self.use_sdk_bridge:
            self._depth_loop_sdk()
        else:
            self._depth_loop_freenect()

    def _depth_loop_freenect(self):
        self._log("Iniciando profundidade Kinect IR (freenect)...", True)
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

    def _depth_loop_sdk(self):
        self._log("Iniciando profundidade Kinect IR (SDK bridge)...", True)
        while self._running:
            try:
                frame = self._sdk_bridge.read_depth() if self._sdk_bridge else None
                if frame is not None:
                    with self._depth_lock:
                        self._depth_frame = frame
            except Exception as e:
                self._log(f"Depth error (SDK bridge): {e} — desativando.", True)
                self.use_depth = False
                break
            time.sleep(1.0 / 30)

    def _sample_depth(self, x_norm: float, y_norm: float) -> Optional[float]:
        with self._depth_lock:
            d = self._depth_frame
        if d is None:
            return None
        h, w = d.shape
        # Os landmarks estão em coordenadas espelhadas (imagem exibida), mas o
        # frame de profundidade vem na orientação original do sensor — sem
        # desfazer o espelho aqui, a leitura caía no ponto simétrico do corpo.
        px = max(2, min(w-3, int((1.0 - x_norm) * w)))
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
                # Baixa pra um arquivo temporário no mesmo diretório e só
                # promove pro nome final com os.replace() (atômico) se o
                # download terminar OK — sem isso, uma queda de rede no
                # meio do download deixava um .task corrompido que
                # "existe" (os.path.exists vira True) e trava
                # permanentemente no fallback da API antiga em toda
                # sessão seguinte, até alguém apagar o arquivo manualmente.
                fd, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(model_path), suffix=".task.part"
                )
                os.close(fd)
                try:
                    urllib.request.urlretrieve(url, tmp_path)
                    os.replace(tmp_path, model_path)
                except Exception:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise
                self._log("Modelo baixado.", True)

            self._model_path = model_path
            self._mp_python  = mp_python
            self._mp_vision  = mp_vision
            self._pose = self._build_landmarker()
            self._api  = "new"
            self._mp   = mp
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

    def _build_landmarker(self):
        """Cria o PoseLandmarker (Tasks API) em modo VIDEO."""
        mp_vision = self._mp_vision
        opts = mp_vision.PoseLandmarkerOptions(
            base_options=self._mp_python.BaseOptions(model_asset_path=self._model_path),
            running_mode=mp_vision.RunningMode.VIDEO,
            output_segmentation_masks=False,
            num_poses=1,
            min_pose_detection_confidence=self.min_det_conf,
            min_pose_presence_confidence=self.min_det_conf,
            min_tracking_confidence=self.min_trk_conf,
        )
        self._last_ts_ms = -1
        return mp_vision.PoseLandmarker.create_from_options(opts)

    def _rebuild_landmarker(self):
        """Descarta o rastreamento atual e recomeça a detecção do zero
        (só a thread de captura chama isto)."""
        try:
            if self._pose:
                self._pose.close()
        except Exception:
            pass
        self._pose = self._build_landmarker()

    # ── Loop principal ────────────────────────────────────────────────────

    def _open_capture(self, cv2, attempts: int = 5, delay: float = 1.5):
        """
        Kinect v1 no Windows costuma sumir e reaparecer por um instante na
        enumeração USB mesmo com o driver certo instalado (energia/porta
        USB no limite). Uma única tentativa de abrir às vezes pega o
        dispositivo bem no meio desse ciclo e falha à toa — tenta de novo
        algumas vezes antes de desistir de vez.
        """
        for attempt in range(1, attempts + 1):
            cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap = cv2.VideoCapture(self.camera_index)
            if cap.isOpened():
                return cap
            if attempt < attempts:
                self._log(f"Câmera não respondeu (tentativa {attempt}/{attempts}), tentando de novo...", False)
                time.sleep(delay)
        return None

    def _read_frame(self, cap, cv2):
        if self.use_sdk_bridge:
            bgr = self._sdk_bridge.read()
            return (bgr is not None), bgr
        return cap.read()

    def _loop(self):
        import cv2

        cap = None
        if self.use_sdk_bridge:
            self._log("Iniciando bridge do Kinect SDK (NUI)...", False)
            from core.kinect_sdk_bridge import KinectSDKBridge
            bridge = KinectSDKBridge(log=self._bridge_log)
            if not bridge.start():
                self._running = False
                self._log("Bridge do Kinect SDK não iniciou.", False)
                return
            self._sdk_bridge = bridge
        else:
            self._log(f"Abrindo câmera (índice {self.camera_index})...", False)
            cap = self._open_capture(cv2)
            if cap is None:
                self._running = False
                self._log("Câmera não encontrada.", False)
                return

            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            # Buffer de 1 quadro: sem isso o driver acumula quadros velhos
            # enquanto o MediaPipe processa e o boneco reflete o passado.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self._cap = cap

        self._log("Câmera aberta. Carregando MediaPipe...", True)

        self._start_depth()

        if not self._load_mediapipe():
            self._running = False
            # Sem isso, a ponte do Kinect (processo nativo) ficava rodando e
            # segurando o sensor até o programa fechar.
            self._release()
            return

        delay        = 1.0 / max(1.0, self.rate_hz)
        fail_t0      = None
        err_cnt      = 0
        # Rastreia se já avisamos "câmera parou" pro dashboard, pra saber
        # quando vale a pena avisar de volta que ela voltou — sem isso, o
        # aviso de erro ficava preso na tela pra sempre mesmo depois da
        # câmera voltar a enviar frames normalmente.
        camera_lost  = False
        # O Kinect leva ~1-2 s depois de aberto até entregar o PRIMEIRO
        # quadro — isso não é "câmera parou": só vale a tolerância curta
        # (1.5 s) depois de já ter recebido pelo menos um.
        got_first    = False

        while self._running:
            loop_t0 = time.monotonic()
            try:
                ok, bgr = self._read_frame(cap, cv2)
                if not ok:
                    # Limite por TEMPO (1.5s sem frame novo), não por nº de
                    # tentativas: com a captura a 30 Hz o loop tenta bem mais
                    # vezes por segundo (espera só 10ms entre tentativas).
                    if fail_t0 is None:
                        fail_t0 = time.monotonic()
                    if time.monotonic() - fail_t0 > (1.5 if got_first else 10.0):
                        self._log("Câmera parou de enviar frames.", True)
                        camera_lost = True
                        fail_t0 = time.monotonic()
                        # Notifica a sessão mesmo sem frame novo da câmera, para
                        # que o watchdog de enquadramento perceba a perda.
                        if self.on_frame:
                            self.on_frame(SkeletonFrame(detected=False, timestamp=time.time()))
                    # Sem quadro novo ainda (a ponte só devolve quando chega
                    # um): espera curta — 10 ms somava até 10 ms de atraso
                    # extra em cada quadro.
                    time.sleep(0.003)
                    continue

                fail_t0 = None
                got_first = True
                if camera_lost:
                    self._log("Câmera voltou a enviar frames.", True)
                    camera_lost = False
                # Detecta na imagem ORIGINAL (sem espelhar): o MediaPipe rotula
                # esquerda/direita pela anatomia de uma pessoa vista de frente
                # numa imagem normal — numa imagem já espelhada ele troca os
                # lados (medido: braço esquerdo real saía como "right_*"),
                # o que invertia o boneco 3D e as falas "joelho esquerdo/
                # direito". Os landmarks saem já com x espelhado (1-x) pra
                # casar com a imagem exibida (modo selfie); o desenho do
                # esqueleto é feito na imagem original e só depois espelhado.
                if self._api == "new":
                    if self._pose_reset_req:
                        self._pose_reset_req = False
                        self._rebuild_landmarker()
                    skel = self._process_new(bgr)
                else:
                    skel = self._process_old(bgr)

                bgr = cv2.flip(bgr, 1)
                skel.image_aspect = bgr.shape[1] / max(1, bgr.shape[0])
                # Brilho médio numa miniatura 64x48 (custo desprezível).
                skel.brightness = float(
                    cv2.resize(bgr, (64, 48), interpolation=cv2.INTER_AREA).mean())
                skel.image_b64 = self._encode(bgr)

                if self.on_frame:
                    self.on_frame(skel)

                err_cnt = 0

            except Exception as e:
                # Um frame ruim isolado (ex.: falha pontual do MediaPipe/cv2)
                # não pode derrubar a thread de captura em silêncio — sem
                # isso, um usuário cego perde o áudio de correção sem
                # nenhum aviso de que a câmera parou de funcionar. Avisa a
                # sessão (detected=False, o watchdog de enquadramento já
                # sabe lidar com isso) e tenta continuar; só desiste depois
                # de erros consecutivos demais.
                err_cnt += 1
                self._log(f"Erro ao processar frame ({e}).", True)
                if self.on_frame:
                    self.on_frame(SkeletonFrame(detected=False, timestamp=time.time()))
                if err_cnt > 30:
                    self._log("Erros consecutivos demais — encerrando captura.", False)
                    break
                time.sleep(0.05)
                continue

            # Dorme só o que SOBRA do período: antes dormia `delay` inteiro
            # depois de processar, então a taxa real ficava bem abaixo da
            # pedida (MediaPipe + JPEG somavam ~80-150ms além dos 100ms de
            # sleep, ~4-5 Hz efetivos) — o boneco parecia lento/suavizado.
            # Descontar 4 ms deixa o próximo quadro ser pego assim que
            # chegar (polling curto acima) em vez de cair no meio de um
            # sono cheio — o atraso médio até o quadro fica em ~2 ms.
            time.sleep(max(0.0, delay - (time.monotonic() - loop_t0) - 0.004))

        self._running = False
        self._release()

    # ── Processamento — nova API (Tasks) ──────────────────────────────────

    def _process_new(self, bgr) -> SkeletonFrame:
        import cv2
        import mediapipe as mp

        skel = SkeletonFrame(detected=False, timestamp=time.time())
        rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        # Timestamp estritamente crescente (exigência do modo VIDEO).
        ts_ms = max(int(time.monotonic() * 1000), self._last_ts_ms + 1)
        self._last_ts_ms = ts_ms
        result   = self._pose.detect_for_video(mp_image, ts_ms)

        if not result.pose_landmarks or not result.pose_landmarks[0]:
            return skel

        lms    = result.pose_landmarks[0]
        points = {}

        for i, lm in enumerate(lms):
            # x espelhado (1-x): coordenadas no espaço da imagem exibida.
            # Confiança já descontada por estar fora do quadro (ver EDGE_FADE).
            x = 1.0 - lm.x
            points[i] = Point3D(x=x, y=lm.y, z=lm.z,
                                visibility=_edge_gated(x, lm.y, lm.presence))

        # Desenha esqueleto no BGR — manualmente com cv2 (Tasks API não tem
        # drawing_utils nativo). Só o que o sistema realmente usa: pontos
        # abaixo de MIN_VIS (fantasma de borda) ficam de fora do desenho.
        self._draw_skeleton_manual(bgr, lms, (0, 255, 140), points)

        # Enriquece ANTES de suavizar: assim a profundidade real também passa
        # pelo filtro de Z (antes entrava crua, frame a frame, depois do
        # filtro — e oscilava/pulava).
        self._enrich_depth(points)
        if self._smoother:
            points = self._smoother.smooth(points, skel.timestamp)

        # raw_landmarks alimenta o boneco 3D do dashboard — precisa ser
        # montado a partir de `points` JÁ suavizado/enriquecido, não dos
        # landmarks brutos do MediaPipe. O One Euro Filter existe
        # justamente para reduzir o jitter de profundidade/posição; se o
        # boneco 3D lê os brutos, ele treme (principalmente nos braços,
        # que têm mais graus de liberdade e mais ruído de Z) mesmo com a
        # análise de exercício já suavizada.
        raw = [
            {
                "name": _LM_NAMES[i] if i < len(_LM_NAMES) else str(i),
                "x": round(p.x, 4), "y": round(p.y, 4),
                "z": round(p.z, 4), "visibility": round(p.visibility, 3),
                "rz": p.real_z,
            }
            for i, p in points.items()
        ]

        skel.points        = points
        skel.metrics       = compute_metrics(points)
        skel.detected      = True
        skel.raw_landmarks = raw
        return skel

    def _draw_skeleton_manual(self, bgr, landmarks, color, points=None):
        """Desenha o esqueleto manualmente para a nova API Tasks.

        `points` (opcional, já com a confiança descontada por borda): pontos
        com visibilidade < MIN_VIS não são desenhados, nem os ossos que
        dependem deles — o overlay mostra o que a análise e o boneco 3D de
        fato usam, não a extrapolação do modelo pra fora da imagem.
        """
        import cv2
        h, w = bgr.shape[:2]
        BONES = [
            (11,12),(11,23),(12,24),(23,24),
            (11,13),(13,15),(12,14),(14,16),
            (23,25),(25,27),(24,26),(26,28),
            (27,29),(28,30),(29,31),(30,32),
        ]
        pts = [(int(lm.x*w), int(lm.y*h)) for lm in landmarks]

        def shown(i):
            return points is None or (i in points and points[i].visibility >= MIN_VIS)

        for a, b in BONES:
            if a < len(pts) and b < len(pts) and shown(a) and shown(b):
                cv2.line(bgr, pts[a], pts[b], color, 2, cv2.LINE_AA)
        for i, (x, y) in enumerate(pts):
            if not shown(i):
                continue
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

        for i, lm in enumerate(lms):
            x = 1.0 - lm.x
            points[i] = Point3D(x=x, y=lm.y, z=lm.z,
                                visibility=_edge_gated(x, lm.y, lm.visibility))

        # Desenha esqueleto com utilitários nativos
        self._drawing.draw_landmarks(
            bgr,
            result.pose_landmarks,
            self._pose_api.POSE_CONNECTIONS,
            landmark_drawing_spec=self._styles.get_default_pose_landmarks_style(),
        )

        # Enriquece ANTES de suavizar: assim a profundidade real também passa
        # pelo filtro de Z (antes entrava crua, frame a frame, depois do
        # filtro — e oscilava/pulava).
        self._enrich_depth(points)
        if self._smoother:
            points = self._smoother.smooth(points, skel.timestamp)

        # Mesmo motivo do _process_new: raw_landmarks (boneco 3D) precisa
        # vir dos pontos já suavizados, não dos brutos do MediaPipe.
        raw = [
            {
                "name": _LM_NAMES[i] if i < len(_LM_NAMES) else str(i),
                "x": round(p.x, 4), "y": round(p.y, 4),
                "z": round(p.z, 4), "visibility": round(p.visibility, 3),
                "rz": p.real_z,
            }
            for i, p in points.items()
        ]

        skel.points        = points
        skel.metrics       = compute_metrics(points)
        skel.detected      = True
        skel.raw_landmarks = raw
        return skel

    # ── Utilidades ────────────────────────────────────────────────────────

    def _enrich_depth(self, points: Dict[int, Point3D]):
        """
        Substitui Z estimado pelo MediaPipe por profundidade real do
        Kinect, para quadril/joelho/tornozelo — relativizada à
        profundidade do quadril, não em metros absolutos.

        Por quê: profundidade absoluta (metros da câmera) e o Z do
        MediaPipe (fração normalizada da imagem, pequena, ~centrada no
        quadril) são escalas completamente diferentes. Por isso mede a
        profundidade do quadril primeiro como referência; sem leitura
        confiável dele, desiste de tudo e mantém o Z do MediaPipe no
        corpo inteiro (consistente, ainda que impreciso).

        E mais: mesmo com a referência do quadril em mãos, aplicar a
        profundidade real só nos pontos individuais que conseguirem
        leitura naquele frame (joelho às vezes falha — roupa/ângulo
        refletem mal o infravermelho — enquanto quadril/tornozelo não)
        faz esse ponto ficar pulando de escala a cada frame (ora
        profundidade real, ora estimativa do MediaPipe), o que parece o
        joelho "indo pro lugar errado" — pior que nunca enriquecer. Por
        isso a decisão é por PERNA INTEIRA: só substitui quadril+joelho+
        tornozelo de um lado se os 3 tiverem leitura válida nesse mesmo
        frame; senão a perna toda fica com a estimativa do MediaPipe
        (internamente consistente) até um frame em que os 3 lerem bem de
        novo.
        """
        if not self.use_depth:
            return

        def sample(idx):
            p = points.get(idx)
            if p and p.visible:
                return p, self._sample_depth(p.x, p.y)
            return p, None

        hip_samples = [d for _, d in (sample(L_HIP), sample(R_HIP)) if d]
        if not hip_samples:
            return
        hip_depth_mm = sum(hip_samples) / len(hip_samples)

        for hip_idx, knee_idx, ankle_idx in ((L_HIP, L_KNEE, L_ANKLE), (R_HIP, R_KNEE, R_ANKLE)):
            leg = [(hip_idx,) + sample(hip_idx), (knee_idx,) + sample(knee_idx), (ankle_idx,) + sample(ankle_idx)]
            if all(d for _, _, d in leg):
                for idx, p, d in leg:
                    points[idx] = Point3D(
                        x=p.x, y=p.y,
                        # Convenção do MediaPipe: z MAIOR = mais LONGE da
                        # câmera. (d - hip) > 0 quando o ponto está atrás do
                        # quadril, então o sinal é positivo — o anterior
                        # (negativo) era o oposto do Z que o resto do
                        # pipeline/boneco 3D espera, e invertia joelho/
                        # tornozelo pra frente/trás nos frames em que a
                        # profundidade real entrava.
                        z=(d - hip_depth_mm) / 1000.0,
                        visibility=p.visibility,
                        real_z=True,
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
