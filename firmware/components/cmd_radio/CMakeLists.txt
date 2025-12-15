idf_component_register(
    SRCS "cmd_radio.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event nvs_flash console
)
