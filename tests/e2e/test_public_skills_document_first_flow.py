from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.contracts.test_change_package_contract import (
    _criterion,
    _functional_requirement,
)
from tests.contracts.test_design_artifact_contract import _artifact
from tests.contracts.test_design_plan_contract import _module, _plan
from tests.contracts.test_design_reference_contract import write_design_reference
from tests.contracts.test_task_planning_code_evidence import _write_task_submission
from tests.test_cli_v16_complex_e2e import MODULE_IDS
from tests.test_cli_v17_draft_contract import (
    import_command,
    requirement_documents,
    write_documents,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SKILLS = {
    "sdlc-material": "codex-sdlc material DRAFT-001",
    "sdlc-discuss": "codex-sdlc draft requirements DRAFT-001",
    "sdlc-design": "codex-sdlc design-plan DRAFT-001",
    "sdlc-start": "codex-sdlc start --file",
    "sdlc-tasks": "codex-sdlc tasks REQ-001",
    "sdlc-task": "codex-sdlc task REQ-001 T-001",
    "sdlc-change": "codex-sdlc change-package REQ-001 CHG-001",
}

SCENARIOS = {
    "完整服务": {
        "requirement": "客服登录后查看工单、客户资料和处理记录。",
        "goal": "交付服务、页面、数据和权限共同组成的工单处理能力。",
        "modules": ("data", "api", "page", "component", "security"),
        "special": "design_drift",
    },
    "资料修订": {
        "requirement": "仓库管理员登记到货单并保留原始供应商资料。",
        "goal": "修订原始资料后只允许当前需求审核继续推进。",
        "modules": ("component",),
        "special": "material_drift",
    },
    "数据接口": {
        "requirement": "财务人员按月份读取结算汇总接口。",
        "goal": "数据字段和接口返回使用同一份设计约束。",
        "modules": ("data", "api"),
        "special": "review_identity",
    },
    "纯前端页面": {
        "requirement": "访客在浏览器查看不依赖服务端的活动说明页。",
        "goal": "只交付页面和组件，不产生数据或接口设计。",
        "modules": ("page", "component"),
        "special": "material_interrupt",
    },
    "任务连续恢复": {
        "requirement": "运维人员调整本地检查开关并保留每次任务运行记录。",
        "goal": "任务完成后可以连续恢复且旧轮次证据保持不变。",
        "modules": ("component", "security"),
        "special": "task_restore",
    },
    "正式版本变更": {
        "requirement": "档案管理员调整文件保留规则并保护正在执行的任务。",
        "goal": "结构化变更能够拒绝错误关闭活动任务并在中断后恢复。",
        "modules": ("data",),
        "special": "change_recovery",
    },
}

BUSINESS_DETAILS = {
    "完整服务": {
        "background": "客服需要在一个工作台核对工单、客户资料和处理记录。",
        "scope": "登录客服查看并处理本人权限内的工单",
        "user_scenario": "客服打开工单工作台并核对客户资料后记录处理结果",
        "rule": "只有具备工单处理权限的客服可以查看客户资料并提交处理记录",
        "elements": ["工单编号", "客户资料", "处理记录", "处理结果"],
        "flow": ["客服登录", "打开工单", "核对客户资料", "提交处理结果"],
        "fact": "工单、客户资料和处理记录需要在同一工作台核对",
        "constraint": "不允许越权读取其他客服不可见的客户资料",
        "exception": "权限不足时拒绝访问，提交失败时保留原处理记录",
        "ac_operation": "客服打开有权限的工单并提交处理结果",
        "ac_expected": "工作台显示工单、客户资料和处理记录并保存新结果",
        "ac_pass": "四类信息完整且越权数据不可见，提交结果可再次读取",
        "code_path": "src/完整服务.py",
        "implementation": "串联工单数据、接口、页面组件和权限检查。",
        "surface": "数据、接口、页面、组件和安全设计必须逐项落到实现。",
        "task_exception": "权限变化或提交失败时不得显示越权数据或覆盖旧记录。",
        "test": "运行完整服务工单工作台定向测试。",
        "manual": "核对工单、客户资料、处理记录和权限拒绝结果。",
        "task_out": "不包含客服组织和权限配置后台。",
        "done": "工单全链路和权限拒绝均有可复核证据。",
    },
    "资料修订": {
        "background": "仓库管理员需要依据当前供应商资料登记到货单并保留批次。",
        "scope": "登记到货单、供应商和到货批次",
        "user_scenario": "仓库管理员核对当前供应商资料后登记一张到货单",
        "rule": "到货登记只能引用当前有效供应商资料并保留原始批次",
        "elements": ["到货单号", "供应商", "到货批次", "登记结果"],
        "flow": ["打开到货登记", "核对供应商", "填写批次", "保存到货单"],
        "fact": "供应商资料修订后旧审核不能继续用于确认",
        "constraint": "不得用已经被替代的供应商资料新建到货单",
        "exception": "资料已修订时停止登记并要求重新审核当前输入",
        "ac_operation": "使用当前供应商资料登记带批次的到货单",
        "ac_expected": "到货单保存供应商和批次并可回查原始资料",
        "ac_pass": "当前资料引用、到货批次和登记结果三者一致",
        "code_path": "src/资料修订.py",
        "implementation": "实现到货登记组件并绑定当前供应商资料版本。",
        "surface": "组件显示供应商版本、到货批次和资料失效状态。",
        "task_exception": "资料修订后阻止继续登记且不留下半成品到货单。",
        "test": "运行供应商资料修订与到货登记定向测试。",
        "manual": "核对资料修订前后审核失效和到货批次回查。",
        "task_out": "不包含供应商结算和采购审批。",
        "done": "当前资料登记成功，旧资料登记被拒绝且没有残留。",
    },
    "数据接口": {
        "background": "财务人员需要按月份读取字段稳定的结算汇总接口。",
        "scope": "按月份查询结算汇总数据",
        "user_scenario": "财务人员选择月份并读取该月结算汇总",
        "rule": "接口月份参数和返回字段必须服从同一份数据设计",
        "elements": ["结算月份", "汇总金额", "结算笔数", "接口状态"],
        "flow": ["选择月份", "调用汇总接口", "校验返回字段", "展示汇总"],
        "fact": "结算数据字段和接口返回需要共同维护",
        "constraint": "不提供逐笔结算修改能力",
        "exception": "月份非法时拒绝请求，数据不可用时返回明确错误",
        "ac_operation": "请求一个有效月份的结算汇总接口",
        "ac_expected": "接口返回该月金额、笔数和成功状态",
        "ac_pass": "参数校验正确且返回字段与数据设计逐项一致",
        "code_path": "src/数据接口.py",
        "implementation": "实现结算汇总数据读取和月份查询接口。",
        "surface": "数据字段和接口请求、响应、错误必须相互引用。",
        "task_exception": "月份无效或数据读取失败时返回固定错误且不返回残缺汇总。",
        "test": "运行结算汇总数据和接口合同测试。",
        "manual": "核对有效月份、非法月份和数据失败三类返回。",
        "task_out": "不包含结算明细修改和付款操作。",
        "done": "数据与接口合同一致且三类返回均有证据。",
    },
    "纯前端页面": {
        "background": "访客需要在浏览器直接查看不依赖服务端的活动说明。",
        "scope": "展示静态活动说明页和页面组件",
        "user_scenario": "访客打开活动地址并阅读完整说明",
        "rule": "页面内容由本地静态资料提供，不调用业务接口",
        "elements": ["活动标题", "活动时间", "活动说明", "参与条件"],
        "flow": ["打开活动地址", "加载静态内容", "展示说明", "适配窄屏"],
        "fact": "活动说明不需要服务端数据或登录状态",
        "constraint": "不包含报名、登录、数据提交和接口调用",
        "exception": "静态资源缺失时显示可理解的页面错误",
        "ac_operation": "在宽屏和窄屏浏览器打开活动说明页",
        "ac_expected": "页面完整显示标题、时间、说明和参与条件",
        "ac_pass": "两种尺寸内容完整，无网络接口请求和布局遮挡",
        "code_path": "src/纯前端页面.py",
        "implementation": "实现静态活动页面与可复用说明组件。",
        "surface": "页面和组件设计不得引入数据表或业务接口。",
        "task_exception": "静态资源缺失时显示错误内容且页面保持可访问。",
        "test": "运行活动说明页静态渲染和响应式测试。",
        "manual": "核对宽屏、窄屏、资源缺失和零接口请求。",
        "task_out": "不包含活动报名、账号登录和服务端接口。",
        "done": "静态页面两种尺寸和错误状态均有可复核结果。",
    },
    "任务连续恢复": {
        "background": "运维人员需要调整本地检查开关并保留每次任务运行记录。",
        "scope": "调整本地检查开关并连续恢复任务轮次",
        "user_scenario": "运维人员完成一次开关调整后按反馈连续恢复检查任务",
        "rule": "每次恢复创建新轮次，旧轮次证据文件不得改写",
        "elements": ["检查开关", "当前轮次", "测试证据", "恢复原因"],
        "flow": ["调整开关", "登记证据", "完成轮次", "按反馈恢复新轮次"],
        "fact": "任务恢复需要保留历史读取清单和测试证据",
        "constraint": "不得覆盖、移动或删除已完成轮次证据",
        "exception": "证据缺失时拒绝完成，恢复失败时保持当前轮次不变",
        "ac_operation": "完成两个任务轮次并连续恢复到第三轮",
        "ac_expected": "第三轮创建成功且前两轮已有文件哈希不变",
        "ac_pass": "轮次编号连续、旧证据逐项一致、恢复原因可追溯",
        "code_path": "src/任务连续恢复.py",
        "implementation": "实现本地检查开关并按任务合同登记每轮证据。",
        "surface": "组件状态和安全约束必须显示当前轮次与失败原因。",
        "task_exception": "证据不全或恢复中断时不得覆盖旧轮次。",
        "test": "运行检查开关和连续任务恢复测试。",
        "manual": "逐轮核对读取清单、测试证据、恢复记录和文件哈希。",
        "task_out": "不包含远程运维平台和跨机器任务调度。",
        "done": "两次完成、两次恢复和旧证据不变均有正式记录。",
    },
    "正式版本变更": {
        "background": "档案管理员需要调整文件保留规则并保护正在执行的任务。",
        "scope": "替换保留规则资料、废弃旧资料并正式生效",
        "user_scenario": "档案管理员提交保留规则变更并完成审核、保护和生效",
        "rule": "变更必须重算五份预计版本并先保护受影响活动任务",
        "elements": ["保留规则", "替换资料", "废弃资料", "活动任务状态"],
        "flow": ["创建变更", "提交预计版本", "完成三类审核", "保护任务并生效"],
        "fact": "活动任务引用发生变化时必须转为失效并保留旧轮次",
        "constraint": "不得跳过审核、任务保护或直接覆盖当前版本",
        "exception": "审核缺失、保护失败或生效中断时停止并保持可恢复事务",
        "ac_operation": "替换当前资料、废弃旧资料并使完整变更正式生效",
        "ac_expected": "三类审核有效，活动轮次受保护，五份当前版本同步更新",
        "ac_pass": "变更事件唯一、版本一致、旧轮次保留且事务目录收口",
        "code_path": "src/正式版本变更.py",
        "implementation": "实现档案保留规则并验证结构化变更完整生效链路。",
        "surface": "数据设计、引用索引、任务计划和版本文件必须属于同一CHG。",
        "task_exception": "审核、保护或生效中断时不得产生半套当前版本。",
        "test": "运行保留规则变更、任务保护和生效事务测试。",
        "manual": "核对替换、废弃、三类审核、活动轮次和五份当前版本。",
        "task_out": "不包含外部档案系统迁移和历史文件物理删除。",
        "done": "变更审核、任务保护、生效恢复和版本一致性全部有证据。",
    },
}

DESIGN_DETAILS = {
    "完整服务": {
        "object": "工单",
        "storage": "work_orders",
        "key": "工单编号",
        "endpoint": "读取工单处理记录",
        "route": "/work-orders/{id}",
        "page": "工单处理页",
        "component": "工单处理结果卡片",
        "security": "工单访问控制",
        "error": "WORK_ORDER_NOT_FOUND",
    },
    "资料修订": {
        "object": "到货资料",
        "storage": "receiving_documents",
        "key": "到货批次",
        "endpoint": "读取到货资料",
        "route": "/receiving-documents/{batch}",
        "page": "到货资料核对页",
        "component": "到货资料完整性卡片",
        "security": "到货原始资料保护",
        "error": "RECEIVING_DOCUMENT_NOT_FOUND",
    },
    "数据接口": {
        "object": "结算汇总",
        "storage": "settlement_summaries",
        "key": "结算月份",
        "endpoint": "按月份读取结算汇总",
        "route": "/settlements/{month}",
        "page": "结算汇总页",
        "component": "结算汇总卡片",
        "security": "结算数据访问控制",
        "error": "INVALID_SETTLEMENT_MONTH",
    },
    "纯前端页面": {
        "object": "活动说明",
        "storage": "activity_guides",
        "key": "活动编号",
        "endpoint": "读取活动说明",
        "route": "/activity",
        "page": "活动说明页",
        "component": "活动参与条件组件",
        "security": "活动说明发布保护",
        "error": "ACTIVITY_GUIDE_NOT_FOUND",
    },
    "任务连续恢复": {
        "object": "检查轮次",
        "storage": "inspection_rounds",
        "key": "轮次编号",
        "endpoint": "读取检查轮次",
        "route": "/inspection-rounds/{id}",
        "page": "检查轮次页",
        "component": "检查轮次状态组件",
        "security": "检查证据完整性保护",
        "error": "INSPECTION_ROUND_NOT_FOUND",
    },
    "正式版本变更": {
        "object": "档案保留规则",
        "storage": "archive_retention_rules",
        "key": "规则编号",
        "endpoint": "读取档案保留规则",
        "route": "/archive-retention/{id}",
        "page": "档案保留规则页",
        "component": "档案保留规则组件",
        "security": "档案变更保护",
        "error": "ARCHIVE_RETENTION_NOT_FOUND",
    },
}

assert set(BUSINESS_DETAILS) == set(SCENARIOS)
assert set(DESIGN_DETAILS) == set(SCENARIOS)
assert len(
    {
        json.dumps(item, ensure_ascii=False, sort_keys=True)
        for item in BUSINESS_DETAILS.values()
    }
) == 6
assert all("课程" not in json.dumps(item, ensure_ascii=False) for item in BUSINESS_DETAILS.values())
assert len(
    {
        json.dumps(
            {
                key: item[key]
                for key in (
                    "background",
                    "scope",
                    "user_scenario",
                    "elements",
                    "flow",
                    "fact",
                    "rule",
                    "constraint",
                    "exception",
                    "ac_operation",
                    "ac_expected",
                    "ac_pass",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in BUSINESS_DETAILS.values()
    }
) == 6
assert len(
    {
        json.dumps(
            {
                key: item[key]
                for key in (
                    "code_path",
                    "implementation",
                    "surface",
                    "task_exception",
                    "test",
                    "manual",
                    "task_out",
                    "done",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in BUSINESS_DETAILS.values()
    }
) == 6


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(value) + b"\n")


def _business_requirement_material(
    scenario_name: str,
    *,
    requirement: str,
    goal: str,
) -> str:
    """把拆分所用的全部业务事实先写入正式归档的需求资料。"""

    detail = BUSINESS_DETAILS[scenario_name]
    return "\n".join(
        (
            f"# {scenario_name}需求",
            "",
            "## 业务目标",
            f"- 原始诉求：{requirement}",
            f"- 交付目标：{goal}",
            f"- 业务背景：{detail['background']}",
            f"- 交付范围：{detail['scope']}",
            f"- 用户场景：{detail['user_scenario']}",
            "",
            "## 全局规则",
            f"- {detail['rule']}",
            "",
            "## 功能要求",
            f"- 功能标题：{detail['scope']}",
            f"- 功能描述：{detail['user_scenario']}",
            f"- 元素或字段：{'；'.join(detail['elements'])}",
            f"- 处理流程：{'；'.join(detail['flow'])}",
            f"- 已确认事实：{detail['fact']}",
            f"- 业务规则：{detail['rule']}",
            f"- 约束：{detail['constraint']}",
            f"- 状态与异常：{detail['exception']}",
            f"- 不在范围：{detail['task_out']}",
            "",
            "## 验收要求",
            f"- 操作：{detail['ac_operation']}",
            f"- 预期：{detail['ac_expected']}",
            f"- 通过标准：{detail['ac_pass']}",
            "",
        )
    )


def _heading_source_ref(
    project: Path,
    material: dict[str, object],
    heading: str,
) -> dict[str, object]:
    """按标题定位归档资料片段，让规则、功能和验收各自回指准确行。"""

    archived = (
        project
        / ".codex-sdlc"
        / "drafts"
        / "DRAFT-001"
        / str(material["stored_path"])
    )
    lines = archived.read_text(encoding="utf-8").splitlines(keepends=True)
    line_start = next(
        index
        for index, line in enumerate(lines, start=1)
        if line.rstrip("\r\n") == heading
    )
    following_headings = [
        index
        for index, line in enumerate(lines[line_start:], start=line_start + 1)
        if line.startswith("## ")
    ]
    line_end = (following_headings[0] - 1) if following_headings else len(lines)
    fragment = "".join(lines[line_start - 1 : line_end]).encode("utf-8")
    return {
        "material_id": str(material["material_id"]),
        "reference": {
            "schema_version": "reference-locator.v1",
            "path": archived.relative_to(project).as_posix(),
            "sha256": _sha256(archived),
            "locator": {
                "kind": "text_range",
                "line_start": line_start,
                "line_end": line_end,
                "fragment_sha256": hashlib.sha256(fragment).hexdigest(),
                "display_heading": heading.removeprefix("## "),
            },
        },
    }


def _apply_business_requirement(
    split: dict[str, object],
    coverage: dict[str, object],
    *,
    project: Path,
    material: dict[str, object],
    scenario_name: str,
    goal: str,
) -> None:
    """完整改写当前业务，并把每类实体回指到归档资料的准确片段。"""

    detail = BUSINESS_DETAILS[scenario_name]
    rule_ref = _heading_source_ref(project, material, "## 全局规则")
    requirement_ref = _heading_source_ref(project, material, "## 功能要求")
    acceptance_ref = _heading_source_ref(project, material, "## 验收要求")
    split.update(
        {
            "title": f"{scenario_name}交付需求",
            "background": detail["background"],
            "goal": goal,
            "scope": [detail["scope"]],
            "out_of_scope": [detail["task_out"]],
            "user_scenarios": [detail["user_scenario"]],
        }
    )
    global_rule = split["global_rules"][0]
    global_rule.update(
        {
            "title": f"{scenario_name}统一业务规则",
            "description": detail["rule"],
            "source_refs": [rule_ref],
        }
    )
    requirement = split["functional_requirements"][0]
    requirement.update(
        {
            "title": detail["scope"],
            "description": detail["user_scenario"],
            "elements": detail["elements"],
            "flow": detail["flow"],
            "facts": [detail["fact"]],
            "rules": [detail["rule"]],
            "constraints": [detail["constraint"]],
            "states_and_exceptions": [detail["exception"]],
            "out_of_scope": [detail["task_out"]],
            "source_refs": [requirement_ref],
        }
    )
    requirement["acceptance_criteria"][0].update(
        {
            "operation": detail["ac_operation"],
            "expected": detail["ac_expected"],
            "pass_standard": detail["ac_pass"],
            "source_refs": [acceptance_ref],
        }
    )
    coverage_refs = (rule_ref, requirement_ref, acceptance_ref)
    for unit, source_ref in zip(coverage["units"], coverage_refs, strict=True):
        unit["source_ref"] = deepcopy(source_ref)
    assert "课程" not in json.dumps(split, ensure_ascii=False)
    archived_text = (
        project
        / ".codex-sdlc"
        / "drafts"
        / "DRAFT-001"
        / str(material["stored_path"])
    ).read_text(encoding="utf-8")
    required_source_text = (
        goal,
        detail["background"],
        detail["scope"],
        detail["user_scenario"],
        *detail["elements"],
        *detail["flow"],
        detail["fact"],
        detail["rule"],
        detail["constraint"],
        detail["exception"],
        detail["task_out"],
        detail["ac_operation"],
        detail["ac_expected"],
        detail["ac_pass"],
    )
    assert all(text in archived_text for text in required_source_text)
    entities = (
        global_rule["source_refs"][0],
        requirement["source_refs"][0],
        requirement["acceptance_criteria"][0]["source_refs"][0],
    )
    assert tuple(unit["source_ref"] for unit in coverage["units"]) == entities
    assert len(
        {
            item["reference"]["locator"]["fragment_sha256"]
            for item in entities
        }
    ) == 3
    requirement_locator = requirement_ref["reference"]["locator"]
    archived_lines = archived_text.splitlines(keepends=True)
    requirement_fragment = "".join(
        archived_lines[
            requirement_locator["line_start"] - 1 : requirement_locator["line_end"]
        ]
    )
    for field_name in (
        "title",
        "description",
        "elements",
        "flow",
        "facts",
        "rules",
        "constraints",
        "states_and_exceptions",
        "out_of_scope",
    ):
        field_value = requirement[field_name]
        source_values = field_value if isinstance(field_value, list) else [field_value]
        assert all(str(value) in requirement_fragment for value in source_values), (
            scenario_name,
            field_name,
        )


def _apply_business_task(
    task: dict[str, object],
    *,
    scenario_name: str,
    goal: str,
    requirement: str,
    design_refs: list[str],
    technical_material_id: str,
) -> None:
    """六类任务分别固定实现范围、测试、异常和完成条件。"""

    detail = BUSINESS_DETAILS[scenario_name]
    task.update(
        {
            "title": f"实现{scenario_name}",
            "goal": goal,
            "deliverables": [requirement],
            "design_refs": design_refs,
            "material_refs": ["MAT-001", technical_material_id],
            "code_scope": {
                "read_paths": [detail["code_path"]],
                "likely_change_paths": [detail["code_path"]],
                "protected_paths": [".codex-sdlc/requirements"],
            },
            "implementation_requirements": [detail["implementation"]],
            "data_api_page_component_requirements": [detail["surface"]],
            "states_and_exceptions": [detail["task_exception"]],
            "automated_tests": [detail["test"]],
            "manual_checks": [detail["manual"]],
            "out_of_scope": [detail["task_out"]],
            "definition_of_done": [detail["done"]],
        }
    )
    assert "不涉及页面" not in json.dumps(task, ensure_ascii=False)
    assert "不执行任务审核" not in json.dumps(task, ensure_ascii=False)


def _business_design_content(
    scenario_name: str,
    module_type: str,
    *,
    depends_on: list[str],
    technical_material_id: str,
) -> dict[str, object]:
    """按当前业务对象生成模块内容，不沿用合同测试中的用户夹具。"""

    detail = DESIGN_DETAILS[scenario_name]
    object_name = detail["object"]
    contents: dict[str, dict[str, object]] = {
        "data": {
            "entities": [
                {
                    "entity_id": "ENT-001",
                    "name": object_name,
                    "storage_name": detail["storage"],
                    "fields": [
                        {
                            "field_id": "DF-001",
                            "name": detail["key"],
                            "type": "string",
                            "nullable": False,
                            "default": None,
                            "unique": True,
                        }
                    ],
                    "unique_constraints": [["DF-001"]],
                    "indexes": [
                        {
                            "index_id": "IDX-001",
                            "field_ids": ["DF-001"],
                            "unique": True,
                        }
                    ],
                    "relations": [],
                }
            ],
            "lifecycle": {
                "retention": f"按{object_name}业务规则保留",
                "deletion": f"只按{object_name}正式变更删除",
            },
            "migration_steps": [f"创建{object_name}存储 {detail['storage']}"],
            "rollback_steps": [f"撤销{object_name}存储变更"],
        },
        "api": {
            "endpoints": [
                {
                    "endpoint_id": "EP-001",
                    "name": detail["endpoint"],
                    "caller": f"{scenario_name}调用方",
                    "provider": f"{object_name}服务",
                    "transport": "HTTP",
                    "path_or_event": f"GET {detail['route']}",
                    "authentication": f"校验{object_name}读取权限",
                    "request_fields": [
                        {
                            "field_id": "AF-001",
                            "name": detail["key"],
                            "type": "string",
                            "required": True,
                            "data_field_ref": "DATA-001#DF-001",
                        }
                    ],
                    "response_fields": [
                        {
                            "field_id": "AF-002",
                            "name": detail["key"],
                            "type": "string",
                            "required": True,
                            "data_field_ref": "DATA-001#DF-001",
                        }
                    ],
                    "errors": [
                        {
                            "error_id": "ERR-001",
                            "code": detail["error"],
                            "condition": f"{object_name}不存在或输入无效",
                            "response": f"返回{object_name}明确错误",
                        }
                    ],
                    "idempotency": f"{object_name}读取接口天然幂等",
                    "retry": "网络失败最多重试一次",
                    "timeout_ms": 3000,
                }
            ]
        },
        "page": {
            "pages": [
                {
                    "page_id": "PG-001",
                    "name": detail["page"],
                    "route": detail["route"],
                    "navigation_refs": [],
                    "elements": [
                        {
                            "element_id": "EL-001",
                            "name": detail["key"],
                            "data_source_refs": (
                                ["API-001#EP-001"]
                                if "API-001" in depends_on
                                else []
                            ),
                        }
                    ],
                    "states": {
                        "initial": f"等待打开{detail['page']}",
                        "loading": f"准备{object_name}内容",
                        "empty": f"显示无{object_name}内容",
                        "ready": f"完整显示{object_name}",
                        "error": f"显示{object_name}读取失败",
                        "forbidden": f"显示{object_name}不可访问",
                    },
                    "layout": f"{detail['page']}主内容布局",
                    "interactions": [f"打开页面后展示{object_name}"],
                    "responsive": [f"{detail['page']}在宽窄屏均完整显示"],
                    "ui_material_refs": [technical_material_id],
                }
            ]
        },
        "component": {
            "components": [
                {
                    "component_id": "CM-001",
                    "name": detail["component"],
                    "responsibilities": [f"显示并保护{object_name}关键信息"],
                    "inputs": [detail["key"]],
                    "outputs": [f"{object_name}展示结果"],
                    "dependencies": depends_on,
                    "states": [f"{object_name}准备中", f"{object_name}可用", f"{object_name}失败"],
                    "error_handling": [f"{object_name}失败时显示明确恢复入口"],
                }
            ]
        },
        "security": {
            "controls": [
                {
                    "control_id": "SEC-001",
                    "name": detail["security"],
                    "assets": [object_name],
                    "actors": [f"{scenario_name}操作人员"],
                    "permissions": [f"只能执行{object_name}授权动作"],
                    "sensitive_data": [detail["key"]],
                    "authentication": [f"校验{object_name}操作身份"],
                    "audit": [f"记录{object_name}处理结果"],
                    "threats": [f"{object_name}被越权修改"],
                    "mitigations": [f"按{object_name}规则校验并保留证据"],
                }
            ]
        },
    }
    return contents[module_type]


def _business_design_summary(
    scenario_name: str,
    module_types: tuple[str, ...],
) -> dict[str, object]:
    """总体说明只登记当前场景真实存在的跨模块对象与依赖。"""

    detail = DESIGN_DETAILS[scenario_name]
    module_ids = [MODULE_IDS[module_type] for module_type in module_types]
    common_objects = []
    relation_specs = (
        (
            "data",
            "api",
            "entity",
            ["DATA-001#ENT-001"],
            f"{detail['object']}核心实体",
        ),
        (
            "data",
            "api",
            "api_field",
            ["DATA-001#DF-001", "API-001#AF-001"],
            f"{detail['object']}数据接口关系",
        ),
        (
            "api",
            "page",
            "page_source",
            ["API-001#EP-001", "PAGE-001#EL-001"],
            f"{detail['page']}数据来源",
        ),
        (
            "page",
            "component",
            "component",
            ["COMP-001#CM-001"],
            detail["component"],
        ),
    )
    for left, right, object_type, source_refs, canonical_name in relation_specs:
        if left not in module_types or right not in module_types:
            continue
        common_objects.append(
            {
                "business_id": f"COMMON-{len(common_objects) + 1:03d}",
                "object_type": object_type,
                "source_refs": source_refs,
                "applies_to_modules": (
                    [
                        MODULE_IDS[item]
                        for item in ("page", "component", "security")
                        if item in module_types
                    ]
                    if object_type == "component"
                    else [MODULE_IDS[left], MODULE_IDS[right]]
                ),
                "definition": {
                    "canonical_name": canonical_name,
                    "contract": (
                        f"{scenario_name}中的{detail['object']}必须在"
                        f"{MODULE_IDS[left]}与{MODULE_IDS[right]}之间保持一致。"
                    ),
                },
            }
        )
    if (
        "component" in module_types
        and "security" in module_types
        and "page" not in module_types
    ):
        common_objects.append(
            {
                "business_id": f"COMMON-{len(common_objects) + 1:03d}",
                "object_type": "component",
                "source_refs": ["COMP-001#CM-001"],
                "applies_to_modules": ["COMP-001", "SAFE-001"],
                "definition": {
                    "canonical_name": f"{detail['component']}安全关系",
                    "contract": (
                        f"{scenario_name}中的{detail['component']}必须受"
                        f"{detail['security']}约束。"
                    ),
                },
            }
        )
    assert common_objects
    return {
        "schema_version": "design-summary.v1",
        "draft_id": "DRAFT-001",
        "common_objects": common_objects,
        "affected_modules": sorted(module_ids),
        "open_questions": [],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(project: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _run(
    project: Path,
    cli: str,
    *args: str,
    thread_id: str,
    expected: int = 0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """业务动作只从默认终端找到的正式入口进入，不调用内部服务。"""

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("CODEX_SDLC_PYTHON", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    env["CODEX_THREAD_ID"] = thread_id
    env.update(extra_env or {})
    result = subprocess.run(
        [cli, *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def _current_request(project: Path, *, stage: str, owner: str) -> dict[str, object]:
    registry = json.loads(
        (project / ".codex-sdlc/trust/reviews/registry.json").read_text(
            encoding="utf-8"
        )
    )
    requests = [
        record["request"]
        for record in registry["requests"].values()
        if record["request"]["stage"] == stage
        and record["request"]["owner_id"] == owner
        and record["status"] == "pending"
    ]
    assert len(requests) == 1
    return requests[0]


def _submit_passed_review(
    project: Path,
    cli: str,
    *,
    request: dict[str, object],
    reviewer: str,
    expected: int = 0,
) -> None:
    result_file = project / "输入" / f"{request['review_id']}-审核结果.json"
    _write_json(
        result_file,
        {
            "schema_version": "review-result.v1",
            "review_id": request["review_id"],
            "stage": request["stage"],
            "owner_id": request["owner_id"],
            "reviewer_run_id": "提交文件中的身份不会覆盖真实任务身份",
            "input_hashes": request["input_hashes"],
            "status": "passed",
            "issues": [],
            "notes": ["全部受控输入、业务范围和验收关系均已核对。"],
            "reviewed_at": "2026-07-29T06:00:00+08:00",
        },
    )
    _run(
        project,
        cli,
        "review",
        "submit",
        "--request",
        str(request["review_id"]),
        "--file",
        result_file.relative_to(project).as_posix(),
        thread_id=reviewer,
        expected=expected,
    )


def _material(project: Path, material_id: str) -> dict[str, object]:
    manifest = json.loads(
        (
            project
            / ".codex-sdlc/drafts/DRAFT-001/material-manifest.v1.json"
        ).read_text(encoding="utf-8")
    )
    materials = manifest.get("materials", manifest)
    if isinstance(materials, dict):
        candidates = list(materials.values())
    else:
        candidates = list(materials)
    return next(item for item in candidates if item["material_id"] == material_id)


def _formal_package(project: Path) -> dict[str, object]:
    index_path = project / ".codex-sdlc/drafts/DRAFT-001/artifact-index.v1.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    manifest = [
        {
            key: deepcopy(value)
            for key, value in item.items()
            if key
            not in {
                "record_version",
                "include_in_formal",
                "producer_task_id",
                "producer_run_id",
                "input_hashes",
            }
        }
        for item in index["artifacts"]
        if item["include_in_formal"] is True
    ]
    registry = json.loads(
        (project / ".codex-sdlc/trust/reviews/registry.json").read_text(
            encoding="utf-8"
        )
    )
    passed = {
        record["request"]["stage"]: record["request"]["review_id"]
        for record in registry["requests"].values()
        if record["request"]["owner_id"] == "DRAFT-001"
        and record["status"] == "completed"
    }
    return {
        "formal_contract_version": "formal.v3",
        "workflow_profile": "document-first.v1",
        "source_draft_id": "DRAFT-001",
        "source_revision_sha256": index["draft_revision_sha256"],
        "reviews": {
            "requirement_split": passed["requirement_split"],
            "integrated_design": passed["integrated_design"],
        },
        "artifact_index": {
            "source_path": "artifact-index.v1.json",
            "archive_path": "original/artifact-index.v1.json",
            "sha256": hashlib.sha256(_canonical_bytes(index) + b"\n").hexdigest(),
        },
        "artifact_manifest": manifest,
        "open_questions": [],
    }


def _change_inputs(
    project: Path,
    requirement_root: Path,
    status: dict[str, object],
) -> dict[str, dict[str, object]]:
    base_paths = {
        "requirement": "effective/requirement.current.json",
        "design": "effective/design.current.json",
        "test_matrix": "effective/test-matrix.current.json",
        "reference_index": "reference-index.v1.json",
        "task_plan": "tasks/task-plan.v2.json",
    }
    bases = {
        name: json.loads((requirement_root / relative).read_text(encoding="utf-8"))
        for name, relative in base_paths.items()
    }
    locator = bases["reference_index"]["entries"]["MAT-001"]
    new_material_id = (
        f"MAT-{max(int(key.removeprefix('MAT-')) for key in bases['reference_index']['entries'] if key.startswith('MAT-')) + 1:03d}"
    )
    change_material = (
        project / str(status["workspace_path"]) / "原始资料/CMAT-001"
    )
    change_material_sha256 = _sha256(change_material)
    new_ac = _criterion("new-ac", "@client:new-fr", locator)
    new_fr = _functional_requirement("new-fr", [new_ac], locator)
    package = {
        "schema_version": "change-package.v1",
        "requirement_id": "REQ-001",
        "change_id": "CHG-001",
        "producer_run_id": "T051-结构化变更生产",
        "reason": "为报表导出增加可单独验收的重试规则。",
        "base_versions": status["base_versions"],
        "source_refs": ["MAT-001"],
        "requirement_operations": [
            {
                "operation": "add",
                "client_key": "new-fr",
                "next_value": new_fr,
                "source_refs": ["MAT-001"],
            }
        ],
        "global_rule_operations": [],
        "acceptance_operations": [
            {
                "operation": "add",
                "client_key": "new-ac",
                "next_value": new_ac,
                "source_refs": ["MAT-001"],
            }
        ],
        "design_operations": [],
        "material_operations": [
            {
                "operation": "add",
                "client_key": "new-mat",
                "source_material_id": "CMAT-001",
                "workspace_path": "原始资料/CMAT-001",
                "sha256": change_material_sha256,
                "version_evidence": {
                    "kind": "local_snapshot",
                    "sha256": change_material_sha256,
                },
                "source_refs": ["CMAT-001"],
            }
        ],
        "task_impacts": {
            "restore": [],
            "add": [],
            "close": [],
            "unaffected": [{"task_id": "T-001", "basis_refs": ["FR-001"]}],
        },
        "review_impacts": [
            {"stage": "requirement_split", "reason_refs": ["FR-001"]}
        ],
        "open_questions": [],
    }
    rewritten = deepcopy(package)
    rewritten["requirement_operations"][0]["next_value"][
        "acceptance_criteria"
    ][0]["owner_fr_ref"] = "FR-002"
    rewritten["acceptance_operations"][0]["next_value"][
        "owner_fr_ref"
    ] = "FR-002"
    package_path = f"{status['workspace_path']}/change-package.v1.json"

    projected_requirement = deepcopy(bases["requirement"])
    projected_requirement["version"] = "requirement.v2"
    projected_requirement["is_current"] = False
    formal_ac = {"id": "AC-002", **deepcopy(new_ac)}
    formal_ac["owner_fr_ref"] = "FR-002"
    formal_fr = {"id": "FR-002", **deepcopy(new_fr)}
    formal_fr["acceptance_criteria"] = [formal_ac]
    projected_requirement["functional_requirements"].append(formal_fr)

    projected_design = deepcopy(bases["design"])
    projected_design["version"] = "design.v2"
    projected_design["is_current"] = False
    projected_test = deepcopy(bases["test_matrix"])
    projected_test["version"] = "test-matrix.v2"
    projected_test["is_current"] = False
    projected_test["acceptance_criteria"].append(
        {
            "id": "AC-002",
            "requirement_id": "FR-002",
            **{key: value for key, value in formal_ac.items() if key != "id"},
        }
    )
    projected_reference = deepcopy(bases["reference_index"])
    package_locator = {
        "schema_version": "reference-locator.v1",
        "path": package_path,
        "sha256": hashlib.sha256(_canonical_bytes(rewritten) + b"\n").hexdigest(),
        "locator": {"kind": "whole_file"},
    }
    projected_reference["entries"].update(
        {
            "AC-002": package_locator,
            "FR-002": package_locator,
            new_material_id: {
                "schema_version": "reference-locator.v1",
                "path": (
                    f"{status['workspace_path']}/原始资料/CMAT-001"
                ),
                "sha256": change_material_sha256,
                "locator": {"kind": "whole_file"},
            },
        }
    )
    projected_reference["entries"] = {
        key: projected_reference["entries"][key]
        for key in sorted(projected_reference["entries"])
    }
    projected_task = deepcopy(bases["task_plan"])
    projected_task["producer_run_id"] = "T051-结构化变更生产"
    projected_task["input_hashes"] = {
        **projected_task["input_hashes"],
        "change_package": _canonical_sha256(rewritten),
        **{
            f"base_{name}": status["base_versions"][name]["sha256"]
            for name in base_paths
        },
    }
    contents = {
        "projected-requirement.v2.json": projected_requirement,
        "projected-design.v2.json": projected_design,
        "projected-test-matrix.v2.json": projected_test,
        "projected-reference-index.v2.json": projected_reference,
        "projected-task-plan.v2.json": projected_task,
    }
    result = {"change-package.v1.json": package}
    base_name_by_file = {
        "projected-requirement.v2.json": "requirement",
        "projected-design.v2.json": "design",
        "projected-test-matrix.v2.json": "test_matrix",
        "projected-reference-index.v2.json": "reference_index",
        "projected-task-plan.v2.json": "task_plan",
    }
    for filename, content in contents.items():
        result[filename] = {
            "schema_version": filename.removesuffix(".json"),
            "requirement_id": "REQ-001",
            "change_id": "CHG-001",
            "base": status["base_versions"][base_name_by_file[filename]],
            "content": content,
            "content_sha256": _canonical_sha256(content),
        }
    return result


def _lifecycle_change_inputs(
    requirement_root: Path,
    status: dict[str, object],
    add_inputs: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """把新增资料改为替换，并按当前基础版本重算五份完整预计结果。"""

    result = deepcopy(add_inputs)
    package = result["change-package.v1.json"]
    package["review_impacts"] = [
        {"stage": stage, "reason_refs": ["FR-001"]}
        for stage in ("requirement_split", "integrated_design", "task_plan")
    ]
    package["task_impacts"] = {
        "restore": [],
        "add": [],
        "close": [],
        "unaffected": [],
    }
    current_references = json.loads(
        (requirement_root / "reference-index.v1.json").read_text(encoding="utf-8")
    )["entries"]
    replacement = package["material_operations"][0]
    replacement["operation"] = "replace"
    replacement["target_id"] = "MAT-001"
    replacement["base_revision_sha256"] = _canonical_sha256(
        current_references["MAT-001"]
    )
    package["material_operations"].append(
        {
            "operation": "deprecate",
            "target_id": "MAT-002",
            "base_revision_sha256": _canonical_sha256(
                current_references["MAT-002"]
            ),
            "reason": "技术资料由结构化变更中的正式资料接替",
            "replacement_refs": ["MAT-001"],
            "source_refs": ["MAT-002", "CMAT-001"],
        }
    )

    # CLI 会把临时 FR/AC 引用改写为正式编号，预计引用和任务输入必须使用改写后的包哈希。
    rewritten = deepcopy(package)
    rewritten["requirement_operations"][0]["next_value"][
        "acceptance_criteria"
    ][0]["owner_fr_ref"] = "FR-002"
    rewritten["acceptance_operations"][0]["next_value"][
        "owner_fr_ref"
    ] = "FR-002"
    package_path = f"{status['workspace_path']}/change-package.v1.json"
    package_locator = {
        "schema_version": "reference-locator.v1",
        "path": package_path,
        "sha256": hashlib.sha256(_canonical_bytes(rewritten) + b"\n").hexdigest(),
        "locator": {"kind": "whole_file"},
    }

    projected_reference = result["projected-reference-index.v2.json"]["content"]
    projected_reference["entries"] = deepcopy(current_references)
    projected_reference["entries"]["AC-002"] = deepcopy(package_locator)
    projected_reference["entries"]["FR-002"] = deepcopy(package_locator)
    change_material = (
        Path(status["workspace_path"]) / "原始资料/CMAT-001"
    ).as_posix()
    projected_reference["entries"]["MAT-001"] = {
        "schema_version": "reference-locator.v1",
        "path": change_material,
        "sha256": replacement["sha256"],
        "locator": {"kind": "whole_file"},
    }
    projected_reference["entries"]["MAT-002"]["lifecycle"] = {
        "status": "deprecated",
        "change_id": "CHG-001",
        "reason": "技术资料由结构化变更中的正式资料接替",
        "replacement_refs": ["MAT-001"],
    }
    projected_reference["entries"] = {
        key: projected_reference["entries"][key]
        for key in sorted(projected_reference["entries"])
    }
    result["projected-reference-index.v2.json"][
        "content_sha256"
    ] = _canonical_sha256(projected_reference)

    projected_task = result["projected-task-plan.v2.json"]["content"]
    projected_task["input_hashes"]["change_package"] = _canonical_sha256(rewritten)
    result["projected-task-plan.v2.json"][
        "content_sha256"
    ] = _canonical_sha256(projected_task)
    return result


def _change_command_args(project: Path, change_root: Path) -> tuple[str, ...]:
    """六份结构化变更输入始终通过公开命令一起提交。"""

    option_by_name = {
        "change-package.v1.json": "--package",
        "projected-requirement.v2.json": "--projected-requirement",
        "projected-design.v2.json": "--projected-design",
        "projected-test-matrix.v2.json": "--projected-test-matrix",
        "projected-reference-index.v2.json": "--projected-reference-index",
        "projected-task-plan.v2.json": "--projected-task-plan",
    }
    args = ["change-package", "REQ-001", "CHG-001"]
    for name, option in option_by_name.items():
        args.extend([option, (change_root / name).relative_to(project).as_posix()])
    return tuple(args)


def _tree_file_hashes(root: Path) -> dict[str, str]:
    """保存旧任务轮次每个文件的内容哈希，恢复后逐项核对。"""

    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _complete_task_run(
    project: Path,
    cli: str,
    requirement_root: Path,
    *,
    run_number: int,
    thread_id: str = "T051-任务开发",
) -> None:
    """为当前轮次登记真实文件证据，再从公开入口完成任务。"""

    task = json.loads(
        (requirement_root / "tasks/T-001.json").read_text(encoding="utf-8")
    )
    evidence_root = (
        requirement_root
        / f"runtime/T-001/runs/{run_number:04d}/evidence-input"
    )
    evidence_root.mkdir(parents=True)
    test_file = evidence_root / "定向测试.log"
    test_file.write_text("定向测试退出码：0\n", encoding="utf-8")
    _run(
        project,
        cli,
        "task-evidence",
        "REQ-001",
        "T-001",
        "--kind",
        "test",
        "--source-file",
        test_file.relative_to(project).as_posix(),
        "--sha256",
        _sha256(test_file),
        "--command",
        "python3 -m pytest -q tests/当前专题",
        "--exit-code",
        "0",
        "--result",
        "passed",
        "--test-item",
        str(task["automated_tests"][0]),
        thread_id=thread_id,
    )
    manual_file = evidence_root / "人工验收.json"
    _write_json(
        manual_file,
        {
            "environment": "仓库外临时项目",
            "checks": [
                {
                    "item": item,
                    "expected": "符合任务合同",
                    "actual": "逐项核对通过",
                    "result": "passed",
                }
                for item in task["manual_checks"]
            ],
        },
    )
    _run(
        project,
        cli,
        "task-evidence",
        "REQ-001",
        "T-001",
        "--kind",
        "verification",
        "--source-file",
        manual_file.relative_to(project).as_posix(),
        "--sha256",
        _sha256(manual_file),
        "--command",
        "人工逐项验收",
        "--exit-code",
        "0",
        "--result",
        "passed",
        thread_id=thread_id,
    )
    _run(
        project,
        cli,
        "task-done",
        "REQ-001",
        "T-001",
        thread_id=thread_id,
    )


@pytest.mark.parametrize(("scenario_name", "spec"), SCENARIOS.items())
def test_default_terminal_and_installed_skills_drive_six_distinct_topics(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    scenario_name: str,
    spec: dict[str, object],
) -> None:
    cli = shutil.which("codex-sdlc")
    assert cli is not None
    assert Path(cli).resolve() == (REPOSITORY_ROOT / "bin/codex-sdlc").resolve()
    for skill, marker in PUBLIC_SKILLS.items():
        source = REPOSITORY_ROOT / "skills" / skill / "SKILL.md"
        installed = Path.home() / ".codex/skills" / skill / "SKILL.md"
        assert source.read_bytes() == installed.read_bytes()
        assert marker in installed.read_text(encoding="utf-8")

    project = tmp_path / scenario_name
    project.mkdir()
    # 用例中途失败时也由 pytest 删除仓库外项目，不能把故障现场遗留成活动状态。
    request.addfinalizer(lambda: shutil.rmtree(project, ignore_errors=True))
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "e2e@example.invalid")
    _git(project, "config", "user.name", f"{scenario_name}验收")
    (project / "README.md").write_text(f"# {scenario_name}\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-q", "-m", "建立临时项目")
    _run(project, cli, "init", thread_id=f"T051-{scenario_name}-初始化")
    _run(
        project,
        cli,
        "draft",
        "create",
        f"{scenario_name}交付需求",
        thread_id=f"T051-{scenario_name}-资料生产",
    )

    requirement_source = project / f"{scenario_name}需求.md"
    requirement_source.write_text(
        _business_requirement_material(
            scenario_name,
            requirement=str(spec["requirement"]),
            goal=str(spec["goal"]),
        ),
        encoding="utf-8",
    )
    solution_source = project / f"{scenario_name}技术方案.md"
    solution_source.write_text(
        f"# {scenario_name}技术方案\n\n## 适用模块\n\n"
        + "、".join(str(item) for item in spec["modules"])
        + "\n",
        encoding="utf-8",
    )
    material_commands = (
        ("requirement", f"{scenario_name}需求原文", requirement_source),
        ("technical-solution", f"{scenario_name}技术方案", solution_source),
    )
    for index, (material_type, title, path) in enumerate(material_commands):
        args = (
            "material",
            "DRAFT-001",
            "--type",
            material_type,
            "--title",
            title,
            "--file",
            path.name,
        )
        if spec["special"] == "material_interrupt" and index == 0:
            _run(
                project,
                cli,
                *args,
                thread_id=f"T051-{scenario_name}-资料生产",
                expected=86,
                extra_env={"CODEX_SDLC_MATERIAL_INTERRUPT_AT": "after_event_append"},
            )
        _run(
            project,
            cli,
            *args,
            thread_id=f"T051-{scenario_name}-资料生产",
        )
    assert _sha256(requirement_source) == _material(project, "MAT-001")["sha256"]

    suffix = f"t051-{list(SCENARIOS).index(scenario_name) + 1}"
    requirement_material = _material(project, "MAT-001")
    split, coverage = requirement_documents(
        project,
        requirement_material,
        suffix=suffix,
        long_description=str(spec["requirement"]),
    )
    split["producer_run_id"] = f"T051-{scenario_name}-需求生产"
    _apply_business_requirement(
        split,
        coverage,
        project=project,
        material=requirement_material,
        scenario_name=scenario_name,
        goal=str(spec["goal"]),
    )
    split_path, coverage_path = write_documents(project, split, coverage)
    _run(
        project,
        cli,
        *import_command(split_path, coverage_path),
        thread_id=f"T051-{scenario_name}-需求生产",
    )
    _run(
        project,
        cli,
        "draft",
        "requirement-review",
        "create",
        "DRAFT-001",
        thread_id=f"T051-{scenario_name}-需求生产",
    )
    requirement_review = _current_request(
        project, stage="requirement_split", owner="DRAFT-001"
    )
    if spec["special"] == "review_identity":
        _submit_passed_review(
            project,
            cli,
                request=requirement_review,
                reviewer=f"T051-{scenario_name}-需求生产",
                expected=1,
        )
    _submit_passed_review(
        project,
        cli,
        request=requirement_review,
        reviewer=f"T051-{scenario_name}-需求独立审核",
    )
    if spec["special"] == "material_drift":
        revised_solution = project / f"{scenario_name}技术方案修订.md"
        revised_solution.write_text(
            solution_source.read_text(encoding="utf-8")
            + "\n到货批次必须保留并可从原始资料核对。\n",
            encoding="utf-8",
        )
        _run(
            project,
            cli,
            "material",
            "DRAFT-001",
            "--type",
            "technical-solution",
            "--title",
            f"{scenario_name}技术方案修订",
            "--file",
            revised_solution.name,
            "--supersedes",
            "MAT-002",
            thread_id=f"T051-{scenario_name}-资料生产",
        )
        _run(
            project,
            cli,
            "draft",
            "requirement-confirm",
            "DRAFT-001",
            "--review",
            str(requirement_review["review_id"]),
            thread_id=f"T051-{scenario_name}-用户确认",
            expected=1,
        )
        _run(
            project,
            cli,
            "draft",
            "requirement-review",
            "create",
            "DRAFT-001",
            thread_id=f"T051-{scenario_name}-需求生产",
        )
        requirement_review = _current_request(
            project, stage="requirement_split", owner="DRAFT-001"
        )
        _submit_passed_review(
            project,
            cli,
            request=requirement_review,
            reviewer=f"T051-{scenario_name}-需求独立复核",
        )
        solution_source = revised_solution
    _run(
        project,
        cli,
        "draft",
        "requirement-confirm",
        "DRAFT-001",
        "--review",
        str(requirement_review["review_id"]),
        thread_id=f"T051-{scenario_name}-用户确认",
    )

    technical_material_id = (
        "MAT-003" if spec["special"] == "material_drift" else "MAT-002"
    )
    reference = write_design_reference(
        project,
        source_text=solution_source.read_text(encoding="utf-8"),
        display_name=f"{scenario_name}技术方案",
        anchor_display_name="适用模块",
        line_start=3,
        line_end=5,
        display_heading="适用模块",
        material_id=technical_material_id,
    )
    _run(
        project,
        cli,
        "design-reference",
        "DRAFT-001",
        "--file",
        reference.name,
        thread_id=f"T051-{scenario_name}-设计生产",
    )
    _run(
        project,
        cli,
        "design-reference-confirm",
        "DRAFT-001",
        "DES-001",
        thread_id=f"T051-{scenario_name}-设计确认",
    )
    (project / "AGENTS.md").write_text("# 项目规则\n\n失败不留半成品。\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (project / "src").mkdir()
    business_code_file = project / str(
        BUSINESS_DETAILS[scenario_name]["code_path"]
    )
    business_code_file.write_text(
        f"BUSINESS = {scenario_name!r}\n",
        encoding="utf-8",
    )
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "建立模块设计代码证据")

    module_types = tuple(str(item) for item in spec["modules"])
    dependency_type = {
        "api": "data",
        "page": "api",
        "component": "page",
        "security": "component",
    }
    modules = []
    for module_type in module_types:
        dependency = dependency_type.get(module_type)
        depends_on = (
            [f"@client:{dependency}-main"]
            if dependency is not None and dependency in module_types
            else []
        )
        module = _module(
            f"{module_type}-main",
            module_type,
            depends_on=depends_on,
            evidence=[business_code_file.relative_to(project).as_posix()],
        )
        module["material_refs"] = [technical_material_id]
        modules.append(module)
    plan_file = project / "输入/设计总计划.json"
    design_plan = _plan(modules)
    design_plan["code_evidence"]["code_files"] = [
        {
            "path": business_code_file.relative_to(project).as_posix(),
            "reason_ref": "FR-001",
        }
    ]
    _write_json(plan_file, design_plan)
    _run(
        project,
        cli,
        "design-plan",
        "DRAFT-001",
        "--file",
        plan_file.relative_to(project).as_posix(),
        thread_id=f"T051-{scenario_name}-设计生产",
    )
    imported_ids = []
    for module_type in module_types:
        artifact_id = MODULE_IDS[module_type]
        dependency = dependency_type.get(module_type)
        depends_on = (
            [MODULE_IDS[dependency]]
            if dependency is not None and dependency in module_types
            else []
        )
        artifact = _artifact(artifact_id, module_type, depends_on=depends_on)
        artifact["material_refs"] = [technical_material_id]
        artifact["content"] = _business_design_content(
            scenario_name,
            module_type,
            depends_on=depends_on,
            technical_material_id=technical_material_id,
        )
        artifact_text = json.dumps(artifact, ensure_ascii=False)
        assert DESIGN_DETAILS[scenario_name]["object"] in artifact_text
        assert all(
            marker not in artifact_text
            for marker in (
                '"users"',
                "读取用户",
                "用户详情",
                "用户摘要卡片",
                "用户数据访问控制",
            )
        )
        artifact_file = project / f"输入/{module_type}-设计.json"
        _write_json(artifact_file, artifact)
        _run(
            project,
            cli,
            "design-artifact",
            "DRAFT-001",
            "--file",
            artifact_file.relative_to(project).as_posix(),
            thread_id=f"T051-{scenario_name}-设计生产",
        )
        imported_ids.append(artifact_id)
    if len(module_types) > 1:
        summary_file = project / "输入/总体设计.json"
        business_summary = _business_design_summary(scenario_name, module_types)
        assert DESIGN_DETAILS[scenario_name]["object"] in json.dumps(
            business_summary,
            ensure_ascii=False,
        )
        _write_json(summary_file, business_summary)
        _run(
            project,
            cli,
            "design-summary",
            "DRAFT-001",
            "--file",
            summary_file.relative_to(project).as_posix(),
            thread_id=f"T051-{scenario_name}-设计生产",
        )

    design_input = ".codex-sdlc/drafts/DRAFT-001/设计/design-plan.v1.json"
    _run(
        project,
        cli,
        "review",
        "create",
        "--review-id",
        "REV-002",
        "--stage",
        "integrated_design",
        "--owner",
        "DRAFT-001",
        "--input",
        design_input,
        thread_id=f"T051-{scenario_name}-设计生产",
    )
    design_review = _current_request(
        project, stage="integrated_design", owner="DRAFT-001"
    )
    _submit_passed_review(
        project,
        cli,
        request=design_review,
        reviewer=f"T051-{scenario_name}-设计独立审核",
    )
    _run(
        project,
        cli,
        "draft",
        "refresh",
        "DRAFT-001",
        thread_id=f"T051-{scenario_name}-设计生产",
    )

    if spec["special"] == "design_drift":
        original_code = business_code_file.read_bytes()
        business_code_file.write_text(
            f"BUSINESS = {scenario_name!r}\nDRIFT = True\n",
            encoding="utf-8",
        )
        drift_formal = project / "输入/formal-代码漂移.json"
        _write_json(drift_formal, _formal_package(project))
        events_before_design_drift = (
            project / ".codex-sdlc/events.jsonl"
        ).read_bytes()
        _run(
            project,
            cli,
            "start",
            "--file",
            drift_formal.relative_to(project).as_posix(),
            thread_id=f"T051-{scenario_name}-正式建档",
            expected=1,
        )
        drifted_design_review = _run(
            project,
            cli,
            "review",
            "status",
            "--review",
            str(design_review["review_id"]),
            thread_id=f"T051-{scenario_name}-设计审核状态",
        )
        assert "stale" in drifted_design_review.stdout
        assert (
            project / ".codex-sdlc/events.jsonl"
        ).read_bytes() == events_before_design_drift
        assert not (project / ".codex-sdlc/requirements/REQ-001").exists()
        business_code_file.write_bytes(original_code)

    formal_file = project / "输入/formal.v3.json"
    _write_json(formal_file, _formal_package(project))
    events = project / ".codex-sdlc/events.jsonl"
    events_before_bad_start = events.read_bytes()
    bad_formal = json.loads(formal_file.read_text(encoding="utf-8"))
    bad_formal["artifact_manifest"] = bad_formal["artifact_manifest"][:-1]
    bad_formal_file = project / "输入/formal-漏项.json"
    _write_json(bad_formal_file, bad_formal)
    _run(
        project,
        cli,
        "start",
        "--file",
        bad_formal_file.relative_to(project).as_posix(),
        thread_id=f"T051-{scenario_name}-正式建档",
        expected=1,
    )
    assert events.read_bytes() == events_before_bad_start
    assert not (project / ".codex-sdlc/requirements/REQ-001").exists()
    _run(
        project,
        cli,
        "start",
        "--file",
        formal_file.relative_to(project).as_posix(),
        thread_id=f"T051-{scenario_name}-正式建档",
    )
    requirement_root = project / ".codex-sdlc/requirements/REQ-001"
    assert requirement_root.is_dir()

    submission = _write_task_submission(project / "输入/任务拆分", requirement_root)
    task_plan_input = json.loads(submission[0].read_text(encoding="utf-8"))
    task_plan_input["code_evidence"]["dependencies"] = ["package-lock.json"]
    task_plan_input["code_evidence"]["code_files"] = [
        {
            "path": str(BUSINESS_DETAILS[scenario_name]["code_path"]),
            "reason_ref": "FR-001",
        }
    ]
    assert task_plan_input["code_evidence"]["code_files"] == design_plan[
        "code_evidence"
    ]["code_files"]
    _write_json(submission[0], task_plan_input)
    task_file = submission[1] / "main.task.v2.json"
    task_input = json.loads(task_file.read_text(encoding="utf-8"))
    _apply_business_task(
        task_input,
        scenario_name=scenario_name,
        goal=str(spec["goal"]),
        requirement=str(spec["requirement"]),
        design_refs=imported_ids,
        technical_material_id=technical_material_id,
    )
    _write_json(task_file, task_input)
    coverage_input = json.loads(submission[2].read_text(encoding="utf-8"))
    coverage_input["design_artifacts"] = {
        artifact_id: {"tasks": ["@client:main"]} for artifact_id in imported_ids
    }
    _write_json(submission[2], coverage_input)
    _run(
        project,
        cli,
        "tasks",
        "REQ-001",
        "--plan-file",
        submission[0].relative_to(project).as_posix(),
        "--tasks-dir",
        submission[1].relative_to(project).as_posix(),
        "--coverage-file",
        submission[2].relative_to(project).as_posix(),
        thread_id=f"T051-{scenario_name}-任务生产",
    )
    # 未审核任务必须由正式入口拒绝，允许补齐辅助输出索引，但不能写事件或运行轮次。
    events_before_unreviewed_task = events.read_bytes()
    runtime_root = requirement_root / "runtime/T-001"
    output_index = requirement_root / "task-outputs/task-output-index.v1.json"
    output_index_before = (
        output_index.read_bytes() if output_index.is_file() else None
    )
    _run(
        project,
        cli,
        "task",
        "REQ-001",
        "T-001",
        thread_id=f"T051-{scenario_name}-未审核开工",
        expected=1,
    )
    assert events.read_bytes() == events_before_unreviewed_task
    assert not runtime_root.exists()
    if output_index_before is not None:
        assert output_index.read_bytes() == output_index_before
    else:
        assert output_index.is_file()

    _run(
        project,
        cli,
        "review",
        "create",
        "--review-id",
        "REV-003",
        "--stage",
        "task_plan",
        "--owner",
        "REQ-001",
        "--input",
        ".codex-sdlc/requirements/REQ-001/tasks/task-plan.v2.json",
        thread_id=f"T051-{scenario_name}-任务生产",
    )
    task_review = _current_request(project, stage="task_plan", owner="REQ-001")
    _submit_passed_review(
        project,
        cli,
        request=task_review,
        reviewer=f"T051-{scenario_name}-任务独立审核",
    )
    _run(project, cli, "task", "REQ-001", "T-001", thread_id="T051-任务开发")
    manifest = requirement_root / "runtime/T-001/runs/0001/task-read-manifest.v1.json"
    _run(
        project,
        cli,
        "task-read-confirm",
        "REQ-001",
        "T-001",
        "--manifest-sha256",
        _sha256(manifest),
        thread_id="T051-任务开发",
    )
    _run(project, cli, "task-run-check", "REQ-001", "T-001", thread_id="T051-任务开发")

    if spec["special"] == "task_restore":
        _complete_task_run(project, cli, requirement_root, run_number=1)
        first_run = requirement_root / "runtime/T-001/runs/0001"
        first_run_hashes = _tree_file_hashes(first_run)
        _run(
            project,
            cli,
            "task-restore",
            "REQ-001",
            "T-001",
            "第一次恢复",
            thread_id="T051-任务恢复",
        )
        first_after_restore = _tree_file_hashes(first_run)
        assert {
            path: first_after_restore[path] for path in first_run_hashes
        } == first_run_hashes
        second_manifest = requirement_root / "runtime/T-001/runs/0002/task-read-manifest.v1.json"
        _run(
            project,
            cli,
            "task-read-confirm",
            "REQ-001",
            "T-001",
            "--manifest-sha256",
            _sha256(second_manifest),
            thread_id="T051-任务恢复",
        )
        _complete_task_run(
            project,
            cli,
            requirement_root,
            run_number=2,
            thread_id="T051-任务恢复",
        )
        second_run = requirement_root / "runtime/T-001/runs/0002"
        completed_run_hashes = {
            "0001": _tree_file_hashes(first_run),
            "0002": _tree_file_hashes(second_run),
        }
        _run(
            project,
            cli,
            "task-restore",
            "REQ-001",
            "T-001",
            "第二次恢复",
            thread_id="T051-任务恢复",
        )
        assert (requirement_root / "runtime/T-001/runs/0003").is_dir()
        for label, run_root in (("0001", first_run), ("0002", second_run)):
            after_restore = _tree_file_hashes(run_root)
            assert {
                path: after_restore[path]
                for path in completed_run_hashes[label]
            } == completed_run_hashes[label]

    change_note = project / f"{scenario_name}变更.md"
    change_note.write_text(f"{scenario_name}增加一条可单独验收的规则。\n", encoding="utf-8")
    create_args = (
        "change-create",
        "REQ-001",
        "--request-key",
        f"t051-{suffix}",
    )
    if spec["special"] == "change_recovery":
        _run(
            project,
            cli,
            *create_args,
            thread_id=f"T051-{scenario_name}-结构化变更生产",
            expected=2,
            extra_env={"CODEX_SDLC_CHANGE_CREATE_INTERRUPT": "after_directory_publish"},
        )
    _run(
        project,
        cli,
        *create_args,
        thread_id=f"T051-{scenario_name}-结构化变更生产",
    )
    _run(
        project,
        cli,
        "change-material",
        "REQ-001",
        "CHG-001",
        "--type",
        "requirement",
        "--file",
        change_note.name,
        thread_id=f"T051-{scenario_name}-结构化变更生产",
    )
    status = json.loads((requirement_root / "changes/CHG-001/status.json").read_text(encoding="utf-8"))
    inputs = _change_inputs(project, requirement_root, status)
    change_root = project / "输入/结构化变更"
    for name, document in inputs.items():
        _write_json(change_root / name, document)
    success_root = change_root
    if spec["special"] == "change_recovery":
        # 替换和废弃必须经过 CLI 独立重算，不能沿用“新增资料”的预计结果。
        lifecycle_inputs = _lifecycle_change_inputs(
            requirement_root,
            status,
            inputs,
        )
        lifecycle = lifecycle_inputs["change-package.v1.json"]
        lifecycle_file = change_root / "change-package-替换并废弃.json"
        _write_json(lifecycle_file, lifecycle)
        lifecycle_args = list(_change_command_args(project, change_root))
        lifecycle_args[4] = lifecycle_file.relative_to(project).as_posix()
        lifecycle_rejected = _run(
            project,
            cli,
            *lifecycle_args,
            thread_id=f"T051-{scenario_name}-结构化变更生产",
            expected=2,
        )
        assert "预计结果" in lifecycle_rejected.stderr

        # 活动任务保护在正确预计版本提交前独立检查。
        blocked = deepcopy(inputs["change-package.v1.json"])
        blocked["task_impacts"] = {
            "restore": [],
            "add": [],
            "close": [
                {
                    "task_id": "T-001",
                    "reason": "错误关闭仍在执行的任务",
                    "replacement_refs": [],
                }
            ],
            "unaffected": [],
        }
        blocked_file = change_root / "change-package-错误关闭.json"
        _write_json(blocked_file, blocked)
        blocked_args = list(_change_command_args(project, change_root))
        blocked_args[4] = blocked_file.relative_to(project).as_posix()
        rejected = _run(
            project,
            cli,
            *blocked_args,
            thread_id=f"T051-{scenario_name}-结构化变更生产",
            expected=2,
        )
        assert "任务" in rejected.stderr

        success_root = project / "输入/结构化变更-替换废弃"
        for name, document in lifecycle_inputs.items():
            _write_json(success_root / name, document)
        _run(
            project,
            cli,
            *_change_command_args(project, success_root),
            thread_id=f"T051-{scenario_name}-结构化变更生产",
            expected=2,
            extra_env={"CODEX_SDLC_CHANGE_PACKAGE_INTERRUPT": "after_files_publish"},
        )
    _run(
        project,
        cli,
        *_change_command_args(project, success_root),
        thread_id=f"T051-{scenario_name}-结构化变更生产",
    )
    if spec["special"] == "change_recovery":
        workspace = project / str(status["workspace_path"])
        committed_package = json.loads(
            (workspace / "change-package.v1.json").read_text(encoding="utf-8")
        )
        material_operations = committed_package["material_operations"]
        assert [item["operation"] for item in material_operations] == [
            "replace",
            "deprecate",
        ]
        assert material_operations[0]["target_id"] == "MAT-001"
        assert material_operations[1]["target_id"] == "MAT-002"
        assert material_operations[1]["replacement_refs"] == ["MAT-001"]
        for filename in (
            "projected-requirement.v2.json",
            "projected-design.v2.json",
            "projected-test-matrix.v2.json",
            "projected-reference-index.v2.json",
            "projected-task-plan.v2.json",
        ):
            committed = json.loads(
                (workspace / filename).read_text(encoding="utf-8")
            )
            assert committed["content"] == lifecycle_inputs[filename]["content"]
        committed_reference = json.loads(
            (workspace / "projected-reference-index.v2.json").read_text(
                encoding="utf-8"
            )
        )["content"]["entries"]
        assert committed_reference["MAT-001"]["path"].endswith(
            "/原始资料/CMAT-001"
        )
        base_mat_002 = json.loads(
            (requirement_root / "reference-index.v1.json").read_text(
                encoding="utf-8"
            )
        )["entries"]["MAT-002"]
        assert {
            key: committed_reference["MAT-002"][key]
            for key in ("schema_version", "path", "sha256", "locator")
        } == base_mat_002
        assert committed_reference["MAT-002"]["lifecycle"] == {
            "status": "deprecated",
            "change_id": "CHG-001",
            "reason": "技术资料由结构化变更中的正式资料接替",
            "replacement_refs": ["MAT-001"],
        }
        committed_task = json.loads(
            (workspace / "projected-task-plan.v2.json").read_text(
                encoding="utf-8"
            )
        )["content"]
        assert committed_task == lifecycle_inputs[
            "projected-task-plan.v2.json"
        ]["content"]

        # 缺少受影响审核时保护动作必须零残留，三类审核通过后再保护活动轮次。
        protection = workspace / "change-protection.v1.json"
        events_before_protection_rejection = events.read_bytes()
        _run(
            project,
            cli,
            "change-protect",
            "REQ-001",
            "CHG-001",
            "--confirm-requirement",
            thread_id=f"T051-{scenario_name}-变更保护",
            expected=1,
        )
        assert events.read_bytes() == events_before_protection_rejection
        assert not protection.exists()
        change_reviews = []
        for index, stage in enumerate(
            ("requirement_split", "integrated_design", "task_plan"),
            start=1,
        ):
            _run(
                project,
                cli,
                "review",
                "create",
                "--review-id",
                f"REV-{100 + index}",
                "--stage",
                stage,
                "--owner",
                "CHG-001",
                "--input",
                str(status["workspace_path"]) + "/change-package.v1.json",
                thread_id=f"T051-{scenario_name}-变更生产",
            )
            change_review = _current_request(
                project,
                stage=stage,
                owner="CHG-001",
            )
            _submit_passed_review(
                project,
                cli,
                request=change_review,
                reviewer=f"T051-{scenario_name}-变更独立审核-{index}",
            )
            change_reviews.append(change_review)
        assert [item["stage"] for item in change_reviews] == [
            "requirement_split",
            "integrated_design",
            "task_plan",
        ]
        _run(
            project,
            cli,
            "change-protect",
            "REQ-001",
            "CHG-001",
            "--confirm-requirement",
            thread_id=f"T051-{scenario_name}-变更保护",
        )
        assert protection.is_file()
        protected_run = json.loads(
            (
                requirement_root
                / "runtime/T-001/runs/0001/task-run.v1.json"
            ).read_text(encoding="utf-8")
        )
        assert protected_run["status"] == "stale"

        # 生效事件写入后模拟进程退出，正式只读入口恢复事务，再幂等确认一次。
        _run(
            project,
            cli,
            "change-accept",
            "REQ-001",
            "CHG-001",
            thread_id=f"T051-{scenario_name}-变更生效",
            expected=86,
            extra_env={
                "CODEX_SDLC_CHANGE_ACCEPT_INTERRUPT": "after_change_event_append",
                "CODEX_SDLC_CHANGE_ACCEPT_INTERRUPT_MODE": "process_exit",
            },
        )
        _run(
            project,
            cli,
            "status",
            thread_id=f"T051-{scenario_name}-变更恢复",
        )
        accepted = _run(
            project,
            cli,
            "change-accept",
            "REQ-001",
            "CHG-001",
            thread_id=f"T051-{scenario_name}-变更生效",
        )
        assert "幂等重试：是" in accepted.stdout
        for filename in (
            "requirement.v2.json",
            "design.v2.json",
            "test-matrix.v2.json",
            "reference-index.v2.json",
            "task-plan.v2.json",
        ):
            assert (requirement_root / "versions" / filename).is_file()
        assert json.loads(
            (requirement_root / "tasks/task-plan.v2.json").read_text(
                encoding="utf-8"
            )
        ) == lifecycle_inputs["projected-task-plan.v2.json"]["content"]
        current_mat_002 = json.loads(
            (requirement_root / "reference-index.v1.json").read_text(
                encoding="utf-8"
            )
        )["entries"]["MAT-002"]
        assert current_mat_002 == lifecycle_inputs[
            "projected-reference-index.v2.json"
        ]["content"]["entries"]["MAT-002"]
        original_task_review = _run(
            project,
            cli,
            "review",
            "status",
            "--review",
            str(task_review["review_id"]),
            thread_id=f"T051-{scenario_name}-审核状态",
        )
        assert "stale" in original_task_review.stdout

    requests = [requirement_review, design_review, task_review]
    assert [item["stage"] for item in requests] == [
        "requirement_split",
        "integrated_design",
        "task_plan",
    ]
    assert len({item["producer_run_id"] for item in requests}) == 3
    assert all(item["input_hashes"] for item in requests)
    transaction_residuals = [
        path
        for path in project.rglob("*")
        if path.is_file()
        and (
            ".projection-staging" in path.parts
            or ".projection-transactions" in path.parts
            or "accept-active" in path.parts
            or "accept-staging" in path.parts
        )
    ]
    assert transaction_residuals == []
    shutil.rmtree(project)
    assert not project.exists()
