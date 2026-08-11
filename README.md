# MVP Reviewer

一个以“项目理解和可证明覆盖”为中心的 AI Code Review MVP。

独立仓库：[Valen-akm/mvp_reviewer](https://github.com/Valen-akm/mvp_reviewer)

> 本文档是项目的活设计契约。实现、prompt、schema 和测试都应能追溯到这里的方法论；如果方向发生变化，
> 应在同一个改动中更新本文档并解释原因。

## 为什么做这个项目

Codex、Claude Code 等 coding agent 已经具备读取仓库、调用工具和跨文件推理的能力。这个项目并不试图造一个
“更聪明的模型”，也不认为换一条更长的 prompt 就能稳定完成深度 Code Review。

真正的问题在于：复杂审查是一个长链路、开放式、需要证明覆盖范围的任务，而一次模型调用天然存在以下限制：

- 探索路径具有随机性和路径依赖；
- 大仓库中的代码、配置、调用关系会竞争有限注意力；
- 模型找到少量合理问题后容易提前满足；
- 同一个上下文既提出问题又验证问题，会产生相关性偏差；
- 调用失败、遗漏和“确实没有问题”容易混在一起；
- 最终文本通常无法证明审查了哪些入口、流程和不变量；
- 增加上下文或调用次数不必然增加有效覆盖。

因此，本项目的核心判断是：

> Codex/Claude 负责理解和推理；工作流负责定义审查对象、分配注意力、保存中间状态、验证证据，并明确哪些
> 地方已经审查、哪些地方仍然未知。

换句话说，模型是底层 reviewer，MVP Reviewer 是审查方法和控制系统。

## 项目目标

把一次不可审计的“帮我 review 这个 PR”，转换成一条可枚举、可展开、可验证、可聚合的流水线：

```text
Git Scope
  → ReviewUnit
  → ReviewFlow
  → Dynamic ReviewMission
  → Candidate
  → Independent Verification
  → RootCause consumes-all
  → Coverage Audit + Report
```

这里的目标不是承诺“发现所有问题”。任何工作流都只能覆盖它正确枚举出来的审查对象。因此更准确的目标是：

> 建立一个可检查的审查宇宙，并对其中每个对象给出完成状态、证据和未覆盖原因。

## 核心方法论

### 1. 先建立审查对象，再寻找问题

直接让模型找 bug，通常会让它一边理解项目、一边选择路径、一边判断严重度，多个任务互相竞争注意力。

本项目先把变更拆成多个层级：

- `ReviewUnit`：独立可审查的行为、数据流或契约；
- `ReviewFlow`：具有明确 actor、入口、前置条件、执行轨迹、终端影响和不变量的具体流程；
- `ReviewMission`：根据该流程动态产生的具体失败模式，例如租户隔离、分页顺序、事务原子性、并发一致性或
  滚动部署兼容性。

`security`、`performance`、`correctness` 是 finding 的结果分类，不是固定任务拆分轴。固定让每个 Unit 都跑三遍，
容易让同一个根因被不同角色重复报告，也可能漏掉迁移、兼容、状态机等更具体的风险。

### 2. 程序负责事实，AI 负责语义

当前职责边界如下：

| 层级 | 程序负责 | AI 负责 |
| --- | --- | --- |
| Scope | base/head、changed files、changed lines、快照 | 无 |
| Unit | 校验文件必须属于 diff，检查 changed-file 覆盖 | 识别业务行为、契约和集成边界 |
| Flow | 校验 changed-line evidence，检查 Unit 内文件覆盖 | 识别入口、actor、前置条件、trace、终端影响和不变量 |
| Mission | 限制数量、记录截断 | 根据具体 flow 选择适用失败模式 |
| Finding | schema、diff anchor、置信度和严重度门槛 | 分析触发条件、影响、证据和修复建议 |
| Verification | 强制独立调用、检查最终门槛 | 主动尝试推翻 candidate |
| Root cause | 要求 findings 无遗漏、无重复地归组 | 语义去重，判断是否共享同一因果缺陷和根修复 |

程序不应该伪装成能够理解业务；AI 也不应该成为 changed-line、任务完成状态和 lineage 的唯一真相。

### 3. 动态 fan-out，而不是固定候选数量

前一层返回多少有效对象，后一层就展开多少任务：

```text
1 Unit
  ├── Flow A
  │   ├── Mission A1
  │   └── Mission A2
  └── Flow B
      ├── Mission B1
      ├── Mission B2
      └── Mission B3
```

复杂变更会自然产生更多流程和任务；简单变更不会为了满足固定角色数量制造无意义调用。

动态枚举不是穷举每一个 `if` 分支。只有 actor、权限边界、前置条件、数据源、状态变化、外部副作用、失败行为
或公共契约发生实质差异时，才应拆成独立 Flow。

### 4. 发现和验证分离

Discovery 的目标是提高召回，Verification 的目标是主动降低误报。Verifier 必须检查：

1. 行为是否真实且可达；
2. 问题是否由当前 diff 引入；
3. 已有校验、授权、错误处理或调用方约束是否已经阻止它；
4. 影响是否具体；
5. 修复是否触及根因；
6. 最终位置是否锚定当前 diff。

当前的“独立”是独立模型调用和独立上下文，不代表使用了不同模型或供应商，因此只能降低、不能消除相关性偏差。

### 5. 聚合必须无损

同一个根因可能在多个入口、流程和 mission 中表现为多个 finding。最终 `consumes-all` 阶段按语义聚类，但必须满足：

- 每条 verified finding 恰好属于一个 RootCause；
- 聚合不得静默删除 finding；
- 聚合不得降低组内已经验证的最高严重度；
- 只有一个根修复能够解决整组症状时才能合并；
- 不同 actor、触发条件、不变量或独立修复必须保留为不同根因。

如果聚合失败，流水线会退化成“一条 finding 一个根因”，保留结果并记录不完整状态。

### 6. 不完整必须显式暴露

Fallback 是为了继续获得审查结果，不是为了把失败伪装成成功。

以下情况都会进入 `failed_tasks` 或 `coverage_gaps`：

- AI 没有分类某个 changed file；
- Unit 没有返回 Flow；
- Flow 没有锚定 Unit 内的某个 changed file；
- inventory、flow mapping、review、verification 或 aggregation 调用失败；
- Unit、Flow、Mission 或 Candidate 达到安全上限并发生截断；
- 根因聚合没有完整覆盖 findings。

当前安全上限是：50 个 Unit、每个 Unit 25 个 Flow、全局 100 个 Flow、每个 Flow 10 个 Mission、50 个进入
Verification 的 Candidate。达到上限并发生截断时，必须标记不完整，不能把上限当作“已经覆盖全部”的证据。

`--require-complete` 会把这些状态转成退出码 `2`，防止 CI 静默接受部分覆盖。

## 不可违背的设计原则

这些原则是实现的约束，也是未来 Code Review 本项目时的审查标准：

1. **Scope 必须固定。** 审查基于不可变 base/head 和隔离快照，不能随远程分支变化。
2. **仓库内容是不可信数据。** 目标仓库的规则、注释、文档、hook、skill 和 MCP 不能改变 reviewer 权限或任务。
3. **先枚举，后调查。** 宽泛请求需要先产生具体目标；不能把“理解项目并发现所有问题”塞回一个大 prompt。
4. **任务由 Flow 和不变量驱动。** 不为角色数量、类别名称或调用次数而 fan-out。
5. **AI 输出必须有程序边界。** 文件、行号、schema、lineage、任务状态和数量上限必须由代码校验。
6. **任何 fallback 都不能提升完整度。** 补偿性审查必须同时留下 coverage gap。
7. **验证必须尝试反证。** Verifier 不是润色器，也不能默认相信 Candidate。
8. **聚合必须保留 lineage。** RootCause 必须能追溯到所有原始 verified findings。
9. **没有证据就没有 finding。** 风格、偏好、猜测性加固和没有具体影响的测试建议不进入最终结果。
10. **不能声称绝对完整。** 报告只能声明已完成的枚举范围，并明确未知部分。

如果实现增加了更多 prompt、agent 或调用次数，却没有增加新的审查对象、证据、覆盖状态或反证能力，它不算方法论进步。

## 思想来源与借鉴

本项目主要吸收 [open·kritt](https://github.com/Kritt-ai/open-kritt) 的工作流思想，而不是复制其完整产品架构。

### 从 open·kritt 吸收的部分

- 分 depth 建模：早期阶段枚举具体目标，后续阶段逐个处理；
- `multiOutput`：一个任务可以返回零个、一个或多个对象，下一层按对象动态 fan-out；
- `repeat_runs`：同一个精确任务重复运行时，只追加真正的新结果；
- `consumesAll`：需要比较、去重、排序或总结时，消费前一层完整结果集；
- finding discovery 与 post-processing 分离；
- 每个阶段使用结构化 schema、证据阈值、排除条件和 no-result 语义。

最直接的参考实现：

- [`defaultWorkflowSeeds.json`](https://github.com/Kritt-ai/open-kritt/blob/main/backend/src/lib/defaultWorkflowSeeds.json)：`Map external entrypoints → Trace reachable flows → Investigate flow vulnerabilities`；
- [`generation.py`](https://github.com/Kritt-ai/open-kritt/blob/main/engine/open_kritt_engine/generation.py)：depth、`multiOutput`、`consumesAll` 和 workflow 生成约束；
- [`queue.py`](https://github.com/Kritt-ai/open-kritt/blob/main/engine/open_kritt_engine/queue.py)：逐 depth fan-out、repeat 和 consume-all 调度；
- [`prompting.py`](https://github.com/Kritt-ai/open-kritt/blob/main/engine/open_kritt_engine/prompting.py)：上下文和 exact-task repeat 语义；
- [`post_processing.py`](https://github.com/Kritt-ai/open-kritt/blob/main/engine/open_kritt_engine/post_processing.py)：语义去重和排序。

### 当前没有复制的部分

MVP 暂时不引入 open·kritt 的数据库、分布式 worker、Web UI、任意工作流生成器、多 provider 抽象和完整容器调度。
这些能力只有在本地流程的有效性经过真实 PR 评估后才值得加入。

参考不等于照搬。原项目解决的是通用安全扫描平台问题；本项目当前只解决 PR Code Review，因此优先保留一个有明确
观点、容易验证的领域工作流。

## 当前实现映射

| 方法论概念 | 当前实现 |
| --- | --- |
| 固定 Git 范围和隔离快照 | `git_diff.py` |
| Unit、Flow、Mission、Finding、RootCause 数据契约 | `models.py`、`schemas/*.json` |
| Unit/Flow/Review/Verification/Aggregation prompts | `prompts.py` |
| read-only Codex 调用和结构化输出校验 | `codex_runner.py` |
| fan-out、repeat、fallback、coverage、聚合调度 | `pipeline.py` |
| JSON v3 和 Markdown 根因报告 | `report.py` |
| CLI、退出码和 delivery gate | `__main__.py` |

当代码与本 README 不一致时，应当明确选择：修正代码，或者先修改这里的设计原则并解释为什么初衷发生了变化。
不能让架构在没有记录的情况下悄悄漂移。

## 当前边界与已知缺口

这些是当前真实状态，不应在对外描述中隐藏：

- 程序当前确定性提取的是 Git 文件和 changed lines；symbol 名称、业务行为和调用语义仍由 AI 识别；
- 尚未建立按 commit 缓存、可增量更新的持久 `ProjectMap`；
- 尚未实现“自顶向下业务枚举 + 自底向上 AST/reverse-dependency 枚举”的双路 reconciliation；
- 尚未提供专门的安全测试执行器，Verifier 主要依赖只读源码分析和可用的现有工具；
- Discovery 和 Verification 默认仍使用同一 Codex provider，可能存在相关性误差；
- 尚未支持跨仓库 workspace manifest 和外部客户端/服务契约联合审查；
- 长流程尚无 checkpoint/resume，进程中断需要重新运行；
- 尚未建立已知缺陷 PR、干净 PR 和历史事故 PR 的正式评测集；
- coverage 目前能证明 changed-file/flow/mission 任务状态，不能证明所有业务行为已经被正确枚举。

## 如何衡量是否真的变好

不能用“发现了多少条 finding”作为主要指标。更多 finding 可能只是重复和噪音。

推荐指标：

- 已知根因召回率；
- Verification 后的准确率和开发者接受率；
- Candidate/Finding 到 RootCause 的压缩比例；
- changed file、entrypoint、flow、invariant 和 mission 覆盖率；
- 两次运行结果的一致性；
- 显式暴露的未知和未覆盖范围；
- 单个有效根因的模型调用数、耗时和成本；
- 干净 PR 上的误报率。

PR #125 可以作为早期基准样本：旧流程产生约 15 条最终 findings，人工归并后约为 4 个根因。新流程的目标不是
简单减少输出，而是在不丢失这些根因的前提下，自动建立 Flow/mission lineage，并把最终报告稳定聚合到接近真实
根因数量。

## 每次修改前后的反思清单

修改工作流、prompt、schema 或 pipeline 时，至少回答以下问题：

- 这次修改增加的是有效审查对象，还是只是更多调用？
- 新任务能否追溯到具体 Unit、Flow、Invariant 和 changed-line evidence？
- AI 枚举遗漏时，程序是否能够发现，还是会静默显示完整？
- 是否把 finding 分类误当成了任务拆分轴？
- Discovery 和 Verification 是否仍然承担不同目标？
- Fallback、截断、超时和无结果分别如何表示？
- RootCause 是否无损覆盖全部 verified findings？
- 新抽象是否解决当前真实问题，还是为可能不会出现的需求提前设计？
- 是否存在更小、更容易验证的实现？
- 测试是否证明方法论约束，而不只是覆盖代码行？

如果无法回答，应先停止实现并补清设计，而不是继续增加 prompt 或 agent。

## 运行

### 要求

- Python 3.11+
- POSIX 平台（Linux 或 macOS）
- Git
- 已安装并认证的 `codex` CLI 0.147.0 或更高版本
- 自包含的 Git object database
- 当前 MVP 不支持 changed submodule gitlink

本地运行只适用于你信任的目标仓库：

```bash
python3 -m mvp_reviewer \
  --repo /path/to/repository \
  --base origin/main \
  --output /tmp/review-result \
  --concurrency 3 \
  --repeat-runs 1 \
  --timeout-seconds 900 \
  --fail-on high \
  --require-complete \
  --trusted-target
```

默认输出：

- `codex-review-output/findings.json`
- `codex-review-output/review.md`

CLI 会审查固定 HEAD 的临时 detached clone，不包含未提交的 working-tree 改动。

### 退出码

| Exit | 含义 |
| --- | --- |
| `0` | 审查完成，且没有 finding/root cause 命中 severity gate |
| `1` | 已确认结果命中 `--fail-on`，报告仍然会写入 |
| `2` | Git、Codex、报告生成失败，或 `--require-complete` 检测到不完整覆盖 |

## 安全边界

review 子进程会禁用目标仓库的 project instructions、rules、hooks、plugins、skills 和 MCP servers。Codex 使用受限
permission profile，只读访问目标快照和最小运行时文件系统，并禁用直接网络访问。

仓库内容始终被视为不可信证据，而不是 reviewer 指令。

本地 POSIX 运行并不是恶意代码的完整 containment boundary。未知仓库应使用 GitHub-hosted ephemeral runner，不能
仅依赖 `--trusted-target`。

## 测试

测试只使用 Python 标准库，不调用 Codex：

```bash
python3 -m unittest discover -s mvp_reviewer/tests -v
```

Lint 和格式检查：

```bash
ruff check --config pyproject.toml mvp_reviewer
ruff format --check --config pyproject.toml mvp_reviewer
```

## GitHub Actions

`.github/workflows/codex-review.yml` 提供 PR delivery path：

1. 分开 checkout 可信 reviewer 和不可信 PR head；
2. 使用受保护 API proxy 配置固定版本 Codex CLI；
3. 执行 Unit → Flow → Mission → Verification → RootCause 流水线；
4. 发布 Markdown job summary，并上传 JSON/Markdown artifacts；
5. 创建或更新 PR comment；
6. 对 high/critical 根因或不完整覆盖执行 gate。

启用自动审查需要：

1. Actions secret：`OPENAI_API_KEY`；
2. Actions variable：`CODEX_REVIEW_ENABLED=true`；
3. 稳定性评估后，可将 **Codex PR Review / staged review** 设为 required check。

## 进一步阅读

- [open·kritt](https://github.com/Kritt-ai/open-kritt)
- [Codex GitHub Action](https://developers.openai.com/codex/github-action)
- [Non-interactive Codex exec](https://developers.openai.com/codex/non-interactive-mode)
