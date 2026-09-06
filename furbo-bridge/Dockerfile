# Furbo Bridge add-on image.
#
# The TUTK/Kalay P2P library and the go2rtc binary are downloaded at BUILD time
# from their upstream projects; neither is committed to this repository, so
# nothing proprietary is redistributed here. Every download is pinned to an
# immutable source (a commit or a release tag) and verified against a recorded
# SHA-256, so a build either produces the exact bytes reviewed here or fails.
#
# The TUTK library is the same one docker-wyze-bridge ships. It is a third-party
# vendor SDK; confirm you are entitled to use and distribute it this way before
# publishing an image built from it (see furbo-bridge/DOCS.md).
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# TUTK P2P library, pinned to an immutable docker-wyze-bridge commit with a
# per-architecture checksum. Update the commit and both hashes together.
ARG TUTK_LIB_COMMIT="bf893749b748f142199c9bce14fac44f8a661d6e"
ARG TUTK_SHA256_AMD64="f7ec63dcb589e1b6bf0b8921af80d9d29ea0ddc4b2437170cbec28b08faa516e"
ARG TUTK_SHA256_ARM64="cc3542f50c0c3f433f7083de4666ed126bbe64e02a91753462c25af20e713737"

# go2rtc release, pinned to a tag with a per-architecture checksum.
ARG GO2RTC_VERSION="1.9.9"
ARG GO2RTC_SHA256_AMD64="aca941066c816cd69f4d689bf556f924057f5087449c2fdd7d5854d9aaec8ea9"
ARG GO2RTC_SHA256_ARM64="d13d4d692833d9606ac0d0e25675bba58e804f3f401e4f70761c867f38d2777a"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

# TUTK 4.2 P2P library, picked and verified for the build architecture.
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64) arch=amd64; sha="$TUTK_SHA256_AMD64" ;; \
      aarch64) arch=arm64; sha="$TUTK_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    url="https://raw.githubusercontent.com/mrlt8/docker-wyze-bridge/${TUTK_LIB_COMMIT}/app/lib/lib.${arch}"; \
    curl -fsSL -o /usr/local/lib/libIOTCAPIs_ALL.so "$url"; \
    echo "${sha}  /usr/local/lib/libIOTCAPIs_ALL.so" | sha256sum -c -; \
    ldconfig

# go2rtc: turns the raw H.264 the P2P client emits into RTSP/WebRTC.
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64) arch=amd64; sha="$GO2RTC_SHA256_AMD64" ;; \
      aarch64) arch=arm64; sha="$GO2RTC_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${arch}"; \
    curl -fsSL -o /usr/local/bin/go2rtc "$url"; \
    echo "${sha}  /usr/local/bin/go2rtc" | sha256sum -c -; \
    chmod +x /usr/local/bin/go2rtc

# Install the runtime deps from a fully hash-locked file (all transitive
# packages pinned with SHA-256), so a compromised index cannot substitute a
# different wheel. Regenerate with:
#   uv pip compile furbo-bridge/requirements.txt --generate-hashes --universal \
#     -o furbo-bridge/requirements.lock
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock

COPY src/ /app/
COPY go2rtc.yaml /app/go2rtc.yaml
COPY stream.sh /app/stream.sh
COPY talk.sh /app/talk.sh
RUN chmod +x /app/stream.sh /app/talk.sh
COPY run.sh /run.sh
RUN chmod +x /run.sh

# /data is the add-on's persistent store; the cloud session lives there.
WORKDIR /data
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/run.sh"]
