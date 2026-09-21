"""
reports/reporter.py
Coleta dados de sessão baseados no esqueleto (Kinect + MediaPipe).
Sem dados de sensores de pressão.
"""

import csv
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque

from core.skeleton import SkeletonFrame
from exercises.base import FeedbackResult, Severity
from reports._brand import LOGO_ICON, LOGO_WORDMARK


@dataclass
class SkeletonRecord:
    """Um registro de frame dentro da sessão."""
    timestamp:      float
    detected:       bool
    confidence:     float
    shoulder_tilt:  float
    hip_tilt:       float
    trunk_lean_x:   float
    knee_valgus_l:  float
    knee_valgus_r:  float
    knee_angle_l:   float
    knee_angle_r:   float
    torso_vertical: float
    feedback:       str
    severity:       str


class SessionReporter:
    # Máximo de frames retidos em memória para exportação (CSV/JSON/HTML).
    # ~5.5h contínuas a 10 Hz — generoso pro uso normal, e evita que uma
    # sessão esquecida rodando o dia inteiro cresça sem limite. Estatísticas
    # da sessão (ok_pct, correções, médias) NÃO dependem desse cap — usam
    # acumuladores incrementais à parte (self._total_n, self._sum_*, etc.)
    # que cobrem a sessão inteira; só a exportação por frame fica limitada
    # à janela mais recente quando o cap é atingido.
    RECORDS_MAX = 200_000

    def __init__(self):
        self._records: Deque[SkeletonRecord] = deque(maxlen=self.RECORDS_MAX)
        # record_skeleton() escreve nessa deque ~10x/s pela thread de
        # análise, enquanto as rotas de relatório (thread do Flask) leem e
        # iteram a mesma deque — sem lock, um clique em "baixar relatório"
        # durante uma sessão ativa pode estourar "deque mutated during
        # iteration" ou gerar um relatório inconsistente.
        self._records_lock            = threading.Lock()
        self._corrections:    int   = 0
        self._session_start:  float = time.time()
        self._last_ok:        bool  = True
        self._exercise_name:  str   = "Exercício"

        # Acumuladores para estatísticas incrementais — cobrem a sessão
        # inteira mesmo depois que self._records começa a descartar frames
        # antigos (deque com maxlen).
        self._total_n:      int   = 0
        self._sum_conf:     float = 0.0
        self._sum_sh:       float = 0.0
        self._sum_hip:      float = 0.0
        self._sum_trunk:    float = 0.0
        self._sum_vl:       float = 0.0
        self._sum_vr:       float = 0.0
        self._detected_n:   int   = 0
        self._ok_n:         int   = 0

    def set_exercise(self, name: str):
        self._exercise_name = name

    def record_skeleton(self, frame: SkeletonFrame,
                        result: FeedbackResult) -> dict:
        """
        Registra um frame e retorna o summary atualizado.
        Chamado pela Session a cada frame analisado.
        """
        m = frame.metrics
        is_ok = result.severity == Severity.OK
        # A duração conta a partir do primeiro quadro realmente monitorado,
        # não desde que o processo abriu (o dashboard fica horas aberto
        # antes de alguém apertar Iniciar).
        if self._total_n == 0:
            self._session_start = time.time()

        rec = SkeletonRecord(
            timestamp      = frame.timestamp,
            detected       = frame.detected,
            confidence     = m.confidence,
            shoulder_tilt  = m.shoulder_tilt,
            hip_tilt       = m.hip_tilt,
            trunk_lean_x   = m.trunk_lean_x,
            knee_valgus_l  = m.knee_valgus_l,
            knee_valgus_r  = m.knee_valgus_r,
            knee_angle_l   = m.knee_angle_l,
            knee_angle_r   = m.knee_angle_r,
            torso_vertical = m.torso_vertical,
            feedback       = result.message,
            severity       = result.severity.value,
        )
        with self._records_lock:
            self._records.append(rec)
        self._total_n += 1

        # Contadores incrementais
        if frame.detected:
            self._detected_n   += 1
            self._sum_conf     += m.confidence
            self._sum_sh       += abs(m.shoulder_tilt)
            self._sum_hip      += abs(m.hip_tilt)
            self._sum_trunk    += abs(m.trunk_lean_x)
            # valgo = joelho pra DENTRO (positivo); pra fora não é valgo.
            self._sum_vl       += max(0.0, m.knee_valgus_l)
            self._sum_vr       += max(0.0, m.knee_valgus_r)

        if is_ok:
            self._ok_n += 1

        # Conta transições ok → desvio como "correção necessária"
        if self._last_ok and not is_ok:
            self._corrections += 1
        self._last_ok = is_ok

        return self.summary()

    def summary(self) -> dict:
        # Usa o contador de vida inteira (self._total_n), não
        # len(self._records) — o deque de records tem maxlen e descarta
        # frames antigos em sessões muito longas, mas as estatísticas
        # (ok_pct, correções, médias) continuam cobrindo a sessão inteira.
        n     = self._total_n or 1
        det   = self._detected_n or 1
        # Antes do primeiro quadro monitorado ainda não há sessão: duração zero. O relógio só começa em
        # record_skeleton(); sem isto o painel mostrava o tempo desde a abertura do programa e depois
        # "voltava" a 00:00 ao apertar Iniciar.
        elapsed = int(time.time() - self._session_start) if self._total_n else 0
        m, s    = divmod(elapsed, 60)

        return {
            "duration_str":      f"{m:02d}:{s:02d}",
            "duration_s":        elapsed,
            "total_frames":      self._total_n,
            "detected_pct":      round(self._detected_n / n * 100, 1),
            "ok_pct":            round(self._ok_n / n * 100, 1),
            "corrections":       self._corrections,
            "mean_confidence":   round(self._sum_conf  / det, 1),
            "mean_shoulder_tilt":round(self._sum_sh    / det, 2),
            "mean_hip_tilt":     round(self._sum_hip   / det, 2),
            "mean_trunk_lean":   round(self._sum_trunk  / det, 2),
            "mean_valgus_l":     round(self._sum_vl    / det, 2),
            "mean_valgus_r":     round(self._sum_vr    / det, 2),
            "exercise":          self._exercise_name,
        }

    def save_csv(self, filepath: str):
        fields = [
            "timestamp", "detected", "confidence",
            "shoulder_tilt", "hip_tilt", "trunk_lean_x",
            "knee_valgus_l", "knee_valgus_r",
            "knee_angle_l",  "knee_angle_r",
            "torso_vertical", "feedback", "severity",
        ]
        with self._records_lock:
            records = list(self._records)
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:   # BOM: o Excel lê os acentos
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in records:
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
        print(f"[relatório] CSV salvo: {filepath}")

    def save_json(self, filepath: str):
        with self._records_lock:
            records = list(self._records)
        data = {
            "summary":  self.summary(),
            "exercise": self._exercise_name,
            "records":  [
                {
                    "t":    r.timestamp,
                    "det":  r.detected,
                    "conf": r.confidence,
                    "sh":   r.shoulder_tilt,
                    "hip":  r.hip_tilt,
                    "trk":  r.trunk_lean_x,
                    "vl":   r.knee_valgus_l,
                    "vr":   r.knee_valgus_r,
                    "kal":  r.knee_angle_l,
                    "kar":  r.knee_angle_r,
                    "sev":  r.severity,
                }
                for r in records
            ],
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[relatório] JSON salvo: {filepath}")

    def generate_html_report(self) -> str:
        s       = self.summary()
        now_str = time.strftime("%d/%m/%Y %H:%M")

        with self._records_lock:
            records = list(self._records)

        # Série temporal de confiança (para gráfico SVG)
        det_records = [r for r in records if r.detected]
        step = max(1, len(det_records) // 80)
        pts  = []
        for i, r in enumerate(det_records[::step]):
            x = round(i / max(len(det_records[::step]) - 1, 1) * 300, 1)
            y = round((100 - r.confidence) * 0.6, 1)
            pts.append(f"{x},{y}")
        polyline = " ".join(pts) if pts else "0,30 300,30"

        # Distribuição de severidade
        total    = len(records) or 1
        ok_pct   = round(sum(1 for r in records if r.severity == "ok")   / total * 100, 1)
        warn_pct = round(sum(1 for r in records if r.severity == "warn")  / total * 100, 1)
        err_pct  = max(0.0, round(100 - ok_pct - warn_pct, 1))

        # Pior valgo registrado
        max_vl = max((max(0.0, r.knee_valgus_l) for r in det_records), default=0)
        max_vr = max((max(0.0, r.knee_valgus_r) for r in det_records), default=0)

        return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>GuiaMove — Relatório de Sessão</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#EEF4FA;color:#1B2733;padding:32px}}
  .page{{max-width:820px;margin:0 auto;background:#fff;border-radius:16px;
         box-shadow:0 1px 2px rgba(4,83,127,.06),0 18px 40px -20px rgba(4,83,127,.35);overflow:hidden}}
  .bar{{height:5px;background:linear-gradient(90deg,#0061AE,#1BAEEE)}}
  .hdr{{display:flex;align-items:center;justify-content:space-between;gap:20px;
        padding:24px 32px 22px;border-bottom:1px solid #DAE6F1}}
  .brand{{display:flex;align-items:center;gap:14px}}
  .brand .bi{{height:58px;width:auto;display:block}}
  .brand .bw{{height:24px;width:auto;display:block}}
  .brand .tag{{font-size:11.5px;color:#4D6074;margin-top:7px;letter-spacing:.03em}}
  .ttl{{text-align:right}}
  .ttl h1{{font-size:11.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#04537F}}
  .ttl .e{{font-size:21px;font-weight:700;color:#1B2733;margin-top:4px}}
  .ttl .d{{font-size:12.5px;color:#4D6074;margin-top:2px}}
  .body{{padding:28px 32px}}
  h2{{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
      color:#04537F;margin:24px 0 12px;border-bottom:1px solid #DAE6F1;padding-bottom:6px}}
  .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}}
  .card{{background:#F2F7FC;border:1px solid #DAE6F1;border-radius:10px;padding:14px}}
  .cl{{font-size:11px;color:#4D6074;margin-bottom:5px}}
  .cv{{font-size:24px;font-weight:700;color:#0061AE}}
  .cs{{font-size:11px;color:#5F7387;margin-top:3px}}
  .bar-row{{display:flex;align-items:center;gap:12px;margin-bottom:8px;font-size:13px}}
  .bl{{width:90px;color:#4D6074}}
  .bt{{flex:1;height:14px;background:#E3EDF7;border-radius:7px;overflow:hidden}}
  .bf{{height:100%;border-radius:7px}}
  .bv{{width:48px;text-align:right;font-weight:600}}
  .ok{{background:#0061AE}}.warn{{background:#E8912A}}.err{{background:#E5484D}}
  .chart{{background:#F2F7FC;border:1px solid #DAE6F1;border-radius:10px;padding:16px;margin-bottom:16px}}
  .info{{background:#F2F7FC;border:1px solid #DAE6F1;border-radius:10px;padding:14px;font-size:13px}}
  .ir{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #DAE6F1}}
  .ir:last-child{{border:none}}
  .ik{{color:#4D6074}}.iv{{font-weight:600}}
  .ftr{{background:#F2F7FC;border-top:1px solid #DAE6F1;padding:14px 32px;font-size:12px;color:#5F7387;
        display:flex;justify-content:space-between}}
  @media print{{*{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
                body{{padding:0;background:#fff}}.page{{box-shadow:none;border-radius:0}}}}
</style>
</head>
<body>
<div class="page">
  <div class="bar"></div>
  <div class="hdr">
    <div class="brand">
      <img class="bi" src="{LOGO_ICON}" alt="">
      <div>
        <img class="bw" src="{LOGO_WORDMARK}" alt="GuiaMove">
        <div class="tag">Monitoramento Postural</div>
      </div>
    </div>
    <div class="ttl">
      <h1>Relatório de Sessão</h1>
      <div class="e">{self._exercise_name}</div>
      <div class="d">{now_str}</div>
    </div>
  </div>
  <div class="body">

    <h2>Resumo da sessão</h2>
    <div class="grid3">
      <div class="card"><div class="cl">Duração</div>
        <div class="cv">{s['duration_str']}</div><div class="cs">minutos:segundos</div></div>
      <div class="card"><div class="cl">Postura correta</div>
        <div class="cv">{s['ok_pct']}%</div><div class="cs">do tempo</div></div>
      <div class="card"><div class="cl">Correções necessárias</div>
        <div class="cv" style="color:#B45309">{s['corrections']}</div>
        <div class="cs">eventos de desvio</div></div>
    </div>
    <div class="grid3">
      <div class="card"><div class="cl">Confiança média</div>
        <div class="cv">{s['mean_confidence']}%</div><div class="cs">detecção MediaPipe</div></div>
      <div class="card"><div class="cl">Frames detectados</div>
        <div class="cv">{s['detected_pct']}%</div><div class="cs">corpo visível</div></div>
      <div class="card"><div class="cl">Total de frames</div>
        <div class="cv" style="font-size:20px">{s['total_frames']}</div>
        <div class="cs">analisados</div></div>
    </div>

    <h2>Confiança de detecção ao longo do tempo</h2>
    <div class="chart">
      <svg width="100%" viewBox="0 0 300 60" preserveAspectRatio="none" style="height:70px">
        <line x1="0" y1="30" x2="300" y2="30" stroke="#C4D6E7" stroke-width="1"/>
        <polyline points="{polyline}" fill="none" stroke="#0061AE" stroke-width="2"/>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:#5F7387;margin-top:4px">
        <span>Início</span><span>← tempo →</span><span>Fim</span>
      </div>
    </div>

    <h2>Distribuição de qualidade postural</h2>
    <div class="chart">
      <div class="bar-row">
        <span class="bl">Correto</span>
        <div class="bt"><div class="bf ok" style="width:{ok_pct}%"></div></div>
        <span class="bv" style="color:#0061AE">{ok_pct}%</span>
      </div>
      <div class="bar-row">
        <span class="bl">Atenção</span>
        <div class="bt"><div class="bf warn" style="width:{warn_pct}%"></div></div>
        <span class="bv" style="color:#B45309">{warn_pct}%</span>
      </div>
      <div class="bar-row">
        <span class="bl">Crítico</span>
        <div class="bt"><div class="bf err" style="width:{err_pct}%"></div></div>
        <span class="bv" style="color:#C62828">{err_pct}%</span>
      </div>
    </div>

    <h2>Métricas biomecânicas médias</h2>
    <div class="grid2">
      <div class="info">
        <div class="ir"><span class="ik">Inclinação ombros (média absoluta)</span>
          <span class="iv">{s['mean_shoulder_tilt']:.2f}°</span></div>
        <div class="ir"><span class="ik">Inclinação quadril (média absoluta)</span>
          <span class="iv">{s['mean_hip_tilt']:.2f}°</span></div>
        <div class="ir"><span class="ik">Inclinação tronco (média absoluta)</span>
          <span class="iv">{s['mean_trunk_lean']:.2f}°</span></div>
      </div>
      <div class="info">
        <div class="ir"><span class="ik">Valgo joelho esq. (média)</span>
          <span class="iv">{s['mean_valgus_l']:.2f}%</span></div>
        <div class="ir"><span class="ik">Valgo joelho dir. (média)</span>
          <span class="iv">{s['mean_valgus_r']:.2f}%</span></div>
        <div class="ir"><span class="ik">Valgo máximo registrado</span>
          <span class="iv">{max(max_vl, max_vr):.2f}%</span></div>
      </div>
    </div>

  </div>
  <div class="ftr">
    <span>GuiaMove — Kinect + MediaPipe</span>
    <span>{now_str}</span>
  </div>
</div>
</body>
</html>"""
