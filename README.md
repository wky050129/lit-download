# lit-download

lit-download 是一个装在 AI Agent（Codex、Claude Code 等）里的技能包（skill），用来批量下载学术文献。用户把一张 DOI 或 arXiv 编号的清单交给 Agent，它会逐篇判断这篇文章哪个数据库能下，一个库一个库去试。下到的每一篇都要核验是不是这篇、文件全不全，然后按统一格式命名放进文献库，最后列出还缺哪几篇。

## 它按什么顺序去哪些库

前面的库下到了，后面的就不用去：

1. **arXiv**：有 arXiv 编号的直接下；
2. **开放获取的出版商**：Nature、MDPI、PLOS 等标为开放获取的文章；
3. **学校图书馆**：经学校 WebVPN 进 ScienceDirect、Wiley、Springer、IEEE、EBSCO 等；人在校园网时，还能直接从 Taylor & Francis 等出版商官网下。图书馆资源列表里对口的试用库和免费平台也属于这一类，但它们没有现成的下载脚本，等 Sci-Hub 和公开版本都没有时，Agent 才去手动找；
4. **Sci-Hub**：有 DOI 的都会试；
5. **公开版本**：知识库、SSRN、arXiv 上的预印本或录用稿；
6. **科研通**：各库都试过仍没有的，花积分发求助，发之前先问用户。

不内置任何学校。第一次用时，Agent 会问学校 WebVPN 门户的网址。用户登录后，它先读一遍门户上有哪些数据库，再决定去哪些库。

## 需要准备

- Python 3.10 以上，以及 4 个包：`pip install -U playwright pymupdf openpyxl websockets`（`-U` 会把电脑上已有的旧版一起升级；不需要再运行 `playwright install`，脚本直接用电脑上的 Chrome）；
- Chrome，装在默认位置即可；
- 一个支持 skill 的 Agent，比如 Codex 桌面版、Claude Code；
- 学校 WebVPN 账号。没有的话，只能走 arXiv、开放获取、Sci-Hub 和公开版本。

## 安装

把整个仓库放进 Agent 的 skills 文件夹，文件夹名用 `lit-download`：

```
# Codex
git clone https://github.com/wky050129/lit-download.git "%USERPROFILE%\.codex\skills\lit-download"
# Claude Code
git clone https://github.com/wky050129/lit-download.git "%USERPROFILE%\.claude\skills\lit-download"
```

装好后，在 Agent 的输入框里输入 `$lit`，能看到 Lit Download 就说明装上了。

## 怎么用

在电脑上建一个项目文件夹，放一个清单文件（一行一个 DOI 或 arXiv 编号），再放一个空的文献库文件夹。在 Agent 里打开这个文件夹，然后把要做的事用一句话告诉它：

```
$lit-download 把 清单.txt 里的文献下载到 文献库 文件夹
```

第一次用时，用户亲手做两件事：

- 回答 Agent 的问题，告诉它学校 WebVPN 门户的网址，以后就不再问了；
- Agent 会打开一个专用 Chrome 窗口，用户在里面登录一次学校 WebVPN。账号、密码、短信验证码只在这个窗口的网页里输入，不要发到对话里。

下面这些话都在实际使用中用过：

```
我现在登录不了学校账号，先跳过要登录的库，把公开版本、Sci-Hub 这些不用登录的先下完
```

```
$lit-download 把 清单2.txt 里的文献下载到 文献库，这次走学校的库，需要登录就告诉我
```

```
剩下那篇用 Sci-Hub 试试，能下就放进文献库
```

```
还有哪些没下载？每篇是从哪个渠道下到的，列个表给我
```

```
$lit-download 把 清单3.txt 里这篇下载到 文献库。这篇我之前在另一张清单里各库都试过：学校的 EBSCO 只有题录，Sci-Hub 没有，SSRN 上的版本不提供下载。确实没有的话，就去科研通发求助
```

日常还可以说“补齐还没下的”“能下的都下”“现在做到什么程度了”“暂停”“我下好了，帮我改名归档”。

下载时，脚本在专用 Chrome 里一篇一篇排队下。遇到人机验证，脚本先松开浏览器，用系统鼠标去点；点不过去时，Agent 会告诉用户是哪个网站，到专用 Chrome 里点一下即可。点的时候鼠标会自己移动，下载期间最好不要同时用电脑打字。

## 结果

- 文献库里的文件按 `第一作者姓(两位出刊年 期刊缩写)完整标题.pdf` 命名，例如 `Jumper(21 Nature)Highly accurate protein structure prediction with AlphaFold.pdf`；
- 期刊缩写来自 `scripts/journal_abbr.json`。表里没有的期刊，脚本先猜一个，Agent 会提醒。用户同意换成别的缩写后，让 Agent 加进表里，并用 `litdl.py rename` 把已经入库的文件一起改过来；
- 项目文件夹里的 `manifest.xlsx` 记着每篇的状态、版本（正式版、预印本、录用稿）和每个库试过的结果，`00_待下载.md` 是缺失清单。

## 文件

| 位置 | 内容 |
|---|---|
| `SKILL.md` | 给 Agent 看的总流程和规则 |
| `references/` | 安装和设置、各数据库的取法、核验、科研通、踩过的坑、实测记录 |
| `scripts/litdl.py` | 建清单、判断收录、下 arXiv、核验入库、记账、缺失清单 |
| `scripts/litbrowser.py` | 专用 Chrome：登录、读门户、排队下载、过人机验证 |
| `scripts/ablesci_assist.py` | 科研通：求助、签到、应助 |
| `tests/test_offline.py` | 离线测试，在仓库根目录运行 `python -X utf8 -m unittest discover -s tests` |

学校 WebVPN 地址等设置存在用户自己的 `~/.litdl/` 里，不在这个仓库里。

## 用到的开源项目

lit-download 没有复制其他项目的代码。运行时用到下面这些开源项目，由用户自己安装，不包含在这个仓库里：

| 项目 | 在 lit-download 里做什么 | 许可证 |
|---|---|---|
| [Playwright for Python](https://github.com/microsoft/playwright-python) | 接管专用 Chrome，排队逐篇下载 | Apache-2.0 |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | 读下到的 PDF，核验题名、DOI 和页数 | AGPL-3.0 或 Artifex 商业许可 |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | 写清单表格 `manifest.xlsx` | MIT |
| [websockets](https://github.com/python-websockets/websockets) | 遇到人机验证、松开浏览器后，读验证页 | BSD-3-Clause |
| [scansci-pdf](https://github.com/Rimagination/scansci-pdf)（可选） | 下载通道之一，小批量下开放获取文章；备用通道 `vpnfetch` 也能用它保存的 WebVPN 登录 | MIT |

## 许可证

MIT，见 `LICENSE`。
