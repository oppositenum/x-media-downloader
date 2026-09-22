# 视频下载器

`yt-dlp` 直接贴推文链接时，敏感帖会拿到 `TweetTombstone`。这个工具走 FxTwitter / VxTwitter 解析媒体直链再下载。

也支持黑料网、海角网的 `/archives/` 详情页：页面里的 DPlayer HLS（AES-128）会自动解析、解密、转成 MP4。海角永久站 `hjw01.com` 直连会被 Cloudflare 重置，界面里填本机代理（例如 `http://127.0.0.1:7890`）即可，网页本身免登录。

## 可视化界面（推荐）

```bash
python3 xdl.py --serve
```

浏览器会打开 `http://127.0.0.1:8787`：

- 左侧粘贴一条或多条推文 / 黑料网 / 海角网 archives 链接，看实时进度（百分比、速度、剩余时间）
- 右侧是已下载列表，点封面即可播放
- 可取消任务、换保存目录、在访达中显示、删除文件

默认保存到项目下的 `downloads/`。

界面左侧可配置下载代理，支持：

- `system`：跟终端环境变量走（`https_proxy` / `http_proxy`）
- `direct`：强制直连
- `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`

点「测试」会探测连通性；设置会写进 `.xdl-settings.json`，下次启动还在。

## 命令行

```bash
# 单条推文
python3 xdl.py "https://x.com/user/status/123"

# 黑料网 archives 页（HLS）
python3 xdl.py "https://l4yh5.pelscmoa.cc/archives/115738/"

# 海角网 archives 页（免登录，永久站走本机代理）
python3 xdl.py --proxy http://127.0.0.1:7890 "https://www.hjw01.com/archives/193648/"

# 只要 720p
python3 xdl.py -q 720 "https://x.com/user/status/123"

# 批量
python3 xdl.py -f urls.txt -o ~/Movies/x

# 指定代理
python3 xdl.py --proxy http://127.0.0.1:7890 "https://x.com/user/status/123"

# 只看格式
python3 xdl.py --info "https://x.com/user/status/123"
```

文件名形如 `用户-推文ID-1440p.mp4`。已经下过的文件会跳过。

依赖：Python 3.9+。有 `yt-dlp` 更好，没有也能下。封面抽取需要本机 `ffmpeg`。海角网页面抓取需要 `pip3 install curl_cffi`（浏览器 TLS 伪装）。
