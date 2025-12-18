import serial
import time
import threading
import sys
import re
import csv
import numpy as np
import bchlib
import hashlib
from datetime import datetime
import matplotlib.pyplot as plt

# ================= НАСТРОЙКИ ПОРТОВ =================
ALICE_PORT = "/dev/ttyUSB0"
BOB_PORT   = "/dev/ttyUSB1"
EVE_PORT   = "/dev/ttyUSB2"  # <--- Порт Евы
BAUD_RATE  = 460800

# Параметры
PHASE_DURATION = 3
WIFI_CHANNEL   = 6
WIFI_BW        = 40
WIFI_SEC       = "below"

# MAC-адреса
MAC_ALICE = "1a:00:00:00:00:01"
MAC_BOB   = "1a:00:00:00:00:02"
# Еве не обязательно менять MAC, она пассивна, но для порядка можно
MAC_EVE   = "1a:00:00:00:00:66" 

# ================= УТИЛИТЫ ЛОГИРОВАНИЯ =================
def log_stage(stage, msg):
    print(f"\n{'='*60}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] STAGE {stage}: {msg}")
    print(f"{'='*60}")

def log_pub(msg):
    print(f"\n[PUBLIC CHANNEL] >>> {msg}")

# ================= ВИЗУАЛИЗАЦИЯ =================
def visualize_results(alice, bob, eve):
    print("\n[VISUALIZATION] Строим графики...")
    
    # 1. Подготовка данных для графиков CSI
    # Мы берем усредненную амплитуду, которую сохранили в PLKG_Logic
    amp_a = alice.plkg.last_mean_amp
    amp_b = bob.plkg.last_mean_amp
    amp_e = eve.plkg.last_mean_amp
    
    if amp_a is None or amp_b is None:
        print("Нет данных для графиков.")
        return

    # 2. Подготовка битов для Heatmap
    def to_bit_array(bytes_val):
        if not bytes_val: return np.zeros(128) # заглушка
        # Превращаем байты в массив 0 и 1
        return np.array([int(b) for b in list(np.unpackbits(np.frombuffer(bytes_val, dtype=np.uint8)))])

    bits_a = to_bit_array(alice.plkg.key_raw_bytes)
    bits_b = to_bit_array(bob.plkg.key_raw_bytes)
    
    # У Евы может не быть ключа
    bits_e = to_bit_array(eve.plkg.key_raw_bytes) if eve.plkg.key_raw_bytes else np.zeros_like(bits_a)
    
    # Обрезаем до одинаковой длины для отображения
    min_len = min(len(bits_a), len(bits_b))
    bits_a = bits_a[:min_len]
    bits_b = bits_b[:min_len]
    bits_e = bits_e[:min_len]

    # --- ПОСТРОЕНИЕ ---
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
    
    # ГРАФИК 1: CSI Amplitude (Channel Reciprocity)
    x_axis = np.arange(len(amp_a))
    ax1.plot(x_axis, amp_a, 'b-', label='Alice (RX from Bob)', linewidth=2)
    ax1.plot(x_axis, amp_b, 'r--', label='Bob (RX from Alice)', linewidth=2)
    if amp_e is not None:
        # У Евы может быть другая длина массива, обрежем или растянем
        if len(amp_e) == len(x_axis):
            ax1.plot(x_axis, amp_e, 'g-', label='Eve (RX)', alpha=0.6)
            
    ax1.set_title('CSI Amplitude Profile (Channel Reciprocity)')
    ax1.set_ylabel('Amplitude (dB)')
    ax1.set_xlabel('Subcarrier Index')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # ГРАФИК 2: Key Bits Comparison
    # Создаем матрицу где: 0=Black, 1=White (или цвета)
    # Но лучше сделать карту ошибок: Зеленый=Совпадение с Алисой, Красный=Ошибка
    
    # Сравниваем с Алисой
    diff_b = (bits_b != bits_a).astype(int) # 0 если совпал, 1 если ошибка
    diff_e = (bits_e != bits_a).astype(int)
    
    # Рисуем сами биты (0 и 1)
    # Матрица 3 x N
    bit_matrix = np.vstack([bits_a, bits_b, bits_e])
    
    cax = ax2.imshow(bit_matrix, cmap='Greys', aspect='auto', interpolation='nearest')
    
    # Подписи
    ax2.set_yticks([0, 1, 2])
    ax2.set_yticklabels(['Alice Key', 'Bob Key', 'Eve Key'])
    ax2.set_title(f'Generated Raw Keys ({min_len} bits)')
    ax2.set_xlabel('Bit Index')
    
    # Добавим красные пометки там, где ошибки (для Боба и Евы)
    # Bob errors
    for i in range(len(diff_b)):
        if diff_b[i] == 1:
            ax2.add_patch(plt.Rectangle((i-0.5, 0.5), 1, 1, fill=True, color='red', alpha=0.5))
    # Eve errors
    for i in range(len(diff_e)):
        if diff_e[i] == 1:
            ax2.add_patch(plt.Rectangle((i-0.5, 1.5), 1, 1, fill=True, color='red', alpha=0.5))

    plt.tight_layout()
    plt.savefig('csi_result.png')
    print("[VISUALIZATION] График сохранен в 'csi_result.png'")
    plt.show()

# ================= ЛОГИКА PLKG =================
class PLKG_Logic:
    def __init__(self, name):
        self.name = name
        self.bch = None
        self.init_bch()
        self.raw_csi_data = [] 
        self.key_raw_bytes = None
        self.final_key = None
        
        # Для графиков
        self.last_mean_amp = None 

    def init_bch(self):
        try: self.bch = bchlib.BCH(8219, 16) 
        except:
            try: self.bch = bchlib.BCH(285, 2)
            except: self.bch = None

    def add_csi_packet(self, csi_str):
        try:
            clean = csi_str.replace('"', '').replace('[', '').replace(']', '')
            arr = np.fromstring(clean, sep=',', dtype=int)
            if len(arr) > 0: self.raw_csi_data.append(arr)
        except: pass

    def generate_key(self):
        if len(self.raw_csi_data) < 10: return False
        
        valid_data = self.raw_csi_data[-150:] 
        lengths = [len(x) for x in valid_data]
        if not lengths: return False
        common_len = max(set(lengths), key=lengths.count)
        valid_data = [x for x in valid_data if len(x) == common_len]
        
        matrix = np.stack(valid_data)
        if common_len > 64: matrix = np.abs(matrix)
        
        # Срез (центр спектра)
        use_slice = slice(10, min(58, matrix.shape[1]))
        matrix = matrix[:, use_slice]
        
        # 1. Вычисляем среднюю амплитуду (Channel Profile)
        mean_vec = np.mean(matrix, axis=0)
        
        # Сохраняем для графиков!
        self.last_mean_amp = mean_vec 
        
        # 2. Квантование
        threshold = np.mean(mean_vec)
        bits = (mean_vec > threshold).astype(int)
        
        num_bytes = len(bits) // 8
        if num_bytes == 0: return False
        self.key_raw_bytes = np.packbits(bits[:num_bytes*8]).tobytes()
        return True

    def create_ecc(self):
        if not self.bch: return b''
        return self.bch.encode(self.key_raw_bytes)

    def reconcile(self, ecc):
        if not self.bch: return False, 0
        bitflips, corrected, checksum = self.bch.decode(self.key_raw_bytes, ecc)
        if bitflips != -1:
            self.key_raw_bytes = corrected
            return True, bitflips
        return False, -1
        
    def hash_key(self):
        if self.key_raw_bytes:
            self.final_key = hashlib.sha256(self.key_raw_bytes).digest()

# ================= УСТРОЙСТВО ESP32 =================
class ESPDevice:
    def __init__(self, port, baud, name, filename):
        self.name = name
        self.plkg = PLKG_Logic(name)
        self.ser = None
        self.port = port
        self.baud = baud
        self.file_h = open(filename, 'w', newline='')
        self.csv = csv.writer(self.file_h)
        self.packet_count = 0
        self.running = False

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except:
            print(f"[{self.name}] ОШИБКА ОТКРЫТИЯ ПОРТА {self.port}")
            sys.exit(1)

    def send_cmd(self, cmd):
        if self.ser:
            self.ser.write(("\n" + cmd + "\n").encode())
            time.sleep(0.1)

    def listen(self):
        self.running = True
        while self.running:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if "CSI_DATA" in line:
                        match = re.search(r'\"\[(.*?)\]\"', line)
                        if match:
                            raw = match.group(1)
                            self.csv.writerow([time.time(), "RX", raw])
                            self.file_h.flush()
                            self.packet_count += 1
                            self.plkg.add_csi_packet(raw)
            except: break
            
    def close(self):
        if self.ser: self.ser.close()
        self.file_h.close()

# =================================================================
#                         ГЛАВНЫЙ ПРОЦЕСС
# =================================================================
def main():
    alice = ESPDevice(ALICE_PORT, BAUD_RATE, "Alice", "alice.csv")
    bob   = ESPDevice(BOB_PORT,   BAUD_RATE, "Bob",   "bob.csv")
    eve   = ESPDevice(EVE_PORT,   BAUD_RATE, "Eve",   "eve.csv")
    devices = [alice, bob, eve]

    for d in devices: d.connect()
    threads = [threading.Thread(target=d.listen) for d in devices]
    for t in threads: t.start()

    try:
        log_stage(1, "ИНИЦИАЛИЗАЦИЯ ОБОРУДОВАНИЯ")
        alice.send_cmd(f"restart")
        bob.send_cmd(f"restart")
        eve.send_cmd(f"restart")
        time.sleep(5)

        alice.send_cmd(f"radio_init -c {WIFI_CHANNEL} -b 40 -s below -m {MAC_ALICE} --restart")
        bob.send_cmd(  f"radio_init -c {WIFI_CHANNEL} -b 40 -s below -m {MAC_BOB}   --restart")
        eve.send_cmd(  f"radio_init -c {WIFI_CHANNEL} -b 40 -s below -m {MAC_EVE}   --restart")
        
        print(">>> Ожидание перезагрузки драйверов (5 сек)...")
        time.sleep(5)
        
        log_stage(2, "СБОР ДАННЫХ CSI (PING-PONG)")
        
        print(">>> [Phase 1] Alice TX -> Bob RX")
        bob.send_cmd(f"recv -m {MAC_ALICE} -t {PHASE_DURATION+1}")
        eve.send_cmd(f"recv -t {PHASE_DURATION*3}") 
        time.sleep(1)
        alice.send_cmd(f"ping -t {PHASE_DURATION}")
        
        for i in range(PHASE_DURATION):
            sys.stdout.write(f"\r Collecting... Bob: {bob.packet_count}")
            sys.stdout.flush()
            time.sleep(1)
        print("")

        time.sleep(2) 

        print(">>> [Phase 2] Bob TX -> Alice RX")
        alice.send_cmd(f"recv -m {MAC_BOB} -t {PHASE_DURATION+1}")
        time.sleep(1)
        bob.send_cmd(f"ping -t {PHASE_DURATION}")

        pkts_start = alice.packet_count
        for i in range(PHASE_DURATION):
            curr = alice.packet_count - pkts_start
            sys.stdout.write(f"\r Collecting... Alice: {curr}")
            sys.stdout.flush()
            time.sleep(1)
        print("")

        log_stage(3, "ГЕНЕРАЦИЯ КЛЮЧЕЙ И ВИЗУАЛИЗАЦИЯ")
        
        a_res = alice.plkg.generate_key()
        b_res = bob.plkg.generate_key()
        e_res = eve.plkg.generate_key()

        if a_res and b_res:
            # === ВЫЗОВ ВИЗУАЛИЗАЦИИ ===
            visualize_results(alice, bob, eve)

        log_stage(4, "ПРОТОКОЛ СОГЛАСОВАНИЯ")
        
        if a_res and b_res:
            ecc_payload = alice.plkg.create_ecc()
            log_pub(f"Алиса отправляет ECC: {ecc_payload.hex().upper()}")
            
            success, fixed = bob.plkg.reconcile(ecc_payload)
            if success:
                print(f"[Bob] УСПЕХ! Исправлено бит: {fixed}")
            else:
                print(f"[Bob] ПРОВАЛ!")

            if e_res:
                e_success, _ = eve.plkg.reconcile(ecc_payload)
                if e_success and eve.plkg.key_raw_bytes == alice.plkg.key_raw_bytes:
                     print("[Eve] ЕВА ВЗЛОМАЛА КЛЮЧ!")
                else:
                     print("[Eve] Ева не смогла получить ключ.")

        log_stage(5, "ФИНАЛИЗАЦИЯ")
        alice.plkg.hash_key()
        bob.plkg.hash_key()
        
        if alice.plkg.final_key == bob.plkg.final_key:
             print("\n>>> ЗАЩИЩЕННЫЙ КАНАЛ УСТАНОВЛЕН <<<")
        else:
             print("\n>>> ОШИБКА СОГЛАСОВАНИЯ <<<")

    except KeyboardInterrupt:
        print("\nStop.")
    finally:
        for d in devices: d.running = False
        for t in threads: t.join()
        for d in devices: d.close()

if __name__ == "__main__":
    main()