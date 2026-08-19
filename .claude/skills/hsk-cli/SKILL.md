---
name: hsk-cli
description: HSK CLI 运维助手 — 内网穿透、文件托管、项目部署
author: HSK Team
license: MIT
tags: [cli, tunnel, nat-traversal, file-hosting, deploy, ai-agent]
---

# HSK CLI — 零配置公网预览

## 何时使用

✅ **应该使用**:
- 用户需要暴露本地服务到公网（内网穿透）
- 用户需要上传文件或目录获取公网链接（文件托管）
- 用户需要构建并部署前端项目（Build + Upload）
- 用户需要排查 HSK CLI 相关问题
- 用户提到"公网访问""外网访问""暴露端口""上传文件"

❌ **不应该使用**:
- 简单的文件系统操作（使用 bash 直接操作）
- 纯网络诊断（使用 curl/wget 等工具）
- 非 HSK 相关的 CLI 工具使用
- 不依赖公网预览的本地开发任务

## 自由度级别

**本 Skill 采用低自由度（严格命令模式）**

- **命令执行**: 低自由度（必须严格按照文档中的命令格式执行）
- **错误处理**: 低自由度（必须按错误码对应的建议操作）
- **用户呈现**: 中等自由度（关键信息必须包含：公网地址、资源ID、操作状态）
- **沙盒判断**: 高自由度（根据环境特征自行调整策略）

## 核心工作流程

```
1. 快速判断 → 2. 环境判断（沙盒/正常） → 3. 策略选择 → 4. 执行命令 → 5. 状态检测
```

### 快速判断（最短路径）

| 用户意图 | 直接命令 | 说明 |
|----------|----------|------|
| 分享文件/目录 | `hsk-cli host <路径> --format json` | 单文件或目录均可 |
| 部署前端项目 | `hsk-cli deploy --format json` | 自动 build 并上传 |
| 暴露动态服务 | `hsk-cli tunnel --ip <IP> --port <PORT> --format json` | WebSocket/API/SSR 等 |
| 不确定 | 先 `deploy`，失败再 `tunnel` | 静态托管优先 |

### 阶段 1：环境判断

1. 检测是否在沙盒/容器环境
2. 检测方法：
   - `echo $CI` — 非空表示沙盒/CI
   - `[ -w "$HOME" ]` — 失败表示文件系统受限
   - `[ -d "$HOME/.hsk" ]` — 不存在表示无法持久化
3. 沙盒中：去掉 `--open`、避免 `--detach`、优先 `host`

### 阶段 2：策略选择

**严格优先级**：
1. 用户要分享文件/目录 → `host`（多文件项目必须上传整个目录）
2. 用户要部署前端项目 → `deploy`（自动 build 后上传构建产物）
3. 用户要暴露 WebSocket/API/动态服务 → `tunnel`
4. 不确定时 → 先 `deploy`，失败再 `tunnel`

**沙盒特殊规则**：
- 浏览器打不开 → 直接告诉用户链接，不要 `--open`
- 进程保活不了 → 前台运行，不要 `--detach`
- 网络不通 → 改用 `host`，不要重复 `tunnel`

### 阶段 3：执行命令

**输出格式**：所有命令默认 `--format pretty`（人类可读），AI 解析推荐 `--format json`。可用 `--dry-run` 预览不执行。

#### 文件托管（首选）

```bash
hsk-cli host <路径> --format json
```

- 支持单文件或目录（目录自动打包 zip）
- 目录无 `index.html` 时，必须指定 `--entry-file`
- 更新资源：`--resource-id <id>`
- 复用检测：`--reuse`

#### 构建并部署（首选）

```bash
hsk-cli deploy --format json
```

- 自动执行 `npm run build` → 上传 `dist/`
- 参数：`--build-cmd`、`--build-dir`、`--no-build`、`--resource-id`、`--entry-file`

#### 内网穿透（降级）

```bash
hsk-cli tunnel --ip <IP> --port <PORT> --format json
```

- 复用检测：`--reuse`
- 强制架构：`--arch <arch>`（如 `win64`, `macos-arm64`）
- 强制重新下载：`--force-download`
- 沙盒环境：去掉 `--detach`，前台运行
- 正常环境：推荐 `--detach` 后台运行

### 阶段 4：状态检测

用户更新内容后，**不要直接说"继续使用之前的链接"**。

先检测：

```bash
hsk-cli status --format json
```

- `valid: true` → 告诉用户"链接仍然有效，刷新即可"
- `valid: false` → 重新执行 `host --reuse` 或 `tunnel --reuse`

## 错误处理标准

### 错误码体系

| 错误码 | 场景 | 应对 |
|--------|------|------|
| `TIMEOUT` | 启动超时（30秒） | 检查本地服务是否监听、网络是否正常 |
| `BINARY_NOT_FOUND` | 客户端未下载 | 运行 `hsk-cli download` 或 `hsk-cli update` |
| `FILE_NOT_FOUND` | 文件不存在 | 提示用户检查路径 |
| `INVALID_PORT` | 端口无效 | 端口号 1-65535；macOS/Linux < 1024 需 root |
| `UPLOAD_FAILED` | 上传失败 | 检查网络、文件大小 |
| `PROCESS_EXIT` | 进程异常退出 | 检查本地服务、端口冲突 |
| `PERMISSION_DENIED` | 权限不足 | macOS 检查"本地网络"权限；端口 < 1024 需 sudo |
| `ENTRY_FILE_MISSING` | 目录无入口文件 | 提示使用 `--entry-file` |
| `UNKNOWN` | 未知错误 | 检查参数、查看日志 |

### 沙盒静默拦截识别

| 现象 | 判断 | 应对 |
|------|------|------|
| `--open` 无响应 | 不报错也不打开浏览器 | 跳过 `--open`，告诉用户手动复制链接 |
| `--detach` 进程消失 | `tunnel list` 找不到 | 去掉 `--detach`，前台运行 `tunnel` |
| 网络请求挂起 | 无响应无错误 | 改用 `host` |
| 文件写入丢失 | 写入后读取不到 | 用 `/tmp` 目录 |

**关键原则**：没有明确错误 = 被静默拦截。被拦截后**换方式**，不要重试。

## 命令速查

> **快捷写法**：`+host`、`+tunnel`、`+deploy` 等价于 `host`、`tunnel`、`deploy`

| 命令 | 用途 |
|------|------|
| `hsk-cli host <path>` | 上传文件或目录 |
| `hsk-cli host <path> --resource-id <id>` | 更新已有资源 |
| `hsk-cli host <path> --entry-file <file>` | 指定入口文件 |
| `hsk-cli deploy` | 构建并部署 |
| `hsk-cli deploy --no-build` | 直接上传现有目录 |
| `hsk-cli tunnel --ip <IP> --port <PORT>` | 内网穿透（前台） |
| `hsk-cli tunnel --ip <IP> --port <PORT> --detach` | 内网穿透（后台） |
| `hsk-cli tunnel list` | 列出后台隧道 |
| `hsk-cli tunnel stop --all` | 停止全部后台隧道 |
| `hsk-cli status` | 检查资源状态 |
| `hsk-cli download` | 预下载客户端 |
| `hsk-cli update` | 更新客户端 |
| `hsk-cli platform` | 检测平台信息 |
| `hsk-cli skill` | 显示 skill 安装信息 |

## 环境变量

| 变量 | 说明 |
|------|------|
| `HSK_FILE_HOSTING_API` | 覆盖默认的 ticket API host |
| `HSK_DOWNLOAD_URL` | 覆盖二进制下载基地址 |

## 常见问题

### Q: 沙盒中 `--open` 无响应怎么办？
A: 不要重试。直接告诉用户手动复制链接访问，跳过 `--open` 参数。

### Q: 用户更新内容后，如何知道之前的链接是否有效？
A: 运行 `hsk-cli status --format json` 检测。不要直接告诉用户继续使用旧链接。

### Q: 上传目录时提示"入口文件缺失"怎么办？
A: 使用 `--entry-file` 指定入口文件，如 `hsk-cli host ./dist --entry-file version.html`。

### Q: 隧道启动后秒退（无错误输出）怎么办？
A: 按顺序排查：1) 前台模式看日志 2) 检查本地服务是否监听 3) 检查端口冲突 4) 检查权限（macOS 检查"本地网络"）5) 检查防火墙 6) 查看日志文件。

### Q: macOS 下隧道无法连接本地服务？
A: 检查"系统设置 → 隐私与安全性 → 本地网络"中是否允许当前终端应用访问本地网络。

### Q: 如何复用已有资源？
A: 使用 `--reuse` 参数，如 `hsk-cli host ./dist --reuse`。CLI 会自动检测资源是否有效，有效则复用，无效则重新创建。

### Q: 沙盒中无法后台运行隧道？
A: 沙盒会 kill 后台进程。去掉 `--detach`，使用前台模式运行，保持终端连接。

### Q: 多文件项目（HTML + CSS/JS）怎么上传？
A: 必须上传整个目录，不能只传单个 HTML 文件。使用 `hsk-cli host ./dist --entry-file index.html`。

### Q: 如何指定客户端架构？
A: 使用 `--arch` 参数，如 `hsk-cli tunnel --ip 127.0.0.1 --port 3000 --arch macos-arm64`。可用值：`win32`, `win64`, `macos-x64`, `macos-arm64`, `linux-x64`, `linux-arm64`。

### Q: 如何强制重新下载客户端二进制？
A: 使用 `--force-download`，如 `hsk-cli tunnel --ip 127.0.0.1 --port 3000 --force-download`。

---

*HSK CLI — 零配置公网预览*
