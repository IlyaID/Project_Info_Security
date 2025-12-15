import sys
from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from PyQt5.QtWidgets import *
from PyQt5.QtGui import QFont, QTextCursor
import serial
import time
import re
import numpy as np
import math

# Импорт модулей PLKG из репозитория
try:
    from datastream import load
    from plkg import greycode_quantization, sha256, aes, ecc
except ImportError:
    print("Убедитесь, что папки datastream и plkg находятся в том же каталоге")
    sys.exit(1)

# Консольные команды ESP32
CMD_PING = "ping --timeout={timeout} "
CMD_RECV = "recv --timeout={timeout} "
CMD_CHECK = "restart"
CMD_RESTART = "restart"
CMD_HELP = "help"
CMD_WIFI_SCAN = "wifi_scan"

# Статусы
prompt_action = '[ACTION]'
prompt_status = '[STATUS]'
prompt_fail = '[FAIL]'
prompt_success = '[SUCCESS]'


class ESP32Device:
    """Класс для работы с ESP32 через Serial"""
    
    def __init__(self, port, baudrate=921600):
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self.buffer = ""
        
    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
            time.sleep(2)
            return True
        except Exception as e:
            raise Exception(f"Не удалось подключиться к {self.port}: {str(e)}")
    
    def disconnect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
    
    def send_command(self, command):
        if self.ser and self.ser.is_open:
            self.ser.write((command + "\n").encode())
            time.sleep(0.1)
    
    def read_output(self, timeout=1):
        if not self.ser or not self.ser.is_open:
            return ""
        
        end_time = time.time() + timeout
        output = ""
        
        while time.time() < end_time:
            if self.ser.in_waiting:
                try:
                    data = self.ser.read(self.ser.in_waiting).decode('utf-8', errors='ignore')
                    output += data
                except:
                    pass
            time.sleep(0.05)
        
        return output
    
    def read_continuous(self):
        if self.ser and self.ser.is_open and self.ser.in_waiting:
            try:
                return self.ser.read(self.ser.in_waiting).decode('utf-8', errors='ignore')
            except:
                return ""
        return ""


class MonitorThread(QThread):
    """Поток для непрерывного мониторинга Serial порта"""
    data_received = pyqtSignal(str, str)
    
    def __init__(self, devices):
        super().__init__()
        self.devices = devices
        self.running = True
    
    def run(self):
        while self.running:
            for name, device in self.devices.items():
                data = device.read_continuous()
                if data:
                    self.data_received.emit(name, data)
            time.sleep(0.1)
    
    def stop(self):
        self.running = False


class CSICollectionThread(QThread):
    """Поток для автоматизированного сбора CSI"""
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    
    def __init__(self, devices, channel, frequency, timeout):
        super().__init__()
        self.devices = devices
        self.channel = channel
        self.frequency = frequency
        self.timeout = timeout
    
    def run(self):
        try:
            csi_data = {name: "" for name in self.devices.keys()}
            
            # 1. Alice начинает ping
            self.progress.emit("Alice начинает передачу CSI...")
            ping_cmd = CMD_PING.format(
                timeout=self.timeout,
                channel=self.channel,
                frequency=self.frequency
            )
            self.devices['alice'].send_command(ping_cmd)
            time.sleep(0.5)
            
            # 2. Bob и Eve начинают прием
            self.progress.emit("Bob и Eve начинают прослушивание...")
            recv_cmd = CMD_RECV.format(
                timeout=self.timeout,
                channel=self.channel
            )
            self.devices['bob'].send_command(recv_cmd)
            self.devices['eve'].send_command(recv_cmd)
            
            # 3. Сбор данных
            for i in range(self.timeout):
                self.progress.emit(f"Сбор CSI данных: {i+1}/{self.timeout} сек")
                
                for name, device in self.devices.items():
                    data = device.read_continuous()
                    if data:
                        csi_data[name] += data
                
                time.sleep(1)
            
            # 4. Дополнительное время на завершение
            time.sleep(2)
            for name, device in self.devices.items():
                data = device.read_continuous()
                if data:
                    csi_data[name] += data
            
            # 5. Извлечение CSI пакетов с новым форматом
            results = {}
            for name, data in csi_data.items():
                # Ищем все строки, начинающиеся с CSI_DATA
                csi_packets = [line for line in data.split('\n') if line.startswith('CSI_DATA')]
                
                results[name] = {
                    'raw_data': data,
                    'csi_packets': csi_packets,
                    'count': len(csi_packets)
                }
                self.progress.emit(f"{name.upper()}: собрано {len(csi_packets)} CSI пакетов")
            
            self.finished.emit(results)
            
        except Exception as e:
            self.progress.emit(f"[ОШИБКА] {str(e)}")


class KeyGenerationThread(QThread):
    """Поток для генерации ключей из CSI данных"""
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    
    def __init__(self, csi_results):
        super().__init__()
        self.csi_results = csi_results
    
    def run(self):
        try:
            keys = {}
            
            self.progress.emit("\n=== ЭТАП 1: Преобразование CSI данных ===")
            
            for name, result in self.csi_results.items():
                if result['count'] == 0:
                    self.progress.emit(f"⚠ {name.upper()}: нет CSI данных")
                    continue
                
                self.progress.emit(f"Обработка {name.upper()}: {result['count']} пакетов...")
                
                # Преобразование CSI с robust обработкой
                try:
                    from datastream import load
                    csi_transformed = load.transform(result['csi_packets'])
                except Exception as e:
                    self.progress.emit(f"⚠ {name.upper()}: ошибка преобразования - {str(e)}")
                    continue
                
                if len(csi_transformed) == 0:
                    self.progress.emit(f"⚠ {name.upper()}: все пакеты повреждены")
                    continue
                
                self.progress.emit(f"  {name.upper()}: успешно обработано {len(csi_transformed)} пакетов")
                
                # Нормализация длины массивов - выравнивание
                min_length = min(len(arr) for arr in csi_transformed)
                csi_normalized = [arr[:min_length] for arr in csi_transformed]
                
                # Преобразование в numpy array
                try:
                    csi_array = np.array(csi_normalized)
                    csi_avg = np.mean(csi_array, axis=0)
                    
                    self.progress.emit(f"  {name.upper()}: средняя амплитуда = {np.mean(csi_avg):.2f}")
                    
                    keys[name] = {
                        'csi_transformed': csi_normalized,
                        'csi_avg': csi_avg,
                        'packet_count': len(csi_normalized)
                    }
                except Exception as e:
                    self.progress.emit(f"⚠ {name.upper()}: ошибка numpy - {str(e)}")
                    continue
            
            if len(keys) < 2:
                self.error.emit("Недостаточно валидных данных для генерации ключей")
                return
            
            self.progress.emit("\n=== ЭТАП 2: Квантование (Quantization) ===")
            
            for name, data in keys.items():
                try:
                    self.progress.emit(f"Квантование {name.upper()}...")
                    
                    from plkg import greycode_quantization
                    
                    # Квантование с использованием Grey Code
                    quantized = greycode_quantization.quantization_1(
                        data['csi_transformed'],
                        nbit=8,
                        qbit=4
                    )
                    
                    data['quantized'] = quantized
                    data['quantized_bits'] = len(quantized)
                    self.progress.emit(f"  {name.upper()}: {len(quantized)} бит")
                    
                except Exception as e:
                    self.progress.emit(f"⚠ {name.upper()}: ошибка квантования - {str(e)}")
                    continue
            
            self.progress.emit("\n=== ЭТАП 3: Усиление конфиденциальности ===")
            
            for name, data in keys.items():
                if 'quantized' not in data:
                    continue
                
                try:
                    self.progress.emit(f"SHA-256 хэширование {name.upper()}...")
                    
                    from plkg import sha256
                    key = sha256.sha_byte(data['quantized'])
                    
                    data['key'] = key
                    self.progress.emit(f"  {name.upper()} KEY: {key[:32]}...")
                    
                except Exception as e:
                    self.progress.emit(f"⚠ {name.upper()}: ошибка генерации ключа - {str(e)}")
                    continue
            
            self.progress.emit("\n=== ЭТАП 4: Сравнение ключей ===")
            
            # Сравнение Alice и Bob
            if 'alice' in keys and 'bob' in keys:
                if 'key' in keys['alice'] and 'key' in keys['bob']:
                    alice_key = keys['alice']['key']
                    bob_key = keys['bob']['key']
                    
                    if alice_key == bob_key:
                        self.progress.emit("✓ Alice и Bob: КЛЮЧИ СОВПАДАЮТ!")
                        keys['alice_bob_match'] = True
                    else:
                        self.progress.emit("✗ Alice и Bob: ключи различаются")
                        keys['alice_bob_match'] = False
                        
                        # BER
                        try:
                            alice_q = keys['alice']['quantized']
                            bob_q = keys['bob']['quantized']
                            min_len = min(len(alice_q), len(bob_q))
                            
                            errors = sum(a != b for a, b in zip(alice_q[:min_len], bob_q[:min_len]))
                            ber = errors / min_len if min_len > 0 else 0
                            keys['ber'] = ber
                            self.progress.emit(f"  BER: {ber*100:.2f}%")
                        except:
                            pass
            
            # Проверка Eve
            if 'eve' in keys and 'alice' in keys:
                if 'key' in keys['eve'] and 'key' in keys['alice']:
                    eve_key = keys['eve']['key']
                    alice_key = keys['alice']['key']
                    
                    if eve_key == alice_key:
                        self.progress.emit("⚠ Eve получила ключ (небезопасно!)")
                        keys['eve_success'] = True
                    else:
                        self.progress.emit("✓ Eve НЕ получила ключ (безопасно!)")
                        keys['eve_success'] = False
            
            self.progress.emit("\n=== Генерация ключей завершена ===")
            self.finished.emit(keys)
            
        except Exception as e:
            import traceback
            self.error.emit(f"Критическая ошибка: {str(e)}\n{traceback.format_exc()}")



class EncryptionTestThread(QThread):
    """Поток для тестирования шифрования"""
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    
    def __init__(self, keys, message):
        super().__init__()
        self.keys = keys
        self.message = message
    
    def run(self):
        try:
            results = {}
            
            if 'alice' not in self.keys or 'bob' not in self.keys:
                self.progress.emit("⚠ Нет ключей Alice или Bob для тестирования")
                return
            
            alice_key = self.keys['alice']['key']
            bob_key = self.keys['bob']['key']
            
            self.progress.emit(f"\n=== Тест шифрования AES ===")
            self.progress.emit(f"Исходное сообщение: '{self.message}'")
            
            # Alice шифрует
            self.progress.emit("\n[Alice] Шифрование сообщения...")
            encrypted = aes.encrypt(self.message, alice_key)
            results['encrypted'] = encrypted
            self.progress.emit(f"Зашифровано: {str(encrypted)[:60]}...")
            
            # Bob расшифровывает
            self.progress.emit("\n[Bob] Расшифровка сообщения...")
            decrypted_bob = aes.decrypt(encrypted, bob_key)
            results['decrypted_bob'] = decrypted_bob
            self.progress.emit(f"Расшифровано: '{decrypted_bob}'")
            
            if self.message == decrypted_bob:
                self.progress.emit("✓ Bob успешно расшифровал сообщение!")
                results['bob_success'] = True
            else:
                self.progress.emit("✗ Bob не смог расшифровать сообщение")
                results['bob_success'] = False
            
            # Eve пытается расшифровать
            if 'eve' in self.keys:
                self.progress.emit("\n[Eve] Попытка расшифровки...")
                eve_key = self.keys['eve']['key']
                
                try:
                    decrypted_eve = aes.decrypt(encrypted, eve_key)
                    results['decrypted_eve'] = decrypted_eve
                    
                    if decrypted_eve == self.message:
                        self.progress.emit("⚠ Eve УСПЕШНО расшифровала сообщение! (плохо)")
                        results['eve_success'] = True
                    else:
                        self.progress.emit("✓ Eve получила мусор вместо сообщения (хорошо)")
                        results['eve_success'] = False
                except Exception as e:
                    self.progress.emit("✓ Eve не смогла расшифровать (ошибка декодирования)")
                    results['eve_success'] = False
            
            self.progress.emit("\n=== Тест шифрования завершен ===")
            self.finished.emit(results)
            
        except Exception as e:
            self.progress.emit(f"[ОШИБКА] {str(e)}")


class ESP32_PLKG_GUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ESP32 PLKG Demo - Physical Layer Key Generation")
        self.setGeometry(50, 50, 1400, 900)
        
        self.devices = {}
        self.monitor_thread = None
        self.csi_results = {}
        self.keys = {}
        
        self.initUI()
    
    def initUI(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        
        # === Заголовок ===
        title = QLabel("🔐 ESP32-S3 Physical Layer Key Generation System")
        title.setFont(QFont("Arial", 16, QFont.Bold))
        title.setStyleSheet("color: #2196F3; padding: 10px;")
        main_layout.addWidget(title)
        
        # === Настройки подключения ===
        conn_group = QGroupBox("📡 Настройки подключения устройств")
        conn_layout = QGridLayout()
        
        conn_layout.addWidget(QLabel("🔵 Alice (Transmitter):"), 0, 0)
        self.alice_port = QLineEdit("/dev/ttyUSB0")
        conn_layout.addWidget(self.alice_port, 0, 1)
        
        conn_layout.addWidget(QLabel("🟢 Bob (Receiver):"), 0, 2)
        self.bob_port = QLineEdit("/dev/ttyUSB1")
        conn_layout.addWidget(self.bob_port, 0, 3)
        
        conn_layout.addWidget(QLabel("🔴 Eve (Eavesdropper):"), 0, 4)
        self.eve_port = QLineEdit("/dev/ttyUSB2")
        conn_layout.addWidget(self.eve_port, 0, 5)
        
        conn_layout.addWidget(QLabel("Baudrate:"), 1, 0)
        self.baudrate = QComboBox()
        self.baudrate.addItems(["115200", "921600", "460800"])
        conn_layout.addWidget(self.baudrate, 1, 1)
        
        conn_group.setLayout(conn_layout)
        main_layout.addWidget(conn_group)
        
        # === Параметры CSI ===
        csi_group = QGroupBox("📊 Параметры сбора CSI")
        csi_layout = QGridLayout()
        
        csi_layout.addWidget(QLabel("WiFi Channel:"), 0, 0)
        self.channel = QSpinBox()
        self.channel.setRange(1, 14)
        self.channel.setValue(6)
        csi_layout.addWidget(self.channel, 0, 1)
        
        csi_layout.addWidget(QLabel("Frequency (Hz):"), 0, 2)
        self.frequency = QSpinBox()
        self.frequency.setRange(1, 1000)
        self.frequency.setValue(100)
        csi_layout.addWidget(self.frequency, 0, 3)
        
        csi_layout.addWidget(QLabel("Timeout (sec):"), 0, 4)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 60)
        self.timeout.setValue(10)
        csi_layout.addWidget(self.timeout, 0, 5)
        
        csi_group.setLayout(csi_layout)
        main_layout.addWidget(csi_group)
        
        # === Кнопки управления ===
        btn_layout = QHBoxLayout()
        
        self.btn_connect = QPushButton("📡 Подключить")
        self.btn_connect.clicked.connect(self.connect_all_devices)
        self.btn_connect.setStyleSheet(self.get_button_style("#4CAF50"))
        btn_layout.addWidget(self.btn_connect)
        
        self.btn_check = QPushButton("✓ Проверить")
        self.btn_check.clicked.connect(self.check_all_devices)
        self.btn_check.setEnabled(False)
        btn_layout.addWidget(self.btn_check)
        
        self.btn_collect = QPushButton("📊 Собрать CSI")
        self.btn_collect.clicked.connect(self.auto_collect_csi)
        self.btn_collect.setEnabled(False)
        self.btn_collect.setStyleSheet(self.get_button_style("#2196F3"))
        btn_layout.addWidget(self.btn_collect)
        
        self.btn_generate = QPushButton("🔑 Генерировать ключи")
        self.btn_generate.clicked.connect(self.generate_keys)
        self.btn_generate.setEnabled(False)
        self.btn_generate.setStyleSheet(self.get_button_style("#FF9800"))
        btn_layout.addWidget(self.btn_generate)
        
        self.btn_encrypt = QPushButton("🔒 Тест шифрования")
        self.btn_encrypt.clicked.connect(self.test_encryption)
        self.btn_encrypt.setEnabled(False)
        self.btn_encrypt.setStyleSheet(self.get_button_style("#9C27B0"))
        btn_layout.addWidget(self.btn_encrypt)
        
        self.btn_save = QPushButton("💾 Сохранить")
        self.btn_save.clicked.connect(self.save_all_data)
        self.btn_save.setEnabled(False)
        btn_layout.addWidget(self.btn_save)
        
        main_layout.addLayout(btn_layout)
        
        # === Сообщение для шифрования ===
        msg_layout = QHBoxLayout()
        msg_layout.addWidget(QLabel("Тестовое сообщение:"))
        self.message_input = QLineEdit("Секретное сообщение для Bob от Alice")
        msg_layout.addWidget(self.message_input)
        main_layout.addLayout(msg_layout)
        
        # === Консоли устройств ===
        consoles_layout = QHBoxLayout()
        
        # Alice
        alice_group = QGroupBox("🔵 Alice Console")
        alice_layout = QVBoxLayout()
        self.alice_console = QTextEdit()
        self.alice_console.setReadOnly(True)
        self.alice_console.setStyleSheet(self.get_console_style("#00d4ff"))
        alice_layout.addWidget(self.alice_console)
        
        alice_cmd_layout = QHBoxLayout()
        self.alice_cmd = QLineEdit()
        self.alice_cmd.setPlaceholderText("Команда...")
        self.alice_cmd.returnPressed.connect(lambda: self.send_manual_command('alice'))
        alice_cmd_layout.addWidget(self.alice_cmd)
        alice_send = QPushButton("Send")
        alice_send.clicked.connect(lambda: self.send_manual_command('alice'))
        alice_cmd_layout.addWidget(alice_send)
        alice_layout.addLayout(alice_cmd_layout)
        
        alice_group.setLayout(alice_layout)
        consoles_layout.addWidget(alice_group)
        
        # Bob
        bob_group = QGroupBox("🟢 Bob Console")
        bob_layout = QVBoxLayout()
        self.bob_console = QTextEdit()
        self.bob_console.setReadOnly(True)
        self.bob_console.setStyleSheet(self.get_console_style("#00ff00"))
        bob_layout.addWidget(self.bob_console)
        
        bob_cmd_layout = QHBoxLayout()
        self.bob_cmd = QLineEdit()
        self.bob_cmd.setPlaceholderText("Команда...")
        self.bob_cmd.returnPressed.connect(lambda: self.send_manual_command('bob'))
        bob_cmd_layout.addWidget(self.bob_cmd)
        bob_send = QPushButton("Send")
        bob_send.clicked.connect(lambda: self.send_manual_command('bob'))
        bob_cmd_layout.addWidget(bob_send)
        bob_layout.addLayout(bob_cmd_layout)
        
        bob_group.setLayout(bob_layout)
        consoles_layout.addWidget(bob_group)
        
        # Eve
        eve_group = QGroupBox("🔴 Eve Console")
        eve_layout = QVBoxLayout()
        self.eve_console = QTextEdit()
        self.eve_console.setReadOnly(True)
        self.eve_console.setStyleSheet(self.get_console_style("#ff0000"))
        eve_layout.addWidget(self.eve_console)
        
        eve_cmd_layout = QHBoxLayout()
        self.eve_cmd = QLineEdit()
        self.eve_cmd.setPlaceholderText("Команда...")
        self.eve_cmd.returnPressed.connect(lambda: self.send_manual_command('eve'))
        eve_cmd_layout.addWidget(self.eve_cmd)
        eve_send = QPushButton("Send")
        eve_send.clicked.connect(lambda: self.send_manual_command('eve'))
        eve_cmd_layout.addWidget(eve_send)
        eve_layout.addLayout(eve_cmd_layout)
        
        eve_group.setLayout(eve_layout)
        consoles_layout.addWidget(eve_group)
        
        main_layout.addLayout(consoles_layout)
        
        # === Системная консоль ===
        system_group = QGroupBox("🖥️ Системная консоль / Анализ PLKG")
        system_layout = QVBoxLayout()
        self.system_console = QTextEdit()
        self.system_console.setReadOnly(True)
        self.system_console.setMaximumHeight(200)
        self.system_console.setStyleSheet(self.get_console_style("#00ff00"))
        system_layout.addWidget(self.system_console)
        system_group.setLayout(system_layout)
        main_layout.addWidget(system_group)
        
        # === Статус бар ===
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("🔴 Устройства не подключены")
        
        # Словарь консолей
        self.consoles = {
            'alice': self.alice_console,
            'bob': self.bob_console,
            'eve': self.eve_console
        }
    
    def get_button_style(self, color):
        return f"""
            QPushButton {{
                background-color: {color};
                color: white;
                font-weight: bold;
                padding: 12px;
                font-size: 13px;
                border-radius: 5px;
            }}
            QPushButton:hover {{
                opacity: 0.8;
            }}
            QPushButton:disabled {{
                background-color: #cccccc;
                color: #666666;
            }}
        """
    
    def get_console_style(self, color):
        return f"""
            background-color: #1e1e1e;
            color: {color};
            font-family: 'Courier New';
            font-size: 10px;
        """
    
    def log_system(self, message, prefix="INFO"):
        timestamp = time.strftime("%H:%M:%S")
        colors = {
            "INFO": "#00ff00",
            "ERROR": "#ff0000",
            "WARNING": "#ffff00",
            "ACTION": "#00d4ff",
            "SUCCESS": "#00ff88"
        }
        color = colors.get(prefix, "#00ff00")
        
        self.system_console.append(
            f'<span style="color: gray;">[{timestamp}]</span> '
            f'<span style="color: {color};">[{prefix}]</span> '
            f'<span style="color: white;">{message}</span>'
        )
        self.system_console.moveCursor(QTextCursor.End)
    
    def log_device(self, device_name, text):
        console = self.consoles.get(device_name)
        if console:
            console.insertPlainText(text)
            console.moveCursor(QTextCursor.End)
    
    def connect_all_devices(self):
        try:
            self.log_system("Подключение к ESP32 устройствам...", "ACTION")
            
            baudrate = int(self.baudrate.currentText())
            
            self.devices['alice'] = ESP32Device(self.alice_port.text(), baudrate)
            self.devices['alice'].connect()
            self.log_system("✓ Alice подключена", "SUCCESS")
            
            self.devices['bob'] = ESP32Device(self.bob_port.text(), baudrate)
            self.devices['bob'].connect()
            self.log_system("✓ Bob подключен", "SUCCESS")
            
            self.devices['eve'] = ESP32Device(self.eve_port.text(), baudrate/2)
            self.devices['eve'].connect()
            self.log_system("✓ Eve подключена", "SUCCESS")
            
            # Мониторинг
            self.monitor_thread = MonitorThread(self.devices)
            self.monitor_thread.data_received.connect(self.log_device)
            self.monitor_thread.start()
            
            self.log_system("Все устройства успешно подключены!", "SUCCESS")
            self.status.showMessage("🟢 Все устройства подключены")
            
            self.btn_connect.setEnabled(False)
            self.btn_check.setEnabled(True)
            self.btn_collect.setEnabled(True)
            
        except Exception as e:
            self.log_system(f"Ошибка подключения: {str(e)}", "ERROR")
            self.status.showMessage("🔴 Ошибка подключения")
    
    def check_all_devices(self):
        self.log_system("Проверка связи...", "ACTION")
        for name, device in self.devices.items():
            device.send_command(CMD_CHECK)
    
    def send_manual_command(self, device_name):
        cmd_inputs = {
            'alice': self.alice_cmd,
            'bob': self.bob_cmd,
            'eve': self.eve_cmd
        }
        
        cmd_input = cmd_inputs.get(device_name)
        command = cmd_input.text().strip()
        
        if command and device_name in self.devices:
            self.devices[device_name].send_command(command)
            cmd_input.clear()
    
    def auto_collect_csi(self):
        self.log_system("Запуск автоматизированного сбора CSI...", "ACTION")
        self.btn_collect.setEnabled(False)
        self.status.showMessage("🔄 Сбор CSI данных...")
        
        self.collection_thread = CSICollectionThread(
            self.devices,
            self.channel.value(),
            self.frequency.value(),
            self.timeout.value()
        )
        
        self.collection_thread.progress.connect(lambda msg: self.log_system(msg, "INFO"))
        self.collection_thread.finished.connect(self.on_collection_finished)
        self.collection_thread.start()
    
    def on_collection_finished(self, results):
        self.csi_results = results
        
        self.log_system("\n=== РЕЗУЛЬТАТЫ СБОРА CSI ===", "SUCCESS")
        for name, data in results.items():
            self.log_system(f"{name.upper()}: {data['count']} CSI пакетов", "INFO")
        
        self.status.showMessage("✓ CSI данные собраны")
        self.btn_collect.setEnabled(True)
        self.btn_generate.setEnabled(True)
        self.btn_save.setEnabled(True)
    
    def generate_keys(self):
        if not self.csi_results:
            self.log_system("Сначала соберите CSI данные!", "ERROR")
            return
        
        self.log_system("\n" + "="*60, "ACTION")
        self.log_system("ЗАПУСК ПРОТОКОЛА PLKG", "ACTION")
        self.log_system("="*60 + "\n", "ACTION")
        
        self.btn_generate.setEnabled(False)
        self.status.showMessage("🔄 Генерация ключей...")
        
        self.keygen_thread = KeyGenerationThread(self.csi_results)
        self.keygen_thread.progress.connect(lambda msg: self.log_system(msg, "INFO"))
        self.keygen_thread.finished.connect(self.on_keygen_finished)
        self.keygen_thread.error.connect(lambda msg: self.log_system(msg, "ERROR"))
        self.keygen_thread.start()
    
    def on_keygen_finished(self, keys):
        self.keys = keys
        
        self.log_system("\n✓ Генерация ключей завершена!", "SUCCESS")
        self.status.showMessage("✓ Ключи сгенерированы")
        
        self.btn_generate.setEnabled(True)
        self.btn_encrypt.setEnabled(True)
    
    def test_encryption(self):
        if not self.keys:
            self.log_system("Сначала сгенерируйте ключи!", "ERROR")
            return
        
        message = self.message_input.text()
        if not message:
            self.log_system("Введите сообщение для шифрования!", "WARNING")
            return
        
        self.log_system("\n" + "="*60, "ACTION")
        self.btn_encrypt.setEnabled(False)
        self.status.showMessage("🔄 Тест шифрования...")
        
        self.encrypt_thread = EncryptionTestThread(self.keys, message)
        self.encrypt_thread.progress.connect(lambda msg: self.log_system(msg, "INFO"))
        self.encrypt_thread.finished.connect(self.on_encrypt_finished)
        self.encrypt_thread.start()
    
    def on_encrypt_finished(self, results):
        self.log_system("\n✓ Тест шифрования завершен!", "SUCCESS")
        self.status.showMessage("✓ Тест завершен")
        self.btn_encrypt.setEnabled(True)
    
    def save_all_data(self):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        
        # Сохранение CSI данных
        for name, data in self.csi_results.items():
            filename = f"csi_{name}_{timestamp}.txt"
            with open(filename, 'w') as f:
                f.write(data['raw_data'])
            self.log_system(f"✓ Сохранено: {filename}", "SUCCESS")
        
        # Сохранение ключей
        if self.keys:
            filename = f"keys_{timestamp}.txt"
            with open(filename, 'w') as f:
                for name, data in self.keys.items():
                    if isinstance(data, dict) and 'key' in data:
                        f.write(f"{name.upper()}_KEY: {data['key']}\n")
            self.log_system(f"✓ Ключи сохранены: {filename}", "SUCCESS")
        
        self.log_system("Все данные сохранены!", "SUCCESS")
    
    def closeEvent(self, event):
        if self.monitor_thread:
            self.monitor_thread.stop()
            self.monitor_thread.wait()
        
        for device in self.devices.values():
            device.disconnect()
        
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    window = ESP32_PLKG_GUI()
    window.show()
    
    sys.exit(app.exec_())
