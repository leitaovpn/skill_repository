"""Synthetic fixtures only: no real account, keys, messages, apps or network."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import bootstrap_keys
import capture_keys
import export_messages
import sqlcipher_probe
import transcribe_local
import voice_export
from wechat_db import Reader, literal, private_write


def blob(value):
    return "x'" + value.hex() + "'"


class DatabaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            sqlcipher_probe.library()
        except Exception as error:
            raise unittest.SkipTest('SQLCipher dynamic library unavailable') from error

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wechat-synthetic-')
        self.base = Path(self.temp.name).resolve()
        self.root = self.base/'db_storage'
        self.root.mkdir()
        self.chat = 'fixture-target'
        self.table = 'Msg_'+hashlib.md5(self.chat.encode()).hexdigest()
        self.keys = {}
        for name in ['message/message_0.db', 'message/media_0.db', 'contact/contact.db']:
            path = self.root/name
            path.parent.mkdir(exist_ok=True)
            key = os.urandom(32).hex()
            self.keys[name] = {'enc_key': key}
            with sqlcipher_probe.DB(path, key, fixture_write=True) as db:
                if name.endswith('contact.db'):
                    db.query("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT);")
                    db.query("INSERT INTO contact VALUES('fixture-target','same-name','target'),('fixture-foreign','same-name','foreign');")
                elif name.endswith('message_0.db'):
                    db.query('CREATE TABLE Name2Id(user_name TEXT);')
                    db.query("INSERT INTO Name2Id VALUES('fixture-self'),('fixture-target');")
                    db.query(f'CREATE TABLE "{self.table}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content BLOB);')
                    reply = b'<msg><appmsg><type>57</type><title>authored reply</title><refermsg><content>private original</content></refermsg><url>fixture-media-secret</url></appmsg></msg>'
                    voice = b'<msg><voicemsg voicelength="1500" voiceurl="fixture-media-secret" aeskey="fixture-credential"/></msg>'
                    messages = [(1, 1, 1, 1, b'authored text'), (2, 2, (57<<32)|49, 2, reply)]
                    messages.extend((i, sid, 34, 2, voice) for i, sid in [(3,100),(4,101),(5,102),(6,103)])
                    for number, server, kind, sender, content in messages:
                        db.query(f'INSERT INTO "{self.table}" VALUES({number},{server},{kind},{1700000000+number},{sender},{blob(content)});')
                else:
                    db.query('CREATE TABLE Name2Id(user_name TEXT);')
                    db.query("INSERT INTO Name2Id VALUES('fixture-target'),('fixture-foreign');")
                    db.query('CREATE TABLE VoiceInfo(chat_name_id INTEGER,svr_id INTEGER,voice_data BLOB);')
                    payload = b'\x02#!SILK_V3synthetic'
                    media = [(1,100,payload),(1,100,b''),(1,100,payload),(2,100,b'foreign-conflict'),
                             (1,101,b''),(1,102,b'candidate-a'),(1,102,b'candidate-b'),(2,103,b'foreign-only')]
                    for chat, server, content in media:
                        db.query(f'INSERT INTO VoiceInfo VALUES({chat},{server},{blob(content)});')
        self.key_file = self.base/'keys.json'
        private_write(self.key_file, {'keys': self.keys})
        self.reader = Reader(self.root, self.key_file)

    def tearDown(self):
        self.temp.cleanup()

    def test_contacts_do_not_choose_first_duplicate_and_literal_cannot_inject(self):
        self.assertEqual(len(self.reader.resolve('same-name')), 2)
        self.assertEqual(len(self.reader.resolve('fixture-target')), 1)
        self.assertEqual(self.reader.resolve("' OR 1=1 --"), [])

    def test_export_preserves_authored_reply_without_original_xml_or_credentials(self):
        result = export_messages.export(self.reader, self.chat)
        self.assertEqual(result['summary']['total_messages'], 6)
        self.assertEqual(result['messages'][1]['text'], 'authored reply')
        self.assertTrue(all(r['from_self'] is None for r in result['messages']))
        serialized = json.dumps(result)
        for secret in ['private original', 'fixture-media-secret', 'fixture-credential', '<voicemsg']:
            self.assertNotIn(secret, serialized)
        limited = export_messages.export(self.reader, self.chat, after=1700000002, before=1700000003)
        self.assertEqual([r['local_id'] for r in limited['messages']], [2])

    def test_voice_isolation_missing_empty_duplicates_and_conflicting_payloads(self):
        summary = voice_export.export_voice(self.reader, self.chat, self.base/'voice')
        self.assertEqual(summary['status_counts'], {'audio_available':1, 'audio_missing':2, 'ambiguous':1})
        manifest = json.loads((self.base/'voice/manifest.json').read_text())
        rows = manifest['messages']
        self.assertEqual(rows[0]['expected_seconds'], 1.5)
        self.assertEqual(rows[0]['media_row_count'], 3)
        self.assertEqual(rows[0]['empty_media_rows'], 1)
        self.assertEqual(rows[0]['duplicate_media_rows'], 2)
        self.assertEqual(rows[0]['sender'], 'unknown')
        self.assertIsNone(rows[0]['from_self'])
        self.assertEqual(Path(rows[0]['audio_path']).read_bytes(), b'\x02#!SILK_V3synthetic')
        self.assertIsNone(rows[2]['audio_path'])
        self.assertIsNone(rows[3]['audio_path'])
        self.assertEqual(len(list((self.base/'voice/source-audio').glob('*.silk'))),1)
        self.assertNotIn('fixture-media-secret', json.dumps(manifest))
        self.assertEqual((self.base/'voice').stat().st_mode & 0o777,0o700)
        self.assertEqual((self.base/'voice/manifest.json').stat().st_mode & 0o777,0o600)

    def test_explicit_self_identity_and_read_only_connection(self):
        result = export_messages.export(self.reader, self.chat, self_id='fixture-self')
        self.assertIs(result['messages'][0]['from_self'],True)
        self.assertIs(result['messages'][1]['from_self'],False)
        path = self.root/'message/message_0.db'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.reader.open(path) as db:
            with self.assertRaises(RuntimeError):
                db.query(f'DELETE FROM "{self.table}";')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_incremental_baseline_and_cursor_regression(self):
        state = self.reader.init_state(self.chat,'fixture-self')
        self.assertEqual(self.reader.check(state)['new_messages'],[])
        path = self.root/'message/message_0.db'
        key = self.keys['message/message_0.db']['enc_key']
        with sqlcipher_probe.DB(path,key,fixture_write=True) as db:
            db.query(f'INSERT INTO "{self.table}" VALUES(7,7,1,1700000007,2,{blob(b"new synthetic message")});')
        batch = self.reader.check(state)
        self.assertEqual([r['local_id'] for r in batch['new_messages']],['7'])
        self.assertEqual(state['cursor']['message_0.db'],6)
        state['cursor']['message_0.db'] = 99
        with self.assertRaisesRegex(RuntimeError,'cursor_regressed'):
            self.reader.check(state)


class SafetyAndASRTests(unittest.TestCase):
    def test_private_output_rejects_repo_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            (base/'repo/.git').mkdir(parents=True)
            with self.assertRaises(voice_export.ExportError):
                voice_export.private_directory(base/'repo/runtime')
            (base/'real').mkdir()
            (base/'linked').symlink_to(base/'real', target_is_directory=True)
            with self.assertRaises(voice_export.ExportError):
                voice_export.private_directory(base/'linked/runtime')

    def test_bootstrap_cleanup_signals_only_its_exact_copy(self):
        copy = Path('/synthetic/private-copy/Contents/MacOS/WeChat')
        inventory = [[(11,'/Applications/WeChat.app/Contents/MacOS/WeChat'),(22,str(copy))],
                     [(11,'/Applications/WeChat.app/Contents/MacOS/WeChat')]]
        with patch.object(bootstrap_keys,'wechat_processes',side_effect=inventory), patch.object(bootstrap_keys.os,'kill') as kill:
            self.assertTrue(bootstrap_keys.close_exact_copy(copy))
            kill.assert_called_once_with(22,signal.SIGTERM)

    def test_capture_page_validation_rejects_corruption(self):
        key = bytes(range(32))
        page = bytearray(os.urandom(4096))
        salt = bytes(x^0x3a for x in page[:16])
        mac_key = hashlib.pbkdf2_hmac('sha512',key,salt,2,32)
        page[4032:4096] = hmac.new(mac_key,page[16:4032]+b'\x01\x00\x00\x00',hashlib.sha512).digest()
        self.assertTrue(capture_keys.verified(key,bytes(page)))
        page[128] ^= 1
        self.assertFalse(capture_keys.verified(key,bytes(page)))

    def test_model_or_tokenizer_changes_invalidate_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp)
            (model/'config.json').write_text('{}')
            (model/'weights.safetensors').write_bytes(b'synthetic weights')
            (model/'vocab.json').write_text('{}')
            first = transcribe_local.model_fingerprint(model)
            (model/'vocab.json').write_text('{"a":1}')
            self.assertNotEqual(first,transcribe_local.model_fingerprint(model))

    def test_asr_flags_empty_truncated_and_repetitive_output(self):
        self.assertIn('no_recognized_speech',transcribe_local.quality_flags('',1,.2))
        self.assertIn('generation_truncated',transcribe_local.quality_flags('合成测试',2,.2,truncated=True))
        self.assertIn('repetitive_output',transcribe_local.quality_flags('你好你好你好你好',2,.2))


if __name__ == '__main__':
    unittest.main()
