# 环境、配置与消息读取

## 必需环境

- 当前登录用户的 macOS 微信 4.x 本地数据，先正常登录官方客户端使数据库落盘。
- 执行脚本的宿主可读取微信容器。遇到 `Operation not permitted` 时检查“系统设置 → 隐私与安全性 → 完全磁盘访问权限”，给实际执行宿主（如终端或 Codex）授权；修改后可能需要重新打开宿主。
- Python 3.10+；已验证 Python 3.12 和 3.14。3.14 可直接解 zstd；旧版遇到压缩消息需在自己的环境安装 `zstandard`。
- SQLCipher 动态库。脚本检查 `SQLCIPHER_LIBRARY`、Homebrew 的 `/opt/homebrew/opt/sqlcipher/lib/libsqlcipher.dylib`、`/usr/local/opt/sqlcipher/lib/libsqlcipher.dylib` 及系统库路径。缺少时可安装 `brew install sqlcipher`，无需为已有库重复安装。

诊断只读取版本与数据库文件名，不读取消息或密钥：

```bash
python3 scripts/doctor.py
python3 scripts/sqlcipher_probe.py self-test
```

`self-test` 只创建临时合成 SQLCipher 数据库，验证只读写入拒绝、已提交 WAL 可见、未提交记录不可见、错误密钥拒绝等，不修改真实微信。

## 私有配置与密钥

推荐状态根目录 `~/Library/Application Support/WeChatLocal`。skill 的调用者可读取该目录下 `config.json` 中的路径，再显式传给脚本；脚本不会自动遍历硬盘寻找密钥。

配置示意（用实际绝对路径替换，占位内容不作为实际配置执行）：

```json
{
  "version": 1,
  "db_root": "/absolute/path/to/selected-account/db_storage",
  "key_file": "/absolute/path/to/private/keys.json",
  "state_root": "/absolute/path/to/private/runtime",
  "voice_python": "/absolute/path/to/optional-voice-venv/bin/python",
  "voice_model": "/absolute/path/to/optional-local-mlx-model"
}
```

后两个字段仅语音转写需要。配置路径可以引用既有私有文件，无需复制密钥。多账号时，配置只表示用户已选定的那一个账号。

密钥文件格式支持 `{"keys": {"message/message_0.db": {"enc_key": "<64位十六进制>"}}}`，或以绝对数据库路径/数据库第一页 salt 为键的同类映射。值是 32 字节 raw SQLCipher key，不是登录密码。`load_key` 要求文件无 group/other 权限，且一个数据库只匹配到一个唯一密钥；不要把实际密钥放在说明示例中。新文件设置为 0600，目录为 0700。

当前 4.x 默认数据根目录为：

```text
~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<account>/db_storage
```

不提供 `--db-root` 时，仅在恰好一个候选账号时自动选择。提供后依旧验证密钥和数据库布局；不要凭相似目录名猜账号。

## 联系人与消息

以下以环境变量只保存路径（不保存密钥值）。`CHAT_ID` 必须来自唯一联系人解析或用户明确选择：

```bash
KEY_FILE="/absolute/path/to/private/keys.json"
DB_ROOT="/absolute/path/to/selected-account/db_storage"
CHAT_ID="<confirmed-contact-username>"

python3 scripts/wechat_db.py --key-file "$KEY_FILE" --db-root "$DB_ROOT" verify
python3 scripts/wechat_db.py --key-file "$KEY_FILE" --db-root "$DB_ROOT" resolve "联系人备注"
python3 scripts/wechat_db.py --key-file "$KEY_FILE" --db-root "$DB_ROOT" read --chat-id "$CHAT_ID" --limit 20
```

`read` 的 limit 为 1..200；多分片合并后按原始时间排序。stdout 会包含本次请求的消息内容，勿转发到公共日志。除密钥外的账号标识也是本地数据，不提交到 Git。

完整导出与可选时间范围：

```bash
python3 scripts/export_messages.py \
  --key-file "$KEY_FILE" --db-root "$DB_ROOT" --chat-id "$CHAT_ID" \
  --output "$HOME/Library/Application Support/WeChatLocal/exports/messages.json"
```

可提供 `--self-id '<confirmed-self-username>'`、`--after <inclusive-unix-seconds>`、`--before <exclusive-unix-seconds>` 和 `--timezone Asia/Shanghai`。JSON 保存消息、账号归属、类型、引用回复自身文字及未解析占位；stdout 只输出摘要。未知本人身份保持空值，不伪造人称。

XML 内的 URL、媒体密钥和被引用的历史消息不进入导出。类型 1 是普通文字；app message 类型低 32 位为 49 且 XML type=57 时，仅保留 title 中的本次回复。新格式无法解析时保留未解析状态，不直接把 XML 当聊天文字。

## 增量读取：基线、处理和确认

只在用户明确需要增量检查时使用，不自动启动后台任务：

```bash
STATE_FILE="$HOME/Library/Application Support/WeChatLocal/incremental/selected-chat.json"
python3 scripts/wechat_db.py --key-file "$KEY_FILE" --db-root "$DB_ROOT" init \
  --chat-id "$CHAT_ID" --self-id '<confirmed-self-username>' --state "$STATE_FILE"
python3 scripts/wechat_db.py --key-file "$KEY_FILE" --db-root "$DB_ROOT" check --state "$STATE_FILE"
```

`init` 以当前最大 local_id 为基线，不回放旧消息。`check` 不推进游标；下游按 `(database, local_id)` 去重，处理成功后，才用返回的 `ack_cursor` 执行 `ack --state "$STATE_FILE" --cursor '<JSON游标>'`。读取消息不等于处理成功；打印到终端不代表应 ack。

数据库被替换、salt 改变或游标倒退时停止并报告，保留旧状态；先核对官方迁移/账号切换再显式重建基线。不自动清空状态以绕过这类检查。

## 常见故障

| 表现 | 先核对 |
|---|---|
| 无数据库候选/Operation not permitted | 宿主 FDA、官方客户端是否已登录、有无本地记录 |
| expected exactly one account | 用户选定哪个 `db_storage` |
| 密钥匹配为零或不唯一 | 所选账号、原数据库 salt、密钥映射；不要删除旧文件 |
| SQLCipher open/query failed | FDA、库版本、SQLCipher格式及密钥；原始错误不输出秘密 |
| 最新消息缺失 | 是否误用只有主 `.db` 的副本、WAL 是否完整、官方同步是否完成 |
| 语音有索引但无内容 | 非空 blob 是否存在；不是必须能下载或重建 |

当前脚本不解析朋友圈数据，不发送微信消息。用户另外请求分析时，以消息上下文和明确证据为依据，并保留未解析媒体与音频缺口。
