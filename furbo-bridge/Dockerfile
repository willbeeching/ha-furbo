# Furbo Bridge add-on image.
#
# The TUTK/Kalay P2P library and the go2rtc binary are downloaded at BUILD
# time from their upstream projects. Neither is committed to this repository,
# so nothing proprietary is redistributed here — the build machine fetches the
# library the same way docker-wyze-bridge does.
FROM python:3.12-slim

ARG TUTK_LIB_URL="https://raw.githubusercontent.com/mrlt8/docker-wyze-bridge/main/app/lib"
ARG GO2RTC_VERSION="1.9.9"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

# TUTK 4.2 P2P library, picked for the build architecture (amd64 or arm64).
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64) arch=amd64 ;; \
      aarch64) arch=arm64 ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/lib/libIOTCAPIs_ALL.so "${TUTK_LIB_URL}/lib.${arch}"; \
    ldconfig

# go2rtc: turns the raw H.264 the P2P client emits into RTSP/WebRTC.
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64) arch=amd64 ;; \
      aarch64) arch=arm64 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${arch}"; \
    chmod +x /usr/local/bin/go2rtc

RUN pip install --no-cache-dir aiohttp cryptography

COPY src/ /app/
COPY go2rtc.yaml /app/go2rtc.yaml
COPY run.sh /run.sh
RUN chmod +x /run.sh

# /data is the add-on's persistent store; the cloud session lives there.
WORKDIR /data
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/run.sh"]
