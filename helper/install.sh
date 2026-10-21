#!/bin/sh
# Install the adoption helper root-owned and let the invoking user run it
# (and only it) as root without a password. Re-run after changing the helper:
# the installed copy is what runs, never the checkout.
#
#   sudo helper/install.sh
set -eu

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
user="${SUDO_USER:?run via sudo from the account that runs miaunifi}"
here=$(dirname "$(readlink -f "$0")")

# Ubuntu's paramiko (4.x) still supports ssh-rsa, the only host key type the controller offers
apt-get install -y python3-paramiko dhcpcd-base

install -d -o root -g root -m 0755 /usr/local/lib/miaunifi-adopt
install -o root -g root -m 0644 "$here/miaunifi_adopt.py" /usr/local/lib/miaunifi-adopt/

cat > /usr/local/bin/miaunifi-adopt <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -I /usr/local/lib/miaunifi-adopt/miaunifi_adopt.py "$@"
EOF
chown root:root /usr/local/bin/miaunifi-adopt
chmod 0755 /usr/local/bin/miaunifi-adopt

tmp=$(mktemp)
echo "$user ALL=(root) NOPASSWD: /usr/local/bin/miaunifi-adopt" > "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/miaunifi
rm -f "$tmp"

echo "installed; $user can now run: sudo -n /usr/local/bin/miaunifi-adopt"
