idf_component_register(
    SRCS "cmd_csi_ping.c"
    INCLUDE_DIRS .
    REQUIRES console esp_wifi cmd_radio esp_timer
)
