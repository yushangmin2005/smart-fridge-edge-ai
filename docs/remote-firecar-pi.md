# firecar-pi 远程部署记录

## 探测结果

- SSH alias：`firecar-pi`
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

## 结论

该设备不能承载 vLLM/SGLang 这类 GPU 推理后端。当前采用 `llama.cpp` Ubuntu ARM64 CPU 用户态运行时，优先使用预编译包；如遇 Ubuntu 20.04 系统库不兼容，则回退到同版本源码编译。模型后续通过 `~/vlm-inference/config/vlm.env` 切换。

## 推荐模型策略

- 调试优先使用小型 GGUF VLM，例如 llama.cpp 官方多模态文档列出的 `SmolVLM-256M` 或 `SmolVLM-500M`。
- 微调模型需要先转换/量化为 GGUF，再放入 `~/vlm-inference/models`。
- 当前磁盘空间不适合直接放置多份 2B+ 以上模型。
