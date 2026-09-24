"""领域契约：把借展业务规则固定为可执行的验收条件。

覆盖：
- 十六位作者长卷的区段、贡献与合作顺序；
- 协议条款（展期/展厅/照度/运输/保险/数字传播）；
- 五种交接的双方签认与生命周期顺序；
- 重复扫码幂等；
- 损伤即冻结、前后图像哈希保全；
- 展签发布即锁定证据快照，学术更正只另起新版；
- 跨馆改期与局部状态争议下，从展签/区段定位实体、保管、授权与风险。
"""

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
        reviewer = {"org": "甲馆", "role": "出借馆", "person": "馆员周甲"}
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(incident["incident_id"], "  ", reviewer)
        # 解除记录必须带复核责任签认。
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(incident["incident_id"], "修复师与双方馆员复核，确认可继续展出")
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(
                incident["incident_id"], "修复师与双方馆员复核，确认可继续展出",
                {"org": "长风运输", "role": "运输方", "person": "司机丁"},
            )
        result = self.registry.resolve_incident(
            incident["incident_id"], "修复师与双方馆员复核，确认可继续展出", reviewer)
        self.assertTrue(result["resolved"])
        self.assertEqual(result["reviewer"]["person"], "馆员周甲")
        self.assertFalse(result["frozen"])
        self.assertEqual(result["open_incident_ids"], [])
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


class RepeatedDamageTest(unittest.TestCase):
    """一件作品先后两次受损：事件与冻结状态必须逐笔关联、互不覆盖。"""

    REVIEWER_A = {"org": "甲馆", "role": "出借馆", "person": "馆员周甲"}
    REVIEWER_B = {"org": "乙馆", "role": "承借馆", "person": "馆员吴乙"}

    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("二度受损图", "独立作品", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))

    def _open_incidents(self):
        return self.registry.risk_view(self.work_id)["open_risks"]

    def _two_damages(self):
        # 第一次损伤随到馆交接登记，作品冻结。
        self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "第一次折痕",
                    "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
        ))
        first = self._open_incidents()[0]["incident_id"]
        # 第二次损伤在冻结待复核期间由复检发现：不推进交接、不消费扫码。
        second_view = self.registry.register_reinspection_incident({
            "work_id": self.work_id,
            "on_date": "2026-09-28",
            "damage_note": "复检发现新霉点",
            "before_hashes": ["c" * 64],
            "after_hashes": ["d" * 64],
        })
        second = second_view["incident_id"]
        self.assertIsNone(second_view["handover_id"])
        self.assertFalse(second_view["resolved"])
        return first, second

    def test_consecutive_damages_keep_work_frozen_until_both_resolved(self):
        first, second = self._two_damages()
        # 两个事件都在开放风险中挂账，冻结标记与风险视图一致。
        self.assertEqual(
            self.registry.get_work_view(self.work_id)["open_incident_ids"],
            [first, second],
        )
        risk = self.registry.risk_view(self.work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual([r["incident_id"] for r in risk["open_risks"]], [first, second])
        self.assertEqual(risk["open_risks"][1]["after_hashes"], ["d" * 64])

        # 只解除第一起：冻结必须保留，布展不得放行。
        result = self.registry.resolve_incident(first, "折痕修复并复核通过", self.REVIEWER_A)
        self.assertTrue(result["frozen"])
        self.assertEqual(result["open_incident_ids"], [second])
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
        # 被拦下的布展扫码未被消费，解冻后仍可使用。
        self.registry.resolve_incident(second, "霉点清除并复核通过", self.REVIEWER_B)
        resumed = self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
        self.assertEqual(resumed["resulting_status"], "展出中")

    def test_reinspection_requires_existing_open_risk(self):
        # 没有开放损伤时，复检登记被拒（应走交接状态报告），且不产生事件。
        with self.assertRaises(ConflictError):
            self.registry.register_reinspection_incident({
                "work_id": self.work_id, "on_date": "2026-09-26", "damage_note": "误报",
            })
        self.assertEqual(self._open_incidents(), [])

    def test_resolving_old_incident_twice_is_idempotent(self):
        first, second = self._two_damages()
        self.registry.resolve_incident(first, "折痕修复并复核通过", self.REVIEWER_A)
        # 旧事件被再次“解除”，且提交内容不同：回放原结论，不覆盖、不改状态。
        replay = self.registry.resolve_incident(
            first, "另一份互相矛盾的结论", self.REVIEWER_B)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["resolution_note"], "折痕修复并复核通过")
        self.assertEqual(replay["reviewer"], {
            "org": "甲馆", "role": "出借馆", "person": "馆员周甲",
        })
        # 第二起仍在复核，整件作品仍冻结。
        self.assertTrue(replay["frozen"])
        self.assertEqual(replay["open_incident_ids"], [second])
        stored = next(i for i in self.registry.incidents if i.incident_id == first)
        self.assertEqual(stored.reviewer_person, "馆员周甲")
        # 幂等重放不带复核结论也不报错（不重新校验，只回放）。
        replay_again = self.registry.resolve_incident(first, "  ", None)
        self.assertTrue(replay_again["idempotent"])

    def test_out_of_order_resolution_only_unfreezes_on_last_open_risk(self):
        first, second = self._two_damages()
        # 乱序：先解除后发生的第二起，第一起仍阻断交接。
        later_closed = self.registry.resolve_incident(second, "霉点先复核关闭", self.REVIEWER_B)
        self.assertTrue(later_closed["frozen"])
        self.assertEqual(later_closed["open_incident_ids"], [first])
        risk = self.registry.risk_view(self.work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual([r["incident_id"] for r in risk["open_risks"]], [first])
        # 最后一项风险关闭：风险视图、作品视图同时转无冻结。
        last_closed = self.registry.resolve_incident(first, "折痕复核关闭", self.REVIEWER_A)
        self.assertFalse(last_closed["frozen"])
        self.assertEqual(last_closed["open_incident_ids"], [])
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])
        self.assertEqual(self.registry.risk_view(self.work_id)["open_risks"], [])

    def test_resolution_validates_work_state_and_reviewer(self):
        first, second = self._two_damages()
        # 事件不存在。
        with self.assertRaises(DomainError):
            self.registry.resolve_incident("incident-ghost", "结论", self.REVIEWER_A)
        # 复核结论为空。
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(first, "   ", self.REVIEWER_A)
        # 复核责任缺失 / 角色不符 / 缺机构。
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(first, "结论", None)
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(
                first, "结论", {"org": "长风运输", "role": "运输方", "person": "司机丁"})
        with self.assertRaises(DomainError):
            self.registry.resolve_incident(
                first, "结论", {"org": "  ", "role": "出借馆", "person": "周甲"})
        # 校验全部失败时不得有任何事件被关闭。
        self.assertEqual(
            self.registry.get_work_view(self.work_id)["open_incident_ids"],
            [first, second],
        )

    def test_concurrent_resolutions_and_handovers_do_not_overwrite(self):
        import threading

        first, second = self._two_damages()
        errors: list[BaseException] = []

        def resolve(incident_id, note, reviewer):
            try:
                self.registry.resolve_incident(incident_id, note, reviewer)
            except BaseException as exc:  # noqa: BLE001 - 并发用例收集断言
                errors.append(exc)

        def attempt_hibernation():
            try:
                self.registry.record_handover(
                    handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
            except ConflictError:
                return
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            else:
                errors.append(AssertionError("仍有开放风险时布展不应成功"))

        threads = [
            threading.Thread(target=resolve, args=(first, "折痕关闭", self.REVIEWER_A)),
            threading.Thread(target=resolve, args=(first, "折痕关闭-重复", self.REVIEWER_B)),
            threading.Thread(target=resolve, args=(first, "折痕关闭-重复2", self.REVIEWER_A)),
            threading.Thread(target=attempt_hibernation),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        # 重复解除只落一笔结论，第二起仍开放，交接被阻断且扫码未被消费。
        stored = next(i for i in self.registry.incidents if i.incident_id == first)
        self.assertTrue(stored.resolved)
        self.assertEqual(stored.resolution_note, "折痕关闭")
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])
        self.assertEqual(
            self.registry.get_work_view(self.work_id)["open_incident_ids"], [second])

        # 两起事件的解除与“下一步交接”并发：无论调度先后，
        # 同一扫码至多成立一次布展，状态最终一致（解冻、停在布展之后）。
        outcomes: list[str] = []
        barrier = threading.Barrier(3)

        def close_second():
            barrier.wait()
            self.registry.resolve_incident(second, "霉点关闭", self.REVIEWER_B)

        def install():
            barrier.wait()
            try:
                self.registry.record_handover(
                    handover_payload(self.work_id, "布展", "SCAN-4", "2026-09-30"))
                outcomes.append("installed")
            except ConflictError:
                outcomes.append("blocked")

        jobs = [
            threading.Thread(target=close_second),
            threading.Thread(target=install),
            threading.Thread(target=install),
        ]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(outcomes.count("installed"), 1)
        if outcomes.count("installed") == 0:
            # 两次交接都抢在解除前：解冻后扫码仍可用，补一次成功。
            retried = self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-4", "2026-09-30"))
            self.assertEqual(retried["resulting_status"], "展出中")
        else:
            # 已有一笔成立：重复扫码必须被拒，不能产生第二笔布展。
            with self.assertRaises(ConflictError):
                self.registry.record_handover(
                    handover_payload(self.work_id, "布展", "SCAN-4", "2026-09-30"))
        self.assertEqual(
            [h.handover_id for h in self.registry.handovers if h.scan_code == "SCAN-4"],
            [h.handover_id for h in self.registry.handovers if h.scan_code == "SCAN-4"][:1],
        )
        self.assertEqual(
            len([h for h in self.registry.handovers if h.scan_code == "SCAN-4"]), 1)
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])
        self.assertEqual(self.registry.get_work_view(self.work_id)["custody"]["status"], "展出中")

    def test_label_snapshot_tracks_open_risks_without_mutating_old_version(self):
        first, second = self._two_damages()
        self.registry.create_label(self.work_id, "复损待核期间的说明", [])
        published = self.registry.publish_label(self.work_id, "2026-09-29")
        # 发布时点两起事件都开放，快照与风险视图一致。
        snapshot_risks = published["evidence_snapshot"]["open_risks"]
        self.assertEqual([r["incident_id"] for r in snapshot_risks], [first, second])
        self.assertTrue(published["evidence_snapshot"]["custody"])

        self.registry.resolve_incident(first, "折痕关闭", self.REVIEWER_A)
        # 旧版快照永不改变；更正后发布新版才反映剩余的一起风险。
        self.registry.correct_label(self.work_id, "复检后更新说明", [])
        republished = self.registry.publish_label(self.work_id, "2026-10-02")
        self.assertEqual(
            [r["incident_id"] for r in republished["evidence_snapshot"]["open_risks"]],
            [second],
        )
        self.assertEqual(
            [r["incident_id"]
             for r in self.registry.label_version(self.work_id, 1)["evidence_snapshot"]["open_risks"]],
            [first, second],
        )

    def test_state_recovers_after_restart(self):
        import os
        import tempfile

        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        handle.close()
        state_file = handle.name
        try:
            # 用带快照文件的注册表走一遍“复损 → 解除一起”的流程。
            registry = LoanRegistry(state_file=state_file)
            work = registry.register_work("二度受损图", "独立作品", "甲馆")
            work_id = work["work"]["work_id"]
            registry.create_agreement(agreement_payload(work_id))
            registry.record_handover(
                handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
            registry.record_handover(handover_payload(
                work_id, "到馆", "SCAN-2", "2026-09-27",
                report={"condition": "损伤", "damage_note": "第一次折痕",
                        "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
            ))
            first = registry.risk_view(work_id)["open_risks"][0]["incident_id"]
            second = registry.register_reinspection_incident({
                "work_id": work_id, "on_date": "2026-09-28", "damage_note": "复检新霉点",
            })["incident_id"]
            registry.resolve_incident(first, "折痕关闭", self.REVIEWER_A)
            self.assertTrue(registry.get_work_view(work_id)["frozen"])

            # 模拟服务重启：新注册表从同一快照恢复。
            restarted = LoanRegistry(state_file=state_file)
            self.assertEqual([h.scan_code for h in restarted.handovers], ["SCAN-1", "SCAN-2"])
            self.assertTrue(restarted.get_work_view(work_id)["frozen"])
            self.assertEqual(
                restarted.get_work_view(work_id)["open_incident_ids"], [second])
            # 已解除事件的复核结论原样恢复。
            stored_first = next(i for i in restarted.incidents if i.incident_id == first)
            self.assertTrue(stored_first.resolved)
            self.assertEqual(stored_first.reviewer_person, self.REVIEWER_A["person"])
            # 重启期间布展仍被阻断，扫码不被消费。
            with self.assertRaises(ConflictError):
                restarted.record_handover(
                    handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))
            # 关闭重启后仍开放的最后一起风险，交接链恢复。
            closing = restarted.resolve_incident(second, "霉点关闭", self.REVIEWER_B)
            self.assertFalse(closing["frozen"])
            self.assertFalse(restarted.get_work_view(work_id)["frozen"])
            resumed = restarted.record_handover(
                handover_payload(work_id, "布展", "SCAN-3", "2026-09-30"))
            self.assertEqual(resumed["resulting_status"], "展出中")

            # 再次重启：已解除的结论与扫码去重记忆仍然保留。
            restarted_again = LoanRegistry(state_file=state_file)
            self.assertFalse(restarted_again.get_work_view(work_id)["frozen"])
            with self.assertRaises(ConflictError):
                restarted_again.record_handover(
                    handover_payload(work_id, "撤展", "SCAN-1", "2027-01-05"))
        finally:
            os.unlink(state_file)


if __name__ == "__main__":
    unittest.main()
