#!/bin/sh
# Build MinosClient.exe under Wine. The staged tree is mounted at /src.
set -eu
wine C:\\Python\\python.exe -c "import os; assert os.path.exists(r'Z:\\src\\templates\\gatk.py'), 'staged source is not visible to Wine'"
wine C:\\Python\\python.exe -m pip install --disable-pip-version-check -q pyinstaller pyyaml httpx pydantic
rm -rf /tmp/pybuild
mkdir -p /tmp/pybuild /out
wine C:\\Python\\python.exe -m PyInstaller --noconfirm --clean \
  --distpath Z:\\out \
  --workpath Z:\\tmp\\pybuild \
  Z:\\src\\deployment\\windows\\pyinstaller.spec
test -f /out/MinosClient.exe
ls -lh /out/MinosClient.exe
