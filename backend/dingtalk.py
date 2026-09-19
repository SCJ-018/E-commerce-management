# -*- coding: utf-8 -*-
"""钉钉推送封装 —— 企业内部应用（机器人）发送消息给指定成员。

【为什么不用群自定义机器人 webhook】
钉钉「群自定义机器人」只支持 text / markdown / link / actionCard / feedCard，
**不支持发送文件**。要把 PDF 作为附件发到个人，必须走企业内部应用机器人接口：
    ① POST /v1.0/oauth2/accessToken                 拿 accessToken（有效期 7200s，本地缓存）
    ② POST /oapi.dingtalk.com/media/upload          上传文件拿 media_id（type=file）
    ③ POST /v1.0/robot/oToMessages/batchSend        单聊发送 sampleFile 文件消息 / sampleMarkdown 摘要

【前提条件（钉钉开放平台侧）】
- 应用类型：企业内部应用，且已开通「机器人」能力
- robotCode 一般等于 AppKey（即应用的 Client ID），留空则自动取 AppKey
- 需要给应用开通的权限：机器人发送单聊消息（默认具备）；
  若希望用手机号自动换 userid，还需「通讯录个人信息读权限 / 成员信息读权限」
- 收件人必须在应用的「可见范围」内，否则 batchSend 会返回 invalidStaffIdList

依赖：requests（服务器 /opt/ecom/venv 已装）
"""
import json
import time

import requests

_OAUTH_URL = 'https://api.dingtalk.com/v1.0/oauth2/accessToken'
_OTO_SEND_URL = 'https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend'
_API_BASE = 'https://api.dingtalk.com'
_OAPI_BASE = 'https://oapi.dingtalk.com'


class DingTalkError(Exception):
    """钉钉接口调用异常，message 为可直接展示给用户的中文说明"""


class DingTalkClient:
    """企业内部应用机器人客户端（无状态，token 按 AppKey 进程级缓存）"""

    # {app_key: (access_token, 过期时间戳)}
    _token_cache = {}

    def __init__(self, app_key, app_secret, robot_code=None, agent_id=None):
        self.app_key = (app_key or '').strip()
        self.app_secret = (app_secret or '').strip()
        self.robot_code = (robot_code or '').strip() or self.app_key
        self.agent_id = (agent_id or '').strip()

    # ---------------- 凭证 ----------------

    def _check_credential(self):
        if not self.app_key:
            raise DingTalkError('未配置 Client ID（原 AppKey），请先在「钉钉推送」设置里填写')
        if not self.app_secret:
            raise DingTalkError('未配置 Client Secret（原 AppSecret），请先在「钉钉推送」设置里填写')

    def get_token(self, force=False):
        """获取 accessToken（提前 60 秒过期，避免边界失效）"""
        self._check_credential()
        now = time.time()
        cached = DingTalkClient._token_cache.get(self.app_key)
        if cached and not force and cached[1] > now + 60:
            return cached[0]

        try:
            resp = requests.post(
                _OAUTH_URL,
                json={'appKey': self.app_key, 'appSecret': self.app_secret},
                timeout=15,
            )
        except Exception as e:
            raise DingTalkError('连接钉钉服务器失败：%s' % e)

        try:
            data = resp.json()
        except Exception:
            raise DingTalkError('钉钉返回内容无法解析（HTTP %s）：%s' % (resp.status_code, resp.text[:200]))

        token = data.get('accessToken')
        if not token:
            # 典型错误：invalidClientIdOrSecret / 401 —— Client ID 或 Client Secret 不对
            code = data.get('code') or ''
            if code == 'invalidClientIdOrSecret':
                raise DingTalkError(
                    '获取 accessToken 失败：Client ID 与 Client Secret 不匹配。'
                    '请到钉钉开放平台 → 应用 → 基础信息 → 凭证与基础信息，'
                    '复制「Client ID」（不是 App ID）与「Client Secret」')
            msg = data.get('message') or code or json.dumps(data, ensure_ascii=False)
            raise DingTalkError('获取 accessToken 失败：%s' % msg)

        expire_in = int(data.get('expireIn') or 7200)
        DingTalkClient._token_cache[self.app_key] = (token, now + expire_in)
        return token

    # ---------------- 媒体 / 消息 ----------------

    def upload_file(self, filename, content, content_type='application/pdf'):
        """上传文件到钉钉，返回 media_id（文件有效期 30 天，单文件上限 20MB）"""
        return self._upload_media('file', filename, content, content_type)

    def upload_image(self, filename, content, content_type='image/jpeg'):
        """上传图片到钉钉，返回 media_id（仅支持 jpg/png，单图上限 10MB）"""
        return self._upload_media('image', filename, content, content_type)

    def _upload_media(self, media_type, filename, content, content_type):
        """media/upload 公共实现（type=file / type=image）"""
        token = self.get_token()
        try:
            resp = requests.post(
                _OAPI_BASE + '/media/upload',
                params={'access_token': token, 'type': media_type},
                files={'media': (filename, content, content_type)},
                timeout=120,
            )
            data = resp.json()
        except Exception as e:
            raise DingTalkError('上传%s失败：%s' % ('图片' if media_type == 'image' else '文件', e))

        media_id = data.get('media_id')
        if not media_id:
            raise DingTalkError('上传%s失败：%s(%s)' % ('图片' if media_type == 'image' else '文件',
                                                      data.get('errmsg'), data.get('errcode')))
        return media_id

    def _robot_code_candidates(self):
        """robotCode 候选列表（按优先级）

        钉钉新版把「App ID（UnifiedAppId，即旧版 AgentId）」和「Client ID（原 AppKey）」拆成了两个值，
        机器人接口的 robotCode 在不同应用上认的值不一样，这里按 显式配置 → Client ID → App ID 依次尝试，
        省掉「改了配置再试一次」的往返。
        """
        out = []
        for v in (self.robot_code, self.app_key, self.agent_id):
            v = (v or '').strip()
            if v and v not in out:
                out.append(v)
        return out

    def _oto_send(self, user_ids, msg_key, msg_param):
        """人与机器人会话批量发送的公共实现（robotCode 多候选自动试错）"""
        if not user_ids:
            raise DingTalkError('收件人列表为空')

        first_err = None
        for robot_code in (self._robot_code_candidates() or ['']):
            body = {
                'robotCode': robot_code,
                'userIds': list(user_ids),
                'msgKey': msg_key,
                'msgParam': msg_param,
            }

            # 首次 token 可能刚好过期 → 强制刷新后重试一次
            for attempt in (0, 1):
                token = self.get_token(force=bool(attempt))
                try:
                    resp = requests.post(
                        _OTO_SEND_URL,
                        json=body,
                        headers={'x-acs-dingtalk-access-token': token},
                        timeout=30,
                    )
                    data = resp.json()
                except Exception as e:
                    raise DingTalkError('发送消息失败：%s' % e)

                invalid = data.get('invalidStaffIdList') or []
                if resp.status_code >= 400 or data.get('code'):
                    msg = data.get('message') or json.dumps(data, ensure_ascii=False)
                    err = '发送消息失败(HTTP %s)：%s' % (resp.status_code, msg)
                    if first_err is None:
                        first_err = err
                    # 400/401 多为 token 失效，重试一次；其它错误直接换下一个 robotCode
                    if attempt == 0 and resp.status_code in (400, 401):
                        continue
                    break

                return {'processQueryKey': data.get('processQueryKey', ''),
                        'invalidStaffIdList': invalid,
                        'robotCode': robot_code}

        raise DingTalkError(first_err or '发送消息失败')

    def send_file(self, user_ids, media_id, filename, file_type='pdf'):
        """发送文件消息（sampleFile）。fileName 决定钉钉里显示的文件名。"""
        param = {'mediaId': media_id, 'fileName': filename, 'fileType': file_type}
        try:
            return self._oto_send(user_ids, 'sampleFile', json.dumps(param, ensure_ascii=False))
        except DingTalkError:
            # 少数版本的模板参数名是 fileMediaId，兼容重试一次
            param2 = {'fileMediaId': media_id, 'fileName': filename, 'fileType': file_type}
            return self._oto_send(user_ids, 'sampleFile', json.dumps(param2, ensure_ascii=False))

    def send_markdown(self, user_ids, title, text):
        """发送 markdown 消息（sampleMarkdown），用于推送指标摘要"""
        param = {'title': title, 'text': text}
        return self._oto_send(user_ids, 'sampleMarkdown', json.dumps(param, ensure_ascii=False))

    def send_text(self, user_ids, content):
        """发送纯文本消息（sampleText），用于连通性测试"""
        return self._oto_send(user_ids, 'sampleText', json.dumps({'content': content}, ensure_ascii=False))

    def send_image(self, user_ids, media_id):
        """发送图片消息（media_id 为 upload_image 返回的值）

        ⚠ 实测（2026-09，本应用）确认的键名，别再凭印象改：
          · msgKey 必须是 **sampleImageMsg**；写 `sampleImage` 会被钉钉直接拒：
            HTTP 400「不支持类型 sampleImage」
          · 参数名 `photoURL` 与 `photoMediaId` 都能收（photoURL 允许直接填 media_id）
        保留双候选是为了兼容模板差异；两个都失败才把错误抛给调用方。
        ⚠ 试错不会重复投递 —— 报错的那次并没有发出去，成功即 return。
        """
        last_err = None
        for msg_key, param_key in (('sampleImageMsg', 'photoURL'),
                                   ('sampleImageMsg', 'photoMediaId')):
            try:
                param = {param_key: media_id}
                return self._oto_send(user_ids, msg_key, json.dumps(param, ensure_ascii=False))
            except DingTalkError as e:
                last_err = e
                print('[钉钉] 图片模板 %s/%s 未通过：%s' % (msg_key, param_key, e))
        raise last_err or DingTalkError('发送图片消息失败')

    # ---------------- 通讯录 ----------------

    def get_userid_by_mobile(self, mobile):
        """用手机号换取企业内 userid（需要通讯录读权限）"""
        mobile = (mobile or '').strip()
        if not mobile:
            raise DingTalkError('手机号为空')
        token = self.get_token()
        try:
            resp = requests.post(
                _OAPI_BASE + '/topapi/v2/user/getbymobile',
                params={'access_token': token},
                json={'mobile': mobile},
                timeout=15,
            )
            data = resp.json()
        except Exception as e:
            raise DingTalkError('手机号换 userid 失败：%s' % e)

        if data.get('errcode') != 0:
            errmsg = str(data.get('errmsg') or '')
            # 60011 = 应用未开通该接口权限，钉钉会在 errmsg 里带上申请链接，直接透传给用户
            if data.get('errcode') == 88 and 'qyapi_get_member_by_mobile' in errmsg:
                raise DingTalkError(
                    '应用缺少「手机号换 userId」权限（qyapi_get_member_by_mobile）。'
                    '两种解决方式：① 在钉钉开放平台 → 该应用 → 权限管理，'
                    '开通「通讯录个人信息读权限 / 成员信息读权限」后重新点「匹配 userId」；'
                    '② 直接在推送人里填该成员的钉钉 userId（填入后即不再需要此权限）')
            raise DingTalkError('手机号 %s 换 userid 失败：%s(%s)'
                                % (mobile, errmsg, data.get('errcode')))
        userid = (data.get('result') or {}).get('userid')
        if not userid:
            raise DingTalkError('手机号 %s 未匹配到企业成员' % mobile)
        return userid

    # ---------------- 组织架构（通讯录） ----------------
    # 供「通告发放」读取钉钉部门树 + 成员列表使用。
    # 需要应用开通「成员信息读权限」「部门信息读权限」（钉钉开放平台 → 权限管理）。

    def list_sub_departments(self, parent_id=1):
        """列出某部门的子部门（parent_id=1 为根部门），不递归"""
        token = self.get_token()
        try:
            resp = requests.post(
                _OAPI_BASE + '/topapi/v2/department/listsub',
                params={'access_token': token},
                json={'dept_id': int(parent_id)},
                timeout=20,
            )
            data = resp.json()
        except Exception as e:
            raise DingTalkError('获取部门列表失败：%s' % e)
        if data.get('errcode') != 0:
            raise DingTalkError('获取部门列表失败：%s(%s)，请确认应用已开通「部门信息读权限」'
                                % (data.get('errmsg'), data.get('errcode')))
        return data.get('result') or []

    def list_dept_users(self, dept_id, max_pages=50):
        """分页拉取某部门下的成员（每页 100），返回 [{userid, name, ...}]"""
        token = self.get_token()
        users, cursor = [], 0
        for _ in range(max_pages):
            try:
                resp = requests.post(
                    _OAPI_BASE + '/topapi/v2/user/list',
                    params={'access_token': token},
                    json={'dept_id': int(dept_id), 'cursor': cursor, 'size': 100},
                    timeout=20,
                )
                data = resp.json()
            except Exception as e:
                raise DingTalkError('获取部门成员失败：%s' % e)
            if data.get('errcode') != 0:
                raise DingTalkError('获取部门成员失败：%s(%s)，请确认应用已开通「成员信息读权限」'
                                    % (data.get('errmsg'), data.get('errcode')))
            result = data.get('result') or {}
            users.extend(result.get('list') or [])
            if not result.get('has_more'):
                break
            cursor = result.get('next_cursor') or 0
        return users

    def fetch_all_contacts(self):
        """拉取整个组织架构：返回 (departments, users)

        departments: [{deptId, name, parentId}]（含根节点 1）
        users:       [{userid, name, deptIds:[...]}]（跨部门成员会去重）
        """
        departments = [{'deptId': 1, 'name': '全公司', 'parentId': 0}]
        users, seen = [], set()

        def _walk(parent):
            subs = self.list_sub_departments(parent)
            for d in subs:
                departments.append({
                    'deptId': d.get('dept_id'),
                    'name': d.get('name') or '',
                    'parentId': parent,
                })
            for d in subs:
                _walk(d.get('dept_id'))

        _walk(1)

        for d in list(departments):
            dept_id = d['deptId']
            try:
                raw = self.list_dept_users(dept_id)
            except DingTalkError:
                # 单个部门无权限/不存在不阻断整体
                continue
            for u in raw:
                uid = u.get('userid')
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                users.append({
                    'userid': uid,
                    'name': u.get('name') or '',
                    'title': u.get('title') or '',
                    'deptIds': [dept_id],
                })
        return departments, users
