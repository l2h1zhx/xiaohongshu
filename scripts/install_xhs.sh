#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
upstream_root="${KOC_UPSTREAM_ROOT:-/workspace/dependencies/XHS-Downloader}"
upstream_sha=2ab2620b0dd35b04530e70f1e9c7ab3f78c5d6c5
export UV_CACHE_DIR="${UV_CACHE_DIR:-$project_root/var/uv-cache}"
private_dir="$project_root/var/private"
mkdir -p "$(dirname "$upstream_root")" "$private_dir"
if [ ! -e "$upstream_root" ]; then
  git clone --no-checkout --depth 1 https://github.com/JoeanAmier/XHS-Downloader.git "$upstream_root"
  git -C "$upstream_root" fetch --depth 1 origin "$upstream_sha"
  git -C "$upstream_root" checkout --detach "$upstream_sha"
fi
if [ ! -d "$upstream_root/.git" ] || [ "$(git -C "$upstream_root" rev-parse HEAD)" != "$upstream_sha" ]; then
  echo '上游目录或版本不符，停止安装以保护已有文件。' >&2
  exit 1
fi
cd "$upstream_root"
if [ ! -d .venv ]; then
  uv venv --python "$project_root/.venv/bin/python" .venv
fi
uv export --frozen --no-dev --no-emit-project --output-file "$private_dir/xhs-requirements.txt" > /dev/null
uv pip install --python .venv/bin/python --index-url https://pypi.org/simple --require-hashes -r "$private_dir/xhs-requirements.txt"
uv pip install --python .venv/bin/python --index-url https://pypi.org/simple httpx==0.28.1
# 只安装，不启动上游默认API；真实读取使用项目受控PoC路径。
