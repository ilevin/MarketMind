# Design

## Context

现状（全部经实读核对）：

- `Dockerfile:15` 在 `pip install --no-cache-dir --no-index --find-links /tmp/wheels marketmind==0.4.1` 中硬编码版本；`Dockerfile:11` 复制脚本生成的 `dist/wheels`。
- `scripts/build-docker.sh:8` 的 `TAG` 仅用于 `docker build -t marketmind:${TAG}`（第 37 行）；脚本不读 `pyproject.toml`。全仓库不存在 `ARG` / `--build-arg` 机制。
- 版本号在构建链路中无参数化，且 `TAG`（`v0.4.2`，带 `v`）与 `pyproject.toml` 的 `version`（`0.4.2`，不带 `v`）前缀形式不同。
- 仓库内另有 `.claude/worktrees/agent-*/Dockerfile` 与 `reference/stocksview/Dockerfile` 两份副本，均为 10 条指令且不含 pin——与本次失败无关。
- 报错中的 `[6/7]` 与仓库内任一 Dockerfile 的指令总数（10）都对不上：该编号是 BuildKit 不计元数据指令（`ENV`/`EXPOSE`/`CMD`）的计数口径——`FROM`(1) + `WORKDIR`(2) + 3×`COPY`(3,4,5) + `RUN pip`(6) + `RUN mkdir`(7) 共 7 步，安装恰为第 6 步，即 `Dockerfile:15`。失败定位无歧义。

约束：

- 镜像构建**必须离线**——`--no-index` 是为规避容器内 DNS 无法解析 pypi.org 而引入（`914a4bd`，`docs/Docker构建指南.md:56-60`），不得改回联网安装。
- Dockerfile 只复制 `dist/wheels` 与 Alembic 资产（`Dockerfile:11-13`），**不复制 `pyproject.toml`**。
- 基础镜像 `python:3.12-slim`；`dist/wheels` 被 `.gitignore` 排除，必须每次构建重建，故其中 wheel 版本必然等于当前 `pyproject.toml` 版本。
- 仓库既有约定是「同一提交内同步全部版本出口」（`4860c01`、`9c01561` 均在同一提交改了 Dockerfile pin），v0.4.2 的 `428847b` 破坏了该约定。防漂移测试先例：`tests/integration/test_migrations.py::test_alembic_head_matches_models_schema`。

动机见 proposal.md - Why；行为契约见 specs/deployment 与 specs/app-version。

## Goals / Non-Goals

**Goals:**

- 版本来源唯一化：`pyproject.toml` 是构建链路的唯一解析点，`Dockerfile` 不含任何版本字面量。
- 失败模式显式化：缺参数、参数与 wheels 不符、标签与版本不符，全部在构建期以可操作的信息失败。
- 漂移自动化拦截：版本不一致在下一次发布提交前即被离线测试发现。

**Non-Goals:**

- 不改变 `APP_VERSION` 的运行时来源语义（见 D4）。
- 不引入 CI（仓库无 `.github/workflows/`）。
- 不改造 `docker-compose.yml` 的 `build: .` 回退（见 D3 与 Open Questions）。
- 不把文档中所有历史版本号变量化（CHANGELOG/README 中的版本属历史叙述，保持原样）。

## Decisions

### D1. 版本经 `APP_VERSION` build ARG 注入，解析点唯一放在构建脚本

`scripts/build-docker.sh` 从 `pyproject.toml` 解析 `version`，经 `docker build --build-arg APP_VERSION="$APP_VERSION"` 注入；`Dockerfile` 以 `ARG APP_VERSION` + `marketmind==${APP_VERSION}` 安装。

理由：既满足「单一来源是 `pyproject.toml`」，又保留精确 pin——装错版本时离线解析会失败，而不是静默装入目录里碰巧存在的那个 wheel。

备选与否决理由：

- **去掉 pin（`pip install marketmind`）**：改动最小，但绕过脚本直接 `docker build` 且 `dist/wheels` 陈旧时会静默产出旧版本镜像，失去唯一的安全网。
- **在 Dockerfile 内 `COPY pyproject.toml` 并用 `python -c` + `tomllib` 解析**：可让 compose/裸 build 路径也自动正确，但会在脚本与 Dockerfile 各留一份独立解析实现（脚本侧仍需解析以做 TAG 校验），两处可能对同一文件得出不同结论；单一解析点更可维护。
- **按 wheel 文件 glob 安装**：需额外断言「`dist/wheels` 内恰好一个 `marketmind-*.whl`」，且装的是路径而非带版本约束的需求，保护力等价于去 pin。

### D2. 缺少 `APP_VERSION` 时 fail-fast，并给出用法

`ARG APP_VERSION` 为空时 `marketmind==` 是非法需求式，pip 会报与真实原因无关的 `Invalid requirement`，排障成本高。因此在安装前增加显式非空守卫，错误信息点明需传入 `--build-arg APP_VERSION=<版本>`。这样「缺参数」与「版本与 wheels 不符」两类失败各有明确语义，不会互相混淆。

### D3. TAG 校验只针对「形如版本号」的标签

`TAG` 匹配 `^v[0-9]` 且不等于 `v$APP_VERSION` 时中止构建；`latest` 与自定义标签放行。

理由：真正的风险是产出「标签声称 vX、内容实为 vY」的镜像，会误导运维与回滚判断。而 `latest`、`test` 这类标签不构成版本声明，强制相等会误伤临时构建与本地验证。

备选：TAG 与版本严格相等（误伤非版本标签）；仅打印告警（无法阻止错误镜像产出）。

### D4. 运行时版本来源不动，只加一致性守护

`app/version.py` 仍是运行时唯一来源，新增测试断言 `APP_VERSION == "v" + pyproject.version`。

理由：改用 `importlib.metadata.version("marketmind")` 派生虽然能彻底只留一处可写，但会让 `APP_VERSION` 依赖「包已安装」这一前提（直接源码运行时会退化为异常或兜底值），改变运行时语义。本次要解决的是构建漂移，不值得为此扩大改动面（proposal 已列非目标）。

### D5. 防漂移测试放 `tests/unit`，纯文件读取

新增 `tests/unit/test_version_consistency.py`，三条断言：`APP_VERSION` 与 `pyproject.toml` 版本一致；`Dockerfile` 使用 `${APP_VERSION}` 且不含 `marketmind==<数字>` 字面量；构建脚本确实从 `pyproject.toml` 读取并经 `--build-arg` 注入。

理由：无数据库、无网络、无 fixture，纳入 `-m 'not online'` 默认回归，与 `test_migrations.py` 的防漂移思路一致；放 `tests/integration` 会平白引入临时 DuckDB 开销。

### D6. 文档示例尽量与版本无关

`docs/Docker构建指南.md` 的镜像标签类示例改为与版本无关的写法（如 `marketmind:<TAG>`，或以当前版本为例并注明「以实际发布版本为准」）；`/health` 输出示例保留当前版本。补两条故障排查：缺少 `APP_VERSION`、tag 与版本不一致被拒绝。

理由：文档里的具体版本号本身就是一份会漂移的副本，能去掉就去掉；去掉后仍随发布更新的（`/health` 输出）保留，由发布清单负责。这正是该文档至今停在 `v0.4.0` 的原因。

## Risks / Trade-offs

- [裸 `docker build` 与 `docker compose build` 需显式传参，属行为收紧] → Dockerfile 守卫给出带 `--build-arg` 的明确用法；构建指南补故障排查；`docker compose build --build-arg APP_VERSION=x` 由 compose 直接透传，无需修改 compose 文件。
- [新增 ARG 与守卫改变 Dockerfile 层结构，既有构建缓存失效] → 仅影响安装层，无正确性影响；守卫与安装合并进同一条 `RUN` 以少加一层。
- [防漂移测试基于文本匹配，重构写法可能误报] → 断言限定在语义稳定的最小集合（不得出现 `marketmind==<数字>`、必须出现 `${APP_VERSION}`、脚本必须引用 `pyproject.toml`），不锁定行号与格式。
- [`^v[0-9]` 是启发式] → 两个方向都会误判，实测：预发布标签 `v0.4.2-rc1` 会被判为不一致而拦下（可用非 `v` 前缀标签绕过），而 `release-0.4.2` 之类变体不会被拦（漏检）。两者都不影响镜像内容的正确性——镜像内容始终由 `pyproject.toml` 决定，标签只影响命名。
- [`pyproject.toml` 解析依赖固定格式] → 行首锚定解析 + 空结果显式报错退出，格式意外变化时立即失败而非注入空串（与 D2 双保险）。

## Migration Plan

1. 合入后按 `./scripts/build-docker.sh v0.4.2` 重新构建 v0.4.2 镜像；本次**不升版本号**（`pyproject.toml` 已是 `0.4.2`）。
2. 镜像内应用行为、`/health` 返回、Alembic 启动迁移均不变；无需数据库迁移与配置变更，已部署实例无需重建即可继续运行。
3. 回滚：变更只涉及 `Dockerfile`、`scripts/build-docker.sh`、`docs/Docker构建指南.md` 与新增测试，回退到前一提交即可，无数据或格式兼容性问题。

## Open Questions

- `docker-compose.yml` 的 `build: .` 回退与 `image: marketmind`（隐含 `:latest`）同脚本产出的 `marketmind:<TAG>` 之间的命名不一致属既有问题，本次不处理。若后续希望 compose 能独立完成构建，需另行决定版本注入方式（例如 compose 侧从环境变量注入 `APP_VERSION`）——该决定不影响本次规格与任务拆分。