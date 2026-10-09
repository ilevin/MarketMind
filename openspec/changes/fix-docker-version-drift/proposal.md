## Why

执行 `./scripts/build-docker.sh v0.4.2` 无法构建镜像：`Dockerfile:15` 把离线安装的版本 pin 硬编码为 `marketmind==0.4.1`，而 `pyproject.toml:3` 已是 `0.4.2`、`dist/wheels/` 内实际只有 `marketmind-0.4.2-py3-none-any.whl`。因 `Dockerfile:11` 采用 `--no-index` 离线安装，pip 只能在 `/tmp/wheels` 内解析，找不到满足 `==0.4.1` 的候选发行版而退出（exit code 1）。

根因不是打错一个数字，而是**版本号在构建链路中存在第三处手工副本**：`scripts/build-docker.sh:8` 的 `TAG` 参数唯一用途是 `docker build -t marketmind:${TAG}`（`scripts/build-docker.sh:37`）的镜像标签，既不校验 `pyproject.toml` 版本，也从未经 `--build-arg` 传给 Dockerfile；全仓库不存在任何 `ARG`/`--build-arg` 机制。历史上 `4860c01`（v0.5.0）、`9c01561`（统一为 v0.4.1）都在同一提交内一并改了 Dockerfile pin，而 v0.4.2 的 `428847b` 只改了 `app/version.py` 与 `pyproject.toml`，任务清单 `10.2` 也只列了这两处——约定被破坏，构建随即失败。只要 pin 仍由人工维护，同类失败必然复发。

## What Changes

- **Dockerfile 版本参数化（消除硬编码）**：改为 `ARG APP_VERSION` + `pip install ... marketmind==${APP_VERSION}`，SHALL NOT 再出现任何版本字面量；构建期未提供 `APP_VERSION` 时 SHALL 以明确错误信息快速失败（而非 pip 的含糊报错），错误信息给出正确用法。
- **构建脚本成为版本注入点与一致性闸门**：`scripts/build-docker.sh` 从 `pyproject.toml` 解析版本（全链路唯一解析点），经 `--build-arg APP_VERSION=` 注入 `docker build`；当 `TAG` 形如版本号（`^v[0-9]`）且与 `pyproject.toml` 版本不一致时 SHALL 中止构建并提示，避免产出「标签声称 vX、内容实为 vY」的镜像；`latest`/自定义非版本标签不受限制。
- **防漂移测试（新增）**：断言 `app/version.py` 的 `APP_VERSION == "v" + pyproject.toml 的 version`；断言 Dockerfile 不含 `marketmind==<数字>` 形式字面量且确实使用 `${APP_VERSION}`；断言构建脚本确实从 `pyproject.toml` 读取并注入 build-arg。仓库已有 `tests/integration/test_migrations.py::test_alembic_head_matches_models_schema` 这类防漂移测试先例，风格对齐。
- **文档去陈旧**：`docs/Docker构建指南.md` 中 7 处 `v0.4.0`（快速构建命令、手动构建镜像标签、compose 片段、验证输出、故障排查）更新，示例优先采用与版本无关的写法；补「构建立即失败：缺少 APP_VERSION」「tag 与版本不一致被拒绝」两条故障排查；`scripts/build-docker.sh:4` 的示例注释同步。
- **规格补齐**：`deployment` 的 Dockerfile 要求当前完全未提离线 wheels 与版本 pin，且仍写着旧项目名 `stock-dashboard`；本次重述并新增「镜像构建脚本」要求，把「版本由 pyproject 派生、不得硬编码」固化进规格。`app-version` 的「版本号唯一来源」补跨来源一致性要求。
- **明确不做**（非目标）：不升版本号——修复后 `./scripts/build-docker.sh v0.4.2` 即可成功构建现有 0.4.2 镜像；不改为 `importlib.metadata` 运行时派生（`app/version.py` 仍是运行时唯一来源，只加一致性守护）；不改造 `docker-compose.yml` 的 `build: .` 回退语义，仅在文档说明次要构建路径需显式 `--build-arg`；不动 `docs/CHANGELOG.md`、`docs/README.md`、`config.example.yaml`、`app/static/style.css` 中属于历史叙述的版本号；不引入 CI（仓库无 `.github/workflows/`）。
- 无 **BREAKING** 变更：离线安装语义、单 worker 约束、容器启动命令均不变，仅镜像构建入口的版本来源改变。

## Capabilities

### New Capabilities

（无）版本一致性归入既有 `app-version`，构建行为归入既有 `deployment`，不引入与二者近似的新能力。

### Modified Capabilities

- `app-version`: 「版本号唯一来源」要求扩展——`APP_VERSION` 除作为运行时展示出口的唯一来源外，SHALL 与 `pyproject.toml` 的 `version` 保持一致（去 `v` 前缀比较），并由自动化测试守护；构建产物（如 Dockerfile）SHALL NOT 出现版本字面量。
- `deployment`: 「Dockerfile」要求重述——补离线 wheels 安装（`--no-index --find-links`）、版本经 `APP_VERSION` build ARG 注入且 SHALL NOT 硬编码、缺失时 fail-fast；修正陈旧的项目名 `stock-dashboard` → `marketmind`；新增「镜像构建脚本」要求——`scripts/build-docker.sh` 从 `pyproject.toml` 派生版本、校验 TAG 与版本一致、经 build-arg 注入镜像构建。

## Impact

- **构建产物**：`Dockerfile`（第 15 行重写，新增 `ARG` 与守卫）、`scripts/build-docker.sh`（新增版本解析、TAG 校验，`docker build` 增加 `--build-arg`，头部示例注释）、`docs/Docker构建指南.md`（7 处版本示例 + 2 条故障排查）。
- **规格**：`openspec/specs/deployment/spec.md`、`openspec/specs/app-version/spec.md` 经本变更 delta 更新；`openspec/specs/deployment/spec.md` 中 `stock-dashboard` 陈旧项目名一并修正。
- **测试**：新增 `tests/unit/test_version_consistency.py`（纯文件读取，无 fixture、无网络、无数据库，纳入默认离线回归）。既有 `tests/integration/test_admin_status_api.py`、`test_fault_tolerance.py`、`test_auth_permissions.py` 中对 `APP_VERSION` 的断言不受影响。
- **运行时**：无影响。镜像内应用行为、`/health` 返回、Alembic 迁移与启动命令均不变；`app/version.py` 仍为运行时唯一来源。
- **次要构建路径的行为变化**：裸 `docker build`、`docker compose build` 现在需显式 `--build-arg APP_VERSION=<版本>`，否则 fail-fast 并给出用法提示（此前这两条路径同样脆弱——依赖 `dist/wheels` 恰好存在且 pin 恰好匹配）；`docker compose up -d` 消费已构建镜像的路径不变。
- **依赖 / 配置 / 数据库**：无新增依赖、无配置项变化、无迁移、无数据影响。