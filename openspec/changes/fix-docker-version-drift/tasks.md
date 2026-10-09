# Tasks

## 1. Dockerfile 版本参数化

- [x] 1.1 `Dockerfile` 引入 `ARG APP_VERSION`，在安装前以同一层内的非空守卫检查（未提供时打印含 `--build-arg APP_VERSION=<版本>` 用法的提示并以非零码退出），安装式改为 `marketmind==${APP_VERSION}`；验证：`grep -n 'APP_VERSION' Dockerfile` 同时显示声明与使用点，且 `grep -nE 'marketmind==[0-9]' Dockerfile` 无输出
- [x] 1.2 验证缺参数 fail-fast：在 `dist/wheels` 已存在的前提下执行 `sudo docker build -t marketmind:nobuildarg .`（不传 `--build-arg`），确认构建在安装层失败且错误信息给出 `--build-arg APP_VERSION` 用法，而非 pip 的 `Invalid requirement`
- [x] 1.3 验证版本不匹配失败：执行 `sudo docker build --build-arg APP_VERSION=0.4.1 -t marketmind:mismatch .`，确认离线解析报「找不到满足 marketmind==0.4.1 的发行版」并列出该版本号

## 2. 构建脚本版本注入与标签校验

- [x] 2.1 `scripts/build-docker.sh` 新增前置步骤：从 `pyproject.toml` 的 `[project]` 段以行首锚定解析 `version`，解析结果为空时打印明确错误并 `exit 1`；该步骤 SHALL 位于清理 `build/`、`dist/` 之前；验证：`bash -n scripts/build-docker.sh` 语法通过，且脚本内解析命令单独执行输出 `0.4.2`
- [x] 2.2 新增 TAG 一致性校验：`TAG` 匹配 `^v[0-9]` 且不等于 `v${APP_VERSION}` 时中止并提示不一致；验证：`time ./scripts/build-docker.sh v9.9.9` 在数秒内中止并打印不一致提示，且 `dist/` 未被删除（证明校验先于清理）
- [x] 2.3 `docker build` 增加 `--build-arg APP_VERSION="${APP_VERSION}"`；`latest` 与自定义非版本标签不触发中止；验证：`./scripts/build-docker.sh v0.4.2` 完整执行并成功产出 `marketmind:v0.4.2` 镜像（该步同时承担第 5 组的端到端构建，不重复构建）
- [x] 2.4 更新脚本头部用法与示例注释（第 3-4 行）为当前版本，并注明版本自动取自 `pyproject.toml`；验证：`head -6 scripts/build-docker.sh` 中不再出现陈旧版本号，且示例命令与 2.2 的校验规则自洽

## 3. 版本一致性防漂移测试

- [x] 3.1 新增 `tests/unit/test_version_consistency.py`：断言 `app/version.py` 的 `APP_VERSION` 等于 `"v" + pyproject.toml 的 version`；验证：`python -m pytest tests/unit/test_version_consistency.py -q` 通过，且该文件不依赖 fixture、数据库或网络
- [x] 3.2 同文件断言 `Dockerfile` 使用 `${APP_VERSION}` 且不含 `marketmind==<数字>` 形式字面量；验证：临时把 `Dockerfile:15` 改回 `marketmind==0.4.1`，该测试失败；还原后通过
- [x] 3.3 同文件断言 `scripts/build-docker.sh` 引用 `pyproject.toml` 且向 `docker build` 传入 `--build-arg APP_VERSION`；验证：临时移除注入行，该测试失败；还原后通过
- [x] 3.4 漂移场景反向验证：临时把 `pyproject.toml` 版本改为 `0.4.3` 而不动 `app/version.py`，确认测试失败并指明两处不一致；还原后通过

## 4. 构建文档去陈旧

- [x] 4.1 `docs/Docker构建指南.md` 更新 7 处 `v0.4.0`（快速构建、手动构建镜像标签、compose 片段、`docker images`、`/health` 示例、故障排查两处）；镜像标签类示例改为与版本无关写法（`marketmind:<TAG>`），`/health` 示例保留当前版本并注明以实际发布版本为准；验证：`grep -n 'v0\.4\.0' docs/Docker构建指南.md` 无输出
- [x] 4.2 手动构建步骤补 `--build-arg APP_VERSION=<版本>` 并说明版本取自 `pyproject.toml`；「为什么需要离线 wheels」段补一句版本来源说明；验证：`grep -n 'build-arg' docs/Docker构建指南.md` 出现在手动构建步骤，且参数名与 `scripts/build-docker.sh` 中实际使用者一致
- [x] 4.3 补两条故障排查：「构建失败：缺少 APP_VERSION」与「构建失败：tag 与项目版本不一致」，各给出原因与解决命令；验证：两条目可检索，且其引用的报错文案与 1.1/2.2 实现的实际输出一致

## 5. 端到端验证与提交

- [x] 5.1 镜像内容校验：`sudo docker run --rm marketmind:v0.4.2 pip show marketmind` 输出的 Version 为 `0.4.2`，与 `pyproject.toml` 一致
- [x] 5.2 启动容器后 `curl http://localhost:8000/health` 返回 `version: v0.4.2`，与 `app/version.py` 一致
- [x] 5.3 全量离线回归 `python -m pytest -m "not online" -q` 全绿（新增一致性测试纳入默认收集），且 `openspec validate fix-docker-version-drift` 通过
- [x] 5.4 以约定前缀（`fix:` 或 `build:`）提交；验证：`git show --stat HEAD` 确认本次未改动 `app/version.py` 与 `pyproject.toml` 的版本号（本次不升版本），且改动集中在 `Dockerfile`、`scripts/build-docker.sh`、`docs/Docker构建指南.md`、新增测试

## Workflow follow-up

- 满足项目评审要求后归档本变更，归档会将 specs 增量合入主规格。
- 归档后确认 `openspec/specs/deployment/spec.md` 的 Dockerfile 要求已不含陈旧项目名 `stock-dashboard`。