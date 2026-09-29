"""共享爆文方法库的跨账号回归测试（不连接真实数据库或 AI）。"""
import importlib.util
import json
import logging
import pathlib
import sys
import types
import unittest


class KnowledgeBaseTest(unittest.TestCase):
    def setUp(self):
        self.payload = {}
        self.account = 'account_a'
        self.rows = []
        self.ai_inputs = []
        request = types.SimpleNamespace(get_json=lambda silent=True: self.payload)
        flask = types.ModuleType('flask')
        flask.request = request
        requests = types.ModuleType('requests')
        requests.RequestException = Exception
        requests.post = lambda *args, **kwargs: None
        requests.get = lambda *args, **kwargs: None
        self.previous_flask = sys.modules.get('flask')
        self.previous_requests = sys.modules.get('requests')
        sys.modules['flask'] = flask
        sys.modules['requests'] = requests
        spec = importlib.util.spec_from_file_location(
            'content_studio_api_tested', pathlib.Path(__file__).with_name('content_studio_api.py'))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

        def ask_ai(api_url, system, user, max_tokens):
            self.ai_inputs.append(user)
            if '电商内容分析师' in system:
                return {'title': '测试爆文', 'contentType': 'video',
                        'breakdown': {'topic': '品类痛点', 'structure': '问题到证据再到结论',
                                      'title': '场景与结论', 'shots': '镜头证据',
                                      'comments': '自然评论', 'learn': '先给结论', 'risks': '核验事实'}}
            return {'topics': ['选题'], 'titles': ['标题'], 'body': '正文', 'imagePrompts': []}

        self.module._ask_ai = ask_ai
        self.routes = {}
        app = types.SimpleNamespace(logger=logging.getLogger(__name__))
        app.route = lambda path, methods=None: lambda fn: self.routes.setdefault(path, fn)

        def db_execute(sql, params=None, fetch=True):
            if 'CREATE TABLE' in sql:
                return 0
            if '`fingerprint` = %s' in sql:
                return [{'id': row['id']} for row in self.rows if row['fingerprint'] == params[0]]
            if 'FROM `content_studio_knowledge_base`' in sql:
                return list(reversed(self.rows))
            return []

        def db_execute_insert(sql, params=None):
            row = {'id': len(self.rows) + 1, 'source_account': params[0], 'title': params[1],
                   'input': params[2], 'source_url': params[3], 'content_type': params[4],
                   'evidence': params[5], 'breakdown': json.loads(params[6]),
                   'fingerprint': params[7], 'created_at': '2026-09-28'}
            self.rows.append(row)
            return row['id']

        self.module.register_content_studio(
            app, lambda data, msg=None: {'code': 0, 'data': data},
            lambda msg, code=None: {'code': code or 1, 'msg': msg}, 'mock-ai',
            db_execute=db_execute, db_execute_insert=db_execute_insert,
            current_session=lambda: {'account': self.account})

    def tearDown(self):
        for name, previous in (('flask', self.previous_flask), ('requests', self.previous_requests)):
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def test_all_accounts_feed_one_private_knowledge_table(self):
        self.payload = {'input': '测试素材甲：先展示真实场景问题，然后给出证据和适用边界。' * 2,
                        'focus': '看标题'}
        first = self.routes['/api/content-studio/analyze']()
        self.assertEqual(first['data']['knowledgeStatus'], 'saved')
        self.payload['focus'] = '看结构'
        duplicate = self.routes['/api/content-studio/analyze']()
        self.assertEqual(duplicate['data']['knowledgeStatus'], 'exists')
        self.account = 'account_b'
        self.payload = {'input': '测试素材乙：先给结论，再呈现细节，最后说明适合什么人。' * 2}
        second = self.routes['/api/content-studio/analyze']()
        self.assertEqual(second['data']['knowledgeStatus'], 'saved')
        self.assertEqual([r['source_account'] for r in self.rows], ['account_a', 'account_b'])
        self.assertNotIn('/api/content-studio/knowledge-base', self.routes)

        self.payload = {'category': '脚垫', 'noteType': '种草', 'productName': '产品',
                        'sellingPoints': '已确认卖点', 'stylePreference': '素人感型'}
        self.ai_inputs.clear()
        self.account = 'account_a'
        self.assertEqual(self.routes['/api/content-studio/generate']()['code'], 0)
        self.account = 'account_b'
        self.assertEqual(self.routes['/api/content-studio/generate']()['code'], 0)
        self.assertEqual(self.ai_inputs[0], self.ai_inputs[1])
        self.assertIn('共享爆文知识库', self.ai_inputs[0])
        self.assertNotIn('account_a', self.ai_inputs[0])
        self.assertNotIn('account_b', self.ai_inputs[0])


if __name__ == '__main__':
    unittest.main()
