#!/bin/sh
# RunPeek installer: downloads the self-contained binary for this platform, puts it on PATH,
# and runs `runpeek setup`. No Python, no repository, no virtual environment required.
# Usage: curl -fsSL https://github.com/AgentMulder404/runpeek/releases/latest/download/install.sh | sh
#        RUNPEEK_BINARY=/path/to/runpeek sh install.sh   (use an already downloaded binary)
set -eu
REPO="AgentMulder404/runpeek"
DEST="${RUNPEEK_INSTALL_DIR:-$HOME/.runpeek/bin}"
mkdir -p "$DEST"
if [ -n "${RUNPEEK_BINARY:-}" ]; then
  cp "$RUNPEEK_BINARY" "$DEST/runpeek"
else
  os=$(uname -s | tr '[:upper:]' '[:lower:]'); arch=$(uname -m)
  case "$arch" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; esac
  url="https://github.com/$REPO/releases/latest/download/runpeek-$os-$arch"
  echo "Downloading $url"
  curl -fsSL "$url" -o "$DEST/runpeek"
fi
chmod 0755 "$DEST/runpeek"
case ":$PATH:" in *":$DEST:"*) ;; *)
  for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
    [ -f "$rc" ] && ! grep -q 'runpeek/bin' "$rc" && printf '\n# RunPeek\nexport PATH="%s:$PATH"\n' "$DEST" >> "$rc" && break
  done
  export PATH="$DEST:$PATH";;
esac
echo "Installed $("$DEST/runpeek" --version) at $DEST/runpeek"
"$DEST/runpeek" setup "$@"
