/* Get Start Example (modified for idempotent Wi-Fi init via cmd_radio) */

#include <stdio.h>
#include <string.h>

#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"

#include "cmd_csi_recv.h"
#include "cmd_radio.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL 11
static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

static const char *TAG = "csi_recv";

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;

    if (!info || !info->buf) {
        ESP_LOGW(TAG, "<%s> wifi_csi_cb", esp_err_to_name(ESP_ERR_INVALID_ARG));
        return;
    }

    /* Фильтрация по MAC источника (как было у вас) */
    if (memcmp(info->mac, CONFIG_CSI_SEND_MAC, 6) != 0) {
        return;
    }

    static uint32_t s_count = 0;
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;

    if (!s_count) {
        ESP_LOGI(TAG, "================ CSI RECV ================");
        ets_printf("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data\n");
    }

    ets_printf("CSI_DATA,%d," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
               s_count++, MAC2STR(info->mac),
               rx_ctrl->rssi, rx_ctrl->rate, rx_ctrl->sig_mode,
               rx_ctrl->mcs, rx_ctrl->cwb, rx_ctrl->smoothing, rx_ctrl->not_sounding,
               rx_ctrl->aggregation, rx_ctrl->stbc, rx_ctrl->fec_coding, rx_ctrl->sgi,
               rx_ctrl->noise_floor, rx_ctrl->ampdu_cnt, rx_ctrl->channel, rx_ctrl->secondary_channel,
               rx_ctrl->timestamp, rx_ctrl->ant, rx_ctrl->sig_len, rx_ctrl->rx_state);

    ets_printf(",%d,%d,\"[%d", info->len, info->first_word_invalid, info->buf[0]);
    for (int i = 1; i < info->len; i++) {
        ets_printf(",%d", info->buf[i]);
    }
    ets_printf("]\"\n");
}

static void wifi_csi_init(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = false,
        .stbc_htltf2_en = false,
        .ltf_merge_en = true,
        .channel_filter_en = true,
        .manu_scale = false,
        .shift = false,
    };

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

static void csi_once(void)
{
    static bool inited = false;
    if (inited) {
        return;
    }

    /* Wi-Fi init/config один раз (idempotent) */
    ESP_ERROR_CHECK(radio_init_apply(CONFIG_LESS_INTERFERENCE_CHANNEL, "below", 40, NULL));

    wifi_csi_init();
    inited = true;
}

static int task_csi_recv(int argc, char **argv)
{
    static struct {
        struct arg_int *timeout;
        struct arg_end *end;
    } recv_args;

    recv_args.timeout = arg_int0(NULL, "timeout", "", "Recv time (seconds), CSI stays enabled after command");
    recv_args.end = arg_end(2);

    int nerrors = arg_parse(argc, argv, (void **) &recv_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, recv_args.end, argv[0]);
        return 1;
    }

    printf("Recv time:%d\n", recv_args.timeout->ival[0]);
    csi_once();
    return 0;
}

void register_csi_recv(void)
{
    /* как и было: просто регистрируем команду recv */
    const esp_console_cmd_t cmd = {
        .command = "recv",
        .help = "Enable CSI RX (idempotent). CSI remains enabled after command.",
        .hint = NULL,
        .func = &task_csi_recv,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}
