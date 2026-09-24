"""HTTP 层契约：验证路由分发与 400/409 状态码映射。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler


def post_json(base_url, path, payload):
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urlopen(request, timeout=2)


def request_json(base_url, method, path, payload=None):
    """发起请求并返回 (status, body)，不把 4xx 当异常。"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}", data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def read_error(error):
    return error.code, json.loads(error.read().decode("utf-8"))


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类使用独立端口；进程内 STATE 由各用例使用不同作品号隔离。
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_register_and_fetch_work(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 烟霭图", "kind": "独立作品", "owner_org": "戊馆",
        }) as response:
            body = json.load(response)
        self.assertEqual(response.status, 200)
        work_id = body["work"]["work_id"]
        with urlopen(f"{self.base_url}/works/{work_id}", timeout=2) as response:
            self.assertEqual(json.load(response)["work"]["owner_org"], "戊馆")

    def test_invalid_kind_is_400(self):
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, "/works", {"title": "x", "kind": "瓷器", "owner_org": "戊馆"})
        code, body = read_error(error.exception)
        self.assertEqual(code, 400)
        self.assertIn("作品类型", body["error"])

    def test_malformed_json_is_400(self):
        request = Request(
            f"{self.base_url}/works", data=b"{not-json",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 400)

    def test_duplicate_scan_is_409_and_chain_continues(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 溪山图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]

        def handover(htype, scan, fr, tr, forg, torg):
            return post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-22",
                "from_party": {"org": forg, "role": fr, "person": "甲"},
                "to_party": {"org": torg, "role": tr, "person": "乙"},
                "report": {"condition": "良好", "image_hashes": ["h"]},
            })

        with handover("出库", "NET-1", "出借馆", "运输方", "甲馆", "运输") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            handover("到馆", "NET-1", "运输方", "承借馆", "运输", "乙馆")
        code, body = read_error(error.exception)
        self.assertEqual(code, 409)
        self.assertIn("重复扫码", body["error"])
        # 新扫码办理到馆成功，证明被拒绝的重复扫码没有破坏生命周期。
        with handover("到馆", "NET-2", "运输方", "承借馆", "运输", "乙馆") as response:
            self.assertEqual(json.load(response)["resulting_status"], "待布展")

    def test_damage_freezes_next_handover_and_risk_reports_it(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 秋林图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]

        def handover(htype, scan, report):
            return post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-22",
                "from_party": {"org": "甲馆", "role": "出借馆" if htype == "出库" else "运输方", "person": "甲"},
                "to_party": {"org": "运输", "role": "运输方" if htype == "出库" else "承借馆", "person": "乙"},
                "report": report,
            })

        handover("出库", "D-1", {"condition": "良好", "image_hashes": ["ok"]}).close()
        with handover("到馆", "D-2", {
            "condition": "损伤", "damage_note": "新增折痕",
            "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
        }) as response:
            self.assertTrue(json.load(response)["frozen"])
        with self.assertRaises(HTTPError) as error:
            handover("布展", "D-3", {"condition": "良好", "image_hashes": ["ok"]})
        self.assertEqual(error.exception.code, 409)
        with urlopen(f"{self.base_url}/works/{work_id}/risk", timeout=2) as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["open_risks"]), 1)
        self.assertEqual(risk["open_risks"][0]["before_hashes"], ["a" * 64])


def _damaged_work_scenario(base_url):
    """连续复损场景：登记作品并走到两起事件同时开放，返回 (work_id, i1, i2)。

    服务进程内 STATE 跨用例共享，扫码全局唯一，因此每次场景取独立前缀。
    """
    import uuid

    prefix = f"R-{uuid.uuid4().hex[:8]}"
    _, body = request_json(base_url, "POST", "/works", {
        "title": "HTTP 复损图", "kind": "独立作品", "owner_org": "甲馆",
    })
    work_id = body["work"]["work_id"]

    def handover(htype, scan, report):
        scan = scan if scan.startswith("RACE-") else f"{prefix}-{scan}"
        fr, tr = ("出借馆", "运输方") if htype == "出库" else (
            ("运输方", "承借馆") if htype == "到馆" else ("承借馆", "承借馆"))
        forg = {"出借馆": "甲馆", "运输方": "长风运输", "承借馆": "乙馆"}[fr]
        torg = {"出借馆": "甲馆", "运输方": "长风运输", "承借馆": "乙馆"}[tr]
        return request_json(base_url, "POST", "/handovers", {
            "work_id": work_id, "type": htype, "scan_code": scan,
            "on_date": "2026-09-22",
            "from_party": {"org": forg, "role": fr, "person": "甲"},
            "to_party": {"org": torg, "role": tr, "person": "乙"},
            "report": report,
        })

    handover("出库", "1", {"condition": "良好", "image_hashes": ["ok"]})
    status, damaged = handover("到馆", "2", {
        "condition": "损伤", "damage_note": "第一次折痕",
        "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
    })
    assert status == 200
    first = damaged["incident_id"]
    # 冻结待复核期间复检再发现第二起损伤。
    status, second_view = request_json(base_url, "POST", f"/works/{work_id}/incidents", {
        "on_date": "2026-09-23", "damage_note": "复检新霉点",
        "before_hashes": ["c" * 64], "after_hashes": ["d" * 64],
    })
    assert status == 200
    return work_id, first, second_view["incident_id"], handover


class IncidentConsistencyApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_views_and_http_consistent_until_last_risk_closed(self):
        base = self.base_url
        work_id, first, second, handover = _damaged_work_scenario(base)

        with urlopen(f"{base}/works/{work_id}", timeout=2) as response:
            work_view = json.load(response)
        self.assertTrue(work_view["frozen"])
        self.assertEqual(work_view["open_incident_ids"], [first, second])
        with urlopen(f"{base}/works/{work_id}/risk", timeout=2) as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual([r["incident_id"] for r in risk["open_risks"]], [first, second])

        reviewer_a = {"org": "甲馆", "role": "出借馆", "person": "周甲"}
        status, resolved_first = request_json(
            base, "POST", f"/incidents/{first}/resolve",
            {"resolution_note": "折痕修复复核通过", "reviewer": reviewer_a})
        self.assertEqual(status, 200)
        self.assertTrue(resolved_first["resolved"])
        self.assertTrue(resolved_first["frozen"])
        self.assertEqual(resolved_first["open_incident_ids"], [second])
        self.assertFalse(resolved_first["idempotent"])

        # 作品视图与风险视图仍反映开放的第二起事件。
        with urlopen(f"{base}/works/{work_id}", timeout=2) as response:
            self.assertTrue(json.load(response)["frozen"])
        # 布展交接仍被 409 拒绝。
        status, body = handover("布展", "3", {"condition": "良好", "image_hashes": ["ok"]})
        self.assertEqual(status, 409)
        self.assertIn("冻结", body["error"])

        # 旧事件重复解除：200 幂等，结论与复核人不被覆盖，仍冻结。
        status, replay = request_json(
            base, "POST", f"/incidents/{first}/resolve",
            {"resolution_note": "另一份矛盾结论",
             "reviewer": {"org": "乙馆", "role": "承借馆", "person": "吴乙"}})
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["resolution_note"], "折痕修复复核通过")
        self.assertEqual(replay["reviewer"], reviewer_a)
        self.assertTrue(replay["frozen"])

        # 最后一项风险关闭：HTTP、作品视图、风险视图一致转解冻。
        status, resolved_last = request_json(
            base, "POST", f"/incidents/{second}/resolve",
            {"resolution_note": "霉点清除复核通过",
             "reviewer": {"org": "乙馆", "role": "承借馆", "person": "吴乙"}})
        self.assertEqual(status, 200)
        self.assertFalse(resolved_last["frozen"])
        self.assertEqual(resolved_last["open_incident_ids"], [])
        with urlopen(f"{base}/works/{work_id}/risk", timeout=2) as response:
            final_risk = json.load(response)
        self.assertFalse(final_risk["frozen"])
        self.assertEqual(final_risk["open_risks"], [])
        # 被拦下的扫码 R-3 未被消费，解冻后成功办理布展。
        status, installed = handover("布展", "3", {"condition": "良好", "image_hashes": ["ok"]})
        self.assertEqual(status, 200)
        self.assertEqual(installed["resulting_status"], "展出中")

    def test_resolution_validates_reviewer_and_state(self):
        base = self.base_url
        work_id, first, _second, _ = _damaged_work_scenario(base)
        # 无复核签认 → 400。
        status, _ = request_json(
            base, "POST", f"/incidents/{first}/resolve",
            {"resolution_note": "结论"})
        self.assertEqual(status, 400)
        # 复核角色不符 → 400。
        status, _ = request_json(
            base, "POST", f"/incidents/{first}/resolve",
            {"resolution_note": "结论",
             "reviewer": {"org": "长风运输", "role": "运输方", "person": "丁"}})
        self.assertEqual(status, 400)
        # 空结论 → 400。
        status, _ = request_json(
            base, "POST", f"/incidents/{first}/resolve",
            {"resolution_note": "   ",
             "reviewer": {"org": "甲馆", "role": "出借馆", "person": "周甲"}})
        self.assertEqual(status, 400)
        # 事件不存在 → 400。
        status, _ = request_json(
            base, "POST", "/incidents/incident-ghost/resolve",
            {"resolution_note": "结论",
             "reviewer": {"org": "甲馆", "role": "出借馆", "person": "周甲"}})
        self.assertEqual(status, 400)
        # 全部失败后两起事件仍开放。
        with urlopen(f"{base}/works/{work_id}/risk", timeout=2) as response:
            self.assertEqual(len(json.load(response)["open_risks"]), 2)

    def test_reinspection_without_open_risk_is_409(self):
        _, body = request_json(self.base_url, "POST", "/works", {
            "title": "HTTP 误报图", "kind": "独立作品", "owner_org": "甲馆",
        })
        work_id = body["work"]["work_id"]
        status, body = request_json(
            self.base_url, "POST", f"/works/{work_id}/incidents",
            {"on_date": "2026-09-23", "damage_note": "并无冻结却复检"})
        self.assertEqual(status, 409)

    def test_concurrent_resolutions_and_handovers_do_not_overwrite(self):
        import concurrent.futures

        base = self.base_url
        work_id, first, second, handover = _damaged_work_scenario(base)
        reviewer_a = {"org": "甲馆", "role": "出借馆", "person": "周甲"}
        reviewer_b = {"org": "乙馆", "role": "承借馆", "person": "吴乙"}

        def resolve(incident_id, note, reviewer):
            return request_json(base, "POST", f"/incidents/{incident_id}/resolve",
                                {"resolution_note": note, "reviewer": reviewer})

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [
                pool.submit(resolve, first, "折痕关闭", reviewer_a),
                pool.submit(resolve, first, "折痕覆盖?", reviewer_b),
                pool.submit(resolve, second, "霉点关闭", reviewer_b),
                pool.submit(resolve, second, "霉点覆盖?", reviewer_a),
                pool.submit(handover, "布展", "RACE-1", {"condition": "良好", "image_hashes": ["ok"]}),
                pool.submit(handover, "布展", "RACE-1", {"condition": "良好", "image_hashes": ["ok"]}),
            ]
            results = [future.result() for future in futures]

        resolution_statuses = [code for code, _ in results[:4]]
        self.assertEqual(resolution_statuses, [200, 200, 200, 200])
        # 同一事件的重复解除至多一笔为非幂等，其余必须回放。
        for incident_id in (first, second):
            replies = [body for code, body in results[:4]
                       if code == 200 and body["incident_id"] == incident_id]
            self.assertEqual(len(replies), 2)
            self.assertEqual(sum(1 for b in replies if not b["idempotent"]), 1)
        # 同一扫码并发布展：绝不允许两笔都成立（否则重复扫码/冻结被穿透）。
        handover_codes = sorted(code for code, _ in results[4:])
        self.assertIn(handover_codes, ([409, 409], [200, 409]))

        with urlopen(f"{base}/works/{work_id}/risk", timeout=2) as response:
            final_risk = json.load(response)
        self.assertFalse(final_risk["frozen"])
        self.assertEqual(final_risk["open_risks"], [])
        # 若两笔布展都抢在解除前被冻结尾下，补一次仍应成功且只有一笔布展。
        status, installed = handover("布展", "RACE-1", {"condition": "良好", "image_hashes": ["ok"]})
        if handover_codes == [409, 409]:
            self.assertEqual(status, 200)
        else:
            self.assertEqual(status, 409)
        with urlopen(f"{base}/works/{work_id}", timeout=2) as response:
            self.assertEqual(json.load(response)["custody"]["status"], "展出中")


class RestartRecoveryApiTest(unittest.TestCase):
    """接口级重启恢复：进程用同一快照文件重启，冻结与解除结论必须延续。"""

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile

        import service

        cls.service = service
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        handle.close()
        cls.state_file = handle.name
        service.configure_state(cls.state_file)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        import os

        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.service.configure_state(None)
        if os.path.exists(cls.state_file):
            os.unlink(cls.state_file)

    def test_01_freeze_state_survives_process_restart(self):
        base = self.base_url
        _, body = request_json(base, "POST", "/works", {
            "title": "HTTP 重启图", "kind": "独立作品", "owner_org": "甲馆",
        })
        work_id = body["work"]["work_id"]

        def handover(htype, scan, report):
            fr, tr = ("出借馆", "运输方") if htype == "出库" else (
                ("运输方", "承借馆") if htype == "到馆" else ("承借馆", "承借馆"))
            forg = {"出借馆": "甲馆", "运输方": "长风运输", "承借馆": "乙馆"}[fr]
            torg = {"出借馆": "甲馆", "运输方": "长风运输", "承借馆": "乙馆"}[tr]
            return request_json(base, "POST", "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-22",
                "from_party": {"org": forg, "role": fr, "person": "甲"},
                "to_party": {"org": torg, "role": tr, "person": "乙"},
                "report": report,
            })

        handover("出库", "BOOT-1", {"condition": "良好", "image_hashes": ["ok"]})
        _, damaged = handover("到馆", "BOOT-2", {
            "condition": "损伤", "damage_note": "折痕",
            "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
        })
        first = damaged["incident_id"]
        _, second_view = request_json(base, "POST", f"/works/{work_id}/incidents", {
            "on_date": "2026-09-23", "damage_note": "复检霉点",
        })
        second = second_view["incident_id"]
        status, resolved = request_json(
            base, "POST", f"/incidents/{first}/resolve",
            {"resolution_note": "折痕关闭",
             "reviewer": {"org": "甲馆", "role": "出借馆", "person": "周甲"}})
        self.assertEqual(status, 200)
        type(self).work_id = work_id
        type(self).first = first
        type(self).second = second

    def test_02_after_restart_open_risk_still_blocks_then_resumes(self):
        # 模拟进程重启：用同一快照文件重建注册表（Handler 每次请求读全局 STATE）。
        self.service.configure_state(self.state_file)
        base = self.base_url
        work_id, first, second = self.work_id, self.first, self.second

        with urlopen(f"{base}/works/{work_id}/risk", timeout=2) as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual([r["incident_id"] for r in risk["open_risks"]], [second])

        def install(scan):
            return request_json(base, "POST", "/handovers", {
                "work_id": work_id, "type": "布展", "scan_code": scan,
                "on_date": "2026-09-30",
                "from_party": {"org": "乙馆", "role": "承借馆", "person": "甲"},
                "to_party": {"org": "乙馆", "role": "承借馆", "person": "乙"},
                "report": {"condition": "良好", "image_hashes": ["ok"]},
            })

        # 重启后冻结仍在，布展被拦，扫码不消费。
        status, body = install("BOOT-3")
        self.assertEqual(status, 409)
        self.assertIn("冻结", body["error"])
        # 已解除事件的结论在重启后保持，重复解除仍幂等。
        status, replay = request_json(
            base, "POST", f"/incidents/{first}/resolve",
            {"resolution_note": "想覆盖旧结论",
             "reviewer": {"org": "乙馆", "role": "承借馆", "person": "吴乙"}})
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["resolution_note"], "折痕关闭")
        # 关闭最后一起开放事件，交接链恢复。
        status, closing = request_json(
            base, "POST", f"/incidents/{second}/resolve",
            {"resolution_note": "霉点关闭",
             "reviewer": {"org": "乙馆", "role": "承借馆", "person": "吴乙"}})
        self.assertEqual(status, 200)
        self.assertFalse(closing["frozen"])
        status, installed = install("BOOT-3")
        self.assertEqual(status, 200)
        self.assertEqual(installed["resulting_status"], "展出中")


if __name__ == "__main__":
    unittest.main()
