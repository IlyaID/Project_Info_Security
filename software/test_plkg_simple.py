import serial
import time
import threading
import sys
import re
import numpy as np

# ================= НАСТРОЙКИ =================
ALICE_PORT = "/dev/ttyUSB0"    # Порт Алисы
BOB_PORT =   "/dev/ttyUSB1"    # Порт Боба
BAUD_RATE = 921600
PHASE_DURATION = 10            # Длительность каждой фазы (сек)
# =============================================

class CSIContainer:
    def __init__(self):
        self.packets = [] # Список амплитуд [amp_vector, amp_vector, ...]
        self.timestamps = []
        self.lock = threading.Lock()

class ESPDevice:
    def __init__(self, port, baud, name):
        self.port = port
        self.baud = baud
        self.name = name
        self.container = CSIContainer()
        self.ser = None
        self.running = False

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
            self.ser.reset_input_buffer()
            print(f"[{self.name}] Подключено к {self.port}")
        except Exception as e:
            print(f"[{self.name}] Ошибка подключения: {e}")
            sys.exit(1)

    def close(self):
        if self.ser: self.ser.close()

    def send_cmd(self, cmd):
        if self.ser and self.ser.is_open:
            # Сброс буфера перед новой командой важен
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\n").encode())
            print(f"[{self.name}] CMD: {cmd}")

    def stop_current_task(self):
        # Перезагрузка - самый надежный способ остановить ping/recv и очистить состояние
        self.send_cmd("restart")
        time.sleep(2) # Ждем загрузки

    def parse_csi_line(self, line):
        try:
            # Ищем массив CSI
            match = re.search(r'\"\[(.*?)\]\"', line)
            if not match: return
            
            raw_data_str = match.group(1)
            
            # Парсим I/Q
            csi_raw = np.fromstring(raw_data_str, dtype=int, sep=',')
            if len(csi_raw) == 0: return

            # Комплексные числа -> Амплитуда
            complex_csi = csi_raw[0::2] + 1j * csi_raw[1::2]
            amplitude = np.abs(complex_csi)
            
            # Убираем "выбросы" (пилотные поднесущие часто шумные)
            # Обычно в ESP32 64 поднесущих, полезные с 6 по 58 (примерно)
            # Оставим все для наглядности, но можно обрезать: amplitude[6:-6]
            
            with self.container.lock:
                self.container.packets.append(amplitude)
                self.container.timestamps.append(time.time())

        except Exception:
            pass

    def listen_loop(self):
        self.running = True
        while self.running:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if "CSI_DATA" in line:
                        self.parse_csi_line(line)
            except Exception:
                break

def generate_shared_key(data_a, data_b):
    """
    Генерирует ключ из последовательно собранных данных.
    Т.к. сбор был в разное время, мы не можем сопоставлять по Packet ID.
    Мы будем сравнивать статистические характеристики канала (Mean amplitude shape).
    """
    if not data_a or not data_b:
        return [], [], 0.0

    # Преобразуем в матрицы (Packet x Subcarrier)
    matrix_a = np.array(data_a)
    matrix_b = np.array(data_b)

    # Усредняем по времени (получаем "портрет" канала за фазу)
    # Это вектор длины 64 (количество поднесущих)
    mean_channel_a = np.mean(matrix_a, axis=0)
    mean_channel_b = np.mean(matrix_b, axis=0)

    # Нормализация (убираем влияние разной мощности передатчиков)
    mean_channel_a = (mean_channel_a - np.mean(mean_channel_a)) / np.std(mean_channel_a)
    mean_channel_b = (mean_channel_b - np.mean(mean_channel_b)) / np.std(mean_channel_b)

    # КВАНТОВАНИЕ (генерация битов)
    # Если поднесущая выше 0 -> 1, иначе -> 0
    key_a = (mean_channel_a > 0).astype(int)
    key_b = (mean_channel_b > 0).astype(int)

    # Сравнение
    matches = np.sum(key_a == key_b)
    kmr = matches / len(key_a)

    return key_a, key_b, kmr

def main():
    alice = ESPDevice(ALICE_PORT, BAUD_RATE, "Alice")
    bob = ESPDevice(BOB_PORT, BAUD_RATE, "Bob")

    alice.connect()
    bob.connect()

    # Запускаем фоновое чтение портов
    t1 = threading.Thread(target=alice.listen_loop)
    t2 = threading.Thread(target=bob.listen_loop)
    t1.start()
    t2.start()

    try:
        print("\n=== ПОДГОТОВКА УСТРОЙСТВ ===")
        alice.send_cmd("restart")
        bob.send_cmd("restart")
        time.sleep(3) # Ждем загрузки после рестарта

        # ================= ФАЗА 1: Alice -> Bob =================
        print(f"\n>>> ФАЗА 1: Alice PING -> Bob RECV ({PHASE_DURATION} сек)")
        
        # 1. Запускаем слушателя (Bob)
        bob.send_cmd("recv")
         
        
        # 2. Запускаем вещателя (Alice)
        alice.send_cmd(f"ping --timeout {PHASE_DURATION}") # timeout тут - кол-во пакетов
        time.sleep(PHASE_DURATION)
        # Ждем сбора
        for i in range(PHASE_DURATION):
            sys.stdout.write(f"\rBob collected: {len(bob.container.packets)}")
            sys.stdout.flush()
            time.sleep(1)
        print("")

        # Останавливаем активность (можно через restart, но попробуем просто подождать конца ping)
        # Для надежности лучше рестарт, чтобы очистить буферы WiFi
        alice.send_cmd("restart")
        bob.send_cmd("restart")
        time.sleep(2)

        # ================= ФАЗА 2: Bob -> Alice =================
        print(f"\n>>> ФАЗА 2: Bob PING -> Alice RECV ({PHASE_DURATION} сек)")
        
        # 1. Запускаем слушателя (Alice)
        alice.send_cmd("recv")
        

        # 2. Запускаем вещателя (Bob)
        bob.send_cmd(f"ping --timeout {PHASE_DURATION}")
        time.sleep(PHASE_DURATION)
        # Ждем сбора
        for i in range(PHASE_DURATION):
            sys.stdout.write(f"\rAlice collected: {len(alice.container.packets)}")
            sys.stdout.flush()
            time.sleep(1)
        print("")
        
        # Финиш
        alice.send_cmd("restart")
        bob.send_cmd("restart")

    except KeyboardInterrupt:
        print("\nПрервано...")
    finally:
        alice.running = False
        bob.running = False
        t1.join()
        t2.join()
        alice.close()
        bob.close()

    # ================= АНАЛИЗ =================
    print("\n=== ГЕНЕРАЦИЯ КЛЮЧЕЙ ===")
    
    cnt_a = len(alice.container.packets)
    cnt_b = len(bob.container.packets)
    
    print(f"Пакеты Alice (Rx): {cnt_a}")
    print(f"Пакеты Bob (Rx):   {cnt_b}")

    if cnt_a > 0 and cnt_b > 0:
        key_a, key_b, kmr = generate_shared_key(alice.container.packets, bob.container.packets)
        
        # Красивый вывод ключа (группировка по 4 бита)
        str_key_a = ''.join(map(str, key_a))
        str_key_b = ''.join(map(str, key_b))
        
        print(f"\nКлюч Alice: {str_key_a}")
        print(f"Ключ Bob:   {str_key_b}")
        print(f"Совпадение (KMR): {kmr*100:.1f}%")
        
        if kmr > 0.75:
            print("\n✅ УСПЕХ! Канал симметричен, ключи похожи.")
        else:
            print("\n⚠ НИЗКОЕ СОВПАДЕНИЕ. Возможные причины:")
            print("1. Слишком большая пауза между фазами (канал успел измениться).")
            print("2. Сильные помехи в одной из фаз.")
            print("3. Устройства перемещались во время теста.")
    else:
        print("\n❌ ОШИБКА: Одно из устройств не получило данные.")

if __name__ == "__main__":
    main()
