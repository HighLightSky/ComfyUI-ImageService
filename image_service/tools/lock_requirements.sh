#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

# Ubuntu / Python 3.12; preserve ComfyUI's shared dependency constraints.
uv pip compile pyproject.toml --python ../../.venv/bin/python --python-version 3.12 --python-platform x86_64-unknown-linux-gnu -c ../../requirements.txt -o requirements.txt --quiet "$@"
uv pip compile pyproject.toml --python ../../.venv/bin/python --python-version 3.12 --python-platform x86_64-unknown-linux-gnu -c ../../requirements.txt -c requirements.txt --extra pose -o requirements-pose.txt --quiet "$@"
uv pip compile pyproject.toml --python ../../.venv/bin/python --python-version 3.12 --python-platform x86_64-unknown-linux-gnu -c ../../requirements.txt -c requirements-pose.txt --extra pose --extra dev -o image_service/tests/requirements.txt --quiet "$@"
for path in requirements.txt requirements-pose.txt image_service/tests/requirements.txt; do
    sed -i '1i# Generated for Ubuntu / Python 3.12. Do not edit; use bash image_service/tools/lock_requirements.sh.' "$path"
done
