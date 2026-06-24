# 智能消防巡检车 VLM 推理框架

当前目标是在远程 `firecar-pi` 上部署可替换模型的 VLM 推理运行时。该设备实际为 NanoPC-T4，Ubuntu 20.04 ARM64，约 3.7 GiB 内存，无 NVIDIA GPU/CUDA/Docker，因此不使用 vLLM/SGLang，而采用 CPU-only 的 `llama.cpp` 多模态推理路线。

## 技术栈

- 运行时：`llama.cpp` Ubuntu ARM64 CPU 包；若系统库不兼容，则自动回退到同版本源码编译
- 兼容库：用户态 OpenSSL 3，共享库路径为远程 `~/vlm-inference/runtime/openssl-current`
- 模型格式：GGUF，多模态模型需要模型 GGUF 与可选 `mmproj` GGUF，或直接使用 `-hf` 加载 llama.cpp 支持的多模态仓库
- 服务接口：`llama-server` OpenAI-compatible `/v1/chat/completions`
- 部署方式：SSH 用户态安装到远程 `~/vlm-inference`，不依赖 sudo、Docker 或系统级服务
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

启动、停止与状态检查：

```bash
ssh firecar-pi '~/vlm-inference/bin/start_vlm.sh'
ssh firecar-pi '~/vlm-inference/bin/status_vlm.sh'
ssh firecar-pi '~/vlm-inference/bin/health_vlm.sh'
ssh firecar-pi '~/vlm-inference/bin/stop_vlm.sh'
```

## 测试规范

- 本地脚本检查：`bash -n scripts/*.sh`
- 远程硬件检查：`scripts/remote_probe.sh firecar-pi`
- 远程运行时检查：`scripts/remote_runtime_check.sh firecar-pi`
- 模型配置后服务检查：`ssh firecar-pi '~/vlm-inference/bin/health_vlm.sh'`
- 图片推理测试必须在模型配置完成后进行，使用 OpenAI-compatible `/v1/chat/completions` 传入图片 URL 或 base64 图片。

## 禁止操作

- 不在 `firecar-pi` 上安装 vLLM/SGLang/Docker/NVIDIA Container Toolkit；该设备无 GPU 且无免密 sudo。
- 不从 Ubuntu 22.04 强行混装 `libssl3` 到 Ubuntu 20.04；如需 `libssl.so.3`，使用项目用户态 OpenSSL 3。
- 不默认下载大模型。NanoPC-T4 根分区只有约 4.2 GiB 可用空间，模型需按需放入 `~/vlm-inference/models`。
- 不提交 `config/*.env`、模型文件、日志、PID 文件或私钥。
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
