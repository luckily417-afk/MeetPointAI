# API 配置教程

本文档说明 MeetPoint AI 需要的 4 个凭据，以及如何申请、填写与排错。

程序只从 **环境变量或 `.env` 文件**读取凭据，源码中不存在任何硬编码 Key。

---

## 1. 需要哪些 Key

| 变量名 | 用途 | 来源 |
| --- | --- | --- |
| `AMAP_WEB_KEY` | 高德 **Web 服务 API**：POI 搜索、地理/逆地理编码、驾车/步行/骑行/公交路径规划、健康探测 | 高德开放平台 → 应用管理 → 添加 Key → 服务平台选择「Web服务」 |
| `AMAP_JS_KEY` | 高德 **Web端(JS) API**：桌面内嵌地图、标记、圆形/多边形绘制 | 同一应用下再添加一个 Key → 服务平台选择「Web端(JS)」 |
| `AMAP_JS_SECURITY_CODE` | JS API 2.0 的安全密钥（`securityJsCode`） | 与 JS Key 成对生成 |
| `DEEPSEEK_API_KEY` | DeepSeek：需求理解、候选比较、推荐理由 | DeepSeek 开放平台 → API Keys |

> 高德 JS Key 与 Web 服务 Key **不能混用**：把 JS Key 拿去调 Web 服务会返回 `INVALID_USER_KEY`/`USERKEY_PLAT_NOMATCH`。

---

## 2. 申请高德 Key

1. 打开 <https://console.amap.com/dev/key/app>，注册/登录并完成实名认证。
2. 「应用管理 → 我的应用 → 创建新应用」，名称随意（如 `MeetPointAI`）。
3. 在应用下「添加 Key」：
   - **Web服务**：Key 名称 `meetpoint-web`，服务平台选 `Web服务`；
   - **Web端(JS)**：Key 名称 `meetpoint-js`，服务平台选 `Web端(JS)`，并会同时给出「安全密钥」。
4. 如需限制来源，JS Key 的域名白名单请保留为空（本程序通过 `QWebEngineView.setHtml` 注入页面，来源为 `https://webapi.amap.com/`，若设置了白名单可能被拒绝）。
5. 复制两个 Key 与安全密钥到 `.env`。

---

## 3. 申请 DeepSeek Key

1. 打开 <https://platform.deepseek.com/api_keys>。
2. 「创建 API Key」，复制 `sk-` 开头的字符串（只显示一次）。
3. 在「充值」页面确认余额 > 0，否则调用返回 HTTP 402 / `QUOTA_LIMITED`。

---

## 4. 填写 `.env`

```ini
AMAP_JS_KEY=你的JS_KEY
AMAP_JS_SECURITY_CODE=你的JS安全密钥
AMAP_WEB_KEY=你的Web服务KEY
DEEPSEEK_API_KEY=sk-你的Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro

# 可选
MPAI_HTTP_TIMEOUT=12
MPAI_LLM_TIMEOUT=120
MPAI_LLM_EFFORT=low
MPAI_LATENCY_WARN_MS=2500
MPAI_HEALTH_INTERVAL=60
MPAI_TOP_N=5
MPAI_MAX_CANDIDATES=20
MPAI_AI_CANDIDATES=12
MPAI_DATA_DIR=
```

字段说明：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | 也可用 `deepseek-flash`（更快更省，理由略简） |
| `MPAI_LLM_EFFORT` | `low` | 推理强度；`low` 可显著减少思考 token 与截断概率 |
| `MPAI_LLM_TIMEOUT` | 120 | 单次对话超时（秒） |
| `MPAI_HTTP_TIMEOUT` | 12 | 高德请求超时（秒） |
| `MPAI_AI_CANDIDATES` | 12 | 送入 DeepSeek 的候选上限（过多会拉长思考、增加截断风险） |
| `MPAI_MAX_CANDIDATES` | 20 | 参与路线规划的候选上限 |
| `MPAI_DATA_DIR` | 空 | 留空即 `%LOCALAPPDATA%\MeetPointAI` |

> `.env` 已写入 `.gitignore`，切勿提交或分享。

---

## 5. 打包后如何配置

`dist\MeetPointAI\MeetPointAI.exe` 会读取**同目录**下的 `.env`。打包脚本会自动复制一份；
若要换 Key，直接编辑 `dist\MeetPointAI\.env` 后重启程序即可。

也可以不改文件，用系统环境变量覆盖（优先级更高，取决于启动方式）：

```powershell
$env:AMAP_WEB_KEY = "新的KEY"
.\dist\MeetPointAI\MeetPointAI.exe
```

---

## 6. 常见错误码与处理

| 现象 | 高德 infocode | 含义 | 处理 |
| --- | --- | --- | --- |
| 鉴权失败 | `10001` / `10005` / `10006` / `10009` | Key 无效 / 安全密钥错 / 平台不匹配 | 核对 Key 类型；JS Key 不要用于 Web 服务 |
| 权限不足 | `10012` / `10021` | 未开通该服务，或瞬时流控 | 控制台检查服务勾选；程序会自动重试一次 |
| 配额受限 | `10003` / `10044` / `10041` / `10029` | 日配额、QPS 或海外调用受限 | 缩小区域、降低请求频率，或次日再试 |
| 服务异常 | `20000` / `20003` | 高德侧故障 | 稍后重试，界面会显示"服务异常" |
| DeepSeek 401 | — | Key 无效 | 重新生成 Key |
| DeepSeek 402 | — | 余额不足 | 充值 |
| DeepSeek 429 | — | 限流 | 降低频率，程序自动退避 |

界面顶部三个状态胶囊的悬浮提示会给出**状态、最近检测/成功时间、延迟、HTTP 码、业务码、错误类型**，
底部「服务日志」标签页还会记录每次探测与业务请求的结果（不含任何 Key）。

---

## 7. 自检命令

```powershell
# 源码
.\.venv\Scripts\python.exe main.py --self-test

# 打包产物（报告写到 %LOCALAPPDATA%\MeetPointAI\self_test.json）
dist\MeetPointAI\MeetPointAI.exe --self-test
```

自检会真实调用：高德健康探测、POI 搜索、驾车路线、DeepSeek 对话，各自独立判定，便于快速定位是哪一路凭据出问题。
