# 出版商与渠道操作手册

走浏览器通道之前读本文件。文中 `<vpn>` 指用户学校的 WebVPN 域名（`LITDL config` 里填的那个），`<opid>` 指这所学校在 EBSCO 的客户号，`LITB`、`LITDL` 的含义见 SKILL.md。

## 目录
1. WebVPN 网址规律
2. 专用 Chrome、登录和读门户
3. EBSCO（INFORMS 等经管类期刊）
4. Wiley
5. Springer、IEEE
6. Elsevier / ScienceDirect
7. T&F 与其他出版商：WebVPN 入口、校园 IP
8. Sci-Hub
9. 公开版本：知识库、SSRN、出版商开放页
10. 没有现成配方时：用原语一步步做
11. 备用：vpnfetch（curl）

另外三份：人机验证在 `verification.md`，科研通在 `ablesci.md`，各配方哪天测过、结果如何在 `test-log.md`。

**适用范围**：配方的步骤是通用的。第 1–7、11 节里的收录情况、速度和日期，是在一所学校实测的（改写域名式的 WebVPN，EBSCO 订的是商科库），下文叫"实测学校"，当例子看。自己学校的情况照 `setup.md` 第 5 节一篇一篇测出来，每个配方先单篇试。

---

## 1. WebVPN 网址规律

脚本支持的是"改写域名"式的 WebVPN：把原域名里的 `.` 换成 `-`，再加上 `-443.<vpn>`。学校的门户是不是这种，登录后点开一个数据库看地址栏；不是的话见 `setup.md` 第 5 节。

| 出版商 | WebVPN 下的地址 |
|---|---|
| ScienceDirect 文章页 | `https://www-sciencedirect-com-443.<vpn>/science/article/pii/{PII}` |
| IEEE 直接取 PDF | `https://ieeexplore-ieee-org-443.<vpn>/stampPDF/getPDF.jsp?tp=&arnumber={N}` |
| Springer | 常规是 `https://link-springer-com-443.<vpn>/article/{doi}`，PDF 是 `/content/pdf/{doi}.pdf`。**实测学校是个例外**：门户用的是 `link-springer-com.<vpn>:443`，用 `-443` 的写法会报"您没有权限访问此应用"，打开后会跳到 `link-springer-com-s.<vpn>`。这类例外写在 `~/.litdl/holdings.json` 的 `webvpn_hosts` 里 |
| Wiley | `https://onlinelibrary-wiley-com-443.<vpn>/doi/{doi}` |
| EBSCO 检索 | `https://research-ebsco-com-443.<vpn>/c/<opid>/search`。`<opid>` 每所学校不同，登录后从这个地址里读出来，`LITDL config --ebsco-opid` 存下 |
| 门户首页 | `https://<vpn>/`。页面里有 `sign_out` 说明已登录，跳到 `/users/sign_in` 说明没登录 |

- `doi.org` 不走代理。所以要先用 Crossref 查到 PII（`alternative-id`）和 IEEE 的 arnumber（`resource.primary.URL` 里的 `/document/N`），再拼出上面的地址。`LITDL plan` 已经做好了这一步。
- 外文资源入口（实测学校）：门户首页的"外文资源"。2026-11 看到的有 Wiley Online、Springer 电子期刊、SciFinder、RSC、PQDT Global、EarthDoc、APS 全文期刊、Ei、Elsevier、EBSCO、IEEE Electronic、ESI。
- **图书馆的资源列表比门户首页全**（2026-11-21，`LITB portal --library`）：实测学校的门户首页只有 Wiley、Springer、Elsevier、IEEE、EBSCO 等 8 个文献库的入口；图书馆"电子资源"页有 91 个库，其中 T&F（`www-tandfonline-com.<vpn>`）、Emerald（`www-emerald-com-s.<vpn>`）、ACM（`dl-acm-org.<vpn>`）都有 WebVPN 入口，写法不是常规的 `-443`。11-21 晚逐个试了单篇：**T&F 可用**（文章页写着 "Access provided by <学校名>"、"Full access"，有 Download PDF 链接），用新配方 `vpnpub`（第 7 节）；**Emerald 不可用**（文章页和首页都停在 Cloudflare 的安全验证上，页面自己写着"无法连接到网站……安全验证过程无法完成"，验证脚本经 WebVPN 加载不了）；**ACM 不可用**（两次打开都 60 秒超时，第二次专用 Chrome 整个退出，之后不再试）。入口在不等于能用，所以哪家走 `vpnpub` 要逐家试过再写进收录表。下一条里"没有入口"的说法是只看门户首页、并且用 `-443` 写法试出来的，以这一条为准。
- **实测学校没开的入口**（2026-11-19，只看了门户首页）：T&F 的 WebVPN 域名（当时用的是常规的 `-443` 写法）会跳回登录页；INFORMS、SAGE、ACM、Emerald 没有入口。这些刊去哪个库由收录表定（SKILL.md 第 3 节）。**WebVPN 没有入口不等于学校没订**：T&F 是按校园 IP 授权的，人在校园网时打开出版商官网就能下（第 7 节）。
- 用户说"应该从 WebVPN 那个页面进文献库，搜对应期刊再下"时，照这个路径走：门户首页 → 外文资源 → 对应的库。

## 2. 专用 Chrome、登录和读门户

- 位置：profile 在 `~/.litdl/chrome-profile`，调试端口 9333（只监听本机）。环境变量 `LITDL_HOME`、`LITDL_CDP_PORT` 可以改。
- 它是普通方式启动的本机 Chrome：没有自动化启动标记，没有改指纹。首次启动时写入三项设置：PDF 直接下载而不是在阅读器里打开；下载不弹"另存为"；启动时恢复上次会话。
- `LITB open` 启动；`LITB status` 查 WebVPN 是否登录并列出标签页；`LITB login webvpn --wait 300` 打开登录页、放到前台、每 4 秒看一眼是否登录成功，期间不开新标签页、不碰页面。
- 脚本运行时下载的文件由 Playwright 接走，直接存进 stage 目录。没有脚本连着的时候，用户在这个窗口里自己点的下载落在 `~/.litdl/downloads`。
- 调试端口意味着本机上的程序可以操作这个 Chrome。所以这个 profile 只用来登 WebVPN、科研通这类文献站点，不要让用户在里面登别的账号。
- 用户日常 Chrome 里的登录状态，这里读不到；反过来也一样。第一次要向用户说明为什么弹出一个单独的窗口。
- **登录后先读门户：`LITB portal [--url <页面> | --tab <id>] [--save]`。** 它把页面上的链接（`a[href]`、`data-url`、`onclick` 里的网址）和已知数据库对一遍。认的办法有两种：链接的主机名是某个数据库的域名按这所学校的 WebVPN 改写后的样子；或者链接读不出原站点，但入口的名字里有数据库的名字。端口看的是页面里写的原始地址（浏览器会把 `:443` 吃掉，而有的门户正是靠它区分站点）。
  - 结果里每个数据库一行：入口的名字、主机名的写法，和常规写法 `<域名里的点换成短横>-443.<vpn>` 不同的会标出来。
  - **一个数据库都没认出来时返回 `NEED_USER`，不继续翻**：把这一页留在前台，列出名字像入口的链接（含"资源""数据库""图书馆"之类字样的）供用户辨认。问用户数据库列表在哪，用户说了再用 `--url` 或 `--tab` 读那一页。
  - `--save` 写三处：`~/.litdl/portal.json`（读到的全部）、`~/.litdl/holdings.json` 的 `webvpn_hosts`（特殊写法）、`.env` 的 `LITDL_EBSCO_OPID`。收录表里哪家出版商走哪个库不会自动改，那要单篇试过才知道。
  - **`--library`：读图书馆的资源列表。** 路径只对 `scripts/schools.json` 里有的学校是现成的（按 WebVPN 域名查，目前只有中国石油大学（华东）：WebVPN 首页 → 图书馆 → 馆藏资源 → 免费资源，网址 `https://library-upc-edu-cn-s.<vpn>/dzzy.htm`）；别的学校只输出一句提示和 `NEED_USER`，由用户说出那一页在哪。这一页如果是汇文的资源导航（页面里有个 Vue 实例 `vm1`，每个库带 `package_status`：1 已购、2 试用、4 免费），脚本直接读出每个库的状态并列出免费的；不是这种系统的页面只按链接认数据库，没有状态。`--save` 时已经记过写法的站点不改（先记下的那种是用过的），并把资源列表存进 `~/.litdl/portal.json`，`LITDL locate` 读它来给每篇文章列对口的免费或试用资源。
  - **免费资源怎么用**：它们是各不相同的站点（NSTL、PubScholar、GoOA、全国开通的回溯库、出版商的开放获取专区等），没有统一的配方。上面的库都没有时，按 `locate` 列的去对口的那个，用 `LITB goto / text / click` 探；探通一个，把步骤补成本文件的一节。实测学校 11-21 读到 29 个免费资源，试了两个：**"INFORMS 期刊（NSTL）"** 的入口是老版 EBSCOhost 的资源选择页，点进去跳到的就是 `research.ebsco.com/c/<opid>/search`，和脚本一直在用的 EBSCO 是同一个库，没有多出来的内容；**NSTL 站点**是一个检索加文献传递的门户，有自己的登录，首页另有"全国开通数据库"一栏，没有往下探。其余 27 个没有试过。结论：免费资源这一步目前只能当线索用，不要向用户许诺能从这里下到。

## 3. EBSCO（INFORMS 等经管类期刊）

```
LITB fetch <manifest> --stage <dir> --via ebsco --bucket informs --dry-run
LITB fetch <manifest> --stage <dir> --via ebsco --bucket informs
LITB fetch <manifest> --stage <dir> --via ebsco --ids 80,81,82      # 别的桶也可以先查 EBSCO
```

脚本对每一篇做的事：

1. 打开带检索词的结果页：`https://research-ebsco-com-443.<vpn>/c/<opid>/search/results?q=<题名>&autocorrect=y`。页面会先跳转一次补上数据库参数，结果 20–25 秒才出来；脚本每 2.5 秒看一次，最多等 75 秒，跳转途中读不到就再读。
2. 结果里的题名链接是 `/c/<opid>/search/details/<记录id>`。页面上的题名和清单里的题名用同一个函数归一化（转小写、去重音、`&` 换成 and、破折号和弯引号等标点一律换成空格）后比对：**完全相同的优先**；其次接受只多了 "Technical Note" "Research Note" 这类前缀的、或者只是被截掉了副标题的；题名里多出 comments、reply、erratum、correction、reflections 这类字眼的**不要**——那是另一篇关于它的文章（2026-11-21 就把 2004 年的一篇评论当成了 1997 年的原文）。不取第一条，结果里常混进不相干的文章。11-19 有一篇题名是 "Machine learning–enabled …"（en dash），当时两边归一化的办法不一样，一边成了 `learningenabled`、一边是 `learning enabled`，被记成搜不到；**结果是 `not_found` 而"看到的"里有很像的题名时，先怀疑比对，不要直接记账。**
3. 同一条如果有全文，旁边有 `/c/<opid>/viewer/pdf/<记录id>` 的"立即获取 (PDF)"链接。这个链接比题名晚几秒渲染，脚本会再等最多 8 秒。没有就是只有题录（`citation_only`）。
4. 有全文就在页面里调接口取文件：`/api/search/v1/record/<id>/fulltext/pdf?sourceRecordId=<id>&opid=<opid>&intent=download&lang=zh-CN`。**取一篇要 40–180 秒，大的扫描件 4–5 分钟，这是 EBSCO 经 WebVPN 的正常速度，不是限流。** 单篇超时默认 480 秒。
5. 取到后查文件头和结尾的 `%%EOF`，完整才存成 `<序号>.pdf`。被截断的不存，结果记 `truncated`。

- **不用 EBSCO 的 PDF 阅读器**：经过 WebVPN 它会显示"页面不可用"（后台的 worker 返回 503），还把渲染进程卡死过。详情页的"下载"按钮有时卡在"正在准备您的文件"。所以一律调接口。
- EBSCO 里的文章有的是正式版，有的是 Just Accepted 稿，ingest 会标出来。

**收录情况（学校订的是 Business Source Premier；逐篇不定，每篇都要搜一次）：**

| 刊 | 实测 |
|---|---|
| INFORMS（MS、MSOM、OR、MKS、ISR、IJOC、IJAA 等）2001–2022 年 | 11-18、11-19 试的几十篇几乎都有全文 |
| INFORMS 2023 年及以后 | 不一定，逐篇搜。11-18、11-19 上午试的十几篇只有题录或搜不到；11-19 下午下到 MSOM 2026 三篇（Just Accepted 或正式版）和 MS 2026 一篇；10-22 下到过 2026 年的 MSOM 和 MS，11-07 下到过 ISR 2024 正式版和 ISR 2026 Just Accepted 稿。"2024 年以后大概没有"这个说法已经三次被证伪 |
| INFORMS 2000 年以前 | 不一定。1992、1993、1995、1996、2000 年的 7 篇只有题录；1999 年的 OR 有全文（6.6 MB 扫描件，取了 421 秒，扩展时代两次被截断） |
| AER | 有全文 |
| POM | 只有题录。改走 Wiley（第 4 节），有全文 |
| T&F（IJPR、JORS、PPC） | 只有题录（11-18 试了 8 篇；11-19 下午 21 篇里 20 篇只有题录，另 1 篇因 en dash 没比对上）。全文走第 7 节：先出版商的 WebVPN 入口，不行再校园 IP |
| Emerald（IJOPM、SCM） | 只有题录（11-18 试了 3 篇；11-19 下午 3 篇题录、1 篇搜不到） |
| SAGE（JM 2026、POM 2026） | 只有题录 |
| SS 2026 一篇 | 有全文，但连续两次被截断（5.5 MB、2.9 MB 处断开），交给用户手动下 |

有题录没全文、或者搜不到的，先换出版商入口和公开版本，都记了账仍没有，才按 SKILL.md 第 6 节和 `ablesci.md` 处理。

## 4. Wiley

```
LITB fetch <manifest> --stage <dir> --bucket wiley
```

1. 打开 `https://onlinelibrary-wiley-com-443.<vpn>/doi/<doi>`。学校订了的刊，题名上方写着 "Full Access"，不用人机验证。
2. 在页面里取 `/doi/pdfdirect/<doi>?download=true`，一篇 6–10 秒，大文件（4 MB 以上）要一两分钟。

- 跳到 `/doi/abs/`，或者返回 403 而页面没有 Full Access：学校没订这一刊（期），结果记 `no_access`。JOM 就是这样（2018 年和 2022 年各试过一篇）。不反复试。
- 学校订了的：POM（到 2023 年）、NRL。2024 年起 POM 转到 SAGE，学校没有入口。
- 第一次打开会弹 cookie 横幅，不影响取文件。必须点的时候选拒绝非必要 cookie。
- 直连 Wiley 官网（不经 WebVPN）有 Cloudflare；开放获取的 Wiley 文章走第 9 节。

## 5. Springer、IEEE

```
LITB fetch <manifest> --stage <dir> --bucket springer,ieee
```

打开文章页，然后在页面里取 `webvpn_pdf` 那个地址（Springer 是 `/content/pdf/<doi>.pdf`，IEEE 是 `stampPDF/getPDF.jsp?tp=&arnumber=N`）。Springer 一篇 1–2 分钟（经 WebVPN 比较慢）。

- Springer 返回的是网页：确认用的是第 1 节那个例外的域名形式。
- IEEE 在浏览器里取：2026-11-21 测过一篇，第一次请求 PDF 地址回的是 404 网页，过几秒同一个请求就给了文件；脚本遇到"不是 PDF"会等 7 秒再试一次。还不行时用第 11 节的 curl 备用通道。
- 文章页和 PDF 的地址在下载时按学校现在的网址写法现算，不用建清单时存下的。所以先建清单、后读门户（`LITB portal --save`）也没关系。

## 6. Elsevier / ScienceDirect

```
LITB fetch <manifest> --stage <dir> --bucket elsevier
```

**严格一篇一篇来，基础间隔 28 秒，每 6 篇歇几分钟，一轮最多 25 篇。**

1. 打开 `webvpn_page`（WebVPN 下的 ScienceDirect 文章页），等页面渲染。文章页本身弹验证时，和别的站点一样先松开浏览器重开再过（`verification.md` 第 1 节）。
2. 在页面上找 `/pii/<PII>/pdfft` 的链接（"View PDF"）。同时读权限标记：Full text access、Open access、Get Access、Purchase PDF。
3. 读出这个链接，**先松开浏览器**：脚本断开 Playwright，再经调试端口的 HTTP 接口在新标签页打开链接，这样不挂任何会话。页面跳到带签名的 `pdf.sciencedirectassets.com/…/main.pdf`，浏览器把文件下到 `~/.litdl/downloads`，脚本从那里接走，再接回浏览器，下一篇照常。
4. 当天第一篇通常要过两道验证：先是 Cloudflare 的 "Are you a robot?"（标签页标题"请稍候…"），过了以后是 Elsevier 自己的 "Request Verification: In Progress"（标题 "Security verification"）。脚本用系统鼠标点，点不过就请用户点，细节和开关见 `verification.md` 第 1、2 节。过了以后，同一个浏览器有一段时间不再弹验证（放行靠 `cf_clearance` 和 `CraftCapiVerify` 两个 cookie）。

- **PDF 地址不能自己拼。** 11-18 文章页经 WebVPN 打开后没有渲染出 `pdfft` 链接，直接访问拼出来的 `/pdfft` 地址得到的是 "There was a problem providing the content you requested"；第二天同一页面正常渲染出了链接。页面上找不到链接时结果记 `no_link`，过一阵再试或交给用户。
- **页面显示 "There was a problem providing the content you requested"**：学校出口 IP 被限了，通常是因为并发或连续访问，十来分钟后自动解除。脚本记 `blocked` 并停掉这一桶。期间别刷新；之后按 SKILL.md 第 4 节用另一篇试探。
  - 如果这时用户开着系统代理，也会是同样的症状，因为出口 IP 不是学校的了。页面上会印出它看到的 IP，拿来核对。
- **只显示 "Get Access" 或 "Purchase PDF"**：学校没订，结果记 `no_access`。
- **"View PDF" 链接比权限标记晚出现**：页面先显示 "Full text access" 和一个 "Finding PDF…" 占位，过几秒才换成链接。脚本最多等 30 秒。11-19 第一次只等了 12 秒，误判成没有链接。
- **PDF 页为什么会弹验证**：文章页经 WebVPN，但 View PDF 最后跳到的 `pdf.sciencedirectassets.com` 不经 WebVPN，站点看到的是电脑自己的出口。2026-11-21 试过把这个 PDF 地址的主机名也按 WebVPN 的写法改写，实测学校的 WebVPN 不代理这个主机，请求被弹回门户首页，此路不通。出口在海外时就弹验证；在校园网（国内出口）时不弹，11-19 晚一篇 21 秒下到。
- **弹了以后过不过，看浏览器上有没有挂着自动化**（2026-11-22 查清）：挂着 Playwright 时验证框不出现、页面每隔几秒自己刷新，或者框出来了点了也不放行；松开浏览器以后两道验证各点一次就过。11-19 当时把原因记成"出口 IP"，只对了一半。对比的经过和随后实测的 8 篇见 `test-log.md` 第 3 节。
- **走不走 Clash**：在家里没有区别，路由器和 Clash 出去的是同一个地址（11-22 实测）；在校园网上，走 Clash 会把国内出口换成海外的，本来不弹的验证就会弹出来。所以专用 Chrome 仍然默认直连。
- 挂着浏览器的老做法（`LITDL_HANDSOFF=0`）遇到验证时，最多等两轮就关掉那一页，结果记 `need_user`，并把页面看到的 IP 写进备注。

### 人在校园网时：Elsevier API

用户在校园网（不是 WebVPN）时，可以走 Elsevier API：

1. 调用 `scansci_pdf_elsevier_setup`，按向导申请免费的 API Key。不需要机构邮箱。
2. 调用 `scansci_pdf_config(key="elsevier_api_key", value=...)` 写入 key，再用 `scansci_pdf_elsevier_setup(test=true)` 验证。
3. 之后 `10.1016` 开头的 DOI 用 `scansci_pdf_download` 下载，每篇 1–2 秒，不需要浏览器。

- 用户不在校园网、只开着 WebVPN 时，这条路不通，因为本机的出口 IP 不是学校的。

## 7. T&F 与其他出版商：WebVPN 入口、校园 IP

**前提**：人在校园网（专用 Chrome 本来就直连，出口就是校园网）。学校的订阅在出版商侧按 IP 识别：T&F 页面会显示 "Access provided by <学校名>"。

- **在不在校园网不用问用户，配方自己看这行字。** 页面上没有 PDF 链接、也没有 "Access provided by …" 时，结果是 `no_campus`：出版商没认出学校，这个结果说的是网络，不是文章，不记进账本；这一轮后面的行跳过这个配方，改走下一个库。有这行字而没有 PDF 链接，才是 `no_access`（学校没订）或 `no_link`。这个判断是 2026-11-21 加的，还没在校园网上跑过。

- **tandf 配方**（2026-11-19 实测 17/18）：`LITB fetch … --via auto --bucket tandf`。每篇打开 `https://doi.org/<doi>`，等页面渲染；遇到人机验证见 `verification.md`；然后点页面上印着的 `Download PDF`（`/doi/pdf/…`），不自己拼地址。每篇 9–14 秒，17 篇约 8 分钟。
- **pubdoi 配方**（emerald、sage；也可 `--via pubdoi` 手动指定）：同样打开 DOI，找页面上的 `/doi/pdf/`、`/insight/content/doi/…/full/pdf`、`/pdfft` 或文字为 Download PDF 的链接。2026-11-19 在 Emerald/SAGE 试的两篇页面能开但没有 PDF 链接（学校无 IP 授权），返回 `no_link`。
- **失败处理**：页面只显示 Get Access / Purchase / Buy article → `no_access`，记 publisher 渠道；页面没渲染出链接 → `no_link`，不要自己拼 PDF 地址。
- **出口在海外时这两个配方到不了"认没认出学校"那一步**：T&F、SAGE 的官网直接弹人机验证（2026-11-21 各试一篇，都停在 Turnstile，复选框点了没过）。脚本把它当成和 `no_campus` 一样的情况：这一轮把这一行改送下一个库，以后各轮看到这一行的未了结记录是 `publisher`，也直接跳过这两个配方。`publisher` 一栏没有定论，所以这一行不会被算成"试尽"。

### 出版商的 WebVPN 入口：`vpnpub`

有的出版商在学校的 WebVPN 里有自己的入口，却没有专门的配方（Wiley、Springer、IEEE、Elsevier 有）。`vpnpub` 是给它们用的通用配方，**在哪个网络都能用**，比上面两个认校园 IP 的配方优先。

- **做法**：按学校的网址写法现算出文章的 WebVPN 页面地址（不用清单里存的旧地址），打开；跳到登录页记 `login`；页面上找 `/doi/pdf/`、`/full/pdf`、`/pdfft` 或 Download PDF 的链接（和 `pubdoi` 同一套），点它，接住下载。只显示 Get Access / Purchase 记 `no_access`（经 WebVPN 出版商看到的是学校的 IP，这是有定论的结果）。
- **安全验证过不去的出版商**：页面写着"无法连接到网站""安全验证过程无法完成"时不等人点，直接记 `blocked`。这种入口谁来点都过不去。
- **点了没下载时**：等 25 秒，改在页面里直接取那个链接，再不行就直接打开它。净室测试里三次跑有一次点 T&F 的 Download PDF 没有反应，配方空等满了 240 秒；手工取同一个链接 1 秒就下到。
- **哪家走它**：写在 `~/.litdl/holdings.json` 里，例如 `"tandf": ["vpnpub", "tandf", "ebsco"]`。先 `LITB fetch … --via vpnpub --ids <一个序号>` 试通一篇再写。Wiley、Springer、IEEE、Elsevier、INFORMS 的行脚本不会给它排 `vpnpub`，它们各有配方。
- **各出版商的间隔分开算**，一家被拦不影响另一家。

## 8. Sci-Hub

Sci-Hub 是正常的一个库（用户 2026-11-21："scihub也要纳入，取消灰线限制"），排在学校订的库后面、公开版本前面，账本里的名字是 `scihub`。配方 `scihub`，不指定 `--via` 时脚本自己会排到它：

```
LITB fetch <manifest> --stage <dir>                       # 轮到 Sci-Hub 的行自动走它
LITB fetch <manifest> --stage <dir> --via scihub --ids 44,45
```

**配方做的事**

1. 在专用 Chrome 里打开 `https://<镜像>/<doi>`。镜像默认依次试 `sci-hub.ru`、`sci-hub.st`、`sci-hub.se`，在 `~/.litdl/.env` 里用 `LITDL_SCIHUB=主机1,主机2` 改。打不开或返回 5xx 的镜像换下一个。
2. 出现"你是机器人吗？"这一页（ALTCHA 工作量证明）：和别的验证一样，先松开浏览器、重开这一页，用系统鼠标点页面上自己的"不是"按钮（`[onclick="check()"]`），等它算完、页面自己跳到文章页，再接回浏览器（`verification.md` 第 1 节；11-22 起，还没实测）。2026-11-21 挂着浏览器时实测普通点击 2 秒就过，之后同一个会话里的文章不再问；`LITDL_HANDSOFF=0` 时仍是这样点，40 秒没过才停下来请用户点。镜像前面挡着 Cloudflare 检查时也一样先松开再过。
3. 文章页里 PDF 嵌在 `<object data="/storage/…/x.pdf">` 里，另有一个同地址的下载链接。脚本在页内 fetch 这个地址（直接导航去下会被浏览器取消），取回来查文件头和 `%%EOF`。页内取失败（PDF 在另一个域名上，比如被转到 `sci-net.xyz`）时改为直接打开那个地址。
4. 页面写着"尚未收录""不可用"（标题是"该文章在 Sci-Hub 上不可用"）：结果 `not_found`，有定论，记进账本。

- **覆盖**：2022 年以后的文章基本没有，老文章多半有。它排在学校的库后面，所以新文章在它这里多花的只是几秒。
- **扫描件没有文字层**（老文章常见）时 `ingest` 会报 NOMATCH：把首页渲染出来核对，确认是本篇再 `ingest --force`（文件名必须是 `<序号>.pdf`）。
- **间隔** 12 秒，和别的库一样排队逐篇，不并发。
- **下到的文件只进用户自己的文献库**，不上传到科研通或别处（SKILL.md 红线）。实测记录在 `test-log.md` 第 4 节。
- scansci 自带的 Sci-Hub 通道不用：它默认走 Tor，而 Tor 没配，直接调用返回 no PDF。

## 9. 公开版本：知识库、SSRN、出版商开放页

```
LITDL alts <manifest>                                           # 先查有哪些公开版本
LITB fetch <manifest> --stage <dir> --via public --ids 79,83,98
LITB get --url <某个网址> --id 83 --stage <dir>                 # 单独指定一个网址
```

脚本对每一篇依次试 `alt_copies` 里的地址、`oa_pdf`、标为 OA 时的 DOI 页面，最多四个，同一个站点只试一次：

1. 像读者一样打开这个地址。直接是 PDF 的，浏览器下载，脚本接走。
2. 是落地页的，先找 `<meta name="citation_pdf_url">`，没有再给页面上的链接打分（地址像 PDF、文字里有 PDF 或 Download 的加分，像补充材料、许可说明的减分），跟着最像的那个走，最多走两步。
3. 遇到人机验证：先松开浏览器、重开这一页再过（`verification.md` 第 1 节），过了接着找 PDF；过不去就放到前台等用户，没人点就记 `need_user`，关掉那一页，本轮不再请求这个站点，别的站点照下。**同一篇在别的站点还有副本时，接着试下一个副本**（新开一个标签页）：2026-11-21 复测里 CityU 的副本弹验证，PolyU 上同一篇的副本直接就能下。
4. 落地页能打开、跟着链接到了文件页却提示限制访问（限校内、禁发期）：记 `no_access`，这是对这一份公开版本的定论，会并进账本。一上来就被拒（Access denied、Too many requests）记 `blocked`，本轮停这个站点，算未了结。

- **这些站点拦非浏览器请求**：curl、脚本直连、在别的页面里 fetch，返回 403、429、502（PolyU、MIT DSpace、CityU、Cardiff、Loughborough、Plymouth 都试过）。真实浏览器导航能过一部分：11-19 Plymouth PEARL 和 Loughborough Figshare 直接下到；Cardiff ORCA、CityU Scholars、Nottingham、tandfonline 的开放获取页弹 Cloudflare 验证；MIT DSpace 返回 429，隔 3 小时再试还是 429；港科大知识库（`repository.hkust.edu.hk`）的文件页提示限制访问；马斯特里赫特 CRIS 只有题录页，没有文件。
- **SSRN**：`papers.ssrn.com` 的摘要页有 Cloudflare 检查，挂着浏览器时多数要用户点一次（11-19）；11-22 起松开浏览器再点，实测点一次就过，之后同一个浏览器里接着访问不再弹。另外两种页面：
  - **注册提示页**（`Data_Integrity_Notice.cfm`，标题 "SSRN Alert"）：请求 `Delivery.cfm` 时会跳到这一页，左边是注册表单（带一个 Cloudflare 方框），右上角是 "Download without registration"。脚本点这个按钮，再点它展开的 "Download"，文件就来了（11-22 实测）。注册表单和它的方框不碰，不填任何东西。
  - **"Not Available for Download"**：作者没在 SSRN 放全文，`citation_pdf_url` 照样指向 `Delivery.cfm`，但那个地址只会跳回摘要页。脚本认出这句话就记 `no_access`（这份副本的定论），不再跟着链接空走。11-22 的 #119 就是这样。
  - 其余照旧：过了以后脚本按 `citation_pdf_url` 走到 `Delivery.cfm` 下文件，每篇 18–37 秒。**SSRN 按量限流**：11-19 连下 8 篇后第 9、10 篇返回 403，80 分钟后又回到验证页。脚本对它间隔 30 秒，并且一小时内下到 6 篇就停（输出里是 `CAP`），歇够一小时再跑。下下来的是工作论文版，要在汇报里标明。**有的文件只是 1 页摘要**（11-19 一篇 94 KB）：`fetch` 会在结果里提示"只有 1–2 页"，ingest 报 SUSPECT，看过首页不是正文就删掉，用 `LITDL mark` 把 `public` 记成"SSRN 只有摘要页"。
- **文件地址一直挂着不返回时，改从详情页进**：11-19 诺丁汉知识库（worktribe）的 `/file/<id>/1/…` 地址过了验证后一直在等响应；打开记录的详情页 `…/output/<id>`，点页面上的 Download 链接就下到了（`LITB click --tab … --text Download --download <stage>/<序号>.pdf`）。
- **Figshare 上挂的常是补充材料**（`tprs_a_*_sm*.pdf`、几十 KB）。下回来首页不是论文正文的，删掉，并用 `LITDL mark` 把这一行的 `public` 记成"只有补充材料"。
- **arXiv 和会议论文**：直接下 arXiv 版。CVF Open Access 上的 CVPR/ICCV 论文也能直接下。
- **作者的机构库**：`alts` 用的是 OpenAlex 的 locations；还可以查 OpenAIRE（`api.openaire.eu/search/publications?doi=...&format=json`）和 Semantic Scholar 的 `openAccessPdf`。
- 这些都不是正式排版版。ingest 会在"版本"列里写明，交付时要提醒。**顺序上先跑学校渠道（EBSCO、出版商），再跑公开版本**：`fetch` 默认只处理待下载的行，公开版本一入库这一行就不再去学校渠道了。已经先入了公开版本的，用 `LITB fetch … --via ebsco --ids <这些行>` 点名重走学校渠道（只有点了名、版本列不是正式版的行才放行），下到后 `ingest --replace` 换掉。

## 10. 没有现成配方时：用原语一步步做

```
LITB tabs                                             # 列出标签页，第一列是标签页 id（不接浏览器）
LITB goto <网址> [--tab <id>|new] [--wait 6]          # 打开，输出 HTTP 状态、标题、是否验证页、是否登录页
LITB text --tab <id> [--max 3000]                     # 页面可见文字
LITB eval --tab <id> "() => …"                        # 在页面里跑 JS，输出 JSON；长脚本写成文件用 @file.js
LITB shot --tab <id> --out <png>                      # 截图，再用 Read 看
LITB widgets --tab <id>                               # 已知验证控件 iframe 的屏幕框（含 closed shadow root）
LITB clickat --tab <id> --x 193 --y 303               # 点一个 CSS 像素坐标（我看图给的）
LITB drag --tab <id> --x1 … --y1 … --x2 … --y2 …     # 按住拖，带缓动弧线
LITB look --tab <id> [--out <png>]                    # 验证页上用这个：不接浏览器，看标题、验证控件、截图
LITB press --tab <id> --x … --y … [--to-x … --to-y …] # 验证页上用这个：不接浏览器，系统鼠标点或拖
LITB click --tab <id> --text "下载" [--download <stage>/<序号>.pdf]
LITB fill --tab <id> --selector "input[name=q]" --value "…"
LITB get --url <完整网址> --id <序号> --stage <dir> [--tab <id> --inpage] [--channel publisher|scihub|public]
LITB front --tab <id>                                 # 放到前台，请用户登录或点验证
LITB close <id> [<id> …]
```

- `--tab` 可以写 id 的前几位，也可以写网址或标题里的一段字。
- 每条命令接上浏览器、做完、断开；标签页留着，下一条命令接着用。没有单次执行的时间上限。例外是 `tabs`、`look`、`press`、`solve`：它们不接浏览器。**验证页上只用这几条**，`shot`、`widgets`、`clickat`、`drag` 一接上，验证就不放行了（`verification.md` 第 1 节）。
- **以 `/` 开头的参数会被 Git Bash 改写成本地路径**，`--url` 一律写完整的 `https://` 网址。
- 探通一个新站点后，把网址规律、权限标记、取 PDF 的办法写进本文件，并在 `test-log.md` 里记一行实测状态。
- 探的时候同样守节奏：一个站点一次一个请求，不为了试接口连发。

## 11. 备用：vpnfetch（curl）

```
LITDL vpnfetch <manifest> --stage <dir> --from-browser            # cookie 取自专用 Chrome，不用另外登录
LITDL vpnfetch <manifest> --stage <dir> --from-browser --dry-run  # 只列出要访问的网址
```

只处理有 `webvpn_pdf` 的行（ieee、springer）。11-06 用 scansci 的 cookie 做过 12 篇，不用人机验证。现在首选第 5 节的浏览器配方；IEEE 在浏览器里取不到时用它。

- 运行时先看门户页有没有 `sign_out`，没有就退出。
- cookie 临时写成 Netscape 格式文件，用完立即删除，不打印。
- 不加 `--from-browser` 时读 scansci 的 `~/.scansci-pdf/cache/instsci-cookies.json`，那要求用户另外在 scansci 自带的窗口里登录一次（`scansci_pdf_login(kind="webvpn")`），尽量不用。
- 间隔和停止规则同 SKILL.md 第 4 节，参数相同。
