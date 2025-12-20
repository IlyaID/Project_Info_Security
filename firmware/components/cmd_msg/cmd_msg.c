/* cmd_msg.c */
#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "esp_wifi.h"
#include "esp_now.h"
#include "esp_mac.h"
#include "cmd_msg.h"
#include "cmd_radio.h"  /* <--- ОБЯЗАТЕЛЬНО */

static const char *TAG = "msg";

/* --- Callbacks --- */
static void msg_espnow_send_cb(const wifi_tx_info_t *tx_info, esp_now_send_status_t status)
{
    // Опционально: логировать результат отправки
    // ESP_LOGI(TAG, "Send status: %s", status == ESP_NOW_SEND_SUCCESS ? "OK" : "FAIL");
}

static void msg_espnow_recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (!info || len <= 0) return;
    
    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
             info->src_addr[0], info->src_addr[1], info->src_addr[2],
             info->src_addr[3], info->src_addr[4], info->src_addr[5]);

    /* Ограничиваем вывод */
    int print_len = (len > 255) ? 255 : len;
    printf("\n[MSG from %s]: %.*s%s\n", mac_str, print_len, data, (len > 255 ? "..." : ""));
}

/* --- Init --- */
static esp_err_t msg_ensure_init(void)
{
    /* 1. Гарантируем, что Wi-Fi и Netif подняты через центральный модуль radio */
    /* Используем дефолты, если еще не инициализировано. Если уже работает - не трогаем. */
    esp_err_t err = radio_init_csi_defaults(); 
    if (err != ESP_OK) return err;

    /* 2. Инициализируем ESP-NOW (безопасно повторно) */
    err = esp_now_init();
    if (err == ESP_ERR_ESPNOW_EXIST) {
        // Уже инициализирован, всё ок.
        // Но нужно проверить, зарегистрированы ли коллбеки. 
        // ESP-NOW не дает проверить регистрацию, поэтому просто перерегистрируем
        // (esp_now_register_... вернет ESP_OK или ESP_ERR_ESPNOW_INTERNAL если занято, не страшно)
    } else if (err != ESP_OK) {
        return err;
    }

    /* Регистрируем коллбеки (можно вызывать многократно, он перезапишет) */
    esp_now_register_send_cb(msg_espnow_send_cb);
    esp_now_register_recv_cb(msg_espnow_recv_cb);

    return ESP_OK;
}


/* --- Commands --- */

static int cmd_msg_listen(int argc, char **argv)
{
    (void)argc; (void)argv;
    esp_err_t err = msg_ensure_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Init failed: %s", esp_err_to_name(err));
        return 1;
    }
    printf("Message listening enabled (ESP-NOW)\n");
    return 0;
}

static struct {
    struct arg_str *mac;
    struct arg_str *text;
    struct arg_end *end;
} msg_send_args;

static int cmd_msg_send(int argc, char **argv)
{
    int nerrors = arg_parse(argc, argv, (void **) &msg_send_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, msg_send_args.end, argv[0]);
        return 1;
    }

    if (msg_ensure_init() != ESP_OK) return 1;

    const char *txt = msg_send_args.text->sval[0];
    size_t len = strlen(txt);
    
    uint8_t dest_mac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF}; // broadcast default
    if (msg_send_args.mac->count > 0) {
        int v[6];
        if (sscanf(msg_send_args.mac->sval[0], "%x:%x:%x:%x:%x:%x", 
                   &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) == 6) {
            for(int i=0; i<6; i++) dest_mac[i] = (uint8_t)v[i];
        } else {
            printf("Invalid MAC\n");
            return 1;
        }
    }

    /* Добавляем пира, если нет */
    if (!esp_now_is_peer_exist(dest_mac)) {
        esp_now_peer_info_t peer = {0};
        memcpy(peer.peer_addr, dest_mac, 6);
        peer.ifidx = WIFI_IF_STA;
        peer.channel = 0; // 0 = current channel
        peer.encrypt = false;
        esp_now_add_peer(&peer);
    }

    esp_now_send(dest_mac, (const uint8_t*)txt, len);
    printf("Sent: %s\n", txt);
    return 0;
}

void register_msg(void)
{
    const esp_console_cmd_t listen = { .command="msg_listen", .func=&cmd_msg_listen, .help="Start receiving msgs" };
    esp_console_cmd_register(&listen);

    msg_send_args.mac = arg_str0("m", "mac", "<aa:bb:...>", "Dest MAC");
    msg_send_args.text = arg_str1(NULL, NULL, "<text>", "Message");
    msg_send_args.end = arg_end(2);

    const esp_console_cmd_t send = { .command="msg_send", .func=&cmd_msg_send, .argtable=&msg_send_args, .help="Send msg" };
    esp_console_cmd_register(&send);
}
