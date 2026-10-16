#!/bin/sh
# Lightsail launch script: prepare an Ubuntu 24.04 nano box to run the app.
# Runs once, as root, on first boot.
#
# MUST be POSIX sh, not bash. Lightsail prepends its own "#!/bin/sh" preamble
# (it installs an SSH CA key) and appends this content below it, so our shebang
# is ignored and the whole thing executes under dash. That means no
# `set -o pipefail` and no `>(process substitution)` — both are bashisms that
# make dash abort with "Illegal option".
set -eux
exec >>/var/log/trolley-bootstrap.log 2>&1

echo "=== Smart Trolley bootstrap starting $(date -Is)"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg git rsync

# --- swap ------------------------------------------------------------------
# The nano bundle has 512 MB of RAM. A docker build can spike past that, so
# give it 2 GB of swap as insurance rather than paying for a bigger box.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Prefer RAM, but use swap before the OOM killer fires.
  sysctl -w vm.swappiness=20
  echo 'vm.swappiness=20' > /etc/sysctl.d/99-trolley.conf
fi

# --- docker ----------------------------------------------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# shellcheck source=/dev/null
. /etc/os-release
cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable
EOF

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker ubuntu

# Cap the journal and docker logs so a 20 GB disk can't fill with logs.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
systemctl restart docker

# --- project directory -----------------------------------------------------
install -d -o ubuntu -g ubuntu /opt/trolley

# --- restart the stack on reboot ------------------------------------------
cat > /etc/systemd/system/trolley.service <<'EOF'
[Unit]
Description=Smart Trolley cloud stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=ubuntu
WorkingDirectory=/opt/trolley
# --env-file is required: Compose looks for .env next to the compose file
# (deploy/lightsail/) but ours lives at the project root. Relative paths inside
# the compose file (build context ../.., ../Caddyfile) still resolve against the
# compose file's directory, so do NOT add --project-directory here.
ExecStart=/usr/bin/docker compose --env-file /opt/trolley/.env -f deploy/lightsail/docker-compose.yml up -d
ExecStop=/usr/bin/docker compose --env-file /opt/trolley/.env -f deploy/lightsail/docker-compose.yml down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable trolley.service

touch /opt/trolley/.bootstrap-complete
echo "=== Smart Trolley bootstrap finished $(date -Is)"
