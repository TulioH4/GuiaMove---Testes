"""
reports/reporter.py
Coleta dados de sessão baseados no esqueleto (Kinect + MediaPipe).
Sem dados de sensores de pressão.
"""

import csv
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from core.skeleton import SkeletonFrame, SkeletonMetrics
from exercises.base import FeedbackResult, Severity


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
        self._records.append(rec)
        self._total_n += 1

        # Contadores incrementais
        if frame.detected:
            self._detected_n   += 1
            self._sum_conf     += m.confidence
            self._sum_sh       += abs(m.shoulder_tilt)
            self._sum_hip      += abs(m.hip_tilt)
            self._sum_trunk    += abs(m.trunk_lean_x)
            self._sum_vl       += abs(m.knee_valgus_l)
            self._sum_vr       += abs(m.knee_valgus_r)

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
        elapsed = int(time.time() - self._session_start)
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
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in self._records:
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
                for r in self._records
            ],
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[relatório] JSON salvo: {filepath}")

    def generate_html_report(self) -> str:
        s       = self.summary()
        now_str = time.strftime("%d/%m/%Y %H:%M")

        # Série temporal de confiança (para gráfico SVG)
        det_records = [r for r in self._records if r.detected]
        step = max(1, len(det_records) // 80)
        pts  = []
        for i, r in enumerate(det_records[::step]):
            x = round(i / max(len(det_records[::step]) - 1, 1) * 300, 1)
            y = round((100 - r.confidence) * 0.6, 1)
            pts.append(f"{x},{y}")
        polyline = " ".join(pts) if pts else "0,30 300,30"

        # Distribuição de severidade
        total    = len(self._records) or 1
        ok_pct   = round(sum(1 for r in self._records if r.severity == "ok")   / total * 100, 1)
        warn_pct = round(sum(1 for r in self._records if r.severity == "warn")  / total * 100, 1)
        err_pct  = round(100 - ok_pct - warn_pct, 1)

        # Pior valgo registrado
        max_vl = max((abs(r.knee_valgus_l) for r in det_records), default=0)
        max_vr = max((abs(r.knee_valgus_r) for r in det_records), default=0)

        return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>SeeMove — Relatório de Sessão</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#f5f7fa;color:#1a1d2e;padding:32px}}
  .page{{max-width:820px;margin:0 auto;background:#fff;border-radius:12px;
         box-shadow:0 2px 20px rgba(0,0,0,.08);overflow:hidden}}
  .hdr{{background:#1D9E75;color:#fff;padding:28px 32px}}
  .hdr h1{{font-size:22px;font-weight:700}}
  .hdr p{{font-size:13px;opacity:.85;margin-top:4px}}
  .body{{padding:28px 32px}}
  h2{{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;
      color:#666;margin:24px 0 12px;border-bottom:1px solid #eee;padding-bottom:6px}}
  .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}}
  .card{{background:#f5f7fa;border-radius:8px;padding:14px}}
  .cl{{font-size:11px;color:#888;margin-bottom:5px}}
  .cv{{font-size:24px;font-weight:700;color:#1D9E75}}
  .cs{{font-size:11px;color:#aaa;margin-top:3px}}
  .bar-row{{display:flex;align-items:center;gap:12px;margin-bottom:8px;font-size:13px}}
  .bl{{width:90px;color:#555}}
  .bt{{flex:1;height:14px;background:#e8eaf0;border-radius:7px;overflow:hidden}}
  .bf{{height:100%;border-radius:7px}}
  .bv{{width:48px;text-align:right;font-weight:600}}
  .ok{{background:#1D9E75}}.warn{{background:#f5a623}}.err{{background:#ff5c5c}}
  .chart{{background:#f5f7fa;border-radius:8px;padding:16px;margin-bottom:16px}}
  .info{{background:#f5f7fa;border-radius:8px;padding:14px;font-size:13px}}
  .ir{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #eee}}
  .ir:last-child{{border:none}}
  .ik{{color:#666}}.iv{{font-weight:600}}
  .ftr{{background:#f5f7fa;padding:14px 32px;font-size:12px;color:#aaa;
        display:flex;justify-content:space-between}}
  @media print{{body{{padding:0;background:#fff}}.page{{box-shadow:none;border-radius:0}}}}
</style>
</head>
<body>
<div class="page">
  <div class="hdr">
    <h1>🦴 SeeMove — Relatório de Sessão</h1>
    <p>{self._exercise_name} &nbsp;·&nbsp; {now_str}</p>
  </div>
  <div class="body">

    <h2>Resumo da sessão</h2>
    <div class="grid3">
      <div class="card"><div class="cl">Duração</div>
        <div class="cv">{s['duration_str']}</div><div class="cs">minutos:segundos</div></div>
      <div class="card"><div class="cl">Postura correta</div>
        <div class="cv">{s['ok_pct']}%</div><div class="cs">do tempo</div></div>
      <div class="card"><div class="cl">Correções necessárias</div>
        <div class="cv" style="color:#f5a623">{s['corrections']}</div>
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
        <line x1="0" y1="30" x2="300" y2="30" stroke="#e0e0e0" stroke-width="1"/>
        <polyline points="{polyline}" fill="none" stroke="#1D9E75" stroke-width="2"/>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:#aaa;margin-top:4px">
        <span>Início</span><span>← tempo →</span><span>Fim</span>
      </div>
    </div>

    <h2>Distribuição de qualidade postural</h2>
    <div class="chart">
      <div class="bar-row">
        <span class="bl">Correto</span>
        <div class="bt"><div class="bf ok" style="width:{ok_pct}%"></div></div>
        <span class="bv" style="color:#1D9E75">{ok_pct}%</span>
      </div>
      <div class="bar-row">
        <span class="bl">Atenção</span>
        <div class="bt"><div class="bf warn" style="width:{warn_pct}%"></div></div>
        <span class="bv" style="color:#f5a623">{warn_pct}%</span>
      </div>
      <div class="bar-row">
        <span class="bl">Crítico</span>
        <div class="bt"><div class="bf err" style="width:{err_pct}%"></div></div>
        <span class="bv" style="color:#ff5c5c">{err_pct}%</span>
      </div>
    </div>

    <h2>Métricas biomecânicas médias</h2>
    <div class="grid2">
      <div class="info">
        <div class="ir"><span class="ik">Inclinação ombros (média)</span>
          <span class="iv">{s['mean_shoulder_tilt']:+.2f}°</span></div>
        <div class="ir"><span class="ik">Inclinação quadril (média)</span>
          <span class="iv">{s['mean_hip_tilt']:+.2f}°</span></div>
        <div class="ir"><span class="ik">Inclinação tronco (média)</span>
          <span class="iv">{s['mean_trunk_lean']:+.2f}°</span></div>
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
    <span>SeeMove — Kinect + MediaPipe</span>
    <span>{now_str}</span>
  </div>
</div>
</body>
</html>"""
