import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from config import Settings
from main import _parse_args, main as cli_main
from web_app import create_app


class TestWebApp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            output_dir=Path(self.tmp_dir.name),
            settings=Settings(_env_file=None),
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_health_and_subpath_middleware(self) -> None:
        resp_root = self.client.get("/api/health")
        self.assertEqual(resp_root.status_code, 200)
        data = resp_root.json()
        self.assertEqual(data["status"], "ok")
        self.assertGreaterEqual(data["products_loaded"], 1)

        resp_spy = self.client.get("/spy/api/health")
        self.assertEqual(resp_spy.status_code, 200)
        self.assertEqual(resp_spy.json()["service"], "tovarspy")

        resp_page = self.client.get("/spy/")
        self.assertEqual(resp_page.status_code, 200)
        self.assertIn("TovarSpy", resp_page.text)
        for marker in ("/api/scan", "/api/export/csv", "/api/alert"):
            self.assertIn(marker, resp_page.text)

        resp_root_page = self.client.get("/")
        self.assertEqual(resp_root_page.status_code, 200)
        self.assertIn("/api/scan", resp_root_page.text)

    def test_config_and_report_endpoints(self) -> None:
        cfg_resp = self.client.get("/api/config")
        self.assertEqual(cfg_resp.status_code, 200)
        cfg = cfg_resp.json()
        self.assertEqual(cfg["target_countries"], ["UA", "PL", "RO", "CZ", "DE", "US"])
        self.assertEqual(cfg["min_duration_days"], 7)
        self.assertEqual(cfg["available_sources"], ["meta", "adheart", "tiktok"])

        rep_resp = self.client.get("/spy/api/report")
        self.assertEqual(rep_resp.status_code, 200)
        report = rep_resp.json()
        self.assertIn("summary", report)
        self.assertIn("products", report)
        self.assertGreaterEqual(report["summary"]["total"], 2)

    def test_scan_with_threshold_override_and_exports(self) -> None:
        scan_resp = self.client.post(
            "/spy/api/scan",
            json={
                "query": "neck massager",
                "countries": ["PL", "UA"],
                "mock": True,
                "winner_net_margin_usd": 20.0,
            },
        )
        self.assertEqual(scan_resp.status_code, 200)
        payload = scan_resp.json()
        self.assertEqual(payload["meta"]["query"], "neck massager")
        # With $20 threshold, Sunset Lamp ($16.98) and Posture ($7.87) are non-winners; Neck Massager ($25.39) is winner
        self.assertEqual(payload["summary"]["winners"], 1)

        csv_resp = self.client.get("/api/export/csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("attachment", csv_resp.headers.get("content-disposition", ""))
        self.assertIn("ad_id", csv_resp.text)

        json_resp = self.client.get("/spy/api/export/json")
        self.assertEqual(json_resp.status_code, 200)
        exported_items = json.loads(json_resp.text)
        self.assertEqual(len(exported_items), payload["summary"]["total"])

    def test_alert_endpoint_mock_and_404(self) -> None:
        report = self.client.get("/api/report").json()
        winner = next(p for p in report["products"] if p["margin"]["is_winner"])

        ok_resp = self.client.post("/spy/api/alert", json={"ad_id": winner["ad"]["ad_id"], "mock": True})
        self.assertEqual(ok_resp.status_code, 200)
        self.assertTrue(ok_resp.json()["sent"])

        missing_resp = self.client.post("/api/alert", json={"ad_id": "nonexistent-id", "mock": True})
        self.assertEqual(missing_resp.status_code, 404)

    def test_cli_serve_arguments_and_uvicorn_launch(self) -> None:
        args = _parse_args(["--serve", "--host", "127.0.0.1", "--port", "8085"])
        self.assertTrue(args.serve)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8085)

        with patch("uvicorn.run") as mock_uvicorn:
            rc = cli_main(["--serve", "--port", "8085"])
        self.assertEqual(rc, 0)
        mock_uvicorn.assert_called_once_with("web_app:app", host="0.0.0.0", port=8085)


if __name__ == "__main__":
    unittest.main()
