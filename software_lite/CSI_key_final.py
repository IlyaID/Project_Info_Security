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
from Crypto.Cipher import AES

# ================= НАСТРОЙКИ =================
ALICE_PORT = "/dev/ttyUSB0" 
BOB_PORT   = "/dev/ttyUSB2"
EVE_PORT   = "/dev/ttyUSB1"
BAUD_RATE  = 460800 

WIFI_CHANNEL = 6
WIFI_BANDWIDTH = 40
MAC_ALICE    = "1a:00:00:00:00:01"
MAC_BOB      = "1a:00:00:00:00:02"
MAC_EVE      = "1a:00:00:00:00:66"
MAC_BROADCAST = "ff:ff:ff:ff:ff:ff"

PHASE_DURATION = 15
PING_RATE     = 100  # пакетов в секунду
CSI_VALID_RANGES = [slice(10, 60), slice(70, 118)]
ALGO_K_MAIN      = 16
ALGO_M_NEIGHBORS = 2

# ================= NETWORK CASCADE RECONCILIATION =================
class NetworkCascade:
    def __init__(self, device, role, block_size=4):
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
        idx = raw_msg.find(tag)
        if idx != -1: return raw_msg[idx:]
        return None

    # --- ALICE (Server) ---
    def start_alice(self, dest_mac, num_passes=6):
        n = len(self.my_bits)
        for pass_idx in range(num_passes):
            print(f"[Cas-Alice] Pass {pass_idx} Init")
            perm = list(range(n)); random.seed(pass_idx); random.shuffle(perm)
            bs = max(2, int(self.block_size * (1.5 ** pass_idx))) 
            
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
                    if "CAS_DONE" in found: break
                    
                    clean = self._extract_payload(found, f"CAS_REQ:{pass_idx}")
                    if clean:
                        try:
                            parts = clean.split(":")
                            if len(parts) >= 4:
                                s, l = int(parts[2]), int(parts[3])
                                req_ind = perm[s : s + l]
                                p = self.calculate_parity(req_ind)
                                self.device.msg_send(f"CAS_RESP:{pass_idx}:{s}:{p}", dest_mac)
                        except: pass
                time.sleep(0.01)
        print("[Cas-Alice] Finished")

    # --- BOB (Client) ---
    def start_bob(self, dest_mac, num_passes=6):
        n = len(self.my_bits)
        for pass_idx in range(num_passes):
            print(f"[Cas-Bob] Pass {pass_idx} Wait...")
            apar = []
            while True:
                found = False
                if self.device.captured_msgs:
                    for k, m in enumerate(self.device.captured_msgs):
                        tag = f"CAS_INIT:{pass_idx}:"
                        if tag in m:
                            clean = self._extract_payload(m, tag)
                            if clean:
                                try:
                                    parts = clean.split(":", 2)
                                    if len(parts) == 3:
                                        p_str = parts[2].strip()
                                        apar = [int(c) for c in p_str if c.isdigit()]
                                        self.device.captured_msgs.pop(k)
                                        found = True; break
                                except: pass
                if found: break
                time.sleep(0.1)
            
            if not apar:
                self.device.msg_send(f"CAS_DONE:{pass_idx}", dest_mac); continue

            perm = list(range(n)); random.seed(pass_idx); random.shuffle(perm)
            bs = max(2, int(self.block_size * (1.5 ** pass_idx))) 
            
            blk = 0
            for i in range(0, n, bs):
                ind = perm[i : i + bs]
                mp = self.calculate_parity(ind)
                if blk < len(apar) and mp != apar[blk]:
                    # print(f"[Cas-Bob] Fix Block {blk}")
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
                                        ap = int(parts[3]); fid = k; break
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
    def start_passive_listen(self, num_passes=6):
        print("[Eve] Passive listening started...")
        n = len(self.my_bits)
        
        for pass_idx in range(num_passes):
            perm = list(range(n)); random.seed(pass_idx); random.shuffle(perm)
            bs = max(2, int(self.block_size * (1.5 ** pass_idx))) 
            
            alice_parities = []
            t0 = time.time()
            # Ева ждет INIT (но не удаляет из буфера, просто читает)
            while time.time() - t0 < 8.0:
                if self.device.captured_msgs:
                    for m in self.device.captured_msgs:
                        print(f"[Eve Debug] MSG: {m[:60]}...")
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
            
            # Ева может попытаться использовать alice_parities, чтобы найти ошибки,
            # но исправить их без CAS_REQ она может только угадыванием (Brute Force в малых блоках).
            # В данном коде Ева просто собирает информацию.
            
            # Ждем окончания раунда
            time.sleep(2) 

        return np.packbits(self.my_bits).tobytes(), 0

# ================= keygen =================
class KEY_GEN_Logic:
    def __init__(self, name):
        self.name = name
        self.raw_csi_data = []
        self.key_raw_bytes = None
        self.final_key = None
        self.mean_amp = None

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
        lns = [len(x) for x in valid]; clen = max(set(lns), key=lns.count)
        valid = [x for x in valid if len(x) == clen]
        mtx = np.abs(np.stack(valid)) if clen > 64 else np.stack(valid)
        parts = []; max_idx = mtx.shape[1]
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

# ================= ESP Driver =================

class ESPDevice:
    def __init__(self, port, baud, name, filename):
        self.name = name
        self.keygen = KEY_GEN_Logic(name) 
        self.ser = None
        self.port = port
        self.baud = baud
        self.file_h = open(filename, 'w', newline='')
        self.csv = csv.writer(self.file_h)
        self.running = False; self.lock = threading.Lock()
        self.captured_msgs = []
        self.dbg = False
    
    def connect(self):
        try: self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except: sys.exit(1)
    
    def _send(self, cmd):
        if self.ser:
            with self.lock: self.ser.write(f"\n{cmd}\n".encode())
            time.sleep(0.05)
    def radio_init(self, c, bw, mac): 
        self._send(f"radio_init -c {c} -b {bw} -m {mac} -s below --restart"); time.sleep(2.5)
    
    def start_recv(self, timeout, source_mac): 
        self._send(f"recv -t {timeout} -m {source_mac}")
    
    def start_ping(self, timeout, rate=100, dest_mac="ff:ff:ff:ff:ff:ff"): 
        self._send(f"ping -t {timeout} -r {rate} -m {dest_mac}")
    
    def msg_listen(self): 
        self._send("msg_listen")
    
    def msg_send(self, txt, dest): 
        self._send(f'msg_send -m {dest} "{txt}"')
    
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
                    
                    if "CSI_DATA" in line:
                        m = re.search(r'\[([0-9, \-]+)\]', line)
                        if m: self.csv.writerow([ts, "CSI", m.group(1)]); self.keygen.add_csi_packet(m.group(1))
                    
                    elif any(k in line for k in ["MSG_RECV", "Chat", "CAS_", "SECURE_MSG", "MSG from"]):
                        # Надежный захват всей строки
                        if "CAS_" in line or "SECURE_MSG" in line: self.captured_msgs.append(line)
                        self.csv.writerow([ts, "MSG", line])
            except: break
    
    def close(self): 
        self.running = False; self.ser.close(); self.file_h.close()

# ================= CRYPTO & CHAT =================
def encrypt_payload(key, text):
    cipher = AES.new(key, AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(text.encode())
    return (cipher.nonce + tag + ct).hex()

def decrypt_payload(key, hex_str):
    try:
        hex_str = hex_str.strip()
        if len(hex_str) % 2 != 0: return None
        d = bytes.fromhex(hex_str)
        if len(d) < 32: return None
        return AES.new(key, AES.MODE_GCM, nonce=d[:16]).decrypt_and_verify(d[32:], d[16:32]).decode()
    except: return None

def plot_channels(alice, bob, eve):
    plt.figure(figsize=(10,6))
    if bob.keygen.mean_amp is not None: plt.plot(bob.keygen.mean_amp, 'b', label='Bob')
    if eve.keygen.mean_amp is not None: plt.plot(eve.keygen.mean_amp, 'r--', label='Eve')
    if alice.keygen.mean_amp is not None: plt.plot(alice.keygen.mean_amp, 'g:', label='Alice')
    plt.legend(); plt.show(block=False)

def start_interactive_chat(alice, bob):
    print("\n" + "="*50)
    print("### SECURE CHAT (ALICE -> BOB) ###")
    print(f"ALICE KEY: {alice.keygen.final_key.hex()[:50]}")
    print(f"BOB   KEY: {bob.keygen.final_key.hex()[:50]}")
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
                        idx = m.find("SECURE_MSG:")
                        if idx != -1:
                            raw_p = m[idx+11:]
                            # Чистим от всего кроме HEX
                            hx = "".join([c for c in raw_p if c in "0123456789abcdefABCDEF"])
                            res = decrypt_payload(bob.keygen.final_key, hx)
                            if res:
                                sys.stdout.write(f"\r\033[K[Bob] > {res}\n")
                                sys.stdout.flush()
                lidx = len(bob.captured_msgs)
            time.sleep(0.1)

    t = threading.Thread(target=rx_loop, daemon=True)
    t.start()

    try:
        while True:
            txt = input("You: ")
            if txt in ["exit", "quit"]: break
            if not txt: continue
            
            enc = encrypt_payload(alice.keygen.final_key, txt)
            alice.msg_send(f"SECURE_MSG:{enc}", MAC_BOB)
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
        time.sleep(3)

        alice.radio_init(WIFI_CHANNEL, WIFI_BANDWIDTH, MAC_ALICE)
        bob.radio_init(WIFI_CHANNEL, WIFI_BANDWIDTH, MAC_BOB)
        eve.radio_init(WIFI_CHANNEL, WIFI_BANDWIDTH, MAC_EVE)
        time.sleep(3)

        print("--- CSI ---")
        print("Starting CSI collection...")

        print("Alice -> Bob")
        bob.start_recv(PHASE_DURATION+2, MAC_ALICE)
        eve.start_recv(PHASE_DURATION+2, MAC_ALICE)
        time.sleep(0.5)
        alice.start_ping(PHASE_DURATION, rate=PING_RATE, dest_mac=MAC_BROADCAST)
        time.sleep(PHASE_DURATION+1)

        print("Bob -> Alice")
        alice.start_recv(PHASE_DURATION+2, MAC_BOB)
        eve.start_recv(PHASE_DURATION+2, MAC_BOB)
        time.sleep(0.5)
        bob.start_ping(PHASE_DURATION, rate=PING_RATE, dest_mac=MAC_BROADCAST)
        time.sleep(PHASE_DURATION+1)

        print("--- KEY GEN ---")
        alice.keygen.generate_key(); bob.keygen.generate_key(); eve.keygen.generate_key()
        plot_channels(alice, bob, eve)

        if alice.keygen.key_raw_bytes and bob.keygen.key_raw_bytes:
            print(f"RAW A: {alice.keygen.key_raw_bytes.hex().upper()}")
            print(f"RAW B: {bob.keygen.key_raw_bytes.hex().upper()}")
            print(f"RAW E: {eve.keygen.key_raw_bytes.hex().upper()}")

            print("--- NETWORK CASCADE ---")
            alice.msg_listen(); bob.msg_listen(); eve.msg_listen(); time.sleep(1)
            alice.captured_msgs = []; bob.captured_msgs = []; eve.captured_msgs = []

            # Инициализация Cascade
            ca = NetworkCascade(alice, "A", block_size=4)
            ca.set_key(alice.keygen.key_raw_bytes)
            
            cb = NetworkCascade(bob, "B", block_size=4)
            cb.set_key(bob.keygen.key_raw_bytes)
            
            ce = PassiveEveCascade(eve, "E", block_size=4)
            ce.set_key(eve.keygen.key_raw_bytes)
            
            def run_alice(): ca.start_alice(MAC_BOB, num_passes=6)
            def run_bob(): 
                nk, fix = cb.start_bob(MAC_ALICE, num_passes=6)
                print(f"BOB FIXED: {fix} bits")
                print(f"NEW KEY B: {nk.hex().upper()}")
                bob.keygen.final_key = hashlib.sha256(nk).digest()
            def run_eve():
                ne, _ =ce.start_passive_listen(num_passes=6)
                print(f"NEW KEY E: {ne.hex().upper()}")
            
            # Запуск потоков
            t1 = threading.Thread(target=run_alice)
            t2 = threading.Thread(target=run_bob)
            t3 = threading.Thread(target=run_eve)
            
            t1.start(); time.sleep(0.2); t2.start(); t3.start()
            t1.join(); t2.join(); t3.join()
            
            alice.keygen.final_key = hashlib.sha256(alice.keygen.key_raw_bytes).digest()

            print("--- CHAT MODE ---")
            start_interactive_chat(alice, bob)
        else: print("Key Gen Failed")

    except KeyboardInterrupt: pass
    finally:
        for d in devs: d.close()

if __name__ == "__main__":
    main()
