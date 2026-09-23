"""HTTP 契约测试（对应 CDE 规范 §4.2 / §4.3 / §4.5 的硬性门槛）。

覆盖：鉴权 401/403、404 与 422 的可区分性、405、400、409 幂等冲突、
响应体字段名一致性、Content-Type、幂等重跑一致性。
"""

from __future__ import annotations

import json

import pytest

from app.handler import handler
from conftest import TOKEN_RO, TOKEN_RW, call, make_event

VALID_PLAN_BODY = {
    "destination": "大阪",
    "start_date": "2026-11-20",
    "days": 2,
    "party": {"adults": 2, "children": 1, "child_ages": [5]},
    "preferences": {"pace": "standard"},
    "options": {"check_availability": True, "include_narrative": True},
}


@pytest.fixture(autouse=True)
def _clock(frozen_clock: None) -> None:
    """全组用例使用冻结时钟。"""


# ---------------------------------------------------------------- 鉴权


def test_missing_token_is_rejected_with_401() -> None:
    """未带令牌必须被拒（规范 §4.2 判分必测项），不得返回 200。"""
    status, body = call("GET", "/v1/health", token=None)
    assert status == 401
    assert body["error_code"] == "UNAUTHORIZED"
    assert "message" in body


def test_invalid_token_is_rejected() -> None:
    status, body = call("GET", "/v1/health", token="MOCK_WRONG_TOKEN")
    assert status == 401
    assert body["error_code"] == "UNAUTHORIZED"


def test_malformed_authorization_header_is_rejected() -> None:
    status, _ = call("GET", "/v1/health", token=None, headers={"authorization": "NotBearer abc"})
    assert status == 401


def test_valid_token_succeeds() -> None:
    status, body = call("GET", "/v1/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["auth"]["current_subject"] == "MOCK_test_rw"


def test_insufficient_scope_is_403_not_401() -> None:
    """只读令牌写操作 → 403；与「令牌无效 → 401」落在不同状态码上（规范 §3.1-1）。"""
    status, body = call("POST", "/v1/plans", body=VALID_PLAN_BODY, token=TOKEN_RO)
    assert status == 403
    assert body["error_code"] == "FORBIDDEN"


def test_readonly_token_can_read() -> None:
    status, _ = call("GET", "/v1/catalog", token=TOKEN_RO)
    assert status == 200


def test_authorizer_context_is_honoured() -> None:
    """Authorizer 透传的认证结论应被业务层采纳。"""
    status, body = call(
        "GET",
        "/v1/health",
        token=None,
        authorizer_context={"auth_status": "OK", "subject": "MOCK_via_authorizer", "scopes": "read,write"},
    )
    assert status == 200
    assert body["auth"]["current_subject"] == "MOCK_via_authorizer"


def test_authorizer_invalid_token_context_yields_401() -> None:
    """AUTH_MODE=handler_decide 路径：Authorizer 放行后由业务层给 401。"""
    status, body = call(
        "GET",
        "/v1/health",
        token=None,
        authorizer_context={"auth_status": "INVALID_TOKEN", "subject": "anonymous", "scopes": ""},
    )
    assert status == 401
    assert body["error_code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------- 404 / 405 / 400


def test_unknown_path_is_404_with_our_body_shape() -> None:
    """路径不存在 → 404，且响应体由本项目产出（不是网关默认体）。"""
    status, body = call("GET", "/v1/definitely-not-a-route")
    assert status == 404
    assert body["error_code"] == "NOT_FOUND"
    assert body["request_id"]


def test_missing_resource_is_404() -> None:
    status, body = call("GET", "/v1/plans/MOCK-PLAN-DOESNOTEXIST")
    assert status == 404
    assert body["error_code"] == "NOT_FOUND"


def test_wrong_method_is_405_not_404() -> None:
    status, body = call("GET", "/v1/bookings")
    assert status == 405
    assert body["error_code"] == "METHOD_NOT_ALLOWED"


def test_malformed_json_is_400() -> None:
    event = make_event("POST", "/v1/plans")
    event["body"] = "{not valid json"
    response = handler(event)
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error_code"] == "BAD_REQUEST"


def test_non_object_json_body_is_400() -> None:
    event = make_event("POST", "/v1/plans")
    event["body"] = "[1, 2, 3]"
    response = handler(event)
    assert response["statusCode"] == 400


# ---------------------------------------------------------------- 422（Lambda 内校验）


def test_invalid_field_type_is_422_not_400() -> None:
    """API Gateway 模型校验只会给 400，422 必须由 Lambda 自行产出（规范 §3.1-2）。"""
    status, body = call("POST", "/v1/plans", body={**VALID_PLAN_BODY, "days": "两天"})
    assert status == 422
    assert body["error_code"] == "VALIDATION_FAILED"
    assert any(e["field"] == "days" for e in body["errors"])


def test_invalid_date_is_422() -> None:
    status, body = call("POST", "/v1/plans", body={**VALID_PLAN_BODY, "start_date": "2026-13-45"})
    assert status == 422
    assert any(e["field"] == "start_date" for e in body["errors"])


def test_past_date_is_422() -> None:
    status, body = call("POST", "/v1/plans", body={**VALID_PLAN_BODY, "start_date": "2020-01-01"})
    assert status == 422
    assert any(e["field"] == "start_date" for e in body["errors"])


def test_unsupported_destination_is_422_with_expected_list() -> None:
    status, body = call("POST", "/v1/plans", body={**VALID_PLAN_BODY, "destination": "冰岛"})
    assert status == 422
    error = next(e for e in body["errors"] if e["field"] == "destination")
    assert "大阪" in error["expected"]


def test_child_age_count_mismatch_is_422() -> None:
    body_in = {**VALID_PLAN_BODY, "party": {"adults": 2, "children": 2, "child_ages": [5]}}
    status, body = call("POST", "/v1/plans", body=body_in)
    assert status == 422
    assert any(e["field"] == "party.child_ages" for e in body["errors"])


def test_unknown_field_is_422() -> None:
    status, body = call("POST", "/v1/plans", body={**VALID_PLAN_BODY, "destinaton": "大阪"})
    assert status == 422
    assert any(e["field"] == "destinaton" for e in body["errors"])


def test_all_field_errors_reported_at_once() -> None:
    """一次返回全部字段错误，避免调用方多轮试错。"""
    status, body = call(
        "POST",
        "/v1/plans",
        body={"destination": "冰岛", "start_date": "bad", "days": 0, "party": {"adults": -1}},
    )
    assert status == 422
    assert len(body["errors"]) >= 4


def test_404_and_422_are_distinguishable() -> None:
    """规范 §4.3 明确要求这两者可区分。"""
    not_found, _ = call("GET", "/v1/products/MOCK_PROD_NOPE")
    invalid, _ = call("GET", "/v1/content", query={"limit": "9999"})
    assert not_found == 404
    assert invalid == 422


# ---------------------------------------------------------------- 响应格式


def test_content_type_is_json_on_success_and_error() -> None:
    for event in (make_event("GET", "/v1/health"), make_event("GET", "/v1/nope")):
        response = handler(event)
        assert response["headers"]["Content-Type"] == "application/json"
        json.loads(response["body"])  # 必须是合法 JSON


def test_error_body_field_names_are_stable() -> None:
    """所有错误响应共用同一组字段名，与网关默认体的 `message` 同名。"""
    for method, path, token in (
        ("GET", "/v1/health", None),
        ("GET", "/v1/nope", TOKEN_RW),
        ("GET", "/v1/bookings", TOKEN_RW),
    ):
        _, body = call(method, path, token=token)
        assert set(body) >= {"message", "error_code", "request_id"}


def test_no_stack_trace_or_token_in_error_body() -> None:
    _, body = call("GET", "/v1/health", token="MOCK_WRONG_TOKEN")
    serialised = json.dumps(body, ensure_ascii=False)
    assert "MOCK_WRONG_TOKEN" not in serialised
    assert "Traceback" not in serialised


# ---------------------------------------------------------------- 幂等（规范 §4.5）


def test_repeated_plan_post_is_idempotent() -> None:
    first_status, first = call("POST", "/v1/plans", body=VALID_PLAN_BODY)
    second_status, second = call("POST", "/v1/plans", body=VALID_PLAN_BODY)

    # 状态码恒定：重跑不会出现 201/200 抖动。
    assert first_status == second_status == 201
    assert first["plan_id"] == second["plan_id"]
    assert first["replayed"] is False and second["replayed"] is True
    # 除 replayed 标记与 request_id 外，响应体逐字段一致。
    ignore = {"replayed", "request_id"}
    assert {k: v for k, v in first.items() if k not in ignore} == {
        k: v for k, v in second.items() if k not in ignore
    }


def test_different_request_yields_different_plan_id() -> None:
    _, first = call("POST", "/v1/plans", body=VALID_PLAN_BODY)
    _, second = call("POST", "/v1/plans", body={**VALID_PLAN_BODY, "days": 3})
    assert first["plan_id"] != second["plan_id"]


def test_idempotency_key_conflict_is_409() -> None:
    booking = {
        "product_id": "MOCK_PROD_AQUARIUM_TICKET",
        "travel_date": "2026-11-20",
        "party": {"adults": 2, "children": 1, "child_ages": [5]},
    }
    headers = {"idempotency-key": "MOCK-IDEM-0001"}
    first, _ = call("POST", "/v1/bookings", body=booking, headers=headers)
    assert first == 201

    conflict, body = call(
        "POST", "/v1/bookings", body={**booking, "travel_date": "2026-11-21"}, headers=headers
    )
    assert conflict == 409
    assert body["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_idempotency_key_replay_returns_same_body() -> None:
    booking = {
        "product_id": "MOCK_PROD_AQUARIUM_TICKET",
        "travel_date": "2026-11-20",
        "party": {"adults": 2, "children": 0},
    }
    headers = {"idempotency-key": "MOCK-IDEM-0002"}
    _, first = call("POST", "/v1/bookings", body=booking, headers=headers)
    _, second = call("POST", "/v1/bookings", body=booking, headers=headers)
    assert first["booking_id"] == second["booking_id"]
    assert second["replayed"] is True


# ---------------------------------------------------------------- 自描述


def test_routes_endpoint_lists_all_routes_without_stage_prefix() -> None:
    status, body = call("GET", "/v1/routes")
    assert status == 200
    assert len(body["routes"]) >= 14
    assert all(route["path"].startswith("/v1/") for route in body["routes"])


def test_stage_prefixed_path_still_resolves() -> None:
    """兼容带 stage 前缀的调用形式（规范 §3.1-4 的路径拼接坑）。"""
    status, _ = call("GET", "/demo/v1/health")
    assert status == 200


def test_trailing_slash_resolves() -> None:
    status, _ = call("GET", "/v1/health/")
    assert status == 200


def test_delivery_notice_present_in_every_success_response() -> None:
    """规范 §1 的交付边界声明内嵌进响应，避免只看接口的人误判成熟度。"""
    for path in ("/v1/health", "/v1/catalog", "/v1/funnel"):
        _, body = call("GET", path)
        assert "CDE 验证原型" in body["delivery_notice"]["maturity"]


# ---------------------------------------------------------------- 必填数组缺失（回归）
# 这三条来自一次代码评审发现的真实缺陷：`object_list(required=True)` 早先只把
# 缺失记入 Validator.missing，而 raise_if_invalid() 只看 errors，
# 导致整个必填数组缺失时被当成空数组静默放过（返回 200/201 而非 422）。


def test_availability_query_without_items_is_422() -> None:
    status, body = call("POST", "/v1/availability/queries", body={"party": {"adults": 2}})
    assert status == 422, "items 整个缺失必须报 422，不能当成空数组"
    assert any(e["field"] == "items" for e in body["errors"])


def test_availability_query_with_empty_items_is_422() -> None:
    status, body = call("POST", "/v1/availability/queries", body={"items": [], "party": {"adults": 2}})
    assert status == 422
    assert any(e["field"] == "items" for e in body["errors"])


def test_events_without_events_array_is_422() -> None:
    status, body = call("POST", "/v1/events", body={"session_id": "MOCK_SESSION_0001"})
    assert status == 422, "events 整个缺失必须报 422，不能返回 accepted_count: 0"
    assert any(e["field"] == "events" for e in body["errors"])


def test_events_without_session_id_is_422() -> None:
    status, body = call("POST", "/v1/events", body={"events": [{"step": "guide_view"}]})
    assert status == 422
    assert any(e["field"] == "session_id" for e in body["errors"])


def test_availability_query_missing_nested_required_is_422() -> None:
    """子对象内的必填字段缺失同样要报 422（nested 必须继承该语义）。"""
    status, body = call(
        "POST",
        "/v1/availability/queries",
        body={"items": [{"travel_date": "2026-11-20"}], "party": {"adults": 2}},
    )
    assert status == 422
    assert any(e["field"] == "items[0].product_id" for e in body["errors"])


def test_availability_query_missing_party_is_422() -> None:
    status, body = call(
        "POST",
        "/v1/availability/queries",
        body={"items": [{"product_id": "MOCK_PROD_AQUARIUM_TICKET", "travel_date": "2026-11-20"}]},
    )
    assert status == 422
    assert any(e["field"] == "party.adults" for e in body["errors"])


def test_booking_missing_required_fields_is_422() -> None:
    status, body = call("POST", "/v1/bookings", body={})
    assert status == 422
    fields = {e["field"] for e in body["errors"]}
    assert {"product_id", "travel_date", "party.adults"} <= fields


def test_single_availability_missing_query_params_is_422() -> None:
    status, body = call("GET", "/v1/products/MOCK_PROD_AQUARIUM_TICKET/availability")
    assert status == 422
    fields = {e["field"] for e in body["errors"]}
    assert "travel_date" in fields and "party.adults" in fields


def test_plans_endpoint_still_clarifies_instead_of_422() -> None:
    """唯一的例外：/v1/plans 的必填缺失仍走澄清（200），不能被这次修复带跑偏。"""
    status, body = call("POST", "/v1/plans", body={})
    assert status == 200
    assert body["status"] == "needs_clarification"
    assert set(body["missing_fields"]) >= {"destination", "start_date", "days", "party.adults"}
