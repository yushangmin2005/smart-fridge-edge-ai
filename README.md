# 智能消防巡检车边缘 AI 推理框架

当前目标是在远程 `firecar-pi` 上部署可替换模型的边缘推理运行时。该设备实际为 NanoPC-T4，Ubuntu 20.04 ARM64，约 3.7 GiB 内存，无 NVIDIA GPU/CUDA/Docker，因此 VLM 默认采用 CPU-only 的 `llama.cpp` 多模态推理路线，YOLO 采用 ONNX Runtime CPU 推理路线。已额外尝试 Mali-T860 OpenCL runtime，但当前 `llama.cpp` OpenCL 后端会将 `Mali-T860` 判定为 unsupported，不能作为默认方案。

## 技术栈

- 运行时：`llama.cpp` Ubuntu ARM64 CPU 包；若系统库不兼容，则自动回退到同版本源码编译
- 兼容库：用户态 OpenSSL 3，共享库路径为远程 `~/vlm-inference/runtime/openssl-current`
- 模型格式：GGUF，多模态模型需要模型 GGUF 与可选 `mmproj` GGUF，或直接使用 `-hf` 加载 llama.cpp 支持的多模态仓库
- 服务接口：`llama-server` OpenAI-compatible `/v1/chat/completions`
- 部署方式：SSH 用户态安装到远程 `~/vlm-inference`，不依赖 sudo、Docker 或系统级服务
- 可选 GPU 实验：`firecar-pi` 有 Mali-T860 OpenCL 1.2 设备，可用 `scripts/deploy_llamacpp_opencl.sh` 编译独立 OpenCL runtime；当前实测只能编译成功，运行时无法枚举为可用 llama.cpp 设备
- YOLO 运行时：`onnxruntime==1.16.3`、`numpy==1.24.4`、`Pillow==10.4.0`，安装到远程 `~/yolo-inference/runtime/python-packages`
- YOLO 模型格式：ONNX；训练/微调可在其他机器完成，板端只负责加载导出的 `.onnx` 文件
- YOLO 训练栈：本地 macOS 使用 `uv` 创建 Python 3.12 虚拟环境，安装 `ultralytics`、`roboflow`、`torch`、`onnx`、`onnxruntime`、`onnxslim`
- YOLO 导出格式：默认 ONNX opset 19，兼容远程 `onnxruntime==1.16.3`
- 公开训练数据：默认使用 Roboflow Universe `fridge-dataset/fridge-food-images/14`，手动导出的 YOLO11/YOLOv8 数据集也可放入 `data/fridge-food-images/`
- 脚本语言：Bash

## 构建与部署命令

```bash
# 探测远程硬件与运行条件
scripts/remote_probe.sh firecar-pi

# 部署 llama.cpp CPU-only 运行时到远程 ~/vlm-inference
scripts/deploy_llamacpp_cpu.sh firecar-pi

# 无 sudo 时补齐 libssl.so.3/libcrypto.so.3 用户态共享库
scripts/deploy_openssl3_user.sh firecar-pi

# 检查远程运行时二进制是否可用
scripts/remote_runtime_check.sh firecar-pi

# 实验性编译 llama.cpp OpenCL runtime；默认不切换 runtime/current
scripts/deploy_llamacpp_opencl.sh firecar-pi

# 部署 YOLO ONNX CPU-only 运行时到远程 ~/yolo-inference
scripts/deploy_yolo_onnx_cpu.sh firecar-pi

# 检查远程 YOLO Python 依赖与 runner
scripts/remote_yolo_check.sh firecar-pi

# 本地配置公开数据集训练参数
cp config/yolo_public_dataset.env.example config/yolo_public_dataset.env

# 创建本地 YOLO11n 训练环境
scripts/setup_yolo_training_local.sh

# 下载 Roboflow 公开数据集；需要 ROBOFLOW_API_KEY 或已登录的 Roboflow CLI
scripts/download_roboflow_dataset.sh

# 无 Roboflow key 时，用 GitHub 5K Groceries 数据集跑通训练链路
scripts/download_groceries5k_dataset.sh

# 使用公开数据集训练 YOLO11n
scripts/train_yolo11n_local.sh

# 导出板端可用的 ONNX 与 classes.txt
scripts/export_yolo11n_onnx_local.sh

# 将导出的 YOLO 模型同步到 firecar-pi
scripts/deploy_yolo_model_to_remote.sh firecar-pi
```

部署后，远程目录结构为：

```text
~/vlm-inference/
  bin/                 # start/stop/status/health 脚本
  config/vlm.env       # 本机实际配置，默认不提交
  config/vlm.env.example
  logs/
  models/              # 后续放置微调/量化后的 GGUF 模型
  run/
  runtime/current      # 当前 llama.cpp 运行时软链接
```

YOLO 部署后，远程目录结构为：

```text
~/yolo-inference/
  bin/                 # yolo_env/yolo_detect/yolo_check 脚本
  config/yolo.env      # 本机实际配置，默认不提交
  config/yolo.env.example
  models/              # 放置导出的 YOLO ONNX 模型
  samples/             # 可选测试图片
  outputs/             # JSON/标注图输出
  runtime/python-packages
  runtime/yolo_detect.py
```

本地 YOLO 训练产物目录为：

```text
data/                 # 公开数据集，默认不提交
runs/                 # Ultralytics 训练输出，默认不提交
models/               # 导出的 ONNX/classes 文件，默认不提交
.venv-yolo/           # 本地训练虚拟环境，默认不提交
```

## 模型配置

具体 VLM 尚未固定，因此服务启动前需要在远程编辑：

```bash
ssh firecar-pi 'nano ~/vlm-inference/config/vlm.env'
```

二选一配置模型来源：

```bash
# 方式 1：使用 llama.cpp 支持的 Hugging Face GGUF 多模态仓库
VLM_MODEL_HF=ggml-org/SmolVLM-256M-Instruct-GGUF

# 方式 2：使用本地微调/转换后的 GGUF 文件
VLM_MODEL_PATH=/home/pi/vlm-inference/models/model.gguf
VLM_MMPROJ_PATH=/home/pi/vlm-inference/models/mmproj.gguf
```

当前 `firecar-pi` 已部署智能冰箱 VLM 模型：

```bash
VLM_MODEL_PATH=/home/pi/vlm-inference/models/smart-fridge-qwen25vl/smart-fridge-qwen25vl-merged-Q4_K_M.gguf
VLM_MMPROJ_PATH=/home/pi/vlm-inference/models/smart-fridge-qwen25vl/mmproj-smart-fridge-qwen25vl-Q8_0.gguf
VLM_CTX_SIZE=2048
VLM_TIMEOUT=3600
VLM_EXTRA_ARGS="--image-min-tokens 256 --image-max-tokens 768 --jinja"
```

该模型包来自本机 `/Users/yushangmin/Desktop/smart_fridge_qwen25vl_gguf/`。远端磁盘在部署后剩余约 1.3 GiB；当前参数优先保证 NanoPC-T4 CPU-only 环境可启动。`VLM_TIMEOUT=3600` 将 `llama-server` 读写超时显式固定为 1 小时；发起图片推理的客户端也需要设置不低于 1 小时的 HTTP 超时。`llama.cpp` 对 Qwen-VL grounding 任务提示 `image-min-tokens=1024` 更适合精细定位，但会进一步增加内存占用与推理时间。

启动、停止与状态检查：

```bash
ssh firecar-pi '~/vlm-inference/bin/start_vlm.sh'
ssh firecar-pi '~/vlm-inference/bin/status_vlm.sh'
ssh firecar-pi '~/vlm-inference/bin/health_vlm.sh'
ssh firecar-pi '~/vlm-inference/bin/stop_vlm.sh'
```

YOLO 模型同样不默认下载。导出 ONNX 后放入远程 `~/yolo-inference/models`，再编辑：

```bash
ssh firecar-pi 'nano ~/yolo-inference/config/yolo.env'
```

最小配置：

```bash
YOLO_MODEL_PATH=/home/pi/yolo-inference/models/model.onnx
YOLO_LABELS=/home/pi/yolo-inference/config/classes.txt
YOLO_IMG_SIZE=640
YOLO_CONF=0.25
YOLO_IOU=0.45
```

运行单张图片检测：

```bash
ssh firecar-pi '~/yolo-inference/bin/yolo_detect.sh --image ~/yolo-inference/samples/test.jpg --output-json ~/yolo-inference/outputs/test.json --output-image ~/yolo-inference/outputs/test.jpg'
```

## 公开数据集训练

默认训练入口见 [docs/yolo-public-dataset-training.md](docs/yolo-public-dataset-training.md)。

最小流程：

```bash
cp config/yolo_public_dataset.env.example config/yolo_public_dataset.env
# 在 config/yolo_public_dataset.env 中填入 ROBOFLOW_API_KEY，或手动导出数据集到 data/fridge-food-images/
scripts/run_public_yolo_training.sh
```

默认训练 `yolo11n.pt`，`imgsz=640`，`epochs=80`，`batch=8`，`device=auto`。脚本会优先使用 Apple MPS，若不可用则回退 CPU。ONNX 导出默认使用 `YOLO_ONNX_OPSET=19`，以兼容板端 `onnxruntime==1.16.3`。

本次公开数据集基线结果：

- 数据集：Roboflow `fridge-dataset/fridge-food-images/14`
- 数据规模：train 4172 张、valid 470 张、test 497 张，30 个食材类别
- 训练模型：`yolo11n.pt`，`imgsz=640`，`batch=8`，Apple MPS，`epochs=80`，`patience=0`
- 最佳轮次：第 66 轮，`P=0.81796`，`R=0.74272`，`mAP50=0.81128`，`mAP50-95=0.58557`
- 最终轮次：第 80 轮，`P=0.81968`，`R=0.73055`，`mAP50=0.80675`，`mAP50-95=0.58047`
- 本地导出：`models/fridge-yolo11n.onnx` 与 `models/fridge-yolo11n.classes.txt`
- 远端部署：`firecar-pi:/home/pi/yolo-inference/models/fridge-yolo11n.onnx`
- 推理验证：本地与 `firecar-pi` 远端单图 ONNX 冒烟均检出 `cucumber`，置信度 `0.852293`

如果暂时没有 Roboflow API key，可先用 GitHub 5K Groceries 数据集验证训练链路：

```bash
scripts/download_groceries5k_dataset.sh
YOLO_DATA_YAML=data/groceries-5k-yolo/data.yaml YOLO_RUN_NAME=groceries-5k-public scripts/train_yolo11n_local.sh
```

如果只做冒烟训练：

```bash
YOLO_FRACTION=0.05 YOLO_EPOCHS=1 scripts/train_yolo11n_local.sh
```

## 测试规范

- 本地脚本检查：`bash -n scripts/*.sh`
- 本地训练配置检查：`cp config/yolo_public_dataset.env.example config/yolo_public_dataset.env && scripts/setup_yolo_training_local.sh`
- 本地训练冒烟：`YOLO_FRACTION=0.05 YOLO_EPOCHS=1 scripts/train_yolo11n_local.sh`，需先准备公开数据集。
- 本地导出检查：`scripts/export_yolo11n_onnx_local.sh`，需先完成训练并产生 `best.pt`。
- 远程硬件检查：`scripts/remote_probe.sh firecar-pi`
- 远程运行时检查：`scripts/remote_runtime_check.sh firecar-pi`
- OpenCL 实验检查：`scripts/deploy_llamacpp_opencl.sh firecar-pi`，默认输出 `opencl_runtime=...` 和 `activated=0`；当前 `llama-server --list-devices` 输出 `unsupported GPU 'Mali-T860'` 与空设备列表，不能切换为默认运行时。
- 远程 YOLO 检查：`scripts/remote_yolo_check.sh firecar-pi`
- 模型配置后服务检查：`ssh firecar-pi '~/vlm-inference/bin/health_vlm.sh'`
- 当前智能冰箱 Qwen2.5-VL GGUF 已通过远端 `/v1/models` health 检查，能力包含 `multimodal`；离线 `llama-mtmd-cli` 单图冒烟已完成模型加载、图片编码并输出部分 JSON，识别到 `黄瓜/蔬菜`，但 128 token 完整生成在 900 秒内未结束。
- 图片推理测试必须在模型配置完成后进行，使用 OpenAI-compatible `/v1/chat/completions` 传入图片 URL 或 base64 图片。
- YOLO 图片检测测试必须在 ONNX 模型放入 `~/yolo-inference/models` 后进行。

## 禁止操作

- 不在 `firecar-pi` 上安装 vLLM/SGLang/Docker/NVIDIA Container Toolkit；该设备无 GPU 且无免密 sudo。
- 不从 Ubuntu 22.04 强行混装 `libssl3` 到 Ubuntu 20.04；如需 `libssl.so.3`，使用项目用户态 OpenSSL 3。
- 不默认下载大模型。NanoPC-T4 根分区只有约 4.2 GiB 可用空间，模型需按需放入 `~/vlm-inference/models`。
- 不默认在 `firecar-pi` 上安装 PyTorch/Ultralytics 完整训练栈；板端 YOLO 默认只跑 ONNX Runtime CPU 推理。
- 不提交 `config/*.env`、公开数据集、训练输出、模型文件、日志、PID 文件或私钥。
- 不删除远程用户目录中与本项目无关的文件。

## 修改历史

- `codex-vlm-inference-framework.0.1.0.202606241031`
  - 新增 CPU-only VLM 推理框架部署说明。
  - 新增远程探测、llama.cpp ARM64 CPU 运行时部署、运行时检查脚本。
  - 支持预编译包不兼容时自动回退到源码编译，并兼容远程 CMake 3.16 的 server UI assets 构建限制。
  - 明确 `firecar-pi` 的硬件约束与模型未定时的配置方式。

- `codex-vlm-inference-framework.0.1.1.202606241212`
  - 新增用户态 OpenSSL 3 部署脚本，用于补齐 Ubuntu 20.04 缺失的 `libssl.so.3/libcrypto.so.3`。
  - VLM 启动与运行时检查脚本支持自动加载 `~/vlm-inference/runtime/openssl-current`。

- `codex-vlm-inference-framework.0.2.0.202607021029`
  - 新增 YOLO ONNX CPU-only 部署脚本与远程检查脚本。
  - 新增轻量 `yolo_detect.py`，支持 ONNX Runtime 推理、阈值过滤、NMS、JSON 输出和可选标注图输出。
  - README 增补 YOLO 模型放置、运行命令、测试规范与板端禁止安装完整 PyTorch/Ultralytics 训练栈的约束。
  - `.gitignore` 增补 Python 缓存文件，避免语法检查产物进入版本管理。

- `codex-vlm-inference-framework.0.3.0.202607021202`
  - 新增公开数据集训练配置模板，默认指向 Roboflow `fridge-dataset/fridge-food-images/14`。
  - 新增本地 YOLO11n 训练环境安装、Roboflow 数据集下载、GitHub 5K Groceries 数据集转换、训练、ONNX 导出和远程模型同步脚本。
  - 新增公开数据集训练文档，明确 YOLO 只做冰箱食材入口识别，风险判断后续交给 VLM/规则层。
  - `.gitignore` 增补本地数据集、训练输出、虚拟环境与权重文件忽略规则。

- `codex-vlm-inference-framework.0.3.1.202607021335`
  - 使用 Roboflow `fridge-dataset/fridge-food-images/14` 完成本地 YOLO11n 公开数据集基线训练，最佳第 10 轮 `mAP50=0.80546`、`mAP50-95=0.55512`。
  - ONNX 导出默认固定 `YOLO_ONNX_OPSET=19`，兼容远程 `onnxruntime==1.16.3`。
  - 修复 `scripts/lib_config.sh` 在 macOS Bash 3.2 空 override 数组下触发 `set -u` 的问题。
  - 修复 YOLO 模型远端同步脚本的本地/远端路径解析，避免 `YOLO_REMOTE_DIR=~/...` 被本机展开为 `/Users/...`。
  - 完成本地 ONNX 推理、`firecar-pi` 远端模型加载检查和远端实际图片检测验证。

- `codex-vlm-inference-framework.0.3.2.202607021853`
  - 从公开数据集 checkpoint 继续训练 YOLO11n 至 80 轮，并使用 `patience=0` 保证跑满目标轮数。
  - 更新公开数据集基线指标：最佳第 66 轮 `mAP50=0.81128`、`mAP50-95=0.58557`，最终第 80 轮 `mAP50=0.80675`、`mAP50-95=0.58047`。
  - 重新导出 ONNX opset 19 模型并同步部署到 `firecar-pi:/home/pi/yolo-inference/models/fridge-yolo11n.onnx`。
  - 完成本地与 `firecar-pi` 远端单图 ONNX 推理冒烟验证，并清理临时验证输出。

- `codex-vlm-inference-framework.0.4.0.202607022022`
  - 将本机 `smart_fridge_qwen25vl_gguf` 模型包部署到 `firecar-pi:/home/pi/vlm-inference/models/smart-fridge-qwen25vl/`。
  - 配置远端 `vlm.env` 使用 `smart-fridge-qwen25vl-merged-Q4_K_M.gguf` 与 `mmproj-smart-fridge-qwen25vl-Q8_0.gguf`，并保持 CPU-only、低并发、`ctx=2048` 的保守参数。
  - 校验远端 GGUF sha256 与本机一致，并启动 `llama-server` 通过 `/v1/models` health 检查。
  - 使用 `llama-mtmd-cli` 做远端单图冒烟，验证模型加载、图片编码和中文 JSON 输出链路可用；完整 128 token 输出在 900 秒内未结束。

- `codex-vlm-inference-framework.0.4.1.202607022154`
  - 新增实验性 `scripts/deploy_llamacpp_opencl.sh`，可在 `firecar-pi` 上编译独立 `llama-b9773-opencl` runtime，默认不切换 `runtime/current`。
  - 针对 NanoPC-T4 的 Mali/OpenCL 2.2 环境关闭 Adreno 专用 OpenCL kernels，并对 b9773 的 QCOM large-buffer OpenCL 3.0 fallback 做条件编译补丁。
  - 实测 OpenCL runtime 能编译成功，但 `llama-server --list-devices` 将 `Mali-T860` 判定为 unsupported 并输出空设备列表，因此当前仍保留 CPU runtime 作为默认方案。
  - 编译测试后恢复原 CPU `llama-server`，并确认 `/v1/models` health 检查可用。

- `codex-vlm-inference-framework.0.4.2.202607022200`
  - 新增显式 `VLM_TIMEOUT=3600` 配置，将 `llama-server` 读写超时固定为 1 小时。
  - VLM health 脚本新增 `VLM_HEALTH_TIMEOUT=60`，避免健康检查在网络异常时无限等待。
  - 同步更新远端 `firecar-pi` 当前 VLM 服务配置，保留 CPU runtime 作为默认方案。
