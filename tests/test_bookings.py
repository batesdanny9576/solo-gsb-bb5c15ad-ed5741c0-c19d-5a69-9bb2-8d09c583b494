"""校准工位预约（GET /api/calibration-slots、POST/GET /api/calibration-bookings）测试。"""
from fastapi.testclient import TestClient

SLOTS_URL = "/api/calibration-slots"
BOOKINGS_URL = "/api/calibration-bookings"

# W 起点证书 CW 有效期 [2026-01-01, 2027-01-01)；预约截止 2027-01-01T00:00:00Z
AT = "2026-06-15T00:00:00Z"
DEADLINE = "2027-01-01T00:00:00Z"


def _inst(code, name=None, root=False):
    return {"code": code, "name": name or code, "is_national_standard": root}


def _cert(cid, inst, issued="2026-01-02T00:00:00",
          vf="2026-01-01T00:00:00", vu="2027-01-01T00:00:00",
          u=0.01, unit="mm", digest=None):
    data = {
        "id": cid, "instrument_code": inst, "cert_number": cid,
        "issued_at": issued, "valid_from": vf, "valid_until": vu,
        "uncertainty": u, "unit": unit,
    }
    if digest is not None:
        data["signature_digest"] = digest
    return data


def _unique_chain(client, instrument="W", leaf="CW", vu="2027-01-01T00:00:00"):
    """G（国家基准）← R ← W：唯一且校验通过的长度溯源链。"""
    r = client.post("/api/submissions", json={
        "instruments": [_inst("G", root=True), _inst("R"), _inst(instrument)],
        "certificates": [
            _cert("CG", "G", issued="2024-01-02T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0001),
            _cert("CR", "R", issued="2025-06-05T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2028-06-01T00:00:00",
                  u=0.001),
            _cert(leaf, instrument, vu=vu, u=0.01),
        ],
        "edges": [
            {"child_cert_id": leaf, "parent_cert_id": "CR"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
        ],
    })
    assert r.status_code == 201, r.text


def _slots(client, instrument="W", start="2026-06-15T00:00:00Z",
           end="2026-06-16T00:00:00Z", minutes=60):
    return client.get(
        SLOTS_URL,
        params={
            "instrument": instrument, "start": start, "end": end,
            "duration_minutes": minutes,
        },
    )


# ---------------------------------------------------------------- 时段查询

def test_slots_empty_window_returns_window_start(client: TestClient):
    _unique_chain(client)
    r = _slots(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["available"] is True
    assert d["station_code"] == "CAL-STATION-01"
    assert d["interval_semantics"] == "half_open [start_at, end_at)"
    assert d["slot"] == {
        "start_at": "2026-06-15T00:00:00Z",
        "end_at": "2026-06-15T01:00:00Z",
        "duration_minutes": 60,
    }
    assert d["busy_intervals"] == []
    assert d["deadline"]["start_certificate_id"] == "CW"
    assert d["deadline"]["certificate_valid_until"] == DEADLINE
    assert d["deadline"]["chain_certificate_ids"] == ["CW", "CR", "CG"]
    assert d["effective_window_end"] == "2026-06-16T00:00:00Z"
    assert d["traceability"]["available"] is True
    assert d["traceability"]["resolution"] == "unique"
    assert d["reason_code"] is None


def test_slots_skips_occupied_interval_and_returns_earliest_gap(client: TestClient):
    _unique_chain(client)
    # 占用 [08:00, 09:30)：最早可用应跳到 09:30（半开相接，不冲突）
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 90,
    })
    assert r.status_code == 201, r.text

    r = _slots(client, start="2026-06-15T08:00:00Z",
               end="2026-06-15T12:00:00Z", minutes=60)
    d = r.json()
    assert d["available"] is True
    assert d["slot"]["start_at"] == "2026-06-15T09:30:00Z"
    assert d["slot"]["end_at"] == "2026-06-15T10:30:00Z"
    assert len(d["busy_intervals"]) == 1
    assert d["busy_intervals"][0]["start_at"] == "2026-06-15T08:00:00Z"
    assert d["busy_intervals"][0]["end_at"] == "2026-06-15T09:30:00Z"


def test_slots_back_to_back_at_boundary_is_not_conflict(client: TestClient):
    _unique_chain(client)
    # 既有预约恰在查询窗起点结束：[07:00, 08:00)，窗口从 08:00 开始
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T07:00:00Z",
        "duration_minutes": 60,
    })
    assert r.status_code == 201, r.text
    r = _slots(client, start="2026-06-15T08:00:00Z",
               end="2026-06-15T09:00:00Z", minutes=60)
    d = r.json()
    assert d["available"] is True
    assert d["slot"]["start_at"] == "2026-06-15T08:00:00Z"
    assert d["busy_intervals"] == []  # [07,08) 与 [08,09) 不相交


def test_slots_other_instrument_booking_still_occupies_single_station(
    client: TestClient,
):
    """单工位：其他仪器的预约同样占用工位。"""
    _unique_chain(client)
    # 为另一台仪器 X 也建唯一链，再预约同一时段
    client.post("/api/submissions", json={
        "instruments": [_inst("X")],
        "certificates": [_cert("CX", "X", u=0.02)],
        "edges": [{"child_cert_id": "CX", "parent_cert_id": "CR"}],
    })
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "X",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 60,
    })
    assert r.status_code == 201, r.text

    r = _slots(client, start="2026-06-15T08:00:00Z",
               end="2026-06-15T10:00:00Z", minutes=60)
    d = r.json()
    assert d["available"] is True
    assert d["slot"]["start_at"] == "2026-06-15T09:00:00Z"
    assert d["busy_intervals"][0]["instrument_code"] == "X"


def test_slots_no_fitting_gap_returns_unavailable(client: TestClient):
    _unique_chain(client)
    # 整个窗口被占满：[08:00, 12:00)
    client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 240,
    })
    r = _slots(client, start="2026-06-15T08:00:00Z",
               end="2026-06-15T12:00:00Z", minutes=60)
    d = r.json()
    assert d["available"] is False
    assert d["slot"] is None
    assert d["reason_code"] == "NO_AVAILABLE_SLOT_IN_WINDOW"
    assert d["traceability"]["available"] is True


def test_slots_window_clamped_by_certificate_deadline(client: TestClient):
    """窗口终点晚于起点证书到期：搜索上限截到到期时刻。"""
    _unique_chain(client)
    r = _slots(client, start="2026-12-31T23:00:00Z",
               end="2027-01-02T00:00:00Z", minutes=120)
    d = r.json()
    # 截止 2027-01-01T00:00:00Z，距窗口起点仅 1 小时，放不下 120 分钟
    assert d["available"] is False
    assert d["reason_code"] == "NO_AVAILABLE_SLOT_IN_WINDOW"
    assert d["effective_window_end"] == DEADLINE
    # 60 分钟恰好放得下，且结束时刻等于到期时刻（<= 允许）
    r = _slots(client, start="2026-12-31T23:00:00Z",
               end="2027-01-02T00:00:00Z", minutes=60)
    d = r.json()
    assert d["available"] is True
    assert d["slot"]["end_at"] == DEADLINE


def test_slots_window_start_after_deadline_unavailable(client: TestClient):
    _unique_chain(client)
    r = _slots(client, start="2027-01-01T00:00:00Z",
               end="2027-01-02T00:00:00Z", minutes=60)
    d = r.json()
    assert d["available"] is False
    assert d["reason_code"] == "NO_AVAILABLE_SLOT_IN_WINDOW"
    assert d["deadline"]["certificate_valid_until"] == DEADLINE


def test_slots_broken_chain_explains_reason(client: TestClient):
    """起点证书有效但向上溯源为死路：链不可用，返回原因而非时段。"""
    client.post("/api/submissions", json={
        "instruments": [_inst("W")],
        "certificates": [_cert("CW", "W")],
        "edges": [],
    })
    r = _slots(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["available"] is False
    assert d["slot"] is None
    assert d["deadline"] is None
    assert d["effective_window_end"] is None
    assert d["busy_intervals"] == []
    t = d["traceability"]
    assert t["available"] is False
    assert t["resolution"] == "broken"
    assert t["reason_code"] == "CHAIN_BROKEN"
    assert t["reason"]  # 链不可用必须给出中文原因
    assert d["reason_code"] == "TRACE_CHAIN_UNAVAILABLE"
    assert t["starts"] == ["CW"]


def test_slots_no_active_certificate_explains_reason(client: TestClient):
    client.post("/api/submissions", json={
        "instruments": [_inst("G", root=True), _inst("R"), _inst("W")],
        "certificates": [
            _cert("CG", "G", vf="2024-01-01T00:00:00",
                  vu="2030-01-01T00:00:00", u=0.0001,
                  issued="2024-01-02T00:00:00"),
            _cert("CR", "R", vf="2025-06-01T00:00:00",
                  vu="2028-06-01T00:00:00", u=0.001,
                  issued="2025-06-05T00:00:00"),
        ],
        "edges": [{"child_cert_id": "CR", "parent_cert_id": "CG"}],
    })
    r = _slots(client)
    d = r.json()
    assert d["available"] is False
    assert d["traceability"]["reason_code"] == "NO_ACTIVE_CERTIFICATE"
    assert d["reason_code"] == "TRACE_CHAIN_UNAVAILABLE"


def test_slots_instrument_not_found_404(client: TestClient):
    r = _slots(client, instrument="NOPE")
    assert r.status_code == 404
    assert "NOPE" in r.json()["detail"]


def test_slots_bad_window_422(client: TestClient):
    _unique_chain(client)
    r = _slots(client, start="2026-06-16T00:00:00Z",
               end="2026-06-15T00:00:00Z")
    assert r.status_code == 422
    r = _slots(client, start="2026-06-15T08:00:00Z",
               end="2026-06-15T08:00:00Z")
    assert r.status_code == 422


def test_slots_duration_must_be_positive(client: TestClient):
    _unique_chain(client)
    r = client.get(SLOTS_URL, params={
        "instrument": "W", "start": "2026-06-15T00:00:00Z",
        "end": "2026-06-16T00:00:00Z", "duration_minutes": 0,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------- 预约确认

def test_booking_success_persists_with_certificate_snapshot(client: TestClient):
    _unique_chain(client)
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 90,
        "note": "例行校准",
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["id"].startswith("BK-")
    assert d["station_code"] == "CAL-STATION-01"
    assert d["instrument_code"] == "W"
    assert d["start_at"] == "2026-06-15T08:00:00Z"
    assert d["end_at"] == "2026-06-15T09:30:00Z"
    assert d["duration_minutes"] == 90
    assert d["start_certificate_id"] == "CW"
    assert d["certificate_valid_until"] == DEADLINE

    # 持久化：可按仪器查询到
    r = client.get(BOOKINGS_URL, params={"instrument": "W"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1 and rows[0]["id"] == d["id"]


def test_booking_ending_exactly_at_deadline_allowed(client: TestClient):
    _unique_chain(client)
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-12-31T23:00:00Z",
        "duration_minutes": 60,
    })
    assert r.status_code == 201, r.text
    assert r.json()["end_at"] == DEADLINE


def test_booking_after_deadline_returns_409(client: TestClient):
    _unique_chain(client)
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-12-31T23:30:00Z",
        "duration_minutes": 60,  # end 2027-01-01T00:30 > deadline
    })
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["code"] == "TRACE_CHAIN_UNAVAILABLE"
    assert body["detail"]["certificate_valid_until"] == DEADLINE
    # 被拒绝的预约不得落库
    assert client.get(BOOKINGS_URL).json() == []


def test_booking_start_without_chain_returns_409(client: TestClient):
    # W 仅有起点证书、无向上溯源边 -> 链断裂
    client.post("/api/submissions", json={
        "instruments": [_inst("W")],
        "certificates": [_cert("CW", "W")],
        "edges": [],
    })
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 60,
    })
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["code"] == "TRACE_CHAIN_UNAVAILABLE"
    assert body["detail"]["resolution"] == "broken"
    assert client.get(BOOKINGS_URL).json() == []


def test_booking_occupied_returns_409_with_conflicting_bookings(client: TestClient):
    _unique_chain(client)
    payload = {
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 60,
    }
    assert client.post(BOOKINGS_URL, json=payload).status_code == 201

    # 重叠预约（08:30-09:30）-> 409
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:30:00Z",
        "duration_minutes": 60,
    })
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["code"] == "SLOT_OCCUPIED"
    clashes = body["detail"]["conflicting_bookings"]
    assert len(clashes) == 1
    assert clashes[0]["start_at"] == "2026-06-15T08:00:00Z"
    # 半开相接（09:00 起）不冲突，应成功
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T09:00:00Z",
        "duration_minutes": 30,
    })
    assert r.status_code == 201, r.text


def test_booking_other_instrument_same_slot_409_single_station(client: TestClient):
    _unique_chain(client)
    client.post("/api/submissions", json={
        "instruments": [_inst("X")],
        "certificates": [_cert("CX", "X", u=0.02)],
        "edges": [{"child_cert_id": "CX", "parent_cert_id": "CR"}],
    })
    assert client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 60,
    }).status_code == 201
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "X",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 60,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SLOT_OCCUPIED"


def test_booking_instrument_not_found_404(client: TestClient):
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "NOPE",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 60,
    })
    assert r.status_code == 404


def test_booking_nonpositive_duration_422(client: TestClient):
    _unique_chain(client)
    r = client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": -5,
    })
    assert r.status_code == 422


def test_list_bookings_filter_and_ordering(client: TestClient):
    _unique_chain(client)
    client.post("/api/submissions", json={
        "instruments": [_inst("X")],
        "certificates": [_cert("CX", "X", u=0.02)],
        "edges": [{"child_cert_id": "CX", "parent_cert_id": "CR"}],
    })
    client.post(BOOKINGS_URL, json={
        "instrument_code": "X", "start_at": "2026-06-16T08:00:00Z",
        "duration_minutes": 30})
    client.post(BOOKINGS_URL, json={
        "instrument_code": "W", "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 30})

    rows = client.get(BOOKINGS_URL).json()
    assert [r["start_at"] for r in rows] == [
        "2026-06-15T08:00:00Z", "2026-06-16T08:00:00Z"]

    w_rows = client.get(BOOKINGS_URL, params={"instrument": "W"}).json()
    assert [r["instrument_code"] for r in w_rows] == ["W"]
    x_rows = client.get(BOOKINGS_URL, params={"instrument": "X"}).json()
    assert len(x_rows) == 1 and x_rows[0]["instrument_code"] == "X"


def test_booking_then_slots_reflects_new_occupancy(client: TestClient):
    """端到端：先查空档 -> 预约 -> 再查，最早时段向后跳过占用。"""
    _unique_chain(client)
    d = _slots(client, start="2026-06-15T08:00:00Z",
               end="2026-06-15T12:00:00Z", minutes=60).json()
    assert d["slot"]["start_at"] == "2026-06-15T08:00:00Z"

    client.post(BOOKINGS_URL, json={
        "instrument_code": "W",
        "start_at": "2026-06-15T08:00:00Z",
        "duration_minutes": 60,
    })
    d = _slots(client, start="2026-06-15T08:00:00Z",
               end="2026-06-15T12:00:00Z", minutes=60).json()
    assert d["available"] is True
    assert d["slot"]["start_at"] == "2026-06-15T09:00:00Z"
    assert len(d["busy_intervals"]) == 1
