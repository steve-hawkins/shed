#!/usr/bin/env bash
set -euo pipefail

# Ensure required tools exist (install iptables/ipset/dnsutils in base/Dockerfile if missing)
for cmd in iptables ipset dig jq curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: Required firewall dependency '$cmd' is not installed." >&2
        exit 1
    fi
done

echo "Initializing Builder Agent egress firewall..."

# 1. Flush existing rules and destroy old ipsets
iptables -F OUTPUT
iptables -F INPUT
ipset destroy allowed-egress 2>/dev/null || true
ipset create allowed-egress hash:net

# 2. Allow loopback and already-established connections
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# 3. Allow Docker / Codespaces internal DNS (127.0.0.11) and host bridge subnet
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

HOST_IP=$(ip route | awk '/default/ {print $3}')
if [ -n "$HOST_IP" ]; then
    HOST_SUBNET=$(echo "$HOST_IP" | sed 's/\.[0-9]*$/.0\/24/')
    iptables -A OUTPUT -d "$HOST_SUBNET" -j ACCEPT
fi

# 4. Populate GitHub official IP ranges (API, Git, Web, Actions/Packages)
echo "Fetching GitHub CIDR ranges..."
GITHUB_RANGES=$(curl -sS https://api.github.com/meta | jq -r '(.web + .api + .git + .packages)[]' | sort -u)
for cidr in $GITHUB_RANGES; do
    # IPv4 only for iptables (skip IPv6 blocks)
    if [[ "$cidr" != *":"* ]]; then
        ipset add allowed-egress "$cidr" -exist
    fi
done

# Helper to extract host and optional port from a hostname or URL without logging secrets
extract_host() {
    local input="$1"
    # Strip scheme (http://, https://), userinfo (user:pass@), path (/...), and :port
    input="${input#*://}"
    input="${input##*@}"
    input="${input%%/*}"
    input="${input%%:*}"
    printf '%s' "$input"
}

extract_port() {
    local input="$1"
    local default_port="$2"
    local authority="${input#*://}"
    authority="${authority##*@}"
    authority="${authority%%/*}"
    if [[ "$authority" == *:* ]]; then
        printf '%s' "${authority##*:}"
    elif [[ "$input" == https://* ]]; then
        printf '%s' "443"
    elif [[ "$input" == http://* ]]; then
        printf '%s' "80"
    else
        printf '%s' "$default_port"
    fi
}

resolve_host_ips() {
    local host="$1"
    if [[ "$host" == "localhost" || "$host" == 127.* ]]; then
        return 0
    fi
    if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        printf '%s\n' "$host"
        return 0
    fi
    dig +short A "$host" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true
}

allow_endpoint_from_env() {
    local label="$1"
    local raw_value="$2"
    local default_port="$3"
    local host port ips ip

    [ -n "$raw_value" ] || return 0

    host=$(extract_host "$raw_value")
    port=$(extract_port "$raw_value" "$default_port")
    [ -n "$host" ] || return 0

    if [[ "$host" == "localhost" || "$host" == 127.* ]]; then
        echo "Skipping loopback endpoint for $label (already allowed on lo)."
        return 0
    fi

    echo "Resolving configured $label endpoint..."
    ips=$(resolve_host_ips "$host")
    for ip in $ips; do
        ipset add allowed-egress "$ip/32" -exist
        if [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" != "443" ] && [ "$port" != "22" ]; then
            iptables -A OUTPUT -d "$ip/32" -p tcp --dport "$port" -j ACCEPT
        fi
    done
}

# 5. Allowlist NuGet, Microsoft SDK feeds, Antigravity/Gemini, LiteLLM, and OTLP/Grafana endpoints
ALLOWED_DOMAINS=(
    "api.nuget.org"
    "globalcdn.nuget.org"
    "azureedge.net"
    "dotnetcli.azureedge.net"
    # Google Gemini & Antigravity CLI (`agy`) API / OAuth endpoints
    "generativelanguage.googleapis.com"
    "antigravity.google"
    "cloudcode-pa.googleapis.com"
    "aiplatform.googleapis.com"
    "oauth2.googleapis.com"
    "accounts.google.com"
)

for domain in "${ALLOWED_DOMAINS[@]}"; do
    echo "Resolving $domain..."
    IPS=$(resolve_host_ips "$domain")
    for ip in $IPS; do
        ipset add allowed-egress "$ip/32" -exist
    done
done

# Dynamically allow runtime-configured OTLP / Grafana Cloud & LiteLLM AI Gateway endpoints
# (Avoids hardcoding private tenant URLs; loopback 127.0.0.1:4318 / :4000 is already allowed on lo)
allow_endpoint_from_env "GRAFANA_OTLP_ENDPOINT" "${GRAFANA_OTLP_ENDPOINT:-}" "443"
allow_endpoint_from_env "OTEL_EXPORTER_OTLP_ENDPOINT" "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" "4318"
allow_endpoint_from_env "LITELLM_PROXY_HOST" "${LITELLM_PROXY_HOST:-}" "${LITELLM_PROXY_PORT:-4000}"
allow_endpoint_from_env "LITELLM_PROXY_URL" "${LITELLM_PROXY_URL:-}" "${LITELLM_PROXY_PORT:-4000}"

# 6. Allow outbound HTTPS/SSH to allowlisted destinations and DROP everything else
iptables -A OUTPUT -m set --match-set allowed-egress dst -p tcp --dport 443 -j ACCEPT
iptables -A OUTPUT -m set --match-set allowed-egress dst -p tcp --dport 22 -j ACCEPT
iptables -P OUTPUT DROP

# 7. Verify firewall enforcement immediately
echo "Verifying firewall rules..."
if curl -sS --connect-timeout 3 https://example.com >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed — unapproved egress to example.com succeeded!" >&2
    exit 1
fi

if ! curl -sS --connect-timeout 5 https://api.github.com/zen >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed — allowlisted egress to api.github.com was blocked!" >&2
    exit 1
fi

echo "Builder Agent egress firewall active and verified."
