/* Get Start Example (modified for idempotent Wi-Fi init via cmd_radio) */

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_now.h"
#include "esp_timer.h"

#include "cmd_csi_ping.h"
#include "cmd_radio.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL 11
#define CONFIG_SEND_FREQUENCY 100  // packets per second

static const char *TAG = "csi_send";

/* broadcast peer */
static const uint8_t BCAST_MAC[6] = {0xff,0xff,0xff,0xff,0xff,0xff};

static void espnow_once(void)
{
    static bool inited = false;
    if (inited) return;

    esp_err_t err = esp_now_init();
    if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
        ESP_ERROR_CHECK(err);
    }

    /* PMK можно ставить один раз */
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));

    if (!esp_now_is_peer_exist(BCAST_MAC)) {
        esp_now_peer_info_t peer = {0};
        memcpy(peer.peer_addr, BCAST_MAC, 6);
        peer.channel = CONFIG_LESS_INTERFERENCE_CHANNEL;
        peer.ifidx = WIFI_IF_STA;
        peer.encrypt = false;
        ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    }

    ESP_LOGI(TAG, "================ CSI SEND ================");
    ESP_LOGI(TAG, "wifi_channel: %d, send_frequency: %d", CONFIG_LESS_INTERFERENCE_CHANNEL, CONFIG_SEND_FREQUENCY);

    inited = true;
}

static bool csi_ping(int timeout_sec)
{
    /* Wi-Fi init/config один раз (idempotent) */
    ESP_ERROR_CHECK(radio_init_apply(CONFIG_LESS_INTERFERENCE_CHANNEL, "below", 40, NULL));
    espnow_once();

    int64_t t_end = esp_timer_get_time() + (int64_t)timeout_sec * 1000000LL;

    uint8_t counter = 0;
    while (esp_timer_get_time() < t_end) {
        esp_err_t ret = esp_now_send(BCAST_MAC, &counter, sizeof(counter));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "ESP-NOW send error: %s", esp_err_to_name(ret));
        }
        counter++;
        vTaskDelay(pdMS_TO_TICKS(1000 / CONFIG_SEND_FREQUENCY));
    }

    return true;
}

static int task_csi_ping(int argc, char **argv)
{
    static struct {
        struct arg_int *timeout;
        struct arg_end *end;
    } ping_args;

    ping_args.timeout = arg_int0(NULL, "timeout", "", "Ping runtime (seconds)");
    ping_args.end = arg_end(2);

    int nerrors = arg_parse(argc, argv, (void **) &ping_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, ping_args.end, argv[0]);
        return 1;
    }

    int t = ping_args.timeout->ival[0];
    printf("Ping time:%d\n", t);
    csi_ping(t);
    return 0;
}

void register_csi_ping(void)
{
    const esp_console_cmd_t cmd = {
        .command = "ping",
        .help = "Send ESP-NOW broadcast packets for N seconds (idempotent init)",
        .hint = NULL,
        .func = &task_csi_ping,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}
