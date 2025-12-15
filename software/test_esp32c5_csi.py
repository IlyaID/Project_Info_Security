#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрипт настройки и тестирования CSI на двух ESP32-C5
Версия с улучшенной обработкой ошибок
"""

import serial
import time
import subprocess
import sys
import os
import re
import signal
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

ESP_IDF_PATH = Path.home() / "esp" / "esp-idf"
PROJECT_PATH = Path.home() / "MIPT/Protect_information/Project/PLKG_on_ESP32/basic"
BUILD_PATH = PROJECT_PATH / "build"

ESP_UAV_PORT = "/dev/ttyUSB0"
ESP_IOT_PORT = "/dev/ttyUSB1"
BAUD_RATE = 115200

TEST_DURATION = 10
WIFI_CHANNEL = 11
SEND_FREQUENCY = 100

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def print_header(text):
    print(f"\n{Colors.HEADER}{'='*70}")
    print(f"  {text}")
    print(f"{'='*70}{Colors.ENDC}")

def print_step(step_num, text):
    print(f"\n{Colors.OKBLUE}[Шаг {step_num}] {text}{Colors.ENDC}")

def print_success(text):
    print(f"{Colors.OKGREEN}✓ {text}{Colors.ENDC}")

def print_error(text):
    print(f"{Colors.FAIL}✗ {text}{Colors.ENDC}")

def print_warning(text):
    print(f"{Colors.WARNING}⚠ {text}{Colors.ENDC}")

def kill_processes_on_port(port):
    """Убивает процессы, использующие порт"""
    try:
        # Находим процессы, использующие порт
        result = subprocess.run(
            ["lsof", "-t", port],
            capture_output=True,
            text=True
        )
        
        if result.stdout:
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                if pid:
                    print(f"  → Завершение процесса {pid} на {port}")
                    os.kill(int(pid), signal.SIGTERM)
                    time.sleep(0.5)
    except Exception as e:
        print_warning(f"Не удалось завершить процессы на {port}: {e}")

# ============================================================================
# КЛАСС ДЛЯ РАБОТЫ С ESP32
# ============================================================================

class ESP32Console:
    def __init__(self, port, name="ESP32"):
        self.port = port
        self.name = name
        self.serial = None
        
    def connect(self, retry=3):
        """Подключение к ESP32 с повторными попытками"""
        for attempt in range(retry):
            try:
                # Закрываем процессы на порту
                kill_processes_on_port(self.port)
                time.sleep(1)
                
                # Подключаемся
                self.serial = serial.Serial(
                    port=self.port,
                    baudrate=BAUD_RATE,
                    timeout=1,
                    write_timeout=1
                )
                
                time.sleep(2)
                self.serial.reset_input_buffer()
                self.serial.reset_output_buffer()
                
                # Проверяем подключение
                self.serial.write(b'\r\n')
                time.sleep(0.5)
                
                if self.serial.is_open:
                    print_success(f"{self.name} подключен к {self.port}")
                    return True
                    
            except serial.SerialException as e:
                print_warning(f"Попытка {attempt + 1}/{retry}: {e}")
                if attempt < retry - 1:
                    time.sleep(2)
                else:
                    print_error(f"Не удалось подключиться к {self.name}")
                    return False
            except Exception as e:
                print_error(f"Неожиданная ошибка подключения: {e}")
                return False
        
        return False
    
    def disconnect(self):
        """Отключение от ESP32"""
        if self.serial:
            try:
                if self.serial.is_open:
                    self.serial.close()
                print_success(f"{self.name} отключен")
            except Exception as e:
                print_warning(f"Ошибка при отключении {self.name}: {e}")
    
    def send_command(self, command, wait_response=True, timeout=5):
        """Отправка команды в консоль ESP32"""
        if not self.serial or not self.serial.is_open:
            print_error(f"{self.name} не подключен")
            return None
        
        try:
            self.serial.reset_input_buffer()
            
            cmd = command.strip() + '\r\n'
            self.serial.write(cmd.encode('utf-8'))
            self.serial.flush()
            print(f"  [{self.name}] → {command}")
            
            if not wait_response:
                return ""
            
            response = ""
            start_time = time.time()
            
            while (time.time() - start_time) < timeout:
                try:
                    if self.serial.in_waiting > 0:
                        chunk = self.serial.read(self.serial.in_waiting).decode('utf-8', errors='ignore')
                        response += chunk
                        
                        if "esp32c5>" in response.lower() or ">" in response:
                            break
                except serial.SerialException as e:
                    print_warning(f"Ошибка чтения: {e}")
                    break
                
                time.sleep(0.1)
            
            return response
            
        except Exception as e:
            print_error(f"Ошибка отправки команды {self.name}: {e}")
            return None
    
    def read_continuous(self, duration):
        """Непрерывное чтение данных с защитой от разрыва соединения"""
        if not self.serial or not self.serial.is_open:
            print_error(f"{self.name} не подключен")
            return []
        
        data = []
        start_time = time.time()
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        print(f"  [{self.name}] Чтение данных в течение {duration} сек...")
        
        while (time.time() - start_time) < duration:
            try:
                if self.serial.in_waiting > 0:
                    # Читаем байты вместо строки для надёжности
                    chunk = self.serial.read(self.serial.in_waiting)
                    text = chunk.decode('utf-8', errors='ignore')
                    
                    # Разбиваем на строки
                    lines = text.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line:
                            data.append(line)
                            
                            # Показываем прогресс
                            if line.startswith("CSI_DATA"):
                                print(f"  [{self.name}] CSI пакетов: {len([l for l in data if l.startswith('CSI_DATA')])}", end='\r')
                    
                    consecutive_errors = 0  # Сброс счётчика ошибок
                else:
                    time.sleep(0.1)
                    
            except serial.SerialException as e:
                consecutive_errors += 1
                print_warning(f"\n  [{self.name}] Ошибка чтения ({consecutive_errors}/{max_consecutive_errors}): {e}")
                
                if consecutive_errors >= max_consecutive_errors:
                    print_error(f"  [{self.name}] Слишком много ошибок, остановка чтения")
                    break
                
                time.sleep(0.5)
                
            except Exception as e:
                print_error(f"  [{self.name}] Неожиданная ошибка: {e}")
                break
        
        print(f"\n  [{self.name}] Получено всего строк: {len(data)}")
        print(f"  [{self.name}] CSI пакетов: {len([l for l in data if l.startswith('CSI_DATA')])}")
        
        return data

# ============================================================================
# ФУНКЦИИ ПРОШИВКИ
# ============================================================================

def check_idf_environment():
    print_step("ENV", "Проверка окружения ESP-IDF")
    
    if not ESP_IDF_PATH.exists():
        print_error(f"ESP-IDF не найден по пути: {ESP_IDF_PATH}")
        return False
    
    try:
        result = subprocess.run(
            ["idf.py", "--version"],
            capture_output=True,
            text=True,
            cwd=PROJECT_PATH
        )
        version = result.stdout.strip()
        print_success(f"ESP-IDF версия: {version}")
        return True
    except Exception as e:
        print_error(f"Ошибка проверки ESP-IDF: {e}")
        return False

def build_firmware():
    print_step("BUILD", "Сборка прошивки")
    
    if not PROJECT_PATH.exists():
        print_error(f"Проект не найден: {PROJECT_PATH}")
        return False
    
    try:
        print("  → Выполняется idf.py build...")
        result = subprocess.run(
            ["idf.py", "build"],
            cwd=PROJECT_PATH,
            capture_output=False
        )
        
        if result.returncode == 0:
            print_success("Прошивка собрана успешно")
            return True
        else:
            print_error("Ошибка сборки прошивки")
            return False
            
    except Exception as e:
        print_error(f"Исключение при сборке: {e}")
        return False

def flash_esp32(port, device_name):
    print(f"\n  → Прошивка {device_name} ({port})...")
    
    # Убиваем процессы на порту
    kill_processes_on_port(port)
    time.sleep(1)
    
    try:
        result = subprocess.run(
            ["idf.py", "-p", port, "flash"],
            cwd=PROJECT_PATH,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print_success(f"{device_name} прошит успешно")
            return True
        else:
            print_error(f"Ошибка прошивки {device_name}")
            if result.stderr:
                print(result.stderr[:500])
            return False
            
    except Exception as e:
        print_error(f"Исключение при прошивке {device_name}: {e}")
        return False

# ============================================================================
# ФУНКЦИИ ТЕСТИРОВАНИЯ
# ============================================================================

def test_basic_commands(esp):
    print(f"\n  → Тестирование базовых команд на {esp.name}...")
    
    # Простая команда Enter для проверки связи
    response = esp.send_command("")
    time.sleep(0.5)
    
    # Команда help
    response = esp.send_command("help", timeout=3)
    if response and ("ping" in response.lower() or "recv" in response.lower()):
        print_success(f"{esp.name}: Консоль работает")
        return True
    else:
        print_warning(f"{esp.name}: Консоль не отвечает корректно")
        return False

def parse_csi_data(lines):
    """Парсинг CSI данных из строк"""
    csi_packets = []
    
    for line in lines:
        if line.startswith("CSI_DATA"):
            parts = line.split(',')
            if len(parts) > 10:
                try:
                    packet = {
                        'count': int(parts[1]),
                        'mac': parts[2],
                        'rssi': int(parts[3]),
                        'rate': int(parts[4]),
                        'channel': int(parts[5]),
                        'timestamp': int(parts[6]),
                        'noise_floor': int(parts[7]),
                        'sig_len': int(parts[8]),
                        'rx_state': int(parts[9]),
                        'csi_len': int(parts[10]),
                        'csi_data': [int(x) for x in parts[11:] if x.strip().replace('-','').isdigit()]
                    }
                    csi_packets.append(packet)
                except (ValueError, IndexError):
                    continue
    
    return csi_packets

def visualize_csi(csi_packets, filename="csi_visualization.png"):
    if not csi_packets:
        print_warning("Нет CSI данных для визуализации")
        return
    
    print(f"\n  → Визуализация {len(csi_packets)} CSI пакетов...")
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('ESP32-C5 CSI Analysis', fontsize=16, fontweight='bold')
    
    timestamps = [p['count'] for p in csi_packets]
    rssi_values = [p['rssi'] for p in csi_packets]
    
    axes[0, 0].plot(timestamps, rssi_values, 'b-', linewidth=1.5)
    axes[0, 0].set_title('RSSI over Time')
    axes[0, 0].set_xlabel('Packet Number')
    axes[0, 0].set_ylabel('RSSI (dBm)')
    axes[0, 0].grid(True, alpha=0.3)
    
    axes[0, 1].hist(rssi_values, bins=30, color='green', alpha=0.7, edgecolor='black')
    axes[0, 1].set_title('RSSI Distribution')
    axes[0, 1].set_xlabel('RSSI (dBm)')
    axes[0, 1].set_ylabel('Count')
    axes[0, 1].grid(True, alpha=0.3)
    
    if csi_packets[0]['csi_data']:
        csi_data = csi_packets[0]['csi_data']
        axes[1, 0].plot(csi_data, 'r-', linewidth=1)
        axes[1, 0].set_title('CSI Data (First Packet)')
        axes[1, 0].set_xlabel('Subcarrier Index')
        axes[1, 0].set_ylabel('CSI Value')
        axes[1, 0].grid(True, alpha=0.3)
    
    max_packets = min(50, len(csi_packets))
    max_len = max(len(p['csi_data']) for p in csi_packets[:max_packets] if p['csi_data'])
    
    heatmap_data = np.zeros((max_packets, max_len))
    for i, packet in enumerate(csi_packets[:max_packets]):
        csi = packet['csi_data']
        heatmap_data[i, :len(csi)] = csi
    
    im = axes[1, 1].imshow(heatmap_data, aspect='auto', cmap='hot', interpolation='nearest')
    axes[1, 1].set_title(f'CSI Heatmap (First {max_packets} Packets)')
    axes[1, 1].set_xlabel('Subcarrier Index')
    axes[1, 1].set_ylabel('Packet Number')
    plt.colorbar(im, ax=axes[1, 1], label='CSI Value')
    
    plt.tight_layout()
    plt.savefig(filename, dpi=150)
    print_success(f"Визуализация сохранена: {filename}")
    plt.close()

def run_csi_test(esp_uav, esp_iot):
    print_step("TEST", "Запуск CSI теста")
    
    print(f"\n  → Подготовка устройств...")
    
    # Очистка буферов
    esp_uav.serial.reset_input_buffer()
    esp_iot.serial.reset_output_buffer()
    
    time.sleep(1)
    
    print(f"\n  → IoT начинает recv...")
    esp_iot.send_command(f"recv --timeout {TEST_DURATION}", wait_response=False)
    time.sleep(2)
    
    print(f"  → UAV начинает ping...")
    esp_uav.send_command(f"ping --timeout {TEST_DURATION}", wait_response=False)
    time.sleep(1)
    
    # Чтение данных с IoT
    print(f"\n  → Сбор CSI данных...")
    csi_lines = esp_iot.read_continuous(TEST_DURATION + 3)
    
    print("\n  → Завершение...")
    time.sleep(2)
    
    return csi_lines

# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    print_header("ESP32-C5 CSI Test Suite")
    
    skip_flash = "--skip-flash" in sys.argv
    skip_build = "--skip-build" in sys.argv
    
    if not check_idf_environment():
        return False
    
    if not skip_build:
        if not build_firmware():
            return False
    else:
        print_warning("Сборка пропущена (--skip-build)")
    
    if not skip_flash:
        print_step(3, "Прошивка устройств")
        
        if not flash_esp32(ESP_UAV_PORT, "UAV"):
            return False
        
        time.sleep(2)
        
        if not flash_esp32(ESP_IOT_PORT, "IoT"):
            return False
        
        time.sleep(3)
    else:
        print_warning("Прошивка пропущена (--skip-flash)")
    
    print_step(4, "Подключение к устройствам")
    
    esp_uav = ESP32Console(ESP_UAV_PORT, "UAV")
    esp_iot = ESP32Console(ESP_IOT_PORT, "IoT")
    
    if not esp_uav.connect():
        return False
    
    if not esp_iot.connect():
        esp_uav.disconnect()
        return False
    
    time.sleep(2)
    
    try:
        print_step(5, "Тест базовых команд")
        
        if not test_basic_commands(esp_uav):
            print_warning("UAV консоль не отвечает, продолжаем...")
        
        time.sleep(1)
        
        if not test_basic_commands(esp_iot):
            print_warning("IoT консоль не отвечает, продолжаем...")
        
        time.sleep(1)
        
        csi_lines = run_csi_test(esp_uav, esp_iot)
        
        print_step(7, "Анализ результатов")
        
        with open("csi_raw_data.txt", "w") as f:
            f.write("\n".join(csi_lines))
        print_success(f"Сырые данные: csi_raw_data.txt ({len(csi_lines)} строк)")
        
        csi_packets = parse_csi_data(csi_lines)
        print_success(f"CSI пакетов распознано: {len(csi_packets)}")
        
        if csi_packets:
            print(f"\n  Статистика CSI:")
            print(f"    - Всего пакетов: {len(csi_packets)}")
            print(f"    - RSSI: {min(p['rssi'] for p in csi_packets)} .. {max(p['rssi'] for p in csi_packets)} dBm")
            print(f"    - Средний RSSI: {sum(p['rssi'] for p in csi_packets) / len(csi_packets):.1f} dBm")
            print(f"    - CSI длина: {csi_packets[0]['csi_len']}")
            
            visualize_csi(csi_packets)
            
            print_header("ТЕСТ УСПЕШНО ЗАВЕРШЁН")
            print_success("✓ ESP32-C5 поддерживает CSI!")
            print_success(f"✓ Получено {len(csi_packets)} CSI пакетов")
            
        else:
            print_warning("CSI данные не получены!")
            print("Проверьте:")
            print("  - ESP32 рядом друг с другом")
            print("  - Прошивка корректна")
            print("  - Нет помех в WiFi")
        
    finally:
        print_step(8, "Отключение")
        esp_uav.disconnect()
        esp_iot.disconnect()
    
    return True

if __name__ == "__main__":
    print(f"\n{Colors.BOLD}Использование:{Colors.ENDC}")
    print("  python3 test_esp32c5_csi.py")
    print("  python3 test_esp32c5_csi.py --skip-build --skip-flash\n")
    
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print(f"\n\n{Colors.WARNING}⚠ Прервано{Colors.ENDC}")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n{Colors.FAIL}✗ Ошибка: {e}{Colors.ENDC}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
