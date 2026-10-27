# MeetPoint AI · V0.0.0.1

**双人见面地点推荐 · Windows 桌面版**

This software only supports Simplified Chinese and isn't designed for regions outside of mainland China.

两个人从不同位置出发，MeetPoint AI 会搜索真实地点、分别规划路线，再结合通勤时间与场所偏好推荐见面地点。你还可以在地图上画圆或多边形，限定地点必须落在指定区域内。

## 目录

- [功能亮点](#功能亮点)
- [系统要求](#系统要求)
- [安装与卸载](#安装与卸载)
- [首次配置](#首次配置)
- [开始使用](#开始使用)
- [推荐模式与区域限制](#推荐模式与区域限制)
- [常见问题](#常见问题)
- [从源码运行与构建](#从源码运行与构建)
- [数据与授权说明](#数据与授权说明)
- [许可与致谢](#许可与致谢)

## 功能亮点

| 能力 | 说明 |
| --- | --- |
| 双人出发点 | 搜索地址、地图选点，或直接输入 GCJ-02 经纬度 |
| 多种出行方式 | 公共交通、驾车、步行、骑行；分别显示 A、B 两侧路线与预计时间 |
| 限定推荐范围 | 在地图上画圆或多边形；程序会复核结果是否位于区域内 |
| 多种推荐模式 | AI 综合、最短总通勤、最少换乘、场所类型优先、自然语言自定义 |
| 一眼比较通勤 | 结果列表用相同比例的短条显示两人通勤时间，点选后查看详情与路线 |
| 结果与服务状态 | 可导出 JSON，并查看高德地图、高德 Web 服务和 DeepSeek 的运行状态 |
| 诊断工具 | 随安装包附带 `meetpoint-debug.exe`，支持离线模拟、录制回放、校验与批量测试 |

## 系统要求

- **Windows 10 或 Windows 11，64 位 x64**。安装包会拒绝 32 位系统。
- 建议至少 4 GB 内存，并预留约 1 GB 磁盘空间。
- 实时地图、地点搜索和路线规划需要网络与有效的高德 Key；AI 推荐还需要 DeepSeek Key。

## 安装与卸载

1. 双击 `MeetPoint-AI-Setup-V0.0.0.1-win-x64.exe`，按向导安装。
2. 默认安装位置为 `%LOCALAPPDATA%\Programs\MeetPoint AI`，按当前用户安装，不要求管理员权限。
3. 从开始菜单启动 **MeetPoint AI**。安装时可选择是否创建桌面快捷方式。

安装包尚未进行代码签名，Windows 可能显示安全提示；请确认文件来自你信任的分发渠道。

如需静默安装，可在 PowerShell 中运行：

```powershell
.\MeetPoint-AI-Setup-V0.0.0.1-win-x64.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

可通过 Windows“已安装的应用”、开始菜单的“卸载 MeetPoint AI”，或安装目录中的 `unins000.exe` 卸载。卸载不会主动删除 `%LOCALAPPDATA%\MeetPointAI` 中的个人配置、缓存和日志。

## 首次配置

启动后打开 **工具 → 设置…**（`Ctrl+,`），在“API 凭据”页填写：

| 凭据 | 用途 |
| --- | --- |
| 高德 Web 服务 Key | 地点搜索、地址解析、路线规划 |
| 高德 Web 端（JS）Key 和安全密钥 | 内嵌地图、地图选点与绘制区域 |
| DeepSeek API Key | AI 综合比较与推荐理由 |

高德 Web 服务 Key 与 Web 端（JS）Key 类型不同，不能互换。保存后程序会重新加载相关服务；也可在 **工具 → 立即检测 API 状态** 查看检测结果。

设置保存在本机 `.env` 文件中。安装目录不可写时，程序会使用 `%LOCALAPPDATA%\MeetPointAI\.env`。**不要把自己的 `.env` 发给别人**；分享源码时使用 `.env.example` 模板。

## 开始使用

1. 在左侧设置 A、B 两个出发地点：搜索地点、在地图上选点，或输入 `经度,纬度`。
2. 选择出行方式、场所类型、推荐模式与推荐数量。自定义模式可填写“安静、适合聊天”等偏好。
3. 如需限定范围，在地图上画圆或多边形。
4. 点击左下角 **开始推荐**，等待地点检索、双方路线规划和排序完成。
5. 在结果列表比较双方通勤，点选地点查看详情；通过 **文件 → 导出当前推荐结果…** 保存 JSON。

AI 服务不可用时，程序可能切换到基础规则排序并给出提示。推荐质量仍受地图数据、接口配额与候选上限影响。

## 推荐模式与区域限制

| 模式 | 主要依据 |
| --- | --- |
| AI 综合推荐 | 综合场所、偏好、两人的路线等候选信息；输出仍须通过程序校验 |
| 最短总通勤时间 | A、B 预计通勤时间之和 |
| 最少换乘 | 双方换乘次数，再参考总时长 |
| 场所类型优先 | 优先匹配指定类型，再参考通勤 |
| 自然语言自定义 | 按你填写的偏好比较候选地点 |

你画出的圆形或多边形是**硬性范围限制**。区域内没有合适候选时，程序会提示调整条件，不会拿区域外的地点凑数。

## 常见问题

**地图空白或提示加载超时？** 使用 **工具 → 重新加载地图**，并检查网络、JS Key 与安全密钥。

**提示高德鉴权失败？** 检查 Web 服务 Key 与 JS Key 是否填反，以及各自对应的平台设置。

**没有推荐结果？** 尝试扩大区域、调整场所类型或更换出行方式。区域内没有合适候选时，程序不会用区域外地点补足数量。

**公共交通显示“步行替代”？** 短途可能没有公交方案，程序会尝试使用实际步行路线并注明。

**如何清除本机数据？** 使用 **文件 → 清除历史数据与缓存**。日志和数据库默认位于 `%LOCALAPPDATA%\MeetPointAI`。

## 从源码运行与构建

将 `MeetPoint-AI-源码-V0.0.0.1.zip` 解压，在源码根目录使用 Python 3.12 或更新版本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env  # 填入自己的 Key，或启动后在界面里填写
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m pytest -q
```

构建 Windows GUI 和独立 CLI：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm meetpoint-ai.spec
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm meetpoint-debug.spec
```

命令行诊断入口：`.\.venv\Scripts\python.exe meetpoint_debug.py --help`。本版本构建前的自动测试为 **224 项通过**；实际使用仍需在你的设备与 API 配置下验证。

## 数据与授权说明

- 使用实时功能时，位置、候选地点及路线相关信息会按功能需要发送给高德与 DeepSeek；请了解相应服务的条款、配额与费用。

## 许可与致谢
本程序代码以 PolyForm Noncommercial 1.0.0 发布，欢迎学习与二次开发，禁止商业使用。
地图数据与路线来自 高德开放平台，请遵守其服务条款与配额限制。
智能分析由 DeepSeek 提供，请遵守其使用条款。
界面基于 Qt for Python (PySide6)，遵循 LGPLv3。
使用前请自行申请并妥善保管 API Key；因 Key 泄露或超额调用产生的费用与责任由使用者承担。