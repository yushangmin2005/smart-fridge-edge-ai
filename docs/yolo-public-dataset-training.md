# YOLO11n 公开数据集训练流程

## 数据集选择

默认先用 Roboflow Universe 的 `fridge-food-images`：

- 页面：https://universe.roboflow.com/fridge-dataset/fridge-food-images
- 用途：冰箱/食材类目标检测，适合先训练 YOLO 的入口识别能力。
- 当前定位：只负责识别食材或包装目标，腐败/临期风险后续交给 VLM 与规则层融合判断。

备选数据集：

- `Whatsinyourfridge`：https://universe.roboflow.com/whats-in-my-fridge/whatsinyourfridge
- `5K Groceries Object Detection`：https://github.com/aleksandar-aleksandrov/groceries-object-detection-dataset

## 一次性配置

```bash
cp config/yolo_public_dataset.env.example config/yolo_public_dataset.env
```

如果使用 Roboflow 程序化下载，需要在 `config/yolo_public_dataset.env` 里写入：

```bash
ROBOFLOW_API_KEY=你的_key
```

脚本里的数据集引用默认是 `fridge-dataset/fridge-food-images/14`。也可以在 Roboflow 网页手动导出 YOLO11/YOLOv8 格式，解压到 `data/fridge-food-images/`，保证存在 `data/fridge-food-images/data.yaml`。

没有 Roboflow API key 时，可先用 GitHub 的 5K Groceries 数据集跑通训练链路：

```bash
scripts/download_groceries5k_dataset.sh
YOLO_DATA_YAML=data/groceries-5k-yolo/data.yaml YOLO_RUN_NAME=groceries-5k-public scripts/train_yolo11n_local.sh
```

这个备选数据集是商品/食材包装图，不是冰箱场景；它适合验证训练和部署流程，最终仍建议换成冰箱视角数据集并补拍本地冰箱照片。

## 本地训练

```bash
scripts/setup_yolo_training_local.sh
scripts/download_roboflow_dataset.sh
scripts/train_yolo11n_local.sh
scripts/export_yolo11n_onnx_local.sh
```

快速冒烟训练可临时缩小数据比例：

```bash
YOLO_FRACTION=0.05 YOLO_EPOCHS=1 scripts/train_yolo11n_local.sh
```

默认参数：

- 模型：`yolo11n.pt`
- 图片尺寸：`640`
- 轮数：`80`
- batch：`8`
- device：`auto`，优先使用 Apple MPS，否则回退 CPU

如果本机发热或内存紧张，改为：

```bash
YOLO_IMG_SIZE=512 YOLO_BATCH=4 scripts/train_yolo11n_local.sh
```

## 导出与部署

导出脚本会生成：

```text
models/fridge-yolo11n.onnx
models/fridge-yolo11n.classes.txt
```

同步到远程板端：

```bash
scripts/deploy_yolo_model_to_remote.sh firecar-pi
```

板端测试：

```bash
ssh firecar-pi '~/yolo-inference/bin/yolo_detect.sh --image ~/yolo-inference/samples/test.jpg --output-json ~/yolo-inference/outputs/test.json --output-image ~/yolo-inference/outputs/test.jpg'
```
