#include <stdio.h>
#include <string.h>
#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_timer.h"

#include "cmd_csi_recv.h"
#include "cmd_radio.h"

static const char *TAG = "csi_recv";

static bool s_filter_enabled = false;
static uint8_t s_target_mac[6] = {0};
static int64_t s_end_time = 0;
static bool s_is_running = false;

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;
    if (!s_is_running) return;

    /* Check timeout */
    if (s_end_time > 0 && esp_timer_get_time() > s_end_time) {
        s_is_running = false;
        ESP_LOGI(TAG, "CSI Timeout reached.");
        return;
    }

    if (!info || !info->buf) return;

    /* Filter */
    if (s_filter_enabled) {
        if (memcmp(info->mac, s_target_mac, 6) != 0) return;
    }

    static uint32_t s_count = 0;
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

    /* CSV Output */
    ets_printf("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,\"[%d",
               s_count++, MAC2STR(info->mac),
               rx_ctrl->rssi, rx_ctrl->rate, rx_ctrl->sig_mode, rx_ctrl->mcs, rx_ctrl->cwb,
               info->len, info->buf[0]);

    for (int i = 1; i < info->len; i++) {
        ets_printf(",%d", info->buf[i]);
    }
    ets_printf("]\"\n");
}

static void csi_init_once(void)
{
    static bool inited = false;
    if (inited) return;

    // Гарантируем настройки радио (канал 11 по умолчанию)
    radio_init_csi_defaults();

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    wifi_csi_config_t csi_config = {
        .lltf_en = true, .htltf_en = false, .stbc_htltf2_en = false,
        .ltf_merge_en = true, .channel_filter_en = true, .manu_scale = false, .shift = false
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
    inited = true;
}

static struct {
    struct arg_str *mac;
    struct arg_int *timeout;
    struct arg_end *end;
} recv_args;

static int task_csi_recv(int argc, char **argv)
{
    int nerrors = arg_parse(argc, argv, (void **) &recv_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, recv_args.end, argv[0]);
        return 1;
    }

    /* MAC Filter setup */
    if (recv_args.mac->count > 0) {
        int v[6];
        if (sscanf(recv_args.mac->sval[0], "%x:%x:%x:%x:%x:%x", 
                   &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) == 6) {
            for(int i=0; i<6; i++) s_target_mac[i] = (uint8_t)v[i];
            s_filter_enabled = true;
            printf("CSI Filter: " MACSTR "\n", MAC2STR(s_target_mac));
        } else {
            printf("Invalid MAC.\n");
            return 1;
        }
    } else {
        s_filter_enabled = false;
        printf("CSI Filter: DISABLED (Receiving ALL packets)\n");
    }

    /* Timeout setup */
    int t = (recv_args.timeout->count > 0) ? recv_args.timeout->ival[0] : 0;
    s_end_time = (t > 0) ? esp_timer_get_time() + (int64_t)t * 1000000LL : 0;
    
    s_is_running = true;
    csi_init_once();
    printf("CSI RX Started...\n");
    return 0;
}

void register_csi_recv(void)
{
    recv_args.mac = arg_str0("m", "mac", "<aa:bb...>", "Filter MAC");
    recv_args.timeout = arg_int0("t", "timeout", "<sec>", "Stop after N sec");
    recv_args.end = arg_end(2);

    const esp_console_cmd_t cmd = {
        .command = "recv",
        .help = "Start CSI RX",
        .func = &task_csi_recv,
        .argtable = &recv_args
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}
