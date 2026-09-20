# marketmind

以 DuckDB 为本地分析内核的个人证券研究工具：集中查看自选的 A 股 / 港股股票、ETF 与指数行情，并为 A 股股票提供 PE(TTM)、PB、股息率(TTM) 估值。表结构为后续历史行情存储与回测预留了正确的地基。

- 行情数据：AKShare（A 股股票，腾讯通道）+ 腾讯批量行情接口（ETF / 港股 / 指数）
- 估值数据：Tushare `daily_basic`（A 股股票）
- 持久层：DuckDB（单文件 `data/marketmind.duckdb`，列式分析型数据库）
- 单体应用：FastAPI + DuckDB + Jinja2 + 原生 JS/CSS，无 Redis / MySQL / Node.js / 前端框架
- 多用户认证（v0.2.0）：登录后使用，自选 / 指数配置 / 标签按用户隔离，行情与估值数据全局共享
- A股历史数据（v0.3.0）：Tushare 日线 / 复权因子 / 每日指标 / 资金流 + 证券主档，落 DuckDB 供后续回测与分析；管理员在 `/admin/data` 查看与补齐

## 环境要求

- Python 3.11+（本地运行），或 Docker
- 网络：需能访问 `qt.gtimg.cn`（行情）、`tushare.pro`（估值，可选）

## 本地启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
# 编辑 config.yaml，填写 tushare.token（不使用估值功能可不填）
alembic upgrade head          # 建表/升级数据库结构（必须，应用启动不做自动建表）
uvicorn app.main:app --reload --workers 1
```

访问 <http://localhost:8000>。数据库结构由 Alembic 管理：启动前必须执行
`alembic upgrade head`（容器镜像已内置该步骤），迁移失败时应用不会启动。

v0.2.0 起页面与 API 均要求登录。首次访问系统时会自动进入 `/setup`，请自行设置第一个用户的用户名和密码；该用户将自动成为管理员并登录。用户名不固定为 `admin`。

从 v0.1.0 升级时，迁移会写入不可登录的占位 legacy owner。首次访问 `/setup` 会用你填写的用户名和密码认领该账户，并保留其原有自选、指数和标签数据。也可在停服后使用管理 CLI 作为后备初始化方式；CLI 与服务进程不能同时打开同一个数据库文件（DuckDB 单写者约束）。

> uv 等价流程：`uv venv && uv pip install -e ".[dev]"` 后同上。

## 关于 DuckDB 与单进程写入

DuckDB 是嵌入式**单写者**分析型数据库，与 SQLite 一样以单文件形式随项目携带，但面向批量扫描与时间序列聚合优化，为后续千万级历史行情与回测数据打底。因此本应用的部署约束为：

- 生产环境 **uvicorn 固定 1 个 worker**（`--workers 1`），后台任务与 Web 请求同进程
- 应用内以写协调器（WriteCoordinator）序列化全部写事务，读路径不加锁
- **不可**多进程 / 多容器并发写同一个 `.duckdb` 文件

## 配置 Tushare

编辑 `config.yaml`：

```yaml
tushare:
  token: "实际 Token"
```

- Token 只从 `config.yaml` 读取，**不使用** `TUSHARE_TOKEN` 环境变量
- 未配置 Token 时应用可正常启动，仅估值功能（PE/PB/股息率）不可用并记录日志
- `config.yaml` 已加入 `.gitignore`，仓库只提交不含真实 Token 的 `config.example.yaml`

## Docker 启动

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml 填写 Token 后：
docker compose up -d
```

等价的直接运行方式（必须挂载 config.yaml 与 data 目录）：

```bash
docker build -t marketmind .
docker run -d --name marketmind \
  -p 8000:8000 \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
  marketmind
```

访问 <http://localhost:8000>。DuckDB 数据库文件与配置均持久化在宿主机（`./data/marketmind.duckdb`、`./config.yaml`）。

容器启动命令为 `alembic upgrade head && uvicorn app.main:app --workers 1`：每次启动先自动执行数据库迁移，成功后才以单 worker 启动应用；迁移失败容器直接退出（不会出现代码与数据库版本不一致的情况）。

首次部署无需在启动前运行 CLI：容器启动并完成迁移后，访问 <http://localhost:8000>，系统会引导至 `/setup`。填写你自行选择的用户名和密码后，该用户立即成为管理员并登录。

无法使用浏览器时，先停掉服务，再执行 CLI 后备流程，完成后重新启动：

```bash
docker compose down
docker compose run --rm marketmind python -m app.cli users create --admin <用户名>
docker compose up -d
```

（CLI 与服务不能同时运行，详见「用户账户与管理 CLI」）。

> 端口映射（`-p 8000:8000`）部署下，应用看到的客户端 IP 是 Docker 网桥地址，
> 登录限速的 IP 维度退化为「全部外部客户端共享同一计数」（用户名维度仍各自独立）。
> 单人 / 家庭自用无影响；多用户对外部署时建议置于反向代理之后，并知悉此限制。

## 用户账户与管理 CLI

应用启用登录认证（v0.2.0）：页面与业务 API 均要求登录，账户由管理员在「用户管理」页（`/admin/users`）创建，或在服务器上通过管理 CLI 创建：

```bash
python -m app.cli users set-password <用户名>      # 设置/重置密码（该用户全部登录会话失效）
python -m app.cli users create [--admin] <用户名>  # 创建用户（--admin 直接创建管理员）
python -m app.cli users promote <用户名>           # 将用户提升为管理员
```

说明：

- 密码经终端安全输入（getpass，不回显），不会出现在 shell history 与仓库文件中；要求至少 8 位，两次输入须一致
- CLI 从当前目录读取 `config.yaml`（与应用一致），操作其中 `database.url` 指向的 DuckDB 数据库；执行前数据库须已完成 `alembic upgrade head`
- 从旧版本升级到多用户认证后，首次访问 `/setup` 可自行设置第一个管理员的用户名和密码；系统会认领迁移生成的 legacy owner，并保留原有用户私有数据
- 如无法使用 Web 引导，可停服后运行 `python -m app.cli users set-password admin` 设置迁移占位账户密码，或使用 `users create --admin <用户名>` 创建管理员
- 全新部署默认通过 `/setup` 创建第一个管理员；也可用 `users create --admin <用户名>` 作为 CLI 后备方式
- Docker 部署：CLI 与应用服务**不能同时运行**——DuckDB 为单写者嵌入式库，
  服务进程独占数据库文件锁，容器运行时另一进程（CLI）无法打开数据库。
  先停服务、以一次性容器执行 CLI、再重启：

```bash
docker compose down
docker compose run --rm marketmind python -m app.cli users set-password admin
docker compose up -d
```

多用户数据隔离：自选列表、指数配置与标签按用户完全隔离（各自可见、同名标签互不冲突、排序互不影响）；行情快照、估值与证券主数据全局共享一份，多用户关注同一证券时行情仍只刷新一次。管理员不例外——同样只能看到自己的自选数据，其权限仅体现在用户管理与系统状态页面。

### 认证配置

`config.yaml` 中 `auth.session` 可调（全部字段可省略）：

```yaml
auth:
  session:
    ttl_days: 7          # 登录 Session 有效期（天），到期需重新登录
    cookie_secure: false # HTTPS 部署必须置 true（Cookie 仅经加密通道发送）
```

## 升级思路

- v0.1.0 → v0.2.0（多用户认证）：

```bash
cp data/marketmind.duckdb data/marketmind.duckdb.bak   # 1. 备份数据库文件
docker compose up -d                                   # 2. 自动执行迁移并启动应用
docker compose logs -f marketmind                     # 3. 确认迁移成功后，在受控网络访问 /setup
```

  部署后访问 <http://localhost:8000>，按页面提示进入 `/setup`。迁移生成的占位 owner 只允许被认领一次；首用户用户名由使用者自行设置，不固定为 `admin`，认领会保留原有用户私有数据。

  无法使用 Web 引导时，CLI 可作为后备路径（必须先停服，避免与应用同时打开同一 DuckDB 文件）：

```bash
docker compose down
docker compose run --rm marketmind python -m app.cli users set-password admin
docker compose up -d
```
- v0.2.0 → v0.3.0（A 股历史数据）：

```bash
docker compose stop                                    # 1. 停服（DuckDB 单写者，必须先停）
cp data/marketmind.duckdb data/marketmind.duckdb.bak   # 2. 备份数据库文件
docker compose up -d                                   # 3. 容器启动时自动执行 alembic upgrade head
docker compose logs -f marketmind                     # 4. 确认迁移成功
```

  迁移 `0003_a_share_historical_data` 可重复执行且只增不删；既有表行数不变。
  启动后在 `/admin/data` 点击"检查并更新数据"触发首次回填（或等待每日 20:30
  定时任务）。首次回填 2010 年至今会跨越多次运行，属预期行为，可在同一页面
  观察进度。回滚 = 还原备份文件 + 回退代码版本。

- 旧 stocksview（SQLite 版）数据**不能**原地升级；SQLite 历史数据导入工具（`import_sqlite.py`）与升级前自动备份（`db_upgrade`）属后续版本
- 后续版本的常规升级：构建新镜像替换容器即可（启动时自动增量迁移，迁移内置数据校验）

## 数据支持情况

| 资产 | 价格 | 涨跌幅 | 量比 | PE(TTM) | PB | 股息率(TTM) | 备注 |
|---|---|---|---|---|---|---|---|
| A股股票 | ✅ | ✅ | ✅ | ✅(Tushare) | ✅(Tushare) | ✅(Tushare) | |
| A股ETF | ✅ | ✅ | - | - | - | - | 不套用个股估值概念 |
| 港股股票 | ✅ | ✅ | - | - | - | - | 免费源为延时行情，标注「港股 · 延时」 |
| 港股ETF | ✅ | ✅ | - | - | - | - | 同港股股票 |
| A股指数 | ✅ | ✅ | - | - | - | - | 首页指数卡片区展示 |
| 港股指数 | ✅ | ✅ | - | - | - | - | 首页指数卡片区展示 |

指数配置与股票/ETF 自选独立管理（`/watchlist` 页面），指数显示在首页表格上方，不进入普通自选表格。

指数代码格式：A股指数为 6 位数字（如 `000001` 上证指数、`399001` 深证成指）；港股指数为**字母缩写**（大小写不敏感），常见如下：

| 代码 | 指数 |
|---|---|
| `HSI` | 恒生指数 |
| `HSCEI` | 恒生中国企业指数（国企指数） |
| `HSTECH` | 恒生科技指数 |
| `CES100` | 港股通100 |

代码以腾讯 `qt.gtimg.cn` 接口可识别为准，识别失败会在报错信息中提示。

## 行情刷新说明

- 交易时段内默认 **每 60 秒** 刷新一次后台行情（后台任务，浏览器只读缓存）
- A 股 / 港股 **独立判断** 市场状态：A 股收盘后港股仍交易时，仅刷新港股
- 午间休市（A 股 11:30-13:00 / 港股 12:00-13:00）：停止自动行情请求
- 收盘后、节假日：停止自动行情请求
- 市场状态从「交易中」切换为「已收盘」时，补抓一次收盘行情，避免缓存停留在收盘前一分钟
- 页面首次打开时无论是否交易都会读取一次缓存，收盘后仍能看到最后一次成功行情
- 数据源故障时页面不会报 500，仍显示最后一次成功数据（交易时段超过 180 秒未更新会标记 ⚠）

## 数据源说明

| 数据 | 来源 | 说明 |
|---|---|---|
| A股股票行情/量比 | AKShare `stock_zh_a_spot_tx` | 东财/新浪通道在部分网络环境不可用，故使用腾讯通道 |
| ETF/港股/指数行情 | 腾讯 `qt.gtimg.cn` 批量接口 | 港股为延时行情（约 15 分钟），页面明确标注 |
| A股估值 | Tushare `daily_basic` | 每日收盘后更新一次，需 Token |
| A股交易日历 | Tushare `trade_cal` | 按年缓存到 DuckDB |
| A股历史数据 | Tushare `daily` / `adj_factor` / `daily_basic` / `moneyflow` / `stock_basic` / `namechange` / `stock_company` | 后台按交易日同步（v0.3.0），需 Token；见「A 股历史数据」 |
| 港股交易日历 | Tushare `trade_cal`(HKEX)，不可用时回退周一至周五近似 | 近似规则下港股节假日会尝试刷新（无害），不影响数据正确性 |

所有数据均可能存在延迟，仅供个人参考，不构成投资建议。

## 配置数据源切换

`config.yaml` 中 `providers.quote` 按市场/资产类型声明数据源（`akshare` / `tencent`），估值源在 `providers.fundamental` 中声明。后续增加新数据源只需实现 Provider 并在注册表登记。

### 数据源超时

```yaml
providers:
  timeout:
    tencent: 8      # 秒；缺省时使用默认值 8
    akshare: 45     # 秒；akshare 内部请求不受控，由包装层限时执行
    tushare: 15     # 秒
```

超时后该次调用按失败处理：保留最后一次成功行情（不删缓存），下一个刷新周期自动重试。成功 / 报错 / 超时分别计数，可通过 `GET /api/admin/status` 查看各数据源运行指标（request/success/error/timeout 计数、最近耗时与最近成功/失败时间）。

## 标签

股票 / ETF 自选支持标签分类（指数不支持）：先在「标签管理」页（`/tags`）创建标签，再到「自选管理」页点击操作列「标签」按钮，在弹层中点击标签添加 / 取消关联（即时保存）。一个自选条目可关联多个标签；被引用的标签不能删除（需先解除全部关联）。行情首页可按标签筛选（全部 / 指定标签 / 无标签），筛选为前端本地过滤，不会增加数据源请求。

## A 股历史数据（v0.3.0）

面向后续回测与分析的历史数据底座：把 Tushare 的 A 股历史数据同步到本地 DuckDB。

### 数据集

| 数据集 | 内容 | 粒度 |
|---|---|---|
| `daily` | 日线行情（未复权，含成交量/成交额） | 证券 × 交易日 |
| `adj_factor` | 复权因子 | 证券 × 交易日 |
| `daily_basic` | 每日指标（PE/PB/换手率/总市值等） | 证券 × 交易日 |
| `moneyflow` | 资金流 | 证券 × 交易日 |
| `stock_basic` | 证券主档（沪深北三市场，含退市，不裁剪到 2010 年起） | 证券 |
| `trade_cal` | 交易日历（严格模式，SSE/SZSE） | 交易日 |
| `namechange` | 证券历史名称 | 事件 |
| `stock_company` | 公司基本信息 | 证券 |

历史起点默认 `2010-01-01`（`history.start_date`）。

### 管理员页面 `/admin/data`

- 顶部整体状态 + 数据起点 + 最新交易日 + 当前任务 + 最后执行
- 四个日级数据集卡片：状态、水位、期望交易日、落后交易日数、累计行数、最近错误
- 主档状态表、当前任务进度、最近 20 次执行记录
- 唯一主操作按钮「检查并更新数据」：手动触发一次补齐，运行中禁用并显示「正在更新...」，
  每 4 秒自动刷新进度，任务结束后停止轮询
- 该页不提供补缺口 / 增量同步 / 重同步等模式选择，也不提供历史数据手工编辑入口；
  缺口补齐由后台按水位自动推进

### 首次回填与限流

- **首次回填 2010 年至今可能跨越多次运行**：Tushare 有积分等级限流，
  同步按交易日逐个推进，每轮推进到限流或当日可用时间边界为止；重复触发不会并行执行
- 请求节奏由进程级 gate 统一约束（默认 0.6 秒/请求；`stock_basic` 接口 1.25 秒），
  对所有 Tushare Provider 生效
- 各数据集有可用时间 cutoff（北京时间）：`adj_factor` 09:30、`daily` 16:30、
  `daily_basic` 17:30、`moneyflow` 20:30；未到点记 `WAITING_SOURCE` 不推进、不报错，
  到点后自动补齐
- 每日 `history.schedule_time`（默认 20:30 北京时间）自动触发；`history.startup_catchup`
  控制启动时是否立即补齐一次
- 同步任务在后台推进，单次全天替换在写事务内原子完成；同步期间不阻塞行情与估值功能

### 数据完整性

- 返回恰 6000 行（Tushare 上限）判为潜在截断：改为逐证券细粒度请求、合并去重后复检，
  仍可疑则本轮失败且不推进水位
- 未知证券：刷新一次证券主档后重新映射；仍未知则报错且不推进水位（不创建占位证券）
- 交易日历严格模式：只认 Tushare 权威日历，缺失即失败，不用工作日推断兜底
- 进程异常退出后残留的运行记录在下次启动标记为 `INTERRUPTED`，水位不受影响

### 配置

```yaml
history:
  enabled: true                       # 总开关；false 时不启动历史同步 Job
  start_date: "2010-01-01"            # 历史数据起点
  schedule_time: "20:30"              # 每日调度时间（北京时间）
  startup_catchup: true               # 启动时自动补齐一次
  max_attempts: 10                    # 单 dataset×单交易日最多尝试次数
  backoff_initial_seconds: 5
  backoff_max_seconds: 300
  jitter_ratio: 0.2
  request_min_interval_seconds: 0.6   # Tushare 全局请求间隔
  stock_basic_min_interval_seconds: 1.25
  stock_basic_refresh_hours: 24       # 证券主档刷新周期
  master_refresh_days: 7              # 公司资料/历史名称刷新周期
  availability:                       # 各数据集可用时间（北京时间）
    adj_factor: "09:30"
    daily: "16:30"
    daily_basic: "17:30"
    moneyflow: "20:30"
```

全部字段可省略（取上述默认值）。历史数据同步需要有效 Tushare Token；未配置
Token 时同步会失败且不推进水位，但**不影响已有行情与估值功能**。失败时 `/admin/data`
显示的 `last_error_code` 取决于日历是否已缓存：

- 全新库、从未成功拉取过交易日历：`trade_cal` 是第一道硬前置，报
  `CALENDAR_UNAVAILABLE`（错误文本会写明 Token 未配置），四个日级数据集本轮不执行。
- 日历已缓存（曾成功同步过）、之后 Token 失效或被移除：前置通过，报
  `TUSHARE_TOKEN_MISSING`。

### 升级注意

DuckDB 为单文件单写者数据库，**升级前请停服并备份 `data/marketmind.duckdb`**：

```bash
docker compose stop            # 或停掉 uvicorn 进程
cp data/marketmind.duckdb data/marketmind.duckdb.bak
# 更新代码 / 配置后启动，容器启动时自动执行 alembic upgrade head
```

迁移 `0003_a_share_historical_data` 新增 11 张表（4 事实 + 3 主档 + 4 同步控制）
与索引，并给 `trading_calendar`
增加 4 个可空列（`exchange` / `pretrade_date` / `source` / `fetched_at`，纯
`ADD COLUMN`，既有行留 NULL，旧写路径行为不变）。既有 12 张表的行数迁移前后
一致（迁移内置校验）。

## 运行状态

- `GET /health`：应用与数据库健康 + 当前版本号
- `GET /api/admin/status`：后台任务最近运行状态（最近开始/成功/失败时间、耗时、连续失败次数）与各数据源运行指标（管理员）
- `/admin/status`：系统状态页面（管理员，服务端渲染上述任务与指标快照）
- `GET /api/admin/history-data/summary`：历史数据整体状态与各数据集水位（管理员，只读，不触发上游请求）
- `POST /api/admin/history-data/sync`：手动触发一次补齐（管理员，202 返回 `run_id`；运行中返回 409 并附当前 `run_id`）
- `GET /api/admin/history-data/runs`、`GET /api/admin/history-data/runs/{run_id}`：执行历史与单次详情（管理员）
- `/admin/data`：数据管理页面（管理员）

## 测试执行方法

```bash
source .venv/bin/activate
pytest                       # 全部测试（不需要网络，跑真实临时 DuckDB 文件）
pytest -m online             # 在线冒烟测试（需要真实网络 + 有效 Tushare Token）
pytest -m "not online" -q    # 与默认相同，显式排除在线用例
```

历史数据的本地性能基准（synthetic 6000 行 × 100 交易日，防逐行 ORM / 每行提交退化）：

```bash
.venv/bin/python scripts/bench/bench_history_write.py
```

## 目录结构

```text
app/
├── main.py            # 应用入口、lifespan、页面路由、健康检查
├── config.py          # config.yaml -> Pydantic 配置模型
├── version.py         # 应用版本号唯一来源
├── db.py              # engine / session / WriteCoordinator（写事务协调）
├── api/               # quotes / watchlist / index_watchlist / admin / admin_history / status / tags / auth / admin_users 路由
├── auth/              # 认证：密码 Argon2id / Session / CSRF / 限速 / FastAPI 依赖
├── models/            # SQLAlchemy 模型（instrument / watchlist / quote / fundamental / tag / app_user / user_session / history_* ...）
├── schemas/           # API Pydantic Schema
├── providers/
│   ├── base.py        # Quote/Fundamental/历史数据模型与 Provider Protocol
│   ├── instrument_names.py
│   ├── tushare_common.py  # Tushare 请求节奏 gate 与 Client 公共封装
│   ├── quote/         # akshare（A股股票）/ tencent（其余）行情 Provider + 注册表（含超时注入）
│   ├── fundamental/   # tushare 估值 Provider
│   ├── history/       # 历史数据 Provider（tushare：八个数据集 + 注册表）
│   └── trading_calendar/  # 交易日历（Tushare + DuckDB 缓存）
├── observability/     # ProviderMetrics 指标与超时包装层
├── repositories/      # 数据访问（含 history_fact / history_master / history_sync）
├── services/          # market_session / quote_cache / refresh / watchlist / tag / job_status / auth / user / history（同步编排）
├── cli/               # 管理 CLI：python -m app.cli users set-password/create/promote
├── jobs/              # 60 秒行情刷新任务、估值刷新任务、历史同步任务（均接入 JobStatus）
├── templates/         # index / watchlist / tags / login / change_password / admin_users / admin_status / admin_data（Jinja2）
└── static/            # 原生 JS / CSS
alembic/               # 数据库迁移（0001_duckdb_baseline → 0002_multi_user_auth → 0003_a_share_historical_data）
alembic.ini
scripts/               # spike 验证脚本 + bench 性能基准（bench_history_write.py）
tests/                 # unit + integration（含迁移测试；@pytest.mark.online 需真实 Tushare Token）
```
