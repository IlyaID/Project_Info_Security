#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "esp_log.h"
#include "esp_err.h"
#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "cmd_radio.h"

static const char *TAG = "radio";

/* --- State --- */
static bool s_stack_inited = false;
static bool s_wifi_inited  = false;
static bool s_wifi_started = false;

/* --- Current Config --- */
static int s_channel = 11;
static wifi_second_chan_t s_second = WIFI_SECOND_CHAN_BELOW;
static wifi_bandwidth_t s_bw = WIFI_BW_HT40; // Храним текущую ширину
static bool s_mac_set = false;
static uint8_t s_mac[6] = {0};

/* --- Helpers --- */
static bool parse_mac_str(const char *str, uint8_t mac[6]) {
    int v[6];
    if (!str) return false;
    if (sscanf(str, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) return false;
    for (int i = 0; i < 6; i++) mac[i] = (uint8_t)v[i];
    return true;
}

static void mac_to_str(const uint8_t mac[6], char out[18]) {
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static wifi_second_chan_t parse_secondary(const char *s) {
    if (!s || strcmp(s, "none") == 0)  return WIFI_SECOND_CHAN_NONE;
    if (strcmp(s, "above") == 0)       return WIFI_SECOND_CHAN_ABOVE;
    if (strcmp(s, "below") == 0)       return WIFI_SECOND_CHAN_BELOW;
    return WIFI_SECOND_CHAN_NONE;
}

static wifi_bandwidth_t parse_bw(int bw_mhz) {
    return (bw_mhz >= 40) ? WIFI_BW_HT40 : WIFI_BW_HT20;
}

/* --- Public API Implementation --- */
wifi_bandwidth_t radio_get_bandwidth(void) {
    return s_bw;
}

static esp_err_t init_stack_once(void) {
    if (s_stack_inited) return ESP_OK;
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ret = esp_netif_init();
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) return ret;
    ret = esp_event_loop_create_default();
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) return ret;
    static bool netif_created = false;
    if (!netif_created) {
        esp_netif_create_default_wifi_sta();
        netif_created = true;
    }
    s_stack_inited = true;
    return ESP_OK;
}

static esp_err_t init_wifi_once(void) {
    if (s_wifi_inited) return ESP_OK;
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_err_t ret = esp_wifi_init(&cfg);
    if (ret != ESP_OK && ret != ESP_ERR_WIFI_INIT_STATE) return ret;
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    s_wifi_inited = true;
    return ESP_OK;
}

static esp_err_t apply_config_now(void) {
    if (s_mac_set) {
        esp_err_t err = esp_wifi_set_mac(WIFI_IF_STA, s_mac);
        if (err == ESP_ERR_WIFI_IF && s_wifi_started) {
            ESP_LOGW(TAG, "Changing MAC requires restart. Stopping WiFi...");
            esp_wifi_stop();
            s_wifi_started = false;
            ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, s_mac));
        } else if (err != ESP_OK) return err;
    }
    if (!s_wifi_started) {
        esp_err_t err = esp_wifi_start();
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err;
        s_wifi_started = true;
    }
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, s_bw));
    ESP_ERROR_CHECK(esp_wifi_set_channel(s_channel, s_second));
    return ESP_OK;
}

bool radio_is_inited(void) { return s_wifi_started; }

esp_err_t radio_init_apply(int channel, const char *secondary, int bw_mhz, const char *mac_str) {
    if (channel > 0) s_channel = channel;
    if (secondary)   s_second = parse_secondary(secondary);
    if (bw_mhz > 0)  s_bw = parse_bw(bw_mhz);
    if (mac_str) {
        uint8_t tmp[6];
        if (!parse_mac_str(mac_str, tmp)) return ESP_ERR_INVALID_ARG;
        memcpy(s_mac, tmp, 6);
        s_mac_set = true;
    }
    ESP_ERROR_CHECK(init_stack_once());
    ESP_ERROR_CHECK(init_wifi_once());
    esp_err_t err = apply_config_now();
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Radio applied: ch=%d sec=%d bw=%s mac_set=%d",
                 s_channel, (int)s_second, (s_bw == WIFI_BW_HT40 ? "HT40" : "HT20"), (int)s_mac_set);
    }
    return err;
}

esp_err_t radio_init_csi_defaults(void) {
    if (radio_is_inited()) return ESP_OK;
    return radio_init_apply(11, "below", 40, "1a:00:00:00:00:00");
}

/* ================== COMMANDS ================== */
static struct {
    struct arg_int *channel;
    struct arg_str *secondary;
    struct arg_int *bw;
    struct arg_str *mac;
    struct arg_lit *restart;
    struct arg_end *end;
} radio_args;

static int cmd_radio_init(int argc, char **argv) {
    int nerrors = arg_parse(argc, argv, (void **)&radio_args);
    if (nerrors) { arg_print_errors(stderr, radio_args.end, argv[0]); return 1; }
    if (radio_args.restart->count > 0 && s_wifi_started) {
        ESP_LOGI(TAG, "Stopping Wi-Fi (force restart)...");
        esp_wifi_stop(); s_wifi_started = false;
    }
    int ch = (radio_args.channel->count) ? radio_args.channel->ival[0] : -1;
    const char *sec = (radio_args.secondary->count) ? radio_args.secondary->sval[0] : NULL;
    int bw = (radio_args.bw->count) ? radio_args.bw->ival[0] : -1;
    const char *mac = (radio_args.mac->count) ? radio_args.mac->sval[0] : NULL;
    esp_err_t ret = radio_init_apply(ch, sec, bw, mac);
    if (ret != ESP_OK) { printf("radio_init failed: %s\n", esp_err_to_name(ret)); return 1; }
    return 0;
}

static int cmd_radio_info(int argc, char **argv) {
    (void)argc; (void)argv;
    char mac_str[18] = "not set";
    if (s_mac_set) mac_to_str(s_mac, mac_str);
    else {
        uint8_t now_mac[6];
        if (esp_wifi_get_mac(WIFI_IF_STA, now_mac) == ESP_OK) mac_to_str(now_mac, mac_str);
    }
    uint8_t prim=0; wifi_second_chan_t sec_d=0;
    if (s_wifi_started) esp_wifi_get_channel(&prim, &sec_d);
    printf("State: stack=%d wifi_init=%d wifi_start=%d\n", s_stack_inited, s_wifi_inited, s_wifi_started);
    printf("Config: ch=%d sec=%d bw=%s mac=%s\n", s_channel, (int)s_second, (s_bw==WIFI_BW_HT40?"HT40":"HT20"), mac_str);
    printf("Actual: ch=%d\n", prim);
    return 0;
}

static struct { struct arg_int *power; struct arg_end *end; } tx_power_args;
static int cmd_radio_tx_power(int argc, char **argv) {
    int nerrors = arg_parse(argc, argv, (void **)&tx_power_args);
    if (nerrors != 0) { arg_print_errors(stderr, tx_power_args.end, argv[0]); return 1; }
    if (!radio_is_inited()) { printf("Error: Radio not initialized.\n"); return 1; }
    if (tx_power_args.power->count > 0) {
        int dbm = tx_power_args.power->ival[0];
        int8_t power_unit = (int8_t)(dbm * 4); 
        ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(power_unit));
        printf("Set TX power to %d dBm\n", dbm);
    }
    int8_t cur_power = 0;
    esp_wifi_get_max_tx_power(&cur_power);
    printf("Current Max TX Power: %.2f dBm\n", cur_power * 0.25f);
    return 0;
}

static struct { struct arg_lit *passive; struct arg_end *end; } scan_args;
static int cmd_radio_scan(int argc, char **argv) {
    int nerrors = arg_parse(argc, argv, (void **)&scan_args);
    if (nerrors != 0) { arg_print_errors(stderr, scan_args.end, argv[0]); return 1; }
    if (!radio_is_inited()) radio_init_csi_defaults();
    wifi_scan_config_t scan_config = { .show_hidden = true, .scan_type = (scan_args.passive->count > 0) ? WIFI_SCAN_TYPE_PASSIVE : WIFI_SCAN_TYPE_ACTIVE, .scan_time.active.min = 100, .scan_time.active.max = 300, .scan_time.passive = 300 };
    printf("Starting scan...\n");
    ESP_ERROR_CHECK(esp_wifi_scan_start(&scan_config, true));
    uint16_t ap_count = 0;
    esp_wifi_scan_get_ap_num(&ap_count);
    if (ap_count == 0) { printf("No APs found.\n"); return 0; }
    wifi_ap_record_t *ap_list = (wifi_ap_record_t *)malloc(ap_count * sizeof(wifi_ap_record_t));
    if (!ap_list) return 1;
    ESP_ERROR_CHECK(esp_wifi_scan_get_ap_records(&ap_count, ap_list));
    printf("\nFound %d APs:\n", ap_count);
    for (int i = 0; i < ap_count; i++) {
        char bssid_str[18];
        snprintf(bssid_str, 18, "%02x:%02x:%02x:%02x:%02x:%02x", ap_list[i].bssid[0], ap_list[i].bssid[1], ap_list[i].bssid[2], ap_list[i].bssid[3], ap_list[i].bssid[4], ap_list[i].bssid[5]);
        printf("| %-32s | %s | %3d | %4d |\n", ap_list[i].ssid, bssid_str, ap_list[i].primary, ap_list[i].rssi);
    }
    free(ap_list);
    return 0;
}

void register_radio(void) {
    radio_args.channel = arg_int0("c", "channel", "<1..14>", "Channel");
    radio_args.secondary = arg_str0("s", "secondary", "<none|above|below>", "Secondary");
    radio_args.bw = arg_int0("b", "bw", "<20|40>", "Bandwidth");
    radio_args.mac = arg_str0("m", "mac", "<aa:bb:...>", "MAC addr");
    radio_args.restart = arg_lit0("r", "restart", "Force Wi-Fi restart");
    radio_args.end = arg_end(5);
    const esp_console_cmd_t init_cmd = { .command = "radio_init", .help = "Init/Configure Wi-Fi", .func = &cmd_radio_init, .argtable = &radio_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&init_cmd));
    const esp_console_cmd_t info_cmd = { .command = "radio_info", .help = "Show radio state", .func = &cmd_radio_info };
    ESP_ERROR_CHECK(esp_console_cmd_register(&info_cmd));
    tx_power_args.power = arg_int0("d", "dbm", "<8..20>", "Max TX power in dBm");
    tx_power_args.end = arg_end(1);
    const esp_console_cmd_t power_cmd = { .command = "tx_power", .help = "Get/Set TX power", .func = &cmd_radio_tx_power, .argtable = &tx_power_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&power_cmd));
    scan_args.passive = arg_lit0("p", "passive", "Passive scan");
    scan_args.end = arg_end(1);
    const esp_console_cmd_t scan_cmd = { .command = "scan", .help = "Scan Wi-Fi networks", .func = &cmd_radio_scan, .argtable = &scan_args };
    ESP_ERROR_CHECK(esp_console_cmd_register(&scan_cmd));
}
