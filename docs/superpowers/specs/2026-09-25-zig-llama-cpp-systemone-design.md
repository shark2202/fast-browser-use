# Zig + llama.cpp + System One 重构设计

> 状态：产品要求已锁定，技术实现通过分批 POC 锁定
> 日期：2026-09-25  
> 分支：`go_v1`

## 1. 目标

将当前 Python 分发形态重构为 Zig 主程序与 `llama.cpp` 本地推理运行时，利用 Zig 的交叉编译能力发布多平台二进制。

V1 仍保留 Playwright/Chromium 作为浏览器后端，同时内置 Jev-like 的 System One API。后续可在不改变上层 API 的前提下，将 Playwright Worker 替换为 Zig 原生 CDP 实现。

## 2. 已确认的平台目标

V1 必须支持：

- macOS arm64
- macOS x86_64
- Linux x86_64
- Linux arm64
- Windows x86_64

## 3. 总体架构

```text
┌─────────────────────────────────────────┐
│                 fbu (Zig)               │
│                                         │
│  CLI / HTTP API / Agent Loop             │
│  System One / Decision / Trace           │
│  Browser State / Verification            │
│  llama.cpp C API Adapter                 │
└───────────────┬─────────────────────────┘
                │ JSON-RPC over stdin/stdout
                ▼
┌─────────────────────────────────────────┐
│ browser-worker                           │
│ Node.js + Playwright + Chromium          │
│ snapshot.js / DOM guards / input actions │
└─────────────────────────────────────────┘
```

### 3.1 Zig Core

Zig 负责：

- Agent 执行循环
- System One 协议
- `noul`、`choice`、`score` 问题类型
- 候选动作生成与单 Token logits 评分
- 页面状态机和 freshness guards 的状态管理
- 文本字段生成调度
- 独立结果验证
- trace、telemetry 和录制元数据
- CLI、HTTP 服务和 JSON-RPC 协议
- 配置、profile、group、handoff 状态管理

建议的核心模块边界：

```text
src/
├── main.zig
├── cli/
├── api/
│   ├── systemone.zig
│   └── browser.zig
├── agent/
├── decision/
├── model/
│   ├── llama.zig
│   ├── tokenizer.zig
│   └── candidates.zig
├── browser/
│   ├── protocol.zig
│   ├── worker.zig
│   ├── state.zig
│   └── guards.zig
├── trace/
├── verify/
└── config/
```

### 3.2 llama.cpp 集成

推理链路：

```text
Zig → llama.cpp C API → GGUF model
```

核心能力：

- Qwen GGUF 模型加载
- 单 Token 候选 logits 评分
- 候选概率归一化
- 文本字段生成
- KV cache 复用
- 模型上下文生命周期管理

逻辑接口：

```zig
pub fn scoreCandidates(
    ctx: *ModelContext,
    prompt: []const u8,
    candidate_tokens: []const u32,
) !DecisionScores;

pub fn generateText(
    ctx: *ModelContext,
    prompt: []const u8,
    options: GenerationOptions,
) !GeneratedText;
```

模型格式统一迁移到 GGUF。MLX 权重和 PyTorch 权重不作为 Zig 运行时输入格式。

### 3.3 Playwright Worker

V1 保留 Playwright，但不再由 Python 驱动。

Zig 主程序通过 stdin/stdout 与 Node.js Worker 通信：

```json
{"id":1,"method":"observe","params":{"url":"https://example.com"}}
```

```json
{
  "id":1,
  "result":{
    "page":{},
    "screenshot":null
  }
}
```

Worker 继续复用当前项目的：

- `snapshot.js`
- DOM 可见性判断
- node identity
- freshness guards
- click/fill/select/scroll/wait 执行语义

Worker 的边界必须保持稳定，以便未来替换为：

```text
Zig → 原生 CDP Client → Chromium
```

而不影响 System One、Agent Loop 和公开 API。

## 4. System One API

### 4.1 通用 API

```text
POST /v1/systemone
```

目标是兼容 Jev-like 的有限决策模型：

- `noul`：二元或有限状态判断
- `choice`：从有限候选中选择一个选项
- `score`：对候选或等级进行评分

请求示例：

```json
{
  "state": "当前页面状态与任务上下文",
  "model": "local-qwen",
  "questions": {
    "next_action": {
      "type": "choice",
      "instructions": "Choose the next browser action",
      "criteria": {
        "A": "Click Search",
        "B": "Fill Query",
        "C": "DONE"
      }
    }
  }
}
```

响应示例：

```json
{
  "model": "local-qwen",
  "answers": {
    "next_action": {
      "type": "choice",
      "choice": "A",
      "probabilities": {
        "A": 0.8,
        "B": 0.15,
        "C": 0.05
      },
      "confidence": 0.8
    }
  },
  "usage": {
    "input_tokens": 0,
    "output_tokens": 1
  }
}
```

### 4.2 浏览器 API

```text
POST /v1/browser/observe
POST /v1/browser/choose
POST /v1/browser/act
POST /v1/browser/run
POST /v1/browser/verify
GET  /health
```

浏览器 API 与 `/v1/systemone` 共用同一套：

- 候选动作表示
- 单 Token logits 评分
- 概率输出
- 页面 freshness 语义
- trace 数据结构

浏览器 API 只增加浏览器领域字段，不在 HTTP 层重复实现模型决策。

## 5. 交叉编译与分发

Zig 主程序目标产物：

```text
fbu-darwin-arm64
fbu-darwin-x86_64
fbu-linux-x86_64
fbu-linux-aarch64
fbu-windows-x86_64.exe
```

构建原则：

- Zig 主程序使用 `build.zig` 统一构建
- llama.cpp CPU 部分纳入可交叉编译链
- Metal 仅在 macOS 目标启用
- CUDA 作为目标平台可选构建，不承诺从任意宿主机无条件交叉产出
- Vulkan 作为跨平台 GPU 扩展点
- GGUF 模型独立下载，不随二进制默认打包
- Chromium/Playwright 运行时按目标平台安装或随发布包提供

发布包至少包含：

```text
fbu
browser-worker/
scripts/
README
```

### 5.1 自动初始化

用户解压平台压缩包后，不要求预先安装 Python、Node.js、Playwright、Chromium 或模型下载工具。

统一入口：

```bash
fbu setup
```

`fbu setup` 必须完成：

1. 检测操作系统、CPU 架构和运行权限。
2. 下载匹配版本的 Node.js runtime。
3. 安装并配置 browser-worker 的 Node 依赖。
4. 安装固定版本 Playwright。
5. 下载匹配版本 Chromium。
6. 配置 Playwright/Chromium 缓存目录。
7. 从 ModelScope 下载指定 GGUF 模型。
8. 校验下载文件的 SHA256。
9. 写入 runtime manifest 和安装状态。
10. 注册 `fast-browser-use` skill。
11. 执行浏览器和模型 smoke test。

初始化必须满足：

- 幂等：重复执行不会重复安装已经验证的组件。
- 可恢复：中断下载后可以断点续传。
- 原子安装：组件先写入临时目录，验证通过后再切换到正式目录。
- 可诊断：每个组件记录版本、来源、revision、文件、SHA256 和安装状态。
- 可离线复用：已安装组件可在 `HF_HUB_OFFLINE` 等环境之外独立运行，不再依赖 Python 包管理器。

推荐目录：

```text
~/.fbu/
├── runtime/
│   ├── node/
│   ├── playwright/
│   └── chromium/
├── models/
│   └── qwen/
├── cache/
├── profiles/
├── storage/
└── manifests/
    └── installed.json
```

### 5.2 ModelScope 原生下载

模型下载由 Zig 原生实现，不调用 Python `modelscope` SDK。

下载器必须支持：

- ModelScope model/revision 定位
- 文件清单获取
- GGUF 文件筛选
- 大文件分片下载
- HTTP Range 断点续传
- 下载缓存
- SHA256 校验
- 临时文件清理
- 失败重试
- 代理和自定义 endpoint

默认模型配置通过 manifest 管理：

```json
{
  "provider": "modelscope",
  "model": "Qwen/Qwen3.5-9B-GGUF",
  "revision": "locked-revision",
  "files": ["*.gguf"],
  "sha256": {}
}
```

### 5.3 Node、Playwright 与 Chromium

发布包包含 browser-worker 源码和锁定的依赖 manifest，但不强制把所有平台的 Node/Chromium 二进制塞进同一个压缩包。

`fbu setup` 根据目标平台下载：

```text
Node.js runtime
Playwright package
Chromium browser revision
```

版本必须写入同一个 runtime manifest，避免 Node、Playwright 和 Chromium 版本漂移。

### 5.4 Linux 系统依赖策略

采用显式双模式：

```bash
fbu setup
fbu setup --with-system-deps
fbu setup --no-system-deps
```

- 默认 `fbu setup`：不擅自调用 sudo；检测缺少的系统库并输出可执行修复建议。
- `--with-system-deps`：明确允许调用系统包管理器并请求 sudo，自动安装 Chromium 所需依赖。
- `--no-system-deps`：禁止系统修改；缺依赖时失败并输出诊断信息。

系统包管理器适配按目标发行版拆分，不把 `apt` 假设为所有 Linux 的唯一实现。

## 6. 兼容策略

V1 尽量保留当前用户可见能力：

- 自然语言 `run`
- JSON trace
- `--expect-url`
- `--expect-title`
- `--expect-text`
- profile/group
- handoff
- Playwright 默认浏览器行为
- Agent Skill 调用入口

新增入口：

```text
fbu systemone
fbu browser
fbu serve
fbu build-targets
```

## 7. 分阶段路线

### 阶段 1：可分发 Zig Runtime

- Zig CLI
- llama.cpp CPU 推理
- GGUF 模型加载
- Playwright Worker
- System One API
- Browser API
- 多目标交叉编译

### 阶段 2：硬件后端

- macOS Metal
- CUDA
- Vulkan
- 各平台性能基准

### 阶段 3：原生 CDP

- Zig CDP client
- Chromium 生命周期管理
- snapshot/guard 语义迁移
- 移除 Node.js/Playwright Worker

## 8. POC 锁定矩阵

实现前按以下顺序验证，每个 POC 必须产出结论、证据和下一步决策。

### POC-1：Zig 交叉编译与压缩包

验证目标：

- 一台宿主机生成五个目标包。
- 每个包的 `fbu --version` 可以启动。
- `fbu doctor` 能识别目标平台、架构和安装目录。
- CLI、skill、manifest、worker 文件路径在所有平台一致。

锁定结果：

- target triple
- release 命名
- zip 目录结构
- 安装入口

### POC-2：llama.cpp GGUF 推理

验证目标：

- Zig 加载 GGUF。
- 能执行 tokenizer 和单 Token logits。
- 候选动作概率与参考实现一致。
- 文本生成能够返回严格 JSON。
- CPU 运行时不依赖 Python。

锁定结果：

- llama.cpp 集成方式
- GGUF metadata 要求
- context/KV cache 生命周期
- 默认 CPU 运行策略

### POC-3：ModelScope 原生下载

验证目标：

- 不安装 Python 也能下载 GGUF。
- 支持 revision、文件筛选、断点续传和 SHA256。
- 网络中断后能恢复。
- 下载完成后能被 llama.cpp 直接加载。

锁定结果：

- ModelScope endpoint 和请求协议
- manifest 格式
- 缓存目录
- 重试和校验策略

### POC-4：Node/Playwright/Chromium 自动配置

验证目标：

- `fbu setup` 自动准备 Node runtime。
- Worker 可以加载 Playwright。
- Chromium 能在三大平台启动。
- observe、click、fill、select、scroll 全部可用。

锁定结果：

- Node runtime 来源和版本
- Playwright 版本
- Chromium revision
- Worker 启动协议

### POC-5：Linux 系统依赖

验证目标：

- `fbu setup` 默认不修改系统。
- `fbu setup --with-system-deps` 可以完成自动安装。
- 缺少 sudo 时给出可操作错误。
- `--no-system-deps` 行为稳定可预测。

锁定结果：

- 发行版检测
- apt/dnf/pacman 适配边界
- 权限请求策略
- 失败回滚行为

### POC-6：System One API

验证目标：

- `/v1/systemone` 支持 `noul`、`choice`、`score`。
- `/v1/browser/*` 与同一决策核心复用。
- API 可被外部 Agent 调用。
- 概率、confidence、usage、错误结构稳定。

锁定结果：

- wire schema
- HTTP 服务启动方式
- 会话和并发模型
- API 兼容范围

### POC-7：端到端与发布包

验证目标：

- 从全新机器解压压缩包。
- 执行 `fbu setup`。
- 下载模型和浏览器依赖。
- 执行浏览器任务。
- 安装 skill。
- 生成 trace 并完成独立验证。

锁定结果：

- 正式安装流程
- smoke test
- release checklist
- 升级和卸载行为

## 9. 当前未决设计项

以下内容在详细设计阶段继续确认：

1. HTTP 服务是否默认启动，还是只在 `fbu serve` 下启动。
2. Playwright Worker 的 Node.js 运行时分发方式。
3. GGUF 模型下载、版本锁定和校验策略。
4. llama.cpp CPU/Metal/CUDA/Vulkan 的具体构建矩阵。
5. `fbu resume` 与现有 persistent/handoff 语义的最终 CLI 设计。
6. 是否在 V1 同时提供 C ABI。
7. System One API 的鉴权、并发和会话生命周期。

## 10. 非目标

V1 不做：

- 完全移除 Node.js/Playwright
- 直接从任意宿主机无条件交叉编译 CUDA 二进制
- 默认内置数 GB 模型文件
- 修改 System One 的核心决策语义以适配某个特定网站
- 重新引入 Python 作为运行时依赖
