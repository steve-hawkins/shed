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

# 5. Allowlist NuGet, Microsoft symbols/SDK feeds, and LLM / AI Gateway endpoints
ALLOWED_DOMAINS=(
    "api.nuget.org"
    "globalcdn.nuget.org"
    "azureedge.net"
    "dotnetcli.azureedge.net"
    "api.anthropic.com"
    "api.openai.com"
    "generativelanguage.googleapis.com"
)

# Also allow an optional custom LiteLLM AI Gateway host passed at runtime
if [ -n "${LITELLM_PROXY_HOST:-}" ]; then
    ALLOWED_DOMAINS+=("$LITELLM_PROXY_HOST")
fi

for domain in "${ALLOWED_DOMAINS[@]}"; do
    echo "Resolving $domain..."
    IPS=$(dig +short A "$domain" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)
    for ip in $IPS; do
        ipset add allowed-egress "$ip/32" -exist
    done
done

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
