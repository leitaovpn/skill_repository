#!/usr/bin/env python3
"""Export only one selected WeChat conversation's local voice payloads, read-only.

No key discovery, playback, downloads, ASR, sending, monitoring or profiling.
IDs and message metadata go to private files; stdout contains counts only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import xml.etree.ElementTree as ET

MAX_XML_BYTES = 4 * 1024 * 1024


class ExportError(RuntimeError):
    """An error code safe to show without paths, account IDs or message bodies."""


def literal(value: str) -> str:
    return "CAST(x'" + value.encode("utf-8").hex() + "' AS TEXT)"


def voice_duration(raw: bytes) -> tuple[float | None, str]:
    """Extract only duration, never voice URLs, AES keys or the original XML."""
    if raw.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            try:
                from compression import zstd
            except ImportError:
                import zstandard as zstd
                raw = zstd.ZstdDecompressor().decompress(raw, max_output_size=MAX_XML_BYTES)
            else:
                with zstd.ZstdFile(__import__("io").BytesIO(raw)) as stream:
                    raw = stream.read(MAX_XML_BYTES + 1)
        except ImportError:
            return None, "zstd_decoder_unavailable"
        except Exception:
            return None, "zstd_decode_failed"
    if len(raw) > MAX_XML_BYTES:
        return None, "xml_too_large"
    try:
        text = raw.decode("utf-8", "strict")
        # Received group messages can have a sender prefix before the XML.
        offset = text.find("<")
        if offset < 0:
            return None, "xml_missing"
        xml = ET.fromstring(text[offset:])
        voice = xml if xml.tag == "voicemsg" else xml.find(".//voicemsg")
        if voice is None:
            return None, "voicemsg_missing"
        duration_ms = int(voice.attrib["voicelength"])
        if duration_ms < 0:
            return None, "invalid_duration"
        return duration_ms / 1000.0, "parsed"
    except (ValueError, KeyError, UnicodeError, ET.ParseError):
        return None, "invalid_voice_xml"


def classify_media(payloads: list[bytes]) -> dict:
    """Empty index rows are missing audio; conflicting payloads are ambiguous."""
    unique = {}
    for payload in payloads:
        if payload:
            unique.setdefault(hashlib.sha256(payload).hexdigest(), payload)
    status = "audio_missing" if not unique else "audio_available" if len(unique) == 1 else "ambiguous"
    digest, payload = next(iter(unique.items())) if len(unique) == 1 else (None, None)
    return {
        "status": status,
        "audio_sha256": digest,
        "payload": payload,
        "media_row_count": len(payloads),
        "empty_media_rows": sum(not payload for payload in payloads),
        "duplicate_media_rows": max(0, len(payloads) - 1),
        "distinct_audio_candidates": len(unique),
    }


def private_directory(path: Path) -> Path:
    """Reject links and repository locations before creating private state."""
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ExportError("output_symlink_rejected")
        if (component / ".git").exists():
            raise ExportError("output_must_be_outside_git_repository")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ExportError("output_directory_not_owned")
    path.chmod(0o700)
    return path


def private_write(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise ExportError("output_symlink_rejected")
    fd, temporary = tempfile.mkstemp(prefix=".voice-export-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def export_voice(reader, chat_id: str, output_dir: Path, self_id: str | None = None) -> dict:
    """Use Reader's read-only connections; return an ID-free summary.

    A supplied reader makes selected-chat behavior independently fixture-testable.
    Neither a missing self-id nor a known sender ID is evidence for a 'her/me' label.
    """
    output_dir = Path(output_dir).absolute()
    db_root = Path(reader.root).resolve()
    if output_dir.resolve().is_relative_to(db_root):
        raise ExportError("output_must_be_outside_wechat_database_root")
    output_dir = private_directory(output_dir)
    audio_dir = private_directory(output_dir / "source-audio")
    messages = []
    for database, table in reader.paths(chat_id):
        # Reader.paths constructs this table from md5(chat_id), not user SQL.
        if not table.startswith("Msg_") or len(table) != 36 or any(c not in "0123456789abcdef" for c in table[4:]):
            raise ExportError("unexpected_message_table")
        with reader.open(database) as db:
            rows = db.query(
                'SELECT m.local_id,m.server_id,m.create_time,n.user_name AS sender_id,'
                'hex(m.message_content) AS body FROM "' + table + '" m '
                'LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id '
                'WHERE m.local_type=34 ORDER BY m.local_id'
            )
        for row in rows:
            expected, parse_status = voice_duration(bytes.fromhex(row.get("body") or ""))
            sender_id = row.get("sender_id")
            from_self = sender_id == self_id if sender_id is not None and self_id is not None else None
            messages.append({
                "message_id": f"{Path(database).name}:{int(row['local_id'])}",
                "database": Path(database).name,
                "local_id": int(row["local_id"]),
                "server_id": str(row["server_id"]) if row.get("server_id") is not None else None,
                "timestamp": int(row["create_time"]),
                "sender_id": sender_id,
                "from_self": from_self,
                "sender": "unknown" if from_self is None else "self" if from_self else "other",
                "duration_expected": expected,
                "expected_seconds": expected,
                "duration_parse_status": parse_status,
            })
    messages.sort(key=lambda m: (m["timestamp"], m["database"], m["local_id"]))
    server_ids = sorted({m["server_id"] for m in messages if m["server_id"] is not None})
    media = defaultdict(list)
    media_databases = sorted((db_root / "message").glob("media_*.db"))
    for database in media_databases:
        with reader.open(database) as db:
            if not db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='VoiceInfo'"):
                continue
            # Both chat and server ID must match. A shared server ID in another
            # conversation cannot supply this conversation's audio.
            for offset in range(0, len(server_ids), 300):
                selected = ",".join(literal(value) for value in server_ids[offset:offset + 300])
                rows = db.query(
                    'SELECT CAST(v.svr_id AS TEXT) AS server_id,hex(v.voice_data) AS body '
                    'FROM VoiceInfo v JOIN Name2Id n ON n.rowid=v.chat_name_id '
                    'WHERE n.user_name=' + literal(chat_id) +
                    ' AND CAST(v.svr_id AS TEXT) IN (' + selected + ')'
                )
                for row in rows:
                    media[str(row["server_id"])].append(bytes.fromhex(row.get("body") or ""))
    written = set()
    for message in messages:
        matched = classify_media(media.get(message["server_id"], []))
        payload = matched.pop("payload")
        message.update(matched, audio_path=None)
        if payload is not None:
            digest = message["audio_sha256"]
            target = audio_dir / (digest + ".silk")
            if digest not in written:
                private_write(target, payload)
                written.add(digest)
            message["audio_path"] = str(target)
    counts = dict(Counter(m["status"] for m in messages))
    summary = {
        "ok": True,
        "total_messages": len(messages),
        "status_counts": counts,
        "unique_audio_files": len(written),
        "duration_parsed_messages": sum(m["duration_parse_status"] == "parsed" for m in messages),
        "unknown_sender_messages": sum(m["sender"] == "unknown" for m in messages),
        "media_databases_checked": len(media_databases),
        "audio_uploaded": False,
        "read_only_database_access": True,
    }
    manifest = {
        "version": 1,
        "chat_id": chat_id,
        "self_id": self_id,
        "summary": summary,
        "messages": messages,
        "notes": [
            "sender identifies an account, not a verified voice identity",
            "duration_expected is seconds from XML voicelength in milliseconds",
            "duplicate_media_rows is max(0, matched index rows minus one), including empty rows",
            "ambiguous payloads are not exported or selected",
            "live database shards are read-only but do not form one atomic cross-file snapshot",
        ],
    }
    private_write(output_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", type=Path)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--self-id")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from wechat_db import Reader, root_for
        summary = export_voice(Reader(root_for(args.db_root), args.key_file), args.chat_id, args.output_dir, args.self_id)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as error:
        # Do not echo raw exceptions: XML, paths and IDs may be private.
        code = str(error) if isinstance(error, ExportError) else "voice_export_failed"
        print(json.dumps({"ok": False, "error": code, "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
