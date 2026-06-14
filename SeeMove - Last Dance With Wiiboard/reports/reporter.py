"""
reports/reporter.py
Coleta dados e gera relatórios completos em CSV, JSON e PDF (via HTML).
"""

import csv
import json
import os
import time
from dataclasses import dataclass, field
from typing import List
from core.balance_board import SensorData
from core.cog import CoGReading


@dataclass
class SessionRecord:
    timestamp: float
    tl: float; tr: float; bl: float; br: float
    total_kg: float
    cog_x: float; cog_y: float
    magnitude: float
    is_centered: bool
    stability_pct: int
    feedback: str = ""
    severity: str = "ok"


class SessionReporter:
    def __init__(self):
        self._records: List[SessionRecord] = []
        self._corrections: int = 0
        self._session_start: float = time.time()
        self._last_centered: bool = True
        self._exercise_name: str = "Exercício"

    def set_exercise(self, name: str):
        self._exercise_name = name

    def record(self, sensor_data: SensorData, cog: CoGReading,
               feedback: str = "", severity: str = "ok"):
        rec = SessionRecord(
            timestamp=sensor_data.timestamp,
            tl=round(sensor_data.top_left, 2),
            tr=round(sensor_data.top_right, 2),
            bl=round(sensor_data.bottom_left, 2),
            br=round(sensor_data.bottom_right, 2),
            total_kg=round(cog.total_kg, 2),
            cog_x=round(cog.x, 4),
            cog_y=round(cog.y, 4),
            magnitude=round(cog.magnitude, 4),
            is_centered=cog.is_centered,
            stability_pct=cog.stability_pct(),
            feedback=feedback,
            severity=severity,
        )
        if self._last_centered and not cog.is_centered:
            self._corrections += 1
        self._last_centered = cog.is_centered
        self._records.append(rec)

    def summary(self) -> dict:
        if not self._records:
            return {"duration_str": "00:00", "duration_s": 0,
                    "centered_pct": 0.0, "corrections": 0,
                    "mean_x": 0.0, "mean_y": 0.0,
                    "mean_stability_pct": 0.0, "total_readings": 0}
        n = len(self._records)
        elapsed = int(time.time() - self._session_start)
        m, s = divmod(elapsed, 60)
        centered = sum(1 for r in self._records if r.is_centered)
        mean_x = sum(r.cog_x for r in self._records) / n
        mean_y = sum(r.cog_y for r in self._records) / n
        mean_stab = sum(r.stability_pct for r in self._records) / n
        return {
            "duration_str": f"{m:02d}:{s:02d}",
            "duration_s": elapsed,
            "total_readings": n,
            "centered_pct": round(centered / n * 100, 1),
            "corrections": self._corrections,
            "mean_x": round(mean_x, 4),
            "mean_y": round(mean_y, 4),
            "mean_stability_pct": round(mean_stab, 1),
        }

    def save_csv(self, filepath: str):
        fieldnames = ["timestamp","tl_kg","tr_kg","bl_kg","br_kg","total_kg",
                      "cog_x","cog_y","magnitude","is_centered","stability_pct",
                      "feedback","severity"]
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in self._records:
                w.writerow({
                    "timestamp": r.timestamp, "tl_kg": r.tl, "tr_kg": r.tr,
                    "bl_kg": r.bl, "br_kg": r.br, "total_kg": r.total_kg,
                    "cog_x": r.cog_x, "cog_y": r.cog_y, "magnitude": r.magnitude,
                    "is_centered": int(r.is_centered), "stability_pct": r.stability_pct,
                    "feedback": r.feedback, "severity": r.severity,
                })
        print(f"[relatório] CSV salvo: {filepath}")

    def save_json(self, filepath: str):
        data = {"summary": self.summary(), "exercise": self._exercise_name,
                "records": [{"t": r.timestamp, "x": r.cog_x, "y": r.cog_y,
                              "mag": r.magnitude, "stab": r.stability_pct,
                              "ok": r.is_centered} for r in self._records]}
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[relatório] JSON salvo: {filepath}")

    def generate_html_report(self) -> str:
        """Gera relatório HTML completo pronto para salvar/imprimir."""
        s = self.summary()
        now_str = time.strftime("%d/%m/%Y %H:%M")

        # Dados para mini-gráfico SVG de estabilidade ao longo do tempo
        stab_points = []
        step = max(1, len(self._records) // 80)
        for i, r in enumerate(self._records[::step]):
            x = round(i / max(len(self._records[::step]) - 1, 1) * 300, 1)
            y = round((100 - r.stability_pct) * 0.6, 1)
            stab_points.append(f"{x},{y}")
        polyline = " ".join(stab_points) if stab_points else "0,60 300,60"

        # Distribuição de severidade
        total = len(self._records) or 1
        ok_pct = round(sum(1 for r in self._records if r.severity == "ok") / total * 100, 1)
        warn_pct = round(sum(1 for r in self._records if r.severity == "warn") / total * 100, 1)
        err_pct = round(100 - ok_pct - warn_pct, 1)

        return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>SeeMove — Relatório de Sessão</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f5f7fa; color: #1a1d2e; padding: 32px; }}
  .page {{ max-width: 820px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 2px 20px rgba(0,0,0,.08); overflow: hidden; }}
  .header {{ background: #1D9E75; color: #fff; padding: 28px 32px; }}
  .header h1 {{ font-size: 24px; font-weight: 700; }}
  .header p {{ font-size: 13px; opacity: .85; margin-top: 4px; }}
  .body {{ padding: 28px 32px; }}
  h2 {{ font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: #555; margin: 24px 0 12px; border-bottom: 1px solid #eee; padding-bottom: 6px; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 8px; }}
  .metric {{ background: #f5f7fa; border-radius: 8px; padding: 14px; }}
  .metric-label {{ font-size: 11px; color: #888; margin-bottom: 6px; }}
  .metric-val {{ font-size: 26px; font-weight: 700; color: #1D9E75; }}
  .metric-sub {{ font-size: 11px; color: #aaa; margin-top: 3px; }}
  .chart-wrap {{ background: #f5f7fa; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .bar-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 8px; font-size: 13px; }}
  .bar-label {{ width: 80px; color: #555; }}
  .bar-track {{ flex: 1; height: 14px; background: #e8eaf0; border-radius: 7px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 7px; }}
  .bar-val {{ width: 48px; text-align: right; font-weight: 600; }}
  .ok-fill {{ background: #1D9E75; }}
  .warn-fill {{ background: #f5a623; }}
  .err-fill {{ background: #ff5c5c; }}
  .footer {{ background: #f5f7fa; padding: 16px 32px; font-size: 12px; color: #aaa; display: flex; justify-content: space-between; }}
  polyline {{ fill: none; stroke: #1D9E75; stroke-width: 2; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  .info-box {{ background: #f5f7fa; border-radius: 8px; padding: 14px; font-size: 13px; }}
  .info-row {{ display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #eee; }}
  .info-row:last-child {{ border: none; }}
  .info-key {{ color: #666; }}
  .info-val {{ font-weight: 600; }}
  @media print {{ body {{ padding: 0; background: #fff; }} .page {{ box-shadow: none; border-radius: 0; }} }}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <h1>⚖ SeeMove — Relatório de Sessão</h1>
    <p>{self._exercise_name} &nbsp;·&nbsp; Gerado em {now_str}</p>
  </div>
  <div class="body">

    <h2>Resumo</h2>
    <div class="grid">
      <div class="metric">
        <div class="metric-label">Duração</div>
        <div class="metric-val">{s['duration_str']}</div>
        <div class="metric-sub">minutos:segundos</div>
      </div>
      <div class="metric">
        <div class="metric-label">Tempo centralizado</div>
        <div class="metric-val">{s['centered_pct']}%</div>
        <div class="metric-sub">dentro do limiar ideal</div>
      </div>
      <div class="metric">
        <div class="metric-label">Estabilidade média</div>
        <div class="metric-val">{s['mean_stability_pct']}%</div>
        <div class="metric-sub">0% = máximo desvio</div>
      </div>
    </div>
    <div class="grid" style="margin-top:14px">
      <div class="metric">
        <div class="metric-label">Correções emitidas</div>
        <div class="metric-val" style="color:#f5a623">{s['corrections']}</div>
        <div class="metric-sub">eventos de desvio</div>
      </div>
      <div class="metric">
        <div class="metric-label">Desvio médio X</div>
        <div class="metric-val" style="font-size:20px;color:#4a9eff">{s['mean_x']:+.3f}</div>
        <div class="metric-sub">esquerda ↔ direita</div>
      </div>
      <div class="metric">
        <div class="metric-label">Desvio médio Y</div>
        <div class="metric-val" style="font-size:20px;color:#4a9eff">{s['mean_y']:+.3f}</div>
        <div class="metric-sub">trás ↔ frente</div>
      </div>
    </div>

    <h2>Estabilidade ao longo do tempo</h2>
    <div class="chart-wrap">
      <svg width="100%" viewBox="0 0 300 60" preserveAspectRatio="none" style="height:80px">
        <line x1="0" y1="30" x2="300" y2="30" stroke="#e0e0e0" stroke-width="1"/>
        <polyline points="{polyline}"/>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:#aaa;margin-top:4px">
        <span>Início</span><span>← tempo →</span><span>Fim</span>
      </div>
    </div>

    <h2>Distribuição de qualidade postural</h2>
    <div class="chart-wrap">
      <div class="bar-row">
        <span class="bar-label">Correto</span>
        <div class="bar-track"><div class="bar-fill ok-fill" style="width:{ok_pct}%"></div></div>
        <span class="bar-val" style="color:#1D9E75">{ok_pct}%</span>
      </div>
      <div class="bar-row">
        <span class="bar-label">Atenção</span>
        <div class="bar-track"><div class="bar-fill warn-fill" style="width:{warn_pct}%"></div></div>
        <span class="bar-val" style="color:#f5a623">{warn_pct}%</span>
      </div>
      <div class="bar-row">
        <span class="bar-label">Crítico</span>
        <div class="bar-track"><div class="bar-fill err-fill" style="width:{err_pct}%"></div></div>
        <span class="bar-val" style="color:#ff5c5c">{err_pct}%</span>
      </div>
    </div>

    <h2>Informações técnicas</h2>
    <div class="info-box">
      <div class="info-row"><span class="info-key">Total de leituras</span><span class="info-val">{s['total_readings']}</span></div>
      <div class="info-row"><span class="info-key">Exercício</span><span class="info-val">{self._exercise_name}</span></div>
      <div class="info-row"><span class="info-key">Desvio máximo X</span><span class="info-val">{max((abs(r.cog_x) for r in self._records), default=0):.3f}</span></div>
      <div class="info-row"><span class="info-key">Desvio máximo Y</span><span class="info-val">{max((abs(r.cog_y) for r in self._records), default=0):.3f}</span></div>
      <div class="info-row"><span class="info-key">Magnitude máxima</span><span class="info-val">{max((r.magnitude for r in self._records), default=0):.3f}</span></div>
    </div>

  </div>
  <div class="footer">
    <span>SeeMove — Sistema de monitoramento postural</span>
    <span>{now_str}</span>
  </div>
</div>
</body>
</html>"""
