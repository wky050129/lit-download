# 安装、填自己学校的信息、第一次运行

第一次在一台机器上用这个 skill 时读本文件。这个 skill 不默认任何学校，学校的 WebVPN 地址要用户给（第 3 节）。`LITDL`、`LITB`、`LITA` 的含义见 SKILL.md。

## 1. 依赖

| 需要什么 | 说明 |
|---|---|
| Python 3.10 以上 | 实测用的是 3.14 |
| `pip install playwright pymupdf openpyxl websockets` | 不需要 `playwright install`，脚本接管本机的浏览器。`websockets` 只给 ScienceDirect 那一步用（见第 4 节末）。只跑 `LITDL arxiv` 的话一个包都不用装 |
| Chrome 或 Edge | 脚本按常见安装位置找；装在别处时每条 `LITB` 命令加 `--exe <路径>` |
| curl | 只有备用通道 `LITDL vpnfetch` 用。Windows 10 以上自带 |
| Windows | 系统鼠标点验证框（`osmouse.py`）只在 Windows 上有；别的系统自动退回浏览器内的点击，其余功能照常 |

检查：

```
python -X utf8 -P -c "import fitz, openpyxl, playwright, websockets; print('ok')"
```

## 2. 放在哪

整个文件夹放到 `~/.claude/skills/lit-download/`，里面是：

```
SKILL.md
references/   setup.md  publisher-recipes.md  verification.md  ablesci.md  pitfalls.md  test-log.md
scripts/      litdl.py  litbrowser.py  captcha_ai.py  osmouse.py  ablesci_assist.py  journal_abbr.json  holdings.json  schools.json
tests/        test_offline.py
```

脚本自己会建 `~/.litdl/`，登录状态和下载的中间文件都在这里，不在 skill 目录里：

| 路径 | 内容 |
|---|---|
| `~/.litdl/chrome-profile/` | 专用 Chrome 的 profile（WebVPN、科研通的登录在这里）。不要发给别人 |
| `~/.litdl/stage/<清单名>/` | 一张清单的临时下载目录、`results.jsonl`、`progress.json`、`fetch.log` |
| `~/.litdl/downloads/` | 没有脚本连着时，用户在专用 Chrome 里自己点的下载；科研通求助收到的文件也先落在这里（文件名带 `ablesci.com`），等 `ingest` 核验 |
| `~/.litdl/assist/<doi>.pdf` | 科研通应助时现下的公开版本，一个 DOI 一个文件。只为上传用，不进文献库，传完可以删 |
| `~/.litdl/assist/log.jsonl` | 科研通的流水，一行一条：每次应助（上传了、没下到、核验不过）、每次发求助、取文件、采纳。查"这条传过没有""这篇求助的 id 是什么"看这里 |
| `~/.litdl/assist/scanned.txt` | `LITA scan` 查过而没有合格公开版本的求助 id，一行一个，下次扫描跳过。删掉它等于全部重查（平时用 `scan --rescan`） |
| `~/.litdl/.env` | 配置：自己学校的 WebVPN 地址、EBSCO 的 opid，以及可选的 key，见第 3、4 节。不要发给别人 |
| `~/.litdl/holdings.json` | 自己学校实测的收录表，盖过 skill 自带的起步表，见第 5 节 |
| `~/.litdl/portal.json` | `LITB portal --save` 从学校门户和图书馆资源页上读到的：哪些数据库有入口、各自的地址；读过图书馆资源页的话，还有每个库是已购、试用还是免费。有这个文件说明这所学校的门户已经看过 |

环境变量 `LITDL_HOME` 可以把 `~/.litdl` 换到别处，`LITDL_CDP_PORT` 改调试端口（默认 9333）。

## 3. 第一次运行：先填自己学校的信息

这个 skill 不默认任何一所学校（`scripts/schools.json` 里只附带了走通过的学校的图书馆路径）。第一件事是把用户学校的 WebVPN 地址填进去；没填的时候 `LITDL plan`、`LITB fetch`、`LITB login webvpn` 都会停下来要。

```
LITDL config                               # 看现在填了什么
LITDL config --webvpn webvpn.xxx.edu.cn    # 学校 WebVPN 门户的域名。没有学校 WebVPN 的填 none
LITB open                                  # 弹出一个单独的 Chrome 窗口。它一律直连，电脑开着 Clash 之类的代理也不走
LITB login webvpn --wait 300               # 打开学校 WebVPN 登录页，用户自己登录
LITB status                                # 应显示 WebVPN 已登录，并列出标签页
LITB portal --save                         # 读门户首页：有哪些数据库的入口、网址怎么写、EBSCO 的 opid，记进配置
```

- **WebVPN 地址由用户给。** 问用户"学校 WebVPN 门户的网址是什么"，不猜、不上网搜、不沿用文档里出现过的别的学校。用户贴来的是整条网址也行，脚本只留主机名。它存在 `~/.litdl/.env`，以后不再问。
- **登录后先读门户。** `LITB portal` 打开门户首页，把页面上的链接和已知数据库对一遍，报出：哪些数据库有入口、这所学校把它们的域名写成什么样（和常规写法不同的会标出来）、EBSCO 的 opid（入口地址里没有时，它会点开 EBSCO 从跳转后的地址里读）。加 `--save` 把这些写进 `~/.litdl/portal.json`、`~/.litdl/holdings.json` 的 `webvpn_hosts` 和 `.env`。
- **门户首页上没有数据库入口时，问用户。** 这时脚本输出 `NEED_USER`，把首页留在前台，并列出名字像入口的链接供辨认。问用户"学校的数据库列表在哪一页"，用户说了位置再读那一页：`LITB portal --url <网址> --save`，或者用户在专用 Chrome 里自己点到了那一页，`LITB portal --tab <标签页> --save`。不替用户猜是哪个栏目，不自己挨个点开找。
- **再读图书馆的资源列表：`LITB portal --library --save`。** 图书馆网站的资源导航页常比 WebVPN 首页全（实测学校：首页认出 8 个数据库，图书馆那一页有 91 个），还会标出免费和试用的。**这条命令只对 `scripts/schools.json` 里记过路径的学校照着走**，目前只有一所；别的学校它只给一句提示并输出 `NEED_USER`，要问用户"图书馆的电子资源 / 数据库导航 / 免费资源在哪一页"，用户说了再 `LITB portal --url <网址> --save`。在新学校走通以后，往 `scripts/schools.json` 里加一条（学校名、人是怎么点过去的、那一页的网址）。
- **门户把目标地址藏起来的**（链接是一长串加密字符）：脚本只能按入口的名字认出数据库，读不出网址写法。这种门户属于第 5 节说的"别的形式"。
- **EBSCO 的 opid 没读出来时手工找**：从门户点开 EBSCO，地址栏是 `https://research-ebsco-com-443.<vpn>/c/<opid>/search`，中间那一段就是，`LITDL config --ebsco-opid <opid>`。学校没订 EBSCO 就不用填，并在收录表里把 `ebsco` 去掉（第 5 节）。
- 然后拿一两个 DOI 走一遍：`LITDL plan` 建清单，`LITDL locate` 看排出的顺序，`LITB fetch … --dry-run` 看要请求什么，`--ids <一个序号>` 单篇试，`LITDL ingest` 入库。单篇通了再放量。

要用科研通时再登录一次：`LITB login ablesci --wait 300`，用户自己输账号密码；`LITA points` 能报出余额就是登上了。

**在 Git Bash 里把这些命令串起来用时**（Claude Code 的 Bash 工具就是 Git Bash）有三处要留意：

- Windows 的 Python 输出行尾是 `\r\n`，`$(…)` 和 `read` 会把 `\r` 留在变量里，后面的 DOI、求助 id 就对不上。接输出时加 `| tr -d '\r'`。
- Git Bash 的 `/tmp` 和 Windows Python 看到的 `/tmp` 不是同一个目录。两边传数据用管道，或者写完整的 `C:/…` 路径。
- 以 `/` 开头的参数会被改写成 `C:/Program Files/Git/…`，网址写全。

## 4. `~/.litdl/.env`

一行一个 `KEY=VALUE`，`#` 开头是注释。已经设在系统环境变量里的以环境变量为准。前两行用 `LITDL config` 写，不用手改；其余全部可选：

```
# 自己的学校
LITDL_WEBVPN=webvpn.xxx.edu.cn
LITDL_EBSCO_OPID=xxxxxx

# Sci-Hub 镜像，按顺序试（不写就是下面这三个）
# LITDL_SCIHUB=sci-hub.ru,sci-hub.st,sci-hub.se
# 专用 Chrome 默认直连、不走系统代理。确实要它走系统代理时才打开这一行
# LITDL_CHROME_PROXY=1

# 无人值守时让视觉模型试点选和滑块验证；不配也行，复选框照样会点，其余交给人
LITDL_CAPTCHA_API_KEY=
LITDL_CAPTCHA_BASE_URL=https://api.openai.com/v1
LITDL_CAPTCHA_MODEL=gpt-4o-mini
# LITDL_CAPTCHA_ATTEMPTS=3
# LITDL_CAPTCHA_TIMEOUT=60
# LITDL_CAPTCHA=0            整个关掉，验证全交给用户
# LITDL_CAPTCHA_OS_MOUSE=0   不用系统鼠标
# LITDL_HANDSOFF=0           遇到验证不先松开浏览器（退回挂着浏览器的老做法，见 verification.md 第 4 节）
```

`websockets` 包用在过验证那一步：松开浏览器后，靠它读验证页的控件位置、截图（`verification.md` 第 1 节）。没装时脚本自动退回挂着浏览器的老做法。

key 由用户自己写进这个文件，不要贴在对话里。

## 5. 把自己学校的情况测出来

和学校有关的东西有三样，都不在 skill 目录里，各人各一份：

| 什么 | 放在哪 | 怎么得到 |
|---|---|---|
| WebVPN 地址 | `~/.litdl/.env` 的 `LITDL_WEBVPN` | 用户给，`LITDL config --webvpn` |
| EBSCO 的 opid | `~/.litdl/.env` 的 `LITDL_EBSCO_OPID` | 登录后从 EBSCO 的地址栏读，`LITDL config --ebsco-opid` |
| 收录表 | `~/.litdl/holdings.json` | 一篇一篇试出来，见下 |

**收录表**说的是哪家出版商（或哪本期刊）的全文在学校的哪个库里。脚本拿到一篇文章先查它，决定去哪个库、按什么顺序。skill 自带的 `scripts/holdings.json` 只是一张起步用的通用表（出版商有自己平台的先去它的平台，经管类再查 EBSCO），不代表任何学校。自己学校测出来的情况写到 `~/.litdl/holdings.json`，格式和它一样，只写不一样的条目，会盖过自带的那份；字段的意思写在自带那份的"_说明"里。改完用 `LITDL locate <manifest>` 看排出来的顺序对不对，`LITDL config` 会列出现在用的是哪几份表。

**前提是学校的 WebVPN 用"改写域名"的方式**：`www.sciencedirect.com` 变成 `www-sciencedirect-com-443.<vpn>`。登录后在门户里随便点开一个数据库，看地址栏是不是这个形式。

- 是这个形式，只是个别站点写法不同（见过 Springer 是 `link-springer-com.<vpn>:443`，用 `-443` 的写法会报"没有权限访问此应用"）：在自己的收录表里加 `"webvpn_hosts": {"link.springer.com": "link-springer-com.{vpn}:443"}`。
- 是别的形式（地址里是一长串加密字符，或者走 EZproxy、CARSI）：`LITDL plan` 生成的 `webvpn_page`、`webvpn_pdf` 不能用。EBSCO、Wiley、Springer、IEEE、Elsevier 这几个配方要先用 `LITB goto / text / eval` 探通，再改 `scripts/litdl.py` 里的 `webvpn_links()`。这种学校在改通之前，可以把 WebVPN 填成 `none`，先用不依赖学校的那几个库。

要逐项确认的事，每一项都先单篇试：

- **门户里有哪些数据库的入口。** `LITB portal` 报出来的就是（第 3 节）。出版商在列表里的，用它自己的配方（`wiley`、`direct`、`elsevier`）；不在列表里的，不等于学校没订，可能是按校园 IP 授权（人在校园网时用 `tandf`、`pubdoi` 试），也可能全文在 EBSCO 这类集成库里。
- **EBSCO 里有什么。** 订的是哪个库，哪些出版商的刊在里面是全文、哪些只有题录。拿每家出版商的两三篇试，结果写进收录表。
- **登录的判断。** 脚本看门户首页有没有 `sign_out` 链接、有没有跳到 `/users/sign_in`。门户长得不一样时 `LITB status` 会误报，要改 `litbrowser.py` 里的 `vpn_logged_in()`。
- **期刊缩写表** `scripts/journal_abbr.json` 是经管和计算机方向常用刊，别的方向遇到"缩写是猜的"提示时往里加。

不依赖学校的部分可以直接用：arXiv、开放获取期刊、Sci-Hub、公开版本（知识库、SSRN）、核验入库和命名、记账、过验证、科研通。

**`publisher-recipes.md` 里的实测数字来自一所学校**（改写域名式的 WebVPN，EBSCO 订的是商科库）。配方的步骤是通用的；"哪个库有什么、一篇要多少秒"这些只是那一所学校的情况，当例子看，自己学校的以自己测的为准。**到 2026-11-21 为止，这个 skill 只在那一所学校实际用过**，在第二所学校从填地址开始走一遍这件事还没有人做过，走的时候把遇到的差别补回本节。

## 6. 改了脚本以后：跑离线测试

```
python -X utf8 -m unittest discover -s tests
```

在 skill 目录下运行，十几秒。它不联网、不开浏览器、不读也不写 `~/.litdl`（用的是临时目录），查的是：收录判断和选库顺序、WebVPN 网址改写、学校配置没填时各命令是否停下、账本、门户页面的识别、按批求助的把关，以及文档里写的每条命令和参数在脚本里都存在。改了脚本或文档就跑一遍。站点那一头（页面结构、能不能下到）它测不了，那部分看 `test-log.md`。

## 7. 可选：scansci-pdf

SKILL.md 第 3 节里开放获取桶可以交给 scansci-pdf 这个 MCP 小批量下，Elsevier API（`publisher-recipes.md` 第 6 节末）也靠它。没装就跳过这两处：开放获取桶改用 `LITB fetch … --via public`，Elsevier 走浏览器配方。
