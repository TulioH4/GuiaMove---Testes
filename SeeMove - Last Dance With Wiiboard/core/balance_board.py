"""
core/balance_board.py
Wii Balance Board — conexão via HID nativo do Windows + simulador.
"""

import struct
import time
import math
import random
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable


# IDs do hardware Nintendo Balance Board
NINTENDO_VID  = 0x057E
BALANCE_PID   = 0x0306


@dataclass
class SensorData:
    top_left:     float
    top_right:    float
    bottom_left:  float
    bottom_right: float
    timestamp: float = field(default_factory=time.time)

    def total(self) -> float:
        return self.top_left + self.top_right + self.bottom_left + self.bottom_right


class CalibrationTable:
    SENSORS = ["top_right", "bottom_right", "top_left", "bottom_left"]

    def __init__(self, raw_bytes: Optional[bytes] = None):
        self.points = {s: [0, 1700, 3400] for s in self.SENSORS}
        if raw_bytes and len(raw_bytes) >= 24:
            self._parse(raw_bytes)

    def _parse(self, data: bytes):
        for i, sensor in enumerate(self.SENSORS):
            self.points[sensor] = [
                struct.unpack_from(">H", data, i * 2)[0],
                struct.unpack_from(">H", data, 8  + i * 2)[0],
                struct.unpack_from(">H", data, 16 + i * 2)[0],
            ]

    def raw_to_kg(self, sensor: str, raw: int) -> float:
        cal = self.points.get(sensor, [0, 1700, 3400])
        if raw < cal[1]:
            low, high, ref_low, ref_high = cal[0], cal[1], 0.0, 17.0
        else:
            low, high, ref_low, ref_high = cal[1], cal[2], 17.0, 34.0
        span = high - low
        if span == 0:
            return ref_low
        return ref_low + (raw - low) / span * (ref_high - ref_low)


class BalanceBoardHardware:
    HID_SET_REPORT = 0x52
    RPT_WRITE_MEM  = 0x16
    RPT_READ_MEM   = 0x17
    RPT_DATA_MODE  = 0x12

    REG_EXT_INIT1   = 0xA400F0
    REG_EXT_INIT2   = 0xA400FB
    REG_CALIBRATION = 0xA40024

    def __init__(self,
                 on_data:   Optional[Callable[[SensorData], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None):
        self.on_data   = on_data
        self.on_status = on_status
        self.calibration = CalibrationTable()
        self._dev = None
        self._connected = False
        self._running   = False
        self._thread: Optional[threading.Thread] = None
        self._pending_cal: Optional[bytes] = None
        self._cal_event = threading.Event()
        self._packet_count = 0
        self._data_count = 0
        self._unknown_count = 0
        self._last_packet_log = 0.0
        
        # --- Variáveis da Tara ---
        self._tare_samples = []
        self._tare_values = {'top_left': 0, 'top_right': 0, 'bottom_left': 0, 'bottom_right': 0}
        self._tared = False
        self._startup_discard = 30  # NOVO: Vai jogar fora as 30 primeiras leituras
        
        # --- O Amortecedor Matemático ---
        self._last_stable_sd = None

    def _log(self, msg: str):
        print(f"[hardware] {msg}")
        if self.on_status:
            self.on_status(msg)

    def _find_device(self):
        try:
            import hid
        except ImportError:
            raise RuntimeError("Execute: pip install hid")
        devices = hid.enumerate(NINTENDO_VID, BALANCE_PID)
        if not devices:
            all_devs = hid.enumerate()
            nintendo = [d for d in all_devs if d["vendor_id"] == NINTENDO_VID]
            if nintendo:
                self._log(f"Nintendo encontrado; tentando abrir primeiro dispositivo: {nintendo[0]}")
                devices = nintendo
            else:
                return None
        return devices[0]["path"]

    def connect(self) -> bool:
        try:
            import hid
        except ImportError:
            self._log("Instale 'hid': pip install hid")
            return False

        self._log("Procurando Balance Board nos dispositivos HID...")
        path = self._find_device()
        if not path:
            self._log("Balance Board não encontrada.")
            return False

        try:
            self._dev = hid.device()
            self._dev.open_path(path)
            self._dev.set_nonblocking(False)
            self._log(f"Dispositivo aberto: {self._dev.get_manufacturer_string()} {self._dev.get_product_string()}")
        except Exception as e:
            self._log(f"Erro ao abrir dispositivo HID: {e}")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        time.sleep(0.05)

        self._write_reg(self.REG_EXT_INIT1, b"\x55")
        time.sleep(0.1)
        self._write_reg(self.REG_EXT_INIT2, b"\x00")
        time.sleep(0.1)

        self._send([self.HID_SET_REPORT, 0x11, 0x10])
        time.sleep(0.05)
        self._send([self.HID_SET_REPORT, 0x15, 0x00])
        time.sleep(0.05)

        self._log("Lendo calibração dos sensores...")
        self._pending_cal = None
        self._cal_event.clear()
        self._request_calibration()
        self._cal_event.wait(timeout=3.0)
        
        if self._pending_cal:
            self.calibration = CalibrationTable(self._pending_cal)
            self._log("Calibração carregada.")
        else:
            self._log("Calibração não lida; usando valores padrão.")

        # Comando de streaming contínuo
        self._send([self.HID_SET_REPORT, self.RPT_DATA_MODE, 0x04, 0x32])
        time.sleep(0.05)

        self._connected = True
        self._log("Conectado! Streaming de dados iniciado.")
        return True

    def _send(self, data: list):
        if self._dev:
            try:
                # O formato nativo que o Windows exige
                if data and data[0] == self.HID_SET_REPORT and len(data) >= 2:
                    buffer = [data[1]] + data[2:]
                else:
                    buffer = [data[0]] + data[1:]
                
                # ESCUDO DE MEMÓRIA: Padroniza para exatos 22 bytes 
                # (Previne o "Access Violation" do hidapi.dll)
                while len(buffer) < 22:
                    buffer.append(0x00)
                    
                buffer = buffer[:22]
                self._dev.write(buffer)
            except Exception as e:
                self._log(f"Erro ao enviar: {e}")

    def _write_reg(self, address: int, data: bytes):
        addr_bytes = list(struct.pack(">I", address)[1:])
        payload = [self.HID_SET_REPORT, self.RPT_WRITE_MEM, 0x04] \
                  + addr_bytes + [len(data) - 1] + list(data.ljust(16, b"\x00"))
        self._send(payload)

    def _request_calibration(self):
        addr_bytes = list(struct.pack(">I", self.REG_CALIBRATION)[1:])
        size_bytes = list(struct.pack(">H", 24))
        self._send([self.HID_SET_REPORT, self.RPT_READ_MEM, 0x04] + addr_bytes + size_bytes)

    def _read_loop(self):
        # Modo Ralo: Lê instantaneamente para a memória do Windows nunca explodir
        self._dev.set_nonblocking(True)
        last_keepalive = time.time()
        
        while self._running:
            packets = []
            
            # 1. ESVAZIA A MEMÓRIA DO WINDOWS
            while True:
                try:
                    packet = self._dev.read(64)
                    if not packet or len(packet) < 2:
                        break
                    packets.append(packet)
                except Exception:
                    break
                    
            if not packets:
                time.sleep(0.01) # Respira se não tiver pacote
                continue

            # 2. PROCESSA O BUFFER PUXADO
            for packet in packets:
                self._packet_count += 1
                report_id = self._report_id(packet)

                if report_id == 0x21:
                    self._handle_read_data(packet)
                elif report_id == 0x20:
                    self._handle_status(packet)
                elif report_id == 0x22:
                    self._handle_ack(packet)
                elif report_id == 0x32:
                    # Só deixa calcular peso se a calibração já rolou
                    if not self._connected:
                        continue
                    sd = self._parse_sensors(packet)
                    if sd and self.on_data:
                        self._data_count += 1
                        self.on_data(sd)
                elif report_id == 0x34:
                    if not self._connected:
                        continue
                    sd = self._parse_sensors_0x34(packet)
                    if sd and self.on_data:
                        self._data_count += 1
                        self.on_data(sd)
                else:
                    self._unknown_count += 1

            # 3. KEEP-ALIVE (Impede a prancha de desligar sozinha aos 20 seg)
            now = time.time()
            if now - last_keepalive > 10.0:
                self._send([self.HID_SET_REPORT, 0x15, 0x00])
                last_keepalive = now

    def _report_id(self, packet) -> int:
        if packet[0] in (0x20, 0x21, 0x22, 0x30, 0x31, 0x32, 0x34):
            return packet[0]
        if len(packet) > 1 and packet[1] in (0x20, 0x21, 0x22, 0x30, 0x31, 0x32, 0x34):
            return packet[1]
        return packet[0]

    def _handle_status(self, packet):
        if len(packet) < 7: return
        flags = packet[3]
        # self._log(f"Status 0x20 recebido: flags=0x{flags:02X}")

    def _handle_ack(self, packet):
        if len(packet) < 5: return

    def _handle_read_data(self, packet):
        if len(packet) < 7: return
        error = packet[3] & 0x0F
        if error:
            self._cal_event.set()
            return
        size = ((packet[3] >> 4) & 0x0F) + 1
        data = bytes(packet[7:7 + size])
        if self._pending_cal is None:
            self._pending_cal = b""
        self._pending_cal += data
        if len(self._pending_cal) >= 24:
            self._cal_event.set()

    def _parse_sensors(self, packet) -> Optional[SensorData]:
        return self._parse_sensor_packet(packet, offsets=(2, 3, 4, 5))

    def _parse_sensors_0x34(self, packet) -> Optional[SensorData]:
        return self._parse_sensor_packet(packet, offsets=(4, 5, 6, 7))

    def _parse_sensor_packet(self, packet, offsets) -> Optional[SensorData]:
        data = bytes(packet)
        for offset in offsets:
            if len(data) < offset + 8:
                continue
            try:
                raw_tr, raw_br, raw_tl, raw_bl = struct.unpack_from(">HHHH", data, offset)
            except struct.error:
                continue
            if not self._looks_like_sensor_values(raw_tr, raw_br, raw_tl, raw_bl):
                continue
            
            # 1. Aplica a calibração
            tl = self.calibration.raw_to_kg("top_left", raw_tl)
            tr = self.calibration.raw_to_kg("top_right", raw_tr)
            bl = self.calibration.raw_to_kg("bottom_left", raw_bl)
            br = self.calibration.raw_to_kg("bottom_right", raw_br)

            # --- 2. ESCUDO ANTI-FANTASMA (Protege a Tara) ---
            # Bloqueia apenas erros absurdos de hardware antes de tentar zerar.
            if any(sensor > 300.0 for sensor in (tl, tr, bl, br)):
                return None

            # 3. TARA LIMPA E TRAVADA
            if not self._tared:
                if self._startup_discard > 0:
                    self._startup_discard -= 1
                    return None
                
                self._tare_samples.append((tl, tr, bl, br))
                
                if len(self._tare_samples) >= 10:
                    self._tare_values['top_left'] = sum(s[0] for s in self._tare_samples) / 10
                    self._tare_values['top_right'] = sum(s[1] for s in self._tare_samples) / 10
                    self._tare_values['bottom_left'] = sum(s[2] for s in self._tare_samples) / 10
                    self._tare_values['bottom_right'] = sum(s[3] for s in self._tare_samples) / 10
                    self._tared = True
                    self._log("Balança zerada e estabilizada!")
                return None

            # 4. APLICA A TARA 
            # (A partir daqui, a balança está em 0.0kg se não houver ninguém em cima)
            tl -= self._tare_values['top_left']
            tr -= self._tare_values['top_right']
            bl -= self._tare_values['bottom_left'] 
            br -= self._tare_values['bottom_right']

            current_tl = max(0.0, tl)
            current_tr = max(0.0, tr)
            current_bl = max(0.0, bl)
            current_br = max(0.0, br)

            # --- 5. O FILTRO DE FÍSICA FINA (Sua ideia, agora no lugar certo!) ---
            # Uma pessoa real não pesa mais de 150kg apoiada em um único canto
            if any(sensor > 150.0 for sensor in (current_tl, current_tr, current_bl, current_br)):
                return None

            # Regra da Torção (Diagonais não podem divergir absurdamente em uma prancha rígida)
            diagonal_a = current_tl + current_br
            diagonal_b = current_tr + current_bl
            if abs(diagonal_a - diagonal_b) > 40.0:
                return None
            # ---------------------------------------------------------------------

            # 6. AMORTECEDOR MATEMÁTICO (Filtro Passa-Baixa)
            if self._last_stable_sd is None:
                self._last_stable_sd = [current_tl, current_tr, current_bl, current_br]
            else:
                alpha = 0.9 # Nível de amortecimento extremo que testamos
                self._last_stable_sd[0] = (self._last_stable_sd[0] * (1 - alpha)) + (current_tl * alpha)
                self._last_stable_sd[1] = (self._last_stable_sd[1] * (1 - alpha)) + (current_tr * alpha)
                self._last_stable_sd[2] = (self._last_stable_sd[2] * (1 - alpha)) + (current_bl * alpha)
                self._last_stable_sd[3] = (self._last_stable_sd[3] * (1 - alpha)) + (current_br * alpha)

            return SensorData(
                top_left     = self._last_stable_sd[0],
                top_right    = self._last_stable_sd[1],
                bottom_left  = self._last_stable_sd[2],
                bottom_right = self._last_stable_sd[3],
            )
            
        return None

    def _looks_like_sensor_values(self, *values) -> bool:
        # A Balance Board envia valores de 16-bits (até 65535)
        return all(0 <= v <= 60000 for v in values) and any(v > 20 for v in values)

    def disconnect(self):
        self._running   = False
        self._connected = False
        try:
            self._send([self.HID_SET_REPORT, 0x11, 0x00])
        except Exception:
            pass
        if self._dev:
            try:
                self._dev.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        self._log("Desconectado.")

    def read(self) -> Optional[SensorData]:
        return None


# ── Simulador ────────────────────────────────────────────────────────────────
class BalanceBoardSimulator:
    def __init__(self, exercise: str = "squat",
                 on_data:   Optional[Callable[[SensorData], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None,
                 rate_hz: float = 10.0):
        self.exercise  = exercise
        self.on_data   = on_data
        self.on_status = on_status
        self._rate     = rate_hz
        self._tick     = 0
        self._running  = False
        self._thread: Optional[threading.Thread] = None

    def connect(self) -> bool:
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if self.on_status: self.on_status(f"Simulador iniciado ({self.exercise})")
        return True

    def _loop(self):
        interval = 1.0 / self._rate
        while self._running:
            time.sleep(interval)
            self._tick += 1
            tl, tr, bl, br = PATTERNS.get(self.exercise, _stand_pattern)(self._tick)
            if self.on_data:
                self.on_data(SensorData(max(0.0, tl), max(0.0, tr), max(0.0, bl), max(0.0, br)))

    def read(self) -> Optional[SensorData]:
        return None

    def disconnect(self):
        self._running = False
        if self._thread: self._thread.join(timeout=2.0)


def _squat_pattern(t):
    n = lambda s=1.5: random.gauss(0, s)
    return (18 + math.sin(t*0.08)*6 + n(), 17 + math.sin(t*0.08+0.3)*6 + n(),
            15 + math.cos(t*0.08)*4 + n(), 20 + math.sin(t*0.05)*5 + n())

def _balance_pattern(t):
    n = lambda s=2.5: random.gauss(0, s)
    return (5 + math.sin(t*0.12)*4 + n(), 30 + math.sin(t*0.09)*8 + n(),
            4 + math.cos(t*0.11)*3 + n(), 31 + math.cos(t*0.07)*7 + n())

def _stand_pattern(t):
    n = lambda: random.gauss(0, 0.8)
    return 17+n(), 18+n(), 17+n(), 18+n()

def _lunge_pattern(t):
    n = lambda s=2.0: random.gauss(0, s)
    return (25 + math.sin(t*0.06)*10 + n(), 10 + math.sin(t*0.06+math.pi)*5 + n(),
            15 + math.cos(t*0.05)*8 + n(), 8  + math.cos(t*0.05+math.pi)*4 + n())

PATTERNS = {"squat": _squat_pattern, "balance": _balance_pattern, "stand": _stand_pattern, "lunge": _lunge_pattern}