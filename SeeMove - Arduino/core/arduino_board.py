"""
core/arduino_board.py
Leitura da plataforma de pressao via Arduino/HX711 pela porta serial.
"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from core.pressure_platform import SensorData


ARDUINO_LINE_RE = re.compile(
    r"FL:\s*(?P<fl>-?\d+(?:\.\d+)?)\s*kg\s*\|\s*"
    r"FR:\s*(?P<fr>-?\d+(?:\.\d+)?)\s*kg\s*\|\s*"
    r"BL:\s*(?P<bl>-?\d+(?:\.\d+)?)\s*kg\s*\|\s*"
    r"BR:\s*(?P<br>-?\d+(?:\.\d+)?)\s*kg",
    re.IGNORECASE,
)


@dataclass
class ArduinoSerialConfig:
    port: str = "auto"
    baudrate: int = 9600
    timeout_s: float = 1.0


class ArduinoBoardHardware:
    """
    Driver serial para o codigo atual do Arduino.

    O Arduino envia linhas no formato:
      FL: 12.34 kg | FR: 13.20 kg | BL: 11.90 kg | BR: 12.80 kg -> TOTAL: 50.24 kg

    Este driver extrai FL/FR/BL/BR e converte para SensorData, mantendo o restante
    do sistema desacoplado do hardware especifico.
    """

    def __init__(
        self,
        port: str = "auto",
        baudrate: int = 9600,
        on_data: Optional[Callable[[SensorData], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.config = ArduinoSerialConfig(port=port, baudrate=baudrate)
        self.on_data = on_data
        self.on_status = on_status
        self._serial = None
        self._connected = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_data_at = 0.0
        self._invalid_lines = 0

    def _log(self, msg: str):
        print(f"[arduino] {msg}")
        if self.on_status:
            self.on_status(msg)

    def connect(self) -> bool:
        try:
            import serial
        except ImportError:
            self._log("Instale 'pyserial': pip install pyserial")
            return False

        if self._connected:
            self._log("Arduino ja conectado.")
            return True

        port = self._resolve_port()
        if not port:
            self._log("Nenhuma porta serial encontrada. Conecte o Arduino ou use --port COMx.")
            return False

        try:
            self._serial = serial.Serial(
                port,
                self.config.baudrate,
                timeout=self.config.timeout_s,
            )
            time.sleep(2.0)  # A maioria dos Arduinos reinicia ao abrir a serial.
            self._serial.reset_input_buffer()
        except Exception as e:
            self._log(f"Erro ao abrir {self.config.port}: {e}")
            return False

        self._running = True
        self._connected = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self._log(f"Conectado em {port} a {self.config.baudrate} bps.")
        return True

    def _resolve_port(self) -> Optional[str]:
        configured = (self.config.port or "").strip()
        if configured and configured.lower() != "auto":
            return configured

        try:
            from serial.tools import list_ports
        except ImportError:
            return None

        ports = list(list_ports.comports())
        if not ports:
            return None

        preferred_terms = ("arduino", "ch340", "ch341", "usb serial", "usb-serial")
        for port in ports:
            text = f"{port.device} {port.description} {port.manufacturer}".lower()
            if any(term in text for term in preferred_terms):
                self._log(f"Porta serial detectada automaticamente: {port.device}")
                return port.device

        self._log(f"Usando primeira porta serial encontrada: {ports[0].device}")
        return ports[0].device

    def _read_loop(self):
        while self._running:
            try:
                raw_line = self._serial.readline()
            except Exception as e:
                self._log(f"Erro lendo serial: {e}")
                time.sleep(0.2)
                continue

            if not raw_line:
                continue

            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            data = self.parse_line(line)
            if data is None:
                self._handle_non_data_line(line)
                continue

            self._last_data_at = time.time()
            if self.on_data:
                self.on_data(data)

    def _handle_non_data_line(self, line: str):
        # Repassa mensagens importantes do setup do Arduino para a UI/terminal.
        lowered = line.lower()
        if any(token in lowered for token in ("iniciando", "tara", "pronto", "calibracao")):
            self._log(line)
            return

        self._invalid_lines += 1
        if self._invalid_lines in (1, 10, 50):
            self._log(f"Linha serial ignorada: {line}")

    @staticmethod
    def parse_line(line: str) -> Optional[SensorData]:
        match = ARDUINO_LINE_RE.search(line)
        if not match:
            return None

        fl = float(match.group("fl"))
        fr = float(match.group("fr"))
        bl = float(match.group("bl"))
        br = float(match.group("br"))

        return SensorData(
            top_left=max(0.0, fl),
            top_right=max(0.0, fr),
            bottom_left=max(0.0, bl),
            bottom_right=max(0.0, br),
        )

    def disconnect(self):
        self._running = False
        self._connected = False

        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

        self._log("Desconectado.")

    def read(self) -> Optional[SensorData]:
        return None


def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [port.device for port in list_ports.comports()]
