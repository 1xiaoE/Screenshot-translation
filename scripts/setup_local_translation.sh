#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
/usr/bin/python3 -m venv --system-site-packages "$project_dir/.venv-local"
"$project_dir/.venv-local/bin/python" -m pip install -r "$project_dir/requirements-local.txt"
archive_path="$(mktemp --suffix=.argosmodel)"
trap 'rm -f -- "$archive_path"' EXIT
if ! curl -fL --retry 2 --retry-all-errors --connect-timeout 15 --max-time 180 \
  https://argos-net.com/v1/translate-en_zh-1_9.argosmodel -o "$archive_path"; then
  curl -fL --noproxy '*' --connect-timeout 15 --max-time 180 \
    https://argos-net.com/v1/translate-en_zh-1_9.argosmodel -o "$archive_path"
fi
/usr/bin/python3 "$project_dir/scripts/install_local_model.py" "$archive_path"
echo '本地英→中翻译已安装，默认启用机器人／机械术语表。'
