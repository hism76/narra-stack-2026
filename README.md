# NarraNexus All-In-One Gateway & Register Stack

高效、轻量级的 **NarraNexus** 智能体网关与自动化账号注册服务栈，全面支持 OpenAI Chat Completions 与 OpenAI Responses (`/v1/responses`) 协议生态。

---

## 🌟 核心特性

1. **双协议全量兼容**：
   - 完整兼容标准 OpenAI `/v1/chat/completions` 流式与非流式接口。
   - 深度对齐 OpenAI 新一代 `/v1/responses` 协议，原生适配 `response.output_text.delta`、`response.reasoning_text.delta` 事件流，无缝接入 New API、One API、Operit 等客户端。
2. **全量模型目录支持**：
   - 内置并同步官方全量 91+ 大语言模型目录，包含 Claude 3.7 / 3.5、DeepSeek-V4、GLM-5.3、Kimi-K3、GPT-5.6、Qwen 等主流模型及别名映射。
3. **极速模式 (Fast Mode) 开关**：
   - 支持请求级透传与后台全局开关，可自动切换至轻量智能体模式，显著降低首字延迟与等待时间。
4. **高可用自动注册 & 接码池**：
   - 集成 YYDS 临时邮箱平台，支持域名智能轮换与验证码智能轮询重试。
   - 结合代理池（如 Clash API）实现 IP 自动切换，有效规避上游频控封禁。
5. **毫秒级主动保活心跳**：
   - 具备长链接 keep-alive 机制与无缓冲推流头（`X-Accel-Buffering: no`），彻底防止 Cloudflare Tunnel 及移动蜂窝网络超时断流。

---

## 🚀 快速开始

### 1. 配置环境变量

复制环境变量模板并根据实际情况配置：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
# YYDS 接码平台 Key (可选)
YYDS_KEY=your_yyds_key_here

# Cloudflare Tunnel 隧道 Token (可选，用于公网发布)
CLOUDFLARE_TUNNEL_TOKEN=your_cloudflare_tunnel_token

# 本地代理网络地址
NETMIND_PROXY=http://clash-proxy:7890

# 控制台初始管理员凭据
ADMIN_USER=admin
ADMIN_PASS=admin123456
```

### 2. 启动服务

使用 Docker Compose 一键构建与拉起服务：

```bash
docker-compose up -d --build
```

启动后服务运行在：
- **控制台**: `http://127.0.0.1:8001` (或 Cloudflare Tunnel 绑定的公网域名)
- **API 网关**: `http://127.0.0.1:8001/v1/chat/completions`

---

## 🔒 安全与隐私建议

- 请勿将 `.env` 或 `shared-data/narra.db` 提交至任何公开或共享代码仓库。
- 生产环境中请务必登录控制台「系统核心设置」及时修改默认管理员密码。

---

## 📄 协议许可

MIT License
