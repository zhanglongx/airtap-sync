# CHANGELOG

## 0.1.0

- 新增 `scripts/gitlab_stats.py`：只读统计 GitLab 工程代码规模，用于确定 Git 缓存大小
- M0 工程骨架：`pyproject.toml`、配置加载与校验、平台路径、日志、进度事件、命令行骨架（各命令暂未实现）、GitHub Actions CI、pytest 测试
- M1 U 盘介质层：`init-usb`（支持 `--pair-id` 重新初始化）、outbox 原子写入与 `commit.json` 校验、传输配额、大文件切片、暂存与拼接
- M2a-1 GitLab 接入：REST 客户端（分页、重试、错误提示）、工程清单与排除规则、令牌存储（keyring，不可用时退回 600 权限文件）、`gitlab login` 命令
- M2a-2 Git 缓存：git 命令封装（令牌只通过环境变量注入且限定在 GitLab 地址）、并发 `ls-remote` 变化检测、按需克隆与 LRU 缓存、`git warm` 命令
- M2b ref 同步：分支与 tag 的三方合并（只快进、分叉停放到 `airtap/conflict/`、删除传播并备份、tag 冲突不覆盖）、增量 bundle 生成/校验/导入、推送（ci.skip、被拒的 ref 单独重试）、分支过滤；含双端随机模拟测试
- M2c 工程层：工程清单（manifest）、工程三方合并（对方新建则本端自动建群组和工程；删除、改名、转移只报告不处理）、改名识别、默认分支与归档状态复制、Git 同步状态库、`git snapshot` / `git baseline` / `git reset` 命令
- M2d Git 完整同步流程：`sync` 命令（`--usb`、`--dry-run`、`--confirm-deletes`）串起读取对方数据、工程与分支合并、导入、导出 bundle（增量、全量回退、按配额分批、断点续传）、防误删阈值确认、同步报告（写入 U 盘和本地）
- 新增 `gitlab check`：只读检查令牌、管理员权限、GitLab 版本、工程列表耗时、通过 HTTP 读取代码、LFS 接口、external_url；新增 LFS Batch 客户端雏形
- 新增 `config.example.toml` 配置示例，并加测试保证示例始终可解析
- 修复：配置中的 `~` 和环境变量现在会展开（此前会在当前目录建出名为 `~` 的目录）；`gitlab check` 的 LFS 探测改为请求一个虚构对象（GitLab 对空请求返回 404）并优先选用有 LFS 数据的工程，external_url 警告会写出服务端报告的地址
- 适配非管理员同步账号：`gitlab check` 将管理员权限降为警告并新增“项目权限”统计；同步时跳过无推送权限的工程并汇总警告；无权创建群组或在他人个人命名空间下建工程时给出明确提示
- `gitlab check`/同步：非管理员对“只是可见、不是成员”的工程视为无推送权限；项目权限检查列出权限分布和集中的顶层命名空间；external_url 与配置地址解析到同一台机器时不再警告
- 新增 `include_projects`（只同步指定命名空间下的工程，`exclude_projects` 优先）；非管理员可在自己的个人命名空间下建工程；`gitlab check` 按同步范围统计权限
- `gitlab check --namespaces`：按命名空间列出可写/总数（混合命名空间再展开一级），便于选择 `include_projects`
- 修复：推送权限改由服务端判断（`min_access_level`），正确识别从上级群组继承的角色；此前项目列表里的 `permissions` 只反映工程所在群组本身的成员关系，导致继承权限的工程被误判为只读
- M2e LFS 同步：扫描新提交里的 LFS 指针，从本端 GitLab 下载对象，随 bundle 经 U 盘（内容寻址、分片、分批）传递；对端推送前先通过 Batch 接口上传服务器还缺的对象；服务端报告的主机名被替换为配置的地址；对象读不到时在发送端报告
- M3 目录同步：忽略规则（gitignore 语法，两端取并集）、带哈希缓存的扫描、三方合并（内容决定是否变化；冲突时较新者为正式文件，另一份以确定性的名字保留在两端；删除与修改冲突时修改优先）、安全落盘（临时文件加替换、保留对端 mtime、同步中被改动则跳过、删除或覆盖前移入 30 天回收区）、防误删（目录不存在或被清空时中止、删除超阈值转确认）、内容去重（改名或对端已有相同内容时不传输）、分片分批传输；`sync` 现在一次同时处理 Git 和目录，共用一个 outbox 提交和传输配额，报告合并为一份
- M5 后台常驻程序：`airtap-sync agent`（托盘图标 + 进度窗口，没有显示环境或缺少托盘组件时自动改为文本模式）、按 pair_id 检测配对 U 盘并自动同步（可配置为先询问）、暂停/立即同步/打开报告和日志目录、单实例锁、`autostart enable|disable|status`（Windows 注册表 Run 键 / Linux autostart）；同步入口抽成 `execute_sync` 供命令行和后台程序共用；托盘依赖放在可选项 `airtap-sync[tray]`
- M6 打包与文档：PyInstaller 打包脚本（`scripts/build_exe.py`，一个目录里两个程序：命令行 `airtap-sync` 和无窗口的后台程序 `airtap-sync-agent`）、GitHub Actions 打包校验和发布流程（推送 `v*` 标签时在 Windows 上构建并上传压缩包）、frozen 环境下开机自启优先使用无窗口的程序；重写 README（原理、安装、配置、初始化流程、日常使用、冲突与恢复、常见问题）；已在 Linux 上构建并冒烟测试通过
- 修复打包：忽略指向已卸载 Python 的 `TCL_LIBRARY`/`TK_LIBRARY`；tkinter 不可用时自动从打包中排除；Windows 专用的隐藏导入只在 Windows 上添加
- 新增 `Makefile`：`make` 一条命令完成打包（自动创建 .venv 并安装依赖），另有 `test`、`lint`、`format`、`check`、`icon`、`version`、`clean`、`distclean`、`help` 等目标；新增 `scripts/clean.py`、`scripts/make_icon.py`
- 应用图标：`src/airtap_sync/assets/icon.png` 随包发布；打包时转成多尺寸 `.ico` 嵌入两个 Windows exe；托盘图标改用该图并在角上加状态色点，进度窗口也使用它；Linux 的开机自启条目引用该 PNG（ELF 可执行文件不能内嵌图标）
- 新增 `status` 命令：只读本地数据，显示上次同步时间、各目录相对上次同步的本地改动（新增/修改/删除）、待处理的冲突副本，以及 Git 基准里已共有的工程数
