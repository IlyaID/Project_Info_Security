#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#include "esp_err.h"
#include "esp_wifi_types.h"

/**
 * Инициализирует (однократно) NVS + netif + default event loop + Wi-Fi,
 * затем применяет параметры радио.
 *
 * secondary: "none" | "above" | "below"
 * bw_mhz: 20 | 40
 * mac_str: "aa:bb:cc:dd:ee:ff" или NULL (не менять MAC)
 */
esp_err_t radio_init_apply(int channel,
                           const char *secondary,
                           int bw_mhz,
                           const char *mac_str);

/** Дефолт под ваш CSI-эксперимент: ch=11, HT40, secondary=below, MAC не меняем */
esp_err_t radio_init_csi_defaults(void);

bool radio_is_inited(void);

/** Консольные команды: radio_init, radio_info */
void register_radio(void);

wifi_bandwidth_t radio_get_bandwidth(void); 

#ifdef __cplusplus
}
#endif
