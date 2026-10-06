"""内置示例数据，首次启动自动导入。

园区示意：

    GATE ──E1──> PLAZA ──E2──> HILL(台阶)
                  │  ╲           │
                  E3  ╲E6(台阶)  E4(台阶,施工封闭)
                  ▼    ╲         ▼
                 LAKE ──E5──── TOWER

10:00–11:00 之间 E4 封闭施工。
下午活动时段 13:00–16:00 步道 E5（LAKE→TOWER 单向）反向通行：
窗内仅允许 TOWER→LAKE，窗外恢复原方向 LAKE→TOWER。
"""

SAMPLE_GRAPH = {
    "nodes": ["GATE", "PLAZA", "HILL", "LAKE", "TOWER"],
    "edges": [
        {"id": "E1", "from": "GATE",  "to": "PLAZA", "travel_seconds": 180, "has_stairs": False},
        {"id": "E2", "from": "PLAZA", "to": "HILL",  "travel_seconds": 240, "has_stairs": True},
        {"id": "E3", "from": "PLAZA", "to": "LAKE",  "travel_seconds": 300, "has_stairs": False},
        {"id": "E4", "from": "HILL",  "to": "TOWER", "travel_seconds": 120, "has_stairs": True},
        {"id": "E5", "from": "LAKE",  "to": "TOWER", "travel_seconds": 240, "has_stairs": False},
        {"id": "E6", "from": "PLAZA", "to": "TOWER", "travel_seconds": 600, "has_stairs": True},
    ],
    # 活动时段反向通行窗（半开 [start, end)）：窗内 E5 仅允许 TOWER→LAKE
    "reverse_windows": {
        "E5": [
            {"start": "2026-10-05T13:00:00Z", "end": "2026-10-05T16:00:00Z"}
        ]
    },
}

SAMPLE_CLOSURES = {
    "closures": [
        {
            "edge_id": "E4",
            "start": "2026-10-05T10:00:00Z",
            "end": "2026-10-05T11:00:00Z",
        }
    ]
}

