# 每日科技速览 · 定时抓取静态站

一个**完全跑在 GitHub 上**的信息站模板：GitHub Actions 定时抓取公开 RSS，生成静态页面，自动部署到 GitHub Pages。

> 当前仓库已进入 M3 多源阶段（5 个源、`state.json`、按发布时间归档、只推新增）。阶段 4 的归档页与浏览器端静态搜索已通过线上验收；阶段 5 的来源连续失败告警已在本地实现并通过本地测试与隔离故障演练，**尚未上传线上**。下面的“单源 / 0 Secret”等描述保留为 M1 的历史部署步骤；M3 的数据口径以 **2.1 归档口径** 和 `config/sources.yml` 为准。

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
├─ scripts/notify.py                   只推新增：读 state 的 pending，成功后标 notified
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

配置文件里已经预留了「阮一峰的网络日志」的注释示例，把每行开头的 `# ` 去掉就能启用。

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

### 当前版本：普通抓取/部署需要 **0 个 Secret**

普通抓取、生成和 Pages 部署全部用 GitHub 内置的 `GITHUB_TOKEN`，不需要申请任何密钥。

只有需要微信推送时，才额外添加一个可选的 `SERVERCHAN_SENDKEY`（见下表）；不配置时会自动跳过推送，不影响抓取和部署。

### 后续加推送时才需要（现在不用建，先了解）

| Secret 名称 | 用途 | 获取方法 |
| --- | --- | --- |
| `FEISHU_WEBHOOK_URL` | 推送到飞书群 | 飞书群聊 → 右上角 `...` → 设置 → 群机器人 → 添加机器人 → **自定义机器人** → 复制 webhook 地址（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxx`）。安全设置建议勾「自定义关键词」填「更新」 |
| `SERVERCHAN_SENDKEY` | 推送到微信（Server 酱） | 打开 <https://sct.ftqq.com> → 微信扫码登录 → 复制页面上的 SendKey（形如 `SCTxxxxxx`） |

添加位置：仓库 **Settings → Secrets and variables → Actions → New repository secret**，Name 填上面的名字（全大写），Secret 粘贴值。

**三条铁律**：

1. 密钥只放 Secrets，**永远不要**写进 `config/sources.yml`、代码或 README
2. 代码里只通过 `os.environ.get("FEISHU_WEBHOOK_URL")` 读取，**不要 print 出来**
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
| 页面某源一直显示「缓存」 | 源挂了、被墙、或返回空数据 | 看 Actions 日志里的 `✗` 行；换源 |
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
| v1（当前） | 单源 / 定时 / 生成 / 部署 / 失败保底 | 已完成 |
| v2 | 飞书或 Server 酱推送摘要 | 新增 `scripts/notify.py`，workflow 加一步 |
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

### 线上实测（GitHub Actions + Pages，2026-11-21）

- 手动触发 workflow：抓取 / 构建 / 部署共约 35 秒，两个 job 全绿
- 单源失效演练：把 `url` 改为不存在的地址后运行，日志出现 `✗ HTTP 404` 并 `沿用上次缓存（14:25:28，10 条）`；workflow 仍为绿色，页面保留 10 条并显示「缓存」+ 缓存时间
- 恢复演练：改回原地址再运行，`✓ 成功，10 条`、`0 个沿用缓存`，缓存标记消失，`item_count` 仍为 10（没有重复追加）
- 完整截图与说明见本地 `验收记录/` 目录（仅作本地留档，不要上传仓库）
