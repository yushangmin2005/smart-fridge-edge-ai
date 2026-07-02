#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SOURCE_DIR="$REPO_ROOT/${GROCERIES5K_SOURCE_DIR:-data/groceries-5k-source}"
OUTPUT_DIR="$REPO_ROOT/${GROCERIES5K_OUTPUT_DIR:-data/groceries-5k-yolo}"
REPO_URL="${GROCERIES5K_REPO_URL:-https://github.com/aleksandar-aleksandrov/groceries-object-detection-dataset.git}"
BRANCH="${GROCERIES5K_BRANCH:-train-val}"

if ! command -v git >/dev/null 2>&1; then
  echo "Missing required command: git" >&2
  exit 2
fi

clone_with_retries() {
  local attempt
  for attempt in 1 2 3; do
    rm -rf "$SOURCE_DIR"
    mkdir -p "$(dirname "$SOURCE_DIR")"
    if GIT_HTTP_VERSION=HTTP/1.1 git -c http.version=HTTP/1.1 clone --depth=1 --single-branch --branch "$BRANCH" "$REPO_URL" "$SOURCE_DIR"; then
      return 0
    fi
    echo "git clone failed; retrying ($attempt/3)..." >&2
    sleep 5
  done
  return 1
}

if [ ! -d "$SOURCE_DIR/.git" ]; then
  clone_with_retries
elif [ "${GROCERIES5K_UPDATE:-0}" = "1" ]; then
  git -C "$SOURCE_DIR" fetch --depth=1 origin "$BRANCH"
  git -C "$SOURCE_DIR" checkout "$BRANCH"
  git -C "$SOURCE_DIR" reset --hard "origin/$BRANCH"
fi

if [ "${GROCERIES5K_FORCE_CONVERT:-0}" = "1" ]; then
  rm -rf "$OUTPUT_DIR"
fi

python3 - <<PY
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

source = Path("$SOURCE_DIR") / "dataset"
output = Path("$OUTPUT_DIR")
classes_path = source / "classes.txt"

if not classes_path.exists():
    raise SystemExit(f"Missing classes file: {classes_path}")

classes = [line.strip() for line in classes_path.read_text().splitlines() if line.strip()]
class_to_id = {name: idx for idx, name in enumerate(classes)}

if (output / "data.yaml").exists() and "${GROCERIES5K_FORCE_CONVERT:-0}" != "1":
    print(f"dataset_yaml={output / 'data.yaml'}")
    print("groceries5k_status=already_converted")
    raise SystemExit(0)

if output.exists():
    shutil.rmtree(output)

counts = {}
box_counts = {}

for split in ("train", "val"):
    image_root = source / split / "images"
    annot_root = source / split / "annotations"
    out_images = output / "images" / split
    out_labels = output / "labels" / split
    out_images.mkdir(parents=True, exist_ok=True)
    out_labels.mkdir(parents=True, exist_ok=True)
    counts[split] = 0
    box_counts[split] = 0

    for xml_path in sorted(annot_root.rglob("*.xml")):
        tree = ET.parse(xml_path)
        root = tree.getroot()
        filename = root.findtext("filename")
        width = int(float(root.findtext("size/width", "0")))
        height = int(float(root.findtext("size/height", "0")))
        class_folder = xml_path.parent.name
        image_path = image_root / class_folder / filename

        if not filename or width <= 0 or height <= 0 or not image_path.exists():
            raise SystemExit(f"Invalid annotation or missing image for {xml_path}")

        stem = f"{class_folder}_{Path(filename).stem}"
        suffix = Path(filename).suffix.lower()
        dst_image = out_images / f"{stem}{suffix}"
        dst_label = out_labels / f"{stem}.txt"
        lines = []

        for obj in root.findall("object"):
            name = obj.findtext("name", "").strip()
            if name not in class_to_id:
                raise SystemExit(f"Unknown class {name!r} in {xml_path}")
            box = obj.find("bndbox")
            xmin = max(0.0, float(box.findtext("xmin", "0")))
            ymin = max(0.0, float(box.findtext("ymin", "0")))
            xmax = min(float(width), float(box.findtext("xmax", "0")))
            ymax = min(float(height), float(box.findtext("ymax", "0")))
            if xmax <= xmin or ymax <= ymin:
                continue
            x_center = ((xmin + xmax) / 2.0) / width
            y_center = ((ymin + ymax) / 2.0) / height
            box_width = (xmax - xmin) / width
            box_height = (ymax - ymin) / height
            lines.append(
                f"{class_to_id[name]} {x_center:.6f} {y_center:.6f} {box_width:.6f} {box_height:.6f}"
            )

        if not lines:
            continue

        shutil.copy2(image_path, dst_image)
        dst_label.write_text("\\n".join(lines) + "\\n")
        counts[split] += 1
        box_counts[split] += len(lines)

yaml_lines = [
    f"path: {output}",
    "train: images/train",
    "val: images/val",
    "names:",
]
yaml_lines.extend(f"  {idx}: {name}" for idx, name in enumerate(classes))
(output / "data.yaml").write_text("\\n".join(yaml_lines) + "\\n")

print(f"dataset_yaml={output / 'data.yaml'}")
print(f"class_count={len(classes)}")
print(f"train_images={counts['train']}")
print(f"val_images={counts['val']}")
print(f"train_boxes={box_counts['train']}")
print(f"val_boxes={box_counts['val']}")
PY

echo "Use this dataset with:"
echo "YOLO_DATA_YAML=${OUTPUT_DIR#$REPO_ROOT/}/data.yaml YOLO_RUN_NAME=groceries-5k-public scripts/train_yolo11n_local.sh"
