# 每日科技速览 · 定时抓取静态站

一个**完全跑在 GitHub 上**的信息站模板：GitHub Actions 定时抓取公开 RSS，生成静态页面，自动部署到 GitHub Pages。

> 当前仓库已进入 M3 多源阶段（5 个源、`state.json`、按发布时间归档、只推新增）。阶段 4 的归档页与浏览器端静态搜索、阶段 5 的来源连续失败告警、阶段 6 的正式基线、全员健康门禁和自然新增推送均已通过线上验收：`push_mode: "on"`，`baseline.established=true`；2026-11-23 和 2026-11-24 两次真实 Schedule 分别推送 6 条、9 条自然新增，每次只发 1 条合并微信摘要，无重复。下面的“单源 / 0 Secret”等描述保留为 M1 的历史部署步骤；M3 的数据口径以 **2.1 归档口径** 和 `config/sources.yml` 为准。

- 不需要你的电脑开机，关机也照常运行
- 不需要服务器、数据库、域名
- 不需要写代码就能换内容：只改 `config/sources.yml` 一个文件
- 别人 fork 后按本文档操作，也能跑出自己的一份

---

## 一、它是怎么运转的

```
① GitHub Actions（定时 / 手动）
        │  运行 scripts/fetch.py
        ▼
② data/latest.json（结构化数据，自动提交回仓库）
        │  运行 scripts/build.py + Jinja2 模板
        ▼
③ site/index.html（静态页面）
        │  actions/deploy-pages
        ▼
④ https://<用户名>.github.io/<仓库名>/（公开链接）
```

几个关键设计：

| 设计 | 解决什么问题 |
| --- | --- |
| 每个源独立抓取、独立重试 | 一个源挂了不影响别的源 |
| 失败时沿用上一次的 `data/latest.json` | 站点不会因为源挂掉变成空白，页面上标「缓存」 |
| 数据文件提交回仓库 | 下次运行才能读到「上一次的数据」；顺带让定时任务保活 |
| 抓取前检查 robots.txt + 请求间隔 | 合规、不打扰对方服务器 |
| 版本全部锁定在 `requirements.txt` | 换机器、换时间跑，结果一致 |

---

## 二、目录结构与每个文件的作用

```
.
├─ .github/workflows/update-site.yml   定时任务：抓取 → 生成 → 部署到 Pages
├─ config/sources.yml                  ★ 订阅源配置：换内容只改这个文件
├─ scripts/core.py                     M3 纯函数层：清洗、日期、关键词、限额、状态
├─ scripts/fetch.py                    多源抓取、失败保底、状态维护、按发布日期归档
├─ scripts/build.py                    读 data/ 生成主页、归档页和搜索索引，输出 site/
├─ scripts/notify.py                   只推新增：baseline 未建立直接跳过；读 state 的 pending，成功后标 notified
├─ scripts/alert.py                    来源连续失败合并告警：达到阈值时一条消息，成功才写 alert_open
├─ templates/base.html                 页面骨架（head、样式引用、公共导航）
├─ templates/_nav.html                 公共导航：首页 / 历史归档 / 搜索
├─ templates/index.html                首页内容（分类、来源状态、页脚）
├─ templates/archive_index.html        归档索引页（按日期倒序、显示条目数）
├─ templates/archive_day.html          单日归档页（历史归档标记、完整条目列表）
├─ templates/search.html               浏览器端静态搜索页
├─ assets/search.js                    读取 data/search-index.json 的本地搜索脚本
├─ assets/style.css                    样式；改配色只看文件顶部 :root 变量
├─ data/latest.json                    本轮展示数据（Actions 会自动提交更新它）
├─ data/state.json                     文章状态机与来源健康计数（Actions 自动提交）
├─ data/archive/YYYY-MM-DD.json        按文章发布时间（北京时间）归档的历史数据
├─ site/                               构建产物（每次重建，不提交到仓库）
├─ site/archive/index.html             归档索引页产物
├─ site/archive/YYYY-MM-DD.html        单日归档页产物
├─ site/search.html                    搜索页产物
├─ site/data/search-index.json         浏览器端搜索索引（纯文本、最多 2000 条）
├─ requirements.txt                    Python 依赖（4 个，版本锁定）
├─ .gitignore                          忽略虚拟环境、产物、本地密钥
├─ LICENSE                             MIT 许可证
└─ README.md                           本文件
```

### 2.1 归档口径（M3 已确认）

`data/archive/YYYY-MM-DD.json` 里的日期，是**文章发布时间在北京时间（UTC+8）对应的日期**，不是 Actions 实际抓取的日期。

- 一篇 2026-11-20 23:30（UTC）发布的文章，北京时间已经是 2026-11-21 07:30，会归入 `2026-11-21.json`。
- 同一篇文章用稳定 ID 去重：重复运行、重复抓到都只会更新展示字段，不会重复增加条目，也不会改变 `first_collected_at`。
- 归档保留本轮成功抓取、通过源级过滤的所有条目（受各源 `max_items` 限制），**不受页面全站 `total_max_items` 截断**；页面只展示全站前 N 条，历史文件可以更完整。
- 后续抓到较早发布的文章时，会幂等合并进对应历史日期文件；如果它已经见过，状态不会被当成新增，也不会补推。
- 阶段 4 的归档页和搜索索引必须遍历 `data/archive/` 下的所有日期文件，不能假设只有一个文件。

### 2.2 归档页与静态搜索（阶段 4）

`scripts/build.py` 会遍历 `data/archive/` 下所有 `YYYY-MM-DD.json`，生成：

- `site/archive/index.html`：归档索引，按日期倒序显示可用日期和条目数；
- `site/archive/YYYY-MM-DD.html`：单日归档页，显示标题、来源、分类、发布时间、摘要和原文链接，并标明“这是历史归档”；
- `site/search.html` + `site/data/search-index.json`：浏览器端静态搜索，不依赖服务器、数据库或第三方服务。

几个关键约束：

- 归档页展示该日期文件里的全部有效条目，不受首页 `total_max_items: 10` 的展示限额影响；例如有 14 条，就生成 14 条，不会截成 10 条。
- 所有页面只用相对路径（如 `../index.html`、`archive/index.html`、`assets/style.css`），以适配 GitHub Pages 的 `/daily-digest/` 子路径；不要写死 `/archive/...`。
- 搜索索引最多 `search_max_items: 2000` 条，按发布时间倒序保留较新的条目；只存 `id/title/summary/source_name/category/published/date/link` 纯文本字段，不写 `state.json`、SendKey 或归档之外的数据。
- 归档页和搜索页都不修改 `data/archive/`，只读；同一日期文件重复构建的输出应稳定。
- 过期归档按 `archive_retention_days: 90` 清理，以北京日期计算；只删除完整的 `YYYY-MM-DD.json`，当天和未来日期不删，索引不会指向已删除页面。
- 缺失、空、损坏或字段不完整的归档文件会被跳过并打印警告，不会让整个站点构建失败。
- 搜索脚本使用 `createElement` / `textContent` / `replaceChildren` 等安全 DOM API 写入结果，不把 Feed 内容拼进 `innerHTML`。

本地构建和预览：

```powershell
# 已有 data/latest.json 时直接生成页面
.\.venv\Scripts\python.exe scripts\build.py

# 搜索页需要 HTTP 访问，不能直接双击 HTML（file:// 下 fetch 会被浏览器拦截）
.\.venv\Scripts\python.exe -m http.server 8000 --directory site
# 浏览器打开 http://localhost:8000/
```

构建日志会输出归档页数量、归档条目数、搜索索引条数和索引体积（例如 `搜索索引：14 条（归档 14 条，上限 2000），8203 B`），便于确认索引没有异常增长。

### 2.3 来源连续失败告警（阶段 5）

普通内容推送和维护告警是两条独立链路：

- `push_mode: "off"` 只停止普通文章推送；告警由 `alerts.enabled` 单独控制，过渡期仍可验证。
- 连续失败达到阈值（当前 `alert_after_failures: 2`）时，`scripts/alert.py` 发送**一条合并告警**；多个来源同时异常也只发 1 条。
- 同一段连续故障只告警一次；恢复后计数归零、`alert_open` 关闭，不发送恢复消息；以后再发生新的一段故障可以重新告警。
- 只有确认存在待告警来源时才读取 `SERVERCHAN_SENDKEY`；无告警、无新增时不会读取或打印密钥。
- 告警步骤位于普通推送之后、数据提交之前，使用 `continue-on-error: true`，告警成功、失败、跳过都不会使 workflow 失败。
- 发送失败时不把 `alert_open` 写成成功，保留 `alert_last_error` 和可重试状态；错误文本会脱敏。
- `data/state.json` 为每个来源维护 `consecutive_failures`、`last_success_at`、`last_attempt_at`、`last_alert_at`、`alert_open`、`last_status` 等字段；旧 state 会自动迁移。

本地验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

告警只影响维护通知，不修改 `data/latest.json`、`data/archive/` 或页面产物。

### 2.4 正式推送、全员健康基线与自然新增（阶段 6，已完成）

阶段 6 已收口：正式基线由一次人工确认的 `workflow_dispatch` 建立；之后两次真实 `schedule` 已验证只推送真正新增，每次运行只发 1 条合并微信摘要。

#### 当前来源与限额

当前启用 5 个来源，配置在 `config/sources.yml`：

| id | 来源 | 分类 | 单次最多 | 时间窗口 |
| --- | --- | --- | ---: | ---: |
| `sspai` | 少数派 | 科技资讯 | 3 条 | 72 小时 |
| `ithome` | IT之家 | 科技资讯 | 2 条 | 24 小时 |
| `solidot` | Solidot 奇客 | 科技资讯 | 2 条 | 24 小时 |
| `appinn` | 小众软件 | 实用工具 | 2 条 | 72 小时 |
| `ruanyifeng` | 阮一峰的网络日志 | 每周精选 | 1 条 | 192 小时 |

- 全站首页最多展示 `total_max_items: 10` 条，刚好等于 5 个源的单源上限之和。
- 归档按文章发布日（北京时间）保存 90 天；去重状态保留 120 天、最多 3000 条 ID；搜索索引最多 2000 条。
- 时区为 `timezone_offset: 8`；摘要最多 160 字。
- `alerts.enabled: true`，同一来源连续失败 2 次才发一条合并维护告警；告警与普通推送相互独立。

#### 正式基线（#15）

- 基线只发生在**用户明确确认后、由 `workflow_dispatch` 手动触发的第一次线上运行**中；`schedule` 即使 `push_mode=on` 且五源健康，也不会建立基线，只记录 `baseline_waiting_manual`。
- 建立基线必须同时满足：`GITHUB_EVENT_NAME=workflow_dispatch`、5 个启用来源全部成功、`source_failed=0`、`source_cached=0`、所有来源 `last_status="ok"` 且 `consecutive_failures=0`。
- 线上基线运行：#15，Run ID `44987389869`，event=`workflow_dispatch`，branch=`main`，run_attempt=`1`，结果 success；`baseline.established=true`，`established_at=2026-11-22T12:59:21+08:00`，共抑制 26 条历史条目（`suppressed(baseline)`）。
- 基线本次 `new_count=0`、`reason=baseline_established`，只归档、不推送；手机确认没有收到任何普通内容消息。
- 基线成功后，`baseline.established` 一直保持 true；后续定时运行不会再次建立或改写基线。

#### 两次真实 Schedule 的自然新增验收

| 运行 | Run ID | 事件 | 分支 | run_attempt | 结果 | 运行开始（北京时间） | head SHA | 本次新增 | 推送结果 |
| --- | --- | --- | --- | ---: | --- | --- | --- | ---: | --- |
| #16 | `45080588405` | `schedule` | `main` | 1 | success | 2026-11-23 06:57:45 | `efdfae279886bbf52e717a08fa81cfab66fa6b83` | 6 条 | 1 条合并微信摘要 |
| #17 | `45286283619` | `schedule` | `main` | 1 | success | 2026-11-24 07:09:19 | `74ef0967778c80d194891563e1d20438abdd6643` | 9 条 | 1 条合并微信摘要 |

两次运行的核对结果：

- 两次都是 `schedule`、`main` 分支、run_attempt=1、没有重跑；`config/sources.yml` 在两次运行前后均为 `push_mode: "on"`，`baseline.established` 始终为 true，`established_at` 未改变。
- #16 的 6 条 pending 与 #17 的 9 条 pending **零交集**；每篇只在第一次成功推送时写 `notified_at`，#16 为 `2026-11-23T06:58:11+08:00`，#17 为 `2026-11-24T07:09:45+08:00`。第二次运行没有把 #16 的 6 条重新标成 pending 或二次推送。
- 两次运行结束后 `pending` 均为 0；当前状态为 26 条 `suppressed(baseline)` + 15 条 `notified`，共 41 条。
- 两次运行均为 5/5 来源成功、`source_failed=0`、`source_cached=0`；来源 `last_status` 全部为 `ok`，没有缓存保底或告警混入普通摘要；告警步骤日志均为“没有达到阈值且未告警的来源，跳过（不读取 SendKey）”。
- 归档从 3 个日期、26 条增长到 4 个日期、32 条，再增长到 5 个日期、41 条；41 个 ID 全部唯一，state 与归档一一对应，没有重复。
- Pages 首页在 2026-11-24 07:09 更新，展示 10 条、5/5 源成功；归档索引为 5 个日期、41 条；搜索索引 `count=41`、`total=41`，41 个 ID 唯一。
- 两次推送日志分别为“准备推送：每日科技速览 · 新增 6 条 / 新增 9 条”“推送成功，微信应已收到消息”“推送成功并已标记 6 条 / 9 条为 notified”。手机端确认一共只有 2 条合并摘要，没有额外重复消息。

#### 微信摘要的形态

`scripts/notify.py` 每次运行最多只调用一次 Server 酱。消息标题为“每日科技速览 · 新增 N 条”，正文包含：

- 真实新增数量（以 `state.json` 的 pending 实际条数为准，不取首页展示条数）；
- 最多 3 条新增标题；
- 多于 3 条时追加“另有 N-3 条新增未展开”；
- 页面链接 `https://1lnmlnm2.github.io/daily-digest/`。

一条消息代表本轮全部新增；推送成功后，本轮**全部** pending 都标记为 `notified`，不只是列出的 3 条。因此微信收到的是“多条新增合并成的一条消息”，不会为每篇文章单独发消息。

#### 门禁与失败处理

- 基线未建立时，`notify.py` 在任何 `push_mode` 下都会跳过普通推送：不调用 Server 酱、不标 `notified`、不读取 SendKey；`latest` 记录 `baseline_not_established`、`baseline_waiting_manual` 或 `baseline_blocked_unhealthy`。
- 基线健康门禁阻断时，workflow 仍可写审计数据，但 `baseline.established` 保持 false、不抑制条目、普通推送为 0；下一次 Schedule 也不会自动建基线，必须恢复健康并经人工再次确认后，才可用 `workflow_dispatch` 重试。
- 普通推送失败时保持 pending，下一次运行再试；不会在一次运行内重试，避免重复消息。
- 告警步骤使用 `continue-on-error: true`，其成功、失败、跳过都不会使 workflow 失败；只有确认存在待告警来源时才读取 `SERVERCHAN_SENDKEY`。

#### `push_mode` 开关与回退

当前配置已经是：

```yaml
settings:
  push_mode: "on"
```

- 当前 `baseline.established=true`。如果以后先改回 `"off"`、再改回 `"on"`，基线仍然有效，不需要也不应该重新建立基线；恢复后只推之后真正新增的文章。
- 只有新仓库 / fork 后 `baseline.established=false` 时，才需要在五源健康和人工确认后，用一次 `workflow_dispatch` 建立基线；`schedule` 不能代替。
- 回退时把 `push_mode` 改成 `"off"`，用普通提交，不 force push、不 reset 历史；普通推送立即停止，维护告警仍受 `alerts.enabled` 控制。
- 不要为了“重新开始”手工改 `state.json` 清空 baseline，也不要用改 state 的方式掩盖重复推送；确需重置时，只能从明确的基线前锚点做一次经确认的数据修正。

#### 只推真正新增

- 基线之后，只有“ID 从未见过、发布时间晚于 `baseline.established_at`、通过时间窗口/关键词/全站限额”的条目才会进入 `pending`。
- 基线前已有的条目、基线后才发现但发布时间早于基线的条目，都会按 `pre_baseline` 抑制，不补推。
- 没有自然新增时，定时运行保持 `new_count=0`，`reason=no_new_items`，不发送普通消息。
- 线上自然新增已经由 #16、#17 验证；验收过程禁止伪造：不改 `latest.json`、`state.json`、archive 或 RSS 制造假新增，不使用假 Feed、假发布时间、假链接。

#### 定时触发

- workflow 只有 `schedule` 和 `workflow_dispatch`，没有 `push` 触发。
- cron 是 UTC `17 22 * * *`，即北京时间每天 06:17；GitHub 可能延迟几十分钟，不能当准点任务。
- 已观察到的启动时间：#14 在 2026-11-22 06:55 启动（延迟约 38 分钟）；#16 在 2026-11-23 06:57 启动；#17 在 2026-11-24 07:09 启动。
- `schedule` 只负责抓取、归档、构建、部署和告警；baseline=false 时它永远不建立基线，也不发送普通内容。

#### 尚未实测的项目

以下场景尚未在线上实测，不能把本仓库的阶段 6 结论直接套用：

- fork 后的完整复用（Actions 启用、Pages Source、Secret 自建、基线是否干净）；
- 公开仓库连续 60 天无提交导致 scheduled workflow 被停用后的恢复；
- `SERVERCHAN_SENDKEY` 的轮换与旧密钥吊销；
- 线上多个来源同时故障的告警演练（阶段 5 已做单源和本地多源合并测试，但未做线上多源同时故障）；
- Server 酱配额耗尽或接口变更时的长期重试与运维流程。

本地阶段 6 测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

本地全量 **129 项通过**（阶段 5 的 116 项 + 阶段 6 新增 13 项：6 项基线健康门禁 + 6 项手动基线/普通推送门禁 + 1 项告警隔离）；线上阶段 6 已完成 #15 基线、#16/#17 两次自然新增验收。

---

## 三、从零部署（全程网页操作，约 15 分钟，不用装任何软件）

### 第 1 步：准备 GitHub 账号

打开 <https://github.com>，注册或登录。免费账号即可，不需要付费计划。

### 第 2 步：新建仓库

1. 点右上角 **`+`** → **New repository**
2. **Repository name** 填一个英文名，例如 `daily-digest`（这个名字会出现在网址里）
3. 选 **Public**（必须公开：公开仓库的 Actions 和 Pages 才免费；私有仓库的 Pages 需要付费计划）
4. **不要**勾选 "Add a README file"、"Add .gitignore"、"Choose a license"（我们要上传自己的文件）
5. 点 **Create repository**

### 第 3 步：上传文件

1. 在新仓库页面点 **Add file** → **Upload files**
2. 打开你电脑上的项目文件夹 `D:\github静态抓取工具站`
3. 把下面 **6 个文件夹** 整个拖进网页的拖拽区域：
   `.github`、`assets`、`config`、`data`、`scripts`、`templates`
4. 点下方 **Commit changes**（弹窗里再点一次 **Commit changes**）
5. 回到仓库首页，再点 **Add file** → **Upload files**，把 **4 个根文件** 拖进去：
   `.gitignore`、`LICENSE`、`README.md`、`requirements.txt`
6. 再点 **Commit changes**

> **注意**：不要上传 `site/` 文件夹（它是生成的产物，仓库里不需要，`.gitignore` 已经忽略它）。
> **提示**：Windows 资源管理器里的 `.github`、`.gitignore` 是能正常拖拽的，不需要改设置。
> **如果拖文件夹失败**：改用 "Create new file"，在文件名框里直接输入 `scripts/fetch.py` 这样的带斜杠路径，GitHub 会自动建目录，然后把内容粘进去。这是备用方案。

### 第 4 步：打开 Pages

1. 仓库顶部点 **Settings**（齿轮）
2. 左栏找到 **Pages**
3. **Build and deployment** → **Source** 下拉框，选 **GitHub Actions**（不要选 "Deploy from a branch"）
4. 选完立即生效，不需要点保存

### 第 5 步：手动跑第一次

1. 仓库顶部点 **Actions**
2. 左栏点 **定时更新站点**
3. 右侧点 **Run workflow** 下拉 → 确认分支是 `main` → 点绿色 **Run workflow**
4. 刷新页面，等 1~2 分钟，会出现一次运行记录

运行记录里应该看到两个绿色对勾：

- `抓取并构建`：装依赖 → 抓取 → 生成页面 → 提交数据 → 上传产物
- `部署到 GitHub Pages`：发布到 Pages

### 第 6 步：拿到公开链接

两种方式都能拿到：

- 点开这次运行 → 左侧点 **deploy** → 展开 **部署** 步骤 → 里面有 `page_url`
- 或者回到 **Settings → Pages**，顶部会显示 **Your site is live at ...**

链接形如：`https://<你的用户名>.github.io/daily-digest/`

打开它，应该看到：

- 大标题「每日科技速览」
- 一行 **最后更新：2026-xx-xx xx:xx（北京时间）**
- 「少数派」区块，10 条内容，带「正常」标签

---

## 四、验收清单（照着勾）

- [ ] 手动 Run workflow 后，两个 job 都是绿勾
- [ ] Pages 链接能打开，能看到抓到的标题和摘要
- [ ] 页面顶部显示「最后更新」时间，且是你运行的时间
- [ ] 把 `config/sources.yml` 里某个 `url` 故意改错，再跑一次：任务仍然绿勾，页面该源显示「缓存」并注明缓存时间，其余内容不变
- [ ] 首页的「历史归档」「搜索」入口可打开；`/daily-digest/archive/index.html` 的日期和条目数正确
- [ ] 任选一个归档页，标题、来源、分类、发布时间、摘要和原文链接都在，且标明「这是历史归档」
- [ ] `/daily-digest/search.html` 能加载索引；中文关键词、英文大小写、来源/分类/日期筛选都有结果或正确的空结果提示
- [ ] 搜索索引条数不超过 `search_max_items`，归档页没有被首页 10 条限额截断
- [ ] 在仓库搜索框搜 `webhook`、`token`、`sk-`、`密码`，搜不到任何明文密钥（本版本本来就没有密钥）

---

## 五、日常修改（都不需要动代码）

### 5.1 换订阅源

打开 `config/sources.yml` → 右上角铅笔图标编辑。

```yaml
sources:
  - id: sspai                 # 唯一标识，英文小写，不能重复
    name: 少数派               # 页面上显示的名字
    home: https://sspai.com    # 站点首页，用来补全相对链接
    type: rss                  # rss 或 atom
    url: https://sspai.com/feed
    enabled: true              # false = 暂停这个源（配置保留）
    max_items: 10              # 最多显示几条
```

**加一个源**：复制上面 6 行，改 `id`、`name`、`url` 即可。
**删一个源**：把 `enabled` 改成 `false`，比整段删掉更安全。

当前 5 个源都已经启用（`enabled: true`）；阮一峰的网络日志也已在正式列表中，不是注释示例。要暂停某个源，把它的 `enabled` 改成 `false` 即可，配置段保留。

改完记得：**Actions → 定时更新站点 → Run workflow** 手动跑一次（否则要等下一次定时）。

### 5.2 改定时频率

打开 `.github/workflows/update-site.yml`，找到：

```yaml
  schedule:
    - cron: "17 22 * * *"
```

**两条铁律**：

1. GitHub 的 cron **只认 UTC**，北京时间要 **减 8 小时**。
2. 定时**不保证准点**，整点是全球高峰，可能延迟几分钟到几十分钟。别把「几点几分必须跑完」当需求。

cron 的 5 个字段：`分 时 日 月 星期`（星期 0 = 周日）。

| 你想要的 | cron（UTC） | 说明 |
| --- | --- | --- |
| 每天北京时间 06:17 | `17 22 * * *` | 默认值，UTC 22:17 = 北京次日 06:17 |
| 每天北京时间 08:00 和 20:00 | `0 0,12 * * *` | UTC 0 点、12 点 |
| 每 6 小时一次 | `17 */6 * * *` | 北京 02:17 / 08:17 / 14:17 / 20:17 |
| 每周一北京时间 07:00 | `0 23 * * 0` | UTC 周日 23:00 |

> 建议保持「每天 1 次」。没必要做高频抓取：既浪费额度，也容易给对方站点造成压力。

### 5.3 改标题、摘要长度、时区

都在 `config/sources.yml` 的 `settings` 段：

```yaml
settings:
  site_title: "每日科技速览"
  site_description: "由 GitHub Actions 定时抓取..."
  timezone_offset: 8            # 8 = 北京时间；0 = UTC
  max_items_per_source: 10
  summary_max_chars: 160        # 摘要最多保留多少字
```

### 5.4 改配色

打开 `assets/style.css`，顶部 `:root` 里的颜色变量改掉即可，例如：

```css
--accent: #2563eb;   /* 强调色（链接悬停） */
--bg: #f6f7f9;       /* 页面底色 */
```

---

## 六、本地调试（可选，但强烈建议改样式时用）

需要先装 **Python 3.10 或更高版本**（安装时勾选 "Add python.exe to PATH"）。

在 PowerShell 里进入项目目录，逐行执行：

```powershell
cd D:\github静态抓取工具站

# 1. 创建虚拟环境（只做一次）
python -m venv .venv

# 2. 安装依赖（国内用清华镜像，快很多）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 抓取（会打印每个源的成功/失败情况）
.\.venv\Scripts\python.exe scripts\fetch.py

# 4. 生成页面
.\.venv\Scripts\python.exe scripts\build.py

# 5. 启动本地静态服务器预览（不要直接双击 HTML：搜索页需要 HTTP）
.\.venv\Scripts\python.exe -m http.server 8000 --directory site

# 6. 浏览器打开 http://localhost:8000/
```

> 这种写法**不需要激活虚拟环境**，可以避开 PowerShell 的「禁止运行脚本」报错，对新手最省事。

macOS / Linux 对应命令：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/fetch.py
.venv/bin/python scripts/build.py
.venv/bin/python -m http.server 8000 --directory site
# 浏览器打开 http://localhost:8000/
```

本地调试的小技巧：

- 只想看数据对不对：打开 `data/latest.json`
- 想模拟「源失效」：把 `sources.yml` 里的 `url` 改成错的，再跑 `fetch.py`，会看到 `↳ 本次失败，沿用上次缓存`
- `site/` 每次都会被清空重建，不要在里面手改东西

---

## 七、Secrets 清单

### 当前：抓取/部署 0 个 Secret；普通推送 1 个

普通抓取、生成和 Pages 部署全部用 GitHub 内置的 `GITHUB_TOKEN`，不需要申请任何密钥。

当前仓库已经启用 `push_mode: "on"` 和 `alerts.enabled: true`，所以线上需要配置 **1 个** `SERVERCHAN_SENDKEY`，普通新增摘要和来源告警共用它。未配置时不会报错：普通推送会跳过并保留 pending，告警会跳过；抓取和部署不受影响。

| Secret 名称 | 用途 | 获取方法 |
| --- | --- | --- |
| `SERVERCHAN_SENDKEY` | 推送微信摘要和来源告警（Server 酱） | 打开 <https://sct.ftqq.com> → 微信扫码登录 → 复制页面上的 SendKey（形如 `SCTxxxxxx`） |

添加位置：仓库 **Settings → Secrets and variables → Actions → New repository secret**，Name 填 `SERVERCHAN_SENDKEY`，Secret 粘贴值。

**三条铁律**：

1. 密钥只放 Secrets，**永远不要**写进 `config/sources.yml`、代码或 README
2. 代码里只通过 `os.environ.get("SERVERCHAN_SENDKEY")` 读取，**不要 print 出来**
3. 本地调试用 `.env` 文件（已被 `.gitignore` 忽略），不要提交

---

## 八、保活与免费额度（必须知道）

### 8.1 定时任务会被自动停用

GitHub 官方规则：**公开仓库连续 60 天没有任何提交活动，scheduled workflow 会被自动停用。**

本方案**天然免疫**这个问题：每次运行都会更新 `data/latest.json` 里的「最后更新时间」，产生一次提交，等于每天给仓库一次活动记录。

万一还是被停了（比如你出差两个月，或者所有源连续失败）：

- Actions 页面顶部会出现黄色提示条
- 点 **Enable workflow** 即可恢复
- 或者直接点一次 **Run workflow**

如果你以后把频率改成「每月一次」这种低频，建议每月手动点一次 Run workflow。

> 不要用第三方「keep-alive」机器人刷空提交，那有被判滥用（abuse）的风险。本项目自己的数据提交就是最干净的保活方式。

### 8.2 免费额度

| 项目 | 额度 | 对本项目的影响 |
| --- | --- | --- |
| Public 仓库 Actions | 标准 runner 免费，不计分钟数 | 每天 1 次、每次约 1 分钟，完全够用 |
| Private 仓库 Actions | Free 计划 2000 分钟/月 | 够用，但没必要设为私有 |
| Pages 站点大小 | 1 GB | 本页面约 10 KB |
| Pages 带宽 | 100 GB/月（软限制） | 个人站点远远用不到 |
| Pages 构建次数 | 10 次/小时（软限制），**用 Actions 自建部署时不适用这条** | 每天 1 次 |
| 单次部署超时 | 10 分钟 | 本任务约 30 秒 |

来源：GitHub 官方文档 [Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)、[Actions 使用限制](https://docs.github.com/en/actions/administering-github-actions/usage-limits-billing-and-administration)。

**不要做的事**：把 cron 改成每 5 分钟一次。既没有内容价值，又可能触发限流或滥用判定。

---

## 九、合规边界（哪些源不能抓）

脚本已经内置了三道保护：**robots.txt 检查**、**请求间隔 2 秒**、**只取标题/摘要/链接**。

但下面这些源，**技术上能抓也不要用**：

- 需要登录、需要 Cookie 长期会话的
- 付费墙后面的内容
- `robots.txt` 明确 `Disallow` 的路径
- 需要破解签名、验证码、加密参数的接口
- 要求「不得转载/不得聚合」的站点

**替代方案**：优先找该站官方 RSS；没有官方 RSS 就换一个同类公开源；或者用 RSSHub 这类公开的 feed 生成服务（注意公共实例也有频率限制）。

**版权**：本站只展示标题 + 最多 160 字摘要 + 原文链接，并在页脚声明版权归属。**不要**把整篇文章正文抓下来重新发布，那是侵权。

---

## 十、故障排查

| 现象 | 最可能的原因 | 怎么解决 |
| --- | --- | --- |
| Actions 红叉，日志 `HTTP 404` | `url` 写错，或源已下线 | 浏览器打开那个 url 确认；换源 |
| 日志 `HTTP 403` | 对方拒绝了这个 User-Agent / 地区 | 换源，不要伪装浏览器硬闯 |
| 报错 `Resource not accessible by integration` | workflow 没有写权限 | Settings → Actions → General → Workflow permissions → 选 **Read and write permissions** → Save |
| deploy 步骤报 `Pages site not found` | Pages 的 Source 没选 GitHub Actions | 回到第 4 步重设 |
| Pages 链接 404 | ① 部署还没完成 ② 路径少了仓库名 | 等 1~2 分钟；项目站地址是 `https://用户名.github.io/仓库名/`，不是根域名 |
| 定时任务一直不跑 | cron 用了北京时间 / 不在默认分支 / 被 60 天规则停用 | 换算成 UTC；确认在 `main` 分支；去 Actions 点 Enable |
| YAML 报语法错误 | 用了 Tab 缩进 / cron 没加引号 | 一律用 2 个空格缩进；`cron: "17 22 * * *"` 要带引号 |
| 页面某源一直显示「缓存」 | 源挂了、被墙、或返回空数据 | 看 Actions 日志里的 `✗` 行；换源；连续 2 次失败会发合并告警 |
| 日志「baseline 尚未建立，跳过普通推送」 | `baseline.established=false`（常见于新 fork 或重置后） | 先确认五源健康，再经人工确认用 `workflow_dispatch` 建基线；`schedule` 不会代建 |
| 日志「未配置 SERVERCHAN_SENDKEY」 | Secret 未建、名字写错或已被删除 | 在 Settings → Secrets 配好；pending 会保留，下一次运行再推，不要手改 state |
| 日志「没有新增内容，跳过普通推送」 | 本轮 `new_count=0` | 正常；不要为了制造推送而手动空跑或伪造新增 |
| 同一摘要重复收到 | 上一次推送成功但 state 没提交，或同一 ID 再次进入 pending | 先停止重跑，保留 state/latest 和运行日志；核对 pending/notified 后再处理，不要改 state 掩盖重复 |
| pip 安装很慢 | 默认源在境外 | 加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 部署成功但页面还是旧内容 | GitHub Pages 的 CDN 缓存约 10 分钟 | 等 10 分钟后 `Ctrl+F5`；想立刻确认部署是否已生效，可在地址后加 `?v=时间戳` 绕过缓存 |

---

## 十一、别人 fork 后怎么用（复用说明）

1. 打开仓库页 → 右上 **Fork** → **Create fork**
2. 进自己 fork 的仓库 → **Actions** 标签页 → 点绿色按钮 **I understand my workflows, go ahead and enable them**
   （GitHub 规定：fork 出来的仓库，定时任务默认是**禁用**的，必须手动启用一次，这一步绕不过去）
3. **Settings → Pages → Source** 选 **GitHub Actions**
4. **Actions → 定时更新站点 → Run workflow** 跑一次
5. 改 `config/sources.yml`，把 `site_title`、`sources` 换成自己的内容
6. 需要推送时，再按第七节加 Secrets

除了第 2、3 步这两个「开关」，**不需要改任何代码**。

---

## 十二、后续可以怎么扩展

| 版本 | 内容 | 改动位置 |
| --- | --- | --- |
| v1（历史） | 单源 / 定时 / 生成 / 部署 / 失败保底 | 已完成 |
| 阶段 5（已上线） | 来源连续失败维护告警，与普通推送分离 | `scripts/alert.py`、workflow、config |
| 阶段 6（已上线并验收） | 手动基线、全员健康门禁、baseline 未建立禁止普通推送、只推自然新增 | `scripts/fetch.py`、`scripts/notify.py`、`config/sources.yml`、README |
| 已完成（阶段 6） | Server 酱微信摘要：一条消息合并多条新增 | `scripts/notify.py` |
| v2 | 多源聚合、按源分组 | 只改 `config/sources.yml` |
| v3 | 关键词过滤、去重、历史归档 | `scripts/fetch.py` 加过滤函数 |
| v3 | 输出 RSS / JSON API 给自己别的项目用 | `scripts/build.py` 多写一个文件 |

架构上已经预留：数据（JSON）与展示（模板）分离，加源不改代码，加输出不改抓取。

---

## 十三、许可证

MIT，见 `LICENSE`。 fork、修改、商用都可以，保留版权声明即可。

---

## 附：本版本实测记录

### 本地实测（Windows 11 + Python 3.13）

- 演示源 `https://sspai.com/feed`：robots.txt 允许，成功解析 10 条
- 故意把 url 改成不存在的地址：脚本退出码仍为 **0**，10 条缓存保留，页面显示「缓存」+ 缓存时间
- **连续两次**失败：缓存时间仍然正确指向「最近一次成功」的时间，不会变成空值
- 生成的 `site/index.html` 用浏览器打开，排版、时间、摘要、链接均正常

### 线上阶段 6 实测（2026-11-22 至 2026-11-24）

- 正式基线 #15：`workflow_dispatch`，Run ID `44987389869`，五源 5/5 成功、`failed=0`、`cached=0`；`baseline.established=true`，`established_at=2026-11-22T12:59:21+08:00`，抑制 26 条历史条目；普通推送 0 条，手机确认没有收到任何普通内容消息。
- 第一次真实 Schedule #16：Run ID `45080588405`，2026-11-23 06:57 启动，head `efdfae2`，新增 6 条；推送日志显示只发 1 条“新增 6 条”的合并摘要，6 条全部标为 notified。
- 第二次真实 Schedule #17：Run ID `45286283619`，2026-11-24 07:09 启动，head `74ef096`，新增 9 条；推送日志显示只发 1 条“新增 9 条”的合并摘要，9 条全部标为 notified。
- 两次新增 ID 零交集；`pending` 均为 0；状态 41 条（26 suppressed + 15 notified）；归档 5 个日期、41 条，搜索索引 41 条，所有 ID 唯一。
- 两次均 5/5 来源成功、`source_failed=0`、`source_cached=0`；告警步骤均跳过，没有告警混入普通摘要。手机端一共只收到 2 条合并摘要，没有额外重复消息。
- 完整证据与逐项核对见本地 `验收记录/16-阶段6-正式基线与自然新增.md`；该目录只作本地留档，不进入源码包。
- 2026-11-21 的旧记录仍未删除：那次手动触发 workflow 约 35 秒完成，两个 job 全绿；单源 404 演练保留缓存、恢复后无重复追加。它属于阶段 5 之前的实测，不代表阶段 6 的基线结论。

### 尚未实测

- fork 后的完整复用（Actions 启用、Pages Source、Secret 自建、基线是否干净）；
- 公开仓库连续 60 天无提交后 scheduled workflow 被停用再恢复；
- `SERVERCHAN_SENDKEY` 轮换与旧密钥吊销；
- 线上多个来源同时故障的告警演练；
- Server 酱配额耗尽或接口变更时的长期重试与运维流程。
