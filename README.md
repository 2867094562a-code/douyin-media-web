# 拾影 · 抖音媒体整理器

一个简洁的 HTML/CSS/JavaScript 网站，连接独立的 FastAPI + Playwright 后端，整理你有权使用的公开抖音作品。

**GitHub Pages 只托管静态前端；未连接后端时不会解析，也不会显示虚假的下载成功。服务器不落盘保存图片或视频。**

## 能做什么

- 批量粘贴短链、视频链接、图文链接或完整分享文案。
- 精确匹配作品 ID，跳过推荐、评论与用户列表，拒绝跳转到另一个作品。
- 展示作者、标题/文案、封面、图片和普通视频预览。
- 按页面实际提供的顺序优先选择原图和普通播放资源；兼容新版 camelCase 与流式页面状态。
- 打开和复制源直链；将图片、MP4、封面保存到用户本地设备。
- 复制文案，保存 JSON 元数据；支持单视频/单张图及整篇图文（如13张图）分别串行保存到浏览器本地。
- 展示真实的排队、解析、部分失败、传输字节与错误原因。未知总大小时不伪造百分比。

只读取页面正常提供的资源。不破解签名、不自动登录、不解决验证码、不绕过 DRM；不保证移除源文件已嵌入的水印。源站可能随地区、网络、访问权限或页面变化拒绝请求，直链也可能过期。遇到验证时应正常访问作品确认权限，不能靠本项目绕过。

## 数据如何流动与下载模式

```text
浏览器（GitHub Pages） → 独立 Python 后端 → 抖音公开页面
                           ↓ 仅内存中的作品元数据
浏览器 ← 作者/文案/源直链/任务状态
   ├─ 原图直连保存（优先） → 浏览器直接从抖音 CDN fetch 保存（服务端 0 转发流量）
   └─ 兼容流式中继 → 后端有界分块读取 CDN → 浏览器流式保存到本机（解决 CORS 跨域限制）
```

- **原图直链优先（源站直连）**：图片直链经实测支持 CORS 跨域访问。前端下载图片时优先由浏览器直接从源站获取并保存，完全不消耗服务端带宽；界面明确标记为“已交给浏览器保存（源站直连）”。客户端直连同样受请求超时、Content-Length 预检、分块累加限流与文件头魔数嗅探保护，超限直接终止，绝不擅自重试。
- **视频与受限资源兼容中继**：视频播放直链因抖音源站未配置 CORS 头，会被浏览器安全策略拦截；图片直连若遇网络异常也会平滑降级。此时走后端的流式兼容中继（`/api/downloads`），零磁盘写入，边中继边传给浏览器。界面明确标记为“已交给浏览器保存”。因此不承诺“全媒体流量完全不经过服务端”，视频保存仍会消耗后端带宽。
- 浏览器下载管理器决定最终落盘位置；“已交给浏览器保存”不等于已确认操作系统底层写入完成。

后端仅用内存保存任务、直链引用和进度，默认 30 分钟过期。进程重启会丢失任务；正在执行的解析/流受总时限约束，结束后按 TTL 回收。下载入口为短期、随机、单次使用的访问能力，不在其中放 API_KEY。没有媒体存储目录，也不需要挂载媒体卷。Chromium 使用新的临时上下文，关闭时销毁；不读取用户既有浏览器配置，解析时阻止图片/媒体下载并关闭 HTTP 缓存。

## 默认后端配置（frontend/config.json）

前端根目录预留了 `frontend/config.json`，用于静态网站初次加载时读取默认后端地址：

```json
{
  "apiBaseUrl": "https://douyin-media-web-api.onrender.com"
}
```

- 采用相对路径 `./config.json` 读取，天然兼容 GitHub Pages 的仓库子路径（如 `https://user.github.io/repo/`）。
- **优先级**：用户在页面“设置”弹窗中手动保存的地址（存入浏览器 `localStorage`）最高，其次为 `config.json` 中的 `apiBaseUrl`。
- **严禁写入密钥**：`config.json` 仅用于非敏感的公开后端根地址，**绝对不能**在其中写入 `API_KEY`。若后端启用了访问密钥，用户在设置弹窗中输入，仅保存在当前会话内存中，绝不写入代码或公开配置。
- **未就绪提示**：若默认后端尚未部署、处于休眠唤醒中或网络未连通，前端界面会保持“未连接后端”并给出明确提示，不会伪装连接成功。

## 本地运行

需要 Python **3.11 或更高版本**；不需要 PyQt5 或 Node 前端构建工具。

```sh
python3 -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 改用：.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
# Linux 服务器首次安装浏览器系统依赖：
# python -m playwright install --with-deps chromium
cp .env.example .env
```

Windows 可在资源管理器复制 `.env.example` 并重命名为 `.env`。然后在**项目根目录**运行：

```sh
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

另开一个终端，在项目根目录启动静态前端：

```sh
python -m http.server 8080 --bind 127.0.0.1 --directory frontend
```

打开 `http://localhost:8080`，点击右上角“未连接后端”，填写 `http://127.0.0.1:8000`，测试并连接。粘贴作品分享内容，点“解析作品”。

如果本机已经安装 Chrome，可在 `.env` 设置 `CHROMIUM_CHANNEL=chrome`；Docker 中保持未设置，使用已安装的 Chromium。

### 本机 TUN / Fake-IP 网络

后端始终拒绝 `198.18.0.0/15` 等非公网地址。如果系统 DNS 返回 TUN Fake-IP，可在 `.env` 选择 `DNS_RESOLVER=cloudflare` 或 `DNS_RESOLVER=alidns`，改用固定、校验证书的可信 HTTPS DNS，再固定连接到核验过的公网 IP。不会修改系统 DNS/代理，也没有“允许私网”的开关。DNS 域名查询会发给所选解析提供商；网络无法访问时会明确失败。默认 `system` 适合普通公网 DNS 部署。

## Docker 后端

```sh
cp .env.example .env
# 编辑 .env，填入精确 CORS_ORIGINS，并建议配置随机 API_KEY
# Docker 内不要设置 CHROMIUM_CHANNEL=chrome
docker compose up --build -d
docker compose logs --tail=50 api
```

Compose 默认仅监听本机 `127.0.0.1:8000`；公网部署在受信 HTTPS 反向代理后使用。镜像非 root 运行，已安装 Playwright Chromium；没有媒体存储卷。

单独构建与运行：

```sh
docker build -t shiying-api .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 --shm-size=256m shiying-api
```

健康接口：`http://127.0.0.1:8000/api/health`；API 文档：`http://127.0.0.1:8000/docs`。健康接口代表 API 已启动，不代表任意抖音作品一定可以访问。

## 上传 GitHub 并开启 Pages

将本项目目录内容作为仓库根目录上传，保留隐藏的 `.github/` 和 `.gitignore`。不要上传 `.env`、虚拟环境、测试媒体、真实分享链接或浏览器资料。

1. 在 GitHub 新建空的公开仓库，例如 `shiying`。
2. 在本项目根目录执行（替换账号与仓库名）：

   ```sh
   git init -b main
   git add .
   git commit -m "Initial open-source release"
   git remote add origin https://github.com/YOUR_ACCOUNT/shiying.git
   git push -u origin main
   ```

3. 仓库 **Settings → Pages → Build and deployment → Source → GitHub Actions**。
4. 在 Actions 运行 `Deploy static frontend to GitHub Pages`。首次推送通常已触发；如果当时尚未开启 Pages，可手动点 **Run workflow** 重跑。
5. 部署完成后访问 `https://YOUR_ACCOUNT.github.io/shiying/`。前端资源全为相对路径，支持仓库子路径。
6. 后端 `.env` 设置：

   ```dotenv
   CORS_ORIGINS=["https://YOUR_ACCOUNT.github.io"]
   ```

   Origin **不带 `/shiying/` 路径，不带末尾斜杠，不使用 `*`**。重启后端，然后在网页中填写独立后端的 HTTPS 地址和可选 API_KEY。

`pages.yml` 仅上传 `frontend/`。`ci.yml` 运行 Python 测试、浏览器安装、前端 JS 语法检查和 Docker 构建。首次上传后需要检查云端 Actions 的实际结果；本地文件存在不代表已经发布。

官方说明：[配置 GitHub Pages 发布来源](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)。

## 免费部署方案（2026-11-17 核实）

### 个人试用：Pages + 自己电脑 + Cloudflare Quick Tunnel

无需购买云服务器或域名，电脑需要在线。本机后端的依赖、浏览器和网络由自己的设备承担。

1. 按前文启动本机后端，把 Pages origin 加入 CORS，并配置 API_KEY。
2. 从官方渠道安装 `cloudflared`，运行：

   ```sh
   cloudflared tunnel --url http://localhost:8000
   ```

3. 把输出的 `https://…trycloudflare.com` 地址填入前端。停止进程后入口失效，每次重启域名可能变化。

这是测试开发方案，无可用性保证，不自动开启本机的公网入口。Quick Tunnel 无需账户或域名、最多 200 个并行请求、不支持 SSE；本项目用轮询展示进度。

官方说明：[Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

### 云端试用：Pages + Render Free Web Service

仓库附 `render.yaml`，或手动创建 **Web Service → GitHub 仓库 → Docker → Free**。设置精确 `CORS_ORIGINS`，并从 Render 环境变量中取得/配置 API_KEY，然后填入前端。镜像使用 Render 的 `PORT` 环境变量。

> [!WARNING]
> **免费云平台解析限制与实测结果（2026-11-17）**：
> 1. **云端实样未通过**：本次 Render（Singapore 节点）对两个授权样例的分享短链与标准作品链接均返回 HTTP 200，但页面标题为空、内嵌状态数为 0、HTML 中不含目标作品 ID，因此**云端实样尚未通过，根因未确证**；而在本机使用真实网络实测均能成功解析。开源版本遵循安全边界，**绝不包含验证码破解或反爬逆向**。若需高可用稳定解析，建议在本地环境自建运行。
> 2. **实例资源上限**：Render Free 每个 Workspace 每月提供 750 小时共享免费实例时长，512MB 内存上限（运行 Playwright Chromium 时切忌多并发，保持并发=1），15 分钟无外部请求会自动休眠，首次访问唤醒约需 1 分钟。
> 3. **配额与费用边界**：未设置付款方式或到达支出上限时暂停/停止构建，已设置付款方式时可能追加收费；运行实例保持 Free，不升级，不启用保活。

官方说明：[Render 免费部署限制](https://render.com/docs/free)。

Hugging Face 新建 Docker/Gradio 计算型 Space 当前需要付费计划，不能仅因 CPU Basic 无小时费而称其为新用户完全免费方案：[Spaces 官方说明](https://huggingface.co/docs/hub/spaces-overview)。

## 配置与安全边界

完整默认值在 `backend/app/config.py`，示例在 `.env.example`。

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `API_KEY` | 空 | 可选 Bearer 访问密钥；公开部署建议设置 |
| `MAX_BATCH_URLS` | 10 | 每次批量链接数 |
| `MAX_CONCURRENT_PARSES` | 1 | 同时运行的浏览器解析任务 |
| `MAX_CONCURRENT_DOWNLOADS` | 1 | 同时传输任务 |
| `MAX_IN_MEMORY_JOBS` | 30 | 内存任务总数上限 |
| `MAX_SINGLE_FILE_BYTES` | 150 MiB | 单个媒体文件上限 |
| `MAX_TASK_TOTAL_BYTES` | 400 MiB | 一次传输的媒体总量上限 |
| `JOB_TTL_SECONDS` | 1800 | 内存元数据保留时间 |
| `PAGE_LOAD_TIMEOUT_SECONDS` | 40 | 页面加载时限，一次普通重试有额外上限 |
| `STREAM_TIMEOUT_SECONDS` | 300 | 等待和传输总时限 |

- URL 仅允许 HTTPS、标准 443 端口、无用户信息且在域边界白名单内。禁止任意 URL 代理；下载接口只接受已经解析过的作品/资源编号。
- DNS 在实际连接路径核验所有答案。拒绝回环、私网、云元数据、保留地址、IPv4 映射与危险 IPv6 过渡地址；固定实际连接 IP，保留 TLS SNI 与证书验证，防 DNS 重绑定。每次重定向仍受检查。
- Chromium 通过仅绑定回环、随机认证的 CONNECT 通道访问受信公网域名；禁用 Service Worker、WebSocket、QUIC 与非代理 WebRTC 出站。没有使用 `route.continue_` 加一次 DNS 预检这种存在竞态的单独防护。
- 解析阶段限总网络字节、嵌入 JSON 大小、状态数量、遍历深度/节点数、作品资源数及元数据量。超出上限明确失败。
- 文件按分块读取并检查文件头、声明长度和实际字节数。返回 HTML、JSON、HLS 或不支持的媒体时明确报错；中途失败将终止传输，任务不会标为完成。请丢弃浏览器留下的不完整文件。
- 接口按连接 IP 限流，内存限流表有界且过期回收。不信任客户端伪造的 `X-Forwarded-For`；反向代理后可能按共享连接 IP 限流。多人生产部署应在可信网关进一步配置限流。
- 不导入或返回 Cookie、登录 Token；不记录请求正文、带签名媒体查询串或下载入口。默认关闭 Uvicorn access log，反向代理也应避免记录这些路径及签名。用户明确需要的 CDN 签名直链只在解析结果中显示，API_KEY 不会进入链接或本地存储。
- 前端使用 `textContent` 展示作品文案，限制 URL scheme、设置 CSP/no-referrer，避免将作品内容作为 HTML 执行。
- 采用 **单进程 `--workers 1`**。多进程/多副本会有不同的内存任务，不受支持；若要扩展，先引入共享的、有鉴权的元数据任务存储，仍保持媒体不落盘。

## 测试与维护

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --check frontend/app.js
```

自动测试使用匿名合成数据与受控网络替身，不依赖真实抖音链接、Cookie 或本机路径，不把夹具成功描述为真实作品下载成功。实际作品应另行从网页进行完整解析和下载验证。

```text
frontend/                  无构建静态页面
backend/app/extraction.py  从桌面版迁移的纯解析逻辑
backend/app/browser.py     公开页面解析与精确作品匹配
backend/app/proxy.py       Chromium 专用安全 CONNECT 通道
backend/app/security.py    出站连接、DNS、URL、限流
backend/app/media.py       原始字节有界流式中继与格式嗅探
backend/app/main.py        API、内存任务、TTL、鉴权
backend/app/config.py      环境配置与边界校验
tests/                     解析、安全、网络、API 与下载测试
.github/workflows/         Pages 发布与 CI
```

解析器由用户提供的《抖音媒体下载器_V2.py》迁移，保留作品 ID 精确匹配、推荐节点过滤、原图候选和播放资源排序；新增流式页面状态、camelCase 兼容、冲突 ID 拒绝及异常字段保护。删除 PyQt5 UI、持久用户 profile、用户 Cookie 读取和客户端任意代理设置。

依赖版本固定在 requirements 文件中，升级后重新跑测试，并用已授权的真实视频/图文确认页面结构是否变化。本项目 MIT 许可证只覆盖代码，不授予第三方媒体的使用权；依赖软件仍遵循各自许可证。
