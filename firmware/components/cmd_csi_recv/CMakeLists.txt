idf_component_register(
    SRCS "cmd_csi_recv.c"
    INCLUDE_DIRS .
    REQUIRES console esp_wifi cmd_radio
)
