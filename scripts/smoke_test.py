#!/usr/bin/env python3
"""针对运行中服务的端到端冒烟测试（仅用标准库）。

用法::

    python3 scripts/smoke_test.py [BASE_URL]

默认 http://127.0.0.1:8080。退出码非 0 表示冒烟失败。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
failures = []


def call(method: str, path: str, body=None, expect_status=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        status = e.code
        payload = json.loads(e.read().decode())
    tag = f"{method} {path}"
    if status != expect_status:
        failures.append(f"{tag}: 期望 HTTP {expect_status}，实际 {status}，body={payload}")
    return status, payload


def main() -> int:
    status, health = call("GET", "/health")
    assert health.get("has_data"), health

    # 1. 正常查询（示例数据：E4 在 10:00-11:00 封闭）
    status, route = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T09:55:00Z",
        "avoid_stairs": False,
    })
    assert status == 200, route
    assert "stops" not in route, route  # 无停靠点时保持原有返回结构
    print("普通查询:", route["edge_sequence"], "->", route["arrival_time"])

    # 2. 避台阶：只能走 E1 E3 E5
    status, route_flat = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:00:00Z",
        "avoid_stairs": True,
    })
    assert route_flat["edge_sequence"] == ["E1", "E3", "E5"], route_flat
    print("避台阶查询:", route_flat["edge_sequence"])

    # 3. 未知节点 → 400
    status, err = call("POST", "/api/route", {
        "origin": "GHOST", "destination": "TOWER",
        "departure_time": "2026-10-05T08:00:00Z",
    }, expect_status=400)
    assert err["error"] == "invalid_payload", err

    # 4. 非法导入（引用未知节点）→ 400 且版本不变
    before = call("GET", "/version")[1]
    status, bad = call("POST", "/admin/graph", {
        "nodes": ["X"],
        "edges": [{"id": "X1", "from": "X", "to": "Y",
                   "travel_seconds": 10, "has_stairs": False}],
    }, expect_status=400)
    after = call("GET", "/version")[1]
    assert before["data_version"] == after["data_version"] == bad["active_version"], (before, after, bad)
    print(f"拒绝非法导入，保留版本 v{after['data_version']}")

    # 5. 非法秒数 → 400
    status, bad2 = call("POST", "/admin/graph", {
        "nodes": ["X", "Y"],
        "edges": [{"id": "X1", "from": "X", "to": "Y",
                   "travel_seconds": 0, "has_stairs": False}],
    }, expect_status=400)
    assert "正数" in bad2["detail"], bad2

    # 6. 无效时间区间 → 400
    status, bad3 = call("POST", "/admin/closures", {
        "closures": [{"edge_id": "E1",
                      "start": "2026-10-05T10:00:00Z",
                      "end": "2026-10-05T09:00:00Z"}],
    }, expect_status=400)

    # 7. 按顺序停靠：LAKE 窗口 [09:00,09:10] 停留 300s
    #    08:50 出发 → 08:58 抵 LAKE，等窗至 09:00，09:05 离开，09:09 到 TOWER
    status, route_stop = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "LAKE",
                   "earliest_start": "2026-10-05T09:00:00Z",
                   "latest_start": "2026-10-05T09:10:00Z",
                   "stay_seconds": 300}],
    })
    assert route_stop["edge_sequence"] == ["E1", "E3", "E5"], route_stop
    assert route_stop["arrival_time"] == "2026-10-05T09:09:00Z", route_stop
    (visit,) = route_stop["stops"]
    assert (visit["arrival_time"], visit["start_time"], visit["end_time"]) == (
        "2026-10-05T08:58:00Z", "2026-10-05T09:00:00Z", "2026-10-05T09:05:00Z"), visit
    assert route_stop["total_stay_seconds"] == 300, route_stop
    print("停靠查询:", visit["node"], visit["start_time"], "->", route_stop["arrival_time"])

    # 8. 停靠窗口不可行（最晚 08:55 早于抵达 08:58）→ 404 no_route
    status, nr = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "LAKE",
                   "earliest_start": "2026-10-05T08:00:00Z",
                   "latest_start": "2026-10-05T08:55:00Z",
                   "stay_seconds": 60}],
    }, expect_status=404)
    assert nr["error"] == "no_route", nr

    # 9. 停留秒数非法 / 停靠地点未知 → 400 invalid_payload
    status, bad4 = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "LAKE",
                   "earliest_start": "2026-10-05T09:00:00Z",
                   "latest_start": "2026-10-05T09:10:00Z",
                   "stay_seconds": -1}],
    }, expect_status=400)
    assert bad4["error"] == "invalid_payload", bad4
    status, bad5 = call("POST", "/api/route", {
        "origin": "GATE", "destination": "TOWER",
        "departure_time": "2026-10-05T08:50:00Z",
        "stops": [{"node": "GHOST",
                   "earliest_start": "2026-10-05T09:00:00Z",
                   "latest_start": "2026-10-05T09:10:00Z",
                   "stay_seconds": 60}],
    }, expect_status=400)
    assert bad5["error"] == "invalid_payload", bad5

    # 10. 多人汇合：alice 09:55 从 GATE、bob 09:50 从 PLAZA；
    #     LAKE：alice 10:03 到、bob 09:55 到 → 汇合 10:03
    #     TOWER：alice 10:07 到、bob 09:59 到 → 汇合 10:07 → 选 LAKE
    status, meeting = call("POST", "/api/meeting", {
        "visitors": [
            {"id": "alice", "origin": "GATE",
             "departure_time": "2026-10-05T09:55:00Z", "avoid_stairs": False},
            {"id": "bob", "origin": "PLAZA",
             "departure_time": "2026-10-05T09:50:00Z", "avoid_stairs": False},
        ],
        "candidates": ["TOWER", "LAKE"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    })
    assert meeting["meeting_node"] == "LAKE", meeting
    assert meeting["meeting_time"] == "2026-10-05T10:03:00Z", meeting
    alice, bob = meeting["visitors"]
    assert alice["edge_sequence"] == ["E1", "E3"], alice
    assert alice["wait_after_arrival_seconds"] == 0
    assert bob["edge_sequence"] == ["E3"]
    assert bob["wait_after_arrival_seconds"] == 480, bob
    print("汇合查询:", meeting["meeting_node"], meeting["meeting_time"])

    # 11. 最晚汇合时刻早于全员可达时刻 → 404 no_meeting_candidate
    status, nm = call("POST", "/api/meeting", {
        "visitors": [
            {"origin": "GATE", "departure_time": "2026-10-05T09:55:00Z"},
            {"origin": "PLAZA", "departure_time": "2026-10-05T09:50:00Z"},
        ],
        "candidates": ["LAKE"],
        "latest_meeting_time": "2026-10-05T10:00:00Z",
    }, expect_status=404)
    assert nm["error"] == "no_meeting_candidate", nm

    # 12. 汇合参数非法：访客不足两人 / 未知候选节点 / 候选为空 → 400
    status, mb1 = call("POST", "/api/meeting", {
        "visitors": [{"origin": "GATE", "departure_time": "2026-10-05T09:00:00Z"}],
        "candidates": ["LAKE"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }, expect_status=400)
    assert mb1["error"] == "invalid_payload", mb1
    status, mb2 = call("POST", "/api/meeting", {
        "visitors": [
            {"origin": "GATE", "departure_time": "2026-10-05T09:00:00Z"},
            {"origin": "PLAZA", "departure_time": "2026-10-05T09:00:00Z"},
        ],
        "candidates": ["GHOST"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }, expect_status=400)
    assert mb2["error"] == "invalid_payload", mb2
    status, mb3 = call("POST", "/api/meeting", {
        "visitors": [
            {"origin": "GATE", "departure_time": "2026-10-05T09:00:00Z"},
            {"origin": "PLAZA", "departure_time": "2026-10-05T09:00:00Z"},
        ],
        "candidates": [],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }, expect_status=400)
    assert mb3["error"] == "invalid_payload", mb3

    if failures:
        print("\n".join("FAIL: " + f for f in failures), file=sys.stderr)
        return 1
    print("全部冒烟检查通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
