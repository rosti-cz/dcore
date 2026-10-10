# Adapted from bootcrew/mono/debian/Containerfile; see UPSTREAM.md.
ARG DEBIAN_RELEASE=trixie
FROM docker.io/library/debian:${DEBIAN_RELEASE} AS base
ARG DEBIAN_RELEASE
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i 's/^Components: main$/Components: main contrib non-free-firmware/' /etc/apt/sources.list.d/debian.sources

FROM base AS builder
ARG BOOTC_REF=v1.17.1
ENV CARGO_HOME=/opt/cargo RUSTUP_HOME=/opt/rustup CARGO_BUILD_JOBS=2 \
    CARGO_PROFILE_RELEASE_DEBUG=0 CARGO_NET_GIT_FETCH_WITH_CLI=true
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git curl make build-essential go-md2man pkgconf \
    dracut libostree-dev ostree libssl-dev libselinux1-dev libclang-dev \
    libzstd-dev liblzma-dev libbz2-dev zlib1g-dev libpcre2-dev libfuse3-dev && \
    curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/rustup.sh && \
    sh /tmp/rustup.sh --profile minimal -y && rm /tmp/rustup.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
    meson ninja-build autoconf automake libtool bison flex libcap-dev libsystemd-dev
RUN --mount=type=bind,source=shared/build-ostree.sh,target=/build-ostree.sh /build-ostree.sh
WORKDIR /home/build
RUN --mount=type=bind,source=shared/build.sh,target=/build-bootc.sh \
    . "${CARGO_HOME}/env" && /build-bootc.sh

FROM base AS system
ARG DEBIAN_RELEASE
ARG INCUS_CHANNEL=stable
ARG RUSTIC_VERSION=latest
ARG RESTIC_VERSION=latest
ARG IMAGE_SOURCE=https://github.com/rosti-cz/dcore
ARG IMAGE_REVISION=local
LABEL containers.bootc="1" ostree.bootable="1" \
    org.opencontainers.image.title="dcore Debian bootc" \
    org.opencontainers.image.source="${IMAGE_SOURCE}" \
    org.opencontainers.image.revision="${IMAGE_REVISION}"
COPY rootfs/ /
RUN --mount=type=tmpfs,target=/tmp --mount=type=tmpfs,target=/boot \
    --mount=type=bind,source=scripts/install-system.sh,target=/install-system.sh \
    --mount=type=bind,source=scripts/install-backup-tools.sh,target=/install-backup-tools.sh \
    /install-system.sh && /install-backup-tools.sh
# Install the source-built boot stack after APT so Debian packages cannot overwrite it.
COPY --from=builder /output/ /
RUN ldconfig
RUN --mount=type=tmpfs,target=/tmp --mount=type=tmpfs,target=/boot \
    --mount=type=bind,source=scripts/configure-system.sh,target=/configure-system.sh \
    --mount=type=bind,source=shared/initramfs.sh,target=/initramfs.sh \
    /configure-system.sh && /initramfs.sh
RUN --mount=type=bind,source=shared/bootc-rootfs.sh,target=/bootc-rootfs.sh \
    sed -i 's|^HOME=.*|HOME=/var/home|' /etc/default/useradd && \
    mkdir -p /usr/lib/opt /usr/lib/sysimage && \
    mv /opt/incus /usr/lib/opt/ && mv /var/lib/dpkg /usr/lib/sysimage/dpkg && \
    /bootc-rootfs.sh && \
    ln -sfn usr/lib/opt /opt && \
    mkdir -p /var/lib && ln -s /usr/lib/sysimage/dpkg /var/lib/dpkg && \
    find /run /tmp -mindepth 1 -maxdepth 1 ! -path /run/secrets ! -path /run/.containerenv -exec rm -rf -- {} +
RUN --mount=type=tmpfs,target=/run --mount=type=tmpfs,target=/tmp /usr/libexec/dcore/verify-image
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
