import unittest

import httpx

from china_matcher import match_1688_suppliers
from config import Settings


class TestChinaMatcher(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            RAPIDAPI_1688_KEY="test-rapid-key",
            RAPIDAPI_1688_HOST="1688-datahub.p.rapidapi.com",
        )

    async def test_mock_mode_returns_three_suppliers(self) -> None:
        offers = await match_1688_suppliers(
            "https://cdn.example.com/products/ems-neck-massager.jpg",
            self.settings,
            mock=True,
        )
        self.assertEqual(len(offers), 3)
        self.assertAlmostEqual(offers[0].wholesale_price_rmb, 24.0)
        self.assertAlmostEqual(offers[0].wholesale_price_usd, 3.36)
        self.assertEqual(offers[0].moq, 5)
        self.assertEqual(offers[0].years_on_1688, 5)

    async def test_rapidapi_parses_and_slices_top_three(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("X-RapidAPI-Key"), "test-rapid-key")
            self.assertEqual(request.headers.get("X-RapidAPI-Host"), "1688-datahub.p.rapidapi.com")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "resultList": [
                            {
                                "item": {
                                    "num_iid": "4",
                                    "title": "Expensive Offer",
                                    "price": "50.0",
                                    "moq": 2,
                                    "seller": {"name": "Supplier D", "score": "4.5", "years": "2"},
                                }
                            },
                            {
                                "item": {
                                    "num_iid": "1",
                                    "title": "Cheapest Offer",
                                    "price": "20.0",
                                    "moq": 10,
                                    "seller": {"name": "Supplier A", "score": "4.9", "years": "6"},
                                }
                            },
                            {
                                "item": {
                                    "num_iid": "2",
                                    "title": "Second Offer",
                                    "price": "25.0",
                                    "moq": 5,
                                    "seller": {"name": "Supplier B", "score": "4.8", "years": "4"},
                                }
                            },
                            {
                                "item": {
                                    "itemId": "3",
                                    "titleEn": "Third Offer",
                                    "itemUrl": "//detail.1688.com/offer/3.html",
                                    "image": "//cbu01-overseas.1688.com/img/3.jpg",
                                    "sku": {"def": {"price": "30.0", "minOrder": "3"}},
                                    "seller": {"name": "Supplier C", "score": "4.7", "years": "3"},
                                }
                            },
                        ]
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            offers = await match_1688_suppliers("https://img.test/p.jpg", self.settings, client=client)

        self.assertEqual(len(offers), 3)
        self.assertEqual([o.supplier_name for o in offers], ["Supplier A", "Supplier B", "Supplier C"])
        self.assertAlmostEqual(offers[0].wholesale_price_usd, 2.80)
        self.assertEqual(offers[0].years_on_1688, 6)
        self.assertEqual(offers[0].moq, 10)
        self.assertEqual(offers[2].product_url, "https://detail.1688.com/offer/3.html")
        self.assertEqual(offers[2].image_url, "https://cbu01-overseas.1688.com/img/3.jpg")
        self.assertEqual(offers[2].moq, 3)

    async def test_missing_key_empty_image_and_http_error(self) -> None:
        empty_key_settings = Settings(_env_file=None)
        self.assertEqual(await match_1688_suppliers("", self.settings), [])
        self.assertEqual(await match_1688_suppliers("https://img.test/p.jpg", empty_key_settings), [])

        def err_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"message": "unavailable"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(err_handler)) as client:
            offers = await match_1688_suppliers("https://img.test/p.jpg", self.settings, client=client)
        self.assertEqual(offers, [])


if __name__ == "__main__":
    unittest.main()
