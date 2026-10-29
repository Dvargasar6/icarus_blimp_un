idf_component_register(SRCS "target_main.c"
                            "blob_detect.c"
                            "tc_pipeline.c"
                            "blob_feat.c"
                            "color_lut.c"
                            "blob_clf.c"
                            "link.c"
                            "settings.c"
                       PRIV_REQUIRES esp_driver_gpio esp_driver_uart esp_timer nvs_flash
                                     esp_app_format
                       INCLUDE_DIRS ".")
