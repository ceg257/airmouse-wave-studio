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

## 回放与标签编辑（2026-11-24）

回放已打开的记录时：

- **显示动作标签**：顶部色块条给出每个区间的类别与时间，各通道路径上画对应颜色的边界虚线，下方清单列出全部区间。
  手打标签读 `events.csv` 的 `label`/`start`/`end`；引导采集的记录取 `phase=1`（执行相）作为动作区间，准备相与放松相不生成动作标签。
- **编辑标签**：勾选“编辑标签”后可拖动色块边界或整段平移、双击清单里的类别改名、从空白处新建区间、删除选中行。
  保存时先把原 `events.csv` 备份为 `events.csv.bak_<时间戳>`，改动写入同目录的 `events.edited.csv`，**原 `events.csv` 不改**；
  再次打开该记录时优先读取 `events.edited.csv`。要撤销编辑，移走编辑文件即可回到原始事件。
- **对比窗口**：“设置与文件 → 新建对比窗口…”每次开一个独立进程，各自选择通道组与时间窗，互不影响。
  也可命令行 `python studio_qt.py --replay "<记录目录>" --compare --group 1 --span 10`（0=肌电，1=IMU）。
- **记住上次回放目录**：正常退出后再次启动会自动打开上次的记录目录；不恢复播放位置，仍从头播放。

已知限制：

- 每个视窗最多同时绘制 16 个标签区间，超出的只在清单中列出；短于视窗 6% 的区间不显示名称。
- 编辑后未保存就关闭窗口或切换记录**没有确认提示**，请先按 Ctrl+S。
- 多个窗口同时编辑同一记录没有冲突保护。
- 对比窗口之间没有联动暂停或同步光标。
- 引导记录的尾段若没有下一次绘制事件，按默认 2 秒并以总时长裁剪，需要人工复核。

## 验证

```powershell
.\.venv\Scripts\python test_guide_smoke.py
```

模拟测试覆盖提示时间记录、暂停重做、结束及样本保存。真实设备下的同步误差、长期稳定性仍待实测。不是独立 exe 包，需按上述方式安装依赖。

`demo_signal.py` 提供 `--demo` 用的模拟 IMU；`wave_core.py` 的演示模式优先导入它，找不到时回退到旧版 `engine.py`。
`verify_wave_v3.py`、`shoot_wave_v3.py` 是本地验证与截图脚本，含本机路径，未纳入本仓库。
