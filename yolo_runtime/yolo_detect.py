#!/usr/bin/env python3
"""Lightweight YOLO ONNX inference runner for CPU-only edge devices."""

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def parse_args():
    parser = argparse.ArgumentParser(description="Run YOLO ONNX object detection on one image.")
    parser.add_argument("--model", required=True, help="Path to a YOLO ONNX model.")
    parser.add_argument("--image", required=True, help="Path to an input image.")
    parser.add_argument("--output-json", help="Path to write detection JSON.")
    parser.add_argument("--output-image", help="Optional annotated image output path.")
    parser.add_argument("--labels", help="Optional class label file, one class per line.")
    parser.add_argument("--img-size", type=int, default=640, help="Square inference size.")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold.")
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold.")
    parser.add_argument("--topk", type=int, default=300, help="Max candidates before NMS.")
    parser.add_argument(
        "--has-objectness",
        choices=("auto", "true", "false"),
        default="auto",
        help="Whether model output includes YOLOv5-style objectness.",
    )
    return parser.parse_args()


def load_labels(path):
    if not path:
        return []
    labels = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            label = line.strip()
            if label:
                labels.append(label)
    return labels


def letterbox(image, size):
    width, height = image.size
    scale = min(size / width, size / height)
    resized_width = int(round(width * scale))
    resized_height = int(round(height * scale))
    resized = image.resize((resized_width, resized_height), Image.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    pad_x = (size - resized_width) // 2
    pad_y = (size - resized_height) // 2
    canvas.paste(resized, (pad_x, pad_y))
    array = np.asarray(canvas, dtype=np.float32) / 255.0
    array = np.transpose(array, (2, 0, 1))[None, ...]
    return array, scale, pad_x, pad_y


def should_use_objectness(channel_count, labels, mode):
    if mode == "true":
        return True
    if mode == "false":
        return False
    if labels:
        if channel_count == len(labels) + 5:
            return True
        if channel_count == len(labels) + 4:
            return False
    # Common COCO YOLOv5 export: xywh + objectness + 80 class scores.
    if channel_count == 85:
        return True
    return False


def normalize_prediction(raw):
    pred = np.asarray(raw)
    while pred.ndim > 2:
        pred = pred[0]
    if pred.ndim != 2:
        raise ValueError(f"Unsupported YOLO output shape: {np.asarray(raw).shape}")
    if pred.shape[0] < pred.shape[1] and pred.shape[0] <= 512 and pred.shape[1] > 512:
        pred = pred.T
    return pred.astype(np.float32, copy=False)


def xywh_to_xyxy(boxes, scale, pad_x, pad_y, width, height):
    x, y, w, h = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    x1 = (x - w / 2.0 - pad_x) / scale
    y1 = (y - h / 2.0 - pad_y) / scale
    x2 = (x + w / 2.0 - pad_x) / scale
    y2 = (y + h / 2.0 - pad_y) / scale
    xyxy = np.stack([x1, y1, x2, y2], axis=1)
    xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, width)
    xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, height)
    return xyxy


def nms(boxes, scores, iou_threshold):
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        current = int(order[0])
        keep.append(current)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[current], x1[rest])
        yy1 = np.maximum(y1[current], y1[rest])
        xx2 = np.minimum(x2[current], x2[rest])
        yy2 = np.minimum(y2[current], y2[rest])
        inter_w = np.maximum(0.0, xx2 - xx1)
        inter_h = np.maximum(0.0, yy2 - yy1)
        inter = inter_w * inter_h
        union = areas[current] + areas[rest] - inter
        iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
        order = rest[iou <= iou_threshold]
    return keep


def detections_from_output(raw, args, labels, image_size, scale, pad_x, pad_y):
    width, height = image_size
    pred = normalize_prediction(raw)
    channel_count = pred.shape[1]
    use_objectness = should_use_objectness(channel_count, labels, args.has_objectness)
    class_offset = 5 if use_objectness else 4
    if channel_count <= class_offset:
        raise ValueError(f"YOLO output has no class score columns: {pred.shape}")

    class_scores = pred[:, class_offset:]
    class_ids = np.argmax(class_scores, axis=1)
    class_conf = class_scores[np.arange(class_scores.shape[0]), class_ids]
    if use_objectness:
        scores = pred[:, 4] * class_conf
    else:
        scores = class_conf

    mask = np.isfinite(scores) & (scores >= args.conf)
    if not np.any(mask):
        return []

    boxes = xywh_to_xyxy(pred[mask, :4], scale, pad_x, pad_y, width, height)
    scores = scores[mask]
    class_ids = class_ids[mask]

    if args.topk > 0 and len(scores) > args.topk:
        order = scores.argsort()[::-1][: args.topk]
        boxes = boxes[order]
        scores = scores[order]
        class_ids = class_ids[order]

    keep = nms(boxes, scores, args.iou)
    results = []
    for index in keep:
        cls = int(class_ids[index])
        name = labels[cls] if cls < len(labels) else str(cls)
        x1, y1, x2, y2 = boxes[index].tolist()
        results.append(
            {
                "class_id": cls,
                "class_name": name,
                "confidence": round(float(scores[index]), 6),
                "box": {
                    "x1": round(float(x1), 2),
                    "y1": round(float(y1), 2),
                    "x2": round(float(x2), 2),
                    "y2": round(float(y2), 2),
                },
            }
        )
    results.sort(key=lambda item: item["confidence"], reverse=True)
    return results


def draw_detections(image, detections, output_path):
    draw = ImageDraw.Draw(image)
    for det in detections:
        box = det["box"]
        label = f'{det["class_name"]} {det["confidence"]:.2f}'
        xyxy = [box["x1"], box["y1"], box["x2"], box["y2"]]
        draw.rectangle(xyxy, outline=(255, 80, 0), width=3)
        try:
            text_box = draw.textbbox((xyxy[0], xyxy[1]), label)
            text_w = text_box[2] - text_box[0]
            text_h = text_box[3] - text_box[1]
        except AttributeError:
            text_w, text_h = draw.textsize(label)
        y_text = max(0, xyxy[1] - text_h - 4)
        draw.rectangle([xyxy[0], y_text, xyxy[0] + text_w + 6, y_text + text_h + 4], fill=(255, 80, 0))
        draw.text((xyxy[0] + 3, y_text + 2), label, fill=(255, 255, 255))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def main():
    args = parse_args()
    labels = load_labels(args.labels)

    import onnxruntime as ort

    session = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
    input_info = session.get_inputs()[0]
    image = Image.open(args.image).convert("RGB")
    tensor, scale, pad_x, pad_y = letterbox(image, args.img_size)
    output = session.run(None, {input_info.name: tensor})[0]
    detections = detections_from_output(output, args, labels, image.size, scale, pad_x, pad_y)

    payload = {
        "model": str(Path(args.model).resolve()),
        "image": str(Path(args.image).resolve()),
        "img_size": args.img_size,
        "detections": detections,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    if args.output_image:
        draw_detections(image, detections, args.output_image)


if __name__ == "__main__":
    main()
