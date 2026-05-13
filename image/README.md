# kros (image-side)

Kros Agent OS 的**镜像内侧** CLI，面向 LLM Agent 提供能力入口。

> 本目录是 Python 实现（`kros -h / browser / file / memory / sandbox`）。
> 宿主机侧的 `kros`（容器启停，Go 实现）未来位于 `../host/`，两者互不依赖。

## 开发安装（本机）

推荐用 uv 的全局工具安装，editable 模式 —— 改代码立即生效：

```bash
cd image
uv tool install --editable .

# 之后在任何目录都可以直接用
kros file read path/to/file.pdf
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

- [x] `kros file read` —— 封装 [fyle](https://github.com/zhixiangxue/fyle)
- [x] `kros memory` —— 封装 [seeka-ai](https://github.com/zhixiangxue/seeka-ai)
- [x] `kros sandbox` —— 封装 [doka-ai](https://github.com/zhixiangxue/doka-ai)
- [x] `kros browser` —— 封装 [lightpanda](https://github.com/lightpanda-io/browser)（CLI fast path + CDP lazy spawn，选型与验证见 [`design/01-kros-overview.md` 附录 D](../design/01-kros-overview.md)）
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
            ├── file.py         # `kros file read`
            ├── memory.py       # `kros memory`
            ├── sandbox.py      # `kros sandbox`
            └── browser/        # `kros browser`
                ├── __init__.py     # Typer app（16 atomic ops + list/switch）
                ├── contract.py     # BrowseDriver protocol + exceptions
                ├── _tabs.py        # Tab state management
                ├── formatting.py   # Output formatting
                └── drivers/        # Driver implementations
                    └── lightpanda_mcp/  # LightPanda MCP driver
```

新增子命令的约定：在 `commands/` 下新建一个文件（或子目录），提供 `register(app: typer.Typer)`，然后在 `cli.py` 里 import + 调用一次 `register`。

## `kros browser` 使用示例

前置条件：系统 PATH 下有 `lightpanda` 二进制（[下载](https://github.com/lightpanda-io/browser/releases)，扔到 `~/.local/bin/lightpanda` 并 `chmod +x`）。CPU 需 Broadwell 及以上（阿里云 ECS 均满足）。

```bash
# 1. 健康检查：driver / 二进制 / 版本 / 存活状态
kros browser info

# 2. 打开页面（新 tab，自动拍快照返回 Markdown + 元素列表）
kros browser open https://example.com

# 3. 读取当前 tab 快照
kros browser read

# 4. 点击 ref 元素
kros browser click --ref 42

# 5. 列出所有 tab
kros browser list

# 6. 关闭所有 tab
kros browser close --all
```

覆盖默认值用环境变量：`KROS_BROWSER_DRIVER` / `KROS_BROWSER_LIGHTPANDA_BIN` / `KROS_BROWSER_RUNTIME_DIR`。

## Docker 镜像

仅支持 `linux/amd64`（lightpanda 只发 x86_64，sandbox 走 bubblewrap）。

### 构建

```bash
docker build -t kros:dev ./image

# 钉一个具体的 lightpanda release tag（默认 nightly）
docker build -t kros:dev \
  --build-arg LIGHTPANDA_VERSION=nightly \
  ./image
```

镜像内预置：`bubblewrap`（sandbox 默认 runtime）、`/usr/local/bin/lightpanda`（browser 默认驱动）、`/app/.venv` 下的 `kros` 入口。`ENTRYPOINT` 就是 `kros`，所以容器后面跟子命令即可。

### 运行

```bash
# file read — 挂载一个目录进来再读
docker run --rm -v $PWD:/workspace kros:dev file read ./report.pdf

# memory — 挂载 ~/.kros 持久化 memos + 复用 .env 里的 API key
docker run --rm -v $HOME/.kros:/root/.kros kros:dev memory list

# browser — lightpanda 已内置，直接用
docker run --rm kros:dev browser open https://example.com

# sandbox — bubblewrap 需要 user namespaces，放宽默认 seccomp 即可
docker run --rm --security-opt seccomp=unconfined \
  kros:dev sandbox run "echo hello from bwrap"
```

### 配置注入

容器内 `kros` 启动时会加载 `/root/.kros/.env`（和宿主侧行为一致）。两种注入方式任选：

```bash
# 方式 A：挂载宿主的 ~/.kros
docker run --rm -v $HOME/.kros:/root/.kros kros:dev memory remember "..."

# 方式 B：docker run -e 直接注入（优先级高于 .env）
docker run --rm \
  -e KROS_LLM_URI=openai/gpt-4o-mini \
  -e KROS_LLM_API_KEY=sk-xxxx \
  kros:dev memory remember "..."
```
