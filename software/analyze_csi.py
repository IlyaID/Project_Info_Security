import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# ================= НАСТРОЙКИ =================
FILES = {
    "Alice": "csi_alice.csv",
    "Bob":   "csi_bob.csv",
    "Eve":   "csi_eve.csv"
}
USE_SUBCARRIERS = slice(6, 58)
# =============================================

def parse_csi_string(csi_str):
    try:
        arr = np.fromstring(csi_str, dtype=int, sep=',')
        complex_csi = arr[0::2] + 1j * arr[1::2]
        return np.abs(complex_csi)
    except:
        return None

def load_data(filename):
    print(f"Загрузка {filename}...")
    try:
        df = pd.read_csv(filename)
        df['amplitude'] = df['raw_data'].apply(parse_csi_string)
        df = df.dropna(subset=['amplitude'])
        return df
    except FileNotFoundError:
        print(f"⚠ Файл {filename} не найден!")
        return pd.DataFrame()

def get_channel_profile(amplitudes):
    if len(amplitudes) == 0: 
        return np.zeros(64), np.zeros(52, dtype=int)
    
    # 1. Определяем самую популярную длину пакета
    lengths = [len(a) for a in amplitudes]
    common_len = max(set(lengths), key=lengths.count)
    print(f"   -> Фильтрация: оставляем только пакеты длиной {common_len} (всего было {len(amplitudes)})")
    
    # 2. Фильтруем данные, оставляя только пакеты этой длины
    valid_amplitudes = [a for a in amplitudes if len(a) == common_len]
    
    # Если пакетов мало - выходим
    if len(valid_amplitudes) < 5:
        print("   ⚠ Слишком мало валидных пакетов!")
        return np.zeros(64), np.zeros(52, dtype=int)

    # 3. Создаем матрицу
    matrix = np.stack(valid_amplitudes)
    
    # 4. Адаптивный выбор поднесущих (slicing)
    # Если пакет длинный (128 или больше, HT40), берем широкий диапазон
    # Если короткий (64, HT20), берем узкий
    if common_len >= 128:
        # Для HT40 полезные данные где-то c 10 по 118
        current_slice = slice(10, 118)
    elif common_len >= 64:
        # Для HT20/Legacy полезные данные c 6 по 58
        current_slice = slice(6, 58)
    else:
        # Совсем короткий пакет, берем всё
        current_slice = slice(0, common_len)

    # Применяем срез
    try:
        matrix = matrix[:, current_slice]
    except IndexError:
        # Если срез не подошел, берем всё
        matrix = matrix

    # Усреднение
    mean_vec = np.mean(matrix, axis=0)
    
    # Проверка на нули (чтобы не делить на ноль при нормализации)
    std_val = np.std(mean_vec)
    if std_val == 0: std_val = 1.0
    
    # Нормализация
    norm_vec = (mean_vec - np.mean(mean_vec)) / std_val
    
    # Ключ
    key = (norm_vec > 0).astype(int)
    
    return norm_vec, key


def calculate_match(key1, key2):
    if len(key1) == 0 or len(key2) == 0: return 0.0
    min_len = min(len(key1), len(key2))
    matches = np.sum(key1[:min_len] == key2[:min_len])
    return matches / min_len

def main():
    data = {}
    profiles = {}
    keys = {}

    # 1. Загрузка и генерация ключей
    for name, fname in FILES.items():
        df = load_data(fname)
        data[name] = df
        if not df.empty:
            prof, key = get_channel_profile(df['amplitude'])
            profiles[name] = prof
            keys[name] = key
        else:
            profiles[name] = []
            keys[name] = []

    # 2. Сравнение
    if len(keys["Alice"]) > 0 and len(keys["Bob"]) > 0:
        kmr_ab = calculate_match(keys["Alice"], keys["Bob"])
        print(f"\n🔹 Alice <-> Bob Match Rate: {kmr_ab*100:.2f}% (LEGITIMATE)")
    
    if len(keys["Eve"]) > 0:
        if len(keys["Alice"]) > 0:
            kmr_ae = calculate_match(keys["Alice"], keys["Eve"])
            print(f"🔸 Alice <-> Eve Match Rate: {kmr_ae*100:.2f}% (ATTACK)")
        
        if len(keys["Bob"]) > 0:
            kmr_be = calculate_match(keys["Bob"], keys["Eve"])
            print(f"🔸 Bob   <-> Eve Match Rate: {kmr_be*100:.2f}% (ATTACK)")

    # 3. Визуализация
    plt.figure(figsize=(14, 8))

    # --- График 1: Профили каналов ---
    plt.subplot(2, 1, 1)
    plt.title("Сравнение профилей канала (Channel State Information)")
    
    if len(profiles["Alice"]) > 0:
        plt.plot(profiles["Alice"], label='Alice', color='blue', linewidth=2)
    if len(profiles["Bob"]) > 0:
        plt.plot(profiles["Bob"], label='Bob', color='green', linestyle='--', linewidth=2)
    if len(profiles["Eve"]) > 0:
        plt.plot(profiles["Eve"], label='Eve (Eavesdropper)', color='red', linestyle=':', linewidth=2)
        
    plt.axhline(0, color='black', linewidth=0.5)
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.ylabel("Норм. Амплитуда")

    # --- График 2: Штрих-коды ключей ---
    plt.subplot(2, 1, 2)
    plt.title("Сгенерированные ключи")
    
    key_list = []
    labels = []
    
    if len(keys["Alice"]) > 0:
        key_list.append(keys["Alice"])
        labels.append("Alice")
    if len(keys["Bob"]) > 0:
        key_list.append(keys["Bob"])
        labels.append("Bob")
    if len(keys["Eve"]) > 0:
        key_list.append(keys["Eve"])
        labels.append("Eve")

    if key_list:
        plt.imshow(key_list, aspect='auto', cmap='binary', interpolation='nearest')
        plt.yticks(range(len(labels)), labels)
        plt.xlabel("Индекс бита")

    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    main()


 