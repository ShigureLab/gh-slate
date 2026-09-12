# gh-slate 渐进式重设计方案

状态：第 1 至 5 层已实现，2026-09-12。包括 `data/meta`、V2 状态、Markdown helpers、显式 profile、多视图路由、JSON Patch，以及旧 jq 运行时精简。以下保留逐层开发顺序与兼容决策；最终接口见 [CLI 参考](cli.md)，验证结果见 [testing](testing.md)。

目标是让 agent 和自动化程序依据明确的数据结构，持续维护风格一致的 GitHub 看板。agent 负责分析与业务判断；gh-slate 负责校验、选模板、渲染和发布。先沿用现有 GitHub 访问与写入流程，逐层替换实现。

## 1. 主要决定

| 问题               | 决定                                                                           |
| ------------------ | ------------------------------------------------------------------------------ |
| 模板放在哪里       | 路径完全由配置指定，不扫描或约定 `.github/slates/` 等目录。                    |
| 配置怎么加载       | 显式 `--config FILE`，或 `GH_SLATE_CONFIG`；第一版没有目录发现和多层配置合并。 |
| 如何复用一组模板   | 一个具名 profile 包含 Schema、单个模板或多个命名视图，以及可选的字段路由。     |
| 成功与失败布局不同 | agent 提交业务结果，工具根据配置做确定的字段到视图映射。                       |
| 模板内的控制流     | 保留循环、可选区域、空状态等局部控制流；整页布局差异使用独立视图。             |
| 渲染上下文         | `$data` 是调用方业务数据，`$meta` 是工具提供的目标上下文。                     |
| 如何修改           | `apply --data` 完整替换，`apply --patch` 标准 JSON Patch；共用一个发布流程。   |
| 当前状态放在哪里   | 继续保存在受管评论中，包括数据、元数据与定义快照。                             |
| 发布范围           | 管理普通 Issue/PR 评论；独立 `render` 可供已有的 review 发布流程使用。         |

这里的 `$data`、`$meta` 是概念命名空间。在 JSON 中使用 `data`、`meta` 两个键，在 Jinja 中使用 `{{ data.summary }}`、`{{ meta.target.number }}`。不为 `$` 改造 Jinja 语法。

## 2. 显式配置与 profile

配置文件采用 TOML。以下文件可以放在任意位置；示例路径没有特殊语义：

```toml
version = 1

[profiles.review]
schema = "contracts/review.json"
view_by = "/outcome"

[profiles.review.views]
approved = "presentation/review-approved.md.j2"
changes_requested = "presentation/review-changes.md.j2"
error = "presentation/review-error.md.j2"

[profiles.benchmark]
schema = "contracts/benchmark.json"
template = "presentation/benchmark.md.j2"
```

路径规则：

1. `--config` 优先于 `GH_SLATE_CONFIG`；环境变量只选择一个文件，不参与内容合并。
2. 配置中的相对路径，相对于配置文件所在目录；CLI 上的相对文件路径，相对于调用目录。
3. 允许配置绝对路径和 `../`。不展开文件内容中的环境变量，不下载远程模板。
4. `--profile NAME` 必须能够解析到一个明确的配置文件和 profile，缺失时直接报错。
5. `schema` 可省略。`template` 与 `views` 互斥；单模板没有 `view_by`；多视图必须指定 `view_by`。
6. 保留现有 `--template FILE` 作为直接文件模式。它与 `--profile` 互斥，避免把文件路径和 profile 名混在同一个参数里。
7. profile 模式不再接受 `--schema`、`--table`、`--list` 等定义覆盖项。需要改变定义时修改配置或使用直接文件模式。

示例：

```bash
gh slate apply review --target <PR_URL> \
  --config /path/to/boards.toml --profile review --data review.json
```

这里第一个 `review` 是远端看板实例名，`--profile review` 是本次加载的定义名。二者可以不同；同一 profile 可用于多个仓库和看板。

普通更新不需要本地配置：

```bash
gh slate apply review --target <PR_URL> --data review.next.json
```

此时复用评论中的定义快照。显式再次传入 `--profile` 表示重新加载并替换定义：读取配置及其引用文件、校验候选数据、渲染，再与数据一起发布。文件修改不会在一次不带定义参数的数据更新中被隐式采用。传入 `--config` 却不选择 `--profile` 是参数错误。

配置更新失败时，远端数据和旧定义一起保持不变；不先发布模板、再尝试发布数据。

## 3. 多视图：业务结果由生产者决定，展示由配置决定

不建议让 agent 每次自行挑一个文件路径。对于成功与失败这种有明确字段依据的变化，提交业务事实后由工具选择视图：

```text
读取候选 data
  → 用 profile 的 JSON Schema 校验
  → 用 view_by 的 JSON Pointer 读取一个字符串
  → 精确匹配 views 中的键
  → 渲染匹配的模板
```

`view_by` 只针对 `$data` 根对象，是标准 JSON Pointer，不支持 jq、Jinja 表达式、条件列表或回调。缺失字段、非字符串值、没有匹配的视图都报错；不默认退回成功模板。第一版不提供 `--view` 强行覆盖路由。

例如 review profile 定义以下业务结果；这些名字不是核心工具的内置枚举：

| `data.outcome`      | 含义                    | 此分支的数据要求                        | 模板               |
| ------------------- | ----------------------- | --------------------------------------- | ------------------ |
| `approved`          | review 已完成，可以接受 | `summary` 为非空字符串                  | 简短结论与验证说明 |
| `changes_requested` | review 已完成，需要修改 | `findings` 为非空、以稳定 ID 为键的对象 | 问题表格与逐项详情 |
| `error`             | review 执行未完成或失败 | `error.message` 为非空字符串            | 错误说明与恢复建议 |

Schema 使用现成的 `oneOf` 与 `const` 区分三种数据形态。业务 profile 规定各分支允许的字段，避免残留的错误信息出现在成功状态中；不需要实现“每个视图再绑定一个 Schema”的第二层校验系统。

“没有 finding”和“review 执行失败”不能混为同一种成功结果。自动化程序根据执行结果填写 `outcome`；需要语义判断时由 agent 填写。gh-slate 不从 prose、数组是否为空或模型输出的语气推断结果。

如果一个 finding 被标为已解决，工具也不会自动把整个 review 改为 `approved`。是否通过可能还依赖其他规则，调用方应明确提交新的 `outcome`。

局部差异仍留在 Jinja：有没有额外说明、是否展开证据、如何遍历行。完全不同的页面组织拆成不同视图。若只是用户选择“紧凑版/详细版”这种展示偏好，首版可配置两个 profile 并显式选择，无须把展示偏好伪装成业务状态。

全部视图的源文本在加载定义时读取并做语法检查，随定义一起保存。后续 `outcome` 改变时，不要求原目录、原机器或原配置还存在。只有被选中的视图针对本次数据执行渲染。

## 4. `$data` 与 `$meta`

`$data` 由调用方提供，保持一个普通 JSON 对象。例如：

```json
{
   "outcome": "changes_requested",
   "source": {
      "head_sha": "551b09958a2c5b0d8cbe45b2da943f55f82cdeb8"
   },
   "findings": {
      "F17": {
         "message": "补充变更动机和验证方式",
         "status": "open",
         "url": "https://github.com/PaddlePaddle/Paddle/pull/79755"
      }
   }
}
```

这是说明接口形状的示例，不是对该 PR 当前 review 状态的判断。

`$meta` 是只读渲染上下文，首版固定为：

```json
{
   "host": "github.com",
   "repository": {
      "owner": "PaddlePaddle",
      "name": "Paddle",
      "full_name": "PaddlePaddle/Paddle",
      "url": "https://github.com/PaddlePaddle/Paddle"
   },
   "target": {
      "kind": "pull_request",
      "number": 79755,
      "id": "PR_example_node_id",
      "url": "https://github.com/PaddlePaddle/Paddle/pull/79755"
   },
   "slate": {
      "name": "review"
   }
}
```

`target.id` 示例为占位值。真实值来自 GitHub 的 `node_id`/GraphQL ID，统一保留字符串；`target.number` 才是仓库内的 `#79755`。`target.kind` 为 `issue` 或 `pull_request`。数据库数字 ID 暂不另设同名字段。

字段归属与更新规则：

| 内容                                                 | 归属与来源                                                     |
| ---------------------------------------------------- | -------------------------------------------------------------- |
| GitHub host、仓库信息、目标身份                      | 工具解析目标并经 GitHub API 确认；不能由数据文件覆盖。         |
| 看板名                                               | 本次命令的实例名。                                             |
| 分析结论、优先级、失败原因、日志链接                 | `$data`，由 agent 或程序提供，按 profile 校验。                |
| 本次分析所依据的 head SHA、run ID、采集时间          | `$data.source`，由数据生产者提交；工具不把它改成当前 head。    |
| 工具 revision、评论 ID/URL、是否写入成功、选中的视图 | 操作结果或读取结果，不进入首版 `$meta`。                       |
| 任意调用方附加字段                                   | 放在 `$data`；不增加一个可以随意覆盖目标身份的 meta 扩展入口。 |

不把“当前 head”自动填进分析数据，是为了避免旧分析被包装成新 head 的结论。需要拒绝过期分析的发布程序，应在写入前核对其源版本并串行发布；看板 revision 不代表源数据新鲜度。

首版不暴露 PR 标题、labels、当前执行账号和自动生成的当前时间。这些可变字段没有当前需求，加入默认上下文会产生额外刷新语义。需要时可以作为业务数据传入；以后若纳入 `$meta`，必须同时规定获取与快照规则。

不暴露 `meta.comment.url`，因为第一次渲染前评论尚未创建；也不暴露自动递增的 `meta.revision`，以免为了展示新 revision 而把无变化更新变成一次写入。评论链接和 revision 在命令结果里返回。

### 4.1 快照与复现

成功 apply 把实际渲染使用的 `$meta` 与 `$data`、定义快照一起保存。

- `view` / 存储态重渲染 / `repair` 使用上次保存的 meta，不通过实时查询偷偷改变已发布内容。
- 新的 apply 重新解析目标身份，将本次 meta 作为候选状态的一部分；若仓库展示信息变化，可以产生一次有意义的更新。
- 对已有实例先核对实际目标和控制账号；不能通过复制另一处的隐藏状态来重新绑定它。展示名称变化不等同于新的目标身份。
- 用状态版本和渲染器版本标识语义；相同版本、定义、data、meta 产生相同 Markdown。

### 4.2 离线预览

离线 `render` 可以显式传入元数据 fixture：

```bash
gh slate render review --config /path/to/boards.toml --profile review \
  --data review.json --meta target.fixture.json
```

`--meta` 仅用于本地 render，按工具的 meta 结构校验；它不是远程 apply 的参数。fixture 的 `slate.name` 必须与命令实例名一致。示例 ID不被当成远端真实身份。

不传 fixture 时，render 提供 `slate.name`，`host`、`repository`、`target` 为 `null`。不使用目标信息的模板可以直接渲染；访问缺失目标字段会给出明确错误和 fixture/目标预览提示，不伪造身份。

需要真实目标的预览用同一发布流程：

```bash
gh slate apply review --target <PR_URL> \
  --config /path/to/boards.toml --profile review \
  --data review.json --dry-run
```

该命令读取 GitHub，但不写入。预览结果说明使用的是本地 fixture 还是已解析目标；这项来源说明只在操作输出中出现。

## 5. 渲染能力

保留 Jinja 的标准语法与 `StrictUndefined`，补齐一组小的、可组合的 Markdown 辅助函数/过滤器：`md_text`、`md_link`、`md_code`、`md_codeblock`、`md_table`、`md_list`、`md_details`。徽章可通过固定模板表达，先不引入远程徽章服务或组件注册系统。

普通数据字符串按文本处理；链接、代码和折叠区域由对应 helper 明确构造。helper 处理各自位置的转义，并允许其输出在表格里组合而不被二次转义。这是渲染层的小型内部类型，不成为业务 JSON 的另一套组件树。

目标模板示例：

```jinja2
## Review 通过

{{ data.summary | md_text }}

仓库：{{ meta.repository.full_name | md_link(meta.repository.url) }}
目标：#{{ meta.target.number }}
```

新模板上下文使用 `data` 和 `meta`。旧 `jinja@1` 中的 `slate.repository` 等变量只由旧渲染路径解释，不长期引入新旧两套别名。

模板只负责展示；不访问 GitHub、执行命令、读取任意文件或决定业务结论。Schema 沿用现成 JSON Schema 校验库，允许同文档引用，不扩展动态代码或远程引用加载。模板作为明确加载的定义处理，保持 sandbox、输入/输出限制，不承诺执行任意不可信模板代码。

表格、统计和详情引用同一份数据。对长期维护的记录，profile 示例采用稳定键；序号和排序只影响展示。自由文本的事实正确性不属于渲染器保证范围。

## 6. 一个 apply，两种数据输入

`--data FILE` 替换完整业务对象；`--patch FILE` 使用 [RFC 6902 JSON Patch](https://www.rfc-editor.org/info/rfc6902/) 修改当前业务对象。两者互斥并支持 stdin，底层都生成一个候选快照。

```bash
gh slate view review --target <PR_URL> --json
gh slate apply review --target <PR_URL> \
  --patch changes.json --if-revision 12 --dry-run
gh slate apply review --target <PR_URL> \
  --patch changes.json --if-revision 12 --json
```

例如从上面的待修改结果切换到通过视图：

```json
[
   { "op": "test", "path": "/outcome", "value": "changes_requested" },
   { "op": "replace", "path": "/outcome", "value": "approved" },
   { "op": "remove", "path": "/findings" },
   { "op": "add", "path": "/summary", "value": "问题已确认解决，review 通过。" }
]
```

应用规则：

1. patch 只作用于 `data`，路径 `/outcome` 不是 `/data/outcome`。不能修改 meta、模板、controller 或 revision。
2. patch 要求实例已存在，且提供 `--if-revision`；不提供创建时的隐式空对象。
3. 使用标准实现支持 RFC 6902 操作，不再维护自定义 jq 修改语言或另一种 Merge Patch 模式。
4. 在内存中依序应用全部操作，再校验最终数据和选择模板。中间状态可以暂时不满足业务 Schema；任何失败都不写远端。
5. 原数据、候选数据与 patch 中的 JSON 类型保持明确；null 是值，删除通过 remove 表达。
6. patch 可与一次显式 profile 变更同批提交：使用当前 data 作为修改起点，但只按新定义校验最终结果，最后一次性发布。
7. selected view 每次从候选数据推导，不能独立修改；写入结果返回该视图名。
8. no-op 不增加 revision、不改评论时间。失败或未知写入结果不自动重放 patch。

发布主流程继续为：读取实例 → 检查预期版本 → 产生候选快照 → 校验/选视图/渲染 → 比较 → 写前检查 → 一次评论写入 → 读回确认。GitHub 普通评论接口不提供这里可依赖的原子事务；版本检查与 patch 的 `test` 都不是多写者锁。

`--dry-run` 展示候选数据差异、定义是否变化、视图切换以及最终 Markdown。普通 `view --json` 返回当前 data、meta、revision、profile 摘要和视图；`state export` 才导出全部模板源码与 Schema，避免每次读取都输出大块定义。

## 7. 持久化与兼容边界

新的状态版本 V2 保存以下内容：

```text
identity / controller / revision
data
meta snapshot
definition snapshot
  ├─ profile name（直接文件模式可以没有）
  ├─ schema
  ├─ single template，或 view_by + 全部 views 源码
  └─ renderer version
render hash
```

配置绝对路径不成为运行时依赖，也不写进公开评论。定义的内容摘要用于比较是否变化，不表示数字签名或来源认证。先复用现有 envelope 编解码和总大小限制，不重写压缩算法，不增加历史日志与注册服务。

加载新定义时先校验源码与总大小。多视图和元数据同样占用评论预算；超限时报告占用并拒绝写入，不自动拆成多条评论或截断结构化状态。

V1 的迁移分两步：

- 增加 V2 时保留 V1 现有行为；新的直接模板创建使用 V2，已有 V1 的不带定义更新先继续走旧路径。
- 用户对已有实例显式传入新 `--profile` 或新 `--template` 时，以原 data 或本次候选 data 校验新定义，再一次性写成 V2。不自动翻译旧 Jinja 变量或任意 jq selector。

最后精简旧运行时前，要先提供上述迁移路径。精简后 V1 仍能读取、导出、检查封装完整性和显式删除；直接更新或从状态修复旧渲染器时，返回迁移指引。读取可显示存储的 Markdown，不谎称已用新渲染器复现旧结果。这允许删除 jq 依赖，而不永久携带一套旧执行引擎。

## 8. 开发顺序与 gh-stack

设计文档单独作为第 0 层。实现按以下顺序推进，每层带自己的测试、使用说明和可运行例子，不把所有验证留到最后。

```text
main
└─ codex/gh-slate-redesign-plan       0. 方案
   └─ codex/slate-render-context     1. data/meta 与可复现渲染
      └─ codex/slate-profiles        2. 显式配置与单模板 profile
         └─ codex/slate-view-routing 3. 按业务字段切换完整视图
            └─ codex/slate-json-patch 4. 统一 apply 的局部修改
               └─ codex/slate-runtime-slim 5. 场景验收与旧运行时精简
```

### 第 1 层：渲染上下文与新状态

交付：新增 Meta 模型、`data/meta` 上下文、文本/链接/代码等基础 helper、离线 `--meta` fixture、V2 meta 快照与新 Jinja 渲染版本。直接文件模板即可在普通评论上使用完整的新流程。

范围：`codec`、`rendering`、目标解析和现有 apply/render 入口。先复用 GitHub 写入代码；不同时重构网络访问、配置系统或 jq 命令。

验收：离线 fixture 和目标预览字段一致；Issue/PR 身份区分正确；链接和特殊字符正确显示；保存后重渲染一致；相同 apply 不产生写入；V1 读取和旧更新行为不变。模板升级到 V2 必须显式选择定义。

### 第 2 层：显式配置与单模板 profile

交付：TOML 加载、`--config` / `GH_SLATE_CONFIG` / `--profile`、相对路径规则、Schema 与模板快照。先实现配置里的单模板形态；`views` 在这一层明确报尚不支持。

范围：一个配置模块加上候选定义构建，不增加通用插件接口。Python 3.10 使用成熟 TOML 解析依赖，较新版本使用标准库接口，避免自写解析器。

验收：同一配置从不同 cwd 加载得到相同定义；没有 `.github/slates/` 特殊处理；CLI 配置覆盖环境变量；schema/template 相对配置定位；移走本地文件后仍可更新已有看板；显式重载定义才改变模板；直接文件模式不受影响。

### 第 3 层：多视图路由

交付：`view_by`、命名 `views`、全部视图快照、未知结果报错、选中视图的读取与预览输出。review 的 approved / changes_requested / error 三种结果通过同一个实例发布。

范围：扩展 profile 定义和渲染分派；JSON Pointer 复用标准实现，不增加表达式语言。Schema 的业务分支仍由现有校验库负责。

验收：三种结果进入不同布局；缺失/未知 outcome 不写入；执行错误不显示为通过；跨视图转换只更新原评论；配置文件缺失时仍能切换；benchmark 单模板没有额外路由要求。

这一层合并后，完整快照生产者已经能使用新产品，不必等待 patch 和旧代码删除。

### 第 4 层：标准 JSON Patch

交付：`apply --patch`、版本前置条件、最终态校验、候选差异和可操作错误。重用 `--data` 的发布函数，不另建一套远程 mutation transaction。

范围：patch 输入与纯内存转换、apply 候选构建。优先选择已有 RFC 6902 实现，并确认它与第 3 层 JSON Pointer 采用一致语义。

验收：修改一个稳定键不影响其他记录；test 或 Schema 失败零写入；旧 revision 拒绝；多操作跨视图转换只做一次发布；no-op 保持 revision；未知写入结果不会重放；fixture 覆盖 JSON Pointer 转义、null 和数组标准行为。

### 第 5 层：场景验收与运行时精简

交付：完成 CI 分析、review 跟踪、benchmark 对比三个实际 profile；更新 bundled skill 和 README；移除已被替代的 jq 修改接口、编辑器包装与 Schema 推断等表面；把旧 V1 限制为前述读取/迁移边界，再删除不再需要的运行时依赖。

保留：稳定的状态导出、恢复/删除、认证诊断、版本检查、重复名称与漂移处理。通用查询可以通过 JSON 输出连接外部 jq，不把查询语言重新内嵌回来。

验收：三个场景共用同一核心，没有业务专用分支；新进程只凭 GitHub 中的状态就能更新；现有完整快照自动化有明确替代路径；在一次性 Issue 和 PR 上完成创建、更新、视图切换、no-op 与清理的真实端到端验证。只在这层跑与最终依赖和包结构有关的打包检查，不重做独立的发布信任模型。

### 测试节奏

每层先运行相关单元/集成测试，再运行仓库要求的格式和静态检查。渲染快照断言可见语义与结构，避免对版本号、帮助文案和具体函数分层做脆弱断言。涉及发布路径的层加入有意义的模拟读写验证；真实 GitHub 验证作为最终完整体验的验收，不能由 fake `gh` 全绿替代。

## 9. gh-stack 操作方式

此处是进入实施时的操作示例，不代表后续实现分支或远端 PR 已创建。先保持工作树干净并确认 `main` 可以快进到最新 `origin/main`。当前方案分支可以作为第 0 层被接入新栈：

```bash
git fetch origin
git switch main
git merge --ff-only origin/main
git config rerere.enabled true
gh stack init --base main codex/gh-slate-redesign-plan
gh stack view --json
```

方案形成一个独立提交后再添加第 1 层；每层完成代码、测试、说明并按文件精确提交，才添加下一层：

```bash
gh stack add codex/slate-render-context
# 开发并提交第 1 层后：
gh stack add codex/slate-profiles
# 依次推进其余层，不预先堆出没有独立改动的 PR。
```

需要发布栈时使用 `gh stack submit --auto --remote origin`。根据最终变更整理每个 PR 的标题、说明、验证证据与规定的 co-author 标记，不依赖自动标题作为最终说明。修改下层时回到对应分支提交，然后运行 `gh stack rebase --upstack --no-trunk`；提交上层前检查 `gh stack view --json`。

合入后再按 live 状态同步和清理。现有分支的本地 tracking 与远端 head 可能不同，不直接对旧栈 force-push。

## 10. 与现有精简 PR 的关系

2026-09-12 开始实施后，#19 已合并；#20（snapshot-only CLI）已关闭；#21（release 简化）已从旧链解耦，作为基于 `main` 的独立 draft PR 保留。

- 新开发栈从最新 `main` 开始，不以 #20 作为产品前提。第 5 层可以复用它的有效删除，但要在替代接口完成后按最终范围整理。
- #21 的发布改动与新产品接口独立，不作为本设计实施的阻塞项。
- 新栈按上述开发顺序发布 draft PR，完成后由用户审阅和决定合入。

参考：[当前 CLI 与状态设计](cli.md)、[#20](https://github.com/ShigureLab/gh-slate/pull/20)、[#21](https://github.com/ShigureLab/gh-slate/pull/21)、[Paddle 示例](https://github.com/PaddlePaddle/Paddle/pull/79755)。
