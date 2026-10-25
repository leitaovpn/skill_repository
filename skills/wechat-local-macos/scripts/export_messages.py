#!/usr/bin/env python3
"""Export one selected WeChat conversation; do not export media credentials."""
import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from wechat_db import Reader, decompress_body, private_write, root_for


def visible_text(raw, message_type):
    """Keep authored text/reply text; omit source XML, quoted originals and media URLs."""
    if message_type == 1:
        return {"text": decompress_body(raw).decode('utf-8', 'strict'), 'content_kind': 'text'}
    if message_type & 0xffffffff == 49:
        body = decompress_body(raw)
        try:
            xml = ET.fromstring(body)
        except (ET.ParseError, ValueError):
            return {'omitted': 'unrecognized_app_message'}
        app = xml.find('appmsg') if xml.tag != 'appmsg' else xml
        if app is not None and app.findtext('type') == '57':
            return {'text': app.findtext('title', ''), 'content_kind': 'quoted_reply'}
    return {'omitted': 'media_or_system_content_not_decoded'}


def export(reader, chat, *, self_id=None, timezone='Asia/Shanghai', after=None, before=None):
    tz = ZoneInfo(timezone)
    result = []
    for path, table in reader.paths(chat):
        with reader.open(path) as db:
            clauses = []
            if after is not None:
                clauses.append('m.create_time>=' + str(int(after)))
            if before is not None:
                clauses.append('m.create_time<' + str(int(before)))
            where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            rows = db.query('SELECT m.local_id,m.server_id,m.local_type,m.create_time,'
                            'n.user_name AS sender_id,hex(m.message_content) AS body FROM "' + table + '" m '
                            'LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id' + where + ' ORDER BY m.local_id')
        for source in rows:
            sender = source['sender_id']
            row = {'message_id': path.name + ':' + source['local_id'], 'database': path.name,
                   'local_id': int(source['local_id']), 'server_id': source['server_id'],
                   'timestamp': int(source['create_time']), 'type': int(source['local_type']),
                   'sender_id': sender,
                   'from_self': (sender == self_id) if sender and self_id else None}
            row['time'] = dt.datetime.fromtimestamp(row['timestamp'], tz).isoformat()
            try:
                content = visible_text(bytes.fromhex(source['body']), row['type'])
                if content.get('text') and chat.endswith('@chatroom') and sender:
                    prefix = sender + ':\n'
                    if content['text'].startswith(prefix):
                        content['text'] = content['text'][len(prefix):]
                row.update(content)
            except (UnicodeError, RuntimeError) as e:
                row.update(omitted='decode_error', decode_error=type(e).__name__)
            result.append(row)
    result.sort(key=lambda r: (r['timestamp'], r['database'], r['local_id']))
    summary = {'total_messages': len(result),
               'message_type_counts': dict(collections.Counter(str(r['type']) for r in result)),
               'decoded_authored_text': sum('text' in r for r in result),
               'decode_errors': sum(r.get('omitted') == 'decode_error' for r in result),
               'self_identity_provided': self_id is not None}
    return {'summary': summary, 'messages': result}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db-root', type=Path)
    ap.add_argument('--key-file', type=Path, required=True)
    ap.add_argument('--chat-id', required=True)
    ap.add_argument('--self-id')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--timezone', default='Asia/Shanghai')
    ap.add_argument('--after', type=int, help='Inclusive Unix timestamp')
    ap.add_argument('--before', type=int, help='Exclusive Unix timestamp')
    args = ap.parse_args()
    os.umask(0o077)
    reader = Reader(root_for(args.db_root), args.key_file)
    data = export(reader, args.chat_id, self_id=args.self_id, timezone=args.timezone, after=args.after, before=args.before)
    if args.output.resolve().is_relative_to(reader.root.resolve()):
        raise RuntimeError('output_must_be_outside_source_database_directory')
    private_write(args.output, data)
    print(json.dumps(data['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
