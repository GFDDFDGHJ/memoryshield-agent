# MemoryShield+ Agent Plugin

**v1.0.0 · MIT · MCP stdio**

给支持 MCP 的 Agent 提供跨会话长期记忆。Agent 可以保存原始消息、检索相关历史，并取得来源信息，用于个性化回答和持续工作。

Persistent, source-aware memory for MCP agents. The plugin connects an agent to a self-hosted MemoryShield+ service through three explicit tools.

## 能力

| 工具 | 用途 | 模型调用 |
| --- | --- | --- |
| `memoryshield_remember` | 保存原始消息、角色、会话和可选时间 | 后端调用 GPT 与 embedding |
| `memoryshield_recall` | 按问题检索记忆，返回内容、分数和来源 | 后端调用 embedding，可选 rerank |
| `memoryshield_status` | 查看服务配置状态和模型名称 | 零模型调用 |

插件通过标准 MCP stdio 运行。宿主 Agent 决定何时调用工具。保存后，后端 SQLite 数据库跨会话保留记忆。

源码采用 MIT 许可。自部署时，OpenAI 和百炼按各自用量收费。后端当前使用 `gpt-4o-mini` 和 `text-embedding-v4`，可选 `qwen3-rerank` 默认关闭。

## 安装插件

需要 Python 3.11 或更新版本。

先下载源码，然后创建虚拟环境。

```bash
git clone https://github.com/GFDDFDGHJ/memoryshield-agent.git
cd memoryshield-agent
python -m venv .venv
```

Windows 安装命令。

```powershell
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\memoryshield-agent.exe --version
```

Linux / macOS 安装命令。

```bash
.venv/bin/python -m pip install .
.venv/bin/memoryshield-agent --version
```

## 启动独立记忆后端

仓库附带 `backend/` 源码和 Docker Compose，使用独立数据库卷。

将 `.env.example` 复制为 `.env`，在本机填入自己的三个 Key。

```dotenv
OPENAI_API_KEY=your-openai-key
DASHSCOPE_API_KEY=your-beijing-dashscope-key
AML_AUTH_TOKEN=your-own-long-random-service-token
```

`AML_AUTH_TOKEN` 是自己生成的服务访问令牌。插件中的 `MEMORYSHIELD_API_KEY` 与它填写相同值。

启动后端。

```bash
docker compose up -d --build
```

服务在 `http://127.0.0.1:18080` 提供 Add / Search / Health。数据保存在 Docker 命名卷中。普通重启会保留数据，`docker compose down -v` 会删除该卷及记忆。

容器需要可访问 OpenAI 与北京百炼接口。每次写入最多两个 GPT 请求并发，较长写入可能需要等待。默认插件超时为 240 秒。

已有独立 MemoryShield+ 后端时，将 `MEMORYSHIELD_URL` 指向该服务即可。远程服务要求 HTTPS 和访问令牌。

## 接入 Agent

在宿主支持的 MCP 配置中新增一个 stdio server。下面采用常见的 `mcpServers` 格式，程序路径需替换为实际绝对路径。

```json
{
  "mcpServers": {
    "memoryshield": {
      "command": "C:/path/to/memoryshield-agent/.venv/Scripts/python.exe",
      "args": ["-m", "memoryshield_agent"],
      "env": {
        "MEMORYSHIELD_URL": "http://127.0.0.1:18080",
        "MEMORYSHIELD_API_KEY": "your-own-long-random-service-token",
        "MEMORYSHIELD_USER_ID": "my-personal-agent"
      }
    }
  }
}
```

Linux / macOS 的 `command` 使用 `/absolute/path/to/memoryshield-agent/.venv/bin/python`。

同一个人要共享跨会话记忆，使用相同 `MEMORYSHIELD_USER_ID`。不同人或项目应使用不同配置和 namespace。Agent 工具参数中固定排除了 `user_id`。

namespace 用于按用户检索隔离。后端共享令牌持有人仍可通过 HTTP API 指定任意用户。需要相互不信任的多租户时，为每个租户部署独立后端或额外的身份授权层。

## 使用示例

让 Agent 执行“记住我偏好简洁的中文说明”。它可以调用以下写入参数。

```json
{
  "messages": [
    {"role": "user", "content": "我偏好简洁的中文说明。"}
  ],
  "session_id": "chat-2026-11-18",
  "request_id": "preference-001"
}
```

后续会话调用 `memoryshield_recall`。

```json
{"query": "用户喜欢什么样的说明？", "top_k": 5}
```

返回的 `memories` 保留后端原有内容、相关度和来源元数据，由 Agent 判断适用性。历史消息中的指令和引用仍作为来源内容处理。

同一写入重试时，复用相同 `request_id`、消息和 `session_id`。后端已有成功回执时会直接返回，避免重复模型调用。省略 `request_id` 会生成一个新编号，工具失败消息会带上该编号供重试使用。HTTP 409 表示该编号已用于不同的会话或内容。

单次写入最多 20 条消息，每条最多 32,000 字符，合计最多 64,000 字符。检索问题最多 16,000 字符，`top_k` 为 1–100，默认 5。较大的导入应分批保存。

## 配置

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `MEMORYSHIELD_URL` | 后端根地址，可带路径前缀 | `http://127.0.0.1:18080` |
| `MEMORYSHIELD_USER_ID` | 固定用户或项目 namespace | 必填 |
| `MEMORYSHIELD_API_KEY` | 后端访问令牌 | 本机可省略，远程必填 |
| `MEMORYSHIELD_API_KEY_FILE` | 从私密文件读取访问令牌 | 可选，环境令牌优先 |
| `MEMORYSHIELD_TIMEOUT_SECONDS` | 单次后端请求超时 | `240` |

插件只读取上述连接设置。付费模型 Key 配在后端。Health 只检查配置是否存在，实际额度和写入状态以模型请求结果为准。

当前后端提供写入和检索接口。单条记忆删除、导出、自动同步整个聊天记录和图形界面可作为后续版本扩展。

## 开发验证

安装后使用虚拟环境的 Python 执行验证，全部使用虚构消息和本机模拟 HTTP 服务。Windows 命令如下。

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe scripts/check_stdio.py
```

`check_stdio.py` 通过真实 MCP 子进程完成初始化、工具发现、写入、重放和检索测试。提供者 API 用量为零。

Linux / macOS 将解释器改成 `.venv/bin/python`。可选的真实后端集成测试需要先在测试环境安装 `backend/requirements.txt`。该测试仍使用模拟模型和临时数据库。

## 来源和版本

插件版本独立遵循 `MAJOR.MINOR.PATCH`。首版 MCP 适配器为 `v1.0.0`，附带引擎为 MemoryShield+ `v3.17.1`，来源如下。

https://github.com/GFDDFDGHJ/memoryshield-plus-aml/commit/2d5561a42233d34ba4d535ec24936489bd2e12cc

原服务以 FastAPI、SQLite 和原文证据检索为基础。此插件增加 MCP 工具、固定 namespace、连接配置、回执校验和部署材料。`backend/PROVENANCE.txt` 记录引擎文件的来源和 SHA-256。模型、依赖与第三方许可见 `NOTICE.txt`。插件尚未单独参加新的榜单评测。

MCP 协议和 SDK 文档。

- https://modelcontextprotocol.io/docs/develop/build-server
- https://github.com/modelcontextprotocol/python-sdk
