#!/usr/bin/env bash
#
# Download the YAMNet TFLite model + AudioSet class map into ./models/.
# Same files work on macOS (dev) and Raspberry Pi (prod).
#
# Usage (from the repo root):
#   ./scripts/download-yamnet.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)/models"
mkdir -p "${MODELS_DIR}"

MODEL_PATH="${MODELS_DIR}/yamnet.tflite"
LABELS_PATH="${MODELS_DIR}/yamnet_class_map.csv"

# The AudioSet class map (index,mid,display_name) -- stable raw URL.
LABELS_URL="https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"

# YAMNet classification model, TFLite build, from TF Hub / Kaggle Models.
# NOTE: if this URL has moved, grab the .tflite from
#   https://www.kaggle.com/models/google/yamnet/tfLite
# and save it as models/yamnet.tflite manually.
MODEL_URL="https://tfhub.dev/google/lite-model/yamnet/classification/tflite/1?lite-format=tflite"

fetch() {  # url, dest
  echo "Downloading $(basename "$2") ..."
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "$2" "$1"
  else
    wget -O "$2" "$1"
  fi
}

fetch "${LABELS_URL}" "${LABELS_PATH}"
fetch "${MODEL_URL}" "${MODEL_PATH}"

echo
echo "Done:"
echo "  ${MODEL_PATH}"
echo "  ${LABELS_PATH}"
echo
echo "Install the inference deps and try it on a saved file:"
echo "  pip install -e '.[infer]'"
echo "  wan-pulse classify recordings/*/bark_*.wav"
