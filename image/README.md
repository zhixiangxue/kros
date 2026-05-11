# kros (image-side)

Kros Agent OS 的**镜像内侧** CLI，面向 LLM Agent 提供能力入口。

> 本目录是 Python 实现（`kros -h / browse / read / memory / sandbox`）。
> 宿主机侧的 `kros`（容器启停，Go 实现）未来位于 `../host/`，两者互不依赖。

## 开发安装（本机）

推荐用 uv 的全局工具安装，editable 模式 —— 改代码立即生效：

```bash
cd image
uv tool install --editable .

# 之后在任何目录都可以直接用
kros read path/to/file.pdf
kros read https://example.com/report.pdf
kros --help
```

卸载：`uv tool uninstall kros`

## 配置（Configuration）

Kros 所有配置统一存于 **`~/.kros/.env`**（标准 dotenv 格式）。`kros` 启动时自动加载；**进程环境里已存在的变量不会被 `.env` 覆盖**（兼容 shell `export` 和 `docker run -e`）。

```bash
mkdir -p ~/.kros
cat > ~/.kros/.env << 'EOF'
KROS_LLM_URI=openai/gpt-4o-mini
KROS_LLM_API_KEY=sk-xxxxxxxx
KROS_EMBEDDING_URI=openai/text-embedding-3-small
KROS_EMBEDDING_API_KEY=sk-xxxxxxxx

# 可选：kros memory 的存储位置与默认命名空间
# KROS_MEMORY_HOME=/path/to/memory
# KROS_MEMORY_NAMESPACE=default
EOF
chmod 600 ~/.kros/.env   # 含 API key，建议仅本人可读写
```

**优先级**：进程已有 env（shell export / `docker run -e`） > `~/.kros/.env`。

> `~/.kros/.env` 是**跨双层共享**的：未来宿主机 `kros`（Go）也从同一个文件读配置，通过 `docker run -e` 注入容器。架构约定见 [`design/01-kros-overview.md` 附录 C](../design/01-kros-overview.md)。

## 当前进度

- [x] `kros read` —— 封装 [fyle](https://github.com/zhixiangxue/fyle)
- [x] `kros memory` —— 封装 [seeka-ai](https://github.com/zhixiangxue/seeka-ai)
- [x] `kros sandbox` —— 封装 [doka-ai](https://github.com/zhixiangxue/doka-ai)
- [x] `kros browse` —— 封装 [lightpanda](https://github.com/lightpanda-io/browser)（CLI fast path + CDP lazy spawn，选型与验证见 [`design/01-kros-overview.md` 附录 D](../design/01-kros-overview.md)）
- [ ] `kros -h` 返回 skill.md

## 目录结构

```
image/
├── pyproject.toml
├── README.md
└── src/
    └── kros/
        ├── __init__.py
        ├── cli.py              # Typer app，注册所有子命令
        └── commands/
            ├── __init__.py
            ├── read.py         # `kros read`
            ├── memory.py       # `kros memory`
            ├── sandbox.py      # `kros sandbox`
            └── browse/         # `kros browse`
                ├── __init__.py     # Typer app（get / interact / serve / info）
                ├── driver.py       # DriverProtocol + CDPEndpoint + 懒加载 registry
                ├── lightpanda.py   # LightpandaDriver 实现
                └── session.py      # Playwright-over-CDP 会话上下文
```

新增子命令的约定：在 `commands/` 下新建一个文件（或子目录），提供 `register(app: typer.Typer)`，然后在 `cli.py` 里 import + 调用一次 `register`。

## `kros browse` 使用示例

前置条件：系统 PATH 下有 `lightpanda` 二进制（[下载](https://github.com/lightpanda-io/browser/releases)，扔到 `~/.local/bin/lightpanda` 并 `chmod +x`）。CPU 需 Broadwell 及以上（阿里云 ECS 均满足）。

```bash
# 1. 健康检查：driver / 二进制 / 版本 / CDP 端点 / 存活状态
kros browse info

# 2. 纯 Markdown 抓取（命令行 fast path，不起 CDP，毫秒级返回）
kros browse get https://example.com

# 3. CSS 选择器抽取（CDP lazy spawn：自动起停 lightpanda serve）
kros browse get https://example.com --selector h1

# 4. 自定义 JS 求值（同上走 CDP）
kros browse get https://example.com --eval "document.title"

# 5. 交互式脚本（脚本里可以直接用全局 `page` 对象，Playwright sync API）
cat > /tmp/click.py << 'EOF'
page.goto("https://example.com")
print(page.locator("h1").inner_text())
EOF
kros browse interact /tmp/click.py

# 6. 常驻 CDP server（前台运行，供外部 Playwright / Puppeteer 连接）
kros browse serve --port 9222
```

覆盖默认值用环境变量：`KROS_BROWSE_DRIVER` / `KROS_BROWSE_LIGHTPANDA_BIN` / `KROS_BROWSE_CDP_HOST` / `KROS_BROWSE_CDP_PORT`。
