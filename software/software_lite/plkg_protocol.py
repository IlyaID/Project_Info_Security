import serial
import time
import threading
import sys
import re
import csv
import numpy as np
import hashlib
import random 
from datetime import datetime

# Криптография (AES-GCM)
try:
    from Crypto.Cipher import AES
except ImportError:
    print("ERROR: install pycryptodome (pip install pycryptodome)")
    sys.exit(1)

# ================= НАСТРОЙКИ =================
ALICE_PORT = "/dev/ttyUSB0" 
BOB_PORT   = "/dev/ttyUSB1"
EVE_PORT   = "/dev/ttyUSB2"
BAUD_RATE  = 460800

WIFI_CHANNEL = 6
MAC_ALICE    = "1a:00:00:00:00:01"
MAC_BOB      = "1a:00:00:00:00:02"
MAC_EVE      = "1a:00:00:00:00:66"

# Параметры сбора
PHASE_DURATION = 10 # Секунд на каждую фазу

# Алгоритм PLKG
CSI_VALID_RANGES = [slice(12, 63), slice(65, 118)]
ALGO_K_MAIN      = 16
ALGO_M_NEIGHBORS = 2
ALGO_Q_BITS      = 2

# ================= УТИЛИТЫ =================
def log(tag, msg):
    ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    print(f"[{ts}] [{tag}] {msg}")

def log_section(title):
    print(f"\n{'='*60}\n=== {title} ===\n{'='*60}")


# ================= CASCADE RECONCILIATION =================
class CascadeReconciliation:
    def __init__(self, block_size=8):
        self.block_size = block_size

    def calculate_parity(self, data):
        return sum(data) % 2

    def binary_search_correction(self, bits_a, parity_a, bits_b, parity_b):
        if parity_a == parity_b: return bits_b, 0
        if len(bits_b) == 1:
            bits_b[0] = 1 - bits_b[0] # Flip bit
            return bits_b, 1

        mid = len(bits_b) // 2
        left_a, left_b = bits_a[:mid], bits_b[:mid]
        right_a, right_b = bits_a[mid:], bits_b[mid:]
        
        p_a_l = self.calculate_parity(left_a)
        p_b_l = self.calculate_parity(left_b)
        
        corr = 0
        if p_a_l != p_b_l:
            new_l, c = self.binary_search_correction(left_a, p_a_l, left_b, p_b_l)
            bits_b[:mid] = new_l
            corr += c
            
        p_a_r = self.calculate_parity(right_a)
        p_b_r = self.calculate_parity(right_b) # Пересчитываем т.к. массив изменился? Нет, binary search идем вглубь
        
        if p_a_r != p_b_r:
            new_r, c = self.binary_search_correction(right_a, p_a_r, right_b, p_b_r)
            bits_b[mid:] = new_r
            corr += c
            
        return bits_b, corr

    def run_cascade(self, key_a_bytes, key_b_bytes, num_passes=4):
        bits_a = list(np.unpackbits(np.frombuffer(key_a_bytes, dtype=np.uint8)))
        bits_b = list(np.unpackbits(np.frombuffer(key_b_bytes, dtype=np.uint8)))
        if len(bits_a) != len(bits_b): return key_b_bytes, 0

        total_corrections = 0
        n = len(bits_a)
        perm_indices = list(range(n))

        for pass_idx in range(num_passes):
            current_block_size = self.block_size * (2 ** pass_idx)
            # Shuffle
            random.seed(pass_idx)
            random.shuffle(perm_indices)
            shuffled_a = [bits_a[i] for i in perm_indices]
            shuffled_b = [bits_b[i] for i in perm_indices]
            
            # Blocks
            for i in range(0, n, current_block_size):
                blk_a = shuffled_a[i : i + current_block_size]
                blk_b = shuffled_b[i : i + current_block_size]
                p_a = self.calculate_parity(blk_a)
                p_b = self.calculate_parity(blk_b)
                
                if p_a != p_b:
                    fixed_blk, c = self.binary_search_correction(blk_a, p_a, blk_b, p_b)
                    shuffled_b[i : i + current_block_size] = fixed_blk
                    total_corrections += c

            # Un-shuffle
            temp_b = [0] * n
            for i, orig_idx in enumerate(perm_indices):
                temp_b[orig_idx] = shuffled_b[i]
            bits_b = temp_b

        return np.packbits(bits_b).tobytes(), total_corrections


# ================= ЛОГИКА PLKG =================
class PLKG_Logic:
    def __init__(self, name):
        self.name = name
        self.raw_csi_data = [] 
        self.key_raw_bytes = None
        self.final_key = None
        self.packets_collected = 0

    def add_csi_packet(self, csi_str):
        try:
            clean = csi_str.replace('"', '').replace('[', '').replace(']', '')
            arr = np.fromstring(clean, sep=',', dtype=int)
            if len(arr) > 0: 
                self.raw_csi_data.append(arr)
                self.packets_collected += 1
        except: pass

    def generate_key(self):
        log(self.name, f"Gen Key from {len(self.raw_csi_data)} pkts...")
        if len(self.raw_csi_data) < 10: return False
        
        # 1. Фильтрация
        valid = self.raw_csi_data[-150:]
        lengths = [len(x) for x in valid]
        if not lengths: return False
        common_len = max(set(lengths), key=lengths.count)
        valid = [x for x in valid if len(x) == common_len]
        
        # 2. Матрица и Амплитуда
        matrix = np.abs(np.stack(valid)) if common_len > 64 else np.stack(valid)
        
        # 3. Склейка
        parts = []
        max_idx = matrix.shape[1]
        for s in CSI_VALID_RANGES:
            start = s.start if s.start else 0
            stop = min(s.stop, max_idx)
            if start < stop: parts.append(matrix[:, start:stop])
        
        if not parts: return False
        clean = np.hstack(parts)
        mean_vec = np.mean(clean, axis=0) 
        
        # 4. Выбор поднесущих и Квантование
        num_sc = len(mean_vec)
        step = (num_sc - 2 * ALGO_M_NEIGHBORS) // ALGO_K_MAIN
        if step < 1: step = 1
        indices = [ALGO_M_NEIGHBORS + i*step for i in range(ALGO_K_MAIN)]
        indices = [i for i in indices if i < num_sc - ALGO_M_NEIGHBORS]
        
        thresholds = np.percentile(mean_vec, [25, 50, 75])
        bits = []
        
        for idx in indices:
            neighbors = mean_vec[idx - ALGO_M_NEIGHBORS : idx + ALGO_M_NEIGHBORS + 1]
            votes = []
            for val in neighbors:
                if val < thresholds[0]: votes.append(0)
                elif val < thresholds[1]: votes.append(1)
                elif val < thresholds[2]: votes.append(3) # Gray 11
                else: votes.append(2)                     # Gray 10
            
            winner = max(set(votes), key=votes.count)
            
            if winner == 0: bits.extend([0, 0])
            elif winner == 1: bits.extend([0, 1])
            elif winner == 2: bits.extend([1, 0])
            elif winner == 3: bits.extend([1, 1])
            
        b_arr = np.array(bits, dtype=int)
        
        # Упаковка
        num_bytes = len(b_arr) // 8
        if num_bytes > 0:
            self.key_raw_bytes = np.packbits(b_arr[:num_bytes*8]).tobytes()
        else:
            padded = np.pad(b_arr, (0, 8-len(b_arr)), 'constant')
            self.key_raw_bytes = np.packbits(padded).tobytes()

        self.final_key = hashlib.sha256(self.key_raw_bytes).digest()
        return True

# ================= КЛАСС УСТРОЙСТВА =================
class ESPDevice:
    def __init__(self, port, baud, name, filename):
        self.name = name
        self.plkg = PLKG_Logic(name)
        self.ser = None
        self.port = port
        self.baud = baud
        self.file_h = open(filename, 'w', newline='')
        self.csv = csv.writer(self.file_h)
        self.running = False
        self.lock = threading.Lock()
        self.captured_msgs = [] # Перехваченные сообщения

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
            log(self.name, f"Connected {self.port}")
        except: sys.exit(1)

    def _send(self, cmd):
        if self.ser:
            with self.lock:
                self.ser.write(f"\n{cmd}\n".encode())
            time.sleep(0.1)

    def radio_init(self, channel=11, bw=40, mac=None, restart=False):
        args = f"-c {channel} -b {bw} -s below"
        if mac: args += f" -m {mac}"
        if restart: args += " --restart"
        self._send(f"radio_init {args}")
        time.sleep(2.5)

    def start_recv(self, timeout=None, mac=None):
        cmd = "recv"
        if timeout: cmd += f" -t {timeout}"
        # Для Боба ставим mac=Alice, для Алисы mac=Bob
        if mac: cmd += f" -m {mac}" 
        self._send(cmd)

    def start_ping(self, timeout):
        self._send(f"ping -t {timeout}")

    def msg_send(self, text, dest_mac):
        # Отправка текста через ESP-NOW
        self._send(f'msg_send -m {dest_mac} "{text}"')
        print(f"[{self.name}] SENT: {text[:20]}...")

    def msg_listen(self):
        """msg_listen"""
        print(f"[{self.name}] CMD: msg_listen")
        self._send("msg_listen")

    def listen(self):
        self.running = True
        self.csv.writerow(["timestamp", "role", "type", "data"])
        while self.running:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if not line: continue
                    ts = time.time()
                    
                    if "CSI_DATA" in line:
                        match = re.search(r'\"\[(.*?)\]\"', line)
                        if match:
                            raw = match.group(1)
                            self.csv.writerow([ts, "RX", "CSI", raw])
                            self.plkg.add_csi_packet(raw)
                    
                    # Ловим сообщения (формат зависит от прошивки, ищем маркеры)
                    elif "MSG_RECV" in line or "Chat" in line or "SECURE_MSG:" in line:
                        # Извлекаем текст сообщения
                        # Пример: [Chat] From XX: "SECURE_MSG:abcd123..."
                        if "SECURE_MSG:" in line:
                            parts = line.split("SECURE_MSG:")
                            if len(parts) > 1:
                                payload = parts[1].strip().strip('"')
                                self.captured_msgs.append(payload)
                                print(f"[{self.name}] CAPTURED: {payload[:15]}...")
                        self.csv.writerow([ts, "RX", "MSG", line])
            except: break
                
    def close(self):
        self.running = False
        if self.ser: self.ser.close()
        self.file_h.close()

# ================= КРИПТО-ФУНКЦИИ (Python Side) =================
def encrypt_payload(key, plaintext):
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode('utf-8'))
    # Формат: nonce(16) + tag(16) + ciphertext
    payload = cipher.nonce + tag + ciphertext
    return payload.hex()

def decrypt_payload(key, hex_str):
    try:
        data = bytes.fromhex(hex_str)
        nonce = data[:16]
        tag = data[16:32]
        ciphertext = data[32:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        text = cipher.decrypt_and_verify(ciphertext, tag)
        return text.decode('utf-8')
    except:
        return None


# ================= MAIN =================
def main():
    log_section("SETUP")
    
    alice = ESPDevice(ALICE_PORT, BAUD_RATE, "Alice", "alice.csv")
    bob   = ESPDevice(BOB_PORT,   BAUD_RATE, "Bob",   "bob.csv")
    eve   = ESPDevice(EVE_PORT,   BAUD_RATE, "Eve",   "eve.csv")
    
    devs = [alice, bob, eve]
    for d in devs: d.connect()
    
    th = [threading.Thread(target=d.listen, daemon=True) for d in devs]
    for t in th: t.start()

    try:
        # 1. INIT
        log_section("RADIO INIT")
        for d in devs: d._send("restart")
        time.sleep(5)
        alice.radio_init(WIFI_CHANNEL, 40, MAC_ALICE, restart=True)
        bob.radio_init(WIFI_CHANNEL, 40, MAC_BOB, restart=True)
        eve.radio_init(WIFI_CHANNEL, 40, MAC_EVE, restart=True)
        time.sleep(2)

        # 2. CSI COLLECTION (TWO WAY)
        log_section("CSI COLLECTION (SHAKE!)")
        
        # --- PHASE 1: Alice TX -> Bob RX (Eve RX) ---
        log("CTRL", ">>> PHASE 1: Alice TX -> Bob RX")
        bob.start_recv(timeout=PHASE_DURATION+2, mac=MAC_ALICE) # Боб слушает Алису
        eve.start_recv(timeout=PHASE_DURATION+2, mac=MAC_ALICE) # Ева слушает Алису
        time.sleep(1)
        alice.start_ping(timeout=PHASE_DURATION)
        
        for i in range(PHASE_DURATION):
            sys.stdout.write(f"\rPh1: {i+1}/{PHASE_DURATION} | Bob={bob.plkg.packets_collected} Eve={eve.plkg.packets_collected}")
            sys.stdout.flush()
            time.sleep(1)
        print()
        
        # Пауза
        time.sleep(2)
        
        # --- PHASE 2: Bob TX -> Alice RX (Eve RX) ---
        log("CTRL", ">>> PHASE 2: Bob TX -> Alice RX")
        alice.start_recv(timeout=PHASE_DURATION+2, mac=MAC_BOB) # Алиса слушает Боба
        eve.start_recv(timeout=PHASE_DURATION*3, mac=MAC_BOB) # Ева тоже (накапливает)
        time.sleep(1)
        bob.start_ping(timeout=PHASE_DURATION)
        
               
        for i in range(PHASE_DURATION):
            sys.stdout.write(f"\rPh2: {i+1}/{PHASE_DURATION} | Alice={alice.plkg.packets_collected} Eve={eve.plkg.packets_collected}")
            sys.stdout.flush()
            time.sleep(1)
        print()

        # 3. KEY GEN & COMPARE
        log_section("KEY ANALYSIS")
        
        # Генерируем у всех
        a_ok = alice.plkg.generate_key()
        b_ok = bob.plkg.generate_key()
        e_ok = eve.plkg.generate_key() # Ева делает ключ из всего, что услышала
        
        if alice.plkg.key_raw_bytes and bob.plkg.key_raw_bytes:
            print(f"Alice Raw: {alice.plkg.key_raw_bytes.hex().upper()}")
            print(f"Bob   Raw: {bob.plkg.key_raw_bytes.hex().upper()}")
            
            # ЗАПУСК CASCADE
            cascade = CascadeReconciliation(block_size=8)
            new_bob_key, fixes = cascade.run_cascade(alice.plkg.key_raw_bytes, bob.plkg.key_raw_bytes)
            print(f"Cascade Corrected {fixes} errors.")
            
            bob.plkg.key_raw_bytes = new_bob_key
            bob.plkg.final_key = hashlib.sha256(new_bob_key).digest()
            print(f"Bob   New: {bob.plkg.key_raw_bytes.hex().upper()}")


        if a_ok and b_ok:
            ka = alice.plkg.key_raw_bytes
            kb = bob.plkg.key_raw_bytes
            
            # --- Сравнение Alice vs Bob ---
            # Вычисляем BER (Bit Error Rate)
            l = min(len(ka), len(kb))
            bits_a = np.unpackbits(np.frombuffer(ka[:l], dtype=np.uint8))
            bits_b = np.unpackbits(np.frombuffer(kb[:l], dtype=np.uint8))
            diff_ab = np.sum(bits_a != bits_b)
            ber_ab = (diff_ab / len(bits_a)) * 100
            
            log("RESULT", f"Alice Key: {ka.hex().upper()[:16]}...")
            log("RESULT", f"Bob   Key: {kb.hex().upper()[:16]}...")
            log("RESULT", f"Alice <-> Bob Mismatch: {ber_ab:.1f}% (Should be low < 15%)")
            
            # --- Сравнение Alice vs Eve ---
            if e_ok:
                ke = eve.plkg.key_raw_bytes
                l2 = min(len(ka), len(ke))
                bits_e = np.unpackbits(np.frombuffer(ke[:l2], dtype=np.uint8))
                diff_ae = np.sum(bits_a[:len(bits_e)] != bits_e)
                ber_ae = (diff_ae / len(bits_e)) * 100
                
                log("RESULT", f"Eve   Key: {ke.hex().upper()[:16]}...")
                log("RESULT", f"Alice <-> Eve Mismatch: {ber_ae:.1f}% (Should be high ~50%)")
            
            if ber_ab < 20 and (not e_ok or ber_ae > 40):
                log("RESULT", "SUCCESS: SECURE CHANNEL ESTABLISHED!")
            else:
                log("RESULT", "WARNING: Channel quality low or Eve correlated.")
                
        else:
            log("RESULT", "Key Gen Failed (Not enough packets).")

        print(">>> Enabling msg_listen on Bob and Eve...")
        bob.msg_listen()
        eve.msg_listen()
        time.sleep(1) # Даем время на применение команды
        # ===============================================
        
        if alice.plkg.final_key and bob.plkg.final_key:
            secret_text = "Launch Codes: 999-000-XYZ"
            
            # АЛИСА: Шифрует
            print(f"[Alice] Encrypting: '{secret_text}'")
            hex_msg = encrypt_payload(alice.plkg.final_key, secret_text)
            
            # АЛИСА: Отправляет
            # (Алисе msg_listen не обязателен, она передатчик, но Бобу и Еве - критичен)
            alice.msg_send(f"SECURE_MSG:{hex_msg}", dest_mac=MAC_BOB)
            
            print("Wait for transmission logs...")
            time.sleep(3) # Ждем, пока UART выплюнет логи
            
            # ПРОВЕРКА БОБА
            print("\n>>> Bob's Decryption Attempt:")
            if bob.captured_msgs:
                last_hex = bob.captured_msgs[-1]
                decrypted = decrypt_payload(bob.plkg.final_key, last_hex)
                if decrypted:
                    print(f"SUCCESS! Bob decrypted: '{decrypted}'")
                else:
                    print("FAIL! Bob could not decrypt (Key Mismatch).")
            else:
                print("FAIL! Bob did not capture any message (Check msg_listen).")

            # ПРОВЕРКА ЕВЫ
            print("\n>>> Eve's Decryption Attempt (ATTACK):")
            if eve.captured_msgs:
                last_hex = eve.captured_msgs[-1]
                decrypted_eve = decrypt_payload(eve.plkg.final_key, last_hex)
                
                if decrypted_eve:
                    print(f"CRITICAL: Eve decrypted the message! '{decrypted_eve}'")
                else:
                    print("SECURE: Eve failed to decrypt (Keys mismatch).")
            else:
                print("INFO: Eve missed the packet.")
                
        else:
            print("Keys not generated, skipping msg test.")

    except KeyboardInterrupt: pass
    finally:
        for d in devs: d.close()

if __name__ == "__main__":
    main()
