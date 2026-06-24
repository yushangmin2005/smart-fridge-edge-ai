#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
OPENSSL_VERSION="${OPENSSL_VERSION:-3.0.21}"
VLM_REMOTE_DIR="${VLM_REMOTE_DIR:-}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "OPENSSL_VERSION='$OPENSSL_VERSION' VLM_REMOTE_DIR='$VLM_REMOTE_DIR' bash -s" <<'REMOTE'
set -euo pipefail

if [ -z "${VLM_REMOTE_DIR:-}" ]; then
  VLM_REMOTE_DIR="$HOME/vlm-inference"
fi

for c in curl tar make perl gcc sha256sum; do
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "Missing required command: $c" >&2
    exit 2
  fi
done

arch="$(uname -m)"
case "$arch" in
  aarch64|arm64) openssl_target="linux-aarch64" ;;
  x86_64|amd64) openssl_target="linux-x86_64" ;;
  *) echo "Unsupported architecture for OpenSSL source build: $arch" >&2; exit 3 ;;
esac

available_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
if [ "$available_kb" -lt 1048576 ]; then
  echo "Not enough free disk under $HOME; need at least 1 GiB for OpenSSL build." >&2
  exit 4
fi

mkdir -p "$VLM_REMOTE_DIR"/{bin,runtime,tmp}

src_url="https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz"
archive="$VLM_REMOTE_DIR/tmp/openssl-${OPENSSL_VERSION}.tar.gz"
checksum="$archive.sha256"
src_dir="$VLM_REMOTE_DIR/tmp/openssl-${OPENSSL_VERSION}"
prefix="$VLM_REMOTE_DIR/runtime/openssl-${OPENSSL_VERSION}"

echo "Downloading $src_url"
curl -fL --retry 3 --connect-timeout 20 -o "$archive" "$src_url"
curl -fL --retry 3 --connect-timeout 20 -o "$checksum" "$src_url.sha256"
(cd "$(dirname "$archive")" && sha256sum -c "$(basename "$checksum")")

rm -rf "$src_dir" "$prefix"
mkdir -p "$src_dir"
tar -xzf "$archive" -C "$src_dir" --strip-components=1
rm -f "$archive" "$checksum"

(
  cd "$src_dir"
  ./Configure "$openssl_target" shared no-tests \
    --prefix="$prefix" \
    --openssldir="$prefix/ssl" \
    --libdir=lib
  make -j "${OPENSSL_BUILD_JOBS:-1}"
  make install_sw
)

rm -rf "$src_dir"
ln -sfn "$prefix" "$VLM_REMOTE_DIR/runtime/openssl-current"

cat > "$VLM_REMOTE_DIR/bin/openssl3_env.sh" <<'EOF'
#!/usr/bin/env bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENSSL3_ROOT="${OPENSSL3_ROOT:-$ROOT/runtime/openssl-current}"

if [ -d "$OPENSSL3_ROOT" ]; then
  export PATH="$OPENSSL3_ROOT/bin:$PATH"
  export LD_LIBRARY_PATH="$OPENSSL3_ROOT/lib:${LD_LIBRARY_PATH:-}"
  export OPENSSL_MODULES="$OPENSSL3_ROOT/lib/ossl-modules"
  if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
    export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
  fi
  if [ -d /etc/ssl/certs ]; then
    export SSL_CERT_DIR="${SSL_CERT_DIR:-/etc/ssl/certs}"
  fi
fi
EOF
chmod +x "$VLM_REMOTE_DIR/bin/openssl3_env.sh"

patch_loader() {
  file="$1"
  [ -f "$file" ] || return 0
  grep -q 'openssl3_env.sh' "$file" && return 0
  tmp="$file.tmp"
  awk '
    /^ROOT=/ && !inserted {
      print
      print "OPENSSL_ENV=\"$ROOT/bin/openssl3_env.sh\""
      print "[ -f \"$OPENSSL_ENV\" ] && . \"$OPENSSL_ENV\""
      inserted=1
      next
    }
    { print }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
  chmod +x "$file"
}

patch_loader "$VLM_REMOTE_DIR/bin/start_vlm.sh"
patch_loader "$VLM_REMOTE_DIR/bin/runtime_check.sh"

. "$VLM_REMOTE_DIR/bin/openssl3_env.sh"
"$prefix/bin/openssl" version
test -f "$prefix/lib/libssl.so.3"
test -f "$prefix/lib/libcrypto.so.3"
echo "openssl3=pass path=$prefix"
REMOTE
