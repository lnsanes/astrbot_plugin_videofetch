# astrbot_plugin_videofetch

给 AstrBot 的两个视频相关工具：**拉取视频**和**总结 UP 主动态**。

## 工具

| 工具 | 作用 |
|---|---|
| `fetch_video` | 给一个 B 站/抖音链接，返回标题、UP、时长、简介；可选直接下载并发送文件 |
| `bili_dynamic` | 拉某个 UP 主最近的动态，做摘要 |

## `fetch_video` 的分工

```
B 站  → yt-dlp 直连（信息 + 下载都行）
抖音  → 交给外部的 douyin-api 服务（带登录态的 Chromium 拦 XHR 取 play_addr）
```

**为什么不自己抓抖音**：抖音网页版的视频详情在 XHR 响应里，不在 SSR 数据里；而请求需要 `a_bogus` 签名，靠 cookie 拼不出来。自己实现等于重写一遍签名算法，且随时会失效。

所以插件只负责**调用**一个已经在跑的 `douyin-api` 服务，不重复造轮子。

## 跨容器发文件的坑

AstrBot 和 NapCat 跑在两个独立容器里，文件系统不共享。直接把容器内路径交给 OneBot 会得到 `ENOENT`——因为真正 `open()` 那个文件的是 NapCat 进程。

解法：

```python
shared = Path("/shared_audio")
if shared.is_dir():                  # 两个容器都挂了这个卷
    shutil.copy2(src, shared / src.name)
    os.chmod(shared / src.name, 0o644)
    file_path = str(shared / src.name)
```

更彻底的办法是给 NapCat 也挂上 AstrBot 的整个 workspace 目录树，让同一个路径字符串在两个容器里指向同一个文件。

## 配置

```json
{
  "douyin_api_base": "http://127.0.0.1:8899",
  "download": true,
  "max_size_mb": 100
}
```

- `download`：是否真的把文件下载下来并发出去（关闭则只返回信息）
- `max_size_mb`：超过这个大小不发文件，只给信息

## 依赖

- `yt-dlp`（B 站）
- 一个可达的 `douyin-api` 服务（抖音）
- `/shared_audio` 共享卷（发文件时）

## 许可

MIT