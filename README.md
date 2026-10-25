# skill_repository

可复用的 Agent skills。

| Skill | 能力 |
|---|---|
| [pan-resource](skills/pan-resource/SKILL.md) | 网盘资源搜索、预览与转存 |
| [wechat-local-macos](skills/wechat-local-macos/SKILL.md) | macOS 微信本地消息读取、指定会话导出、语音提取和离线转写 |

## 微信本地消息 skill

将 `skills/wechat-local-macos` 整个目录安装到自己的 skills 目录，例如 `~/.codex/skills/wechat-local-macos`，然后使用 `$wechat-local-macos`。

运行环境和命令见 [读取与配置](skills/wechat-local-macos/references/reading.md)；语音依赖和本机模型见 [语音说明](skills/wechat-local-macos/references/voice.md)。普通读取需要 Python、SQLCipher、本机数据访问权限及已验证的私有数据库密钥；首次没有可用密钥时，才按 [可选引导流程](skills/wechat-local-macos/references/bootstrap.md) 操作。

示例请求：`使用 $wechat-local-macos 读取本机微信中某联系人的最近 20 条消息。`

真实密钥、账号配置、数据库、音频和聊天导出放在仓库外的私有目录，不能提交到此仓库。此 skill 不包含发送消息或自动营销功能。

## 本地验证

```bash
python3 -m unittest discover -s skills/wechat-local-macos/tests -v
python3 skills/wechat-local-macos/scripts/sqlcipher_probe.py self-test
```

测试使用合成数据库和消息，不访问真实微信。数据库测试需要 SQLCipher 动态库；语音转写是可选环境，不影响基础读取。
