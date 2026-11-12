#!/usr/bin/env bash
# Build (and optionally publish) the Pillow Lambda layer for the console path.
#
# Works on Linux, macOS, WSL and AWS CloudShell. pip downloads Linux wheels
# that match the Lambda runtime, so the OS you run it on does not matter.
#
# Usage:
#   ./scripts/build_layer.sh            # builds build/pillow-layer.zip
#   ./scripts/build_layer.sh --publish  # builds and publishes the layer
set -euo pipefail

PYTHON_VERSION="3.13"
PILLOW_VERSION="12.3.0"
LAYER_NAME="${LAYER_NAME:-pillow-py313}"
BUILD_DIR="build/pillow-layer"

rm -rf "$BUILD_DIR" build/pillow-layer.zip
mkdir -p "$BUILD_DIR/python"

python3 -m pip install "Pillow==${PILLOW_VERSION}" \
  --platform manylinux_2_28_x86_64 \
  --implementation cp \
  --python-version "$PYTHON_VERSION" \
  --only-binary=:all: \
  --target "$BUILD_DIR/python"

(cd "$BUILD_DIR" && zip -qr9 ../pillow-layer.zip python)
echo "Built build/pillow-layer.zip ($(du -h build/pillow-layer.zip | cut -f1))"

if [[ "${1:-}" == "--publish" ]]; then
  aws lambda publish-layer-version \
    --layer-name "$LAYER_NAME" \
    --description "Pillow ${PILLOW_VERSION} for python${PYTHON_VERSION} x86_64" \
    --zip-file fileb://build/pillow-layer.zip \
    --compatible-runtimes "python${PYTHON_VERSION}" \
    --compatible-architectures x86_64 \
    --query LayerVersionArn --output text
fi
