"""
core/skeleton.py
Estruturas de dados do esqueleto e métricas biomecânicas calculadas
a partir dos landmarks do MediaPipe Pose.

MediaPipe retorna 33 landmarks, cada um com (x, y, z, visibility):
  x, y: coordenadas normalizadas [0,1] na imagem
  z: profundidade relativa (negativo = mais perto da câmera)
  visibility: confiança de detecção [0,1]

Índices dos landmarks relevantes para o SeeMove:
  0=nose  11=left_shoulder  12=right_shoulder
  13=left_elbow  14=right_elbow  15=left_wrist  16=right_wrist
  23=left_hip  24=right_hip
  25=left_knee  26=right_knee
  27=left_ankle  28=right_ankle
  29=left_heel  30=right_heel
  31=left_foot_index  32=right_foot_index
"""

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Índices MediaPipe Pose
NOSE          = 0
L_EAR         = 7
R_EAR         = 8
L_SHOULDER    = 11
R_SHOULDER    = 12
L_ELBOW       = 13
R_ELBOW       = 14
L_WRIST       = 15
R_WRIST       = 16
L_HIP         = 23
R_HIP         = 24
L_KNEE        = 25
R_KNEE        = 26
L_ANKLE       = 27
R_ANKLE       = 28
L_HEEL        = 29
R_HEEL        = 30
L_FOOT        = 31
R_FOOT        = 32

# Mínima visibilidade para usar um landmark
MIN_VIS = 0.45


@dataclass
class Point3D:
    x: float
    y: float
    z: float
    visibility: float

    @property
    def visible(self) -> bool:
        return self.visibility >= MIN_VIS


@dataclass
class SkeletonMetrics:
    """
    Métricas biomecânicas calculadas a partir do esqueleto.
    Todas em graus, salvo indicação contrária.
    """
    # Inclinações (positivo = lado direito mais baixo na imagem = mais alto no corpo)
    shoulder_tilt: float = 0.0      # assimetria de ombros
    hip_tilt:      float = 0.0      # assimetria de quadril
    trunk_lean_x:  float = 0.0      # inclinação lateral do tronco

    # Joelhos (valgo: joelho para dentro; varo: para fora)
    knee_angle_l:   float = 180.0   # ângulo quadril–joelho–tornozelo (esq)
    knee_angle_r:   float = 180.0   # (dir) — 180° = alinhado
    knee_valgus_l:  float = 0.0     # desvio do joelho esq (+ = valgo)
    knee_valgus_r:  float = 0.0     # desvio do joelho dir

    # Quadril/tronco
    hip_flexion:    float = 0.0     # flexão do quadril (agachamento)
    torso_vertical: float = 0.0     # desvio do tronco da vertical (graus)

    # Profundidade (Kinect depth / z do MediaPipe)
    depth_l_knee:   float = 0.0     # z relativo do joelho esq
    depth_r_knee:   float = 0.0

    # Confiança geral
    confidence:     float = 0.0     # média de visibility dos landmarks visíveis


@dataclass
class SkeletonFrame:
    """Frame completo do esqueleto com landmarks e métricas pré-calculadas."""
    points:    Dict[int, Point3D] = field(default_factory=dict)
    metrics:   SkeletonMetrics   = field(default_factory=SkeletonMetrics)
    detected:  bool = False
    timestamp: float = field(default_factory=time.time)
    image_b64: Optional[str] = None   # JPEG base64 para o dashboard
    raw_landmarks: List[dict] = field(default_factory=list)  # para serialização


def get_point(frame: SkeletonFrame, idx: int) -> Optional[Point3D]:
    p = frame.points.get(idx)
    return p if (p and p.visible) else None


def _deg(rad: float) -> float:
    return math.degrees(rad)


def _angle_3pts(ax, ay, bx, by, cx, cy) -> float:
    """Ângulo ABC em graus (B = vértice)."""
    v1x, v1y = ax - bx, ay - by
    v2x, v2y = cx - bx, cy - by
    dot  = v1x * v2x + v1y * v2y
    mag  = math.sqrt((v1x**2 + v1y**2) * (v2x**2 + v2y**2))
    if mag < 1e-9:
        return 180.0
    return _deg(math.acos(max(-1.0, min(1.0, dot / mag))))


def _tilt_deg(left: Point3D, right: Point3D) -> float:
    """
    Inclinação entre dois pontos simétricos em graus.
    Positivo = ponto direito mais baixo na imagem
    (= lado direito mais ALTO no corpo — câmera Y invertida).
    """
    if not (left.visible and right.visible):
        return 0.0
    dy = right.y - left.y   # y aumenta para baixo na imagem
    dx = right.x - left.x or 1e-9
    return _deg(math.atan2(dy, abs(dx)))


def compute_metrics(points: Dict[int, Point3D]) -> SkeletonMetrics:
    """Calcula todas as métricas biomecânicas a partir dos landmarks."""
    m = SkeletonMetrics()
    g = points.get

    def gv(idx) -> Optional[Point3D]:
        p = g(idx)
        return p if (p and p.visible) else None

    ls, rs = gv(L_SHOULDER), gv(R_SHOULDER)
    lh, rh = gv(L_HIP),      gv(R_HIP)
    lk, rk = gv(L_KNEE),     gv(R_KNEE)
    la, ra = gv(L_ANKLE),    gv(R_ANKLE)
    lf, rf = gv(L_FOOT),     gv(R_FOOT)

    vis_vals = [p.visibility for p in points.values() if p.visible]
    m.confidence = round(sum(vis_vals) / len(vis_vals) * 100, 1) if vis_vals else 0.0

    # ── Inclinação de ombros ──────────────────────────────────────────────
    if ls and rs:
        m.shoulder_tilt = round(_tilt_deg(ls, rs), 2)

    # ── Inclinação de quadril ─────────────────────────────────────────────
    if lh and rh:
        m.hip_tilt = round(_tilt_deg(lh, rh), 2)

    # ── Inclinação lateral do tronco ──────────────────────────────────────
    if ls and rs and lh and rh:
        mid_sh_x  = (ls.x + rs.x) / 2
        mid_hip_x = (lh.x + rh.x) / 2
        mid_sh_y  = (ls.y + rs.y) / 2
        mid_hip_y = (lh.y + rh.y) / 2
        dy = mid_hip_y - mid_sh_y or 1e-9  # positivo = quadril abaixo dos ombros
        dx = mid_sh_x - mid_hip_x
        m.trunk_lean_x = round(_deg(math.atan2(dx, abs(dy))), 2)

    # ── Flexão do quadril ─────────────────────────────────────────────────
    if ls and rs and lh and rh and lk and rk:
        mid_sh_y  = (ls.y + rs.y) / 2
        mid_hip_y = (lh.y + rh.y) / 2
        mid_k_y   = (lk.y + rk.y) / 2
        hip_h = abs(mid_k_y - mid_hip_y) or 1e-9
        sh_h  = abs(mid_sh_y - mid_hip_y) or 1e-9
        # Quanto o tronco se dobrou em relação às pernas
        m.hip_flexion = round(_deg(math.atan2(sh_h, hip_h)), 2)

    # ── Ângulo do joelho (quadril–joelho–tornozelo) ───────────────────────
    if lh and lk and la:
        m.knee_angle_l = round(_angle_3pts(lh.x, lh.y, lk.x, lk.y, la.x, la.y), 2)
    if rh and rk and ra:
        m.knee_angle_r = round(_angle_3pts(rh.x, rh.y, rk.x, rk.y, ra.x, ra.y), 2)

    # ── Valgo/varo de joelho (desvio lateral no plano frontal) ───────────
    # Comparamos posição X do joelho vs tornozelo (e quadril)
    if lh and lk and la:
        # Linha ideal: quadril–tornozelo deve passar pelo joelho
        ideal_x = lh.x + (la.x - lh.x) * ((lk.y - lh.y) / (la.y - lh.y + 1e-9))
        dev = (lk.x - ideal_x) * 100   # em % da largura da imagem
        m.knee_valgus_l = round(dev, 2)  # positivo = joelho para dentro (valgo)
    if rh and rk and ra:
        ideal_x = rh.x + (ra.x - rh.x) * ((rk.y - rh.y) / (ra.y - rh.y + 1e-9))
        dev = (ideal_x - rk.x) * 100   # espelhado para o lado direito
        m.knee_valgus_r = round(dev, 2)

    # ── Verticalidade do tronco ───────────────────────────────────────────
    if ls and rs and lh and rh:
        sh_x  = (ls.x + rs.x) / 2
        hip_x = (lh.x + rh.x) / 2
        sh_y  = (ls.y + rs.y) / 2
        hip_y = (lh.y + rh.y) / 2
        dy = abs(hip_y - sh_y) or 1e-9
        m.torso_vertical = round(_deg(math.atan2(abs(sh_x - hip_x), dy)), 2)

    # ── Profundidade (z do MediaPipe, proxy para profundidade do Kinect) ──
    if lk:
        m.depth_l_knee = round(lk.z, 4)
    if rk:
        m.depth_r_knee = round(rk.z, 4)

    return m
