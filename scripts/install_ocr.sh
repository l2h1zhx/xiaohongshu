#!/usr/bin/env bash
set -euo pipefail
# 开源本地OCR；不调用付费模型，不安装个人电脑上的软件。
if ! command -v tesseract >/dev/null; then
  if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
    echo 'tesseract is required; install through the environment package manager.' >&2
    exit 1
  fi
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends tesseract-ocr
fi
KOC_TESSDATA_DIR="${KOC_TESSDATA_DIR:-/workspace/dependencies/tessdata}"
export KOC_TESSDATA_DIR
mkdir -p "$KOC_TESSDATA_DIR"
python3 - <<'PY'
import hashlib,os,urllib.request
from pathlib import Path
root=Path(os.environ['KOC_TESSDATA_DIR'])
commit='87416418657359cb625c412a48b6e1d6d41c29bd'
hashes={'chi_sim':'a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730','eng':'7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'}
for lang,expected in hashes.items():
    path=root/(lang+'.traineddata')
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==expected: continue
    with urllib.request.urlopen(f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{commit}/{lang}.traineddata',timeout=60) as response:
        data=response.read(10*1024*1024)
    if hashlib.sha256(data).hexdigest()!=expected: raise SystemExit('OCR model checksum mismatch: '+lang)
    path.write_bytes(data)
print('Verified Chinese and English OCR models.')
PY
