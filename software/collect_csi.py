import serial
import time
import threading
import sys
import re
import csv

# ================= НАСТРОЙКИ =================
ALICE_PORT = "/dev/ttyUSB0"
BOB_PORT   = "/dev/ttyUSB2"
EVE_PORT   = "/dev/ttyUSB1"  # <--- Порт Евы
BAUD_RATE  = 921600
PHASE_DURATION = 50

FILES = {
    "Alice": "csi_alice.csv",
    "Bob":   "csi_bob.csv",
    "Eve":   "csi_eve.csv"
}
# =============================================

class ESPDevice:
    def __init__(self, port, baud, name, filename):
        self.port = port
        self.baud = baud
        self.name = name
        self.filename = filename
        self.ser = None
        self.running = False
        self.file_handle = open(filename, 'w', newline='')
        self.csv_writer = csv.writer(self.file_handle)
        self.csv_writer.writerow(["timestamp", "role", "raw_data"])
        self.packet_count = 0

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
            self.ser.reset_input_buffer()
            print(f"[{self.name}] Подключено к {self.port}")
        except Exception as e:
            print(f"[{self.name}] Ошибка ({self.port}): {e}")
            # Для Евы ошибка не критична, если тестируем без неё, 
            # но в данном контексте лучше упасть.
            sys.exit(1)

    def close(self):
        if self.ser: self.ser.close()
        if self.file_handle: self.file_handle.close()

    def send_cmd(self, cmd):
        if self.ser and self.ser.is_open:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\n").encode())
            print(f"[{self.name}] CMD: {cmd}")

    def listen_loop(self):
        self.running = True
        while self.running:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if "CSI_DATA" in line:
                        match = re.search(r'\"\[(.*?)\]\"', line)
                        if match:
                            raw_data = match.group(1)
                            timestamp = time.time()
                            self.csv_writer.writerow([timestamp, "RX", raw_data])
                            self.file_handle.flush()
                            self.packet_count += 1
            except Exception:
                break

def main():
    alice = ESPDevice(ALICE_PORT, BAUD_RATE, "Alice", FILES["Alice"])
    bob   = ESPDevice(BOB_PORT,   BAUD_RATE, "Bob",   FILES["Bob"])
    eve   = ESPDevice(EVE_PORT,   BAUD_RATE/2, "Eve",   FILES["Eve"])

    alice.connect()
    bob.connect()
    eve.connect()

    threads = [
        threading.Thread(target=d.listen_loop) for d in [alice, bob, eve]
    ]
    for t in threads: t.start()

    try:
        print("\n=== ПОДГОТОВКА (Сброс) ===")
        alice.send_cmd("restart")
        bob.send_cmd("restart")
        eve.send_cmd("restart")
        time.sleep(3)

        # Ева слушает ВСЕГДА
        print(">>> Ева начала прослушку...")
        eve.send_cmd("recv")
        time.sleep(0.5)

        # --- ФАЗА 1: Alice -> Bob ---
        print(f"\n>>> ФАЗА 1: Alice (TX) -> Bob (RX)")
        bob.send_cmd("recv")
        time.sleep(0.5)
        alice.send_cmd(f"ping --timeout {PHASE_DURATION}")
        
        for i in range(PHASE_DURATION + 1):
            sys.stdout.write(f"\rPkts: Bob={bob.packet_count} | Eve={eve.packet_count}")
            sys.stdout.flush()
            time.sleep(1)
        print("")

        alice.send_cmd("restart")
        bob.send_cmd("restart")
        # Еву не рестартим, пусть слушает дальше или перезапускаем recv
        # Для надежности рестартнем и её, чтобы разделить фазы (опционально)
        # eve.send_cmd("restart") 
        time.sleep(3)
        
        # Если Еву рестартили - включить снова
        # eve.send_cmd("recv")

        # --- ФАЗА 2: Bob -> Alice ---
        print(f"\n>>> ФАЗА 2: Bob (TX) -> Alice (RX)")
        alice.send_cmd("recv")
        time.sleep(0.5)
        bob.send_cmd(f"ping --timeout {PHASE_DURATION}")

        for i in range(PHASE_DURATION + 1):
            sys.stdout.write(f"\rPkts: Alice={alice.packet_count} | Eve={eve.packet_count}")
            sys.stdout.flush()
            time.sleep(1)
        print("")
        
        alice.send_cmd("restart")
        bob.send_cmd("restart")
        eve.send_cmd("restart")

    except KeyboardInterrupt:
        print("\nПрервано...")
    finally:
        alice.running = False
        bob.running = False
        eve.running = False
        for t in threads: t.join()
        alice.close()
        bob.close()
        eve.close()
        
    print(f"\nДанные сохранены: {list(FILES.values())}")

if __name__ == "__main__":
    main()
