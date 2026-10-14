# 技术瓜群流

把本机微信 4.x 里**你已经能收到的群发言**，做成一条本地信息流：本地网页、gmgn 推特监控页插入、白名单网页浮窗三种看法。群里出现合约（CA）、星标用户发言或有新消息时，可以分别响不同的提示音。

只读本机数据，不发消息、不上传聊天内容。许可证 [MIT](LICENSE)。

作者：[@0xTechMelon](https://x.com/0xTechMelon) · 更多产品：[docs.jishugua.top](https://docs.jishugua.top)

- 第一次安装、日常启停、常见问题：[docs/使用指南.md](docs/使用指南.md)
- 打包发给别人：[docs/发布清单.md](docs/发布清单.md)

## 公开使用前请先读这三件事

### 1. 它只读你自己电脑上的微信

适配 Windows 上的微信 `4.1.13.65` 这一代 4.x 桌面版。服务只监听 `127.0.0.1:8765`，局域网里的其他设备访问不到群消息。不要把 `data/` 文件夹发给任何人：里面有库密钥、GMGN Key 和近 30 天的消息缓存。

### 2. WeChatDataAnalysis（WDA）是干什么的

本项目**不拷贝 WDA 源码**。需要 WDA 时到它的 GitHub 自行下载开源版。说明见 [docs/wda.md](docs/wda.md)。

微信 4.x 的本地库是加密的。第一次打开开源版 [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis)，按它的提示完成解密并保持开着。本服务向 WDA 的本机接口要一次库密钥（以及图片密钥），写进 `data/direct.json`。`wechat-feed.cmd status` 显示数据来源为 `direct` 之后，就可以把 WDA 关掉。之后用本项目自己的轻量只读逻辑盯微信库，不再依赖 WDA。

请用开源版，不要用文件名以 `advanced-` 开头的高级版。本仓库不会附带 WDA 安装包，请到它自己的 GitHub 下载。

还需要再开一次 WDA 的情况：微信换了账号导致密钥失效（`status` 变成 `wda`）、或者从来没解开过群图片。

### 3. 浏览器插件不上架商店

插件以 **Chrome / Edge 开发者模式 → 加载已解压的扩展程序** 安装，选仓库里的 `extension` 文件夹。不会上架 Chrome 网上应用店。

不上架的原因：浮窗要出现在你自己加白名单的网站上，扩展权限按站点动态注册；商店审核这条既麻烦也没必要。Chrome 每次启动可能提示「停用开发者模式扩展程序」，点「保留」即可。

## 快速上手

1. 安装 [uv](https://docs.astral.sh/uv/)（PowerShell）：

   ```powershell
   powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   装完后重新打开命令行窗口。

2. 克隆或解压到固定目录，例如 `D:\wechat-feed`，进入该目录：

   ```powershell
   .\wechat-feed.cmd setup
   ```

3. 打开开源版 WDA，完成解密，保持它开着。

4. 启动本服务并设为开机自启：

   ```powershell
   .\wechat-feed.cmd install
   .\wechat-feed.cmd status
   ```

   数据来源显示 `direct` 后即可关掉 WDA。本地页面：`http://127.0.0.1:8765/`。

5. 安装插件：打开 `chrome://extensions`（Edge 是 `edge://extensions`）→ 打开「开发者模式」→「加载已解压的扩展程序」→ 选项目里的 `extension` 文件夹。点开插件弹窗，不再提示「本地服务没连上」即成功。

## 服务管理

| 命令 | 作用 |
| --- | --- |
| `wechat-feed.cmd start` | 后台无窗口启动 |
| `wechat-feed.cmd stop` | 停止 |
| `wechat-feed.cmd restart` | 重启（改了代码或配置后用） |
| `wechat-feed.cmd status` | 是否在跑、内存、数据来源、是否开机自启 |
| `wechat-feed.cmd install` / `uninstall` | 开启 / 取消开机自启 |
| `wechat-feed.cmd logs` | 看最近 40 行日志（`data\logs\wechat-feed.log`） |

开机自启是在 Windows「启动」文件夹放一个 `wechat-feed.lnk`，登录后用 `.venv\Scripts\pythonw.exe -m wechat_feed serve` 无窗口启动。想看实时输出就前台跑 `uv run wechat-feed serve`（先 `stop` 掉后台的，两个会抢 8765 端口）。

## 功能

- **本地页**：分页加载、悬停屏蔽群、点「不看此人」屏蔽某个爱刷屏的人、点名字/群名写备注、星标关注的人、全部/只看 CA（两套独立的群来源多选，在「群聊选择管理」里勾）。每个监控群可展开成员，默认监听全员，也可只听部分人，并在这里星标或屏蔽。监控群消息全盘入库，显示层再按规则过滤。合约先走 GMGN search 定链和基本信息，再补 token/info；DexScreener 只作打底。EVM 地址 search 同时覆盖 bsc / eth / robinhood，命中即可，不必盲探。钱包地址会标成「这是钱包地址，不是代币合约」。合约识别覆盖 EVM、Solana、Tron、Sui、TON，正文里的合约显示完整地址并带「复制」。
- **gmgn 插入**：在 `https://gmgn.ai/follow` 把微信卡片按时间戳插进推特信息流。`gmgn_main.js` 在页面主环境读出每条推文的 `tw_timestamp` 写到 DOM 上，`content.js` 把落在相邻两条推文之间的消息插到较旧那条上方，比最新推文还新的放在顶部（最多 20 条）。跟随 gmgn 的亮色 / 暗色主题。插入流自己的全部 / 只看 CA，和本地页、浮窗分开。
- **白名单网页浮窗**：只在插件弹窗「浮窗白名单」里列出的网址上出现（默认 `https://gmgn.ai/follow?target=xTracker`），可拖拽、缩放。浮窗自己的全部 / 只看 CA 可与 gmgn 插入不同。匹配规则：协议、域名、路径一致，白名单里写出的参数也一致，网址多出来的参数不影响。插件脚本静态只注入 gmgn.ai，白名单里的其他站点由后台用 `chrome.scripting` 动态注册，不在名单上的网页完全不加载插件。
- **gmgn / 浮窗卡片样式**：簇顶一行（小头像、发言人、群名），按类型着色（普通消息微信绿、含合约红色、星标用户用星标色）。同一人同一群连续发言（5 分钟内）只在最新一条显示身份，后面只留正文。点群名可改群备注；写过备注的名字带蓝色荧光笔底纹，备注过的群是蓝描边加粗胶囊。鼠标移到簇顶换成星标、「不看此人」和「不看此群」。底部一行是时间（今天只显示时分秒，更早的加月-日，悬停看完整日期），簇顶附带水印「𝕏 @0xTechMelon」（可点开推特），以及整条消息的「复制」。
- **群图片**：直读模式下按消息的 `packed_info_data` 找到 `msg/attach/<群md5>/<月份>/Img/<文件>.dat`，用图片密钥解开（V1 固定密钥、V2 AES-128-ECB + 尾段异或、老格式整份异或）。列表里显示缩略图 `_t`。鼠标悬停或点击放大时优先用 `_h` 高清版；原图 `.dat` 多是微信的 wxgf（HEVC），浏览器显示不了会跳过。本服务只读本机已经下载的文件，不会向微信 CDN 拉原图——要高清请先在微信里点开一次。若高清还没下完，放大时仍用缩略图，且不会把缩略图当成高清缓存。图片密钥第一次用时从 WDA 桌面版的本机配置 `%APPDATA%\wechat-data-analysis-desktop\output\account_keys.json` 读出，存进 `data/direct.json`，之后 WDA 不用开。新消息到达时缩略图可能还没下载完，插件会在 15 秒内重试几次。早先存下的图片卡片会在服务启动回拉后自动补上图片。
- **提示音**：插件弹窗里分三类开关，各有 3 段内置音并都可以换成自己的音频（3 MB 以内）：合约（中文语音「注意，群里有新合约」、叮咚、急促三连音）、星标用户（清亮钟声、竖琴上行、中文语音「星标用户发言了」）、普通消息（仿微信的木琴叮咚、气泡、轻敲）。一条消息只响一种，优先级合约 > 星标用户 > 普通消息，前一类没开就往下找。听得见的范围跟群来源一致：只勾了「只看 CA」的群，没有合约的普通消息不响。由插件后台的离屏文档播放，开多个标签页也只响一次；同一条只响一次；1.5 秒内同级或更低级的不再响，更重要的可以插队；自己发的、服务启动回拉的旧消息（超过 2 分钟）、屏蔽的群和屏蔽的人都不响。内置音频由 `scripts/make_sounds.py` 生成。

以上共用同一份本机配置（群备注、用户备注、星标、Key）。群消息不会发给 gmgn 或其他网站。

## 数据来源

### 直读模式（默认）

服务启动时先向 WDA 要一次库密钥，写进 `data/direct.json`，之后直接只读打开微信的 SQLCipher 4 库：每 0.3 秒看一眼 `message_N.db` 和 `-wal` 的修改时间和大小，变了才按 `local_id` 增量查新消息，读到就立刻推给页面。实测常驻约 100 MB 内存、空闲约占一个核的 1.6%。群名、群昵称、备注、头像、群列表也直接读 `contact.db`、`session.db`、`head_image.db`。

消息从发出到出现在页面上约 1.5–2 秒，绝大部分是微信自己把消息写进本地库的时间；本服务这一侧最多再加 0.3 秒。

密钥失效（微信换账号等）时自动退回 WDA 模式，并每 30 秒重试取密钥。`/api/health` 的 `source` 显示当前是 `direct` 还是 `wda`。设 `WECHAT_FEED_DISABLE_DIRECT=1` 可以强制只用 WDA。

### WDA 备用模式

已指定监控群时，只订阅 WDA 的库目录变更 SSE（`scope=chat`），库文件有写入才去拉这几个群的最新消息，两次拉取至少间隔 1.5 秒。WDA 每轮拉取约 1.8 CPU 秒，间隔再短只会让 WDA 一直满载，消息并不会更快。用这个模式时不要开 WDA 的 AI、语音、朋友圈实时同步。

### GMGN 限频

每秒已用权重不超过套餐权重（免费版 5）：`market/search`、`token/info`、`token/security`、`token/pool` 每次扣 1，`token/holders`、`token/traders` 每次扣 5。先无链 search 定链或标钱包，未命中的 EVM 再按 bsc / eth / robinhood 补 search，然后查 token/info，不再对 token/info 盲探。按 1 秒滑动窗口排队，收到 429 先冷却 8 秒。升级套餐后设环境变量 `WECHAT_FEED_GMGN_RATE=<新权重>`。

## 边界

- 服务只监听 `127.0.0.1:8765`；跨源只放行浏览器扩展，普通网页读不到群消息。
- 上游只能是本机回环地址。
- 直读只用 `mode=ro` 打开微信库，不写入。
- 图片只读本机已下载的文件，语音只显示时长，不向微信 CDN 拉文件。
- `data/` 里是本机私有数据：`feed.db`（近 30 天卡片）、`settings.json`（配置和 GMGN Key）、`direct.json`（库密钥，等同于能读全部聊天记录）、`logs/`。不要外传，打包脚本也不会带上。

## 开发

```powershell
uv sync                     # 含 pytest、ruff
uv run pytest -q
uv run ruff check src tests scripts
```

- 本地页和插件共用 `extension/feed.js`，本地页通过 `/shared/feed.js` 读同一个文件。
- 改完插件代码后到 `chrome://extensions` 点「重新加载」；改完后端代码执行 `wechat-feed.cmd restart`。
- `scripts/direct_e2e.py`：在 8799 端口起临时实例，上游指向不存在的端口，验证关掉 WDA 后仍能工作，并测内存和 CPU。
- `scripts/direct_compare.py`：把直读生成的卡片和 `feed.db` 里已有的卡片逐条对比。
- `scripts/package.py`：检查版本号一致后生成 `dist/wechat-feed-<版本>.zip`。
- `scripts/make_sounds.py`：重新生成内置提示音。
- `scripts/make_icon.py`：重新生成产品图标（`web/favicon.*`、`extension/icon.*`）。

## 插件自测清单

1. 本地页只出现约 30 条，滚到底部会继续加载。
2. 点用户名/群名写备注后，名称变蓝斜体并带「备注」标记；点☆选颜色后该用户卡片有对应描边。
3. 弹窗保存 `gmgn_` 开头的 Key 后，状态变成「Key 已保存在本机服务」。
4. 打开 gmgn 插入，刷新 `https://gmgn.ai/follow?target=xTracker&chain=bsc`，微信卡片按时间戳插在推文之间；切换 gmgn 亮色 / 暗色，卡片跟着变。
5. 打开浮窗，只在白名单网址上出现并可拖拽缩放；其他网页（包括 gmgn 的其他页面）不出现。
6. 弹窗里三类提示音各点一次「试听」都能听到；打开合约提示后，本地页点「插入一条样例」（带合约）会自动响一次。
7. 群里发一张图片，gmgn 卡片和浮窗里几秒内出现缩略图，鼠标悬停有大图预览；簇顶卡片底部有固定水印「𝕏 @0xTechMelon」。
8. 本地页从「只看 CA」切到「全部消息」应马上换列表，没有整页空白再转圈。
9. 「群聊选择管理」里两套来源都可以同时勾多个群；插件弹窗里可以 gmgn 只看 CA、浮窗看全部。
