"""extract_sessions.py 的單元測試；以合成 SQLite 模擬 OpenCode V2 的 session_v2／session_message。

執行：python -I -m unittest discover -s recall/scripts -p "test_*.py"
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import extract_sessions as es  # noqa: E402

NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000
PROJECT = os.path.normpath("D:/work/proj-a")
OTHER_PROJECT = os.path.normpath("D:/work/proj-b")


def tool(name, status, inp, error=None):
    state = {"status": status, "input": inp}
    if error:
        state["error"] = {"type": "tool.execution", "message": error}
    return {"type": "tool", "id": f"call_{name}_{status}", "name": name, "state": state}


class Fixture:
    """建立最小可用的 OpenCode V2 資料表，只放腳本實際讀取的欄位。"""

    def __init__(self, root: Path):
        self.path = root / "opencode.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(
            """
            create table session_v2 (
                id text primary key, parent_id text, directory text, title text,
                agent text, model text, cost real, tokens_input integer, tokens_output integer,
                time_created integer, time_updated integer);
            create table session_message (
                id text primary key, session_id text, type text, seq integer,
                time_created integer, time_updated integer, data text);
            """
        )
        self._msg = 0

    def session(self, sid, directory=PROJECT, parent=None, age_days=1.0, title="t", agent="build"):
        ts = NOW_MS - int(age_days * DAY_MS)
        self.conn.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, parent, directory, title, agent, json.dumps({"id": "m1"}), 0.5, 100, 20, ts - 1000, ts),
        )

    def message(self, sid, mtype, data):
        self._msg += 1
        self.conn.execute(
            "insert into session_message values (?,?,?,?,?,?,?)",
            (f"msg_{self._msg}", sid, mtype, self._msg, NOW_MS, NOW_MS, json.dumps(data, ensure_ascii=False)),
        )

    def user(self, sid, text, skills=None):
        self.message(sid, "user", {"time": {"created": NOW_MS}, "text": text, "skills": skills or []})

    def assistant(self, sid, *content):
        self.message(sid, "assistant", {"time": {"created": NOW_MS}, "agent": "build", "content": list(content)})

    def close(self):
        self.conn.commit()
        self.conn.close()


class ExtractSessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fx = Fixture(self.root)
        self.out = self.root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def run_extract(self, *extra):
        self.fx.close()
        argv = ["--db", str(self.fx.path), "--out", str(self.out), "--directory", PROJECT, "--no-env-exclude", *extra]
        self.assertEqual(es.main(argv), 0)
        return json.loads((self.out / "meta.json").read_text(encoding="utf-8"))

    def selected_ids(self, meta):
        return [s["id"] for s in meta["sessions"]]

    # 子 session 的工具呼叫、錯誤與對話內容都要併回根 session，子 session 本身不得成為獨立項目。
    def test_child_session_rolls_up_into_root(self):
        self.fx.session("ses_root")
        self.fx.session("ses_child", parent="ses_root", agent="explore")
        self.fx.user("ses_root", "幫我找檔案")
        self.fx.assistant("ses_root", tool("subagent", "completed", {"agent": "explore", "description": "找檔"}))
        self.fx.assistant("ses_child", tool("grep", "error", {"pattern": "Foo"}, "Search timed out"))

        meta = self.run_extract()

        self.assertEqual(self.selected_ids(meta), ["ses_root"])
        rec = meta["sessions"][0]
        self.assertEqual(rec["child_sessions"], 1)
        self.assertEqual(rec["tool_calls"]["grep"], 1)
        self.assertEqual(rec["tool_errors"]["grep"], 1)
        transcript = (self.out / rec["transcript"]).read_text(encoding="utf-8")
        self.assertIn("ses_child", transcript)
        self.assertIn("Search timed out", transcript)

    # 預設只取 --directory 底下的 session；加 --all-projects 才納入其他專案。
    def test_directory_filter_and_all_projects(self):
        self.fx.session("ses_a", directory=os.path.join(PROJECT, "sub"))
        self.fx.session("ses_b", directory=OTHER_PROJECT)
        self.fx.user("ses_a", "a")
        self.fx.user("ses_b", "b")
        meta = self.run_extract()
        self.assertEqual(self.selected_ids(meta), ["ses_a"])

        out2 = self.root / "out2"
        argv = ["--db", str(self.fx.path), "--out", str(out2), "--all-projects", "--no-env-exclude"]
        self.assertEqual(es.main(argv), 0)
        meta2 = json.loads((out2 / "meta.json").read_text(encoding="utf-8"))
        self.assertCountEqual(self.selected_ids(meta2), ["ses_a", "ses_b"])

    # --directory 可重複指定：任一目錄底下的 session 都納入，其餘目錄不納入。
    def test_multiple_directories(self):
        third = os.path.normpath("D:/work/proj-c")
        self.fx.session("ses_a", directory=PROJECT)
        self.fx.session("ses_b", directory=os.path.join(OTHER_PROJECT, "sub"))
        self.fx.session("ses_c", directory=third)
        for sid in ("ses_a", "ses_b", "ses_c"):
            self.fx.user(sid, sid)
        meta = self.run_extract("--directory", OTHER_PROJECT)
        self.assertCountEqual(self.selected_ids(meta), ["ses_a", "ses_b"])
        self.assertEqual(meta["params"]["directory"], [PROJECT, OTHER_PROJECT])

    # --balance-projects 依目錄輪流選取：上限 3 時，較舊的 B 專案仍至少選入 1 個；未加時只取最新的 A 專案。
    def test_balance_projects_round_robin(self):
        for i in range(5):
            self.fx.session(f"ses_a{i}", directory=PROJECT, age_days=1 + i * 0.1)
        for i in range(2):
            self.fx.session(f"ses_b{i}", directory=OTHER_PROJECT, age_days=3 + i * 0.1)
        self.fx.close()
        base = ["--db", str(self.fx.path), "--all-projects", "--no-env-exclude", "--max-sessions", "3"]

        out_plain = self.root / "plain"
        self.assertEqual(es.main([*base, "--out", str(out_plain)]), 0)
        plain = json.loads((out_plain / "meta.json").read_text(encoding="utf-8"))
        self.assertCountEqual(self.selected_ids(plain), ["ses_a0", "ses_a1", "ses_a2"])

        out_bal = self.root / "balanced"
        self.assertEqual(es.main([*base, "--balance-projects", "--out", str(out_bal)]), 0)
        bal = json.loads((out_bal / "meta.json").read_text(encoding="utf-8"))
        self.assertCountEqual(self.selected_ids(bal), ["ses_a0", "ses_a1", "ses_b0"])
        self.assertEqual(bal["stats"]["sessions_by_directory"], {PROJECT: 2, OTHER_PROJECT: 1})
        self.assertTrue(bal["params"]["balance_projects"])

    # 超過 --days 時間窗的 session 不納入。
    def test_time_window_excludes_old_sessions(self):
        self.fx.session("ses_new", age_days=1)
        self.fx.session("ses_old", age_days=30)
        self.fx.user("ses_new", "n")
        self.fx.user("ses_old", "o")
        meta = self.run_extract("--days", "14")
        self.assertEqual(self.selected_ids(meta), ["ses_new"])

    # 同一工具失敗後緊接著再呼叫同一工具，要計為一次錯誤後重試，並彙整錯誤訊息。
    def test_retry_after_error_is_detected(self):
        self.fx.session("ses_r")
        self.fx.assistant(
            "ses_r",
            tool("shell", "error", {"command": "ls -la"}, "command not found"),
            tool("shell", "completed", {"command": "Get-ChildItem"}),
            tool("read", "completed", {"path": "a.txt"}),
        )
        meta = self.run_extract()
        rec = meta["sessions"][0]
        self.assertEqual(rec["retry_after_error"], {"shell": 1})
        self.assertEqual(meta["totals"]["tool_errors"]["shell"], 1)
        self.assertTrue(any("command not found" in e["message"] for e in meta["totals"]["top_errors"]))

    # 對話紀錄須遮蔽 email、密碼、token、手機與身分證號等敏感資訊。
    def test_sensitive_values_are_redacted(self):
        self.fx.session("ses_s", title="客戶 someone@example.com 的問題")
        self.fx.user(
            "ses_s",
            "連線 Server=db;User Id=sa;Password=Secr3t!;，token ghp_abcdefghijklmnopqrstuvwxyz0123456789，"
            "電話 0912345678，身分證 A123456789，Authorization: Bearer eyJabc.def.ghi",
        )
        meta = self.run_extract()
        transcript = (self.out / meta["sessions"][0]["transcript"]).read_text(encoding="utf-8")
        for secret in ["someone@example.com", "Secr3t!", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                       "0912345678", "A123456789", "eyJabc.def.ghi"]:
            self.assertNotIn(secret, transcript)
        self.assertNotIn("someone@example.com", json.dumps(meta, ensure_ascii=False))

    # 命令列參數帶入的密碼（bcp／sqlcmd 的 -P、ijhttp 的 -P 私密參數、--password）也要遮蔽，其餘參數保留。
    def test_command_line_passwords_are_redacted(self):
        self.fx.session("ses_cli")
        self.fx.user(
            "ses_cli",
            'bcp db.dbo.T in "D:\\a.dat" -S host.example -d Db -U app_user -P Pa55w0rd@1 -N -E；'
            "sqlcmd -S h -P \"Quoted Pw\" -Q x；ijhttp -P token=abc123secret run.http；tool --password=Hunter2!",
        )
        meta = self.run_extract()
        transcript = (self.out / meta["sessions"][0]["transcript"]).read_text(encoding="utf-8")
        for secret in ["Pa55w0rd@1", "Quoted Pw", "abc123secret", "Hunter2!"]:
            self.assertNotIn(secret, transcript)
        self.assertIn("-U app_user", transcript)
        self.assertIn("-N -E", transcript)

    # 資料庫必須以唯讀模式開啟，寫入要被 SQLite 拒絕。
    def test_database_is_opened_read_only(self):
        self.fx.session("ses_x")
        self.fx.close()
        conn = es.open_db_readonly(self.fx.path)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("insert into session_v2 (id) values ('ses_y')")
        conn.close()

    # 明確排除的 session，以及載入或呼叫過 --exclude-skill 所列 skill 的 session，都不納入分析。
    def test_excluded_sessions_and_skills_are_skipped(self):
        self.fx.session("ses_keep")
        self.fx.session("ses_self")
        self.fx.session("ses_loaded")
        self.fx.session("ses_invoked")
        self.fx.user("ses_keep", "k")
        self.fx.user("ses_self", "s")
        self.fx.assistant("ses_loaded", tool("skill", "completed", {"id": "reflect"}))
        self.fx.message("ses_invoked", "skill", {"skill": "legacy-skill"})
        meta = self.run_extract(
            "--exclude-session", "ses_self", "--exclude-skill", "reflect", "--exclude-skill", "legacy-skill"
        )
        self.assertEqual(self.selected_ids(meta), ["ses_keep"])
        self.assertEqual(meta["stats"]["sessions_skipped_skill"], 2)

    # 未帶 --exclude-skill 時，載入過任何 skill 的 session 都照常納入。
    def test_no_skill_exclusion_by_default(self):
        self.fx.session("ses_loaded")
        self.fx.assistant("ses_loaded", tool("skill", "completed", {"id": "legacy-skill"}))
        meta = self.run_extract()
        self.assertEqual(self.selected_ids(meta), ["ses_loaded"])

    # --session 只取指定 session：子 session 換成主 session，不受時間窗、目錄與排除條件限制，找不到的 ID 列在 stats。
    def test_explicit_sessions_bypass_filters(self):
        self.fx.session("ses_old_other", directory=OTHER_PROJECT, age_days=60)
        self.fx.session("ses_old_child", parent="ses_old_other", age_days=60)
        self.fx.session("ses_recent")
        self.fx.user("ses_old_other", "舊任務")
        self.fx.assistant("ses_old_other", tool("skill", "completed", {"id": "reflect"}))
        self.fx.user("ses_recent", "最近任務")
        meta = self.run_extract(
            "--session", "ses_old_child", "--session", "ses_missing",
            "--exclude-session", "ses_old_other", "--exclude-skill", "reflect", "--days", "1",
        )
        self.assertEqual(self.selected_ids(meta), ["ses_old_other"])
        self.assertEqual(meta["sessions"][0]["child_sessions"], 1)
        self.assertEqual(meta["stats"]["sessions_not_found"], ["ses_missing"])
        self.assertIsNone(meta["params"]["since"])

    # 未指定 --out 時，暫存輸出目錄以 --label 為前綴。
    def test_label_sets_temp_dir_prefix(self):
        self.fx.session("ses_l")
        self.fx.close()
        argv = ["--db", str(self.fx.path), "--directory", PROJECT, "--no-env-exclude", "--label", "recall"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(es.main(argv), 0)
        out = Path(json.loads(buf.getvalue().strip().splitlines()[-1])["out"])
        try:
            self.assertTrue(out.name.startswith("recall-"))
            self.assertTrue((out / "meta.json").is_file())
        finally:
            shutil.rmtree(out, ignore_errors=True)

    # 助理長回覆要同時保留開頭與結尾，並標示省略字數，避免結尾的結論消失。
    def test_long_assistant_text_keeps_head_and_tail(self):
        self.fx.session("ses_long")
        text = "開頭" + "中" * 3000 + "最後結論"
        self.fx.assistant("ses_long", {"type": "text", "text": text})
        meta = self.run_extract()
        transcript = (self.out / meta["sessions"][0]["transcript"]).read_text(encoding="utf-8")
        self.assertIn("開頭", transcript)
        self.assertIn("最後結論", transcript)
        self.assertIn("（略 ", transcript)

    # 分段後每個選入的 session 恰好出現在一個 slice，且 6 個以上 session 至少分 2 段以利交叉比對。
    def test_slices_cover_every_session_once(self):
        for i in range(7):
            sid = f"ses_{i}"
            self.fx.session(sid, age_days=1 + i * 0.1)
            self.fx.user(sid, f"任務 {i}")
        meta = self.run_extract()
        slices = json.loads((self.out / "slices.json").read_text(encoding="utf-8"))
        flat = [sid for s in slices for sid in s["sessions"]]
        self.assertCountEqual(flat, self.selected_ids(meta))
        self.assertGreaterEqual(len(slices), 2)

    # 沒有符合條件的 session 時仍要輸出 meta.json，且 sessions_selected 為 0。
    def test_no_sessions_still_writes_meta(self):
        meta = self.run_extract()
        self.assertEqual(meta["stats"]["sessions_selected"], 0)


if __name__ == "__main__":
    unittest.main()
