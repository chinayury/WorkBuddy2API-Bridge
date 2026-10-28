# -*- coding: utf-8 -*-
"""settings.json 损坏时必须 fail closed。

这是 v0.1.0 审计发现的最高危问题。

旧实现里，「文件不存在」与「文件存在但读不出来」是**同一件事**（都返回空 dict），
于是四件事同时发生：
  1. 面板密码回落成默认的 "admin"；
  2. API Key 列表变空 ⇒ auth_required() 判定为「不需要鉴权」
     ⇒ 网关对所有能碰到端口的人开放（/v1/chat/completions 直接可调）；
  3. 每日限额/积分限额一并回落到默认值 —— 保护在最需要的时候没了；
  4. 面板的第一次写入会用这个空 dict **覆盖**坏文件
     ⇒ API Key 与密码哈希彻底丢失，回不来了。

修复方向：
  - 加载失败时打上 `_UNREADABLE` 标记；
  - 面板登录 / 鉴权判定看到标记就拒绝（fail closed）；
  - save() 拒绝用带标记的内容覆盖坏文件，并把坏文件留档成 .corrupt。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_settings as S  # noqa: E402


class UnreadableSettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-settings-corrupt-")
        self.path = S.settings_path(self.dir)
        # 每个用例独立：清掉模块级缓存
        S._load_cache["key"] = None

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        S._load_cache["key"] = None

    def _write(self, content):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_a_missing_file_is_not_unreadable(self):
        """全新安装：文件不存在是正常状态，不是故障。"""
        self.assertFalse(S.settings_unreadable(self.dir))
        self.assertEqual({}, S.load(self.dir))

    def test_a_corrupt_file_is_unreadable(self):
        self._write("{ this is not json")
        self.assertTrue(S.settings_unreadable(self.dir))

    def test_a_non_object_json_is_unreadable(self):
        self._write("[1, 2, 3]")
        self.assertTrue(S.settings_unreadable(self.dir))

    def test_a_valid_file_is_readable(self):
        self._write(json.dumps({"api_keys": []}))
        self.assertFalse(S.settings_unreadable(self.dir))

    def test_load_marks_the_corrupt_file(self):
        self._write("{ broken")
        data = S.load(self.dir)
        self.assertTrue(data.get(S._UNREADABLE))

    def test_load_does_not_mark_a_missing_file(self):
        data = S.load(self.dir)
        self.assertNotIn(S._UNREADABLE, data)

    def test_load_does_not_mark_a_valid_file(self):
        self._write(json.dumps({"x": 1}))
        data = S.load(self.dir)
        self.assertNotIn(S._UNREADABLE, data)


class FailClosedTests(unittest.TestCase):
    """损坏时必须拒绝，而不是回落到默认值。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-settings-failclosed-")
        self.path = S.settings_path(self.dir)
        S._load_cache["key"] = None

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        S._load_cache["key"] = None

    def _corrupt(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ broken")

    def test_the_default_password_does_not_open_a_corrupt_panel(self):
        """核心：损坏时不能用 "admin" 进门。"""
        self._corrupt()
        self.assertFalse(S.verify_panel_password(self.dir, "admin"))
        self.assertFalse(S.verify_panel_password(self.dir, ""))

    def test_no_password_opens_a_corrupt_panel(self):
        self._corrupt()
        self.assertFalse(S.verify_panel_password(self.dir, None))

    def test_panel_password_is_not_reported_as_default_when_corrupt(self):
        """说「还是默认密码」是在替一个未知状态背书。"""
        self._corrupt()
        self.assertFalse(S.panel_password_is_default(self.dir))

    def test_a_missing_file_still_allows_the_default_password(self):
        """全新安装的默认行为不变（面板默认口令 admin 是既有设计）。"""
        self.assertTrue(S.verify_panel_password(self.dir, "admin"))

    def test_a_valid_custom_password_still_works(self):
        S.set_panel_password(self.dir, "correct horse battery staple")
        S._load_cache["key"] = None
        self.assertTrue(S.verify_panel_password(self.dir, "correct horse battery staple"))
        self.assertFalse(S.verify_panel_password(self.dir, "admin"))


class NoClobberTests(unittest.TestCase):
    """坏文件不许被空内容覆盖 —— 那是密钥永久丢失的根因。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-settings-clobber-")
        self.path = S.settings_path(self.dir)
        S._load_cache["key"] = None

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        S._load_cache["key"] = None

    def test_saving_the_unreadable_marker_is_refused(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ broken")
        with self.assertRaises(RuntimeError):
            S.save(self.dir, {S._UNREADABLE: True})
        # 原文件内容还在
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn("broken", fh.read())

    def test_quarantine_keeps_a_copy(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ broken")
        copy = S.quarantine_unreadable(self.dir)
        self.assertIsNotNone(copy)
        self.assertTrue(os.path.exists(copy))
        with open(copy, encoding="utf-8") as fh:
            self.assertIn("broken", fh.read())

    def test_quarantine_of_a_missing_file_returns_none(self):
        self.assertIsNone(S.quarantine_unreadable(self.dir))

    def test_a_normal_save_still_works(self):
        S.save(self.dir, {"api_keys": [{"id": "k1"}]})
        S._load_cache["key"] = None
        data = S.load(self.dir)
        self.assertEqual([{"id": "k1"}], data.get("api_keys"))


if __name__ == "__main__":
    unittest.main()
