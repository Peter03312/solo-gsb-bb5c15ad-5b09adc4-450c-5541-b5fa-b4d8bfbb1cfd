"""反向通行时间窗测试：方向门、跨窗边界、封闭叠加、等待与选路。

反向窗为半开 ``[start, end)``：
- 窗内仅允许原终点 → 原起点（反向）；
- 窗外仅允许原方向（正向）；
- 方向以进入路段时刻判定，途中跨越窗边界仍走完全程；
- 封闭记录仍约束整个通行区间，与方向无关；允许在节点等待。
"""
import unittest

from app.db import ClosureRow, EdgeRow, ReverseWindowRow, Snapshot
from app.routing import (
    MeetingVisitor,
    NoMeetingCandidateError,
    NoRouteError,
    RouteEngine,
    Stop,
)


def make_engine(nodes, edges, windows=(), closures=()):
    return RouteEngine(
        Snapshot(
            version_id=1,
            nodes=list(nodes),
            edges=[EdgeRow(*e) for e in edges],
            closures=[ClosureRow(*c) for c in closures],
            reverse_windows=[ReverseWindowRow(*w) for w in windows],
        )
    )


def E(eid, u, v, secs, stairs=False):
    return (eid, u, v, secs, stairs)


def W(eid, start, end):
    return (eid, start, end)


class DirectionGateTests(unittest.TestCase):
    def setUp(self):
        # 单边 A -> B，100s；反向窗 [1000, 2000)
        self.eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)], [W("E1", 1000, 2000)]
        )

    def test_forward_outside_window(self):
        # 窗外正向：0 出发 100 到
        r = self.eng.plan("A", "B", 0)
        (s,) = r.segments
        self.assertEqual(s.direction, "forward")
        self.assertEqual((s.from_node, s.to_node), ("A", "B"))
        self.assertEqual((s.enter_time, s.leave_time), (0, 100))

    def test_forward_at_window_end_half_open(self):
        # 2000 时刻窗已结束（半开），立即允许正向进入
        r = self.eng.plan("A", "B", 2000)
        self.assertEqual(r.segments[0].direction, "forward")
        self.assertEqual(r.segments[0].enter_time, 2000)

    def test_forward_inside_window_waits_until_end(self):
        # 1500 处于窗内，正向须在 A 等到窗结束 2000
        r = self.eng.plan("A", "B", 1500)
        s = r.segments[0]
        self.assertEqual(s.direction, "forward")
        self.assertEqual((s.enter_time, s.leave_time), (2000, 2100))
        self.assertEqual(s.waiting_seconds, 500)

    def test_reverse_inside_window(self):
        # 窗内反向：B 1200 出发，立即反向进入
        r = self.eng.plan("B", "A", 1200)
        s = r.segments[0]
        self.assertEqual(s.direction, "reverse")
        self.assertTrue(s.reverse)
        self.assertEqual((s.from_node, s.to_node), ("B", "A"))
        self.assertEqual((s.enter_time, s.leave_time), (1200, 1300))

    def test_reverse_before_window_waits_at_node(self):
        # 窗外想反向：在 B 等到窗开始 1000
        r = self.eng.plan("B", "A", 500)
        s = r.segments[0]
        self.assertEqual(s.direction, "reverse")
        self.assertEqual((s.enter_time, s.leave_time), (1000, 1100))
        self.assertEqual(s.waiting_seconds, 500)

    def test_reverse_at_window_start_half_open(self):
        # 恰好 1000 进入允许（窗半开下界已生效）
        r = self.eng.plan("B", "A", 1000)
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (1000, 1100))

    def test_reverse_after_last_window_is_unreachable(self):
        # 窗外反向且此后无窗 → B 无法到达 A
        with self.assertRaises(NoRouteError):
            self.eng.plan("B", "A", 2000)

    def test_reverse_edge_unknown_without_window(self):
        # 未配置反向窗时行为不变：B -> A 根本不存在
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 100)])
        with self.assertRaises(NoRouteError):
            eng.plan("B", "A", 0)

    def test_wait_for_next_of_multiple_windows(self):
        # 两段窗 [0,50)、[200,300)；B 在 100 须等到 200 才能反向
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 10)],
            [W("E1", 0, 50), W("E1", 200, 300)],
        )
        r = eng.plan("B", "A", 100)
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (200, 210))
        # 第一窗结束后、第二窗开始前正向可走
        r2 = eng.plan("A", "B", 100)
        self.assertEqual((r2.segments[0].enter_time, r2.arrival_time), (100, 110))


class EntryTimeBoundaryTests(unittest.TestCase):
    def test_direction_fixed_at_entry_crossing_window_end(self):
        # 正向边 A->B 60s；窗 [30,60)；0 时刻窗外正向进入，
        # 通行 [0,60) 途中 30 跨入窗、60 出窗，方向仍为正向走完全程
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 60)], [W("E1", 30, 60)])
        r = eng.plan("A", "B", 0)
        self.assertEqual(r.segments[0].direction, "forward")
        self.assertEqual(r.arrival_time, 60)

    def test_reverse_entry_crossing_window_end(self):
        # 反向 30 进入窗 [30,90)，通行 60s 到 90，途中 90 出窗，仍允许走完
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 60)], [W("E1", 30, 90)])
        r = eng.plan("B", "A", 30)
        self.assertEqual(r.segments[0].direction, "reverse")
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (30, 90))

    def test_adjacent_windows_are_allowed(self):
        # [0,50) 与 [50,100) 端点相接：50 落在第二窗内（只反向）
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 10)],
            [W("E1", 0, 50), W("E1", 50, 100)],
        )
        r = eng.plan("B", "A", 50)
        self.assertTrue(r.segments[0].reverse)
        self.assertEqual(r.arrival_time, 60)
        # A 在 50 想正向：50 已在第二窗内 → 等到 100
        r2 = eng.plan("A", "B", 50)
        self.assertEqual((r2.segments[0].enter_time, r2.arrival_time), (100, 110))


class ClosureWithReverseWindowTests(unittest.TestCase):
    def test_closure_covers_reverse_transit(self):
        # A->B 100s；反向窗 [0,2000)；封闭 [50,150)
        # B 在 0 反向进入会与封闭重叠 → 在 B 等到 150（仍在窗内）再进入
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)],
            [W("E1", 0, 2000)], [("E1", 50, 150)],
        )
        r = eng.plan("B", "A", 0)
        s = r.segments[0]
        self.assertEqual(s.direction, "reverse")
        self.assertEqual((s.enter_time, s.leave_time), (150, 250))
        self.assertEqual(s.waiting_seconds, 150)

    def test_closure_wait_moves_forward_out_of_window(self):
        # 正向需求；窗 [0,100)；封闭 [50,200)；边 100s
        # 0 时窗内只反向 → 等窗结束 100；[100,200) 与封闭重叠 → 等 200；
        # 200 时窗外正向且封闭刚结束 → 200 进入，300 到
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)],
            [W("E1", 0, 100)], [("E1", 50, 200)],
        )
        r = eng.plan("A", "B", 0)
        s = r.segments[0]
        self.assertEqual(s.direction, "forward")
        self.assertEqual((s.enter_time, s.leave_time), (200, 300))

    def test_closure_wait_moves_reverse_into_later_window(self):
        # 反向需求；窗 [0,50)、[200,300)；封闭 [0,250)；边 100s
        # 0 在窗内但通行区间撞封闭 → 等 250；250 落在第二窗内，
        # [250,350) 与 [0,250) 半开不重叠 → 反向进入
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 100)],
            [W("E1", 0, 50), W("E1", 200, 300)],
            [("E1", 0, 250)],
        )
        r = eng.plan("B", "A", 0)
        s = r.segments[0]
        self.assertEqual(s.direction, "reverse")
        self.assertEqual((s.enter_time, s.leave_time), (250, 350))

    def test_enter_exactly_at_closure_end_inside_window(self):
        # 封闭 [0,100)、反向窗 [0,500)、边 50s；B 0 等到 100 反向进入
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 50)],
            [W("E1", 0, 500)], [("E1", 0, 100)],
        )
        r = eng.plan("B", "A", 0)
        self.assertEqual((r.segments[0].enter_time, r.arrival_time), (100, 150))
        self.assertTrue(r.segments[0].reverse)


class ReversePathSelectionTests(unittest.TestCase):
    def test_reverse_chain(self):
        # 两条边全天反向窗：C -> B -> A
        eng = make_engine(
            ["A", "B", "C"],
            [E("E1", "A", "B", 10), E("E2", "B", "C", 20)],
            [W("E1", 0, 1000), W("E2", 0, 1000)],
        )
        r = eng.plan("C", "A", 0)
        self.assertEqual([s.edge_id for s in r.segments], ["E2", "E1"])
        self.assertTrue(all(s.reverse for s in r.segments))
        self.assertEqual([(s.from_node, s.to_node) for s in r.segments],
                         [("C", "B"), ("B", "A")])
        self.assertEqual(r.arrival_time, 30)

    def test_mixed_forward_and_reverse_segments(self):
        # E1 A->B（原方向），E2 C->B（原方向），仅 E2 有反向窗
        # A 到 C：正向走 E1 到 B，再反向走 E2 到 C
        eng = make_engine(
            ["A", "B", "C"],
            [E("E1", "A", "B", 10), E("E2", "C", "B", 10)],
            [W("E2", 0, 1000)],
        )
        r = eng.plan("A", "C", 0)
        self.assertEqual(
            [(s.edge_id, s.from_node, s.to_node, s.direction)
             for s in r.segments],
            [("E1", "A", "B", "forward"), ("E2", "B", "C", "reverse")],
        )

    def test_lex_tie_with_reverse_directions(self):
        # T -> S 反向两条等时路径：(T->B=E3,B->S=E1) 与 (T->A=E4,A->S=E2)
        edges = [
            E("E2", "S", "A", 10), E("E4", "A", "T", 10),
            E("E1", "S", "B", 10), E("E3", "B", "T", 10),
        ]
        eng = make_engine(
            ["S", "A", "B", "T"], edges,
            [W(eid, 0, 10000) for eid in ("E1", "E2", "E3", "E4")],
        )
        r = eng.plan("T", "S", 0)
        self.assertEqual([s.edge_id for s in r.segments], ["E3", "E1"])
        self.assertTrue(all(s.reverse for s in r.segments))

    def test_reverse_window_can_create_shortcut(self):
        # 原方向只能 S->A->T（10+100）；X->S 的边在窗内反向 S->X->T 更快
        # EX1 X->S 5s，EX2 T->X 5s，反向窗让 S->X、X->T 可用 → 5+5
        edges = [
            E("EA", "S", "A", 10), E("EB", "A", "T", 100),
            E("EX1", "X", "S", 5), E("EX2", "T", "X", 5),
        ]
        eng = make_engine(
            ["S", "A", "T", "X"], edges,
            [W("EX1", 0, 10000), W("EX2", 0, 10000)],
        )
        r = eng.plan("S", "T", 0)
        self.assertEqual(r.arrival_time, 10)
        self.assertEqual([s.edge_id for s in r.segments], ["EX1", "EX2"])
        self.assertTrue(all(s.reverse for s in r.segments))

    def test_avoid_stairs_filters_reverse_edges(self):
        # E1 A->B 台阶（反向窗全天），E2 C->B 平路（反向窗全天）
        eng = make_engine(
            ["A", "B", "C"],
            [E("E1", "A", "B", 10, stairs=True), E("E2", "C", "B", 10)],
            [W("E1", 0, 10000), W("E2", 0, 10000)],
        )
        r = eng.plan("B", "C", 0, avoid_stairs=True)
        self.assertEqual([s.edge_id for s in r.segments], ["E2"])
        self.assertTrue(r.segments[0].reverse)
        with self.assertRaises(NoRouteError):
            eng.plan("B", "A", 0, avoid_stairs=True)


class ReverseWindowStopsTests(unittest.TestCase):
    def test_stop_then_reverse_segment(self):
        # E1 A->B，E2 C->B（仅 E2 反向窗）；在 B 停留 100s 后反向到 C
        eng = make_engine(
            ["A", "B", "C"],
            [E("E1", "A", "B", 10), E("E2", "C", "B", 10)],
            [W("E2", 0, 10000)],
        )
        r = eng.plan("A", "C", 0, stops=(Stop("B", 0, 1000, 100),))
        self.assertEqual(r.arrival_time, 120)
        (visit,) = r.stops
        self.assertEqual((visit.arrival_time, visit.start_time, visit.end_time),
                         (10, 10, 110))
        self.assertEqual(
            [(s.edge_id, s.direction, s.enter_time, s.leave_time)
             for s in r.segments],
            [("E1", "forward", 0, 10), ("E2", "reverse", 110, 120)],
        )

    def test_arrive_during_window_must_wait_for_forward_leg(self):
        # 第 1 站 B 停留结束时身处反向窗内，正向续程须等窗结束
        # E1 A->B 10s，E2 B->T 10s，E2 反向窗 [0, 200)；
        # B 站停留 0->100 结束，但窗内只反向 → 等 200 再正向 → 210 到 T
        eng = make_engine(
            ["A", "B", "T"],
            [E("E1", "A", "B", 10), E("E2", "B", "T", 10)],
            [W("E2", 0, 200)],
        )
        r = eng.plan("A", "T", 0, stops=(Stop("B", 0, 1000, 100),))
        self.assertEqual(r.segments[1].direction, "forward")
        self.assertEqual((r.segments[1].enter_time, r.arrival_time), (200, 210))

    def test_reverse_leg_between_stops(self):
        # A->B（E1 正向），C->B（E2 原向，窗内反向）
        # 停靠顺序 B -> C：B 不停留，反向 E2 到 C 停靠 20s 结束
        eng = make_engine(
            ["A", "B", "C"],
            [E("E1", "A", "B", 10), E("E2", "C", "B", 10)],
            [W("E2", 0, 10000)],
        )
        r = eng.plan("A", "C", 0, stops=(Stop("C", 0, 1000, 20),))
        self.assertEqual(r.arrival_time, 40)
        (visit,) = r.stops
        self.assertEqual((visit.node, visit.arrival_time, visit.end_time),
                         ("C", 20, 40))
        self.assertEqual(r.segments[1].direction, "reverse")


class ReverseMeetingTests(unittest.TestCase):
    def test_meeting_routes_use_reverse_windows(self):
        # E1 A->B（窗 [2000,3000) 内只反向，窗外正向）；E2 C->B（窗全天，B->C 反向）
        eng = make_engine(
            ["A", "B", "C"],
            [E("E1", "A", "B", 10), E("E2", "C", "B", 10)],
            [W("E1", 2000, 3000), W("E2", 0, 10000)],
        )
        # alice 0 从 A 到汇合点 C：E1 正向 0->10 到 B，E2 反向 10->20 到 C
        # bob 0 从 B 到 C：E2 反向 0->10
        plan = eng.plan_meeting(
            (MeetingVisitor("alice", "A", 0, False),
             MeetingVisitor("bob", "B", 0, False)),
            ("C",), 1000,
        )
        self.assertEqual(plan.node, "C")
        self.assertEqual(plan.meeting_time, 20)
        alice, bob = plan.arrivals
        self.assertEqual([s.direction for s in alice.route.segments],
                         ["forward", "reverse"])
        self.assertEqual(bob.route.segments[0].direction, "reverse")
        # bob 早到 10，等待 alice 到 20
        self.assertEqual(plan.meeting_time - bob.route.arrival_time, 10)

    def test_meeting_candidate_infeasible_when_reverse_window_closed(self):
        # A->B 唯一一条边，反向窗 [100,200)；B 0 时刻反向到 A 须等 100→110
        eng = make_engine(["A", "B"], [E("E1", "A", "B", 10)], [W("E1", 100, 200)])
        plan = eng.plan_meeting(
            (MeetingVisitor(1, "A", 0, False),
             MeetingVisitor(2, "B", 0, False)),
            ("A",), 200,
        )
        self.assertEqual(plan.meeting_time, 110)
        # 最晚汇合 105：B 最早 110 才到 → 无合格候选
        with self.assertRaises(NoMeetingCandidateError):
            eng.plan_meeting(
                (MeetingVisitor(1, "A", 0, False),
                 MeetingVisitor(2, "B", 0, False)),
                ("A",), 105,
            )

    def test_data_version_fixed_per_meeting_query(self):
        # 引擎本身即由单个不可变 Snapshot 构造；此处仅确认反向窗在
        # 全部访客的逐人路线中一致生效（同一固定版本语义）。
        eng = make_engine(
            ["A", "B"], [E("E1", "A", "B", 10)], [W("E1", 0, 1000)]
        )
        plan = eng.plan_meeting(
            (MeetingVisitor("a", "B", 0, False),
             MeetingVisitor("b", "B", 50, False)),
            ("A",), 1000,
        )
        self.assertEqual(
            [a.route.arrival_time for a in plan.arrivals], [10, 60]
        )
        self.assertTrue(all(a.route.segments[0].reverse
                            for a in plan.arrivals))


if __name__ == "__main__":
    unittest.main()
