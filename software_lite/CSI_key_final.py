import serial
import time
import threading
import sys
import re
import csv
import numpy as np
import hashlib
import random
import matplotlib.pyplot as plt
from datetime import datetime
from Crypto.Cipher import AES

# ================= НАСТРОЙКИ =================
ALICE_PORT = "/dev/ttyUSB0" 
BOB_PORT   = "/dev/ttyUSB1"
EVE_PORT   = "/dev/ttyUSB2"
BAUD_RATE  = 460800 

WIFI_CHANNEL = 6
WIFI_BANDWIDTH = 40
MAC_ALICE    = "1a:00:00:00:00:01"
MAC_BOB      = "1a:00:00:00:00:02"
MAC_EVE      = "1a:00:00:00:00:66"

PHASE_DURATION = 15
CSI_VALID_RANGES = [slice(10, 60), slice(70, 118)]
ALGO_K_MAIN      = 16
ALGO_M_NEIGHBORS = 2

# ================= NETWORK CASCADE RECONCILIATION =================
class NetworkCascade:
    def __init__(self, device, role, block_size=8):
        self.device = device
        self.role = role
        self.block_size = block_size
        self.my_bits = None
        self.corrected_count = 0

    def set_key(self, key_bytes):
        self.my_bits = list(np.unpackbits(np.frombuffer(key_bytes, dtype=np.uint8)))

    def calculate_parity(self, indices):
        s = 0
        for i in indices:
            if i < len(self.my_bits): s += self.my_bits[i]
        return s % 2

    def _extract_payload(self, raw_msg, tag):
        """Извлекает часть строки, начиная с тега (CAS_INIT и т.д.)"""
        idx = raw_msg.find(tag)
        if idx != -1:
            return raw_msg[idx:]
        return None

    # --- ALICE ---
    def start_alice(self, dest_mac, num_passes=4):
        n = len(self.my_bits)
        for pass_idx in range(num_passes):
            print(f"[Cas-Alice] Pass {pass_idx} Init")
            perm = list(range(n)); random.seed(pass_idx); random.shuffle(perm)
            bs = self.block_size * (2 ** pass_idx)
            
            parities = []
            for i in range(0, n, bs):
                parities.append(self.calculate_parity(perm[i : i + bs]))
            
            p_str = "".join(map(str, parities))
            self.device.msg_send(f"CAS_INIT:{pass_idx}:{p_str}", dest_mac)
            
            while True:
                found = None; idx = -1
                if self.device.captured_msgs:
                    for k, m in enumerate(self.device.captured_msgs):
                        if f"CAS_DONE:{pass_idx}" in m: found=m; idx=k; break
                        if f"CAS_REQ:{pass_idx}" in m: found=m; idx=k; break
                
                if found:
                    self.device.captured_msgs.pop(idx)
                    
                    # Чистим сообщение от [MSG from...]
                    if "CAS_DONE" in found: break
                    
                    clean_req = self._extract_payload(found, f"CAS_REQ:{pass_idx}")
                    if clean_req:
                        try:
                            # CAS_REQ:pass:start:len
                            parts = clean_req.split(":")
                            if len(parts) >= 4:
                                start, length = int(parts[2]), int(parts[3])
                                req_ind = perm[start : start + length]
                                p = self.calculate_parity(req_ind)
                                self.device.msg_send(f"CAS_RESP:{pass_idx}:{start}:{p}", dest_mac)
                        except: pass
                time.sleep(0.02)
        print("[Cas-Alice] Finished")

    # --- BOB ---
    def start_bob(self, dest_mac, num_passes=4):
        n = len(self.my_bits)
        for pass_idx in range(num_passes):
            print(f"[Cas-Bob] Pass {pass_idx} Wait...")
            apar = []
            while True:
                found = False
                if self.device.captured_msgs:
                    for k, m in enumerate(self.device.captured_msgs):
                        # Ищем тег CAS_INIT:pass:
                        tag = f"CAS_INIT:{pass_idx}:"
                        if tag in m:
                            clean = self._extract_payload(m, tag)
                            if clean:
                                try:
                                    # clean = CAS_INIT:0:10101...
                                    parts = clean.split(":", 2)
                                    if len(parts) == 3:
                                        p_str = parts[2].strip()
                                        apar = [int(c) for c in p_str if c.isdigit()]
                                        self.device.captured_msgs.pop(k)
                                        found = True
                                        break
                                except: pass
                if found: break
                time.sleep(0.1)
            
            # Если паритеты не пришли или пусты - пропускаем проход
            if not apar:
                print(f"[Cas-Bob] Pass {pass_idx} skipped (no parities)")
                self.device.msg_send(f"CAS_DONE:{pass_idx}", dest_mac)
                continue

            perm = list(range(n)); random.seed(pass_idx); random.shuffle(perm)
            bs = self.block_size * (2 ** pass_idx)
            
            blk = 0
            for i in range(0, n, bs):
                ind = perm[i : i + bs]
                mp = self.calculate_parity(ind)
                if blk < len(apar) and mp != apar[blk]:
                    print(f"[Cas-Bob] Fixing Block {blk} (Alice={apar[blk]}, Bob={mp})")
                    self.interactive_binary_search(pass_idx, i, len(ind), ind, dest_mac)
                blk += 1
            
            self.device.msg_send(f"CAS_DONE:{pass_idx}", dest_mac)
            time.sleep(0.5)
            
        return np.packbits(self.my_bits).tobytes(), self.corrected_count

    def interactive_binary_search(self, pid, off, ln, inds, dest):
        c_off, c_len, c_inds = off, ln, inds
        while c_len > 1:
            mid = c_len // 2
            li = c_inds[:mid]; ri = c_inds[mid:]
            mlp = self.calculate_parity(li)
            
            self.device.msg_send(f"CAS_REQ:{pid}:{c_off}:{mid}", dest)
            ap = -1; t0 = time.time()
            while time.time()-t0 < 3.0:
                fid = -1
                if self.device.captured_msgs:
                    for k, m in enumerate(self.device.captured_msgs):
                        tag = f"CAS_RESP:{pid}:{c_off}:"
                        if tag in m:
                            clean = self._extract_payload(m, tag)
                            if clean:
                                try:
                                    parts = clean.split(":")
                                    if len(parts) >= 4:
                                        ap = int(parts[3])
                                        fid = k; break
                                except: pass
                if fid != -1: self.device.captured_msgs.pop(fid); break
                time.sleep(0.01)
            
            if ap == -1: return 
            if mlp != ap: c_inds = li; c_len = mid
            else: c_inds = ri; c_len = c_len - mid; c_off += mid
            
        b = c_inds[0]
        self.my_bits[b] = 1 - self.my_bits[b]
        self.corrected_count += 1
        print(f"[Cas-Bob] Fixed Bit {b}")


# ================= PASSIVE EVE =================
class PassiveEveCascade(NetworkCascade):
    def start_passive_listen(self, num_passes=8):
        print("[Eve] Started passive key correction...")
        n = len(self.my_bits)
        
        # Ева следит за каждым проходом
        for pass_idx in range(num_passes):
            # 1. Ева должна знать перестановку
            perm = list(range(n)); random.seed(pass_idx); random.shuffle(perm)
            bs = max(2, int(self.block_size * (1.5 ** pass_idx))) 
            
            # Ева слушает CAS_INIT от Алисы
            alice_parities = []
            t0 = time.time()
            while time.time() - t0 < 5.0: # Ждем INIT
                if self.device.captured_msgs:
                    # Ищем без удаления (peek), вдруг пригодится для дебага
                    for m in self.device.captured_msgs:
                        tag = f"CAS_INIT:{pass_idx}:"
                        if tag in m:
                            clean = self._extract_payload(m, tag)
                            if clean:
                                parts = clean.split(":", 2)
                                if len(parts) == 3:
                                    p_str = parts[2].strip()
                                    alice_parities = [int(c) for c in p_str if c.isdigit()]
                                    break
                if alice_parities: break
                time.sleep(0.1)
            
            if not alice_parities: 
                print(f"[Eve] Missed Pass {pass_idx} Init"); 
                continue

            # Ева сверяет свои блоки
            blk = 0
            for i in range(0, n, bs):
                ind = perm[i : i + bs]
                mp = self.calculate_parity(ind)
                if blk < len(alice_parities) and mp != alice_parities[blk]:
                    # ОШИБКА НАЙДЕНА!
                    # Ева пассивна, она не может слать CAS_REQ.
                    # Но она слушает ответы Алисы Бобу (CAS_RESP)
                    # Если Боб спросит про этот блок, Ева подслушает.
                    pass 
                blk += 1
                
            # Слушаем CAS_RESP
            t_end_pass = time.time() + 10.0 # Слушаем 10 сек пока Боб работает
            while time.time() < t_end_pass:
                 if self.device.captured_msgs:
                    # Ева жадно читает все RESP
                    msg_copy = list(self.device.captured_msgs)
                    self.device.captured_msgs = [] # Очищаем буфер, мы все прочитали
                    
                    for m in msg_copy:
                        if f"CAS_RESP:{pass_idx}:" in m:
                            clean = self._extract_payload(m, f"CAS_RESP:{pass_idx}:")
                            if clean:
                                try:
                                    # CAS_RESP:pass:start:parity
                                    parts = clean.split(":")
                                    start_idx = int(parts[2])
                                    alice_p = int(parts[3])
                                    
                                    # Ева проверяет: а какова длина этого запроса?
                                    # К сожалению, RESP не содержит длину. 
                                    # Ева должна помнить, какая длина соответствует этому start_idx в бинарном поиске?
                                    # Это сложно для пассивного режима без отслеживания состояния Боба.
                                    # УПРОЩЕНИЕ: Ева просто пытается применить бит флип, если это конец бин.поиска (len=1)
                                    # Но Ева не знает длину...
                                    pass
                                except: pass
                 time.sleep(0.1)

        # Возвращаем результат Евы
        return np.packbits(self.my_bits).tobytes(), 0


# ================= PLKG LOGIC =================
class PLKG_Logic:
    def __init__(self, name):
        self.name = name; self.raw_csi_data = []; self.key_raw_bytes = None; self.final_key = None; self.mean_amp = None

    def add_csi_packet(self, csi_str):
        try:
            cln = csi_str.replace('"', '').replace('[', '').replace(']', '').strip()
            if not cln: return
            arr = np.fromstring(cln, sep=',', dtype=int)
            if len(arr) > 0: self.raw_csi_data.append(arr)
        except: pass

    def generate_key(self):
        if len(self.raw_csi_data) < 10: return False
        valid = self.raw_csi_data[-200:]
        lns = [len(x) for x in valid]
        if not lns: return False
        clen = max(set(lns), key=lns.count)
        valid = [x for x in valid if len(x) == clen]
        mtx = np.abs(np.stack(valid)) if clen > 64 else np.stack(valid)
        parts = []
        max_idx = mtx.shape[1]
        for s in CSI_VALID_RANGES:
            if s.start < max_idx: parts.append(mtx[:, s])
        if not parts: return False
        
        mvec = np.mean(np.hstack(parts), axis=0); self.mean_amp = mvec
        idxs = [ALGO_M_NEIGHBORS + i*((len(mvec)-2*ALGO_M_NEIGHBORS)//ALGO_K_MAIN) for i in range(ALGO_K_MAIN)]
        th = np.percentile(mvec, [25, 50, 75])
        bits = []
        for ix in idxs:
            if ix >= len(mvec)-ALGO_M_NEIGHBORS: break
            win = 0; cnt = [0]*4; neig = mvec[ix-ALGO_M_NEIGHBORS:ix+ALGO_M_NEIGHBORS+1]
            for v in neig:
                if v<th[0]: cnt[0]+=1
                elif v<th[1]: cnt[1]+=1
                elif v<th[2]: cnt[3]+=1
                else: cnt[2]+=1
            win = cnt.index(max(cnt))
            bits.extend([0,0] if win==0 else [0,1] if win==1 else [1,0] if win==2 else [1,1])
            
        self.key_raw_bytes = np.packbits(np.array(bits, dtype=np.uint8)).tobytes()
        self.final_key = hashlib.sha256(self.key_raw_bytes).digest()
        return True

# ================= ESPDEVICE =================
class ESPDevice:
    def __init__(self, port, baud, name, filename):
        self.name = name; self.plkg = PLKG_Logic(name); self.ser = None
        self.port = port; self.baud = baud; self.file_h = open(filename, 'w', newline='')
        self.csv = csv.writer(self.file_h); self.running = False; self.lock = threading.Lock()
        self.captured_msgs = []; self.dbg = False

    def connect(self):
        try: self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except: sys.exit(1)

    def _send(self, cmd):
        if self.ser:
            with self.lock: self.ser.write(f"\n{cmd}\n".encode())
            time.sleep(0.05)

    def radio_init(self, c, bw, mac): self._send(f"radio_init -c {c} -b {bw} -m {mac} -s below --restart"); time.sleep(2.5)
    def start_recv(self, t, m): self._send(f"recv -t {t} -m {m}")
    def start_ping(self, t): self._send(f"ping -t {t}")
    def msg_listen(self): self._send("msg_listen")
    def msg_send(self, txt, dest): self._send(f'msg_send -m {dest} "{txt}"')

    def listen(self):
        self.running = True
        self.csv.writerow(["ts", "type", "data"])
        while self.running:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode('utf-8', errors='replace').strip()
                    if not line: continue
                    ts = time.time()
                    if not self.dbg and len(line)>5: print(f"[{self.name} RAW] {line[:60]}..."); self.dbg=True
                    
                    # 1. CSI
                    if "CSI_DATA" in line:
                        m = re.search(r'\[([0-9, \-]+)\]', line)
                        if m:
                            self.csv.writerow([ts, "CSI", m.group(1)])
                            self.plkg.add_csi_packet(m.group(1))

                    # 2. MESSAGES (Robust parsing)
                    # Ищем любые признаки сообщения
                    elif any(k in line for k in ["MSG_RECV", "Chat", "CAS_", "SECURE_MSG", "MSG from"]):
                        # Мы сохраняем ВСЮ строку, а парсить будем уже в классах через find()
                        # так надежнее, чем пытаться угадать формат кавычек здесь.
                        if "CAS_" in line or "SECURE_MSG" in line:
                            self.captured_msgs.append(line)
                        self.csv.writerow([ts, "MSG", line])

            except: break
    def close(self): self.running = False; self.ser.close(); self.file_h.close()

# ================= CRYPTO & CHAT =================
def encrypt_payload(key, text):
    cipher = AES.new(key, AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(text.encode())
    return (cipher.nonce + tag + ct).hex()

def decrypt_payload(key, hex_str):
    try:
        # Убираем пробелы и переносы
        hex_str = hex_str.strip()
        
        # Проверка длины
        if len(hex_str) % 2 != 0:
            print(f" [Decrypt Error] Odd hex length: {len(hex_str)}")
            return None
            
        data = bytes.fromhex(hex_str)
        
        # Проверка размера данных (Nonce 16 + Tag 16 = 32 байта минимум)
        if len(data) < 32:
            print(f" [Decrypt Error] Data too short: {len(data)} bytes")
            return None
            
        cipher = AES.new(key, AES.MODE_GCM, nonce=data[:16])
        return cipher.decrypt_and_verify(data[32:], data[16:32]).decode()
        
    except ValueError as e:
        print(f" [Decrypt Error] Value: {e}") # Скорее всего MAC check failed или non-hex
        return None
    except Exception as e:
        print(f" [Decrypt Error] {e}")
        return None

def plot_channels(alice, bob, eve):
    plt.figure(figsize=(10,6))
    if bob.plkg.mean_amp is not None: plt.plot(bob.plkg.mean_amp, 'b', label='Bob')
    if eve.plkg.mean_amp is not None: plt.plot(eve.plkg.mean_amp, 'r--', label='Eve')
    if alice.plkg.mean_amp is not None: plt.plot(alice.plkg.mean_amp, 'g:', label='Alice')
    plt.legend(); plt.show(block=False)

def start_interactive_chat(alice, bob):
    print("\n" + "="*50)
    print("### SECURE CHAT (ALICE -> BOB) ###")
    print(f"Alice KEY: {alice.plkg.final_key.hex()}")
    print("Type message to send. Type 'exit' to quit.")
    print("="*50 + "\n")
    
    bob.captured_msgs = []
    
    chatting = True
    def rx_loop():
        lidx = 0
        while chatting:
            if len(bob.captured_msgs) > lidx:
                for i in range(lidx, len(bob.captured_msgs)):
                    m = bob.captured_msgs[i]
                    if "SECURE_MSG" in m:
                        try:
                            # 1. Ищем старт payload
                            idx = m.find("SECURE_MSG:")
                            if idx != -1:
                                raw_p = m[idx+11:] # Пропускаем SECURE_MSG:
                                
                                # 2. Агрессивная чистка
                                # Оставляем только символы 0-9, a-f, A-F
                                hx = "".join([c for c in raw_p if c in "0123456789abcdefABCDEF"])
                                
                                # 3. Пытаемся расшифровать
                                res = decrypt_payload(bob.plkg.final_key, hx)
                                
                                if res:
                                    sys.stdout.write(f"\r\033[K[Bob] > {res}\nYou: ")
                                    sys.stdout.flush()
                                else:
                                    # Если не вышло, выводим диагностику
                                    print(f"\n[Bob DEBUG] Failed hex: {hx[:100]}... (Len: {len(hx)})")
                                    
                        except Exception as e:
                            print(f"Loop Err: {e}")
                lidx = len(bob.captured_msgs)
            time.sleep(0.1)

    t = threading.Thread(target=rx_loop, daemon=True)
    t.start()

    try:
        while True:
            txt = input("You: ")
            if txt in ["exit", "quit"]: break
            if not txt: continue
            
            enc = encrypt_payload(alice.plkg.final_key, txt)
            alice.msg_send(f"SECURE_MSG:{enc}", MAC_BOB)
            print(f"[Alice TX] Encrypted: {enc}")
            time.sleep(0.1)
    except KeyboardInterrupt: pass
    finally: chatting = False

# ================= MAIN =================
def main():
    alice = ESPDevice(ALICE_PORT, BAUD_RATE, "Alice", "alice.csv")
    bob   = ESPDevice(BOB_PORT,   BAUD_RATE, "Bob",   "bob.csv")
    eve   = ESPDevice(EVE_PORT,   BAUD_RATE, "Eve",   "eve.csv")
    devs = [alice, bob, eve]
    for d in devs: d.connect(); threading.Thread(target=d.listen, daemon=True).start()

    try:
        print("--- SETUP ---")
        alice._send(f"restart")
        bob._send(f"restart")
        eve._send(f"restart")
        time.sleep(5)

        alice.radio_init(WIFI_CHANNEL, WIFI_BANDWIDTH, MAC_ALICE)
        bob.radio_init(WIFI_CHANNEL, WIFI_BANDWIDTH, MAC_BOB)
        eve.radio_init(WIFI_CHANNEL, WIFI_BANDWIDTH, MAC_EVE)
        time.sleep(3)

        print("--- CSI ---")
        bob.start_recv(PHASE_DURATION+2, MAC_ALICE)
        eve.start_recv(PHASE_DURATION+2, MAC_ALICE)
        time.sleep(0.5); alice.start_ping(PHASE_DURATION); time.sleep(PHASE_DURATION+1)
        
        alice.start_recv(PHASE_DURATION+2, MAC_BOB)
        eve.start_recv(PHASE_DURATION+2, MAC_BOB)
        time.sleep(0.5); bob.start_ping(PHASE_DURATION); time.sleep(PHASE_DURATION+1)

        print("--- KEY GEN ---")
        alice.plkg.generate_key() 
        bob.plkg.generate_key() 
        eve.plkg.generate_key()
        plot_channels(alice, bob, eve)

        if alice.plkg.key_raw_bytes and bob.plkg.key_raw_bytes:
            print(f"RAW A: {alice.plkg.key_raw_bytes.hex().upper()}")
            print(f"RAW B: {bob.plkg.key_raw_bytes.hex().upper()}")
            print(f"RAW E: {eve.plkg.key_raw_bytes.hex().upper()}")

            print("--- NETWORK CASCADE ---")
            alice.msg_listen(); bob.msg_listen(); eve.msg_listen(); time.sleep(1)
            
            # Очистка старого мусора
            alice.captured_msgs = []; bob.captured_msgs = [] ; eve.captured_msgs = []

            ca = NetworkCascade(alice, "A", block_size=4) 
            ca.set_key(alice.plkg.key_raw_bytes)
            
            cb = NetworkCascade(bob, "B", block_size=4)
            cb.set_key(bob.plkg.key_raw_bytes)
            
            ce = PassiveEveCascade(eve, "E", block_size=4)
            ce.set_key(eve.plkg.key_raw_bytes)
            
            def run_alice(): 
                ca.start_alice(MAC_BOB, num_passes=10)
            
            def run_bob(): 
                nk, fix = cb.start_bob(MAC_ALICE, num_passes=10)
                print(f"BOB FIXED: {fix} bits")
                print(f"NEW KEY B: {nk.hex().upper()}")
                bob.plkg.final_key = hashlib.sha256(nk).digest()
            
            def run_eve():
                nk, _ = ce.start_passive_listen(num_passes=10)
                print(f"EVE NEW:   {nk.hex().upper()}")


            t1 = threading.Thread(target=run_alice)
            t2 = threading.Thread(target=run_bob)
            t3 = threading.Thread(target=run_eve)

            t1.start(); time.sleep(0.5); t2.start()
            t1.join(); t2.join()
            
            alice.plkg.final_key = hashlib.sha256(alice.plkg.key_raw_bytes).digest()

            print("--- CHAT MODE ---")
            start_interactive_chat(alice, bob)
        else: print("Key Gen Failed")

    except KeyboardInterrupt: pass
    finally:
        for d in devs: d.close()

if __name__ == "__main__":
    main()
