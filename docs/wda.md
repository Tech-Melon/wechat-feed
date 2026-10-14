# 和 WeChatDataAnalysis 的关系

本仓库**不拷贝** [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis)（下称 WDA）的源码，也不把它当 git 子模块带进来。需要 WDA 时，请到它自己的 GitHub 下载开源版（不要用 `advanced-` 开头的高级版）。

## WDA 只做一件事：取钥匙

微信 4.x 的库是加密的。第一次启动时打开 WDA、完成解密，本服务向它的本机接口（默认 `http://127.0.0.1:10392`）要一次库密钥和图片密钥，写进 `data/direct.json`。

`wechat-feed.cmd status` 显示数据来源为 `direct` 之后，就可以把 WDA 关掉。之后由本项目自己的轻量只读逻辑盯微信库文件，有写入才读新消息。

还需要再开 WDA 的情况：微信换了账号导致密钥失效（`status` 变成 `wda`）、或者从来没解开过群图片。

## 轻量监控是本项目自己的

取到钥匙之后：

- 只读打开微信库（不写入）
- 看库文件有没有变，变了才查新消息
- 群图片按本机已下载的 `.dat` 解开

这套读库、解图写在 `src/wechat_feed/wechatdb.py`、`wximage.py`、`direct.py`，不是从 WDA 仓库拷过来的。WDA 继续负责「怎么从微信进程里拿出钥匙」这种重活。

## 不要做的事

- 不要把 WDA 源码复制进本仓库
- 不要把 WDA 安装包打进发布压缩包
- 不要把 `data/direct.json` 发给任何人（等同于能读你全部聊天记录）
