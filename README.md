# AirMouse Wave Studio

Windows 肌电/IMU 可视化与引导采集工具。支持九通道 EMG、六通道 IMU、实时显示、原始 CSV 保存、回放及引导采集。

## 安装运行

需要 Python（开发环境为 3.14）和系统自带 OpenSSH 客户端。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-wave.txt
.\.venv\Scripts\python studio_qt.py --demo
```

真实设备模式去掉 `--demo`。程序监听本机 127.0.0.1:9002。
开发板推流程序是 TCP 客户端；若使用 SSH 反向隧道，在另一个终端运行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=10 -R 127.0.0.1:19002:127.0.0.1:9002 USER@BOARD_IP
```

然后在开发板启动兼容的 rk_stream，目标 127.0.0.1:19002。板端 SDK/蓝牙驱动未包含在本仓库。界面设置中的采样率须与设备匹配（默认 EMG 500 Hz、IMU 200 Hz）。

## 引导采集

点击“引导采集”，修改动作列表、次数、准备/执行/放松时间。程序随机安排动作并自动保存。暂停会作废当前尝试，继续时重新准备本轮；关闭面板结束该次记录。动作名称是预期指令，尚未验证实际执行是否正确。

记录 cue_requested、cue_painted、trial_invalid、trial_complete、session_end，试次和尝试编号存于 events.csv 的 label JSON 字段。guide_ 事件不要当作普通 start/end 动作标签读取。

提示绘制回调不是屏幕实际发光时刻，也不是肌肉动作起点。使用主机单调时钟关联接收时间；BLE 缓冲和传输延迟尚未校准。timeline_s 只是单调显示时间，不能当作精确硬件同步。事件缺失结束记录或断流结束的尝试应视为不完整。

## 文件与隐私

每次记录输出 emg.csv、imu.csv、events.csv、metadata.json。原始九通道值保留；显示去直流仅影响波形。断流结束当前保存，重新连接后需新建记录。

录制数据、日志、个人配置、虚拟环境不会纳入版本控制。首次启动请在“设置与文件”中设置保存目录和受试者编号。请勿上传未经授权的受试者数据。

## 验证

```powershell
.\.venv\Scripts\python test_guide_smoke.py
```

模拟测试覆盖提示时间记录、暂停重做、结束及样本保存。真实设备下的同步误差、长期稳定性仍待实测。不是独立 exe 包，需按上述方式安装依赖。
