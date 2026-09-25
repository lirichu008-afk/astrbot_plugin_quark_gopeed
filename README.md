# astrbot_plugin_quark_gopeed

> 微信发送夸克网盘分享链接 → AstrBot 自动解析真实下载直链 → 调用 GoPeed 创建下载任务 → 回执结果

AstrBot 插件，版本 `v1.2.0`。

---

## 一、项目简介

在微信里把夸克网盘的分享链接（连同提取码）发给机器人，插件会自动完成：

1. 识别消息中的 `pan.quark.cn/s/xxx` 链接与提取码；
2. 用你配置的夸克 Cookie 换取访问令牌，列出分享内容，**转存到你自己的网盘**；
3. 换取带签名的下载直链；
4. 调用 GoPeed 的 HTTP API 逐个创建下载任务；
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
   ▼
[正则解析] pan.quark.cn/s/xxx + 提取码
   │
   ▼
[白名单校验] allowed_users
   │
   ▼
[夸克] sharepage/token  → stoken
       sharepage/detail → 文件列表
       sharepage/save   → 转存（必须）
       task             → 轮询直到完成
       file/download    → 下载直链（有效期短！）
   │
   ▼
[GoPeed] POST 创建下载任务（逐个文件）
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
| `gopeed_auth_header` | `Authorization` | 认证头名称 |
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

### 如何获取夸克 Cookie

1. 浏览器登录 `https://pan.quark.cn`；
2. 按 `F12` 打开开发者工具 → `Network`；
3. 随便点一个请求，在 `Request Headers` 里找到 `Cookie`；
4. 复制完整值（通常包含 `__puus`、`__pus` 等字段）；
5. 粘贴到 `quark_cookie`。

---

## 四、使用示例

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

认证头若不匹配（提示 401/403），再尝试把 `gopeed_auth_header` 改为 `X-Gopeed-Token`。

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
astrbot_plugin_quark_gopeed v1.2.0 已加载
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

### 3. 仅处理文件，不处理目录（默认）

默认只提交分享中的文件。文件夹需开启 `quark_recursive`。

### 4. 转存会占用你的夸克网盘空间

每次下载都会在你网盘里留下一份转存文件。插件**不会自动删除**。
请定期清理，或指定 `quark_save_dir_fid` 集中管理。

### 5. 风控风险

夸克对批量、高频的转存与下载有风控。请务必配置 `allowed_users` 白名单，
并控制 `max_files`，避免账号被限制。

### 6. 凭据安全

`quark_cookie` 与 `gopeed_token` 属于敏感凭据：

- 插件日志中**不会**打印它们的值；
- 但它们以明文存储在 AstrBot 配置中，请确保 AstrBot 的 WebUI 不暴露在公网。

---

## 七、文件结构

```
astrbot_plugin_quark_gopeed/
├── main.py                  # 插件主体：事件监听、链接解析、流程编排
├── quark_client.py          # 夸克网盘异步客户端（转存 → 直链）
├── gopeed_client.py         # GoPeed HTTP API 异步客户端
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置项定义（WebUI 配置界面读这个）
├── requirements.txt         # 依赖
├── config.example.yaml      # 配置参考示例（AstrBot 不直接读取）
└── README.md                # 本文件
```

---

## 八、排障速查

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 插件没反应 | 未加载 / 白名单拦截 | 查日志、检查 `allowed_users` |
| 提示 Cookie 失效 | Cookie 过期 | 重新抓取 Cookie |
| 无法连接 GoPeed | `gopeed_host` 填了 `127.0.0.1` | 改填 NAS 局域网 IP |
| 提示成功但 GoPeed 无任务 | 旧版未校验 `code` + 旧的裸 url 模板 | 升级到 v1.1.0，并修正 `gopeed_payload_template` |
| GoPeed 返回 404 | 接口路径不对 | `gopeed_api_path` 应为 `/api/v1/tasks` |
| GoPeed 拒绝任务 code=1002 | 请求体缺 `req` 包装 | 恢复 `gopeed_payload_template` 默认值 |
| GoPeed 返回 401/403 | 认证头或 token 不对 | 调整 `gopeed_auth_header` / `gopeed_token` |
| 夸克解析失败 | 接口变更或分享失效 | 查日志；确认分享在浏览器能打开 |
| 提取码报错 | 提取码缺失/错误 | 消息里带上「提取码：xxxx」 |
| 文件夹无法下载 | 默认不递归 | 开启 `quark_recursive` |
| GoPeed 里下载失败 | 直链过期 / UA 不匹配 | 尽快下载；统一 User-Agent |
