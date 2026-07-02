#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/setup_yolo_training_local.sh"
"$SCRIPT_DIR/download_roboflow_dataset.sh"
"$SCRIPT_DIR/train_yolo11n_local.sh"
"$SCRIPT_DIR/export_yolo11n_onnx_local.sh"

echo "public_yolo_training_pipeline=complete"
