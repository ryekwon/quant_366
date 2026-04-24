---
name: global-agent-rules
description: >
  【最高优先级全局规范】每次对本项目代码进行任何修改后，Agent 必须自动执行的操作清单。
  无需用户提醒，视同 git commit hook 强制执行。
---

# 🌐 全局 Agent 操作规范（Global Agent Rules）

> [!IMPORTANT]
> 本文件是所有 Skill 中**最高优先级**的规范。每次进入新对话、读取任何其他 Skill 之前，
> 必须先读本文件并严格遵守。

---

## 🔁 强制规则 1：代码修改后自动更新演进日志（类 git commit）

**触发条件**：只要对项目内任何 `.py` / `.yaml` / `.json` / `.env` 文件进行了实质性修改。

**必须执行**：在完成代码修改后，无需等待用户提醒，立刻将变更摘要追加到：

```
z:\QuantpC_Workspace\Quant_Pilot\.agents\skills\evolution-log\SKILL.md
```

### 演进日志写入格式

```markdown
## 📅 YYYY-MM-DD (Manual Fix / Automated Sync / Architecture)

### 变更文件：`filename.py`

- **[BUG/ARCH/FEAT/PERF] 标题**：一句话描述变更目的
  - **根因**：为什么要改
  - **修复**：改了什么（可引用行号或函数名）
  - **影响**：对系统行为的影响
```

### 写入位置
- 追加在文件**末尾**（不要插入中间）
- 同一天的多次修改合并到同一个 `## 📅` 节下
- 使用 `### 变更文件` 区分不同文件的改动

### 分类标签说明

| 标签 | 含义 |
|---|---|
| `[BUG]` | 修复已知 Bug |
| `[ARCH]` | 架构级重构 |
| `[FEAT]` | 新功能 |
| `[PERF]` | 性能优化 |
| `[OPS]` | 运维/配置调整 |
| `[PATTERN]` | 新设计模式，可复用 |

---

## 🔁 强制规则 2：修改前必读相关 Skill

修改任何核心文件前，必须先读：
1. `quant-v4-patterns/SKILL.md` — 四大铁律合规检查
2. `quant-safe-patterns/SKILL.md` — 已知陷阱清单

---

## 🔁 强制规则 3：涉及下单/账本的修改必须语法验证

```powershell
# 改完后执行
& .venv\Scripts\python.exe -m py_compile <filename>.py
echo "Exit: $LASTEXITCODE"
```

只有 Exit 0 才能认为修改完成。

---

## 🔁 强制规则 4：多引擎防火墙同步原则

新增任何策略标的或修改防火墙逻辑时，必须同步更新：
- `t0_multigrid_executor.py` — T0 防火墙
- `t1_grid_executor.py` — T1 防火墙  
- `etf_rotation_executor.py` — 轮动防火墙

---

## 📋 本日志的维护方式

本文件由 Agent 在每次会话结束时检查是否需要更新。
用户无需手动维护，Agent 负责保持同步。
