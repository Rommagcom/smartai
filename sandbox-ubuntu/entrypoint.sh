#!/usr/bin/env bash
set -euo pipefail

SANDBOX_USER="${SANDBOX_USER:-aismartsandbox}"
SANDBOX_PASSWORD="${SANDBOX_PASSWORD:-sandbox_smartai}"
ROOT_PASSWORD="${ROOT_PASSWORD:-$SANDBOX_PASSWORD}"

mkdir -p /var/run/sshd

if ! id "$SANDBOX_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$SANDBOX_USER"
fi

echo "$SANDBOX_USER:$SANDBOX_PASSWORD" | chpasswd
echo "root:$ROOT_PASSWORD" | chpasswd

usermod -aG sudo "$SANDBOX_USER"
echo "$SANDBOX_USER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-sandbox
chmod 440 /etc/sudoers.d/90-sandbox

sed -ri 's/^#?PermitRootLogin\s+.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -ri 's/^#?PasswordAuthentication\s+.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -ri 's/^#?PubkeyAuthentication\s+.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

if ! grep -q '^PermitRootLogin' /etc/ssh/sshd_config; then
  echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
fi
if ! grep -q '^PasswordAuthentication' /etc/ssh/sshd_config; then
  echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
fi
if ! grep -q '^PubkeyAuthentication' /etc/ssh/sshd_config; then
  echo 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config
fi

echo "Starting sshd for user: $SANDBOX_USER"
exec /usr/sbin/sshd -D -e
