# airtap-sync

在两个物理隔离的网络（网络 A 和网络 B）之间，通过 **U 盘**同步两样东西：

- **目录**：电脑 A 和电脑 B 上指定的目录，同步后内容一致；
- **Git 仓库**：两个网络里各自的 GitLab 上的全部工程（分支、tag 和 LFS 对象）。

每天早上在电脑 A 上插入 U 盘同步一次，再在电脑 B 上插入同一个 U 盘同步一次，两边就一致了。也可以让后台程序检测到 U 盘后自动完成。

> 状态：目录同步、Git 同步（含 LFS）、后台常驻程序、跨网电脑直连模式（`relay`）和 `status` 命令均已完成。详细设计见 `docs/development-plan.md`。

## 它是怎么工作的

U 盘只是一个“信箱”。每台电脑在 U 盘上只写自己的那一半（`outbox/A` 或 `outbox/B`），读对方的那一半：

1. **读对方**：读取对方留下的清单（有哪些文件、哪些工程和分支）；
2. **合并**：和自己的当前状态、以及“上次双方一致的状态”做三方比较，决定哪些要接收、哪些要删除、哪些有冲突；
3. **写自己**：把自己的最新清单，以及对方缺的内容（文件内容、增量 git bundle、LFS 对象）写回 U 盘。

清单记录的是**完整状态**而不是操作日志，所以漏插一天、U 盘丢失、同步中途拔盘，下一次都会自动补齐；重复运行也是安全的。每次写入都是“先写临时文件再改名，最后才写完成标记”，拔盘只会让那次写入被对方忽略。

## 安全保护

- **永不强推**：Git 分支只做快进。两端在同一分支上各自提交造成分叉时，对方的版本会被放到 `airtap/conflict/<对方>/<分支>`，由人合并，合并后程序自动清理。
- **永不丢数据**：目录同步里被删除或覆盖的文件先移入 `<目录>/.airtap-sync/trash/<日期>/`，保留 30 天；Git 里被删除的分支和 tag 先备份到本地缓存的 `refs/airtap-trash/`。两边都改了同一个文件时，较新的作为正式文件，另一份以 `名称.sync-conflict-<端>-<日期>-<哈希>.扩展名` 保留在两边。
- **防误删**：目录不存在或被清空、GitLab 返回零个工程、一次删除或新建超过阈值（默认 20）时，不自动执行，转人工确认。
- **同步中途出现变化**：替换文件前会再次核对它没被改动；被占用的文件留到下一轮。
- **分批传输**：超过 U 盘配额的内容切片，分多天传完，已传的部分不会重传。

## 安装

### 方式一：直接使用打包好的程序（Windows 推荐）

从 GitHub Releases 下载 `airtap-sync-<版本>-windows.zip`，解压到两台电脑上（也可以放在 U 盘的 `airtap-sync/bin/` 里随身带）。里面有两个程序：

- `airtap-sync.exe`：命令行程序；
- `airtap-sync-agent.exe`：没有黑窗口的后台常驻程序（托盘图标）。

两台电脑都需要安装 **Git**（2.31 及以上）。

### 方式二：用 Python 安装

需要 Python 3.12 及以上和 Git：

```bash
python -m venv .venv
.venv/bin/pip install -e ".[tray]"      # Windows：.venv\Scripts\pip ...
```

`[tray]` 是托盘图标需要的组件（pystray、Pillow）；只用命令行可以省略。

## 快速开始

### 1. 准备 U 盘（只做一次）

建议格式化为 exFAT（FAT32 单个文件不能超过 4 GiB）。在任意一台电脑上：

```bash
airtap-sync init-usb E:\
```

会在 U 盘上创建 `airtap-sync/` 目录，并打印一个 `pair_id`。**把这个 `pair_id` 记下来**，两台电脑的配置里都要用。U 盘丢了或要换盘，用同一个 `pair_id` 重新初始化即可：`airtap-sync init-usb F:\ --pair-id <原来的 pair_id>`，下一次同步会自动补齐。

### 2. 写配置文件

默认位置：Windows 是 `%APPDATA%\airtap-sync\config.toml`，Linux 是 `~/.config/airtap-sync/config.toml`；也可以用 `--config 文件` 指定。示例见 [`config.example.toml`](config.example.toml)，最小的例子：

```toml
side = "A"                       # 这台电脑在哪个网络："A" 或 "B"
pair_id = "<init-usb 打印的 pair_id>"

[folders.docs]                   # 目录同步：两端用相同的名称
path = 'D:\work\docs'
ignore = ["build/", "*.log"]     # 额外的忽略规则（gitignore 语法）

[gitlab]                         # Git 同步
url = "http://10.1.2.3"          # 本网络 GitLab 的地址，不带结尾的 /
include_projects = ["ZH/*"]      # 可选：只同步这些工程（按完整路径的通配符）

[gitlab.cache]
dir = 'D:\airtap-cache'          # 本地仓库缓存目录
max_size = "100GiB"
```

两台电脑的配置只有 `side`、路径和 GitLab 地址不同；**`include_projects`、`exclude_projects`、分支过滤要写成一样**。只想同步目录就不要写 `[gitlab]`，只想同步 Git 就不要写 `[folders.*]`。

### 3. 目录同步

不需要别的准备，直接同步（见“日常使用”）。第一次不会传内容，而是先交换清单，所以头几轮（A、B、A、B）才会收敛。如果两边目录原本就一样（比如你手工拷过一份），这几轮几乎不传数据，只建立基准。

### 4. Git 同步的准备

先保存令牌并检查：

```bash
airtap-sync gitlab login                      # 输入个人访问令牌（api 权限），保存到系统凭据库
airtap-sync gitlab check                      # 只读检查
airtap-sync gitlab check --namespaces         # 各命名空间里你可推送的工程数，便于选 include_projects
```

`check` 会检查：令牌是否有效、是否管理员、GitLab 版本、工程列表耗时、令牌能否通过 HTTP 读取代码、LFS 接口、`external_url`、以及你在各工程里能否推送。

**权限说明**：往一个工程推送需要在该工程里至少是 Developer（包括从上级群组继承的角色）。同步账号最好是管理员——这样才能在别人的个人命名空间下建工程、也不会遇到只读的工程。不是管理员时程序也能工作，只是对没有推送权限的工程只能导出、不能接收，对方新建的、本端无权创建的工程会写进报告。

**初始化（A 备份、B 恢复）**：首次同步不经过 U 盘。把 A 的 GitLab 整机备份，在 B 上恢复，然后让两边从同一份“基准”出发：

```bash
# 电脑 A，GitLab 备份之前（尽量选没人提交的时段）：
airtap-sync git snapshot --output E:\airtap-sync\baseline.json
# …… B 上恢复 GitLab 备份 ……
# 电脑 B：
airtap-sync git baseline E:\airtap-sync\baseline.json
# 两边各运行一次，预先克隆最近活跃的工程（可选，能避免之后的完整克隆）：
airtap-sync git warm --days 90
```

不做这一步也能同步，但程序会把两边都有的工程当作首次同步处理，并在报告里警告；需要重新整机同步时，先 `airtap-sync git reset --yes` 清空本地 Git 状态，再重复上面的流程。

### 5. 第一次同步

建议先试运行，看看会发生什么而不做任何改动：

```bash
airtap-sync sync --usb E:\ --dry-run
airtap-sync sync --usb E:\
```

## 日常使用

### 命令行

```bash
airtap-sync sync --usb E:\                 # 同步目录和 Git
airtap-sync sync --usb E:\ --only files    # 只同步目录
airtap-sync sync --usb E:\ --only git      # 只同步 Git
airtap-sync sync --usb E:\ --confirm-deletes   # 确认执行超过阈值的删除和新建
```

退出码：`0` 正常，`1` 出错或中止，`3` 已完成但有需要人工处理的事项（冲突、失败、待确认）。

### 后台常驻程序（推荐）

```bash
airtap-sync agent            # 托盘图标 + 进度窗口；没有图形环境时自动用文本模式
airtap-sync autostart enable # 开机自动启动（disable 关闭，status 查看）
```

常驻后每 3 秒检查一次 U 盘，发现 `pair_id` 匹配的就自动同步，完成后通知“同步完成，可以拔出 U 盘”。托盘菜单：立即同步、打开最近报告、打开日志目录、暂停自动同步、退出。图标颜色：灰蓝=空闲，蓝=同步中，绿=成功，橙=有事项要处理，红=出错。配置里 `[agent] auto_sync = false` 则检测到 U 盘后先询问。配置文件改动不用重启。

### 跨网电脑模式（只同步 Git）

如果有一台能同时访问两个网络的电脑，可以不经过 U 盘，直接在两个 GitLab 之间同步 Git（完整的带注释示例见 [`config-relay.example.toml`](config-relay.example.toml)）：

```toml
# relay.toml —— 这台电脑不需要 side、pair_id，也不需要 [gitlab]
[relay]
url_a = "http://gitlab-a.example.com"
url_b = "http://gitlab-b.example.com"
include_projects = ["ZH/*"]       # 筛选规则两边共用
[relay.cache]
dir = 'D:\airtap-relay-cache'
```

```bash
airtap-sync --config relay.toml gitlab login   # 依次输入两个 GitLab 的令牌
airtap-sync --config relay.toml relay --dry-run
airtap-sync --config relay.toml relay
```

每个工程在同一个本地缓存里先把 B 的变化合并到 A，再把 A 的（更新后的）状态合并到 B；规则和 U 盘模式完全一样：只快进、分叉放到 `airtap/conflict/`、删除传播并备份、tag 冲突不覆盖，LFS 对象直接在两台服务器之间复制，超过阈值的删除和新建同样要确认。基准保存在这台电脑本地，所以 relay 和 U 盘模式交替使用时，最坏情况是某个已删除的分支被“复活”，不会丢失提交；不建议两种模式长期混用。

### 查看状态

```bash
airtap-sync status    # 上次同步时间、各目录相对上次同步的本地改动、待处理的冲突副本
```

只读本地数据，不访问网络和 U 盘，随时可以运行。

### 每天的流程

1. 电脑 A：插入 U 盘，等待“同步完成”，拔出。
2. 电脑 B：插入同一个 U 盘，等待“同步完成”，拔出。

B 上的改动要到第二天早上在 A 上插入 U 盘时才会到达 A，这是一天一次的节奏决定的。

## 报告和需要人工处理的事项

每次同步生成一份 Markdown 报告，同时保存在 U 盘的 `airtap-sync/reports/` 和本机日志目录的 `reports/` 下。需要你处理的内容放在最前面：

| 报告里的内容 | 怎么处理 |
|---|---|
| **冲突（目录）** | 两个版本都在目录里了：正式文件是较新的一份，另一份是 `*.sync-conflict-*` 文件。比较后保留想要的，删掉另一份（删除会同步到对端）。 |
| **冲突（Git 分叉）** | 对方的版本在 `airtap/conflict/<对方>/<分支>`。在自己的 GitLab 上把它合并进原分支，下一轮同步程序会自动清理这个冲突分支。 |
| **tag 冲突** | 同名 tag 在两边指向不同提交，两边都不会被改动。统一其中一边的 tag 后会自动恢复。 |
| **待确认：删除分支或文件、新建工程** | 超过安全阈值被暂停了。确认无误后加 `--confirm-deletes` 重新运行（后台程序会弹窗询问）。 |
| **工程变化（未自动处理）** | 某个工程在一端被删除、改名或转移。程序从不自动删除或改名工程，请在对端手工做同样的操作。 |
| **失败** | 逐条给出原因，例如无推送权限、文件被占用、受保护分支拒绝推送。 |
| **等待数据** | 内容还没传完（U 盘配额不足时分多天传），下一轮自动继续。 |

### 恢复被删除或覆盖的东西

- **目录文件**：在 `<目录>/.airtap-sync/trash/<日期>/` 下按原路径找到，拷回即可；30 天后自动清理。
- **Git 分支或 tag**：在本机的缓存仓库（`[gitlab.cache] dir` 下对应的 `*.git`）里，备份在 `refs/airtap-trash/<时间>/…`，用 `git branch 名称 <提交>` 恢复后推送到 GitLab。缓存仓库会按最近最少使用淘汰，所以尽早处理。

## 配置参考

完整的带注释示例见 [`config.example.toml`](config.example.toml)。要点：

| 配置 | 说明 |
|---|---|
| `side`、`pair_id` | 必填。 |
| `[transfer]` | `max_bytes`（单次最多写入 U 盘，默认 28GiB）、`reserve_free`（U 盘保留空间，默认 1GiB）、`part_size`（切片大小，默认 512MiB）。 |
| `[folders.<名称>]` | `path`、`ignore`。默认已忽略 `.git/`、`~$*`、`*.tmp`、`Thumbs.db`、`desktop.ini`。 |
| `[gitlab]` | `url`；`include_projects`、`exclude_projects`；`include_branches`、`exclude_branches`；`ci_skip`（默认 true，推送时附带 `ci.skip`）；`concurrency`。 |
| `[relay]` | 跨网电脑模式：`url_a`、`url_b`，另外可用与 `[gitlab]` 相同的筛选项和 `[relay.cache]`。 |
| `[gitlab.cache]` | `dir`（必填）、`max_size`（默认 100GiB，超出后按最近最少使用淘汰）。 |
| `[safety]` | `max_deletions`、`max_new_projects`（默认 20）。 |
| `[agent]` | `auto_sync`、`poll_seconds`。 |

路径里的 `~` 和环境变量会被展开：Windows 上 `'%APPDATA%\airtap-sync\docs'`、`'%USERPROFILE%\Documents'`（也可写成 `$APPDATA`、`${APPDATA}`），Linux 上 `'~/docs'`、`'$HOME/docs'`；含反斜杠的路径要用单引号（TOML 的字面量字符串）。引用了没有定义的环境变量会直接报错，不会创建一个名字里带 `%` 的目录。令牌保存在系统凭据库（Windows 凭据管理器）；没有凭据库时保存在权限为 600 的文件里；也可以用环境变量 `AIRTAP_GITLAB_TOKEN`。

## 常见问题

- **`gitlab check` 说“git 读取失败”**：令牌权限不够，在 GitLab 里给令牌加上 `read_repository` 和 `write_repository` 后重新 `gitlab login`。
- **LFS 上传下载地址不通**：服务端会报告它自己的主机名（`external_url`），程序会把它替换成你配置的 `url`，所以不依赖那个主机名能否解析。
- **只能看到一部分工程**：非管理员只能看到有权限的工程，看不到的不会同步。
- **某个工程的更新没有被接收**：报告里会汇总“本端无推送权限”的工程；请 GitLab 管理员给同步账号加权限，或把它们加进 `exclude_projects`。
- **同步很久没动静**：大工程第一次被处理时需要完整克隆（最大的工程约 18 GiB）；之后只拉增量。
- **U 盘空间不够**：超过配额的内容会分批，报告里会写“本轮未发送”，下一轮继续；可以调大 `[transfer] max_bytes` 或换更大的盘。
- **杀毒软件拦截 exe**：把 `airtap-sync` 目录加入白名单；PyInstaller 打包的程序偶尔会被误报。
- **打包时报 `Tk data/library directory ... could not be collected`**：多半是环境变量 `TCL_LIBRARY` 或 `TK_LIBRARY` 还指向一个已经卸载的 Python（路径里的版本和你正在用的不一致）。`scripts/build_exe.py` 现在会自动忽略它们；也可以在 PowerShell 里 `Remove-Item Env:TCL_LIBRARY, Env:TK_LIBRARY` 后再试。如果这个 Python 的 tkinter 本身不可用，打包会自动去掉它，后台程序就只能用文本模式。
- **托盘图标不出现**：Windows 上确认安装了 `[tray]` 组件；Linux 的 GNOME 需要 AppIndicator 扩展。没有托盘时程序会自动改用文本模式，用 `--no-gui` 也可以强制文本模式。

## 开发和打包

```bash
make            # 一条命令打包：首次会自动创建 .venv、安装依赖，然后生成 dist/airtap-sync/ 和压缩包
make test       # 测试
make check      # 代码检查 + 测试
make format     # 自动修复并格式化
make clean      # 删除 build/、dist/ 和各种缓存
make distclean  # 同时删除 .venv
make help       # 列出所有目标
```

Windows 请在 Git Bash（或其他带 `sh` 的环境）里运行 GNU make；没有 make 时，直接 `python scripts/build_exe.py` 效果相同。`PYTHON=<解释器>` 指定用哪个 Python 创建 `.venv`，`PY=<解释器>` 则直接使用该解释器、跳过 `.venv`（CI 里常用）。

打包产物里，Windows 的两个 exe 都带有应用图标（`src/airtap_sync/assets/icon.png`，构建时自动转成多尺寸 `.ico` 嵌入）；托盘图标和进度窗口也用同一张图。Linux 的可执行文件格式不能内嵌图标，所以只在开机自启条目（`.desktop`）里引用这张 PNG。

手工搭环境：

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,tray,build]"
.venv/bin/pytest                 # 测试
.venv/bin/ruff check .           # 代码检查（B, E, F, I, UP）
.venv/bin/ruff format .
python scripts/build_exe.py      # 打包 exe（PyInstaller）
```

测试里用进程内的模拟 GitLab 接口和真实的 git 仓库做端到端验证，不需要真实的 GitLab。`scripts/gitlab_stats.py` 是一个只读的小脚本，用来统计一个 GitLab 的工程数量和代码规模。

变更记录见 [`CHANGELOG.md`](CHANGELOG.md)，许可证为 GPL-3.0（见 `LICENSE`）。
