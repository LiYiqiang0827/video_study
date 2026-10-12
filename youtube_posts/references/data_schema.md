# Posts 快照字段

`posts.json` 是 UTF-8 JSON，顶层字段：

- `schema_version`: 当前为 `1`。
- `channel_url`, `collected_at`, `collector`: 来源、采集时间和解析器名称。
- `post_count`, `posts`: 去重后的帖子条目。

每个帖子包含 `post_id`、`url`、`channel_url`、`published_time_raw`、`published_timestamp`、`text`、`attachment_type`、`image_urls`、`video_ids`、`playlist_ids` 与 `downloaded_images`。YouTube 只返回类似“2天前”时必须原样保留，不能擅自换算成精确发布时间。若页面结构没有帖子 ID，则使用正文 renderer 的 SHA256 前缀形成可重复 ID，并在 `post_id` 前标识 `sha256-`。

`downloaded_images` 记录归档相对位置、字节数、SHA256 或下载失败类型。脚本只下载 ytimg、googleusercontent、ggpht 的 HTTPS 图片；关联视频只保留链接和 ID。帖子可能只对登录/会员账号可见，未能访问不等于频道没有帖子。

yt-dlp Posts tab 接口不是 YouTube Data API 的稳定 Posts API。本板块改写 yt-dlp 的私有 renderer 钩子以保留帖子文字，因此必须在版本升级后跑离线测试，并对实际频道做小样本验收。
