# Python 原版与 Zig 重构版功能差异

> 对比日期：2026-09-25  
> 原版路径：`fast_browser_use/`  
> Zig 版本状态：规划中，尚未实现  
> 关联设计：`2026-09-25-zig-llama-cpp-systemone-design.md`

## 1. 结论

Zig 版本的目标不是重新设计浏览器自动化功能，而是重构：

- 运行时
- 模型推理层
- 分发方式
- 安装流程
- 对外 API

浏览器任务的用户操作方式、动作语义、验证机制和安全边界应尽量保持兼容。

目标关系：

```text
Python Agent  → Zig Core
MLX/PyTorch   → llama.cpp + GGUF
Python API    → CLI + HTTP System One API + Browser API
Python Playwright → Node.js Playwright Worker
```

## 2. 用户使用方式

| 场景 | Python 原版 | Zig 版本目标 |
|---|---|---|
| 执行任务 | `fbu run URL --goal "..."` | 保持兼容 |
| 模型下载 | Python/uv + HuggingFace 或 ModelScope | `fbu setup` 自动从 ModelScope 下载 |
| 浏览器安装 | 手动安装 Playwright Chromium | 自动下载 Node、Playwright、Chromium |
| Python | 必须安装 Python 3.12 和依赖 | 不需要 Python |
| 分发 | 源码、uv、虚拟环境 | macOS/Linux/Windows 压缩包 |
| Skill | 手动安装 Skill | 压缩包内置并自动注册 |
| API | Python API 为主 | CLI + HTTP System One API + Browser API |

目标使用体验：

```bash
unzip fast-browser-use-linux-x86_64.zip
cd fast-browser-use
./fbu setup
./fbu run https://example.com --goal "Open the More information link."
```

## 3. 浏览器操作功能

以下功能计划在 Zig 版本保持一致：

| 功能 | Python 原版 | Zig 版本目标 |
|---|---:|---:|
| 页面观察 | ✅ | ✅ |
| 可见 DOM 元素发现 | ✅ | ✅ |
| 点击按钮/链接 | ✅ | ✅ |
| 文本输入 | ✅ | ✅ |
| 密码输入 | ✅ | ✅ |
| 原生下拉框 | ✅ | ✅ |
| 复选框/单选框 | ✅ | ✅ |
| 页面滚动 | ✅ | ✅ |
| 容器滚动 | ✅ | ✅ |
| 等待页面更新 | ✅ | ✅ |
| freshness guard | ✅ | ✅ |
| 遮挡检测 | ✅ | ✅ |
| 过期动作拒绝 | ✅ | ✅ |
| `DONE` / `BLOCKED` | ✅ | ✅ |
| 独立结果验证 | ✅ | ✅ |

V1 浏览器链路：

```text
Zig → Node.js browser-worker → Playwright → Chromium
```

因此 V1 的目标是复用原版的：

- `snapshot.js`
- DOM 可见性判断
- node identity
- freshness guards
- click/fill/select/scroll/wait 执行语义

## 4. 模型推理差异

### Python 原版

支持：

```text
MLX
PyTorch
Qwen3.5-9B
Qwen3.5-35B-A3B
```

模型输入格式：

```text
MLX 4-bit
PyTorch 原始权重
```

### Zig 版本目标

统一迁移到：

```text
llama.cpp
GGUF
```

目标能力：

```text
单 Token logits
候选动作概率
文本生成
KV cache
本地 CPU 推理
```

两者不保证输出逐位一致：

- 模型权重格式不同
- tokenizer 实现可能存在差异
- logits 和概率不保证完全一致
- 同一任务可能选择不同动作

兼容目标是：

> 动作空间、执行约束、验证语义和公开 API 兼容，不承诺模型输出逐字节一致。

## 5. Agent 和 API 差异

两者共用有限动作集合：

```text
CLICK
FILL
SELECT
SCROLL
WAIT
DONE
BLOCKED
```

两者都不允许模型直接生成 CSS/XPath/Playwright 代码。

Zig 版本新增通用 System One API：

```text
POST /v1/systemone
```

新增浏览器 API：

```text
POST /v1/browser/observe
POST /v1/browser/choose
POST /v1/browser/act
POST /v1/browser/run
POST /v1/browser/verify
GET  /health
```

原版主要通过：

```python
from fast_browser_use import Agent
```

调用；Zig 版本主要通过：

```text
CLI
HTTP
JSON-RPC
```

调用。

## 6. 安装与配置差异

### Python 原版

典型流程：

```bash
uv sync
uv run playwright install chromium
uv run fbu download
```

依赖：

- Python 3.12
- uv
- Python packages
- Playwright
- Chromium
- MLX 或 PyTorch
- 模型缓存

### Zig 版本目标

典型流程：

```bash
fbu setup
```

自动完成：

- Node.js runtime
- Playwright
- Chromium
- ModelScope GGUF 模型
- 模型校验
- 缓存目录
- Skill 注册
- smoke test

相关命令：

```bash
fbu doctor
fbu update
fbu model list
fbu model install qwen3.5-9b
fbu browser install
fbu skill install
```

## 7. Profile、登录和人工接管

目标保持原版语义：

| 能力 | Python 原版 | Zig 目标 |
|---|---:|---:|
| isolated 浏览器 | ✅ | ✅ |
| persistent profile | ✅ | ✅ |
| group 隔离 | ✅ | ✅ |
| login/captcha/2FA 检测 | ✅ | ✅ |
| pause handoff | ✅ | ✅ |
| resume handoff | 部分完成 | 完整实现目标 |

原版已具备部分 `takeover()` 和持久会话逻辑，但当前 CLI 尚未完整提供跨进程 `fbu resume` 子命令。Zig 版本将其作为正式会话恢复能力重新定义并通过 POC 验证。

Ego backend 是否进入 Zig V1，暂不作为三大平台核心依赖。

## 8. Trace、录制与验证

计划保持以下 trace 字段和语义：

```text
trace.json
history
decisions
rejections
handoffs
verification
elapsed_ms
```

保持的验证参数：

```text
--expect-url
--expect-title
--expect-text
```

保持的安全行为：

- `DONE` 只是模型判断，不是最终事实
- 验证失败后有限重试
- 页面变化后重新观察
- 不盲目重放已经执行的 mutation
- password 页面值只暴露掩码

视频录制方式需要通过 POC 验证，尤其是 Zig 与 Playwright Worker 的 WebM 录制边界。

## 9. 可能的临时差异

Zig V1 初期可能分阶段实现：

1. `fbu record` 视频录制
2. Ego backend
3. Metal/CUDA/Vulkan 加速
4. 完整跨进程 `resume`
5. Python API 兼容层
6. 所有 Linux 发行版系统依赖自动安装
7. 完全离线的 Node/Chromium 分发

这些不能在 POC 完成前宣称为完全兼容。

## 10. 保持不变的边界

Zig 版本仍不自动解决：

- iframe 深层编排
- Shadow DOM 深层操作
- Canvas 操作
- 多 Tab 复杂编排
- 任意网站成功率
- 模型幻觉本身
- trace 中模型生成密码的脱敏问题，除非单独修复
- CUDA 从任意平台无条件交叉编译

## 11. 对用户的最终影响

### 普通用户

```text
任务写法基本不变
浏览器动作基本不变
验证方式不变
Skill 使用方式更简单
```

### 部署者

```text
不再安装 Python/uv
改为下载对应平台压缩包
运行 fbu setup 自动准备运行时
从 ModelScope 自动下载 GGUF
```

### 开发者

```text
Python Agent → Zig Core
MLX/PyTorch → llama.cpp
Python API → HTTP/JSON-RPC/System One API
Python Playwright → Node Playwright Worker
```

## 12. 当前状态

```text
产品需求：已锁定
总体架构：已记录
实现代码：尚未开始
技术细节：通过 POC-1 ～ POC-7 分批锁定
```

