"""HTTP 层契约：验证路由分发、400/409 状态码映射、解除幂等与重启恢复。"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import ApiState, Handler


def post_json(base_url, path, payload):
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urlopen(request, timeout=2)


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


class IncidentResolveApiTest(unittest.TestCase):
    """损伤事件解除的接口行为：幂等、校验、与视图一致的冻结反馈。"""

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

    def _register_work(self, title):
        with post_json(self.base_url, "/works", {
            "title": title, "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            return json.load(response)["work"]["work_id"]

    def _handover(self, work_id, htype, scan, report=None):
        pairs = {
            "出库": ("出借馆", "运输方", "甲馆", "长风运输"),
            "到馆": ("运输方", "承借馆", "长风运输", "乙馆"),
            "布展": ("承借馆", "承借馆", "乙馆", "乙馆"),
            "撤展": ("承借馆", "运输方", "乙馆", "长风运输"),
        }
        from_role, to_role, from_org, to_org = pairs[htype]
        return post_json(self.base_url, "/handovers", {
            "work_id": work_id, "type": htype, "scan_code": scan,
            "on_date": "2026-09-22",
            "from_party": {"org": from_org, "role": from_role, "person": "甲"},
            "to_party": {"org": to_org, "role": to_role, "person": "乙"},
            "report": report or {"condition": "良好", "image_hashes": ["h"]},
        })

    def _resolve(self, incident_id, payload):
        return post_json(self.base_url, f"/incidents/{incident_id}/resolve", payload)

    def _get(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return json.load(response)

    def test_second_damage_stays_frozen_when_old_incident_resolved_again(self):
        """连续复损场景：误点第一次事件的解除，第二次损伤仍阻断交接。"""
        work_id = self._register_work("HTTP 复损图")
        self._handover(work_id, "出库", "R-1").close()
        with self._handover(work_id, "到馆", "R-2", {
            "condition": "损伤", "damage_note": "第一次损伤",
        }) as response:
            first_incident = json.load(response)["incident_id"]
        with self._resolve(first_incident, {
            "resolution_note": "第一次复核通过", "reviewed_by": "复核员甲",
        }) as response:
            self.assertFalse(json.load(response)["frozen"])
        with self._handover(work_id, "布展", "R-3", {
            "condition": "损伤", "damage_note": "第二次损伤，仍在复核",
        }) as response:
            second_incident = json.load(response)["incident_id"]

        # 误点第一次事件的解除：幂等返回，冻结保持。
        with self._resolve(first_incident, {"resolution_note": "误点重复提交"}) as response:
            self.assertEqual(response.status, 200)
            body = json.load(response)
        self.assertTrue(body["idempotent"])
        self.assertEqual(body["resolution_note"], "第一次复核通过")
        self.assertTrue(body["frozen"])
        self.assertEqual(body["open_incidents"], [second_incident])

        # 作品视图与风险视图一致反映仍开放的事件。
        self.assertTrue(self._get(f"/works/{work_id}")["frozen"])
        risk = self._get(f"/works/{work_id}/risk")
        self.assertTrue(risk["frozen"])
        self.assertEqual([r["incident_id"] for r in risk["open_risks"]], [second_incident])

        # 下一步交接仍被 409 阻断。
        with self.assertRaises(HTTPError) as error:
            self._handover(work_id, "撤展", "R-4")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 第二次损伤解除后，交接链恢复。
        with self._resolve(second_incident, {
            "resolution_note": "第二次复核通过", "reviewed_by": "复核员乙",
        }) as response:
            self.assertFalse(json.load(response)["frozen"])
        with self._handover(work_id, "撤展", "R-4") as response:
            self.assertEqual(json.load(response)["resulting_status"], "待归还")

    def test_resolve_validates_reviewer_and_work(self):
        """解除接口校验：缺复核责任人 400，作品不符 409，重复提交幂等。"""
        work_id = self._register_work("HTTP 校验图")
        other_id = self._register_work("HTTP 另一件")
        self._handover(work_id, "出库", "V-1", {
            "condition": "损伤", "damage_note": "出库即发现损伤",
        }).close()
        incident_id = self._get(f"/works/{work_id}/risk")["open_risks"][0]["incident_id"]

        with self.assertRaises(HTTPError) as error:
            self._resolve(incident_id, {"resolution_note": "结论"})
        self.assertEqual(error.exception.code, 400)
        self.assertIn("复核责任人", json.loads(error.exception.read())["error"])
        error.exception.close()

        with self.assertRaises(HTTPError) as error:
            self._resolve(incident_id, {
                "resolution_note": "结论", "reviewed_by": "复核员甲", "work_id": other_id,
            })
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 上述失败均未改变冻结状态。
        self.assertTrue(self._get(f"/works/{work_id}")["frozen"])

        with self._resolve(incident_id, {
            "resolution_note": "复核通过", "reviewed_by": "复核员甲", "work_id": work_id,
        }) as response:
            body = json.load(response)
        self.assertEqual(body["reviewed_by"], "复核员甲")
        self.assertFalse(body["frozen"])

        # 重复提交幂等：返回原结论，不被新内容覆盖。
        with self._resolve(incident_id, {
            "resolution_note": "试图改写结论", "reviewed_by": "别人",
        }) as response:
            again = json.load(response)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["resolution_note"], "复核通过")
        self.assertEqual(again["reviewed_by"], "复核员甲")

    def test_late_incident_registration_and_out_of_order_resolve(self):
        """迟报损伤补登记：两个开放事件可乱序解除，最后一项关闭才解冻。"""
        work_id = self._register_work("HTTP 迟报图")
        with self._handover(work_id, "出库", "L-1") as response:
            outbound_id = json.load(response)["handover_id"]
        with self._handover(work_id, "到馆", "L-2", {
            "condition": "损伤", "damage_note": "到馆发现第一处损伤",
        }) as response:
            first_incident = json.load(response)["incident_id"]

        # 复核期间在出库照片上迟报第二处损伤。
        with post_json(self.base_url, "/incidents", {
            "work_id": work_id, "handover_id": outbound_id,
            "note": "复核出库照片发现第二处旧伤",
            "before_hashes": ["c" * 64], "after_hashes": ["d" * 64],
        }) as response:
            self.assertEqual(response.status, 200)
            late = json.load(response)
        second_incident = late["incident_id"]
        self.assertTrue(late["frozen"])
        self.assertEqual(len(self._get(f"/works/{work_id}/risk")["open_risks"]), 2)

        # 乱序：先解除后登记的事件，冻结保持。
        with self._resolve(second_incident, {
            "resolution_note": "第二处结论", "reviewed_by": "复核员乙",
        }) as response:
            step1 = json.load(response)
        self.assertTrue(step1["frozen"])
        self.assertEqual(step1["open_incidents"], [first_incident])
        with self.assertRaises(HTTPError) as error:
            self._handover(work_id, "布展", "L-3")
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 最后一项风险关闭，交接恢复。
        with self._resolve(first_incident, {
            "resolution_note": "第一处结论", "reviewed_by": "复核员甲",
        }) as response:
            step2 = json.load(response)
        self.assertFalse(step2["frozen"])
        self.assertEqual(step2["open_incidents"], [])
        with self._handover(work_id, "布展", "L-3") as response:
            self.assertEqual(response.status, 200)

    def test_concurrent_resolve_requests_stay_consistent(self):
        """并发解除：同一事件只解除一次，全部请求拿到一致的记录。"""
        work_id = self._register_work("HTTP 并发图")
        self._handover(work_id, "出库", "C-1", {
            "condition": "损伤", "damage_note": "出库损伤",
        }).close()
        incident_id = self._get(f"/works/{work_id}/risk")["open_risks"][0]["incident_id"]

        results, errors = [], []

        def worker(note):
            try:
                with self._resolve(incident_id, {
                    "resolution_note": note, "reviewed_by": "复核员",
                }) as response:
                    results.append(json.load(response))
            except HTTPError as error:
                errors.append(error.code)

        threads = [threading.Thread(target=worker, args=(f"结论-{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        # 并发请求不互相覆盖：所有响应是同一份解除记录。
        self.assertEqual(len({body["resolution_note"] for body in results}), 1)
        self.assertFalse(self._get(f"/works/{work_id}")["frozen"])


class RestartRecoveryTest(unittest.TestCase):
    """服务重启：状态落盘后，未解除风险、扫码去重与交接进度恢复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.server = None
        self.thread = None

    def tearDown(self):
        self._stop()
        self.tmp.cleanup()

    def _stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None

    def _restart(self):
        self._stop()
        state = ApiState(self.state_file)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.state = state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def test_state_survives_restart(self):
        base = self._restart()
        with post_json(base, "/works", {
            "title": "HTTP 重启图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]
        with post_json(base, "/handovers", {
            "work_id": work_id, "type": "出库", "scan_code": "BOOT-1",
            "on_date": "2026-09-22",
            "from_party": {"org": "甲馆", "role": "出借馆", "person": "甲"},
            "to_party": {"org": "长风运输", "role": "运输方", "person": "乙"},
            "report": {"condition": "损伤", "damage_note": "出库损伤",
                       "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
        }) as response:
            incident_id = json.load(response)["incident_id"]

        # 第一次“重启”：冻结与未解除风险恢复。
        base = self._restart()
        with urlopen(f"{base}/works/{work_id}", timeout=2) as response:
            view = json.load(response)
        self.assertTrue(view["frozen"])
        self.assertEqual(view["open_incidents"], [incident_id])
        with urlopen(f"{base}/works/{work_id}/risk", timeout=2) as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual(risk["open_risks"][0]["before_hashes"], ["a" * 64])

        # 重复扫码仍 409，冻结中的下一步交接仍被阻断。
        for scan, htype in (("BOOT-1", "到馆"), ("BOOT-2", "到馆")):
            with self.assertRaises(HTTPError) as error:
                post_json(base, "/handovers", {
                    "work_id": work_id, "type": htype, "scan_code": scan,
                    "on_date": "2026-09-23",
                    "from_party": {"org": "长风运输", "role": "运输方", "person": "甲"},
                    "to_party": {"org": "乙馆", "role": "承借馆", "person": "乙"},
                    "report": {"condition": "良好", "image_hashes": ["h"]},
                })
            self.assertEqual(error.exception.code, 409)
            error.exception.close()

        # 解除在重启后仍然有效，交接链从断点继续。
        with post_json(base, f"/incidents/{incident_id}/resolve", {
            "resolution_note": "重启后复核通过", "reviewed_by": "复核员甲",
        }) as response:
            self.assertFalse(json.load(response)["frozen"])
        with post_json(base, "/handovers", {
            "work_id": work_id, "type": "到馆", "scan_code": "BOOT-2",
            "on_date": "2026-09-23",
            "from_party": {"org": "长风运输", "role": "运输方", "person": "甲"},
            "to_party": {"org": "乙馆", "role": "承借馆", "person": "乙"},
            "report": {"condition": "良好", "image_hashes": ["h"]},
        }) as response:
            self.assertEqual(response.status, 200)

        # 第二次“重启”：交接进度与解除记录保留。
        base = self._restart()
        with urlopen(f"{base}/works/{work_id}", timeout=2) as response:
            view = json.load(response)
        self.assertFalse(view["frozen"])
        self.assertEqual(view["custody"]["status"], "待布展")
        with urlopen(f"{base}/works/{work_id}/risk", timeout=2) as response:
            self.assertEqual(json.load(response)["open_risks"], [])


if __name__ == "__main__":
    unittest.main()
