# 个人自用原型：本地服务与 ChatGPT 分开验收

本阶段复用 `/home/fqtuser/Video_ingest` 所在的 Linux 开发环境，不购买服务器或域名，
不开发多人服务。当前代码使用 Linux 的进程锁与资源限制；已有环境即可进行本地开发和测试，
无需另建 Linux 云服务器。

部署前记录实际账号的套餐、工作区、开发者模式入口、Tunnel 权限和测试模型。
个人自用不等于个人工作区；开发者模式不构成远端可达或图片读取通过的证据。

## 第一步：服务确实返回文字和图片

在现有工作目录执行：

```bash
cd /home/fqtuser/Video_ingest
.venv/bin/python scripts/check_probe.py
```

检查器启动临时 MCP 服务，分别通过 stdio 和本机 HTTP 调用 `probe_text`、`probe_images`，
检查真实文字、三个可解码的 PNG 图片内容块及其时间戳关联。图片中的随机数字仅存在于像素中，
不写入工具的文字说明或结构化元数据。检查结果保存在
[`docs/runs/prototype-local-verification.json`](runs/prototype-local-verification.json)。

这一步验收的是服务运行和本地协议传输。脚本解码图片成功，不能证明网页版 ChatGPT 的模型读到了图片。
随机探针独立于视频平台，排查时不会把平台下载问题与模型读取问题混在一起。

## 第二步：真实账号的网页版 ChatGPT 确实读取

先核对实际账号、工作区、开发者模式、创建页的连接方式以及所选模型。
按该账号实际支持的连接方式配置，再执行以下测试。现阶段不预设账号具备 Tunnel 权限，
不把购买服务器或域名作为连接前提。

如果确认的连接方式需要本机 HTTP 探针，可在工作目录运行：

```bash
.venv/bin/python -m server.probe --transport streamable-http --port 8766
```

探针地址为本机 `http://127.0.0.1:8766/mcp`，服务默认仅监听回环地址。
这个地址可供本机客户端或获准的连接桥接使用，不能直接证明 ChatGPT 远端能够访问。
探针自身没有 HTTP 身份认证，因此保持本机监听；实际连接方案必须保持私有访问。
如果账号支持的方式采用 stdio，对应命令为 `.venv/bin/python -m server.probe`，
工作目录必须是本仓库。两种启动方式择一，不要求同时运行。

若客户端不能指定工作目录，使用绝对路径启动器：
`/home/fqtuser/Video_ingest/.venv/bin/python /home/fqtuser/Video_ingest/scripts/run_probe.py`。
启动器也接受 `--transport`、`--port` 和 `--data-dir`；使用自定义数据目录时，记录器必须传入相同的 `--data-dir`。

### 私有连接候选：Secure MCP Tunnel

2026-09-22 核对的[官方文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
支持通过出站连接转发本地 stdio MCP，无需开放公网端口。使用前需确认账号有开发者入口、
Tunnel 权限及目标工作区关联，并在运行环境私下配置运行密钥；不要把密钥发到聊天或写入仓库。
仓库不会自动安装客户端、创建隧道或启动连接。

账号具备条件且已按官方说明安装客户端后，在仓库根目录执行：

```bash
.venv/bin/python scripts/configure_tunnel.py --tunnel-id tunnel_YOUR_ID --target probe
```

如客户端不在 PATH，追加 `--client /absolute/path/to/tunnel-client`。
脚本仅在忽略目录 `.runtime/tunnel/probe/` 创建本机配置，不保存密钥、不连接远端。
私下设置 `CONTROL_PLANE_API_KEY` 后执行脚本打印的 `doctor` 和 `run` 命令。

再在 ChatGPT 创建开发者应用时选择 Tunnel 并选择对应隧道。以上为候选配置，尚未实测；
若账号没有该入口，应记录实际可选方式，再选择支持其认证的私有 HTTPS 网关。
先只连接独立探针，通过后再连接视频后端。

连接完成后，在**网页版 ChatGPT 的实际会话**中分别发送：

> 请调用一次 `probe_text`。保留返回的 `probe_id`，逐字复述工具实际返回的随机文字，
> 并输出答案 JSON：`{"text":"你读到的随机文字"}`。如果调用失败，请报告实际错误。

> 请调用一次 `probe_images`。保留返回的 `probe_id`，直接读取三张图片像素里的六位数字，
> 按图片与元数据的关联列出各自的 `timestamp_ms`。
> 按工具图片顺序输出答案 JSON：`{"digits":["第一张数字","第二张数字","第三张数字"],"timestamps_ms":[第一张时间戳,第二张时间戳,第三张时间戳]}`。
> 如果没有收到图片或无法辨认，请明确说明，不要猜测。

不要把本地 `private_ground_truth.json` 的答案粘贴给模型，也不要把本地图片手动上传到会话
来替代 MCP 图片传递。每次测试使用新探针，防止旧答案影响结果。
文字与图片分别保留工具调用及模型回答证据；图片通过要求三个数字和三个时间戳全部对应正确。

### 保存并核对真实会话结果

在 `.runtime/host-validation/` 保存原始会话截图或文本、模型给出的答案 JSON。
该目录不进入 Git；保存内容时去除账号邮箱、凭据和其他个人信息。
答案 JSON 只使用上述指定字段，不包含 `probe_id` 或解释文字。两次探针分别记录，
并在各自生成后的 **30 分钟内**完成核对：

```bash
mkdir -p .runtime/host-validation
# 先保存真实会话证据和答案；下面的占位值必须替换为实际值。
.venv/bin/python scripts/record_host_probe.py \
  --probe-id probe_REPLACE_WITH_ACTUAL_ID \
  --connection secure-mcp-tunnel \
  --host 'ChatGPT web' --host-version '实际版本或测试日期' \
  --model '实际模型' --account-mode '实际套餐及工作区类型，不含账号标识' \
  --evidence .runtime/host-validation/captured-response.txt \
  --answer-json .runtime/host-validation/answer.json
```

`--connection` 必须填写实际方式：`secure-mcp-tunnel`、`private-https-gateway`、
`local-stdio` 或 `local-http`；后两者不能作为网页版验收。

记录器将实际回答与私有真值比较，并写入 `docs/runs/host/<probe_id>.json`，包含答案、
客户端信息和证据校验值。该报告目录会进入 Git，提交前只保留适合公开的测试信息；
原始截图和会话仍留在忽略目录。记录器无法独立证明回答来自所填客户端，因此必须保留真实会话证据。
只有观察到具体不支持行为时才使用 `--unsupported --reason '实际观察到的限制'`；
入口尚未确认或尚未建立连接，状态应为待验证。

## 障碍按证据归类

| 类别 | 当前判断与下一步 |
|---|---|
| 账号权限 | 按实际账号核对开发者模式与 Tunnel 权限；未连接时不能判定模型读取是否可用。 |
| 网络连接 | 本机传输由第一步检查；ChatGPT 到当前环境的连接需选定方式后实测。回环地址本身不代表远端可达。 |
| 运行环境 | 复用当前 Linux；以第一步生成的报告为准。若启动失败，记录具体依赖或进程错误。 |
| 媒体交付 | 本地 PNG 返回与模型视觉读取分别记录；只有真实会话测试才能判断模型是否收到并读懂图片。 |

若第二步失败，保留具体错误、发生阶段和连接方式，再决定是否需要额外资源。
网络未连通时不判定媒体不支持，账号入口未知时不判定必须购买云服务器。

## 视频下载与模型读取分别记录

| 验收项目 | 通过证据 | 不能代替的验收 |
|---|---|---|
| 视频获取 | 后端实际取得媒体字节并完成媒体探测；记录平台、资源与结果。 | 不证明字幕存在，不证明模型读取。 |
| 文字与帧处理 | 后端返回真实字幕/ASR片段和实际视频帧，附来源与时间。 | 不证明 ChatGPT 收到了内容。 |
| 网页模型读取 | 实际 ChatGPT 会话成功读取随机文字、图片像素数字及对应时间戳。 | 不证明原生视频或音频可被读取。 |
| 真实视频端到端 | 探针通过后，再用一个允许处理的视频，核对模型根据工具返回的字幕和帧回答，并正确引用时间。 | 不能只凭下载链接或下载完成状态通过。 |

当前最小范围是文字与图片；原生音频、原生视频及完整平台矩阵保留为后续独立验收。
已有真实平台处理结果见[验收记录](acceptance_report.md)，更完整的探针说明见
[兼容性矩阵](compatibility_matrix.md)。
