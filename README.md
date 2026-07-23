# 智能冰箱边缘 AI 管理系统

## 项目背景

家庭冰箱承担着日常食品储存的核心功能，但冰箱内部的食材种类、数量、存放时间和状态通常缺少连续记录。用户往往只能依靠记忆管理库存，容易出现食材被遮挡或遗忘、重复购买、临近变质时未及时处理等问题。联合国环境规划署发布的《2024 年食物浪费指数报告》显示，2022 年全球产生约 10.5 亿吨食物浪费，其中 60% 来自家庭，这说明家庭储存和消费环节具有直接的改进空间。[来源：UNEP](https://www.unep.org/news-and-stories/press-release/world-squanders-over-1-billion-meals-day-un-report)

减少浪费也已成为明确的社会和行业需求。《中华人民共和国反食品浪费法》第十四条提出，家庭应按照日常生活实际需要采购、储存和制作食品。[来源：国家市场监督管理总局](https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/bgt/art/2023/art_5f92392ecaa14e048bd9a673715c20ca.html) 现行国家标准 `GB/T 37877—2025` 进一步将设备运行与环境参数采集、食品存储信息管理、保质期预警、自动补货提醒和健康饮食建议纳入智能电冰箱的发展方向。[来源：国家市场监督管理总局](https://www.samr.gov.cn/xw/sj/art/2025/art_05f13b4a01e44eed855251d12cd6c2f3.html)

这类需求不能只靠一次图像识别解决。系统既要知道“当前有什么”，也要理解食材何时出现、是否重复入库、外观状态如何变化，并结合温度、湿度、门状态、存放时长和历史记录持续判断。若所有图片都直接上传云端处理，还会受到网络稳定性、响应时间、服务成本和家庭图像隐私等因素制约，因此需要在低功耗边缘设备上建立可离线运行、云端能力可选接入的识别链路。

基于上述问题，本项目以普通冰箱的低成本智能化改造为场景，使用 ESP32-S3 采集环境和门状态，以 RK3399 作为边缘计算节点：YOLO 只负责变化定位和候选区域生成，VLM 负责食物名称与可见状态分析，规则和大模型再融合传感器数据、库存 ID 与历史状态形成提醒和建议；结果写入 SQLite，并通过中文 Web 面板展示库存、环境和状态变化。项目目标是完成可解释、可追踪的辅助管理闭环，而不是替代保质期标签、专业检测或食品安全结论。

## 当前实现

当前目标是在远程 `firecar-pi` 上部署可替换模型的边缘推理运行时。该设备实际为 NanoPC-T4，Ubuntu 20.04 ARM64，约 3.7 GiB 内存，无 NVIDIA GPU/CUDA/Docker，因此 VLM 默认采用 CPU-only 的 `llama.cpp` 多模态推理路线，YOLO 采用 ONNX Runtime CPU 推理路线。已额外尝试 Mali-T860 OpenCL runtime，但当前 `llama.cpp` OpenCL 后端会将 `Mali-T860` 判定为 unsupported，不能作为默认方案。

部署脚本默认使用 SSH alias `firecar-pi`。请在本机 `~/.ssh/config` 中将其映射到 NanoPC-T4 的实际地址，例如 `pi@<NANOPC_IP>`；智能冰箱 Web 面板默认地址为 `http://<NANOPC_IP>:8090/`。

公开模型仓库：

- VLM：[BeimingJingli/smart-fridge-qwen25vl-gguf](https://huggingface.co/BeimingJingli/smart-fridge-qwen25vl-gguf)
- YOLO：[BeimingJingli/smart-fridge-yolo11n](https://huggingface.co/BeimingJingli/smart-fridge-yolo11n)

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
- 智能冰箱数据库：SQLite，板端默认路径为 `~/smart-fridge/data/fridge.sqlite3`
- 智能冰箱调度：`ffmpeg` + v4l2 每 1 小时拍照一次，默认使用 `/dev/video10` UVC 摄像头
- 环境传感器接入：Python 标准库 `termios/select` 持续读取 ESP32-S3 的 `115200 8N1` JSON Lines v2 数据，原子写入 `sensor_state.json`
- ESP32-S3 烧录：macOS 使用 Arduino CLI + `esp32:esp32@2.0.17` 编译，NanoPC-T4/RK3399 使用隔离的 `esptool==4.5.1` 经 CP2102N 完成备份和 USB 烧录
- 智能冰箱 Web 前端：Python 标准库 `http.server` + SQLite 只读查询，默认端口 `8090`
- 智能冰箱自启动：`systemd --user` 管理传感器、VLM、Web、自动识别管线和维护定时器；`loginctl linger` 保持用户服务开机常驻
- 智能冰箱维护：Python 标准库脚本定时清理临时图片、YOLO/VLM 输出和日志，并把异常提醒写入 `alerts.json` 供 Web 展示
- 远端维护工具：Pi Coding Agent `@earendil-works/pi-coding-agent`，用户态安装到 `firecar-pi:~/.local`
- 板端 Pi 工具扩展：TypeScript Pi extension，将 NanoPC-T4 GPIO、I2C、串口、摄像头等外设能力注册为 Pi agent tool
- 板端 GPIO/I2C 系统工具：`gpiod`、`libgpiod-dev`、`i2c-tools`，配合 `gpio/i2c` 用户组和 udev 规则开放非 root 访问
- 云端建议：每轮自动识别后将当前 active objects 和最新环境传感器快照发送到 DeepSeek `deepseek-v4-flash`，结构化建议写入 `pipeline_state.json`
- 板端时间同步：`systemd-timesyncd` 固定 IP NTP 源 + `smart-fridge-http-time-sync.timer` HTTPS Date 兜底校时
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

# 部署智能冰箱 SQLite 主库与数据库 CLI
scripts/deploy_smart_fridge_db.sh firecar-pi

# 安装 GPIO/I2C 系统工具和设备权限规则；需要远端 sudo
scripts/install_remote_board_system_deps.sh firecar-pi

# 安装板端时间同步兜底服务；需要远端 sudo
scripts/install_remote_time_sync.sh firecar-pi

# 检查远程 SQLite schema 与完整性
scripts/remote_smart_fridge_db_check.sh firecar-pi

# 启用/查看智能冰箱 systemd 用户级自启动服务
ssh firecar-pi '~/smart-fridge/bin/install_autostart.sh'
ssh firecar-pi '~/smart-fridge/bin/status_autostart.sh'

# 运行维护清理与异常检查
ssh firecar-pi '~/smart-fridge/bin/fridge_maintenance.sh run-all'
ssh firecar-pi '~/smart-fridge/bin/fridge_monitor.sh'
ssh firecar-pi '~/smart-fridge/bin/fridge_cleanup.sh'

# 清空测试库存数据；执行前建议先备份 ~/smart-fridge/data/fridge.sqlite3
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh reset --yes'

# 启动/停止/查看一小时自动识别链路
ssh firecar-pi '~/smart-fridge/bin/start_pipeline.sh'
ssh firecar-pi '~/smart-fridge/bin/status_pipeline.sh'
ssh firecar-pi '~/smart-fridge/bin/stop_pipeline.sh'

# 启动/停止/查看智能冰箱 Web 状态面板
ssh firecar-pi '~/smart-fridge/bin/start_web.sh'
ssh firecar-pi '~/smart-fridge/bin/status_web.sh'
ssh firecar-pi '~/smart-fridge/bin/stop_web.sh'

# 启动/停止/查看 ESP32-S3 环境采集
ssh firecar-pi '~/smart-fridge/bin/start_sensor.sh'
ssh firecar-pi '~/smart-fridge/bin/status_sensor.sh'
ssh firecar-pi '~/smart-fridge/bin/stop_sensor.sh'

# 编译传感器固件，并通过 RK3399 备份和烧录 ESP32-S3
ESP32_FIRMWARE_DIR=/path/to/smart_fridge_sensor_node \
  scripts/flash_esp32s3_via_rk3399.sh firecar-pi

# 查看远端 Pi agent 版本
ssh firecar-pi 'bash -lc "pi --version; piagent --version"'

# 部署板端 GPIO/外设 Pi tools
scripts/deploy_pi_board_tools.sh firecar-pi

# 通过 Pi agent 调用板端外设清单工具做冒烟检查
ssh firecar-pi 'bash -lc "pi --provider deepseek --model deepseek-v4-flash --thinking off --no-session --no-builtin-tools --tools board_inventory -p '\''Use board_inventory once and summarize available device groups in Chinese.'\''"'
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

智能冰箱数据库部署后，远程目录结构为：

```text
~/smart-fridge/
  bin/                 # fridge_db/fridge_db_check 脚本
  config/smart_fridge.env
  data/fridge.sqlite3  # SQLite 主库，默认不提交
  data/pipeline_state.json
  data/sensor_state.json # 已纠正门状态的最新传感器快照
  data/alerts.json
  data/backups/        # 手动备份库，默认不提交
  firmware/backups/    # 每次烧录前的 8 MB ESP32 全闪存备份
  firmware/releases/   # 已校验哈希的可重复烧录固件包
  firmware/current     # 最近一次通过烧录和传感器验收的固件软链接
  logs/fridge-alerts.log
  logs/fridge-web.log
  logs/fridge-sensor.log
  run/fridge-web.pid
  run/fridge-sensor.pid
  tmp/captures/        # 定时拍照临时图，最多保留 24 张
  tmp/crops/           # 新增目标裁剪图
  tmp/yolo/            # YOLO JSON 输出
  tmp/vlm/             # VLM 严格 JSON 输出
  runtime/fridge_db.py
  runtime/fridge_maintenance.py
  runtime/fridge_pipeline.py
  runtime/fridge_sensor.py
  runtime/fridge_web.py
  runtime/vlm_food_prompt.txt
```

自启动部署后，远端用户级 systemd 单元为：

```text
~/.config/systemd/user/smart-fridge-vlm.service
~/.config/systemd/user/smart-fridge-sensor.service
~/.config/systemd/user/smart-fridge-web.service
~/.config/systemd/user/smart-fridge-pipeline.service
~/.config/systemd/user/smart-fridge-maintenance.service
~/.config/systemd/user/smart-fridge-maintenance.timer
```

本地 YOLO 训练产物目录为：

```text
data/                 # 公开数据集，默认不提交
runs/                 # Ultralytics 训练输出，默认不提交
models/               # 导出的 ONNX/classes 文件，默认不提交
.venv-yolo/           # 本地训练虚拟环境，默认不提交
```

远端 Pi agent 安装位置：

```text
~/.local/bin/pi       # Pi Coding Agent 主命令
~/bin/pi              # 用户 PATH 软链接
~/bin/piagent         # 兼容命令名，指向同一 Pi CLI
~/.pi/agent/auth.json # Pi agent 认证文件，权限 0600，默认不提交
~/.pi/agent/settings.json # Pi agent 全局设置，权限 0600，默认不提交
~/.pi/agent/extensions/firecar-board-tools.ts # 板端 GPIO/外设 tools 扩展
```

非交互 SSH 不一定加载远端用户 PATH，检查或调用 Pi agent 时优先使用 `ssh firecar-pi 'bash -lc "pi --version"'`，或直接调用 `~/.local/bin/pi`。

当前远端 Pi agent 已配置 DeepSeek 服务：

```text
defaultProvider=deepseek
defaultModel=deepseek-v4-flash
可选高质量模型=deepseek-v4-pro
```

不在 README 或 Git 中保存 DeepSeek API key。验证命令：

```bash
ssh firecar-pi 'bash -lc "pi --list-models deepseek"'
ssh firecar-pi 'bash -lc "pi --provider deepseek --model deepseek-v4-flash --thinking off --no-tools --no-session -p '\''reply exactly: pong'\''"'
```

板端 GPIO/外设 tools 由 `pi_extensions/firecar_board_tools.ts` 提供，部署后 Pi agent 会从全局扩展目录自动发现并注册以下工具：

```text
board_inventory       # 汇总 GPIO/I2C/SPI/串口/摄像头设备节点与可用命令
board_gpio_info       # 查看 GPIO chip 与 line 信息；需要 libgpiod 工具
board_gpio_read       # 读取 GPIO line；需要 gpioget 与设备权限
board_gpio_write      # 写 GPIO line；默认禁用，需要环境变量和确认语
board_i2c_scan        # 扫描 I2C bus；需要 i2c-tools 与设备权限
board_i2c_read        # 读取 I2C register；需要 i2cget 与设备权限
board_i2c_write       # 写 I2C register；默认禁用，需要环境变量和确认语
board_camera_capture  # 用 ffmpeg 从 /dev/video10 默认摄像头抓取单帧 JPEG
```

当前 `firecar-pi` 可见 `/dev/gpiochip0..4`、`/dev/i2c-0/1/2/4/7/9/10`、`/dev/ttyS0`、`/dev/ttyS4` 和 `/dev/video*`；未发现 `/dev/spidev*`。已安装 `gpioinfo/gpioget/gpioset` 与 `i2cdetect/i2cget/i2cset`，`pi` 已加入 `gpio`、`i2c` 组，`/dev/gpiochip*` 与 `/dev/i2c-*` 已通过 udev 规则开放给对应组。

写硬件默认关闭。只有在确认接线、pin mapping、总线地址和外设安全后，才允许给 Pi agent 进程设置：

```bash
SMART_FRIDGE_PI_TOOLS_ALLOW_GPIO_WRITE=1
SMART_FRIDGE_PI_TOOLS_ALLOW_I2C_WRITE=1
```

## 智能冰箱识别链路

当前智能冰箱采用混合识别架构：YOLO 负责低成本地发现变化并生成候选区域；`llama.cpp` 承载的 VLM 主识别服务负责判断候选是否为食物、输出食物名称和状态，并将确认后的结构化结果写入数据库。最终判断与建议由数据库中同一食物 ID 的历史内容、最新视觉状态、存放时间和规则层共同生成。

详细职责边界见 [docs/smart-fridge-hybrid-pipeline.md](docs/smart-fridge-hybrid-pipeline.md)。

## 环境传感器链路

ESP32-S3 通过 CP2102N 串口每秒上报一行 v2 JSON，包含环境温度、开尔文温度、湿度、NTC 探头温度、估算/过温标志、门状态、序列号、运行时间和三类传感器健康标志。`smart-fridge-sensor.service` 独占读取串口，并持续更新 `~/smart-fridge/data/sensor_state.json`。

当前硬件上报的 `door_open/door_state` 与实际门状态相反，因此默认配置 `SMART_FRIDGE_SENSOR_DOOR_INVERTED=1`。纠正在 `fridge_sensor.py` 接入层只执行一次：设备上报 `closed/false` 时，对外统一提供实际 `open/true`；原始值保留在快照的 `raw` 和 `reported_*` 字段中供排查。固件的 `door_open_count` 无法仅凭单帧可靠反推为实际开门次数，因此对 AI 明确标记为 `reported_door_open_count`，不在主界面展示为实际计数。

```bash
SMART_FRIDGE_SENSOR_DEVICE=auto
SMART_FRIDGE_SENSOR_BAUD_RATE=115200
SMART_FRIDGE_SENSOR_STATE_PATH=/home/pi/smart-fridge/data/sensor_state.json
SMART_FRIDGE_SENSOR_DOOR_INVERTED=1
SMART_FRIDGE_SENSOR_STALE_SECONDS=10
SMART_FRIDGE_SENSOR_RETRY_SECONDS=5
SMART_FRIDGE_SENSOR_READ_TIMEOUT_SECONDS=10
```

## 自动识别管线

板端自动链路由 `~/smart-fridge/bin/fridge_pipeline.sh` 执行单轮识别，`~/smart-fridge/bin/start_pipeline.sh` 启动后台循环。默认每 3600 秒执行一次：

```text
摄像头拍照 -> 保留最近 24 张临时图 -> YOLO 生成低阈值变化候选框
  -> YOLO 零候选时复查上一轮对象区域；无历史对象时使用整帧候选
  -> 用类别无关 IoU + 视觉指纹与上一轮状态匹配
  -> unchanged 维持原 food_id，不重复调用 VLM
  -> suppressed 跳过此前已被 VLM 判定为非食物且画面未变的背景候选
  -> added 裁剪真正新增/变化的框，与最新环境快照一起发送给 llama.cpp VLM
  -> VLM 独立判断是否为食物、具体名称和可见状态；确认是食物后写 SQLite
  -> 无法确认消失时进入 pending_removals，连续两轮缺失后才写入 food.removed
  -> 本轮结束后将 active objects、变化摘要和最新环境快照发送给 DeepSeek
```

YOLO 在该链路中只承担变化定位和候选区域生成，其类别与置信度仅作为调试和路由元数据，不作为最终食物名称。VLM 是食物身份和可见状态的语义判断来源。为兼顾召回率与 RK3399 的慢速 VLM，管线以较低阈值接收候选，再用 64 位视觉指纹抑制未变化对象和重复背景；同一位置的像素内容发生明显变化时会重新进入 VLM。若 YOLO 完全漏检，管线会在上一轮对象位置重新计算视觉指纹，避免把单次零候选直接解释为空冰箱；只有明确识别出替代对象，或同一对象连续缺失达到确认次数后，才关闭旧库存记录。

单轮入口使用 Python `fcntl.flock` 非阻塞独占锁，手动触发和 systemd 定时轮次不能同时修改 `pipeline_state.json` 与 SQLite；锁已占用时后启动的轮次返回 `cycle_already_running` 并跳过。锁行为依据 [Python `fcntl` 官方文档](https://docs.python.org/3/library/fcntl.html)。

关键配置在远程 `~/smart-fridge/config/smart_fridge.env`：

```bash
SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS=3600
SMART_FRIDGE_CAPTURE_KEEP=24
SMART_FRIDGE_CAMERA_DEVICE=/dev/video10
SMART_FRIDGE_YOLO_BIN=/home/pi/yolo-inference/bin/yolo_detect.sh
SMART_FRIDGE_YOLO_CHANGE_MIN_CONFIDENCE=0.45
SMART_FRIDGE_CHANGE_HASH_MAX_DISTANCE=16
SMART_FRIDGE_REMOVAL_CONFIRMATIONS=2
SMART_FRIDGE_PIPELINE_LOCK_PATH=/home/pi/smart-fridge/run/fridge-pipeline.lock
SMART_FRIDGE_WRITE_FALLBACK_ON_VLM_ERROR=0
SMART_FRIDGE_VLM_URL=http://127.0.0.1:8080/v1/chat/completions
SMART_FRIDGE_VLM_TIMEOUT=3600
SMART_FRIDGE_VLM_USE_RESPONSE_FORMAT=1
SMART_FRIDGE_CLOUD_ADVICE_ENABLED=1
SMART_FRIDGE_CLOUD_ADVICE_MODEL=deepseek-v4-flash
SMART_FRIDGE_CLOUD_ADVICE_AUTH_PATH=/home/pi/.pi/agent/auth.json
SMART_FRIDGE_SENSOR_STATE_PATH=/home/pi/smart-fridge/data/sensor_state.json
SMART_FRIDGE_SENSOR_DOOR_INVERTED=1
```

`SMART_FRIDGE_YOLO_MIN_CONFIDENCE` 仅保留为旧部署兼容回退；配置了 `SMART_FRIDGE_YOLO_CHANGE_MIN_CONFIDENCE` 后，管线不再使用旧的 `0.65` 语义过滤阈值。

`SMART_FRIDGE_WRITE_FALLBACK_ON_VLM_ERROR=0` 表示 VLM 超时或失败时不写入食物数据库，候选会在下一轮重新分析。即使人工开启旧的容错写入，记录也只能标记为待确认的 `unknown_food`，不能沿用 YOLO 类别。

VLM prompt 位于 `~/smart-fridge/runtime/vlm_food_prompt.txt`，要求只输出 JSON，字段包含 `food_name`、`category`、`composition`、`freshness`、`freshness_score`、`visible_state`、`storage_advice`、`risk_level`、`confidence` 和 `notes`。提示词明确要求忽略 YOLO 类别的语义暗示，并根据图片独立识别食物；新鲜度和储存建议可参考带时间戳的温湿度、探头和实际门状态，过期数据或估算探头值不能作为确定性结论。正式请求按照 [llama-server 文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) 通过 `response_format.schema` 约束字段类型和枚举，客户端还会拒绝带选项分隔符的名称或不合法枚举；校验失败时不写 SQLite。

每轮自动识别完成后，管线会把当前 `active_objects`、本轮新增/未变/移除摘要、下次识别时间和最新传感器快照发送给 DeepSeek 云端模型，要求返回 JSON：`summary`、`risk_level`、`action_items`、`item_suggestions`、`next_check`。结果写入 `~/smart-fridge/data/pipeline_state.json` 的 `cloud_advice` 字段，并由 Web 面板展示。DeepSeek API key 默认从远端 Pi agent 的 `~/.pi/agent/auth.json` 读取，不写入项目仓库。

维护定时器默认每 10 分钟执行一次 `~/smart-fridge/bin/fridge_maintenance.sh run-all`，清理临时文件并检查磁盘、服务 PID、传感器数据时效、最近照片、SQLite、Web API、VLM API 和 GPIO/I2C 命令。最新检查写入 `~/smart-fridge/data/alerts.json`，历史写入 `~/smart-fridge/logs/fridge-alerts.log`。

## Web 状态面板

板端 Web 前端由 `~/smart-fridge/bin/fridge_web.sh` 启动，默认监听 `0.0.0.0:8090`，页面每 30 秒刷新一次。它只读取现有 SQLite、`pipeline_state.json`、`sensor_state.json`、临时照片目录和管线日志，不主动触发 YOLO/VLM 推理。默认视图聚焦运行是否正常、冰箱环境、最新画面、下次识别时间、当前库存、需注意食物、最近变化和近期照片；服务 PID、数据库路径、YOLO/VLM 输出文件和日志收进“调试信息”折叠区。页面展示层会把常见状态、事件类型、风险等级和 YOLO 食材类别汉化；JSON API 同时提供纠正后的环境数据和原始串口帧，方便联调。

```bash
ssh firecar-pi '~/smart-fridge/bin/start_web.sh'
ssh firecar-pi '~/smart-fridge/bin/status_web.sh'
```

浏览器访问：

```text
http://<NANOPC_IP>:8090/
```

页面展示内容：

- 自动识别和主识别服务是否可用，默认不展示 PID。
- 冰箱实际门状态、环境温度、相对湿度、内部探头温度、数据更新时间和连接健康状态。
- 最新拍照画面、下次识别时间和最近 24 张临时照片。
- 异常提醒：磁盘、服务、数据库、接口和板端工具状态。
- 云端综合建议、当前库存、需注意数量、食物新鲜度和风险建议。
- 最近入库、更新、移除变化事件。
- 折叠调试信息：数据库路径、服务 PID、YOLO/VLM 输出文件和管线日志 tail。

可调整配置：

```bash
SMART_FRIDGE_WEB_HOST=0.0.0.0
SMART_FRIDGE_WEB_PORT=8090
SMART_FRIDGE_WEB_REFRESH_SECONDS=30
```

## 数据库命令

初始化或检查远程 SQLite 主库：

```bash
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh init'
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh health'
```

写入一次 YOLO/VLM 观察记录：

```bash
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh ingest \
  --image-ref /home/pi/yolo-inference/samples/test.jpg \
  --yolo-json /home/pi/yolo-inference/outputs/test.json \
  --vlm-name 黄瓜 \
  --vlm-state normal \
  --vlm-confidence 0.72 \
  --vlm-description 新鲜蔬菜，外观无明显腐败 \
  --advice-label normal'
```

查询当前库存与单个食物历史：

```bash
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh list-foods'
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh show-food --food-id food_xxx'
```

清空测试数据但保留 schema：

```bash
ssh firecar-pi 'mkdir -p ~/smart-fridge/data/backups && cp -a ~/smart-fridge/data/fridge.sqlite3 ~/smart-fridge/data/backups/fridge-before-reset-$(date -u +%Y%m%dT%H%M%SZ).sqlite3'
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh reset --yes'
ssh firecar-pi '~/smart-fridge/bin/fridge_db.sh health'
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
VLM_EXTRA_ARGS="--image-min-tokens 64 --image-max-tokens 64 --jinja"
```

该模型包可从公开的 [Hugging Face VLM 仓库](https://huggingface.co/BeimingJingli/smart-fridge-qwen25vl-gguf) 获取。远端磁盘在部署后剩余约 1.3 GiB；当前参数优先保证 NanoPC-T4 CPU-only 环境可启动。`VLM_TIMEOUT=3600` 将 `llama-server` 读写超时显式固定为 1 小时；发起图片推理的客户端也需要设置不低于 1 小时的 HTTP 超时。由于 YOLO 已经负责定位并裁剪新增区域，VLM 默认使用 `image-min-tokens=64`、`image-max-tokens=64` 做裁剪图语义识别；`llama.cpp` 对 Qwen-VL grounding 任务提示 `image-min-tokens=1024` 更适合精细定位，但会进一步增加内存占用与推理时间。

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
- 数据库 CLI 语法检查：`python3 -m py_compile smart_fridge_runtime/fridge_db.py`
- 自动管线语法检查：`python3 -m py_compile smart_fridge_runtime/fridge_pipeline.py`
- 环境采集与 Web 语法检查：`python3 -m py_compile smart_fridge_runtime/fridge_sensor.py smart_fridge_runtime/fridge_web.py`
- 维护脚本语法检查：`python3 -m py_compile smart_fridge_runtime/fridge_maintenance.py`
- 单元测试：`python3 -m unittest discover -s tests -v`，必须覆盖门状态双向取反、数据时效、VLM/DeepSeek 请求载荷、Web API 字段、低阈值变化候选、类别无关匹配、视觉内容变化、零候选复查、连续缺失确认、单实例锁、背景候选抑制和非法 VLM 结果拒绝。
- 本地 SQLite 冒烟：使用临时目录执行 `fridge_db.py init/ingest/list-foods/show-food/health`，完成后删除临时库。
- 本地管线差分冒烟：使用假 YOLO 与 mock VLM 执行三轮 `added -> unchanged -> removed`，并检查 24 张临时图保留。
- 本地训练配置检查：`cp config/yolo_public_dataset.env.example config/yolo_public_dataset.env && scripts/setup_yolo_training_local.sh`
- 本地训练冒烟：`YOLO_FRACTION=0.05 YOLO_EPOCHS=1 scripts/train_yolo11n_local.sh`，需先准备公开数据集。
- 本地导出检查：`scripts/export_yolo11n_onnx_local.sh`，需先完成训练并产生 `best.pt`。
- 远程硬件检查：`scripts/remote_probe.sh firecar-pi`
- 远程连接检查：`ssh firecar-pi 'hostname; whoami; ip -4 addr show wlan0'`，应返回登录用户 `pi` 和主机名 `NanoPC-T4`。
- 远程运行时检查：`scripts/remote_runtime_check.sh firecar-pi`
- OpenCL 实验检查：`scripts/deploy_llamacpp_opencl.sh firecar-pi`，默认输出 `opencl_runtime=...` 和 `activated=0`；当前 `llama-server --list-devices` 输出 `unsupported GPU 'Mali-T860'` 与空设备列表，不能切换为默认运行时。
- 远程 YOLO 检查：`scripts/remote_yolo_check.sh firecar-pi`
- 远程 SQLite 检查：`scripts/remote_smart_fridge_db_check.sh firecar-pi`
- 远程维护检查：`ssh firecar-pi '~/smart-fridge/bin/fridge_maintenance.sh run-all'`，当前 `alerts.alert_count=0`。
- 远程自启动检查：`ssh firecar-pi 'systemctl --user is-active smart-fridge-vlm.service smart-fridge-sensor.service smart-fridge-web.service smart-fridge-pipeline.service smart-fridge-maintenance.timer'`，五项应均为 `active`。
- 远程传感器检查：`ssh firecar-pi '~/smart-fridge/bin/fridge_sensor.sh --check'`，应返回 `fresh=true`，且当前设备上报 `closed/false` 时纠正字段为 `door_state=open`、`door_open=true`。
- ESP32-S3 烧录脚本检查：`bash -n scripts/flash_esp32s3_via_rk3399.sh scripts/rk3399_flash_esp32s3.sh`；正式烧录必须依次通过本机构建、bundle SHA-256、ESP32-S3/8 MB 探测、烧录前全闪存备份、写入校验、传感器 v2 连续帧和服务恢复。
- 远程管线检查：真实 `/dev/video10` 摄像头应通过 `ffmpeg -f v4l2` 拍出 640x360 JPEG；YOLO 输出只计为变化候选，食物名称必须来自 VLM；同图连续执行时应保持原 `food_id` 且不增加 SQLite observation；YOLO 零候选时必须复查上一轮区域，单次漏检不得写 `food.removed`；已拒绝背景在视觉指纹未变化时不得重复调用 VLM。
- 模型配置后服务检查：`ssh firecar-pi '~/vlm-inference/bin/health_vlm.sh'`
- 当前智能冰箱 Qwen2.5-VL GGUF 已通过远端 `/v1/models` health 检查，能力包含 `multimodal`；离线 `llama-mtmd-cli` 单图冒烟已完成模型加载、图片编码并输出部分 JSON，识别到 `黄瓜/蔬菜`，但 128 token 完整生成在 900 秒内未结束。
- 图片推理测试必须在模型配置完成后进行，使用 OpenAI-compatible `/v1/chat/completions` 传入图片 URL 或 base64 图片。
- YOLO 图片检测测试必须在 ONNX 模型放入 `~/yolo-inference/models` 后进行。
- 远端 Pi agent 检查：`ssh firecar-pi 'bash -lc "pi --version; piagent --version"'`，当前版本为 `0.80.3`。
- 远端 Pi agent DeepSeek 检查：`ssh firecar-pi 'bash -lc "pi --list-models deepseek"'`，并用 `deepseek-v4-flash` 做最小 `pong` 请求。
- 板端 Pi tools 检查：`scripts/deploy_pi_board_tools.sh firecar-pi`，再通过 `pi --tools board_inventory` 调用扩展工具确认 GPIO/I2C/串口/摄像头设备清单可返回。
- 板端 GPIO/I2C 检查：`scripts/install_remote_board_system_deps.sh firecar-pi` 后，`gpioinfo` 与 `i2cdetect -l` 应能以 `pi` 用户运行。
- 云端建议检查：使用 `SMART_FRIDGE_CLOUD_ADVICE_MOCK_JSON` 做本地/远端隔离冒烟；正式链路通过 `pipeline_state.json.cloud_advice.ok=true` 和 Web “云端建议”卡片验证。
- 时间同步检查：`ssh firecar-pi 'timedatectl; cat /var/lib/smart-fridge/time-sync-state'`；当前网络下 UDP NTP 会超时，因此用 `smart-fridge-http-time-sync.timer` 每 30 分钟通过 HTTPS Date 兜底校准系统时间和 RTC。

## 禁止操作

- 不在 `firecar-pi` 上安装 vLLM/SGLang/Docker/NVIDIA Container Toolkit；该设备无 GPU 且无免密 sudo。
- 不从 Ubuntu 22.04 强行混装 `libssl3` 到 Ubuntu 20.04；如需 `libssl.so.3`，使用项目用户态 OpenSSL 3。
- 不默认下载大模型。NanoPC-T4 根分区只有约 4.2 GiB 可用空间，模型需按需放入 `~/vlm-inference/models`。
- 不默认在 `firecar-pi` 上安装 PyTorch/Ultralytics 完整训练栈；板端 YOLO 默认只跑 ONNX Runtime CPU 推理。
- 不提交 `config/*.env`、公开数据集、训练输出、模型文件、日志、PID 文件或私钥。
- 不提交 SQLite 主库、WAL/SHM 附属文件或临时数据库。
- 不删除远程用户目录中与本项目无关的文件。
- 不在未确认接线、pin mapping、总线地址和外设安全前启用 Pi board tools 的 GPIO/I2C 写操作。
- `smart-fridge-sensor.service` 运行期间不再用 `cat/head` 等其他进程持续读取同一串口；串口数据由单一采集服务持有，网页和 AI 只读 `sensor_state.json`。
- 不直接执行 `erase_flash`、eFuse 写入或跳过备份烧录；ESP32-S3 更新统一使用项目脚本，脚本会独占串口并在退出时恢复 `smart-fridge-sensor.service`。
- 不把 sudo 密码、DeepSeek API key、Roboflow API key 或其他凭据写入 README、脚本、systemd unit 或 Git。

## 开源许可

本仓库源代码采用 [MIT License](LICENSE)。数据集、基础模型、微调模型和第三方依赖不属于该许可证授权范围，使用时需分别遵守其原始许可证和服务条款。

## 修改历史

- `main.0.15.2.202607232032`
  - 为自动管线增加 `fcntl.flock` 非阻塞单实例锁，手动识别与 systemd 定时轮次不再并发写状态文件和 SQLite。
  - YOLO 零候选时自动复查上一轮活动对象和已拒绝区域；没有历史区域时才使用整帧 VLM 候选。
  - 新增两轮消失确认与同 `food_id` 重识别保护，单次 YOLO/VLM 漏检不再直接写 `food.removed`。
  - 补充 4 项并发与零候选回归测试，本地共 20 项单元测试。
  - NanoPC-T4 真实零候选复测使用 `previous_regions` 保留苹果，`removed=[]`、`pending_removals=[]`；真实锁冲突返回 `cycle_already_running`。
  - 远端 SQLite、状态文件和 Web API 的活动 `food_id` 一致，VLM/传感器/Web/自动管线/维护定时器均为 `active`，维护告警为 0。

- `main.0.15.1.202607231928`
  - 为 VLM 响应增加 llama-server JSON Schema，严格约束字段类型、类别及状态枚举。
  - 客户端新增二次语义校验，拒绝带选项分隔符的食物名称和非法枚举，校验失败时不写 SQLite。
  - 重写 VLM JSON 示例，避免模型把 `normal|attention|danger|unknown` 等选项列表原样复制到结果。
  - 新增 3 项 VLM 输出校验测试，本地共 16 项单元测试。
  - 将异常测试记录保留为审计事件并移出活跃状态；RK3399 使用真实照片复测后输出 `food_name=小白菜`、`freshness=normal`、`risk_level=normal`、`confidence=0.8`，合法结果写入 SQLite。

- `main.0.15.0.202607231356`
  - 将 YOLO 明确调整为低阈值变化候选和区域定位层，类别与置信度不再作为食物身份结论；VLM 独立负责是否为食物、具体名称和可见状态。
  - 新增类别无关 IoU 与 64 位视觉指纹匹配，同框内容明显变化时重新触发 VLM，光照小幅变化或 YOLO 类别抖动时保留原 `food_id`。
  - `pipeline_state.json` 新增 `rejected_candidates`，VLM 已拒绝且画面未变的背景候选会被抑制，避免每小时重复执行慢速推理。
  - VLM 失败时默认不写库并在下一轮重试；显式容错记录只允许使用 `unknown_food`，不会采用 YOLO 类别或置信度作为识别结论。
  - 新增 `SMART_FRIDGE_YOLO_CHANGE_MIN_CONFIDENCE=0.45` 与 `SMART_FRIDGE_CHANGE_HASH_MAX_DISTANCE=16`，并补充 7 项变化路由单元测试。
  - 本地 Python/Shell 检查与 13 项单元测试通过；远端运行时 SHA-256 与本机一致，VLM、传感器、Web 和自动管线服务均为 `active`。
  - RK3399 隔离实测使用真实小白菜照片连续执行两轮：第一轮 `added=1`，第二轮 `unchanged=1`、视觉指纹距离为 `0`，临时 SQLite 始终保持 `foods=1`、`food_observations=1`、`food_events=1`，未重复入库。

- `codex-vlm-inference-framework.0.14.0.202607222022`
  - 为 GitHub 公开发布补充 MIT 许可证、公开模型链接和通用部署地址，移除个人电脑路径、固定局域网地址与 USB 设备序列号。
  - ESP32-S3 烧录脚本改为自动发现唯一 CP2102N 串口，并让 esptool 根据已探测芯片自动设置 Flash 容量。
  - 无论传感器服务原先是否运行，烧录后都必须通过连续 v2 帧验收，再恢复原服务状态并更新当前固件链接。

- `codex-vlm-inference-framework.0.13.0.202607211501`
  - 新增 macOS 编译、RK3399 USB 烧录链路，固定使用 Arduino ESP32 2.0.17 与匹配的 `esptool 4.5.1`。
  - 板端烧录前验证 bundle SHA-256、芯片型号和 8 MB Flash，并自动保存完整 Flash 备份；禁止擦除 Flash 或写 eFuse。
  - 烧录期间自动停止串口采集服务，退出时恢复服务；写入完成后要求 ESP32-S3 连续输出递增的 JSON Lines v2 帧才更新当前固件链接。

- `codex-vlm-inference-framework.0.12.1.202607221242`
  - 新增面向比赛材料和项目答辩的项目背景，说明家庭食物浪费、食品存储管理和智能冰箱行业需求。
  - 明确 YOLO、VLM、传感器、数据库和 Web 面板构成的项目切入点，以及系统仅提供辅助判断的能力边界。

- `codex-vlm-inference-framework.0.12.0.202607102131`
  - 新增 ESP32-S3 串口环境采集模块和 `smart-fridge-sensor.service`，持续保存带时效标记的 v2 传感器快照。
  - 在接入层统一纠正实际门状态：设备上报 `closed/false` 对外转换为 `open/true`，同时保留原始字段用于排查。
  - 本地 VLM 与 DeepSeek 云端综合建议请求均新增完整传感器上下文，温湿度、探头、门状态与健康标志可参与判断。
  - Web 面板新增“冰箱环境”区域和环境采集健康状态；维护任务新增传感器服务、断连、过期和子传感器异常检查。
  - 新增 6 项标准库单元测试，覆盖门状态纠正、数据时效、AI 载荷和 Web 展示字段。

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

- `codex-vlm-inference-framework.0.5.0.202607022217`
  - 新增智能冰箱混合识别链路文档，明确 YOLO 负责预识别、入库提醒和重复候选标记。
  - 明确 `llama.cpp` VLM 负责主识别、食物状态评估、数据库写入和结构化观察结果。
  - README 增补 YOLO + VLM + 数据库融合的当前系统职责摘要。

- `codex-vlm-inference-framework.0.6.0.202607022230`
  - 新增 `smart_fridge_runtime/fridge_db.py`，用 Python 标准库 `sqlite3` 落地 `foods`、`food_observations`、`food_events` 三类核心表。
  - 新增智能冰箱 SQLite 部署与远程检查脚本，默认数据库路径为 `firecar-pi:~/smart-fridge/data/fridge.sqlite3`。
  - README 增补数据库部署、初始化、写入观察记录、查询库存和测试规范。

- `codex-vlm-inference-framework.0.7.0.202607022254`
  - 新增 `smart_fridge_runtime/fridge_pipeline.py`，串联定时拍照、YOLO 差分、目标裁剪、VLM 严格 JSON 分析和 SQLite 写入。
  - 新增 `vlm_food_prompt.txt`，要求 llama/VLM 输出食物名称、种类、组成、新鲜度、建议和置信度等固定 JSON 字段。
  - 远程部署脚本新增 `fridge_pipeline.sh`、`start_pipeline.sh`、`stop_pipeline.sh`、`status_pipeline.sh`，默认每 1 小时运行一次并保留最近 24 张拍照图。
  - 远程验证真实摄像头拍照、真实 YOLO 检测、mock VLM 写库、同图 unchanged 和消失 removed 差分路径。

- `codex-vlm-inference-framework.0.7.1.202607030113`
  - 压缩 VLM JSON prompt，减少 CPU-only Qwen2.5-VL 单次裁剪分析的输入 token。
  - 将当前智能冰箱 VLM 裁剪图识别参数调整为 `image-min-tokens=64`、`image-max-tokens=64`，由 YOLO 承担定位，VLM 专注语义与状态分析。
  - 管线现在会保存 VLM 原始 HTTP 响应、模型文本和规范化 JSON，便于定位真实模型未写库时是解析失败、非食物判断还是推理超时。
  - 增加 `SMART_FRIDGE_YOLO_MOCK_JSON` 测试钩子，用于单框验证真实 VLM 到 SQLite 的闭环，不影响正式 YOLO 调用。
  - 远端单框真实 VLM 闭环验证通过：`milk` 裁剪图由 Qwen2.5-VL 输出 `food_name=牛奶`、`category=dairy`、`freshness=attention`、`confidence=0.9`，并写入临时 SQLite 的 `foods`、`food_observations`、`food_events`。

- `codex-vlm-inference-framework.0.8.0.202607031429`
  - 新增 `smart_fridge_runtime/fridge_web.py`，使用 Python 标准库提供智能冰箱 Web 状态面板和 JSON API。
  - Web 页面展示最新拍照、当前库存、食物状态、变化事件、active objects 和管线日志。
  - 部署脚本新增 `fridge_web.sh`、`start_web.sh`、`stop_web.sh`、`status_web.sh`，默认监听 `0.0.0.0:8090`。
  - 配置样例新增 `SMART_FRIDGE_WEB_HOST`、`SMART_FRIDGE_WEB_PORT`、`SMART_FRIDGE_WEB_REFRESH_SECONDS`。

- `codex-vlm-inference-framework.0.8.1.202607031443`
  - Web 前端增加展示层汉化映射，不改变 SQLite 和 JSON API 的原始字段值。
  - 将页面中的运行状态、库存状态、新鲜度、风险等级、变化事件和常见 YOLO 食材类别显示为中文。

- `codex-vlm-inference-framework.0.8.2.202607031452`
  - 修复 Web 前端在浏览器缩放、平板宽度和手机宽度下的横向溢出问题。
  - 给主网格、卡片、表格容器和事件文本补充响应式收缩与断行规则，库存表格改为容器内横向滚动。

- `codex-vlm-inference-framework.0.8.3.202607031455`
  - Web 前端移除“最近裁剪”展示卡片，保留裁剪文件和 JSON API 字段用于调试。

- `codex-vlm-inference-framework.0.8.4.202607031501`
  - 简化 Web 状态面板默认视图，去掉主界面中的 PID、数据库路径、VLM 超时、置信度、`food_id` 和日志等技术字段。
  - 新增“调试信息”折叠区，保留服务、数据库、YOLO/VLM 文件和日志信息，供排查问题时展开查看。
  - Web 服务对浏览器自动请求的 `/favicon.ico` 返回空响应，避免控制台出现无关 404。

- `codex-vlm-inference-framework.0.8.5.202607031506`
  - Web 面板新增“下次识别”时间，优先使用管线摘要中的 `next_scheduled_at`，旧日志回退为上一轮识别时间加识别间隔。
  - 自动识别管线摘要新增 `completed_at` 和 `next_scheduled_at` 字段，便于前端展示下一轮计划时间。

- `codex-vlm-inference-framework.0.8.6.202607031515`
  - 在 `firecar-pi` 用户态安装 Pi Coding Agent `@earendil-works/pi-coding-agent@0.80.3`。
  - 新增 `~/bin/pi` 与 `~/bin/piagent` 软链接，并将 `~/bin`、`~/.local/bin` 写入远端 `~/.profile` 与 `~/.bashrc`。
  - README 增补远端 Pi agent 安装位置、版本检查命令和测试规范。

- `codex-vlm-inference-framework.0.8.7.202607031538`
  - 在 `firecar-pi` 的 `~/.pi/agent/auth.json` 中配置 DeepSeek API key，文件权限固定为 `0600`，key 不写入 README 或 Git。
  - 在 `~/.pi/agent/settings.json` 中设置 Pi agent 默认 `defaultProvider=deepseek`、`defaultModel=deepseek-v4-flash`。
  - 远端验证 `pi --list-models deepseek` 可见 `deepseek-v4-flash`、`deepseek-v4-pro`，并通过最小 `pong` 请求确认 DeepSeek 服务可用。

- `codex-vlm-inference-framework.0.9.0.202607031559`
  - 自动识别管线新增云端建议步骤：每轮完成 active objects 更新后调用 DeepSeek，返回综合摘要、风险等级、行动建议和单项食物建议。
  - `pipeline_state.json` 新增 `cloud_advice` 字段；Web 面板新增“云端建议”卡片展示最新云端综合判断。
  - 配置模板和部署脚本新增 `SMART_FRIDGE_CLOUD_ADVICE_*` 配置项，默认从远端 Pi agent `auth.json` 读取 DeepSeek key，不在项目配置中保存明文 key。

- `codex-vlm-inference-framework.0.10.0.202607031619`
  - 新增 `pi_extensions/firecar_board_tools.ts`，将 NanoPC-T4 GPIO、I2C、串口、摄像头等板端外设清单与操作入口注册为 Pi agent tools。
  - 新增 `scripts/deploy_pi_board_tools.sh`，部署扩展到 `firecar-pi:~/.pi/agent/extensions/firecar-board-tools.ts` 并做 Pi CLI 加载检查。
  - 记录当前 `firecar-pi` 已暴露的 GPIO/I2C/串口/视频设备节点，以及 `libgpiod/i2c-tools` 命令缺失、无免密 sudo 的限制。
  - GPIO/I2C 写工具默认禁用，必须显式设置环境变量并传入确认语后才会尝试改变硬件状态。

- `codex-vlm-inference-framework.0.11.0.202607031927`
  - 新增 `smart_fridge_runtime/fridge_maintenance.py`，支持临时文件清理、日志裁剪、磁盘/服务/数据库/Web/VLM/GPIO/I2C 异常检查，并将结果写入 `alerts.json`。
  - Web 面板新增“异常提醒”卡片，显示维护检查生成的磁盘、服务、数据库、接口和板端工具状态。
  - `deploy_smart_fridge_db.sh` 新增 systemd 用户服务部署：VLM、Web、自动识别管线和维护 timer 均可开机自启动。
  - 新增 `scripts/install_remote_board_system_deps.sh`，安装 `gpiod/libgpiod-dev/i2c-tools`，配置 `gpio/i2c` 组和 udev 权限规则。
  - `fridge_db.py` 新增 `reset --yes`，用于清空测试库存数据但保留 SQLite schema。
  - 远端已备份并清空测试库，当前 `foods=0`、`food_observations=0`、`food_events=0`，维护检查 `alert_count=0`。

- `codex-vlm-inference-framework.0.11.1.202607061039`
  - 更新当时的 `firecar-pi` DHCP 地址，本机 `~/.ssh/config` 已同步并验证 `ssh firecar-pi` 可登录 `NanoPC-T4`。
  - Web 状态面板通过 NanoPC-T4 的 `8090` 端口提供服务，`/api/overview` 已验证可访问。
  - 记录远端 `timedatectl` 当前未完成系统时间同步，避免误判照片年龄和下次识别时间。

- `codex-vlm-inference-framework.0.11.2.202607061045`
  - 新增 `scripts/install_remote_time_sync.sh`，为 `firecar-pi` 安装时间同步修复：固定 IP NTP 配置、HTTPS Date 兜底脚本和 systemd timer。
  - 远端时区改为 `Asia/Shanghai`，系统时间与 RTC 已校准到当前时间。
  - 记录当前局域网 UDP NTP 超时，正式可用状态以 `/var/lib/smart-fridge/time-sync-state` 和 timer 状态为准。

- `codex-vlm-inference-framework.0.11.3.202607061052`
  - `pipeline_state.json` 新增 `last_cycle` 字段，保存最近一轮拍照、YOLO、云端建议和下次识别时间摘要。
  - Web 状态面板优先读取 `pipeline_state.json.last_cycle`，旧日志解析仅作为兼容回退，避免 systemd journal 与文件日志不同步导致“下次识别”显示旧时间。
  - 新增 `SMART_FRIDGE_YOLO_MIN_CONFIDENCE=0.65` 管线阈值，过滤当前天花板画面中 0.516 置信度的低置信误检，避免误触发 1 小时 VLM 分析。
