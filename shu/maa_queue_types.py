"""JSON-only queue contracts. Types describe facts, never GUI authority."""
from typing import Literal, NotRequired, TypedDict


class Ref(TypedDict):
    path: str
    sha256: str


class ProcessBinding(TypedDict):
    hwnd: int
    pid: int
    creation_time: int
    executable: str


class MaaBinding(TypedDict):
    process: ProcessBinding
    current_config: str
    config_path: str
    maa_version: str


class ImportTarget(TypedDict):
    target_key: str
    stage_key: str
    stage_id: str
    stage_code: str
    difficulty: Literal[1, 2]
    source_difficulty: int
    batch_key: str
    slot: Literal['primary', 'backup']
    candidate_ref: Ref
    source_binding_ref: Ref
    document_ref: Ref
    roster_ref: dict
    mystery_code: str
    source_semantic_sha256: NotRequired[str]


class ImportBatch(TypedDict):
    batch_key: str
    label: str
    stage_keys: list[str]
    target_keys: list[str]
    navigation_scope_ref: Ref
    navigation_verified: Literal[False]


class QueuePlan(TypedDict):
    schema: Literal['maa_queue_plan/v1']
    task_id: str
    revision: int
    epoch: int
    structure_ref: Ref
    targets: list[ImportTarget]
    batches: list[ImportBatch]
    exclusions: list[dict]
    deferred: list[dict]
    plan_sha256: str


class QueueRow(TypedDict):
    identity: str
    ordinal: int
    copilot_id: int
    file_path: str
    is_raid: bool
    is_checked: bool
    name: str
    index: int | None
    is_nav_name_override: bool | None
    body_ref: Ref | None
    body_sha256: str | None
    semantic_sha256: str | None
    stage_id: str | None
    verification: Literal['verified', 'opaque']


class QueueSnapshot(TypedDict):
    schema: Literal['maa_queue_snapshot/v1']
    binding: MaaBinding
    captured_at: float
    config_state: dict
    projection_sha256: str
    rows: list[QueueRow]
    stable: bool
    screenshot_refs: list[Ref]
    visible_complete: bool
    evidence_ref: Ref
    screenshot_observations: NotRequired[list[dict]]


class QueueDiff(TypedDict):
    preserved: list[str]
    reusable: list[str]
    missing: list[str]
    conflicts: list[dict]
    extra_owned: list[str]
    external_changes: list[dict]


class QueueItemResult(TypedDict):
    target_key: str
    status: Literal['pending', 'reused', 'added', 'conflict', 'unknown', 'blocked']
    row_identity: str | None
    owned: bool
    operation_ids: list[str]
    evidence_refs: list[Ref]
    code: str


class QueueReceipt(TypedDict):
    schema: Literal['maa_queue_receipt/v1']
    plan_sha256: str
    binding: MaaBinding
    baseline_ref: Ref
    final_snapshot_ref: Ref
    items: list[QueueItemResult]
    preserved_baseline: bool
    unexpected_rows: list[str]
    evidence_refs: list[Ref]
    battle_start_submitted: Literal[False]


class QueueGuard(TypedDict):
    task_id: str
    revision: int
    epoch: int
    dispatch_id: str
    plan_sha256: str
    snapshot_sha256: str
    target_key: str | None
    phase: Literal['focus', 'mode', 'code', 'add', 'delete', 'scroll', 'options']
    row_identity: str | None
    item_version: int
