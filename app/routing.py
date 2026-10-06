"""通行规划引擎。

时间规则
========
封闭记录是半开区间 ``[start, end)``，路段通行区间也是半开
``[enter, leave)``（``leave = enter + travel_seconds``）。
二者重叠当且仅当::

    enter < close_end and close_start < leave

因此允许恰好在封闭结束时刻 ``end`` 进入路段，也允许在 ``end`` 之前
停在节点上等候；通行区间一旦开始就不能与任何封闭区间重叠。

反向通行时间窗
==============
每条边可携带若干互不重叠的半开时间窗 ``[start, end)``：

- **窗内**仅允许**原终点 → 原起点**（反向）进入；
- **窗外**仅允许**原方向**（``from → to``）进入；
- 方向以**进入路段的时刻**判定；途中即使跨越窗边界也按进入时方向
  走完全程（封闭区间仍约束整个通行区间，与方向无关）；
- 窗外想反向进入时，可在（原终点）节点等候到下一个窗的 ``start``；
  窗内想正向进入时，可在（原起点）节点等候到该窗的 ``end``。

由此"在 ready_at 或之后最早可进入时刻（含实际方向）"关于 ready_at
单调非降（FIFO）：可选择的进入时刻集合与方向只随时刻向后推延，封闭
等候逻辑也不变。故时间依赖 Dijkstra 仍然成立。

算法
====
第一阶段：时间依赖的标号设定（Dijkstra 变种）。
``earliest_entry`` 在 FIFO 假设下（晚出发不会更早通过同一条边）
保证最早到达时刻正确，得到终点最早到达 ``T``。

第二阶段：在"到达终点不晚于 T"的状态空间做多标号搜索，状态键为
``(到达时刻, 路段编号序列)``，按该二元组的字典序出堆。边的通行秒数
为正，每次扩展到达时刻严格增大，所以终点第一次出堆即为
"最早到达、同到达时刻路段编号序列字典序最小"的路线。

为什么不能只做一次 Dijkstra
---------------------------
节点等待会把较晚到达的路径"拉平"到相同的后续出发时刻，于是在某个
中间节点上"时刻更优"的路径，续接同一条后缀后可能与另一条路径同时
到达终点，而路段编号序列反而更大。故字典序平局判定必须在第二阶段
对完整序列进行。

按顺序停靠
==========
查询可携带一组**按顺序**完成的停靠点，每站给出开始停留的闭区间
``[earliest, latest]`` 与非负整数停留秒数：

- 抵达某站后可在节点等到窗口开放，开始停留不得晚于 ``latest``，
  结束停留后才能前往下一站；
- 途经尚未轮到的站点（包括未来各站的节点）不算完成停靠，该节点
  此时只当作普通节点通过；
- 起点本身就是第 1 站、或相邻两站位于同一节点时，立即（链式）完成停靠。

选路目标不变：**最终到达**（完成全部停靠并抵达终点）最早优先，平局
按**全程**路段编号序列字典序选取——不得逐段独立打破平局。停靠窗口
的"拉平"效应（晚到但在窗口下界前到达，结束停留时刻相同）意味着
逐段最优拼接不等于全程最优，因此第二阶段在 ``(节点, 已完成停靠数)``
状态空间上做多标签搜索，标签仍为 ``(时刻, 全程编号序列)``。
"""
from __future__ import annotations

import bisect
import heapq
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple, Optional

from .db import Snapshot


@dataclass(frozen=True)
class Edge:
    id: str
    src: str
    dst: str
    seconds: int
    has_stairs: bool


class Traversal(NamedTuple):
    """从某节点出发对一条边的一次可行方向扩展。"""

    edge: Edge
    reverse: bool   # True = 原终点 → 原起点（处于反向窗内进入）

    @property
    def target(self) -> str:
        return self.edge.src if self.reverse else self.edge.dst


class Segment(NamedTuple):
    edge_id: str
    from_node: str
    to_node: str
    enter_time: int          # 进入该路段的时刻（在起点等候后的实际出发时刻）
    leave_time: int          # 离开该路段（到达下一节点）的时刻
    travel_seconds: int
    waiting_seconds: int     # 进入前在 from_node 的等候秒数
    has_stairs: bool
    reverse: bool = False    # 是否以反向（原终点→原起点）通行

    @property
    def direction(self) -> str:
        """实际通行方向：``forward`` 原方向 / ``reverse`` 反向窗内反向。"""
        return "reverse" if self.reverse else "forward"


class Route(NamedTuple):
    origin: str
    destination: str
    departure_time: int      # 用户请求的出发时刻
    arrival_time: int        # 最早到达时刻（完成全部停靠并抵达终点）
    avoid_stairs: bool
    segments: tuple[Segment, ...]
    stops: tuple["StopVisit", ...] = ()   # 各停靠点的抵达/开始/结束记录


class Stop(NamedTuple):
    """一个按顺序停靠点（查询输入，时刻为纪元秒）。"""

    node: str
    earliest: int   # 最早开始停留时刻（闭区间下界）
    latest: int     # 最晚开始停留时刻（闭区间上界）
    stay: int       # 停留秒数（非负整数）


class StopVisit(NamedTuple):
    """一个停靠点的实际停靠记录。"""

    node: str
    arrival_time: int   # 抵达该站时刻
    start_time: int     # 实际开始停留时刻（max(抵达, 窗口下界)）
    end_time: int       # 结束停留时刻（start + stay_seconds）
    stay_seconds: int


class NoRouteError(Exception):
    """没有在约束下可行的路线。"""


class MeetingVisitor(NamedTuple):
    """一名汇合查询访客（查询输入，时刻为纪元秒）。"""

    id: object            # 访客标识（未给 id 时为 1 起始序号）
    origin: str
    departure: int        # 最早出发时刻
    avoid_stairs: bool


class VisitorArrival(NamedTuple):
    """一名访客到某候选点的最早到达结果。"""

    visitor: MeetingVisitor
    route: Route          # 复用既有单人路线查询语义（含全程字典序破平局）


class MeetingPlan(NamedTuple):
    """一次多人汇合点规划的选中结果。"""

    node: str                              # 选中的汇合节点
    meeting_time: int                      # 全员实际汇合时刻（最晚个人抵达）
    arrivals: tuple[VisitorArrival, ...]   # 与请求 visitors 同序
    total_arrival_seconds: int             # 个人抵达时刻总和（二级择优指标）


class NoMeetingCandidateError(Exception):
    """没有任何候选点能让所有人在最晚时刻前抵达。"""


def _overlaps(enter: int, leave: int, start: int, end: int) -> bool:
    """[enter, leave) 与 [start, end) 是否重叠。"""
    return enter < end and start < leave


class RouteEngine:
    def __init__(self, snapshot: Snapshot):
        self.version_id = snapshot.version_id
        self._adj: dict[str, list[Edge]] = defaultdict(list)
        # 反向邻接：节点（即某边原终点 dst）-> 可在此反向进入的边
        self._radj: dict[str, list[Edge]] = defaultdict(list)
        self._edges: dict[str, Edge] = {}
        for row in snapshot.edges:
            edge = Edge(row.id, row.src, row.dst, row.seconds, row.has_stairs)
            if edge.id in self._edges:
                raise ValueError(f"路段编号在版本内重复: {edge.id}")
            self._edges[edge.id] = edge
            self._adj[edge.src].append(edge)
            self._radj[edge.dst].append(edge)
        for edges in self._adj.values():
            edges.sort(key=lambda e: e.id)
        for edges in self._radj.values():
            edges.sort(key=lambda e: e.id)

        self._closures: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for c in snapshot.closures:
            self._closures[c.edge_id].append((c.start, c.end))
        for intervals in self._closures.values():
            intervals.sort()

        # 反向通行窗：edge_id -> 按起点排序的 [start, end) 列表（互不重叠）
        self._reverse_windows: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for w in snapshot.reverse_windows:
            self._reverse_windows[w.edge_id].append((w.start, w.end))
        for intervals in self._reverse_windows.values():
            intervals.sort()

    def _traversals(self, node: str, avoid_stairs: bool) -> list[Traversal]:
        """在 ``node`` 可尝试进入的全部（边, 方向）选项。

        正向选项来自原起点邻接，反向选项来自原终点邻接；是否真正可进入
        及最早进入时刻由 :meth:`earliest_entry` 按当前时刻与时间窗判定。
        """
        out: list[Traversal] = []
        for edge in self._adj.get(node, ()):
            if avoid_stairs and edge.has_stairs:
                continue
            out.append(Traversal(edge, False))
        for edge in self._radj.get(node, ()):
            if avoid_stairs and edge.has_stairs:
                continue
            out.append(Traversal(edge, True))
        return out

    @staticmethod
    def _in_windows(t: int, windows: list[tuple[int, int]]) -> bool:
        """时刻 ``t`` 是否落在任一窗 [start, end) 内。"""
        i = bisect.bisect_right(windows, (t, float("inf"))) - 1
        return i >= 0 and windows[i][0] <= t < windows[i][1]

    def _next_window_start(self, t: int, windows: list[tuple[int, int]]) -> Optional[int]:
        """严格晚于 ``t`` 开始的下一个反向窗起点；没有返回 None。

        端点相接（t == 某窗 end == 下窗 start）时返回该下窗起点，因为
        半开区间下 ``t`` 已落在窗外，须在下窗开始后才能反向进入。
        """
        starts = [s for s, _ in windows]
        i = bisect.bisect_right(starts, t)
        return windows[i][0] if i < len(windows) else None

    def _current_window_end(self, t: int, windows: list[tuple[int, int]]) -> Optional[int]:
        """若 ``t`` 落在某反向窗内，返回该窗的 end；否则 None。"""
        i = bisect.bisect_right(windows, (t, float("inf"))) - 1
        if i >= 0 and windows[i][0] <= t < windows[i][1]:
            return windows[i][1]
        return None

    def _direction_open(self, edge: Edge, t: int, reverse: bool) -> Optional[int]:
        """方向门：时刻 ``t`` 是否允许以 ``reverse`` 方向进入。

        允许时返回 ``t``；不允许时返回**最早**重新允许该方向的时刻：
        窗外想反向 → 下一个窗的 start（无后续窗则不可行，返回 None）；
        窗内想正向 → 当前窗的 end。
        """
        windows = self._reverse_windows.get(edge.id, ())
        in_window = self._in_windows(t, windows)
        if reverse == in_window:
            return t  # 窗内反向、窗外正向，方向一致
        if in_window:
            return self._current_window_end(t, windows)  # 正向须等当前窗结束
        nxt = self._next_window_start(t, windows)        # 反向须等下一窗开始
        return nxt

    def earliest_entry(
        self, edge: Edge, ready_at: int, reverse: bool
    ) -> Optional[int]:
        """在 ``ready_at`` 或之后，最早可以以 ``reverse`` 方向进入该边的时刻。

        不可行（例如窗外想反向且此后再无反向窗）时返回 None。

        两类约束交替满足：
        1. **方向门**（反向时间窗）：窗内仅反向、窗外仅正向；
        2. **封闭记录**：整个通行区间 [enter, enter+seconds) 不得与任何
           封闭区间重叠，与通行方向无关；不满足则推迟到该封闭的 end。

        每轮调整都把时刻推到某个严格更晚的区间边界，故必然终止；
        结果关于 ``ready_at`` 单调非降（FIFO）。
        """
        t = ready_at
        # 边界数量有限，双重调整至多 windows + closures 轮
        for _ in range(len(self._closures.get(edge.id, ()))
                      + len(self._reverse_windows.get(edge.id, ())) + 2):
            opened = self._direction_open(edge, t, reverse)
            if opened is None:
                return None  # 此后再无允许该方向的时段
            t = opened
            bumped = False
            for start, end in self._closures.get(edge.id, ()):
                if end <= t:
                    continue
                if start >= t + edge.seconds:
                    break  # 已按 start 排序，后面的封闭更不可能重叠
                if _overlaps(t, t + edge.seconds, start, end):
                    t = end
                    bumped = True
                    break  # 封闭调整可能跨进/跨出反向窗，回到外层重判方向门
            if not bumped:
                return t
        # 理论上不可达：每轮都严格推进且边界有限
        return None

    # ------------------------------------------------------------------
    # 第一阶段：最早到达时刻
    # ------------------------------------------------------------------
    def _earliest_arrival_map(
        self, origin: str, departure: int, avoid_stairs: bool
    ) -> dict[str, int]:
        dist: dict[str, int] = {origin: departure}
        heap: list[tuple[int, str]] = [(departure, origin)]
        while heap:
            t, u = heapq.heappop(heap)
            if t != dist[u]:
                continue
            for trav in self._traversals(u, avoid_stairs):
                enter = self.earliest_entry(trav.edge, t, trav.reverse)
                if enter is None:
                    continue  # 该方向此后再无允许时段
                arrival = enter + trav.edge.seconds
                if arrival < dist.get(trav.target, float("inf")):
                    dist[trav.target] = arrival
                    heapq.heappush(heap, (arrival, trav.target))
        return dist

    # ------------------------------------------------------------------
    # 第二阶段：最早到达前提下，路段编号序列字典序最小
    # ------------------------------------------------------------------
    @staticmethod
    def _dominates(t1: int, s1: tuple[str, ...], t2: int, s2: tuple[str, ...]) -> bool:
        """标签 1 是否支配标签 2（对任意同后缀都不会更差）。

        两条规则，都要避开“前缀陷阱”——较短序列追加后缀后字典序可能翻转：

        1. 到达时刻相同：正秒数下图中不存在零耗时环，同一节点上同到达
           时刻的两条不同序列不可能互为前缀，可直接按字典序裁剪；
        2. 到达时刻严格更早：只有**等长**序列才安全（等长时首个不同
           元素的比较与追加同后缀后的完整比较一致）；长度不同且互为
           前缀的必须同时保留。
        """
        if t1 > t2:
            return False
        if t1 == t2:
            return s1 < s2
        # t1 < t2
        if len(s1) != len(s2):
            return False
        return s1 < s2

    def _lex_smallest(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool,
        deadline: int,
    ) -> tuple[str, ...]:
        """在所有“到达终点不晚于 deadline”的路线中取序列字典序最小者。

        不能在终点标签第一次出堆时返回：堆键 ``(到达时刻, 序列)`` 只在
        同一节点间有可比性，而时刻更小的中间节点标签可能继续扩展出更优的
        终点标签。故完整展开搜索（秒数为正 + deadline 剪枝保证终止），
        最后在终点的标签集合里取二元组最小项。
        """
        # 每个节点保留一组互不支配的标签
        labels: dict[str, list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
        labels[origin].append((departure, ()))
        heap: list[tuple[int, tuple[str, ...], str]] = [(departure, (), origin)]

        while heap:
            t, seq, u = heapq.heappop(heap)
            if (t, seq) not in labels.get(u, ()):
                continue  # 已被支配后移除的过期标签

            for trav in self._traversals(u, avoid_stairs):
                edge = trav.edge
                enter = self.earliest_entry(edge, t, trav.reverse)
                if enter is None:
                    continue
                leave = enter + edge.seconds
                if leave > deadline:
                    continue  # 不可能参与“最早到达 T”的路线
                new_seq = seq + (edge.id,)
                new_label = (leave, new_seq)

                existing = labels[trav.target]
                if any(self._dominates(et, es, leave, new_seq) for et, es in existing):
                    continue
                # 新标签支配旧标签则移除（前缀不可比的保留）
                labels[trav.target] = [
                    (et, es) for et, es in existing
                    if not self._dominates(leave, new_seq, et, es)
                ]
                labels[trav.target].append(new_label)
                heapq.heappush(heap, (leave, new_seq, trav.target))

        best = min(labels.get(destination, ()), default=None, key=lambda item: (item[0], item[1]))
        if best is None:
            raise NoRouteError("内部错误：最早到达已知，但字典序选路失败")
        return best[1]

    # ------------------------------------------------------------------
    def plan(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool = False,
        stops: tuple[Stop, ...] = (),
    ) -> Route:
        if stops:
            return self._plan_with_stops(origin, destination, departure, avoid_stairs, stops)

        dist = self._earliest_arrival_map(origin, departure, avoid_stairs)
        if destination not in dist:
            raise NoRouteError(
                "在当前步道图与封闭记录下不存在可行路线"
                + ("（已避开台阶路段）" if avoid_stairs else "")
            )
        deadline = dist[destination]
        best_seq = self._lex_smallest(
            origin, destination, departure, avoid_stairs, deadline
        )

        segments = self._replay(origin, departure, best_seq)[0]
        return Route(
            origin=origin,
            destination=destination,
            departure_time=departure,
            arrival_time=deadline,
            avoid_stairs=avoid_stairs,
            segments=tuple(segments),
        )

    def _replay(
        self,
        origin: str,
        departure: int,
        edge_ids: tuple[str, ...],
        avoid_stairs: bool = False,
        stops: tuple[Stop, ...] = (),
    ) -> tuple[list[Segment], list[StopVisit]]:
        """按获胜的路段编号序列重放，逐边判定实际方向并生成逐段明细。

        序列只记录路段编号；每到一个节点，根据"最早可进入时刻 + 方向"
        与当前时刻唯一确定该边该以哪个方向进入（除自环且恰在窗边界的
        退化情形外，正、反两个方向不可能同为最早；自环为正秒数环，永远
        不会出现在最早到达路线中）。

        带停靠时，每抵达当前轮到的站点先完成（可能链式的）停留，时钟
        跳到停留结束后再续接下一路段；同时返回各站停靠记录。
        """
        segments: list[Segment] = []
        visits: list[StopVisit] = []
        current = origin
        clock = departure
        k = 0

        # 起点处可能立即（链式）完成停靠
        k, clock, v0 = self._stays_at(origin, k, clock, stops)
        if v0 is not None:
            visits.extend(v0)

        for edge_id in edge_ids:
            edge = self._edges[edge_id]
            candidates = [
                tr for tr in self._traversals(current, avoid_stairs)
                if tr.edge.id == edge_id
            ]
            chosen: Optional[tuple[int, Traversal]] = None
            for trav in candidates:
                enter = self.earliest_entry(edge, clock, trav.reverse)
                if enter is None:
                    continue
                if chosen is None or enter < chosen[0]:
                    chosen = (enter, trav)
            if chosen is None:
                # 获胜序列必然可行；走到这里属于内部一致性错误
                raise NoRouteError(
                    f"内部错误：重放获胜序列时路段 {edge_id} 在节点 {current} 不可进入"
                )
            enter, trav = chosen
            leave = enter + edge.seconds
            target = edge.src if trav.reverse else edge.dst
            segments.append(
                Segment(
                    edge_id=edge.id,
                    from_node=current,
                    to_node=target,
                    enter_time=enter,
                    leave_time=leave,
                    travel_seconds=edge.seconds,
                    waiting_seconds=enter - clock,
                    has_stairs=edge.has_stairs,
                    reverse=trav.reverse,
                )
            )
            clock = leave
            current = target
            if k < len(stops) and stops[k].node == current:
                k, clock, vk = self._stays_at(current, k, clock, stops)
                if vk is not None:
                    visits.extend(vk)
        return segments, visits

    # ------------------------------------------------------------------
    # 按顺序停靠：窗口应用与两阶段选路
    # ------------------------------------------------------------------
    @staticmethod
    def _stays_at(node: str, k: int, clock: int, stops: tuple[Stop, ...]):
        """在 ``node`` 上链式完成所有“当前轮到且地点即 node”的停靠。

        返回 ``(k', clock', visits)``；若某站无法按窗口停留则返回
        ``(k, clock, None)`` 表示该走法不可行。
        """
        visits: list[StopVisit] = []
        while k < len(stops) and stops[k].node == node:
            stop = stops[k]
            start = max(clock, stop.earliest)
            if start > stop.latest:
                return k, clock, None
            end = start + stop.stay
            visits.append(StopVisit(node, clock, start, end, stop.stay))
            clock = end
            k += 1
        return k, clock, visits

    def _earliest_final_with_stops(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool,
        stops: tuple[Stop, ...],
    ) -> int:
        """第一阶段：完成全部停靠并抵达终点的最早时刻。

        各腿的最早到达映射与“停留结束时刻”都是关于出发时刻的单调
        非降函数，其复合亦然，故逐腿取最早到达即得全局最早最终到达；
        同时最早到达时若窗口已关，则更晚到达也不可能满足窗口。
        """
        suffix = "（已避开台阶路段）" if avoid_stairs else ""
        clock = departure
        current = origin
        k = 0
        while True:
            # 当前节点即下一停靠点时立即（链式）完成停靠
            k, clock, visits = self._stays_at(current, k, clock, stops)
            if visits is None:
                raise NoRouteError(
                    f"第 {k + 1} 站（{stops[k].node}）无法按时停留："
                    f"开始停留不得晚于该站最晚时刻"
                )
            if k >= len(stops):
                break
            stop = stops[k]
            dist = self._earliest_arrival_map(current, clock, avoid_stairs)
            if stop.node not in dist:
                raise NoRouteError(f"无法到达第 {k + 1} 站（{stop.node}）{suffix}")
            clock = dist[stop.node]
            current = stop.node
        if current != destination:
            dist = self._earliest_arrival_map(current, clock, avoid_stairs)
            if destination not in dist:
                raise NoRouteError(
                    "在当前步道图与封闭记录下不存在可行路线" + suffix
                )
            clock = dist[destination]
        return clock

    def _lex_smallest_with_stops(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool,
        stops: tuple[Stop, ...],
        deadline: int,
    ) -> tuple[str, ...]:
        """在“最终到达不晚于 deadline”的走法中取全程编号序列字典序最小者。

        状态为 ``(节点, 已完成停靠数)``；抵达当前轮到的站点即强制完成
        停靠（可链式），途经未来站点不产生停靠。支配规则与单段情形相同，
        因为同一状态下任意共同后缀的窗口/封闭行为一致。
        """
        k0, t0, visits0 = self._stays_at(origin, 0, departure, stops)
        if visits0 is None:
            raise NoRouteError("第 1 站即起点，但出发时刻已错过其最晚开始停留时刻")

        labels: dict[tuple[str, int], list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
        labels[(origin, k0)].append((t0, ()))
        heap: list[tuple[int, tuple[str, ...], str, int]] = [(t0, (), origin, k0)]

        while heap:
            t, seq, u, k = heapq.heappop(heap)
            if (t, seq) not in labels.get((u, k), ()):
                continue  # 已被支配后移除的过期标签

            for trav in self._traversals(u, avoid_stairs):
                edge = trav.edge
                enter = self.earliest_entry(edge, t, trav.reverse)
                if enter is None:
                    continue
                leave = enter + edge.seconds
                if leave > deadline:
                    continue  # 不可能参与“最早到达”的路线
                nk, nt = k, leave
                if nk < len(stops) and stops[nk].node == trav.target:
                    nk, nt, visits = self._stays_at(trav.target, nk, leave, stops)
                    if visits is None or nt > deadline:
                        continue  # 窗口不可行或注定超过最早到达
                new_seq = seq + (edge.id,)
                new_label = (nt, new_seq)

                state = (trav.target, nk)
                existing = labels[state]
                if any(self._dominates(et, es, nt, new_seq) for et, es in existing):
                    continue
                labels[state] = [
                    (et, es) for et, es in existing
                    if not self._dominates(nt, new_seq, et, es)
                ]
                labels[state].append(new_label)
                heapq.heappush(heap, (nt, new_seq, trav.target, nk))

        best = min(
            labels.get((destination, len(stops)), ()),
            default=None,
            key=lambda item: (item[0], item[1]),
        )
        if best is None:
            raise NoRouteError("内部错误：最早到达已知，但字典序选路失败")
        return best[1]

    def _plan_with_stops(
        self,
        origin: str,
        destination: str,
        departure: int,
        avoid_stairs: bool,
        stops: tuple[Stop, ...],
    ) -> Route:
        deadline = self._earliest_final_with_stops(
            origin, destination, departure, avoid_stairs, stops
        )
        best_seq = self._lex_smallest_with_stops(
            origin, destination, departure, avoid_stairs, stops, deadline
        )

        # 按获胜序列重放：逐段明细（含实际方向）与各站停靠记录一并生成。
        segments, visits = self._replay(
            origin, departure, best_seq, avoid_stairs, stops
        )

        return Route(
            origin=origin,
            destination=destination,
            departure_time=departure,
            arrival_time=deadline,
            avoid_stairs=avoid_stairs,
            segments=tuple(segments),
            stops=tuple(visits),
        )

    # ------------------------------------------------------------------
    # 多人汇合点：候选点筛选与择优
    # ------------------------------------------------------------------
    def plan_meeting(
        self,
        visitors: tuple[MeetingVisitor, ...],
        candidates: tuple[str, ...],
        latest_meeting_time: int,
    ) -> MeetingPlan:
        """在候选节点中选出全员可在最晚时刻前抵达的汇合点。

        每名访客从各自最早出发时刻独立通行（封闭等候、避台阶、最早到达
        及全程路段编号序列字典序破平局的单人路线语义不变），抵达后可在
        汇合点等待。只接受**所有**访客抵达时刻均不晚于
        ``latest_meeting_time`` 的候选点。

        合格候选按以下顺序择优：
        1. 全员实际汇合时刻（最晚个人抵达时刻）最早；
        2. 个人抵达时刻总和最小；
        3. 候选节点编号字典序最小。
        """
        # (汇合时刻, 抵达总和, 节点编号) 三级字典序即择优顺序
        best_key: Optional[tuple[int, int, str]] = None
        best_arrivals: Optional[tuple[VisitorArrival, ...]] = None
        best_node: Optional[str] = None

        for node in candidates:
            arrivals: list[VisitorArrival] = []
            feasible = True
            for visitor in visitors:
                try:
                    route = self.plan(
                        origin=visitor.origin,
                        destination=node,
                        departure=visitor.departure,
                        avoid_stairs=visitor.avoid_stairs,
                    )
                except NoRouteError:
                    feasible = False
                    break
                if route.arrival_time > latest_meeting_time:
                    feasible = False
                    break
                arrivals.append(VisitorArrival(visitor, route))
            if not feasible:
                continue

            meeting_time = max(a.route.arrival_time for a in arrivals)
            total = sum(a.route.arrival_time for a in arrivals)
            key = (meeting_time, total, node)
            if best_key is None or key < best_key:
                best_key = key
                best_node = node
                best_arrivals = tuple(arrivals)

        if best_key is None:
            raise NoMeetingCandidateError(
                "没有任何候选节点能让所有访客在最晚汇合时刻前抵达"
            )
        return MeetingPlan(
            node=best_node,
            meeting_time=best_key[0],
            arrivals=best_arrivals,
            total_arrival_seconds=best_key[1],
        )


def build_engine(snapshot: Snapshot) -> RouteEngine:
    return RouteEngine(snapshot)