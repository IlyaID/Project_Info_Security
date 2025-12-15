idf_component_register(
    SRCS "cmd_msg.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event console nvs_flash cmd_radio
)
