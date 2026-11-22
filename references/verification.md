# 人机验证

遇到人机验证、要配视觉模型、或者要我当视觉模型时读本文件。脚本先自己试，过不去才停下来请用户点。账号、密码、短信验证码不在这里面，一律用户自己来。

## 1. 总规矩：先松开浏览器，再过验证

**用户 2026-11-22 定的**："如果遇到验证，都用这个方式过，就是松开脚本再过"。所有站点的验证都这样处理，不再挂着浏览器去点。

- **为什么**：2026-11-22，同一个 Chrome、同一个海外出口、同一篇 ScienceDirect 文章，脚本挂在浏览器上时验证点了不放行；松开浏览器以后两道验证各点一次就下到了，之后实测 8 篇全部下到（`test-log.md` 第 3 节）。海外出口决定弹不弹验证，挂没挂自动化决定过不过。验证页是在加载时判断有没有自动化的，点完再断开不够（第 5 节第一条）。
- **怎么做**（代码是 `litbrowser.py` 的 `get_past` 和 `handsoff_fetch`）：
  1. 配方读页面时发现是验证页，就记下地址，把这一页换成空白页（挂着自动化加载的这一次已经作废），断开 Playwright。
  2. 经调试端口的 HTTP 接口（`/json/new`）在新标签页重开同一个地址，这样不挂任何会话。地址里 Cloudflare 写进去的 `__cf_chl_…` 参数先去掉。
  3. 等这一页：自己放行的（Cloudflare 常这样）不用点；有已知复选框（Cloudflare Turnstile、reCAPTCHA、hCaptcha）或 Sci-Hub 的"不是"按钮的，系统鼠标点；点选、滑块这类，配了视觉模型时截图交给模型，系统鼠标照着点或拖。看页面只用一个不开 Runtime 的裸 DevTools 连接，每次连一下就断。
  4. 都不行就输出 `NEED_USER`，把那一页放到前台请用户点，并写出标签页 id。这时浏览器是松开的，用户点的才算数（挂着浏览器时用户点了也不放行）。
  5. 页面过了验证（标题和页面元素里都没有验证了，隔 4 秒再看一次还是这样），脚本接回浏览器，配方在**这个标签页**上接着做。验证后面直接是文件的，文件落到 `~/.litdl/downloads`，脚本接走，这一篇就算下到。
- **哪些地方**：所有配方——EBSCO、Wiley、Springer 和 IEEE、Elsevier 的文章页和 PDF 页、T&F 官网、出版商官网（`pubdoi`）、WebVPN 里的出版商（`vpnpub`）、公开版本（知识库、SSRN 等）、Sci-Hub 的机器人确认页——还有 `LITB get`。点了 PDF 链接以后才出来的验证也一样处理。ScienceDirect 的 PDF 页更进一步：读出 View PDF 链接后，不等验证出现就先松开浏览器再打开（`publisher-recipes.md` 第 6 节）。
- **一篇最多过三次验证**，第四次出现就不再重开，交给用户。
- **开关**：`LITDL_HANDSOFF=0` 退回挂着浏览器的老做法（第 4 节）。关了自动过验证（`--no-auto-captcha`、`LITDL_CAPTCHA=0`）或者不在 Windows 上时，脚本一下也不点，只松开浏览器、把页面放到前台等用户点。需要 Python 的 `websockets` 包，没装时自动退回老做法。
- **会抢鼠标**：点的时候系统鼠标会移过去，专用 Chrome 会跳到前台。用户正在用电脑（打游戏、开会）时会被打断，所以开始一轮下载前最好跟用户说一声。
- **只点自己的窗口**：窗口按浏览器进程和标签页标题找，标题对不上不点。用户正在用别的程序时，Windows 常常不让后台程序切前台；这时只有当要点的那个屏幕点上露出来的就是专用 Chrome（没被别的窗口盖住），才照点。Chrome 自己弹的小窗（中文页上的"翻译此页？"）不算挡住。
- **这一轮跑着的时候，不跑会接上浏览器的命令**：`LITB status`、`shot`、`goto`、`text`、`eval`、`click`、`widgets`、`clickat`、`drag`、`hoverat`、`front`、`close`，以及 `LITA` 的命令。它们一接上，浏览器就又挂着自动化了。不接浏览器的照常用：`LITB tabs`（读调试端口的标签页列表）、`look`、`press`、`solve`，以及只读文件的 `progress`、`wait`、`pause`。
- **只认这一篇的文件**：松开期间，下载文件夹里新出现的文件才算；ScienceDirect 只认文件名带这篇 PII 的（`1-s2.0-<PII>-main.pdf`）。文件很大、到时限还在下时，等待会顺延（最多多等 10 分钟），不报成验证没过。
- **实测情况**：ScienceDirect 的 PDF 页 11-22 实测过（11 篇）。推广到所有站点后，11-22 晚实测了 ScienceDirect、SSRN、INFORMS 官网的 Cloudflare 检查和 Sci-Hub 的机器人页，都过了；T&F、EBSCO、Wiley、Springer 上的还没碰到（`test-log.md` 第 1 节）。

## 2. 点几次、等多久

- 验证页出来 5 秒后才点第一下（太早点不算数）；点完等 12 秒看页面变没变，再决定要不要补点。同一页最多点两次，一篇最多四次。ScienceDirect 先后有两道验证（Cloudflare 的"请稍候…"，然后 Elsevier 自己的 "Security verification"），各算一页。
- **点哪里**：Turnstile 标准 300×65 卡片的方框中心约在 `(left+16, top+0.36h)`；reCAPTCHA、hCaptcha 在 `(left+0.095w, top+0.5h)`；Sci-Hub 的"不是"按钮点正中。控件在 closed shadow root 里也找得到（`DOM.getDocument(pierce=True)`）。
- **没有已知控件时**：配了模型，页面出来 8 秒后让模型看截图，最多 `LITDL_CAPTCHA_ATTEMPTS` 次（默认 3），每次隔 12 秒；模型说没有验证控件或给不出能做的操作，就不再试。没配模型，40 秒后叫用户。模型能做两类：
  - **点选 / 语义选择**：读页面上的提示（"请依次点击：…"、"Select all images with …"），按顺序点文字或图块，需要时再点确认按钮。
  - **滑块 / 拼图**：定位把手、拼图块和缺口，按"缺口中心 − 拼图块中心"的水平距离拖过去；拼图块不可见时按"把手 → 缺口"拖。
- **等多久**：重开的页面 90 秒内既没过、也没到叫人的时候，就算卡住；叫了用户以后再等 `--human-wait` 秒（默认 300）。没人点就把这一篇记成"等用户点人机验证"，关掉那一页，这个站点这一轮停下（公开版本只停这一个站点）。
- **长得像验证、其实不是的页面不点**：SSRN 的注册提示页（标题 "SSRN Alert"）上也有 Cloudflare 方框，但它是注册表单的一部分；这一页走它自己的 "Download without registration" 按钮（`publisher-recipes.md` 第 9 节）。一个很长的正文页里嵌着一个验证框，也不当成验证页。
- **不点的东西**：reCAPTCHA 右下角那个 `size=invisible`、地址里带 `badge=` 的 iframe 不是验证，点它反而会把正常页面点出一个挑战。模型说是复选框而页面上没有已知 iframe 时也不点。
- **只做三件事**：按坐标点击、按住拖动、把光标移过去。不填账号，不读短信，不碰密码框。

## 3. 模型配置，以及我当视觉模型

```
LITB status                          # 最后一行显示 AI 过验证是开是关
LITB solve --tab <id>                # 对某个正在等人的标签页手动试一次（不接浏览器）
LITB fetch … --no-auto-captcha       # 这一轮不点，松开浏览器直接等用户
LITB fetch … --captcha-attempts 2    # 模型每页最多试几次（默认 3，最多 6）
```

**模型配置**放在 `~/.litdl/.env`（真实环境变量优先），任意 OpenAI 兼容端点：

```
LITDL_CAPTCHA_API_KEY=...
LITDL_CAPTCHA_BASE_URL=https://api.openai.com/v1
LITDL_CAPTCHA_MODEL=gpt-4o-mini
```

也可以直接用 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`。**没有 key 时只点已知复选框和 Sci-Hub 的按钮**，`LITB status` 会显示"只点已知复选框"。另有几个开关：`LITDL_CAPTCHA_ATTEMPTS`（默认 3）、`LITDL_CAPTCHA_TIMEOUT`（模型请求的秒数，默认 60）、`LITDL_CAPTCHA=0`（整个关掉，验证全交给用户，Sci-Hub 页面上自己的"不是"按钮也不点）、`LITDL_CAPTCHA_OS_MOUSE=0`（不用系统鼠标，这时脚本一下也不点）。用带视觉能力的模型；纯文本模型会在第一步就失败。模型服务要能访问，需要代理时给进程配 `HTTPS_PROXY`，或把 `LITDL_CAPTCHA_BASE_URL` 指向可达端点。

**我在场时不需要配模型**，我直接当视觉模型用。`NEED_USER` 那一行和 `LITB progress` 都写着标签页 id。验证页上只用不接浏览器的命令：

```
LITB look --tab <id> --out <临时文件>.png         # 标题、验证控件和建议的点击位置、页面文字；--out 存截图，我读图
LITB press --tab <id> --x 193 --y 303            # 系统鼠标点一个 CSS 像素坐标
LITB press --tab <id> --x … --y … --to-x … --to-y …   # 按住拖（带缓动弧线）
LITB press --tab <id> --x … --y … --hover        # 只把光标移过去（滑块拼图常需悬停才出现）
```

截图是设备像素，`press` 要的是 CSS 像素：`look` 输出里的 `image_px_per_css` 就是两者之比（屏幕缩放 200% 时是 2，图上的坐标除以 2 再给 `press`）。`shot`、`widgets`、`clickat`、`drag`、`hoverat` 会接上浏览器，只用于不是验证的页面，或者老做法（第 4 节）。

配置模型只是为了让 `fetch --detach` 这种**我不在循环里的无人值守轮次**也能过点选和滑块。

## 4. 老做法：挂着浏览器（`LITDL_HANDSOFF=0`，或没装 `websockets`）

```
进入验证页 → 先等最多 8 秒（Cloudflare 常自己放行）
→ 有已知复选框 iframe：系统鼠标点方框 → 等 3–6 秒
→ token 字段（cf-turnstile-response / g-recaptcha-response / h-captcha-response）非空，
   或连续两次看不到验证控件 → 算过
→ 没有或没过：截图 → 模型返回 JSON 坐标 → 换算 → Playwright 点击/拖拽
→ 连续两次看不到验证控件，或模型看新截图判定通过 → 继续原流程
```

- 代码是 `litbrowser.py` 的 `wait_human` 和 `captcha_ai.solve`。每次最多 `LITDL_CAPTCHA_ATTEMPTS` 次，每次重新截图定位；已知复选框最多点一次，不会连点。
- 失败就回人工：模型返回 `none`、坐标不合法、执行出错、判定没通过，都立刻返回，把页面置前等用户，页面不改动、不刷新，留给用户点。滑块失败会复位，不影响用户手点。
- 复选框走系统鼠标，窗口用浏览器进程 PID 锁定，PID 对不上就退回 CDP 鼠标。Sci-Hub 的"不是"按钮用 Playwright 点。
- 这条路在海外出口下多半过不了 Cloudflare 的托管挑战（第 5 节），只在松开浏览器的做法用不了时才用。

## 5. 已知边界

- **Cloudflare 的托管挑战（managed challenge）会检测自动化。** CDP 注入的点击能被区分；系统鼠标点击能通过交互式 Turnstile（2026-11-19 用 Cloudflare 官方强制交互测试 key `3x00000000000000000000FF` 实测，点后返回 SUCCESS）。但 managed challenge 还看浏览器 profile、出口 IP 和有没有挂着自动化：同一晚在 T&F 和诺丁汉实测，系统鼠标点中方框后控件进入"正在验证…"，光标留在控件上、点完再断开 CDP 等 25 秒，仍然拿不到 token。11-22 在 ScienceDirect 上查清，点完再断开不够，要从验证页加载起就不挂（第 1 节）。
- **先看出口，再怀疑点击。** 2026-11-19 走 Clash（出口在美国）时，T&F、诺丁汉的验证点了始终无 token；同一天在校园网直连（Cloudflare trace 显示 `loc=CN`）后，T&F 的 DOI 页直接是开放获取正文，诺丁汉的挑战自己通过。所以遇到 managed challenge，优先换出口（校园直连、手机热点），而不是反复调鼠标。重启 Chrome 会掉 WebVPN 登录，登录仍由用户本人完成。
- **别重复点，也别连续刷。** 失败次数多了会把 profile 和出口 IP 的风控分拉高（T&F 最后直接 `ERR_BLOCKED_BY_RESPONSE`）。managed challenge 只试一到两次。2026-11-19 实测 SSRN（`abstract_id=4346526`）：Turnstile 在 closed shadow root，CDP 定位到 (176,280,300×65)，点中方框中心 (193,303)，转圈后复位；断开 CDP 15 秒再让它验证也一样（那时是挂着浏览器加载的）。
- **但刷新一次能救回来**：同一个 SSRN 页面，经用户同意后 `LITB goto <同一网址> --tab <id>` 刷新，直接返回 HTTP 200 正文页，没有验证。松开浏览器的做法本身就是重开一次；重开以后还过不去，就交给用户，不再自己反复重开。
- **重开等于对这个地址多请求一次。** 挂着浏览器加载的那一次作废，松开后再开一次。按站点排队的间隔照旧，一篇最多三次。
- 拖拽是带缓动的贝塞尔弧线，不做完整的人体运动仿真；带轨迹分析的验证（部分极验、易盾高级版）可能过不了。
- 点选类靠模型认字形和图块；提示是图片也能读，但模糊、扭曲、遮挡时准确率下降。
- **风险自担**：代点可能触发更严格的验证，也会给账号和学校出口 IP 留风控记录。连续两轮没过就停手，不要靠加次数硬撞。
