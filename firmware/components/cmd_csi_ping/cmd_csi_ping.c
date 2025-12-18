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

#include "cmd_csi_ping.h"
#include "cmd_radio.h"

#define PING_RATE_HZ 100
static const char *TAG = "csi_ping";
static const uint8_t BCAST_MAC[6] = {0xff,0xff,0xff,0xff,0xff,0xff};

static void espnow_init_reliable(void)
{
    static bool inited = false;
    if (inited) return;

    /* 1. Убеждаемся, что радио работает */
    radio_init_csi_defaults();

    /* 2. Инициализируем ESP-NOW */
    esp_err_t err = esp_now_init();
    if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
        ESP_LOGE(TAG, "ESP-NOW init failed: %s", esp_err_to_name(err));
        return;
    }
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));

    /* 3. Добавляем Broadcast Peer */
    if (!esp_now_is_peer_exist(BCAST_MAC)) {
        esp_now_peer_info_t peer = {0};
        memcpy(peer.peer_addr, BCAST_MAC, 6);
        peer.channel = 0; // На текущем канале
        peer.ifidx = WIFI_IF_STA;
        peer.encrypt = false;
        ESP_ERROR_CHECK(esp_now_add_peer(&peer));
        
        /* 4. ВАЖНО: Фиксируем скорость передачи (MCS0, HT40) для стабильного CSI */
        /* Это то, чего не хватало! */
        esp_now_rate_config_t rate_config = {
            .phymode = WIFI_PHY_MODE_HT40, 
            .rate = WIFI_PHY_RATE_MCS0_SGI, 
            .ersu = false, 
            .dcm = false
        };
        esp_err_t rate_err = esp_now_set_peer_rate_config(BCAST_MAC, &rate_config);
        if (rate_err != ESP_OK) {
             ESP_LOGW(TAG, "Rate config failed: %s (Check IDF version)", esp_err_to_name(rate_err));
        } else {
             ESP_LOGI(TAG, "Fixed Rate: HT40 MCS0 SGI");
        }
    }
    inited = true;
}

static void csi_ping_loop(int timeout_sec)
{
    espnow_init_reliable();
    
    ESP_LOGI(TAG, "Ping start (%d sec)...", timeout_sec);
    int64_t t_end = esp_timer_get_time() + (int64_t)timeout_sec * 1000000LL;
    uint8_t seq = 0;

    while (esp_timer_get_time() < t_end) {
        esp_err_t ret = esp_now_send(BCAST_MAC, &seq, 1);
        if (ret != ESP_OK && (seq % 100 == 0)) {
             // Логируем ошибку изредка
             ESP_LOGW(TAG, "Send error: %s", esp_err_to_name(ret));
        }
        seq++;
        vTaskDelay(pdMS_TO_TICKS(1000 / PING_RATE_HZ));
    }
    ESP_LOGI(TAG, "Ping done.");
}

static struct {
    struct arg_int *timeout;
    struct arg_end *end;
} ping_args;

static int task_csi_ping(int argc, char **argv)
{
    int nerrors = arg_parse(argc, argv, (void **) &ping_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, ping_args.end, argv[0]);
        return 1;
    }
    int t = (ping_args.timeout->count) ? ping_args.timeout->ival[0] : 10;
    csi_ping_loop(t);
    return 0;
}

void register_csi_ping(void)
{
    ping_args.timeout = arg_int0("t", "timeout", "<sec>", "Ping duration");
    ping_args.end = arg_end(2);

    const esp_console_cmd_t cmd = {
        .command = "ping",
        .help = "Send CSI generation packets",
        .func = &task_csi_ping,
        .argtable = &ping_args
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}
