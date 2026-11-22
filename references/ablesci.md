# 科研通：求助和应助

科研通（`www.ablesci.com`）是文献互助平台，在这个 skill 里有两种用法：**求助**，花用户的积分请别人传文件，是一篇文章所有库都试过以后的最后一步；**应助**，用开放获取文章帮别人，给用户攒积分。两种都由 `LITA`（`scripts/ablesci_assist.py`）在专用 Chrome 里做，科研通由用户先登录：`LITB login ablesci --wait 300`。什么时候可以去、每一批要先问用户什么，SKILL.md 第 6 节有摘要，细节在下面。

## 1. 发求助

**什么时候能去**：只有 `LITDL missing <manifest> --exhausted` 列出来的行，也就是能收录它的库都有了结果、仍没下到的。用户原话："你要多个库都尝试完了以后，确定去科研通下载"。`request-batch` 自己会查账本，有一行缺渠道就整批拒绝，一篇都不发。

- **先看一眼图书馆的免费资源。** `LITDL locate` 在某一篇下面列了"图书馆资源页里对口的"资源时，先去那里试，再谈花积分。这一步脚本不拦，靠我照做，试的结果用 `LITDL mark … --note` 记下。
- **花的是用户的积分**（普通求助每篇 10 积分，是最低悬赏，不调高）。发之前告诉用户这一批是哪几篇、每篇多少积分、合计多少，得到明确同意再发。同意只对说好的这一批有效，下一批重新问。`--budget` 填用户同意的积分数，要花的超过它脚本就拒绝。
- **用户同意之后跑 `request-batch`**，先加 `--dry-run` 看它打算发哪几行。它对每一行依次做四步，哪一步不干净就停在那一篇，后面的不发：
  1. `request`：站点按 DOI 提取出的卡片要和清单对得上（DOI 相同、题名吻合九成以上），才点发布；然后等人上传，用免费线路把文件下到 `~/.litdl/downloads`。
  2. `LITDL ingest … --src ~/.litdl/downloads --pattern "*ablesci.com*.pdf" --move` 核验入库。
  3. `LITDL mark … --tried "ablesci=已应助（日期）"` 记账。
  4. `adopt` 采纳。**只有第 2 步报了这一行的 `FILED` 才采纳。**
- **发一篇、处理一篇**：上一篇有人上传了文件而没处理，平台不让发下一篇。脚本就是按这个顺序做的，不要自己并行发。
- **`ingest` 报 `NOMATCH` 或 `SUSPECT` 时，不采纳也不驳回。** 先看首页文字和 PDF 里的 DOI 再决定：2026-11-21 有一篇文件是对的，只是首页印着"[DOI 10.1108/…]"，核验把"]"算进了 DOI（已修）。文件确实不对才到求助详情页驳回，并告诉用户。
- **到时间没人应助的**（`WAITING`）：求助留在站上，积分已经扣了，清单里记着求助 id。过后再跑同一条 `request-batch --ids <那几行> --budget 0`，它对已经发过的行只取文件、不重发；或者单独 `LITA fetch-upload --req <id>`，再照上面第 2–4 步做。
- **别人传来的常是 SSRN 版或录用稿**，`ingest` 会在版本列里记下，交付时告诉用户。

### `request-batch` 怎么把关

一批几篇时用 `request-batch`，它把下面"单篇"的四步按顺序串起来，一行做完再做下一行：

```
LITA request-batch <manifest> --ids 53,55,58 --dest <库> --budget 30 --dry-run   # 只查行和预算、列计划，不开科研通
LITA request-batch <manifest> --ids 53,55,58 --dest <库> --budget 30 [--wait 420]
```

- **发之前整批检查，有一行不合格就一篇都不发**：行不在清单里；已经下到了；账本里 `ebsco`、`publisher`、`public` 缺哪一条；没有 DOI；账本里已有科研通记录但读不出求助 id。
- **`--budget` 是用户同意的积分数**。新发的篇数 × 10 超过它就拒绝。
- **已经发过求助的行不重发**：账本里 `ablesci` 一栏有"求助 id xxx"的，只去看有没有人上传。所以没人应助的行过后再跑一遍同样的命令就是"补取"，`--budget 0` 即可。
- **停的条件**：`request` 的结尾不是 `DOWNLOADED` 或 `WAITING`；`ingest` 没有报这一行的 `FILED`（文件留在 `~/.litdl/downloads`，求助不采纳也不驳回）；`adopt` 没有报 `ADOPTED`；发出去了却读不到求助 id。停下时后面的行都不发，输出以 `STOP` 开头说明停在哪。
- `WAITING` 的行：自动记 `ablesci=已发求助，还没人应助（日期，求助 id …）`，接着发下一行；每发一行之前和全部发完之后，各回头看一次这些行有没有文件。
- 结尾报还在等人应助的行、余额和清单的总数。

### 单篇、补取、采纳

单篇，或者 `request-batch` 停下以后接着手工做：

```
LITA request --doi <doi> --title "<清单里的题名>" [--wait 600]   # 发一篇（-10 积分）→ 等人上传 → 免费线路下到 ~/.litdl/downloads
LITA request --doi <doi> --title "<清单里的题名>" --dry-run      # 只走到站点提取出卡片、和题名比对；不发布、不扣积分
LITDL ingest <manifest> --src ~/.litdl/downloads --pattern "*ablesci.com*.pdf" --move --dest <库>
LITDL mark <manifest> --ids N --tried "ablesci=已应助（日期）"
LITA adopt --req <request 打印的 req>                            # ingest 报 FILED 之后才采纳
LITA fetch-upload --req <req>                                    # request 等到超时、后来才有人上传的：补下文件
```

- `request` 发之前自己查三件事：余额够 10 分；"待确认"里没有没处理的文件；站点提取出的卡片上 DOI 相同、题名和 `--title` 吻合九成以上。不满足就不发。
- `request` 不采纳，`adopt` 不核验。中间那步 `ingest` 没报 `FILED #N` 就停下：文件留在 `~/.litdl/downloads`，看过再决定采纳还是到详情页驳回。
- 结尾是 `DOWNLOADED`（下到了）、`WAITING`（`--wait` 秒内没人上传，求助留在站上）、`TIMEOUT`（下载没完成）、`SUSPECT`（卡片对不上，没发）、`CHECK`（点了发布没读到成功提示，先去"我的求助"看，不要重发）。
- 页面选择器：一键求助框 `#onekey`，提取按钮 `button.onekey-search`，卡片和成功提示都在 `.layui-layer` 里，"信息正确，直接发布"是 `.layui-layer-btn0`；待确认列表每行一个 `input[name=assistID]`（值是求助 id）和一个 `a[href*="/assist/download"]`；批量采纳按钮 `#accept-assist-batch`，确认框的"确定"也是 `.layui-layer-btn0`。

### 手工做时的页面步骤

脚本出问题时照着用 `LITB` 原语走。这些步骤 2026-11-18 用 Chrome 扩展实测过，2026-11-21 在专用 Chrome 里测通后写成了上面的命令。

1. **登录**：`LITB login ablesci --wait 300`，用户自己输密码。
2. **看余额、签到**：`LITA points`、`LITA signin`。页面上是这样的：首页右侧的签到栏（`.fly-signin`）写着"当前拥有 N 积分"（`#user-point-now`），旁边是"今日打卡签到"按钮，每天一次，10 积分（遇到过双倍 20）。签到会弹一个结果框，点"关闭"。签过以后按钮消失，签到栏只剩"已连续签到 N 天"（`#sign-count`）；积分记录（`/my/point`）里多一行"`<日期 时间>` +10 签到"，`signin` 以这一行为准判断今天签没签、签上没有。
3. **发求助**：打开 `https://www.ablesci.com/assist/create`，在"一键求助"框里填 DOI，点"智能提取文献信息"，等约 8 秒。弹出的卡片上有题名、DOI、期刊、作者，**逐项和 manifest 核对**，都对了再点"信息正确，直接发布"。悬赏默认 10 积分，是最低值，不调高，除非用户说了。
4. **发下一篇之前必须先处理上一篇**：上一篇已经有人上传文件而没处理，发布页会变成"处理提示"，不让发新的。所以是发一篇、处理一篇。
5. **取文件**：打开 `https://www.ablesci.com/my/assist-my?status=uploaded`，用 `LITB eval` 读出下载链接 `a[href*="/assist/download"]` 里的 `id`，再用 `LITB goto https://www.ablesci.com/assist/download?id=<id> --tab new` 打开下载页。
6. **下载页**会自动选普通线路开始下。普通线路免费，只有 5–15 KB/s，3 MB 的文件要 4–6 分钟，6 MB 的要二十分钟。"高速通道"要扣 2 积分，不用。中途弹"下载持续较慢"就点"继续当前下载"。页面可能被整页广告盖住，点右上角"关闭"。没有脚本连着时文件落在 `~/.litdl/downloads`，文件名是 `<题名>(科研通-ablesci.com).pdf`；或者用 `LITB click --tab … --text … --download <stage>/<序号>.pdf` 让脚本接走。
7. **核验入库**：`LITDL ingest <manifest> --src ~/.litdl/downloads --pattern "*ablesci.com*.pdf" --move --dest <库>`。题名、DOI、页数都对上才算数。然后 `LITDL mark <manifest> --ids N --tried "ablesci=已应助（日期）"`。
8. **采纳**：核验通过后回到第 5 步的列表页，勾选这一行，点"批量采纳所选项"，再点"确定"。文件不对就不采纳，进求助详情页按提示驳回，并告诉用户。驳回还没有实际做过，页面长什么样到时现看。
9. 每篇从发布到采纳大约 6–8 分钟，时间主要花在免费线路下载上，这段时间可以做别的桶。

## 2. 积分：签到和应助

- **签到**：`LITA signin`，一天一次（+10，偶尔 +20）。用户 2026-11-21 定的做法：哪天我本来就要在科研通干活（发求助、应助、查积分），就顺手签一次，签完报"+几分、余额多少"；不为了签到单独去开专用 Chrome，不设定时任务。今天签过了它只报 `ALREADY`；输出 `CHECK` 时不连着点，先看页面。
- **应助**（用户说"帮我在科研通赚点积分"）：只应助带再分发许可的开放获取文章。脚本只从 OpenAlex 给出的公开地址取文件，它没有上传本地文件的入口，清单里经学校订阅下到的 PDF 不走这条路。
- **一次一篇，每一批先 `--dry-run`**，把标题、DOI、许可证、来源、页数报给用户，用户同意这一批再去掉 `--dry-run`。上传是对外发布，撤不回；传错了被驳回会记在用户的账号上。**"去应助""把积分攒够"这类笼统的话不算对某一批的同意**（见下面"注意"）。
- **产出很低，要照实告诉用户**：合格的求助一两分钟内就被别人答掉，等用户看完清单多半已经没了（`assist` 上传前会再查一次，不是求助中了就报 `SKIP`）。所以攒积分主要算每天的签到，应助适合用户在场、能马上回话的时候做。不要许诺"今晚能攒够"。
- **积分要等求助者确认才到账**，不是上传就有；48 小时没人确认，站点按应助成功处理。`LITA points` 里"应助被采纳 (id=…)"才算数，汇报时把"已到账"和"待确认"分开说。

```
LITA points [--last 8]
LITA signin [--dry-run]
LITA scan [--pages 3] [--max 8] [--visit 40] [--by-points] [--skip id1,id2] [--rescan] [--allow-nc]
LITA assist --req https://www.ablesci.com/assist/detail?id=<id> --doi <doi> [--dry-run] [--allow-nc] [--timeout 200] [--human-wait 45]
```

**顺序**（用户 2026-11-21 定的，见本节末"注意"）：`scan` 找候选 → 每条 `assist --dry-run`（文件下好、核验过，停在上传之前）→ 把这一批的标题、DOI、许可证、来源、页数报给用户 → 用户同意 → 逐条 `assist`。等用户回话的这段时间里求助常被别人答掉，`assist` 会在上传前再查一次并报 `SKIP`，这是正常结果，不为了抢时间跳过给用户看这一步。

**能应助什么**：只有 OpenAlex 标了许可证、给了 PDF 地址的开放获取文章。默认认 `cc-by`、`cc-by-sa`、`cc0`、`public-domain`，`--allow-nc` 再加 `cc-by-nc`、`cc-by-nc-sa`、`cc-by-nc-nd`、`cc-by-nd`。文件只从这些公开地址现下，脚本不接受本地文件。

**页面结构**（2026-11-19 实测）

| 用途 | 地址或选择器 |
|---|---|
| 是否登录、余额 | `https://www.ablesci.com/my/point`：没登录会跳到 `/site/login`；登录后页面上有"您当前的总积分为： N"和积分历史 |
| 求助中的列表 | `https://www.ablesci.com/assist/index?status=waiting&page=<n>`。每条是一个 `li`，题名链接 `a[href*="assist/detail"]`，文字里有"`<积分>` 求助中"或"待确认"；已有人上传的带 `.assist-file-id-val` |
| 详情页 | `https://www.ablesci.com/assist/detail?id=<id>`。标题以【求助中】【待确认】【已完结】开头；正文里有 DOI；求补充材料的写着"求助补充材料" |
| 上传 | 详情页里的 `input[type=file]` 选文件，等约 3 秒站点算完 MD5，点 `#upload-bth`（"确认上传"） |
| 结果提示 | `.layui-layer-content`："上传成功，感谢您的帮助，我们已通知求助者前来确认。" 点 `.layui-layer-btn0` 关掉 |

**`scan` 做的事**：读前几页"求助中"列表，留下状态是求助中、还没人上传的，从最新的开始逐条打开详情页（`--by-points` 改成积分高的在前；`--skip id1,id2` 跳过处理过的），取 DOI，跳过求补充材料的，查 OpenAlex；有许可范围内的公开版本就输出一行 JSON（详情页、积分、DOI、许可证、期刊、题名）。只读，不下载、不上传。详情页之间隔 1–2 秒，最多看 40 条。查过而没有合格公开版本的求助记在 `~/.litdl/assist/scanned.txt`，下次扫描不再打开，所以反复扫只看新出现的求助；改了 `--allow-nc` 之后加 `--rescan` 重查一遍。查询出错的不记，结尾会报"有 N 条没查成"。

**`assist` 做的事**，按这个顺序，任何一步不满足就停：

1. 查 OpenAlex：这个 DOI 有没有许可范围内、带 PDF 地址的公开版本。没有就退出。脚本自己连不上 OpenAlex（DNS 不通之类）时，改由专用 Chrome 打开同一个 API 地址来查。
2. 打开详情页：标题里还有"求助中"，求的不是补充材料，页面上的 DOI 和 `--doi` 一致。
3. 用专用 Chrome 依次试前三个公开地址（`nav_fetch`，和 `--via public` 同一套），文件存到 `~/.litdl/assist/<doi>.pdf`，查文件头和 `%%EOF`。 PMC 的地址（OpenAlex 给的是 `…/articles/PMC<号>/pdf/`，打开是个没有 PDF 链接的中间页）改成先开文章页 `…/articles/PMC<号>/`，从 `citation_pdf_url` 取真实文件名再下。
4. 核验：不少于 3 页；题名里的词在**第一页**能找到六成以上；第一页开头几行不是补充材料或检查表的抬头（Supplementary …、Supporting Information、Multimedia Appendix、checklist）。不满足就报 `SUSPECT`，不上传。只看第一页是因为补充材料常在第二页重复文章题名（见下面 11-19 被驳回的那篇）。
5. 再打开一次详情页：下载的这一会儿常被别人答完。不是求助中了就跳过。
6. `--dry-run` 到这里为止。否则选文件、点确认上传、读提示，结果追加到 `~/.litdl/assist/log.jsonl`。

**注意**

- **开放获取的求助几分钟内就被答掉。** 11-20 一次扫出 6 条候选，逐条处理时 4 条已经是待确认。所以从最新的扫起，一批不要攒多（`scan --max 2` 或 `3`），扫到就马上演练、马上报给用户；积分高而挂了很久的，多半是没有公开版本的。
- **从别的程序接 DOI 时去掉回车符**：Windows 上 Python 输出的行尾是 `\r\n`，bash 的 `read` 会把 `\r` 留在 DOI 里。脚本已经对 `--doi` 去空白。
- **`LITA scan` 候选数是 0**：多半是真没有；也可能是查询本身不通，看结尾有没有"有 N 条没查成"。脚本连不上 OpenAlex 时会改由专用 Chrome 查。
- **不抢"待确认"的。** 已有人上传、在等求助者确认的请求，不再传一份。
- **不写循环批量传。** 11-20 晚和 11-21 早的那几轮没有照"一次一篇、先演练"做：用户说了"继续在科研通应助，把积分攒够"之后，用一个临时循环扫到一条就直接传（传上的 3 篇都被采纳了）。用户 11-21 被问到时选的是**保留先演练、看过再传**。那个循环没有收进 skill，以后也不照那样做。
- 上传时站点会自动去掉 PDF 里的下载单位、IP 等信息（它的提示里这么写），不用自己处理文件。
- 下下来的文件留在 `~/.litdl/assist/`，不进用户的文献库。
