# astrbot_plugin_quark_gopeed

> ⚠️ **AI 生成声明**
>
> 本项目的**全部代码、文档与配置均由 AI 生成**，未经人工逐行审查，也未经过完整测试。
> 请在使用前自行阅读源码并评估风险；因使用本项目造成的任何直接或间接损失，由使用者自行承担。
>
> This project is entirely AI-generated, without line-by-line human review or full testing. Use at your own risk.

> 微信发送「搜索 稻香」→ 检索网盘资源 → 回复列表 → 回复「下载 1」→ 自动解析夸克直链并交给 GoPeed 下载
>
> 也可以直接发送夸克网盘分享链接（含提取码）自动下载。

AstrBot 插件，版本 `v1.3.3`。

---

## 一、项目简介

插件提供两条使用路径：

**路径 A：搜索下载（v1.3.0 新增）**

1. 发送 `搜索 稻香`；
2. 插件调用搜索站接口，返回带夸克网盘资源的结果列表；
3. 回复 `下载 1`；
4. 插件取出该结果的夸克分享链接，交给下面的下载链路。

**路径 B：直接发链接**

在微信里把夸克网盘的分享链接（连同提取码）发给机器人，插件直接开始处理。

两条路径最终都走同一套下载流程：

1. 识别 `pan.quark.cn/s/xxx` 分享链接与提取码；
2. 用你配置的夸克 Cookie 换取访问令牌，列出分享内容，**转存到你自己的网盘**；
3. 换取带签名的下载直链（并使用夸克刷新后的会话 Cookie）；
4. 调用 GoPeed 的 HTTP API 逐个创建下载任务（通过 `req.extra.header` 把所需请求头一并传给 GoPeed）；
5. 通过微信回复每个文件的提交结果。

### 为什么必须先"转存"？

夸克网盘的分享链接**无法直接下载**。必须先把文件转存到自己账号下，才能拿到 `file/download`
接口所需的 `fid` 并换取直链。这是夸克的设计，任何方案都绕不过去。

因此插件会在你的夸克网盘里产生一份转存文件。建议通过 `quark_save_dir_fid` 指定一个专用目录，
而不是让它堆在根目录。

### 工作流程图

```
微信消息
   │
   ├─「搜索 XX」─→ [搜索层] /api/music/search ─→ 结果列表 ─→ 回复用户
   │                    │                                    │
   │                    └─→ [会话缓存 SessionStore] ←────────┘
   │                                    │
   ├─「下载 N」─→ 查缓存 ─→ /api/music/<id> ─→ 夸克分享链接
   │                                              │
   └─ 夸克分享链接 ───────────────────────────────┤
                                                  ▼
                                        [下载链路]
                              sharepage/token  → stoken
                              sharepage/detail → 文件列表
                              sharepage/save   → 转存（必须）
                              task             → 轮询直到完成
                              file/download    → 下载直链（有效期短！）
                                                  │
                                                  ▼
                              [GoPeed] POST /api/v1/tasks 创建任务
                                       附带 req.extra.header（刷新后的 Cookie）
                                                  │
                                                  ▼
                                              微信回执
```

---

## 二、安装步骤

### 1. 确认前置条件

- AstrBot 已运行，且**已配置好微信适配器**（Gewechat / AstrWeChat 等）。本插件只处理文本消息事件，不关心具体适配器。
- GoPeed 已运行，并已获取接口令牌。
- 有一个可用的夸克网盘账号 Cookie。

### 2. 放置插件

把整个 `astrbot_plugin_quark_gopeed` 目录放入 AstrBot 的插件目录：

```
<AstrBot 数据目录>/data/plugins/astrbot_plugin_quark_gopeed/
```

两种方式任选：

**方式 A：WebUI 上传（推荐）**

把 `astrbot_plugin_quark_gopeed` 目录压缩成 zip，然后在
AstrBot WebUI → 插件管理 → 安装插件 → 上传 zip。

**方式 B：直接拷贝目录**

在 NAS 的文件管理器里，把 `astrbot_plugin_quark_gopeed` 整个目录拷进 `data/plugins/` 下，
然后在 WebUI 插件管理页面点「重载插件」。

> ⚠️ 注意：拷进去的必须是**目录本身**，即最终路径为
> `data/plugins/astrbot_plugin_quark_gopeed/main.py`，
> 而不是 `data/plugins/main.py`。

### 3. 安装依赖

插件依赖 `httpx`。AstrBot 通常已自带，若报 `ModuleNotFoundError: No module named 'httpx'`，
在 AstrBot 环境中执行：

```bash
pip install -r requirements.txt
```

如果是 Docker 部署的 AstrBot：

```bash
docker exec -it <astrbot容器名> pip install httpx
```

### 4. 配置插件

进入 AstrBot WebUI → 插件管理 → `astrbot_plugin_quark_gopeed` → 配置，填写下面几项。

---

## 三、配置说明

### 必填项

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `quark_cookie` | string | 空 | 夸克账号完整 Cookie。**必填** |
| `gopeed_token` | string | 空 | GoPeed 接口令牌。GoPeed 未开启 API 鉴权时留空即可（实测 1.9.3 默认未开鉴权） |
| `gopeed_host` | string | `127.0.0.1` | GoPeed 地址，见下方⚠️ |
| `gopeed_port` | int | `9999` | GoPeed HTTP API 端口 |

### GoPeed 接口形态（已按 v1.9.3 实测校准）

下表默认值均在真实 GoPeed 1.9.3 上验证过，**通常不需要改动**：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `gopeed_api_path` | `/api/v1/tasks` | 创建任务的路径。`/api/v1/download` **不存在**（会返回 404） |
| `gopeed_auth_header` | `X-Api-Token` | 认证头名称。**实测 GoPeed 1.9.3 只认这个**，`Authorization` 会 401 |
| `gopeed_auth_scheme` | `Bearer` | 认证前缀，留空则只发 token |
| `gopeed_payload_template` | `{"req": {"url": "{url}"}, "opt": {"name": "{name}", "path": "{path}"}}` | 请求体模板，支持 `{url}` `{name}` `{path}`。**不要改回裸 `{"url": "{url}"}`**，会被 GoPeed 以 `code=1002` 拒绝 |
| `gopeed_download_path` | 空 | 下载目录（GoPeed 容器内路径）。留空用 GoPeed 默认（`/app/downloads`） |
| `gopeed_forward_cookies` | `true` | **务必保持开启**。把刷新后的 Cookie 转发给 GoPeed，否则夸克 CDN 返回 412 |
| `quark_user_agent` | 空 | 下载时用的 UA，留空用内置桌面 Chrome UA |
| `gopeed_use_ssl` | `false` | 是否使用 HTTPS |
| `gopeed_timeout` | `15` | 请求超时（秒） |

> 📌 **关于 GoPeed 的错误返回方式**：GoPeed 用 **HTTP 200 承载业务错误码**。
> 例如请求体结构不对时，它返回 `HTTP 200` 且 body 为
> `{"code":1002,"msg":"param invalid: rid or req"}`。
> 因此插件必须解析响应体里的 `code` 字段才能判断成败 —— 只在早期版本中
> 遗漏了这一步，导致出现「插件提示成功、GoPeed 里却没有任何任务」的现象。
> 该问题已于 v1.1.0 修复，并有回归测试覆盖。

### 行为控制

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `allowed_users` | `[]` | 用户/群 ID 白名单，留空=允许所有人（**建议填写**） |
| `max_files` | `5` | 单次最多处理文件数 |
| `quark_recursive` | `false` | 是否递归下载文件夹内容 |
| `quark_save_dir_fid` | `0` | 转存目标目录 fid，`0`=根目录 |
| `quark_task_timeout` | `90` | 等待转存完成的超时（秒） |
| `request_timeout` | `30` | 夸克接口 HTTP 超时（秒） |
| `dedup_seconds` | `300` | 重复链接拦截窗口（秒），`0`=不拦截 |

> ⚠️ **`gopeed_host` 是最容易踩的坑**：AstrBot 和 GoPeed 如果是两个独立容器，
> 填 `127.0.0.1` 会指向 AstrBot 容器自己，**必然连不上**。
> 请填写 NAS 的局域网 IP（如 `192.168.1.100`），或确保两者在同一 Docker 网络时使用容器名。

### 搜索功能配置（v1.3.0 新增）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `search_enabled` | `true` | 是否启用搜索功能 |
| `search_sources` | `["quanpansou"]` | 启用的搜索源，留空=全部已注册源 |
| `search_max_results` | `10` | 每次最多展示几条结果 |
| `search_only_quark` | `true` | 只显示含夸克链接的结果 |
| `search_session_ttl` | `300` | 回复「下载 N」的有效期（秒） |
| `search_pick_first_quark` | `false` | 一条结果有多个夸克链接时只取第一个 |
| `search_rename_by_result` | `true` | 用「歌名 — 歌手」命名下载文件（见下方说明） |
| `search_timeout` | `20` | 搜索接口超时（秒） |
| `search_base_url` | 空 | 搜索站地址，留空用默认 `https://so.liumingye.cn` |
| `search_user_agent` | 空 | 搜索请求的 UA，留空用内置桌面 Chrome UA |
| `search_max_sessions` | `500` | 会话缓存上限 |

### 如何获取夸克 Cookie

1. 浏览器登录 `https://pan.quark.cn`；
2. 按 `F12` 打开开发者工具 → `Network`；
3. 随便点一个请求，在 `Request Headers` 里找到 `Cookie`；
4. 复制完整值（通常包含 `__puus`、`__pus` 等字段）；
5. 粘贴到 `quark_cookie`。

---

## 四、使用示例

### 方式一：搜索后下载（v1.3.0 新增）

**第 1 步 — 搜索**

```
搜索 稻香
```

机器人回复：

```
🔍 「稻香」找到 10 条夸克资源：

1. 稻香 — 周杰伦  [夸克MP3 / 夸克FLAC]
2. 稻香 — 王俊凯  [夸克MP3 / 夸克FLAC]
3. 稻香 — 杨瑾儿  [夸克MP3]
4. 稻香(Live) — 周杰伦  [夸克FLAC / 夸克MP3]
...

（已过滤 3 条无夸克链接的结果）

回复「下载 序号」开始下载，5 分钟内有效
```

支持这些触发写法：`搜索 稻香`、`搜 稻香`、`搜稻香`、`查找 周杰伦`、`搜：稻香`。

**第 2 步 — 确认下载**

```
下载 1
```

机器人回复：

```
📥 开始处理「稻香 — 周杰伦」（夸克MP3、夸克FLAC）…

✅ 已提交 2 个下载任务，文件将很快开始下载。
  • 周杰伦-稻香.mp3
  • 周杰伦-稻香.flac
```

若只想要其中一个质量，把 `search_pick_first_quark` 设为 `true`，插件就只提交第一个夸克链接。

> **关于下载文件名的说明（v1.3.3）**
>
> 网盘分享里的文件常被上传者命名成一串无意义的东西，例如：
>
> ```
> 周杰伦 - (1785291570)七里香(2).mp3      ← 网盘原名
> 6762d0f94c1b9a7f58584885833290176c0ae13f.flac   ← 或被命名成 hash
> ```
>
> 因此从**搜索**下载时，插件默认会用你选的那条结果来命名（`search_rename_by_result`）：
>
> ```
> 七里香 - 周杰伦.mp3
> ```
>
> 想保留网盘原名就把 `search_rename_by_result` 设为 `false`。
> 而**直接发夸克链接**时没有搜索结果可依据，会沿用网盘原名。

> 会话隔离：私聊按用户、群聊按「群+人」分别记住搜索结果，互相不串。
> 群里若没有待确认结果，`下载 1` 会被**静默忽略**，避免误吞正常聊天。

### 方式二：直接发夸克链接

在微信里直接发链接即可，插件会自动识别：

**带提取码（中文格式）**

```
https://pan.quark.cn/s/1a2b3c4d5e
提取码：8f2k
```

**带提取码（URL 内嵌）**

```
https://pan.quark.cn/s/1a2b3c4d5e?pwd=9x7q
```

**无提取码的公开分享**

```
帮我下一下 https://pan.quark.cn/s/1a2b3c4d5e
```

**机器人回复**

```
🔍 已收到夸克分享链接，正在解析并提交下载…

✅ 已提交 2 个下载任务，文件将很快开始下载。
  • 电影名称.mkv
  • 字幕文件.ass
```

失败时：

```
❌ 有 1 个文件提交失败：
  • 电影名称.mkv
    原因：无法连接 GoPeed（http://127.0.0.1:9999）：All connection attempts failed。
    请确认 GoPeed 已启动、端口正确，且 AstrBot 能访问到该地址
```

---

## 五、常见问题

### Q1：提示「夸克 Cookie 已失效」

**原因**：夸克 Cookie 有效期较短，通常是几天到几周。

**解决**：重新按「三、配置说明」里的步骤抓取 Cookie 并更新配置，然后重载插件。

建议把 Cookie 失效当作常态，定期检查。

### Q2：提示「无法连接 GoPeed」

按顺序排查：

1. **GoPeed 是否在运行**？在 NAS 上执行 `curl http://127.0.0.1:9999` 看是否有响应。
2. **`gopeed_host` 填对了吗**？这是最常见的原因。AstrBot 和 GoPeed 不在同一容器时，
   必须填 NAS 的局域网 IP，不能填 `127.0.0.1`。
3. **容器网络是否互通**？如果 AstrBot 是 fnOS 应用（容器），GoPeed 也是容器，
   它们默认可能不在同一 Docker 网络，用宿主 IP + 映射端口最稳。
4. **防火墙**是否放行了 9999 端口。

### Q3：提示「GoPeed 返回 404，接口路径可能不对」

说明 GoPeed 的创建任务接口不是当前配置的路径。**正确接口是 `/api/v1/tasks`**，
`/api/v1/download` 不存在。

在配置中确认：

- `gopeed_api_path` = `/api/v1/tasks`
- `gopeed_payload_template` = `{"req": {"url": "{url}"}, "opt": {"name": "{name}", "path": "{path}"}}`

认证头若不匹配（提示 401/403），见下一节。

### Q3.4：提示「GoPeed 认证失败（HTTP 401）」

**实测 GoPeed 1.9.3 的 API 鉴权只认 `X-Api-Token` 头，且值为裸 token。**

下面的写法全部返回 401（已在真实实例上逐一验证）：

| 写法 | 结果 |
|---|---|
| `X-Api-Token: <token>` | ✅ 200 |
| `Authorization: Bearer <token>` | ❌ 401 |
| `Authorization: <token>` | ❌ 401 |
| `X-Gopeed-Token: <token>` | ❌ 401 |
| `X-Token: <token>` | ❌ 401 |
| `?token=<token>` | ❌ 401 |

正确配置：

```
gopeed_token        = <你的 token>
gopeed_auth_header  = X-Api-Token
gopeed_auth_scheme  = （留空）
```

token 从哪里来：GoPeed 容器的环境变量 `GOPEED_API_TOKEN`（docker-compose 里设置）。
在 NAS 上执行 `docker inspect gopeed --format '{{range .Config.Env}}{{println .}}{{end}}'` 即可看到。

> v1.3.1 起，插件默认值已改为 `X-Api-Token` + 空 scheme。
> 升级后若仍是 401，请检查配置里是否残留旧的 `Authorization` / `Bearer`。

### Q3.5：微信提示「✅ 已提交下载任务」，但 GoPeed 里什么都没有

**这是 v1.0.0 的 bug，v1.1.0 已修复。**

原因是 GoPeed **用 HTTP 200 承载业务错误码**。当请求体结构不对时，它返回：

```
HTTP 200
{"code":1002,"msg":"param invalid: rid or req","data":null}
```

v1.0.0 只检查了 HTTP 状态码，把 200 一律当作成功，于是把 GoPeed 的拒绝
误报成了成功。

修复内容（v1.1.0）：

1. `gopeed_api_path` 默认改为 `/api/v1/tasks`；
2. `gopeed_payload_template` 默认改为带 `req` / `opt` 包装的结构；
3. `create_task` 现在会解析响应体，`code != 0` 一律判为失败并回报原因。

如果你正遇到这个现象，请确认插件版本为 v1.1.0 以上，并检查配置里
`gopeed_payload_template` **不是**旧的 `{"url": "{url}"}` ——
升级时已保存的配置不会自动迁移，需要手动改或删掉该项让它回落到默认值。

### Q3.6：任务在 GoPeed 里显示 `error`，且无法下载（夸克返回 412）

**这是 v1.1.0 的 bug，v1.2.0 已修复。**

现象：微信提示提交成功，GoPeed 里任务也出现了，但状态是 `error`。
此时用 curl / 浏览器插件等任何方式直接访问那条直链，都会得到：

```
HTTP 412 Precondition Failed
```

**根因**：夸克在**每次 API 调用**时都会通过 `Set-Cookie` 下发新的会话令牌
`__puus`。而下载直链**必须携带这个刷新后的 `__puus`**，否则 CDN 一律返回 412。

v1.1.0 把 Cookie 写死在 HTTP 请求头里，httpx 虽然收到了 `Set-Cookie`，
却永远不会把它用到后续请求上 —— 于是下载时用的始终是配置里那份**过期会话**。

**修复内容（v1.2.0）**：

1. Cookie 改由 httpx 的 cookie jar 管理，自动接收并使用服务端刷新的值；
2. 新增 `export_cookies()`，在会话结束前导出**刷新后**的 Cookie；
3. 通过 GoPeed 的 `req.extra.header` 把这些 Cookie（连同 `Referer`、`User-Agent`）
   随下载任务一起传过去；
4. 新增 `gopeed_forward_cookies` 配置项（默认开启）。

> ⚠️ **安全提示**：开启 `gopeed_forward_cookies` 后，你的夸克 Cookie 会随任务
> 保存在 GoPeed 的数据库里（`gopeed.db`）。这是让 GoPeed 能下载夸克直链的必要代价。
> 如果你的 GoPeed 暴露在公网，请务必设好访问令牌。

**验证结果**：修复后在 GoPeed 1.9.3 上实测，夸克直链可被完整下载
（39MB 音频文件，状态 `done`），任务名也正确显示为真实文件名。

### Q3.7：微信提示已提交，GoPeed 里任务也在，但**一直不开始下载**

**先看 GoPeed 的任务状态**：如果状态是 `error`、进度始终 0，八成是
**GoPeed 容器的 DNS 失效** —— 它解析不了夸克 CDN 的域名。

**这是 Docker 的一个经典坑，与插件无关**：

> Docker 只在**创建容器时**读取宿主的 `/etc/resolv.conf` 并写进容器。
> 之后你改了 NAS 的网段或 DNS，**已存在的容器不会跟着更新**，
> 于是一直用一个早就失效的 DNS 地址。

**怎么确认**：

```bash
cat /etc/resolv.conf                        # 宿主当前用的 DNS
docker exec gopeed cat /etc/resolv.conf     # 容器里实际用的 DNS
docker exec gopeed nslookup dl-pc-zb.drive.quark.cn   # 是否解析得出
```

GoPeed 的日志里会有非常明确的证据：

```bash
cat /vol1/1000/docker/gopeed/storage/logs/core.log | tail -5
```

```
task download failed ... error="... dial tcp:
  lookup dl-pc-zb.drive.quark.cn on 192.168.1.1:53: read udp ...: i/o timeout"
                                               ^^^^^^^^^^^^^ 旧网段的 DNS
```

**修复（临时，立即生效）**：

```bash
docker exec gopeed sh -c 'printf "nameserver 192.168.218.110\nnameserver 223.5.5.5\n" > /etc/resolv.conf'
```

> 把 `192.168.218.110` 换成你自己 `cat /etc/resolv.conf` 里看到的地址。
> 容器里这个文件一旦被手动改过，Docker 就不会再覆盖它，所以**容器重启也会保留**。

**修复（永久）**：在 fnOS 的 Docker 管理界面里，给 gopeed 容器指定 DNS
（或删除容器后重新创建，让它重新读取宿主当前的 `resolv.conf`）。

**同一个坑会影响所有老容器**，可以用这条命令批量体检：

```bash
for c in $(docker ps --format '{{.Names}}'); do
  printf '%-20s %s\n' "$c" "$(docker exec "$c" cat /etc/resolv.conf 2>/dev/null | grep -i nameserver | head -1)"
done
```

凡是显示旧网段地址（如 `192.168.1.1`）的，DNS 都是坏的 —— 本次排查中就发现
`gopeed`、`iptvs-app2`、`frpc` 三个容器都中招。

### Q4：提示「提取码错误或缺失」

夸克的错误提示本身不够明确。请确保消息里带上了正确提取码，例如：

```
链接：https://pan.quark.cn/s/xxx
提取码：1234
```

支持的关键字：`提取码`、`访问码`、`提取密码`、`密码`、`pwd`，中英文冒号均可。

### Q5：分享是文件夹，提示无法下载

默认情况下插件只处理**文件**，遇到文件夹会跳过。

- 如果文件夹里内容不多，开启 `quark_recursive` 即可递归展开（最大递归 3 层）。
- 如果文件夹很大，建议手动转存后自行下载，避免一次创建过多任务触发风控。

### Q6：任务创建成功了，但 GoPeed 里下载失败

插件只负责把**直链**交给 GoPeed。夸克直链有两个特点：

1. **有效期很短**（通常几分钟到几小时），过期后 GoPeed 会下载失败；
2. **绑定 Cookie 与 UA**，GoPeed 若使用不同的 UA 访问可能被拒。

如果经常失败，建议在 GoPeed 中设置与插件一致的 `User-Agent`，或改用支持夸克的
专门下载方案。

### Q7：会出现重复任务吗？

插件有 `dedup_seconds` 去重窗口（默认 300 秒），同一分享链接重复发送会被拦截。
但这只防误发，不代表任务级幂等 —— GoPeed 侧不会自动去重。

### Q8：怎么确认插件加载成功了？

看 AstrBot 日志，应该出现：

```
astrbot_plugin_quark_gopeed v1.3.3 已加载（搜索源：quanpansou）
```

如果没出现，检查：

- 目录层级是否正确（`data/plugins/astrbot_plugin_quark_gopeed/main.py`）；
- 是否缺少 `httpx`；
- 是否有 Python 语法/缩进错误（日志里会有 traceback）。

---

## 六、已知限制与免责声明

请在部署前了解以下几点，它们影响可用性与维护成本：

### 1. 夸克接口为逆向实现，可能失效

夸克网盘**没有公开的官方 API**。`quark_client.py` 中的所有端点、参数、错误码语义
均来自社区逆向实践，**未经官方文档确认**，可能随夸克前端更新而失效。

失效时的表现是解析报错。修复方法：集中修改 `quark_client.py` 顶部的 `EP_*` 常量。

### 2. GoPeed 接口形态（已于 v1.1.0 实测确认）

需求文档给出的 `POST /api/v1/download` 经实测**不存在**，会返回 404。
真实接口为 `POST /api/v1/tasks`，请求体需要 `req` / `opt` 包装：

```json
{
  "req": { "url": "下载直链" },
  "opt": { "name": "文件名", "path": "下载目录" }
}
```

以上结论来自在 GoPeed **1.9.3** 上的实际探测与端到端验证，默认值已据此校准。
若你使用其他版本，接口路径、认证头、请求体结构仍可在配置中调整。

> 注：GoPeed 用 HTTP 200 承载业务错误码，`code != 0` 即为失败。
> 插件已按此解析（v1.1.0 起）。

### 3. 搜索依赖第三方站点，站点改版会失效（v1.3.0 新增）

搜索功能依赖 `so.liumingye.cn`（全盘搜）的接口，该接口为实测所得，**非官方承诺**：

```
搜索  GET /api/music/search?q=<关键词>&pageSize=<n>
详情  GET /api/music/<id>
```

若站点改版或换域名：

- **换域名** → 改配置 `search_base_url` 即可，无需改代码；
- **接口结构变更** → 只需修 `search_quanpansou.py` 一个文件，主流程不受影响。

另外该站的搜索是**模糊匹配**：输入毫不相关的词也会返回一堆结果，
插件不会（也无法）判断结果是否真的匹配你的关键词，需要你自己看标题挑选。

### 4. 仅处理文件，不处理目录（默认）

默认只提交分享中的文件。文件夹需开启 `quark_recursive`。

### 5. 转存会占用你的夸克网盘空间

每次下载都会在你网盘里留下一份转存文件。插件**不会自动删除**。
请定期清理，或指定 `quark_save_dir_fid` 集中管理。

### 6. 风控风险

夸克对批量、高频的转存与下载有风控。请务必配置 `allowed_users` 白名单，
并控制 `max_files`，避免账号被限制。

### 7. 内容来源合规

搜索功能检索的是第三方站点聚合的公开网盘分享。这类资源**多为未经授权传播的版权内容**。
下载行为发生在你自己的设备与账号上，请自行判断使用范围与合法性，插件不对内容来源作任何背书。

### 8. 凭据安全

`quark_cookie` 与 `gopeed_token` 属于敏感凭据：

- 插件日志中**不会**打印它们的值；
- 但它们以明文存储在 AstrBot 配置中，请确保 AstrBot 的 WebUI 不暴露在公网。

---

## 七、文件结构

```
astrbot_plugin_quark_gopeed/
├── main.py                  # 插件主体：事件监听、指令路由、流程编排
├── quark_client.py          # 夸克网盘异步客户端（转存 → 直链）
├── gopeed_client.py         # GoPeed HTTP API 异步客户端
├── search_base.py           # 搜索层数据结构与抽象接口（SearchResult / SearchSource）
├── search_engine.py         # 搜索编排：多源聚合、去重、过滤、截断
├── search_quanpansou.py     # 「全盘搜」搜索源适配器
├── session.py               # 会话状态：待确认的搜索结果（按会话隔离 + TTL）
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置项定义（WebUI 配置界面读这个）
├── requirements.txt         # 依赖
├── config.example.yaml      # 配置参考示例（AstrBot 不直接读取）
└── README.md                # 本文件
```

### 如何新增一个搜索站

搜索层做了适配器抽象，加站点只需三步，**主流程一行都不用改**：

1. 新建 `search_<站点名>.py`，继承 `SearchSource`，实现两个方法：

   ```python
   from search_base import DownloadLink, SearchResult, SearchSource

   class MySiteSource(SearchSource):
       name = "mysite"
       display_name = "我的站点"

       async def search(self, keyword: str, limit: int = 10) -> list[SearchResult]:
           ...  # 返回 SearchResult 列表
       async def get_downloads(self, detail_id: str) -> list[DownloadLink]:
           ...  # 返回 DownloadLink 列表
   ```

2. 在 `search_engine.py` 的 `SOURCE_REGISTRY` 里注册：

   ```python
   SOURCE_REGISTRY = {
       QuanPanSouSource.name: QuanPanSouSource,
       MySiteSource.name: MySiteSource,     # 新增
   }
   ```

3. 在插件配置 `search_sources` 里加上 `mysite`。

`SearchResult` 只需要填 `detail_id`、`title`、`artist`、`quality` 几个字段；
`quality` 里带「夸克」字样就会被识别为夸克资源（例如 `夸克MP3`、`夸克FLAC`）。

---

## 八、排障速查

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 插件没反应 | 未加载 / 白名单拦截 | 查日志、检查 `allowed_users` |
| 搜索没反应 | `search_enabled` 关闭或指令写法不对 | 用「搜索 XX」；检查配置 |
| 搜索报错「返回的不是 JSON」 | 搜索站改版 | 看日志；必要时改 `search_base_url` |
| 搜索全是百度云等非夸克 | 站点结果本身如此 | 保持 `search_only_quark` 开启，换个关键词 |
| 「下载 N」没反应 | 搜索结果已过期 / 群里无会话 | 重新搜索；有效期默认 5 分钟 |
| 序号超出范围 | 列出的条数少于你回复的序号 | 看回复里的条数 |
| 提示 Cookie 失效 | Cookie 过期 | 重新抓取 Cookie |
| 无法连接 GoPeed | `gopeed_host` 填了 `127.0.0.1` | 改填 NAS 局域网 IP |
| 提示成功但 GoPeed 无任务 | 旧版未校验 `code` + 旧的裸 url 模板 | 升级到 v1.1.0+，并修正 `gopeed_payload_template` |
| GoPeed 返回 404 | 接口路径不对 | `gopeed_api_path` 应为 `/api/v1/tasks` |
| GoPeed 拒绝任务 code=1002 | 请求体缺 `req` 包装 | 恢复 `gopeed_payload_template` 默认值 |
| GoPeed 返回 401/403 | 认证头或 token 不对 | 调整 `gopeed_auth_header` / `gopeed_token` |
| GoPeed 里任务报 error | 夸克 CDN 返回 412（Cookie 未刷新） | 升级到 v1.2.0+，并保持 `gopeed_forward_cookies` 开启 |
| 任务在但一直不下载、进度为 0 | **GoPeed 容器 DNS 失效** | 见 Q3.7：`docker exec gopeed cat /etc/resolv.conf` |
| 下载文件名是一串字符 | 网盘原名就是 hash 或无意义数字 | 保持 `search_rename_by_result` 开启（v1.3.3+） |
| 夸克解析失败 | 接口变更或分享失效 | 查日志；确认分享在浏览器能打开 |
| 提取码报错 | 提取码缺失/错误 | 消息里带上「提取码：xxxx」 |
| 文件夹无法下载 | 默认不递归 | 开启 `quark_recursive` |
| GoPeed 里下载失败 | 直链过期 / UA 不匹配 | 尽快下载；统一 User-Agent |
