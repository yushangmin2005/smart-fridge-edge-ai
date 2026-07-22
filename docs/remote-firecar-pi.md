# firecar-pi 远程部署记录

## 探测结果

- SSH alias：`firecar-pi`
- 网络地址：`wlan0` 使用 DHCP；通过本机 SSH alias `firecar-pi` 管理，不依赖固定局域网 IP
- 主机名：`NanoPC-T4`
- 系统：Ubuntu 20.04.6 LTS
- 架构：`aarch64`
- CPU：6 核
- 内存：约 3.7 GiB
- 根分区可用空间：约 4.2 GiB
- NVIDIA GPU/CUDA：无
- Docker/Podman：无
- Python：3.8.10
- sudo：存在，但无免密 sudo
- OpenSSL：系统版本为 1.1.1f，无 `libssl.so.3/libcrypto.so.3`
- 时间同步：当前局域网 UDP NTP 超时，已改用 `smart-fridge-http-time-sync.timer` 通过 HTTPS Date 兜底校时

## 结论

该设备不能承载 vLLM/SGLang 这类 GPU 推理后端。当前采用 `llama.cpp` Ubuntu ARM64 CPU 用户态运行时，优先使用预编译包；如遇 Ubuntu 20.04 系统库不兼容，则回退到同版本源码编译。模型后续通过 `~/vlm-inference/config/vlm.env` 切换。

由于 Ubuntu 20.04 默认没有 `libssl.so.3`，且该设备无免密 sudo，不从 Ubuntu 22.04 混装系统包。项目通过 `scripts/deploy_openssl3_user.sh` 在 `~/vlm-inference/runtime/openssl-current` 下提供 OpenSSL 3 用户态共享库。

YOLO 不在该板上安装完整 PyTorch/Ultralytics 训练栈。当前采用 ONNX Runtime CPU 用户态推理，依赖安装到 `~/yolo-inference/runtime/python-packages`，模型通过 `~/yolo-inference/config/yolo.env` 指向导出的 `.onnx` 文件。

## 推荐模型策略

- 调试优先使用小型 GGUF VLM，例如 llama.cpp 官方多模态文档列出的 `SmolVLM-256M` 或 `SmolVLM-500M`。
- 微调模型需要先转换/量化为 GGUF，再放入 `~/vlm-inference/models`。
- YOLO 训练/微调优先在其他机器完成，导出 ONNX 后再放入 `~/yolo-inference/models`。
- 当前磁盘空间不适合直接放置多份 2B+ 以上模型。
