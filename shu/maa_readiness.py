"""Read-only preflight for the already bound MAA WPF client.

This helper evaluates evidence; it never opens UIA, sends input, launches a
process, requests elevation, or uses UIAccess. A ready result is a precondition,
not an action permit or evidence that an operation succeeded. Supplied permission
receipts must come from inspecting this same bound target in the current run.
"""
from __future__ import annotations

import math
import ntpath
import time
from typing import Mapping

from .maa_uia import MAA_EXECUTABLE
from .permissions import inspect_game_input_permission


# Match MaaAgentRoles._observation, which is stricter than MaaUia's action TTL.
MAX_OBSERVATION_AGE = 8.0
MAX_FUTURE_SKEW = 0.5
_PATTERNS = frozenset({"invoke", "toggle", "set_value", "select"})
_MESSAGES = {
    "ready": "MAA 身份、权限与新鲜 UIA 控件观察通过前置检查；实际操作仍需动作授权和结果核验。",
    "identity_invalid": "绑定身份不是指定的 MAA 客户端，不能查询或操作该目标。",
    "permission_mismatch": "MAA 当前以管理员身份运行，黍当前未提升，存在已确认的权限差异；本次阻止 UIA 输入。",
    "permission_unknown": "运行权限证据不完整，不能确认 UIA 输入条件；当前只保留已核验的只读观察。",
    "snapshot_missing": "缺少 MAA UIA 观察，不能确认控件支持。",
    "snapshot_invalid": "MAA UIA 观察格式无效，不能作为运行前证据。",
    "snapshot_wrong_target": "UIA 观察的窗口、进程或程序路径与绑定 MAA 不匹配。",
    "snapshot_stale": "MAA UIA 观察已过期或时间超出容差，需要重新观察。",
    "snapshot_unavailable": "MAA UIA 观察未成功，不能确认控件支持。",
    "provider_unsupported": "MAA 未暴露可用 UIA 控件；仅有根窗口不代表支持控件操作。",
    "snapshot_truncated": "MAA UIA 观察被截断，只能读取已有内容，不能据此输入或确认完整界面状态。",
    "no_actionable_controls": "当前 MAA 观察可读，但没有可确认支持输入的可见、启用控件。",
}
_BLOCKING = frozenset({"identity_invalid", "permission_mismatch", "snapshot_missing",
                       "snapshot_invalid", "snapshot_wrong_target", "snapshot_stale",
                       "snapshot_unavailable", "provider_unsupported"})
_PRIORITY = ("identity_invalid", "permission_mismatch", "snapshot_wrong_target",
             "snapshot_invalid", "snapshot_stale", "snapshot_unavailable",
             "snapshot_missing", "provider_unsupported", "snapshot_truncated",
             "permission_unknown", "no_actionable_controls")


def _positive_int(value):
    return type(value) is int and value > 0


def _path(value):
    if not isinstance(value, str) or not value or "\x00" in value or not ntpath.isabs(value):
        return None
    return ntpath.normcase(ntpath.normpath(value))


def _identity(value):
    fields = ("hwnd", "pid", "executable")
    result = ({key: value.get(key) for key in fields} if isinstance(value, Mapping)
              else {key: getattr(value, key, None) for key in fields})
    if (not _positive_int(result["hwnd"]) or not _positive_int(result["pid"])
            or result["pid"] > 0xffffffff or _path(result["executable"]) != _path(MAA_EXECUTABLE)):
        return None
    return result


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _permission(receipt):
    receipt = receipt if isinstance(receipt, Mapping) else {}
    result = {key: receipt.get(key) if type(receipt.get(key)) is bool else None
              for key in ("own_elevated", "target_elevated")}
    for key in ("own_token_error", "target_token_error"):
        result[key] = receipt.get(key) if type(receipt.get(key)) is int else None
    manifest = receipt.get("manifest_level")
    result["manifest_level"] = manifest if manifest in ("asInvoker", "highestAvailable", "requireAdministrator") else None
    # A manifest or access-denied error alone never proves target elevation.
    result["confirmed_mismatch"] = result["own_elevated"] is False and result["target_elevated"] is True
    return result


def _snapshot_facts(snapshot, target, now):
    facts = {"uia_supported": False, "observable": False, "element_count": 0,
             "actionable_count": 0, "snapshot_id": None, "age_seconds": None}
    if snapshot is None:
        return facts, ["snapshot_missing"]
    if (not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("snapshot_id"), str)
            or not snapshot["snapshot_id"].strip() or not isinstance(snapshot.get("snapshot_status"), str)
            or not isinstance(snapshot.get("window"), Mapping) or not _number(snapshot.get("timestamp"))):
        return facts, ["snapshot_invalid"]
    window = snapshot["window"]
    if (not _positive_int(window.get("hwnd")) or not _positive_int(window.get("pid"))
            or any(window.get(key) != target[key] for key in ("hwnd", "pid"))
            or _path(window.get("executable")) != _path(target["executable"])):
        return facts, ["snapshot_wrong_target"]
    facts["age_seconds"] = now - snapshot["timestamp"]
    if not -MAX_FUTURE_SKEW <= facts["age_seconds"] < MAX_OBSERVATION_AGE:
        return facts, ["snapshot_stale"]
    elements = snapshot.get("elements")
    truncated = snapshot.get("possibly_truncated", False)
    if (not isinstance(elements, list) or type(truncated) is not bool
            or any(not isinstance(item, Mapping) or not isinstance(item.get("patterns"), (list, tuple))
                   or any(not isinstance(pattern, str) for pattern in item["patterns"])
                   for item in elements)):
        return facts, ["snapshot_invalid"]
    status = snapshot["snapshot_status"]
    if status != "ok":
        return facts, ["provider_unsupported" if status == "unsupported" else "snapshot_unavailable"]
    facts["element_count"] = len(elements)
    # Recompute MaaUia's support criterion: a successful transport/root HWND is
    # insufficient, even if a caller incorrectly labels that snapshot "ok".
    facts["uia_supported"] = len(elements) > 1 or any(_PATTERNS.intersection(item["patterns"]) for item in elements)
    if not facts["uia_supported"]:
        return facts, ["provider_unsupported"]
    facts["observable"] = True
    facts["snapshot_id"] = snapshot["snapshot_id"]
    for item in elements:
        if item.get("enabled") is not True or item.get("offscreen") is not False or item.get("password") is not False:
            continue
        patterns = _PATTERNS.intersection(item["patterns"])
        if item.get("read_only") is not False:
            patterns = patterns - {"set_value"}
        if patterns:
            facts["actionable_count"] += 1
    reasons = ["snapshot_truncated"] if truncated else []
    if not facts["actionable_count"]:
        reasons.append("no_actionable_controls")
    return facts, reasons


def inspect_maa_readiness(identity, snapshot=None, *, permission_report=None, native=None, now=None) -> dict:
    """Classify the bound client's evidence as ready, read_only or blocked.

    ``permission_report`` accepts ``inspect_game_input_permission``'s receipt;
    when absent, that existing read-only probe is called with the bound identity
    and optional injectable ``native``. This helper never obtains a snapshot.
    Snapshot freshness follows the formal agent's eight-second observation rule.
    """
    target = _identity(identity)
    facts = {"uia_supported": False, "observable": False, "element_count": 0,
             "actionable_count": 0, "snapshot_id": None, "age_seconds": None}
    if target is None:
        reasons, permission = ["identity_invalid"], _permission(None)
    else:
        current = time.time() if now is None else now
        if not _number(current):
            raise ValueError("now must be a finite timestamp")
        if permission_report is None:
            try:
                permission_report = inspect_game_input_permission(target, native=native)
            except (OSError, ValueError, TypeError):
                permission_report = None
        permission = _permission(permission_report)
        facts, reasons = _snapshot_facts(snapshot, target, current)
        if permission["confirmed_mismatch"]:
            reasons.append("permission_mismatch")
        elif permission["own_elevated"] is None or permission["target_elevated"] is None:
            reasons.append("permission_unknown")
    reasons = [code for code in _PRIORITY if code in reasons]
    status = "blocked" if _BLOCKING.intersection(reasons) else "read_only" if reasons else "ready"
    code = reasons[0] if reasons else "ready"
    return {"status": status, "code": code, "reasons": reasons, "message": _MESSAGES[code],
            "actionable": status == "ready", "verification_required": True,
            "permission": permission, **facts}
