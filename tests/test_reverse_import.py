"""反向通行窗的导入校验、原子拒绝与版本持久化测试。"""
import os
import tempfile
import unittest
from pathlib import Path

from app.db import (
    connect,
    current_version,
    get_snapshot,
    init_db,
    publish_closures,
    publish_graph,
)
from app.schemas import ValidationError

T0 = "2026-10-05T08:00:00Z"
T1 = "2026-10-05T09:00:00Z"
T2 = "2026-10-05T10:00:00Z"
T3 = "2026-10-05T11:00:00Z"


def base_graph(**edge_kw):
    edge = {"id": "E1", "from": "A", "to": "B",
            "travel_seconds": 10, "has_stairs": False}
    edge.update(edge_kw)
    return {"nodes": ["A", "B"], "edges": [edge]}


class ReverseWindowValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["APP_DB_PATH"] = self.db_path
        self.conn = connect(Path(self.db_path))
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def test_graph_without_reverse_windows_stays_compatible(self):
        v = publish_graph(self.conn, base_graph())
        snap = get_snapshot(self.conn, v)
        self.assertEqual(list(snap.reverse_windows), [])

    def test_null_reverse_windows_treated_as_absent(self):
        payload = base_graph()
        payload["reverse_windows"] = None
        v = publish_graph(self.conn, payload)
        self.assertEqual(list(get_snapshot(self.conn, v).reverse_windows), [])

    def test_valid_single_window_persisted(self):
        payload = base_graph()
        payload["reverse_windows"] = {"E1": [{"start": T0, "end": T1}]}
        v = publish_graph(self.conn, payload)
        snap = get_snapshot(self.conn, v)
        wins = snap.reverse_windows
        self.assertEqual(len(wins), 1)
        self.assertEqual((wins[0].edge_id, wins[0].start, wins[0].end),
                         ("E1", _epoch(T0), _epoch(T1)))

    def test_multiple_non_overlapping_windows_allowed(self):
        payload = base_graph()
        payload["reverse_windows"] = {
            "E1": [{"start": T0, "end": T1}, {"start": T1, "end": T2},
                   {"start": T2, "end": T3}]
        }
        v = publish_graph(self.conn, payload)
        self.assertEqual(len(get_snapshot(self.conn, v).reverse_windows), 3)

    def test_unknown_edge_rejected(self):
        payload = base_graph()
        payload["reverse_windows"] = {"GHOST": [{"start": T0, "end": T1}]}
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, payload)
        self.assertEqual(current_version(self.conn), 0)

    def test_invalid_window_interval_rejected(self):
        for bad_end in (T0, "2026-10-05T07:00:00Z"):
            payload = base_graph()
            payload["reverse_windows"] = {"E1": [{"start": T0, "end": bad_end}]}
            with self.assertRaises(ValidationError):
                publish_graph(self.conn, payload)

    def test_bad_time_format_rejected(self):
        payload = base_graph()
        payload["reverse_windows"] = {"E1": [{"start": "noon", "end": T1}]}
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, payload)

    def test_missing_fields_rejected(self):
        for item in ({"start": T0}, {"end": T1}):
            payload = base_graph()
            payload["reverse_windows"] = {"E1": [item]}
            with self.assertRaises(ValidationError):
                publish_graph(self.conn, payload)

    def test_window_entry_must_be_object(self):
        payload = base_graph()
        payload["reverse_windows"] = {"E1": ["not-object"]}
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, payload)

    def test_windows_must_be_list(self):
        payload = base_graph()
        payload["reverse_windows"] = {"E1": {"start": T0, "end": T1}}
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, payload)

    def test_reverse_windows_must_be_object(self):
        payload = base_graph()
        payload["reverse_windows"] = [{"start": T0, "end": T1}]
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, payload)

    def test_overlapping_windows_reject_whole_import(self):
        # 重叠（非端点相接）必须拒绝
        payload = base_graph()
        payload["reverse_windows"] = {
            "E1": [{"start": T0, "end": T2}, {"start": T1, "end": T3}]
        }
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, payload)
        self.assertEqual(current_version(self.conn), 0)

    def test_overlapping_in_second_edge_still_rejects(self):
        graph = {
            "nodes": ["A", "B", "C"],
            "edges": [
                {"id": "E1", "from": "A", "to": "B",
                 "travel_seconds": 10, "has_stairs": False},
                {"id": "E2", "from": "B", "to": "C",
                 "travel_seconds": 10, "has_stairs": False},
            ],
        }
        publish_graph(self.conn, graph)
        graph["reverse_windows"] = {
            "E1": [{"start": T0, "end": T1}],
            "E2": [{"start": T0, "end": T2}, {"start": T1, "end": T3}],
        }
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, graph)

    def test_rejection_keeps_old_version_intact(self):
        # 旧版本带有合法反向窗；新导入因窗口重叠被拒，旧版本完整保留
        old = base_graph()
        old["reverse_windows"] = {"E1": [{"start": T0, "end": T1}]}
        v1 = publish_graph(self.conn, old)
        bad = base_graph()
        bad["reverse_windows"] = {
            "E1": [{"start": T0, "end": T2}, {"start": T1, "end": T3}]
        }
        with self.assertRaises(ValidationError):
            publish_graph(self.conn, bad)
        self.assertEqual(current_version(self.conn), v1)
        snap = get_snapshot(self.conn)
        self.assertEqual(len(snap.reverse_windows), 1)
        self.assertEqual((snap.reverse_windows[0].start, snap.reverse_windows[0].end),
                         (_epoch(T0), _epoch(T1)))

    def test_graph_republish_replaces_reverse_windows(self):
        old = base_graph()
        old["reverse_windows"] = {"E1": [{"start": T0, "end": T1}]}
        publish_graph(self.conn, old)
        # 新图不带 reverse_windows：旧窗随图整体替换而清除
        v3 = publish_graph(self.conn, base_graph())
        snap = get_snapshot(self.conn, v3)
        self.assertEqual(list(snap.reverse_windows), [])

    def test_windows_dropped_when_edge_removed(self):
        graph = {
            "nodes": ["A", "B", "C"],
            "edges": [
                {"id": "E1", "from": "A", "to": "B",
                 "travel_seconds": 10, "has_stairs": False},
                {"id": "E2", "from": "B", "to": "C",
                 "travel_seconds": 10, "has_stairs": False},
            ],
            "reverse_windows": {
                "E1": [{"start": T0, "end": T1}],
                "E2": [{"start": T0, "end": T1}],
            },
        }
        publish_graph(self.conn, graph)
        new_graph = {
            "nodes": ["B", "C"],
            "edges": [{"id": "E2", "from": "B", "to": "C",
                       "travel_seconds": 10, "has_stairs": False}],
        }
        v = publish_graph(self.conn, new_graph)
        snap = get_snapshot(self.conn, v)
        self.assertEqual([w.edge_id for w in snap.reverse_windows], [])

    def test_epoch_seconds_accepted_as_window_times(self):
        payload = base_graph()
        payload["reverse_windows"] = {"E1": [{"start": 100, "end": 200}]}
        v = publish_graph(self.conn, payload)
        win = get_snapshot(self.conn, v).reverse_windows[0]
        self.assertEqual((win.start, win.end), (100, 200))

    def test_empty_window_list_allowed(self):
        payload = base_graph()
        payload["reverse_windows"] = {"E1": []}
        v = publish_graph(self.conn, payload)
        self.assertEqual(list(get_snapshot(self.conn, v).reverse_windows), [])

    def test_integer_edge_key_normalized_to_string(self):
        # JSON 对象键实际只能是字符串；直接走规范化层确认整数键也接受
        from app.schemas import normalize_graph

        graph = base_graph()
        graph["reverse_windows"] = {1: [{"start": T0, "end": T1}]}
        # 整数键规范化为 "1"，与边 "E1" 不匹配 → 引用不存在路段
        with self.assertRaises(ValidationError):
            normalize_graph(graph)

    def test_bool_or_blank_edge_key_rejected(self):
        from app.schemas import normalize_graph

        for bad_key in (True, "  "):
            graph = base_graph()
            graph["reverse_windows"] = {bad_key: [{"start": T0, "end": T1}]}
            with self.assertRaises(ValidationError):
                normalize_graph(graph)

    def test_closures_still_independent_after_window_graph(self):
        payload = base_graph()
        payload["reverse_windows"] = {"E1": [{"start": T0, "end": T1}]}
        publish_graph(self.conn, payload)
        v2 = publish_closures(
            self.conn, {"closures": [{"edge_id": "E1", "start": T1, "end": T2}]}
        )
        snap = get_snapshot(self.conn, v2)
        self.assertEqual(len(snap.closures), 1)
        self.assertEqual(len(snap.reverse_windows), 1)


def _epoch(s):
    from app.timeutil import parse_time

    return parse_time(s)


if __name__ == "__main__":
    unittest.main()
