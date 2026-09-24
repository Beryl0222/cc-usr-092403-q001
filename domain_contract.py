"""领域契约：把借展业务规则固定为可执行的验收条件。

覆盖：
- 十六位作者长卷的区段、贡献与合作顺序；
- 协议条款（展期/展厅/照度/运输/保险/数字传播）；
- 五种交接的双方签认与生命周期顺序；
- 重复扫码幂等；
- 损伤即冻结、前后图像哈希保全；冻结由未解除事件派生，
  解除须登记复核责任人，重复解除幂等，多个事件可乱序处理；
- 展签发布即锁定证据快照，学术更正只另起新版；
- 跨馆改期与局部状态争议下，从展签/区段定位实体、保管、授权与风险；
- 状态快照在服务重启后恢复未解除风险与交接进度。
"""

import json
import threading
import unittest

from domain import (
    CONTRIBUTION_KINDS,
    ConflictError,
    DomainError,
    LoanRegistry,
)


def make_long_scroll(registry: LoanRegistry) -> dict:
    """一件含十六位作者、四个区段的长卷。"""
    segment_ids = ["SEG-A", "SEG-B", "SEG-C", "SEG-D"]
    segments = [
        {"segment_id": "SEG-A", "label": "引首", "start_cm": 0, "end_cm": 80, "note": "书引首与题签"},
        {"segment_id": "SEG-B", "label": "画心甲", "start_cm": 80, "end_cm": 360},
        {"segment_id": "SEG-C", "label": "画心乙", "start_cm": 360, "end_cm": 640},
        {"segment_id": "SEG-D", "label": "尾纸题跋", "start_cm": 640, "end_cm": 900},
    ]
    authors = [
        ("赵某", "作画"), ("钱某", "作画"), ("孙某", "作画"), ("李某", "作画"),
        ("周某", "作画"), ("吴某", "作画"), ("郑某", "作画"), ("王某", "作画"),
        ("冯某", "作画"), ("陈某", "作画"), ("褚某", "作画"), ("卫某", "作画"),
        ("蒋某", "题跋"), ("沈某", "题跋"), ("韩某", "书引首"), ("杨某", "题签"),
    ]
    seg_for_order = [
        "SEG-C", "SEG-B", "SEG-B", "SEG-B",
        "SEG-B", "SEG-C", "SEG-C", "SEG-C",
        "SEG-C", "SEG-C", "SEG-B", "SEG-B",
        "SEG-D", "SEG-D", "SEG-A", "SEG-A",
    ]
    contributions = [
        {"author": a, "kind": k, "order": i + 1, "segment_id": seg_for_order[i]}
        for i, (a, k) in enumerate(authors)
    ]
    return registry.register_work(
        title="百年会面图卷", kind="长卷", owner_org="甲馆",
        segments=segments, contributions=contributions,
    )


def agreement_payload(work_id: str, **overrides) -> dict:
    payload = {
        "work_id": work_id,
        "lender_org": "甲馆",
        "borrower_org": "乙馆",
        "start_on": "2026-10-01",
        "end_on": "2026-12-31",
        "gallery": "三号厅",
        "max_lux": 50,
        "transport": {"mode": "专车恒温", "escort": "随馆馆员", "temp_c": (18, 22)},
        "insurance": {"insured_value": "议定价值", "coverage": "钉到钉", "policy": "POL-001"},
        "digital_rights": {"web": True, "social_media": False, "print_catalog": True, "term": "展期内"},
    }
    payload.update(overrides)
    return payload


def handover_payload(work_id: str, htype: str, scan: str, on_date: str,
                     from_person="出库员甲", to_person="接收员乙",
                     location="甲馆库房", report=None, linked_segments=None) -> dict:
    pairs = {
        "出库": ("出借馆", "运输方", "甲馆", "长风运输"),
        "到馆": ("运输方", "承借馆", "长风运输", "乙馆"),
        "布展": ("承借馆", "承借馆", "乙馆", "乙馆"),
        "撤展": ("承借馆", "运输方", "乙馆", "长风运输"),
        "归还": ("运输方", "出借馆", "长风运输", "甲馆"),
    }
    fr, tr, forg, torg = pairs[htype]
    return {
        "work_id": work_id,
        "type": htype,
        "scan_code": scan,
        "on_date": on_date,
        "at_location": location,
        "from_party": {"org": forg, "role": fr, "person": from_person},
        "to_party": {"org": torg, "role": tr, "person": to_person},
        "report": report or {"condition": "良好", "image_hashes": ["出库全景照"]},
        "linked_segments": linked_segments or [],
    }


class CompositeWorkTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.view = make_long_scroll(self.registry)

    def test_scroll_has_sixteen_authors_in_order_with_segments(self):
        self.assertEqual(len(self.view["contributions"]), 16)
        self.assertEqual([c["order"] for c in self.view["contributions"]], list(range(1, 17)))
        self.assertEqual(len({c["contribution_id"] for c in self.view["contributions"]}), 16)
        self.assertEqual(self.view["segments"][3]["label"], "尾纸题跋")
        by_order = {c["order"]: c for c in self.view["contributions"]}
        self.assertEqual(by_order[13]["kind"], "题跋")
        self.assertEqual(by_order[16]["kind"], "题签")
        # 每位作者都定位到具体区段，而不是只挂在整件作品上。
        self.assertTrue(all(c["segment_id"] for c in self.view["contributions"]))

    def test_duplicate_collaboration_order_rejected(self):
        with self.assertRaises(DomainError):
            self.registry.register_work(
                title="合作山水", kind="合作画", owner_org="甲馆",
                contributions=[
                    {"author": "甲", "kind": "作画", "order": 1},
                    {"author": "乙", "kind": "作画", "order": 1},
                ],
            )

    def test_contribution_must_reference_known_segment(self):
        with self.assertRaises(DomainError):
            self.registry.register_work(
                title="册页", kind="独立作品", owner_org="甲馆",
                segments=[{"label": "扉页"}],
                contributions=[{"author": "甲", "kind": "题跋", "order": 1, "segment_id": "seg-ghost"}],
            )

    def test_independent_work_and_catalog_have_their_own_kinds(self):
        painting = self.registry.register_work("独钓图", "独立作品", "丙馆")
        catalog = self.registry.register_work("百年前展场图录", "历史图录", "丁馆")
        self.assertEqual(painting["work"]["kind"], "独立作品")
        self.assertEqual(catalog["work"]["kind"], "历史图录")


class AgreementTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.work = self.registry.register_work("松鹤图", "合作画", "甲馆")
        self.work_id = self.work["work"]["work_id"]

    def test_agreement_constraints_are_stored_and_exposed(self):
        view = self.registry.create_agreement(agreement_payload(self.work_id))
        self.assertEqual(view["gallery"], "三号厅")
        self.assertEqual(view["max_lux"], 50)
        self.assertFalse(view["digital_rights"]["social_media"])
        self.assertEqual(view["insurance"]["coverage"], "钉到钉")
        risk = self.registry.risk_view(self.work_id)
        self.assertEqual(risk["authorization"]["exhibition_period"],
                         {"start": "2026-10-01", "end": "2026-12-31"})

    def test_invalid_period_and_lux_rejected(self):
        with self.assertRaises(DomainError):
            self.registry.create_agreement(
                agreement_payload(self.work_id, start_on="2027-01-01", end_on="2026-12-31"))
        with self.assertRaises(DomainError):
            self.registry.create_agreement(agreement_payload(self.work_id, max_lux=0))

    def test_cross_museum_reschedule_creates_new_version_and_keeps_old(self):
        old = self.registry.create_agreement(agreement_payload(self.work_id))
        new = self.registry.reschedule_agreement(
            old["agreement_id"],
            {"start_on": "2026-11-15", "end_on": "2027-02-15", "gallery": "五号厅"},
        )
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["supersedes"], old["agreement_id"])
        self.assertEqual(new["gallery"], "五号厅")
        # 旧版条款原样可查。
        self.assertEqual(self.registry.agreements[old["agreement_id"]].gallery, "三号厅")
        work_view = self.registry.get_work_view(self.work_id)
        self.assertEqual(work_view["current_agreement"], new["agreement_id"])
        self.assertEqual(len(work_view["agreement_versions"]), 2)


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("溪山行旅", "独立作品", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))

    def test_full_chain_requires_both_signatures_and_moves_custody(self):
        chain = [
            ("出库", "SCAN-1", "2026-09-25", "甲馆库房"),
            ("到馆", "SCAN-2", "2026-09-27", "乙馆收货区"),
            ("布展", "SCAN-3", "2026-09-30", "乙馆三号厅"),
            ("撤展", "SCAN-4", "2027-01-05", "乙馆三号厅"),
            ("归还", "SCAN-5", "2027-01-07", "甲馆库房"),
        ]
        for htype, scan, day, location in chain:
            view = self.registry.record_handover(
                handover_payload(self.work_id, htype, scan, day, location=location))
            self.assertTrue(view["signed_by_both"])
        custody = self.registry.get_work_view(self.work_id)["custody"]
        self.assertEqual(custody["status"], "已归还")
        self.assertEqual(custody["custodian_role"], "出借馆")

    def test_missing_signature_rejected(self):
        payload = handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25")
        payload["to_party"]["person"] = ""
        with self.assertRaises(DomainError):
            self.registry.record_handover(payload)

    def test_wrong_party_role_rejected(self):
        payload = handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25")
        payload["to_party"]["role"] = "承借馆"
        with self.assertRaises(DomainError):
            self.registry.record_handover(payload)

    def test_skip_step_rejected(self):
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-X", "2026-09-30"))

    def test_duplicate_scan_cannot_create_second_handover(self):
        first = self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-DUP", "2026-09-25"))
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "到馆", "SCAN-DUP", "2026-09-27"))
        # 第一次交接仍然有效，生命周期停在出库之后。
        self.assertEqual(self.registry.get_work_view(self.work_id)["custody"]["status"], "运输中")
        # 被拒绝的重复扫码没有消耗下一步——合法的到馆仍可办理。
        self.registry.record_handover(
            handover_payload(self.work_id, "到馆", "SCAN-OK", "2026-09-27"))
        # 第一次交接记录未被重复扫码覆盖或复制。
        self.assertTrue(first["handover_id"].startswith("handover-"))
        self.assertEqual(
            [h.handover_id for h in self.registry.handovers if h.scan_code == "SCAN-DUP"],
            [first["handover_id"]],
        )

    def test_rejected_scan_before_freeze_is_not_consumed(self):
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))
        # 错序办理“归还”应失败，且该扫码之后仍可用于它真正对应的步骤。
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "归还", "SCAN-RETRY", "2026-09-26"))
        self.registry.record_handover(
            handover_payload(self.work_id, "到馆", "SCAN-2", "2026-09-27"))
        self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
        self.registry.record_handover(
            handover_payload(self.work_id, "撤展", "SCAN-4", "2027-01-05"))
        done = self.registry.record_handover(
            handover_payload(self.work_id, "归还", "SCAN-RETRY", "2027-01-07"))
        self.assertEqual(done["type"], "归还")


class DamageAndFreezeTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("秋林群鹿", "独立作品", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))

    def test_damage_freezes_all_following_handovers_and_preserves_hashes(self):
        before = "a" * 64
        after = "b" * 64
        damaged = self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={
                "condition": "损伤",
                "damage_note": "画心左下角发现新增折痕",
                "image_hashes": ["到馆检视照"],
                "before_hashes": [before],
                "after_hashes": [after],
            },
        ))
        self.assertTrue(damaged["frozen"])
        self.assertEqual(damaged["condition"]["before_hashes"], [before])
        self.assertEqual(damaged["condition"]["after_hashes"], [after])
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])

        # 冻结后任何后续交接都不得成立，换一个新扫码也不行。
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
        risk = self.registry.risk_view(self.work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["open_risks"]), 1)
        self.assertEqual(risk["open_risks"][0]["before_hashes"], [before])

    def test_damage_report_requires_note(self):
        with self.assertRaises(DomainError):
            self.registry.record_handover(handover_payload(
                self.work_id, "到馆", "SCAN-2", "2026-09-27",
                report={"condition": "损伤", "image_hashes": ["x"]},
            ))

    def test_incident_can_be_resolved_then_chain_resumes(self):
        self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "边缘轻微磨损",
                    "before_hashes": ["c" * 64], "after_hashes": ["d" * 64]},
        ))
        incident = self.registry.risk_view(self.work_id)["open_risks"][0]
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(incident["incident_id"], "  ")
        result = self.registry.resolve_incident(
            incident["incident_id"], "修复师与双方馆员复核，确认可继续展出",
            reviewed_by="复核员甲",
        )
        self.assertTrue(result["resolved"])
        self.assertFalse(result["frozen"])
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])
        self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))


class LabelSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.scroll = make_long_scroll(self.registry)
        self.work_id = self.scroll["work"]["work_id"]
        self.segment_ids = [s["segment_id"] for s in self.scroll["segments"]]
        agreement = self.registry.create_agreement(agreement_payload(self.work_id))
        self.agreement_id = agreement["agreement_id"]

    def test_published_label_locks_evidence_and_corrections_do_not_alter_old_version(self):
        citations = [{"ref": self.segment_ids[1], "note": "画心甲的合笔位置"}]
        self.registry.create_label(self.work_id, "百年会面：两位画家的合作见证", citations)
        published = self.registry.publish_label(self.work_id, "2026-10-01")
        self.assertTrue(published["frozen"])
        snapshot = published["evidence_snapshot"]
        self.assertEqual(len(snapshot["contributions"]), 16)
        self.assertEqual(snapshot["agreement_version"]["version"], 1)
        self.assertEqual(snapshot["custody"]["status"], "在库")

        # 发布后发生学术更正与跨馆改期。
        self.registry.reschedule_agreement(
            self.agreement_id, {"gallery": "七号厅", "start_on": "2026-11-01"})
        corrected = self.registry.correct_label(
            self.work_id, "百年会面：据新发现信札修订合笔顺序",
            [{"ref": self.segment_ids[2], "note": "画心乙主笔改订"}],
        )
        self.assertEqual(corrected["version"], 2)
        self.assertFalse(corrected["frozen"])

        # 旧版展签与证据快照保持发布时的内容。
        old = self.registry.label_version(self.work_id, 1)
        self.assertEqual(old["status"], "已发布")
        self.assertEqual(old["evidence_snapshot"]["agreement_version"]["gallery"], "三号厅")
        self.assertEqual(old["narrative"], "百年会面：两位画家的合作见证")
        self.assertEqual(len(old["evidence_snapshot"]["contributions"]), 16)

        # 新版发布后才锁定当时的新证据。
        new_published = self.registry.publish_label(self.work_id, "2026-11-01")
        self.assertEqual(new_published["evidence_snapshot"]["agreement_version"]["gallery"], "七号厅")
        # 最新版默认查询，旧版仍可按版本号取回。
        self.assertEqual(self.registry.label_version(self.work_id)["version"], 2)

    def test_cannot_publish_same_version_twice(self):
        self.registry.create_label(self.work_id, "展签草稿", [])
        self.registry.publish_label(self.work_id, "2026-10-01")
        with self.assertRaises(ConflictError):
            self.registry.publish_label(self.work_id, "2026-10-02")

    def test_damage_appears_as_open_risk_in_snapshot(self):
        self.registry.create_label(self.work_id, "有局部争议的长卷", [])
        # 出库即发现画心乙局部问题。
        self.registry.record_handover(handover_payload(
            self.work_id, "出库", "SCAN-1", "2026-09-25",
            linked_segments=[self.segment_ids[2]],
            report={"condition": "损伤", "damage_note": "画心乙疑似新霉点",
                    "before_hashes": ["e" * 64], "after_hashes": ["f" * 64]},
        ))
        published = self.registry.publish_label(self.work_id, "2026-09-26")
        risks = published["evidence_snapshot"]["open_risks"]
        self.assertEqual(len(risks), 1)
        self.assertIn("霉点", risks[0]["note"])
        self.assertTrue(published["evidence_snapshot"]["custody"])


class SegmentDisputeTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.scroll = make_long_scroll(self.registry)
        self.work_id = self.scroll["work"]["work_id"]
        self.segments = {s["label"]: s["segment_id"] for s in self.scroll["segments"]}
        self.registry.create_agreement(agreement_payload(self.work_id))

    def test_locate_segment_reaches_work_contributions_custody_and_risk(self):
        seg_b = self.segments["画心乙"]
        self.registry.record_handover(handover_payload(
            self.work_id, "出库", "SCAN-1", "2026-09-25",
            linked_segments=[seg_b],
            report={"condition": "损伤", "damage_note": "画心乙局部水渍成因存疑",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        located = self.registry.locate_segment(self.work_id, seg_b)
        # 从争议区段可回到实体。
        self.assertEqual(located["work"]["work_id"], self.work_id)
        # 可定位到该区段上的作者贡献（画心乙上有多位作画者）。
        self.assertTrue(located["contributions"])
        self.assertTrue({c["author"] for c in located["contributions"]} & {"赵某", "吴某"})
        self.assertTrue(all(c["kind"] == "作画" for c in located["contributions"]))
        self.assertEqual(
            [c["order"] for c in located["contributions"]],
            sorted(c["order"] for c in located["contributions"]),
        )
        # 当前保管责任明确（出库后由运输方承担）。
        self.assertEqual(located["custody"]["custodian_role"], "运输方")
        # 风险未解除且处于冻结。
        self.assertTrue(located["frozen"])
        self.assertEqual(len(located["segment_incidents"]), 1)
        self.assertFalse(located["segment_incidents"][0]["resolved"])

    def test_risk_view_carries_authorization_scope_during_dispute(self):
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))
        risk = self.registry.risk_view(self.work_id)
        self.assertEqual(risk["authorization"]["digital_rights"]["term"], "展期内")
        self.assertEqual(risk["custody"]["custodian_role"], "运输方")
        self.registry.reschedule_agreement(
            risk["authorization"]["agreement_id"], {"max_lux": 30})
        risk2 = self.registry.risk_view(self.work_id)
        self.assertEqual(risk2["authorization"]["max_lux"], 30)
        self.assertEqual(risk2["authorization"]["version"], 2)


class MultiIncidentFreezeTest(unittest.TestCase):
    """损伤事件与作品冻结的关联：冻结由未解除事件派生，解除可乱序、可重试。"""

    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("寒林重汀", "独立作品", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.outbound = self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))

    def _damaged_handover(self, htype, scan, day, note):
        return self.registry.record_handover(handover_payload(
            self.work_id, htype, scan, day,
            report={"condition": "损伤", "damage_note": note,
                    "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
        ))

    def _late_incident(self, note="复核出库照片时发现第二处旧伤"):
        return self.registry.register_incident({
            "work_id": self.work_id,
            "handover_id": self.outbound["handover_id"],
            "on_date": "2026-09-28",
            "note": note,
            "before_hashes": ["c" * 64],
            "after_hashes": ["d" * 64],
        })

    def test_consecutive_redamage_freezes_again(self):
        """连续复损：第一次解除后交接恢复，第二次损伤重新冻结并继续阻断。"""
        first = self._damaged_handover("到馆", "SCAN-2", "2026-09-27", "第一处折痕")
        self.registry.resolve_incident(
            first["incident_id"], "第一处修复完成", reviewed_by="复核员甲")
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])

        second = self._damaged_handover("布展", "SCAN-3", "2026-09-30", "第二处霉斑")
        self.assertTrue(second["frozen"])
        view = self.registry.get_work_view(self.work_id)
        self.assertTrue(view["frozen"])
        self.assertEqual(view["open_incidents"], [second["incident_id"]])
        risk = self.registry.risk_view(self.work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual([r["incident_id"] for r in risk["open_risks"]],
                         [second["incident_id"]])
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "撤展", "SCAN-4", "2027-01-05"))

    def test_re_resolving_old_incident_is_idempotent_and_keeps_freeze(self):
        """旧事件重复解除：幂等返回原记录，不撤掉仍开放事件维持的冻结。"""
        first = self._damaged_handover("到馆", "SCAN-2", "2026-09-27", "第一处折痕")
        self.registry.resolve_incident(
            first["incident_id"], "第一处修复完成", reviewed_by="复核员甲")
        second = self._damaged_handover("布展", "SCAN-3", "2026-09-30", "第二处霉斑，仍在复核")

        # 工作人员误点第一次事件的解除：幂等返回，不改动任何状态。
        again = self.registry.resolve_incident(first["incident_id"], "误点重复提交")
        self.assertTrue(again["resolved"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["resolution_note"], "第一处修复完成")
        self.assertEqual(again["reviewed_by"], "复核员甲")
        self.assertTrue(again["frozen"])
        self.assertEqual(again["open_incidents"], [second["incident_id"]])

        # 作品视图与风险视图一致：第二次损伤仍开放，交接继续被阻断。
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])
        risk = self.registry.risk_view(self.work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["open_risks"]), 1)
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "撤展", "SCAN-4", "2027-01-05"))

    def test_out_of_order_resolution_unfreezes_only_after_last_open_incident(self):
        """乱序解除：先解除后登记的事件，最后一项风险关闭才解冻并恢复交接。"""
        damaged = self._damaged_handover("到馆", "SCAN-2", "2026-09-27", "交接时发现第一处损伤")
        first_id = damaged["incident_id"]
        late = self._late_incident()
        second_id = late["incident_id"]
        self.assertTrue(late["frozen"])
        self.assertEqual(len(self.registry.risk_view(self.work_id)["open_risks"]), 2)

        # 乱序：先解除后登记的第二处，第一处未解除，冻结必须保持。
        step1 = self.registry.resolve_incident(
            second_id, "第二处认定为旧伤，不影响展出", reviewed_by="复核员乙")
        self.assertTrue(step1["frozen"])
        self.assertEqual(step1["open_incidents"], [first_id])
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))

        # 最后一项风险关闭：冻结解除，被阻断期间拒绝过的扫码可重新使用。
        step2 = self.registry.resolve_incident(
            first_id, "第一处修复完成，双方确认", reviewed_by="复核员甲")
        self.assertFalse(step2["frozen"])
        self.assertEqual(step2["open_incidents"], [])
        self.assertEqual(self.registry.risk_view(self.work_id)["open_risks"], [])
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])
        self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))

    def test_resolution_validates_work_state_and_reviewer(self):
        """解除记录校验：作品须匹配、须登记复核责任人、未解除前状态保持。"""
        damaged = self._damaged_handover("到馆", "SCAN-2", "2026-09-27", "画心损伤")
        incident_id = damaged["incident_id"]
        other = self.registry.register_work("另一件", "独立作品", "甲馆")

        # 校验作品：解除记录指向其他作品时拒绝。
        with self.assertRaises(ConflictError):
            self.registry.resolve_incident(
                incident_id, "复核通过", reviewed_by="复核员甲",
                expected_work_id=other["work"]["work_id"])
        # 校验复核责任：必须登记复核责任人。
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(incident_id, "复核通过", reviewed_by="  ")
        # 校验当前状态：上述失败均未改变冻结。
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])

        done = self.registry.resolve_incident(
            incident_id, "复核通过", reviewed_by="复核员甲",
            expected_work_id=self.work_id, reviewed_on="2026-09-29")
        self.assertTrue(done["resolved"])
        self.assertEqual(done["reviewed_by"], "复核员甲")
        self.assertEqual(done["resolved_on"], "2026-09-29")
        # 不存在的损伤事件仍然报错。
        with self.assertRaises(DomainError):
            self.registry.resolve_incident("incident-9999", "复核通过", reviewed_by="复核员甲")

    def test_late_incident_must_reference_handover_of_same_work(self):
        other = self.registry.register_work("另一件", "独立作品", "甲馆")
        with self.assertRaises(DomainError):
            self.registry.register_incident({
                "work_id": other["work"]["work_id"],
                "handover_id": self.outbound["handover_id"],
                "note": "挂错作品的损伤",
            })
        with self.assertRaises(DomainError):
            self.registry.register_incident({
                "work_id": self.work_id, "handover_id": "handover-9999", "note": "无交接",
            })

    def test_concurrent_resolves_and_scans_do_not_overwrite(self):
        """并发：同一事件重复解除幂等，不同事件乱序解除互不覆盖，同码交接只成立一次。"""
        damaged = self._damaged_handover("到馆", "SCAN-2", "2026-09-27", "第一处损伤")
        first_id = damaged["incident_id"]
        second_id = self._late_incident()["incident_id"]

        results, errors = [], []
        barrier = threading.Barrier(6)

        def resolve(iid, note):
            try:
                barrier.wait(timeout=5)
                results.append(self.registry.resolve_incident(iid, note, reviewed_by="复核员"))
            except Exception as error:  # noqa: BLE001 - 测试需收集一切并发异常
                errors.append(error)

        threads = [threading.Thread(target=resolve, args=(first_id, f"结论-{i}")) for i in range(3)]
        threads += [threading.Thread(target=resolve, args=(second_id, f"结论-{i}")) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        # 每个事件只解除一次：并发响应一致地返回同一份解除记录。
        for incident_id in (first_id, second_id):
            notes = {r["resolution_note"] for r in results if r["incident_id"] == incident_id}
            self.assertEqual(len(notes), 1)
        # 两个事件都已解除，冻结撤掉。
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])

        # 并发提交同一扫码的下一步交接：只有一次成立，其余 409。
        outcomes = []

        def attempt_handover():
            try:
                self.registry.record_handover(
                    handover_payload(self.work_id, "布展", "SCAN-RACE", "2026-09-30"))
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        racers = [threading.Thread(target=attempt_handover) for _ in range(5)]
        for thread in racers:
            thread.start()
        for thread in racers:
            thread.join(timeout=10)
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 4)
        self.assertEqual(
            len([h for h in self.registry.handovers if h.scan_code == "SCAN-RACE"]), 1)

    def test_snapshot_restore_preserves_open_incidents_and_chain(self):
        """服务重启：快照恢复后未解除事件继续阻断，扫码去重与交接进度保留。"""
        damaged = self._damaged_handover("到馆", "SCAN-2", "2026-09-27", "第一处损伤")
        first_id = damaged["incident_id"]
        second_id = self._late_incident()["incident_id"]

        # 经 JSON 往返恢复，与服务落盘格式一致。
        restored = LoanRegistry.restore(json.loads(json.dumps(self.registry.snapshot())))

        view = restored.get_work_view(self.work_id)
        self.assertTrue(view["frozen"])
        self.assertEqual(set(view["open_incidents"]), {first_id, second_id})
        self.assertEqual(len(restored.risk_view(self.work_id)["open_risks"]), 2)
        # 交接进度与保管状态恢复。
        self.assertEqual(view["custody"]["status"], "待布展")
        # 重复扫码仍然 409。
        with self.assertRaises(ConflictError):
            restored.record_handover(
                handover_payload(self.work_id, "到馆", "SCAN-2", "2026-09-27"))
        # 冻结中的下一步交接仍被阻断。
        with self.assertRaises(ConflictError):
            restored.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))

        # 乱序解除在恢复后仍然有效，最后一项关闭后交接继续。
        restored.resolve_incident(second_id, "第二处结论", reviewed_by="复核员乙")
        self.assertTrue(restored.get_work_view(self.work_id)["frozen"])
        restored.resolve_incident(first_id, "第一处结论", reviewed_by="复核员甲")
        self.assertFalse(restored.get_work_view(self.work_id)["frozen"])
        restored.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))

        # 恢复后新记录编号不与快照中的既有记录冲突。
        again = restored.register_work("重启后登记", "独立作品", "甲馆")
        self.assertNotIn(again["work"]["work_id"], {self.work_id})
        new_incident = restored.register_incident({
            "work_id": self.work_id,
            "handover_id": self.outbound["handover_id"],
            "note": "重启后补登记的损伤",
        })
        self.assertNotIn(new_incident["incident_id"], {first_id, second_id})


if __name__ == "__main__":
    unittest.main()
