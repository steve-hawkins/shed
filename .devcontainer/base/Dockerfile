FROM mcr.microsoft.com/devcontainers/dotnet:10.0-bookworm

# Install Python/pipx for Semgrep and curl/jq for verification scripts
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get -y install --no-install-recommends \
        pipx \
        jq \
        ripgrep \
        ca-certificates \
        iptables \
        ipset \
        dnsutils \
        sudo \
    && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install Gitleaks (secret scanning)
ARG GITLEAKS_VERSION=8.21.2
RUN ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then GL_ARCH="x64"; else GL_ARCH="$ARCH"; fi \
    && curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${GL_ARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks \
    && chmod +x /usr/local/bin/gitleaks

# Install Semgrep and Stryker.NET as the non-root vscode user
USER vscode
ENV PIPX_HOME=/home/vscode/.local/pipx \
    PIPX_BIN_DIR=/home/vscode/.local/bin \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    PATH="/home/vscode/.local/bin:/home/vscode/.dotnet/tools:${PATH}"

RUN pipx install semgrep \
    && dotnet tool install --global dotnet-stryker \
    && dotnet dev-certs https --trust || true

# Copy firewall script and allow vscode to run ONLY this script via sudo
COPY builder-agent/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod 0755 /usr/local/bin/init-firewall.sh \
    && chown root:root /usr/local/bin/init-firewall.sh \
    && echo "vscode ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/builder-firewall \
    && chmod 0440 /etc/sudoers.d/builder-firewall
