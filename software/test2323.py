from datastream import chat, csi_interface
from plkg import plkg
import time

# Настройка устройств
uav_com = csi_interface.com_esp('/dev/ttyUSB0', 921600)  # Ваш COM-порт UAV
iot_com = csi_interface.com_esp('/dev/ttyUSB2', 921600)  # Ваш COM-порт IoT

# Создание end-устройств
uav_device = plkg.end_device('U')  # UAV
iot_device = plkg.end_device('I')  # IoT device

# Настройка chat manager для обмена данными
alice_chat = chat.chat_manager("192.168.0.143")  # IP IoT устройства
bob_chat = chat.chat_manager("192.168.0.142")    # IP UAV

alice_chat.chat_init()
bob_chat.chat_init()

uav_device.set_chatmanager(alice_chat)
iot_device.set_chatmanager(bob_chat)

# Выполнение PLKG протокола
print("Запуск синхронизации времени...")
uav_device.time_synchronize()
iot_device.time_synchronize()

print("Запуск генерации ключа...")
uav_device.plkg()
iot_device.plkg()

# Получение ключей
uav_key = uav_device.key
iot_key = iot_device.key

print(f"UAV ключ: {uav_key}")
print(f"IoT ключ: {iot_key}")
print(f"Ключи совпадают: {uav_key == iot_key}")
