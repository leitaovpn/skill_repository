# 本地语音提取与转写

只处理用户明确选定会话的本机缓存。缺少音频时保留缺失记录，不从上下文编造语音内容；不通过此流程发消息、自动回复、定时监控或额外生成画像。只有用户另行提出分析任务，才进入对应分析流程。

## 提取

使用已经验证、权限为 `0600` 的密钥清单；本脚本不提取密钥，不修改微信。`--chat-id` 必须先经联系人解析或用户提供确认，不可凭相似昵称选第一个结果。

```bash
python3 scripts/voice_export.py \
  --key-file "$HOME/Library/Application Support/WeChatLocal/private/keys.json" \
  --chat-id '<已确认的会话ID>' \
  --output-dir "$HOME/Library/Application Support/WeChatLocal/voice-export"
```

存在多个账号时提供 `--db-root`，缺省只允许自动选取唯一账号。只有已从已知本人消息确认账号 ID 后才提供 `--self-id`；未提供时，`sender` 一律为 `unknown`，`from_self` 为 `null`。`sender_id` 仍保留数据库已知账号用户名，不能据此猜测实际发声者。群聊、借账号录音、背景播报均须额外核对。

`voice_export.py` 使用 `wechat_db.Reader` 和 SQLCipher 的只读连接及 `query_only`，只查询目标 `Msg_<md5(chat-id)>` 表中 `local_type=34` 的消息；媒体匹配同时限定 `VoiceInfo` 的目标会话和精确 `svr_id`。不会遍历导出其他会话内容。多个分片是在不同时间读取，不能声称是跨文件的原子快照；必要时重复运行核对新增缓存。

输出目录必须在 Git 仓库及微信数据库目录外，目录 `0700`，文件 `0600`；拒绝输出路径中的符号链接。标准输出只含数量摘要，账号与消息 ID 只写私有 `manifest.json`。消息 XML 仅用于解析 `voicelength`（毫秒转秒），不保存 XML、`voiceurl`、`aeskey`。zstd 内容优先用 Python 3.14 的 `compression.zstd`；旧版本需已有的 `zstandard`，未安装时记录无法解析时长，不阻断音频提取。

状态含义：

- `audio_available`：至少一行非空音频，全部非空候选的 SHA-256 相同，保存至 `source-audio/<sha256>.silk`。
- `audio_missing`：无匹配索引，或所有匹配行的音频 blob 都为空。存在媒体索引行不等于存在音频。
- `ambiguous`：同一目标会话、同一 server ID 有多个不同非空 SHA-256；不任选、不导出冲突音频，待核对。

manifest 使用 `messages` 列表，保留 `message_id`、字符串 `server_id`、`timestamp`、`sender_id`、`from_self`、`sender`、秒数 `duration_expected` / `expected_seconds`、状态、`audio_sha256`、`audio_path`、`media_row_count`、`duplicate_media_rows`、`empty_media_rows` 和 `distinct_audio_candidates`。重复行数定义为匹配索引总行数减一（最低为零），因此不能拿它当非空音频数量。

重复运行会更新 manifest 并按哈希去重写入当前可用音频，不删除历史音频文件。请为不同目标会话使用不同输出目录。缺失音频只能在用户通过微信官方功能补齐本机缓存后再次提取；不要拿 XML 内的不透明媒体参数拼接下载地址。

## 本机转写

转写入口为 `scripts/transcribe_local.py`，使用 MLX Qwen3-ASR。显式提供本地模型路径，不为仅仅读文字默认下载模型或安装 ASR 环境，不上传音频或调用第三方语音 API。已有环境与模型优先复用；没有时，说明来源、下载量和存储位置后，在用户已授权的转写任务范围内准备。

已验证的一种配置是 Python 3.12、`mlx-qwen3-asr==0.4.4` 和 `moona3k/mlx-qwen3-asr-0.6b-8bit`，模型 revision `83ce2a8ef9a382d2ba2209aab383bc51fff366f5`，权重约 836 MB。它是可复现的已测配置，不承诺其他模型/版本相同表现。原生 Apple Silicon 使用 MLX，不适用于 Intel/Rosetta。

需要新环境时（已有时跳过）：

```bash
STATE_ROOT="$HOME/Library/Application Support/WeChatLocal"
python3.12 -m venv "$STATE_ROOT/voice-venv"
"$STATE_ROOT/voice-venv/bin/python" -m pip install -r scripts/requirements-voice.txt
"$STATE_ROOT/voice-venv/bin/hf" download moona3k/mlx-qwen3-asr-0.6b-8bit \
  --revision 83ce2a8ef9a382d2ba2209aab383bc51fff366f5 \
  --local-dir "$STATE_ROOT/model-qwen3-asr-06b-8bit"
```

这是模型下载，原音频不参与网络请求。之后离线转写：

```bash
"$STATE_ROOT/voice-venv/bin/python" scripts/transcribe_local.py \
  --manifest "$STATE_ROOT/voice-export/manifest.json" \
  --model "$STATE_ROOT/model-qwen3-asr-06b-8bit" \
  --output-dir "$STATE_ROOT/transcription" --language Chinese
```

`--language` 和 `--max-new-tokens` 可调整；不要在未核对实际语言时一律认定中文。输出 `voice-transcripts.json`、`progress.json`、`语音转写.md` 及本地 WAV/缓存。推理设置 Hugging Face 离线模式。Qwen 输出没有本脚本可用的词级置信分数；质量提示仅是复听线索，不是准确率评估。

微信 Silk 可用 `pilk.decode(..., pcm_rate=24000)` 解为单声道 24 kHz、16-bit PCM，再用标准库 `wave` 包装 WAV；不要只改文件扩展名冒充 WAV。机器识别如需 16 kHz，可用 NumPy 转为浮点数组，再用 SciPy `resample_poly(audio, 2, 3)` 重采样。`pilk`、NumPy、SciPy 与 MLX ASR 库必须在独立本地环境中已经安装；提取本身不需要这些包。

Apple Silicon 可选 MLX Whisper 或 MLX Qwen ASR。以每条消息的已知边界单独识别，或使用可验证边界的批处理；不要让相邻录音串成一段后猜说话人。语言与模型选择由实际录音决定，不把模型输出或置信指标当人工确认。

缓存键至少包含音频 SHA-256、模型标识/固定版本、识别参数；模型或参数变化时不得误用旧缓存。每条完成后原子写私有缓存，随后更新进度和汇总，断点续跑跳过已经成功的相同配置结果。保留消息 ID、时间、账号发送人、模型版本和本地原音频链接，便于复听；失败项与空音频不能标成已完成转写。

最终将结果区分为机器转写、`needs_review`、失败、缺音频或媒体冲突。无文本、异常重复、时间戳越界、明显背景播报、模型不一致或置信异常等均需复听；未标记也不保证正确。不从多个候选里挑最符合预设结论的一句，不擅自修正疑似错字，不推断语气、情绪强度、健康或其他私人事实。用户要求分析时，应以转写与上下文谨慎归属，并注明音频缺口及未人工复听的范围。
