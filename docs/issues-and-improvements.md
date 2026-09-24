# 问题与改进记录（Issues & Improvements）

> 记录时间：2026-09-24
> 来源：对「供应链平台 · 客户平台」登录流程做 fbu 浏览器自动化实测，失败后的复盘分析
> 环境：fbu (fast-browser-use) + Qwen3.5-9B-4bit (MLX) + Chromium（Apple Silicon）

---

## 问题 1（根因 · 高优先级）· password 输入框被过滤，登录类表单无法完成

**现象**：对登录页（账号 + 密码 + 登录按钮）做自动化时，模型只识别出「账号文本框 + 登录按钮」2 个元素，密码框缺失。模型填了账号、反复点登录（密码始终为空），页面停在 `/login`，13.8s 后低置信度自报 DONE（假完成）。

**根因（代码定位）**：`fast_browser_use/snapshot.js` 第 9 行：

```js
const safe = e => !['password','file','hidden'].includes(e.type);
```

该 `safe` 过滤器（第 50 行 `[...document.querySelectorAll('input,textarea,select')].filter(safe)`）把 `type=password` 输入框整个排除在可操作元素之外。对比：账号框 `type=text` 被正确提取为 textbox 且成功 TYPE_TEXT；密码框因 type=password 被过滤，模型拿不到「填密码」这个动作。

**改进建议**：password 输入框应「可填、不可读」——

- 保留 password 输入框为**可操作元素**（允许 `TYPE_TEXT` 填入凭据）；
- 但 snapshot 中**不暴露其 value**（掩码/置空），避免明文凭据进入模型上下文（凭据零穿透）；
- 用独立 role（如 `passwordbox`）或 flag 标记，让模型知道「这是密码框：可填、勿读」。

---

## 问题 2（中优先级）· 假完成——低置信度下自报 DONE

**现象**：模型反复失败后，置信度从 0.99 衰减到 0.25，最终在 0.35 的低置信度下选择 DONE，但页面仍停在 `/login`（目标未达成）。

**改进建议**：

- DONE 前做一次独立的目标达成校验（`--expect-title` / `--expect-text` / `--expect-url` 断言），断言不满足则继续而非 DONE；
- 或当模型连续 N 步置信度 < 阈值时，触发「重新观察 / 降级」，而非直接 DONE。

---

## 问题 3（低优先级）· 动态页面导致决策频繁被拒

**现象**：Vue SPA 响应式更新，导致 4 次 rejection 都是「Page changed since the decision」，模型基于旧快照的决策过期，陷入「点 → 页面变 → 重观察 → 再点」循环。

**改进建议**：

- rejection 后立即用新快照重新决策，减少「重观察」空转；
- 对「表单提交后」这类必然变页面的动作，放宽 freshness 判定。

---

## 复现方式

```bash
fbu run 'http://localhost:28082/' \
  --goal '登录客户端平台：账号 e2e_client，密码 E2e@12345。登录成功后确认能看到模型广场页面。' \
  --trace /tmp/fbu-login.json
```

登录页含 `input[type=password]` 时，password 框不会出现在 trace 的 `elements` 列表里。

---

## 修复记录（2026-09-24）

### 问题 1 已修复：password 可填、按长度掩码

改动集中在 `fast_browser_use/snapshot.js`：

- `safe` 过滤器不再排除 `password`（`file`/`hidden` 保留：file 弹原生对话框，headless 无法驱动）；
- `role()` 将 `type=password` 映射为 `textbox`，密码框以 `TYPE_TEXT`（fill）动作出现；
- 新增 `value_of()` 辅助函数：密码值只暴露**按长度生成的掩码**（`•`×min(len,16)），空值为空串。掩码按长度而非常量，保证「填入密码」仍会改变 marker/pageKey/guard，新鲜度与死循环检测照常工作；
- 三处读取密码值的位置全部走掩码：action 的 `value`（进模型 prompt）、`guard()` 的 `control.value`（进 trace 与新鲜度比对）、`pageKey()` 的 `e.value`（进 trace）。

密码内容本身从用户 goal 经 `generate_text` 提取，与页面读取无关——「可填、不可读」由此成立。已知残留：已输入的明文密码仍会出现在 trace 的 `history[].text` 中（写一次轨迹设计使然），E2E 测试凭据可接受；如需彻底脱敏需在 trace 序列化层处理。

`scripts/check_guards.py` 新增断言：密码框以 fill 动作出现且值为掩码；`act` 填入后掩码按新长度更新；`json.dumps(整个快照)` 不含任何明文凭据（单条断言覆盖全部泄漏路径）。

### 问题 2 已修复：有界验证反馈回路（不含置信度门控）

`fbu run` 现在在模型报告 DONE 且带 `--expect-*` 断言时：断言不满足 → `Agent.resume()` 回退最近 milestone、状态重置为 ready、基于新观察继续跑，而不是直接终止整个任务。重试上限由 `FBU_VERIFY_RETRIES` 控制（默认 2 次拒绝；`0` 恢复旧行为立即失败）。每次拒绝记录进 trace 的 `verification_rejections`。断言内容**永不进入模型 prompt**，保持 design.md 的独立验证教义。

**置信度门控未实现，且建议保持不实现**：design.md 明确「候选 softmax 是相对偏好，不是校准的成功概率，不能作为跳过守卫的依据」。低置信 DONE 的正确处理正是现在这条回路——被外部断言拒绝后重新观察，而非引入一个未校准的阈值数字。

### 问题 3 决定不修改代码

「放宽表单提交后的 freshness 判定」会动摇写一次轨迹/新鲜度守卫的根基：在过期节点上执行变更等于重复提交表单，这是整个防护体系存在的理由。rejection→重观察→重新决策是设计内的恢复路径，代价仅是一次被浪费的打分（秒级）；Vue 响应式抖动已由 `prepare()` 的 150ms 静默窗口缓解。复盘记录保留，代码不动。
