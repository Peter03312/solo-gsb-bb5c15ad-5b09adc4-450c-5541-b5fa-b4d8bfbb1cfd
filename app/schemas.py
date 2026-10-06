"""导入与查询负载的校验、规范化。

所有校验在写库事务开启之前完成，错误以 :class:`ValidationError` 抛出，
调用方负责返回 400 并保留数据库旧版本。
"""
from typing import Any

from .timeutil import TimeError, format_time, parse_time


class ValidationError(ValueError):
    """负载非法（未知节点、非正秒数、无效时间区间等）。"""


def _require_object(payload: Any, kind: str) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError(f"{kind}必须是 JSON 对象")
    return payload


def _clean_identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f"{field}必须是非空字符串")
    text = str(value).strip()
    if not text:
        raise ValidationError(f"{field}不能为空")
    return text


def _validate_seconds(value: Any) -> int:
    # 严格类型：拒绝 bool、float、数字字符串等隐式转换
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("通行秒数(travel_seconds)必须是正整数")
    if value <= 0:
        raise ValidationError(f"通行秒数(travel_seconds)必须为正数，收到 {value}")
    return value


def _validate_stairs(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("台阶标记(has_stairs)必须是布尔值 true/false")
    return value


def _normalize_reverse_windows(
    raw: Any, edge_ids: set[str]
) -> list[dict]:
    """校验反向通行时间窗（按路段分组）。

    期望格式（键为路段编号，值为半开时间窗数组）::

        {"reverse_windows": {
            "E1": [{"start": "2026-10-05T08:00:00Z",
                    "end":   "2026-10-05T10:00:00Z"}]}}

    每个窗为半开 ``[start, end)``，必须满足 ``start < end``；同一路段的
    窗两两不得重叠（端点相接允许：``[a,b)`` 与 ``[b,c)`` 合法）。窗内只
    允许原终点走向原起点，窗外只允许原方向。
    """
    if not isinstance(raw, dict):
        raise ValidationError("reverse_windows 必须是对象（路段编号 -> 时间窗数组）")

    # 先规范化键（拒绝 bool、非字符串/整数、空串），再按编号稳定排序报错
    keyed: list[tuple[str, Any]] = []
    for raw_key in raw.keys():
        edge_key = _clean_identifier(raw_key, "reverse_windows 的路段编号")
        keyed.append((edge_key, raw[raw_key]))
    keyed.sort(key=lambda kv: kv[0])

    windows: list[dict] = []
    for edge_id, raw_wins in keyed:
        if edge_id not in edge_ids:
            raise ValidationError(
                f"reverse_windows[{edge_id!r}] 引用了不存在的路段: {edge_id!r}"
            )
        if not isinstance(raw_wins, list):
            raise ValidationError(f"reverse_windows[{edge_id!r}] 必须是数组")

        intervals: list[tuple[int, int]] = []
        for j, item in enumerate(raw_wins):
            where = f"reverse_windows[{edge_id!r}][{j}]"
            if not isinstance(item, dict):
                raise ValidationError(f"{where} 必须是对象")
            if "start" not in item:
                raise ValidationError(f"{where} 缺少 start")
            if "end" not in item:
                raise ValidationError(f"{where} 缺少 end")
            try:
                start = parse_time(item["start"])
                end = parse_time(item["end"])
            except TimeError as exc:
                raise ValidationError(f"{where}: {exc}") from None
            if start >= end:
                raise ValidationError(
                    f"{where} 无效时间窗: start({item['start']}) 必须早于 end({item['end']})"
                )
            intervals.append((start, end))

        intervals.sort()
        for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
            if s2 < e1:
                raise ValidationError(
                    f"reverse_windows[{edge_id!r}] 存在重叠时间窗: "
                    f"[{format_time(s1)}, {format_time(e1)}) 与 "
                    f"[{format_time(s2)}, {format_time(e2)})"
                )
        for start, end in intervals:
            windows.append({"edge_id": edge_id, "start": start, "end": end})
    return windows


def normalize_graph(payload: Any) -> dict:
    """校验步道图导入负载，返回规范化结构。

    期望格式::

        {
          "nodes": ["gate", "plaza", ...],
          "edges": [
            {"id": "E1", "from": "gate", "to": "plaza",
             "travel_seconds": 120, "has_stairs": false},
            ...
          ],
          "reverse_windows": {              # 可选，缺省行为不变
            "E1": [{"start": "...", "end": "..."}]
          }
        }
    """
    data = _require_object(payload, "步道图")
    raw_nodes = data.get("nodes")
    raw_edges = data.get("edges")
    if not isinstance(raw_nodes, list):
        raise ValidationError("nodes 必须是数组")
    if not isinstance(raw_edges, list):
        raise ValidationError("edges 必须是数组")

    nodes: list[str] = []
    node_set: set[str] = set()
    for i, raw in enumerate(raw_nodes):
        node = _clean_identifier(raw, f"nodes[{i}]")
        if node in node_set:
            raise ValidationError(f"节点重复: {node!r}")
        node_set.add(node)
        nodes.append(node)

    edges: list[dict] = []
    edge_ids: set[str] = set()
    for i, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise ValidationError(f"edges[{i}] 必须是对象")
        where = f"edges[{i}]"
        edge_id = _clean_identifier(raw.get("id"), f"{where}.id")
        if edge_id in edge_ids:
            raise ValidationError(f"路段编号重复: {edge_id!r}")
        edge_ids.add(edge_id)

        src = _clean_identifier(raw.get("from"), f"{where}.from")
        dst = _clean_identifier(raw.get("to"), f"{where}.to")
        unknown = [n for n in (src, dst) if n not in node_set]
        if unknown:
            raise ValidationError(f"{where} 引用了未定义的节点: {', '.join(unknown)}")

        if "travel_seconds" not in raw:
            raise ValidationError(f"{where} 缺少 travel_seconds")
        seconds = _validate_seconds(raw["travel_seconds"])
        has_stairs = _validate_stairs(raw.get("has_stairs", False))

        edges.append(
            {
                "id": edge_id,
                "src": src,
                "dst": dst,
                "seconds": seconds,
                "has_stairs": has_stairs,
            }
        )

    # 反向通行时间窗可选；未配置时行为与旧版本完全一致
    reverse_windows: list[dict] = []
    if "reverse_windows" in data and data["reverse_windows"] is not None:
        reverse_windows = _normalize_reverse_windows(data["reverse_windows"], edge_ids)

    # 允许空图；允许自环（正秒数下不会被最短路采用）
    return {"nodes": nodes, "edges": edges, "reverse_windows": reverse_windows}


def normalize_closures(payload: Any, known_edges: set[str]) -> list[dict]:
    """校验封闭记录负载。

    期望格式::

        {"closures": [
          {"edge_id": "E1", "start": "2026-10-05T08:00:00Z",
           "end": "2026-10-05T10:00:00Z"}, ...]}

    时间区间为半开 ``[start, end)``，必须满足 ``start < end``。
    """
    data = _require_object(payload, "封闭记录")
    raw_closures = data.get("closures")
    if not isinstance(raw_closures, list):
        raise ValidationError("closures 必须是数组")

    closures: list[dict] = []
    for i, raw in enumerate(raw_closures):
        if not isinstance(raw, dict):
            raise ValidationError(f"closures[{i}] 必须是对象")
        where = f"closures[{i}]"
        edge_id = _clean_identifier(raw.get("edge_id"), f"{where}.edge_id")
        if edge_id not in known_edges:
            raise ValidationError(f"{where} 引用了不存在的路段: {edge_id!r}")
        if "start" not in raw:
            raise ValidationError(f"{where} 缺少 start")
        if "end" not in raw:
            raise ValidationError(f"{where} 缺少 end")
        try:
            start = parse_time(raw["start"])
            end = parse_time(raw["end"])
        except TimeError as exc:
            raise ValidationError(f"{where}: {exc}") from None
        if start >= end:
            raise ValidationError(
                f"{where} 无效时间区间: start({raw['start']}) 必须早于 end({raw['end']})"
            )
        closures.append({"edge_id": edge_id, "start": start, "end": end})
    return closures


def _normalize_stop(raw: Any, index: int, node_set: set[str]) -> dict:
    """校验一个按顺序停靠点。

    期望格式::

        {"node": "PLAZA",
         "earliest_start": "2026-10-05T10:00:00Z",
         "latest_start":   "2026-10-05T10:30:00Z",
         "stay_seconds": 600}

    时间窗为闭区间 ``[earliest_start, latest_start]``：开始停留不得早于
    ``earliest_start``、不得晚于 ``latest_start``；``stay_seconds`` 为
    非负整数秒。
    """
    where = f"stops[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} 必须是对象")
    for field in ("node", "earliest_start", "latest_start", "stay_seconds"):
        if field not in raw:
            raise ValidationError(f"{where} 缺少必填字段: {field}")

    node = _clean_identifier(raw.get("node"), f"{where}.node")
    if node not in node_set:
        raise ValidationError(f"{where} 引用了未知节点: {node!r}")

    try:
        earliest = parse_time(raw["earliest_start"])
        latest = parse_time(raw["latest_start"])
    except TimeError as exc:
        raise ValidationError(f"{where}: {exc}") from None
    if earliest > latest:
        raise ValidationError(
            f"{where} 无效时间窗: earliest_start({raw['earliest_start']}) "
            f"必须不晚于 latest_start({raw['latest_start']})"
        )

    stay = raw["stay_seconds"]
    # 严格类型：拒绝 bool、float、数字字符串等隐式转换
    if isinstance(stay, bool) or not isinstance(stay, int):
        raise ValidationError(f"{where}.stay_seconds 必须是非负整数")
    if stay < 0:
        raise ValidationError(f"{where}.stay_seconds 必须是非负整数，收到 {stay}")

    return {"node": node, "earliest": earliest, "latest": latest, "stay": stay}


def _normalize_meeting_visitor(raw: Any, index: int, node_set: set[str]) -> dict:
    """校验一名汇合查询访客。

    期望格式::

        {"id": "alice",              # 可选，缺省按序号生成
         "origin": "GATE",
         "departure_time": "2026-10-05T09:00:00Z",   # 最早出发时刻
         "avoid_stairs": false}
    """
    where = f"visitors[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} 必须是对象")
    for field in ("origin", "departure_time"):
        if field not in raw:
            raise ValidationError(f"{where} 缺少必填字段: {field}")

    origin = _clean_identifier(raw.get("origin"), f"{where}.origin")
    if origin not in node_set:
        raise ValidationError(f"{where}.origin 引用了未知节点: {origin!r}")

    try:
        departure = parse_time(raw["departure_time"])
    except TimeError as exc:
        raise ValidationError(f"{where}.departure_time: {exc}") from None

    avoid_stairs = raw.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise ValidationError(f"{where}.avoid_stairs 必须是布尔值 true/false")

    visitor = {
        "id": index + 1,  # 默认按 1 起始序号
        "origin": origin,
        "departure": departure,
        "avoid_stairs": avoid_stairs,
    }
    if "id" in raw:
        visitor["id"] = _clean_identifier(raw.get("id"), f"{where}.id")
    return visitor


def normalize_meeting(payload: Any, node_set: set[str]) -> dict:
    """校验一次多人汇合点查询负载。

    期望格式::

        {"visitors": [{"origin": ..., "departure_time": ...,
                       "avoid_stairs": ...}, ...],
         "candidates": ["PLAZA", "TOWER"],
         "latest_meeting_time": "2026-10-05T12:00:00Z"}
    """
    data = _require_object(payload, "汇合查询")
    for field in ("visitors", "candidates", "latest_meeting_time"):
        if field not in data:
            raise ValidationError(f"缺少必填字段: {field}")

    raw_visitors = data["visitors"]
    if not isinstance(raw_visitors, list):
        raise ValidationError("visitors 必须是数组")
    if len(raw_visitors) < 2:
        raise ValidationError("visitors 至少需要包含两名访客")
    visitors = [
        _normalize_meeting_visitor(raw, i, node_set)
        for i, raw in enumerate(raw_visitors)
    ]
    seen_ids: set = set()
    for v in visitors:
        if v["id"] in seen_ids:
            raise ValidationError(f"访客标识重复: {v['id']!r}")
        seen_ids.add(v["id"])

    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list):
        raise ValidationError("candidates 必须是数组")
    if not raw_candidates:
        raise ValidationError("candidates 不能为空")
    candidates: list[str] = []
    candidate_set: set[str] = set()
    for i, raw in enumerate(raw_candidates):
        node = _clean_identifier(raw, f"candidates[{i}]")
        if node not in node_set:
            raise ValidationError(f"candidates[{i}] 引用了未知节点: {node!r}")
        if node in candidate_set:
            raise ValidationError(f"candidates 存在重复节点: {node!r}")
        candidate_set.add(node)
        candidates.append(node)

    try:
        latest = parse_time(data["latest_meeting_time"])
    except TimeError as exc:
        raise ValidationError(f"latest_meeting_time: {exc}") from None

    return {
        "visitors": visitors,
        "candidates": candidates,
        "latest_meeting_time": latest,
    }


def normalize_query(payload: Any, node_set: set[str]) -> dict:
    """校验一次路线查询负载。"""
    data = _require_object(payload, "查询")
    for field in ("origin", "destination", "departure_time"):
        if field not in data:
            raise ValidationError(f"缺少必填字段: {field}")

    origin = _clean_identifier(data.get("origin"), "origin")
    destination = _clean_identifier(data.get("destination"), "destination")
    for name, node in (("origin", origin), ("destination", destination)):
        if node not in node_set:
            raise ValidationError(f"{name} 引用了未知节点: {node!r}")

    try:
        departure = parse_time(data["departure_time"])
    except TimeError as exc:
        raise ValidationError(f"departure_time: {exc}") from None

    avoid_stairs = data.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise ValidationError("avoid_stairs 必须是布尔值 true/false")

    raw_stops = data.get("stops", [])
    if not isinstance(raw_stops, list):
        raise ValidationError("stops 必须是数组")
    stops = [_normalize_stop(raw, i, node_set) for i, raw in enumerate(raw_stops)]

    return {
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "avoid_stairs": avoid_stairs,
        "stops": stops,
    }
