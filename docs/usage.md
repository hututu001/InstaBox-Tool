# InstaBox Tool 使用手册

**版本：0.0.1 · 作者：Pre** · [返回首页](../README.md) · [保活说明](keepalive.md)

## 安装

### 准备环境和授权

通过 SSH 登录要部署的 InstaBox 机器，以普通用户运行安装命令。需要 Linux x86_64、Python 3，以及可用的用户级 systemd；不要给安装命令加 `sudo`。

登录 [InstaBox Dashboard](https://dashboard.instabox.sh/)，进入 **API Keys → Create Key**。选择 Restricted 时授予 `machines:write`，该权限包含机器读取权限；Full access Key 也可使用。复制只显示一次的 `ibk_...` 密钥。若控制台要求完成账号开通，先按页面提示处理，并确认账号能访问目标机器。[官方授权说明](https://instabox.sh/docs/keys-sdk)

### 完成安装

```bash
export PATH="$HOME/.local/bin:$PATH" && bash <(curl -fsSL https://raw.githubusercontent.com/hututu001/InstaBox-Tool/main/setup.sh)
```

按以下顺序完成首次配置：

1. 粘贴 API Key 后回车。输入不显示字符，验证成功后保存在本机。
2. 安装器部署 sing-box 和 Cloudflare Quick Tunnel，生成名为 **IBT-Quick** 的 VLESS 链接。
3. 自动识别当前机器并配置 API 保活；无法唯一确定本机时，按列表明确选择这一台。
4. 安装器检查节点与保活结果，打开管理菜单。复制完整的 `vless://` 链接，导入客户端测试连接。

未完成的步骤会给出处理提示。节点异常先运行 `ibt diagnose`；授权问题用 `ibt key set` 更新密钥，再执行 `ibt keepalive auto-local` 配置本机保活。

### 菜单操作

输入 `ibt` 打开主页，输入编号后回车；`0` 返回或退出。退出菜单后，已启用的后台服务继续运行。尚未安装节点时，“查看节点”会引导进入节点管理安装。

终端采用统一缩进、标签和表格宽度；设置 `NO_COLOR=1` 可关闭颜色。API Key、SSH/Mosh 命令等隐藏输入中，粘贴后按回车即可。

API Key 保存在 `~/.config/insta-box/api-key`，文件权限为 `0600`。云端管理、API 保活和可选的 SSH token 自动换新共用这份授权：

| 操作 | 命令 |
| --- | --- |
| 更换密钥 | `ibt key set` |
| 查看授权状态 | `ibt key status` |
| 清除本机保存的密钥 | `ibt key forget` |

## 节点与隧道

主菜单 **3「节点管理」** 集中管理节点服务、隧道、日志和连接检查。

| 需要做什么 | 命令 |
| --- | --- |
| 复制当前节点链接 | `ibt show` |
| 查看服务状态 | `ibt status` |
| 修复服务并检查连接 | `ibt repair` |
| 检查配置、本机和公网 WebSocket 握手 | `ibt diagnose` |
| 仅检查本机 | `ibt diagnose --local-only` |
| 查看节点日志 | `ibt logs` |
| 查看临时／固定隧道日志 | `ibt logs quick` / `ibt logs named` |
| 重启节点服务 | `ibt restart` |

修复保留现有节点配置。WebSocket 握手通过说明相应链路已连通，完整的 VLESS 授权和代理出站仍需用客户端验证。

### 临时隧道

| 操作 | 命令 |
| --- | --- |
| 启动临时隧道 | `ibt quick` |
| 重建隧道并获取新地址 | `ibt refresh-quick` |
| 切换到 HTTP/2 并重建 | `ibt quick-protocol http2` |
| 恢复自动传输并重建 | `ibt quick-protocol auto` |
| 查看开机自启设置 | `ibt quick-autostart status` |
| 开启／关闭开机自启 | `ibt quick-autostart enable` / `ibt quick-autostart disable` |
| 停止临时隧道 | `ibt stop-quick` |

**重建会更换 Quick 域名。** 操作完成后用 `ibt show` 复制当前链接并重新导入客户端。尚未取得地址时，查看 `ibt logs quick`；不要继续使用此前导出的域名。

启用开机自启后，机器完整重启时会启动新的临时隧道。暂停后恢复可能保留原进程，因此不一定更换域名。修改自启开关只影响后续启动，不会重建当前运行的隧道。Quick Tunnel 的服务限制见 [Cloudflare 官方说明](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)。

### 固定域名

固定隧道让节点使用你自己的地址，例如 `proxy.example.com`，重启隧道后也能继续使用这个域名。下面以**首次创建固定隧道**为例，按“Cloudflare 网页 → 机器终端 → Cloudflare 网页 → 机器终端”的顺序操作。

开始前准备好：

- 一个已经添加到 Cloudflare、正常托管的域名。
- 一台**已经完成本项目一键安装**的 InstaBox 机器。后面的终端命令都在这台机器上执行，使用当初安装工具的普通用户；可以用网页 Terminal，也可以先通过 SSH 登录。

**已安装旧版本的用户，先运行 `ibt update`，更新后重新输入 `ibt` 打开新菜单。**

整个过程需要保存两项内容：**Tunnel Token** 是连接隧道的一长串凭据；**完整主机名**是你设置的域名，例如 `proxy.example.com`。下面会分别说明在哪里复制、在哪里使用。

#### 第 1 步：在 Cloudflare 创建隧道，复制 Token

1. 登录 [Cloudflare 控制台](https://dash.cloudflare.com/)。
2. 在左侧菜单点击 **联网（Networking）**，再点击 **Tunnels**。
3. 点击右上角 **＋创建隧道（Create Tunnel）**，填写一个自己能认出的名称，例如 `instabox`，然后点击 **创建隧道**。
4. 在操作系统处选择 **Debian**；如果页面还要求选择架构，本项目使用 **64-bit / amd64 / x86_64**。这里选的是远端机器的系统，不是你电脑的系统；其他 Linux 系统按实际情况选择。
5. 找到下方 **Install cloudflared**。这一栏用于安装隧道程序；**本项目一键安装时已经安装了 cloudflared，可以跳过这一项**。如需按网页安装，点击右侧复制按钮，粘贴到目标机器的 Terminal 或 SSH 终端中执行。
6. 找到 **Install as service**，点击右侧复制按钮。复制的命令大致如下：

   ```text
   sudo cloudflared service install eyJ……
   ```

7. 末尾**从 `eyJ` 开头的完整字符串**就是 Tunnel Token。可以只复制这一串，也可以直接保留刚复制的整条命令，下一步都能粘贴。示例中的省略号不属于真实 Token。

**不用在终端执行这条安装命令。** 下一步把它粘贴到本工具的 Token 输入提示中，脚本会提取 Token 并启动隧道。保留 Cloudflare 页面，暂时不用点击“继续”。Token 请妥善保存，勿对外公开。[官方 Token 获取说明](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)

#### 第 2 步：回到机器终端，按提示配置固定隧道

回到安装本工具的那台机器，输入 `ibt` 打开菜单，依次选择 **3「节点管理」→ 3「管理域名隧道」→ 5「配置固定隧道」**。已经在菜单中时，直接进入该选项即可。

也可以在命令行直接运行：

```bash
ibt tunnel
```

出现 Token 提示后，粘贴第 1 步复制的**完整 Token 或整条安装命令**，然后回车。**输入时不显示字符，属于正常现象。** 脚本会检查格式、保存 Token 并启动隧道。

已有配置时，直接回车可沿用，粘贴新 Token 会自动更换并重启；本地保存或服务启动失败时会保留原配置并提示原因。需要取消时输入 `0`；首次配置直接回车也会取消。

执行成功后会提示“**固定隧道服务已启动**”，并显示**本机入口**，例如 `http://127.0.0.1:20000`。记下这个地址，下一步要填入 Cloudflare；也可以随时运行 `ibt status` 再次查看。

回到 Cloudflare 页面，等待隧道显示已连接；隧道列表中的正常状态为 **Healthy**，然后点击下方 **继续（Continue）**。如果一直未连接，先在机器上运行 `ibt logs named` 查看原因。

#### 第 3 步：在 Cloudflare 添加路由，设置固定域名

1. 进入刚创建的隧道，在 **路由（Routes）** 区域点击 **添加路由（Add route）**。
2. 选择 **已发布的应用程序（Published application）**。
3. 按下表填写。这里用 `proxy.example.com` 演示，请换成自己的域名：

| 页面字段 | 怎么填 |
| --- | --- |
| 左侧「子域名 / Subdomain」 | 填一个好记的名称，例如 `proxy` |
| 右侧「域名 / Domain」 | 选择自己已在 Cloudflare 托管的域名，例如 `example.com` |
| 下方显示的「完整主机名 / Hostname」 | 复制并保存，例如 `proxy.example.com`，第 4 步要用 |
| 「路径 / Path」 | **留空，不填写** |
| 「服务 URL / Service URL」 | 填第 2 步脚本显示的**完整本机入口**，例如 `http://127.0.0.1:20000` |

**`20000` 只是示例，以脚本显示的本机端口为准。** `127.0.0.1` 表示运行本工具的这台机器；这里填写 `http://`。如果你的页面把协议与地址分成两个输入项，则服务类型选择 **HTTP**，地址只填 `127.0.0.1:实际端口`。路径保持为空，不需要填节点链接中的 WebSocket 路径。

4. 检查无误后，点击 **添加路由（Add route）** 保存。Cloudflare 会为这个主机名创建对应的 DNS 记录。[官方创建与路由说明](https://developers.cloudflare.com/tunnel/get-started/) · [DNS 说明](https://developers.cloudflare.com/tunnel/concepts/routing/)

#### 第 4 步：回到机器终端，把完整主机名填给脚本

执行：

```bash
ibt bind
```

看到“代理域名”提示后，粘贴第 3 步保存的**完整主机名**，例如：

```text
proxy.example.com
```

**只填域名，不带 `https://`，不带端口和路径。** 这里的域名必须与 Cloudflare 路由中的完整主机名一致。这个操作会记录域名并生成固定节点链接；第 3 步已经保存好路由，无需再添加一次。

菜单操作对应 **`ibt` → 3「节点管理」→ 3「管理域名隧道」→ 3「绑定固定域名」**。

#### 第 5 步：复制固定节点，导入客户端验证

运行：

```bash
ibt show
```

找到“**固定节点**”下面的完整 `vless://` 链接，节点名为 **InstaBox-Tool-Fixed**，复制后导入客户端并测试连接。此时可能同时显示旧的 **IBT-Quick** 临时节点，注意选用刚生成的固定节点。

确认固定节点能正常使用后，如果不再需要临时节点，再执行：

```bash
ibt stop-quick
```

固定域名用于节点连接，是否成功以客户端实际连接为准；不需要通过浏览器打开域名来判断。

#### 常见卡点

| 遇到的情况 | 怎么处理 |
| --- | --- |
| 找不到自己托管的域名 | 确认当前登录的 Cloudflare 账号正确，且域名已添加到该账号并正常托管 |
| 找不到“配置固定隧道”，或运行后没有 Token 提示 | 先执行 `ibt update`，更新后重新打开 `ibt` 菜单 |
| Token 提示格式错误 | 重新复制 `eyJ` 开头的完整字符串，或 Cloudflare 的整条 `sudo cloudflared service install ...` 命令 |
| 需要更换 Token | 再次运行 `ibt tunnel` 并粘贴新 Token，脚本会自动保存并重启 |
| 固定隧道未运行，或网页一直未连接 | 按终端提示处理，再用 `ibt logs named` 查看原因 |
| 添加路由时提示同名 DNS 记录已存在 | 换一个尚未使用的子域名，并在 `ibt bind` 中使用新的完整主机名 |
| 隧道显示 Healthy，但固定节点无法连接 | 核对服务 URL 中的端口、HTTP 协议，以及 `ibt bind` 的域名；再确认客户端导入的是固定节点链接 |

<details>
<summary>可选：使用 Cloudflare API 自动添加路由</summary>

已有固定隧道、希望由脚本添加路由和 DNS 时，可使用 `ibt bind-auto`。它需要另行创建 **Cloudflare API Token**，授予 Tunnel 配置读写、Zone Read、DNS Read/Edit 权限，修改前会保存相关配置备份。

这里的 **Cloudflare API Token** 与上文 `eyJ` 开头的 **Tunnel Token** 用途不同，也不同于 InstaBox 的 `ibk_...` API Key。按上面的网页流程配置时，无需使用 `ibt bind-auto` 或另建 API Token。

</details>

## CF 优选

进入 **3「节点管理」→ 8「优选节点地址」**，或运行 `ibt cf`：

1. 选择本机固定节点、已取得当前地址的 Quick 节点，或手动粘贴 VLESS 链接。
2. 选择地址来源、IPv4／IPv6 和生成数量。
3. 复制生成的链接，在客户端导入并测试。

功能参考 [Joey 的 cfy](https://github.com/byJoey/cfy)，支持 **VLESS + WebSocket + TLS**。生成时保留 UUID、端口、SNI、Host 和 WebSocket 路径，只调整连接地址与名称。

选用该功能时会检查 Python 3 和 CA 证书依赖；支持 apt 且有免密 sudo 时自动安装缺失项。设置 `IBT_NO_SUDO=1` 可禁止提权；无法自动安装时会提示需要的依赖。

结果保存在权限为 `0600` 的 `~/.config/insta-box/cf-nodes.txt`，用 `ibt cf --show-last` 再次查看。Quick 域名重建后，需要基于当前链接重新生成优选链接。

## 机器保活

首次安装已为本机配置 API 保活。进入 **4「机器保活」**，可以查看最近结果、下次执行时间和已配置目标。

| 菜单 | 用途 |
| --- | --- |
| 1 一键保活机器 | 加入当前授权可见的运行中机器，并开启后台任务 |
| 2 管理保活目标 | 手选云端机器、添加 SSH/Mosh 命令、移除目标 |
| 3 管理自动任务 | 立即执行、启用、停止、查看日志 |
| 4 管理命令凭据 | 更新 SSH 命令、配置 token 自动换新 |
| 5 平台高级选项 | 配置并读回平台暂停策略 |
| 6 打开机器管理 | 切换到全部机器总览 |
| 7 查看保活详情 | 查看各目标的成功时间、有效期和错误 |

一键保活对应 `ibt keepalive auto`，保留已有目标与配置，不会启动暂停或停止的机器。后台维护已经加入的目标；之后创建或启动其他机器，需要再次执行一键保活。只想维护部分机器时，进入 **管理保活目标 → 选择云端机器**。

一个运行中的控制端即可管理多台机器。控制端暂停或停止后，其内部定时任务也会停止；此时需要从控制台或独立控制端恢复。API 保活、SSH 命令和凭据换新的完整说明见[保活说明](keepalive.md)。

## 机器管理

进入 **5「机器管理」**，一次显示当前授权可见的全部机器，包括未启动的机器。`ibt cloud` 可直接查看总览。

| 菜单 | 用途 |
| --- | --- |
| 1 查看机器详情 | 查看所选机器的状态和平台配置 |
| 2 启动所选机器 | 选择一台或多台启动 |
| 3 暂停所选机器 | 确认后暂停所选机器 |
| 4 停止所选机器 | 确认后停止所选机器 |
| 5 管理机器保活 | 调整所选机器在本控制端的 API 保活 |
| 6 查看操作进度 | 查询云端异步操作 |
| 7 管理账户授权 | 查看账户配额、检查或更换 API Key |
| 8 打开保活面板 | 切换到后台保活管理 |
| 9 刷新机器列表 | 重新读取当前状态 |

启动和保活分别操作：启动负责开机，保活负责周期维护；开启保活不等于开机，关闭保活也不等于关机。预先选入的暂停或停止目标会等待机器运行。

批量操作明确选择目标，留空取消。暂停或停止多台时，若包括当前控制端，会最后处理它。云端返回已受理不等于操作完成；工具会查询进度，结果暂时不明确时保留操作 ID，便于继续查询。

命令行对应 `ibt cloud batch start|pause|stop`、`ibt cloud protect on|off`、`ibt cloud info --choose` 和 `ibt cloud operations`。账户信息用 `ibt cloud account` 查看。生命周期含义以 [InstaBox 官方说明](https://instabox.sh/docs/api/lifecycle#start)为准。

## 本机信息与时区

主菜单 **1「本机信息」** 显示当前运行脚本这台机器的系统、内核、CPU、内存、Swap、磁盘、网络和服务状态。`ibt info` 查看同一信息，`ibt info --json` 输出 JSON；读取本机信息不请求外部 IP 查询服务。

时间默认跟随机器时区。可在本机信息菜单中修改显示时区：

```bash
ibt timezone set Asia/Shanghai
ibt timezone status
```

该设置影响本工具的时间显示，不改变系统时钟、保活间隔或凭据到期时刻。使用 `ibt timezone reset` 恢复默认；显式设置的 `TZ` 环境变量优先。SSH 不会自动把电脑时区传给服务器。

## 机器优化

主菜单 **6「机器优化」** 先读取本机情况，再给出建议。修改前展示计划并要求确认；操作作用于当前机器，与云端菜单选中的目标无关。

| 操作 | 命令 |
| --- | --- |
| 查看现状／建议 | `ibt optimize status` / `ibt optimize recommend` |
| 应用推荐设置 | `ibt optimize apply` |
| 开启 Swap，容量单位 MiB | `ibt optimize swap-on [MiB]` |
| 关闭本工具创建的 Swap | `ibt optimize swap-off` |
| 开启 BBR + fq／恢复原值 | `ibt optimize bbr-on` / `ibt optimize bbr-off` |
| 恢复本工具管理的系统设置 | `ibt optimize restore` |

Swap 创建前检查内存、现有 Swap、剩余磁盘和文件系统条件，使用 `/var/lib/insta-box-tool/swapfile`。已有 Swap 不会被覆盖。

BBR 设置检查内核支持，写入 `/etc/sysctl.d/90-insta-box-tool-bbr.conf`，并读回拥塞控制与默认队列参数；保存修改前的值以便恢复。默认队列设置不代表当前网卡已经替换队列，面板会分别显示。[内核参数说明](https://docs.kernel.org/networking/ip-sysctl.html#tcp-variables)

查看信息不需要 root；系统修改需要 sudo。设置 `IBT_NO_SUDO=1` 可禁止提权。卸载时可选择恢复这些设置或保留。

## 更新与卸载

运行 `ibt update` 更新工具，完成后重新输入 `ibt`。节点配置、API Key 和保活目标会保留；工具更新不会自动替换 sing-box 核心。`ibt version` 查看工具版本。

运行 `ibt uninstall`，按提示确认后移除本机组件。已在 Cloudflare 创建的云端资源不随本机卸载删除。

## 故障排查

| 现象 | 处理方法 |
| --- | --- |
| 提示找不到 `ibt` | 执行 `export PATH="$HOME/.local/bin:$PATH"`，或直接运行 `~/.local/bin/ibt` |
| 安装提示 `~/.local` 等父目录权限不安全 | 重新运行最新的一键安装命令；安装器会自动收紧当前用户拥有的安装父目录写权限。若仍失败，按提示检查具体路径的所有者、符号链接或只读挂载；不要使用 `chmod -R 777` 或 `sudo` 安装 |
| 粘贴密钥后没有字符 | 隐藏输入属于正常显示；粘贴后按回车 |
| 重启后 Quick 链接失效 | 运行 `ibt show`，重新导入当前链接 |
| Quick 尚无地址或连接失败 | 查看 `ibt logs quick` 和 `ibt diagnose`；需要时重建或切换 HTTP/2 |
| API 授权失败 | 用 `ibt key status` 检查，再用 `ibt key set` 更换有效授权 |
| 保活结果异常 | 查看 `ibt keepalive status` 和 `ibt keepalive logs`，确认目标、期限及定时器 |
| 机器已暂停或停止 | 先从控制台或独立控制端恢复，再检查暂停原因和保活状态 |

提交本 fork 的问题可使用 [Issues](https://github.com/hututu001/InstaBox-Tool/issues)。附上相关命令、工具版本和错误信息；请删除 API Key、SSH token、完整节点链接等私密内容。
