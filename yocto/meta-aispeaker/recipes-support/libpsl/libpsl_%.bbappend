# libpsl defaults to the ICU backend, which drags libicudata into the image —
# 32 MB of Unicode tables for a device that only needs public-suffix lookups
# for libsoup (GStreamer's souphttpsrc, i.e. internet radio). libidn2 does the
# same job with a fraction of the footprint.
PACKAGECONFIG = "idn2"
