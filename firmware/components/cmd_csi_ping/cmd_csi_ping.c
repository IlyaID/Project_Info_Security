#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_now.h"
#include "esp_wifi.h" 
#include "esp_timer.h"
#include "esp_mac.h"

#include "cmd_csi_ping.h"
#include "cmd_radio.h" // Нужен для radio_get_bandwidth

static const char *TAG = "csi_ping";

// Структура для аргументов консоли
static struct {
    struct arg_int *timeout;
    struct arg_int *rate; // Частота
    struct arg_str *mac;  // Целевой MAC
    struct arg_end *end;
} ping_args;

// Хелпер для парсинга MAC адреса из строки
static bool parse_mac_address(const char *str, uint8_t *mac) {
    unsigned int bytes[6];
    if (sscanf(str, "%x:%x:%x:%x:%x:%x", 
        &bytes[0], &bytes[1], &bytes[2], 
        &bytes[3], &bytes[4], &bytes[5]) == 6) {
        for (int i = 0; i < 6; i++) mac[i] = (uint8_t)bytes[i];
        return true;
    }
    return false;
}

// Инициализация ESP-NOW и добавление пира
static void ensure_peer_exists(const uint8_t *target_mac)
{
    /* 1. Инициализируем ESP-NOW */
    static bool espnow_inited = false;
    if (!espnow_inited) {
        // Мы НЕ вызываем radio_init_csi_defaults(), чтобы не сбросить настройки пользователя
        // Но проверяем, включен ли Wi-Fi
        if (!radio_is_inited()) {
            ESP_LOGW(TAG, "Radio not inited! Loading defaults.");
            radio_init_csi_defaults();
        }

        esp_err_t err = esp_now_init();
        if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
            ESP_LOGE(TAG, "ESP-NOW init failed: %s", esp_err_to_name(err));
            return;
        }
        ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
        espnow_inited = true;
    }

    /* 2. Если пир уже есть, выходим */
    if (esp_now_is_peer_exist(target_mac)) {
        return; 
    }

    /* 3. Добавляем пира */
    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, target_mac, 6);
    peer.channel = 0; // Использовать текущий канал
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;
    
    esp_err_t add_err = esp_now_add_peer(&peer);
    if (add_err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add peer: %s", esp_err_to_name(add_err));
        return;
    }

    /* 4. Динамическая настройка скорости на основе radio_init */
    wifi_bandwidth_t bw = radio_get_bandwidth();
    wifi_phy_mode_t phymode = (bw == WIFI_BW_HT40) ? WIFI_PHY_MODE_HT40 : WIFI_PHY_MODE_HT20;

    esp_now_rate_config_t rate_config = {
        .phymode = phymode, 
        .rate = WIFI_PHY_RATE_MCS0_SGI, // Фиксируем модуляцию на MCS0 для стабильного CSI
        .ersu = false, 
        .dcm = false
    };
    
    esp_err_t rate_err = esp_now_set_peer_rate_config(target_mac, &rate_config);
    if (rate_err != ESP_OK) {
         ESP_LOGW(TAG, "Rate config failed: %s", esp_err_to_name(rate_err));
    } else {
         ESP_LOGI(TAG, "Added peer "MACSTR" | Mode: %s MCS0", 
                  MAC2STR(target_mac), (bw == WIFI_BW_HT40) ? "HT40" : "HT20");
    }
}

static void csi_ping_loop(int timeout_sec, int rate_hz, const uint8_t *target_mac)
{
    ensure_peer_exists(target_mac);
    
    ESP_LOGI(TAG, "Ping -> "MACSTR" (%d s, %d Hz)...", 
             MAC2STR(target_mac), timeout_sec, rate_hz);

    int64_t t_end = esp_timer_get_time() + (int64_t)timeout_sec * 1000000LL;
    uint8_t seq = 0;
    
    if (rate_hz < 1) rate_hz = 1;
    TickType_t delay_ticks = pdMS_TO_TICKS(1000 / rate_hz);
    if (delay_ticks == 0) delay_ticks = 1;

    while (esp_timer_get_time() < t_end) {
        esp_err_t ret = esp_now_send(target_mac, &seq, 1);
        
        if (ret != ESP_OK && (seq % 100 == 0)) {
             ESP_LOGW(TAG, "Send error: %s", esp_err_to_name(ret));
        }
        seq++;
        vTaskDelay(delay_ticks);
    }
    ESP_LOGI(TAG, "Ping done.");
}

static int task_csi_ping(int argc, char **argv)
{
    int nerrors = arg_parse(argc, argv, (void **) &ping_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, ping_args.end, argv[0]);
        return 1;
    }

    int t = (ping_args.timeout->count) ? ping_args.timeout->ival[0] : 10;
    int r = (ping_args.rate->count)    ? ping_args.rate->ival[0]    : 100;
    
    uint8_t target_mac[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff}; // Default Broadcast
    if (ping_args.mac->count > 0) {
        if (!parse_mac_address(ping_args.mac->sval[0], target_mac)) {
            ESP_LOGE(TAG, "Invalid MAC. Use format xx:xx:xx:xx:xx:xx");
            return 1;
        }
    }

    csi_ping_loop(t, r, target_mac);
    return 0;
}

void register_csi_ping(void)
{
    ping_args.timeout = arg_int0("t", "timeout", "<sec>", "Duration (def: 10)");
    ping_args.rate    = arg_int0("r", "rate",    "<hz>",  "Rate Hz (def: 100)");
    ping_args.mac     = arg_str0("m", "mac",     "<mac>", "Target MAC (def: Broadcast)");
    ping_args.end     = arg_end(3);

    const esp_console_cmd_t cmd = {
        .command = "ping",
        .help = "Send CSI packets. Ex: ping -t 10 -r 100 -m 1a:00:00:00:00:02",
        .func = &task_csi_ping,
        .argtable = &ping_args
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}
