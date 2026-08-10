from __future__ import annotations

import json
import threading
import unittest
from unittest import mock
from urllib import error as urllib_error

from amazon_scraper.openai_client import (
    OpenAIClient,
    OpenAIConfig,
    OpenAIConfigError,
    OpenAIRunCancelledError,
)


class _RecordingClient(OpenAIClient):
    def __init__(self, *, allow_remote_image_urls: bool = False) -> None:
        super().__init__(
            OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
                vision_model="gpt-5.4",
                allow_remote_image_urls=allow_remote_image_urls,
            )
        )
        self.calls: list[tuple[str, dict]] = []
        self.downloaded_urls: list[str] = []

    def _post_json(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "value": "ok",
                                "evidence": "visible label",
                                "source": "image",
                                "confidence": 0.9,
                            }
                        )
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 20,
                "total_tokens": 140,
            },
        }

    def _download_image_data_url(self, image_url: str) -> str:
        self.downloaded_urls.append(image_url)
        return "data:image/jpeg;base64,QUJDRA=="


class OpenAIClientImageInputTests(unittest.TestCase):
    def test_create_chat_completion_inlines_image_inputs_by_default(self) -> None:
        client = _RecordingClient()

        response = client.create_chat_completion(
            "system prompt",
            "user prompt",
            asin="B0TEST",
            field="ingredients",
            model="gpt-5.4",
            image_inputs=[
                {"url": "https://example.com/front.jpg", "detail": "low"},
                {"url": "https://example.com/facts.jpg", "detail": "high"},
            ],
        )

        self.assertIn('"value": "ok"', response)
        self.assertEqual(len(client.calls), 1)
        path, payload = client.calls[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["messages"][0]["role"], "developer")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertEqual(payload["messages"][1]["content"][0], {"type": "text", "text": "user prompt"})
        self.assertEqual(
            client.downloaded_urls,
            ["https://example.com/front.jpg", "https://example.com/facts.jpg"],
        )
        self.assertEqual(
            payload["messages"][1]["content"][1],
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJDRA==", "detail": "low"}},
        )
        self.assertEqual(
            payload["messages"][1]["content"][2],
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJDRA==", "detail": "high"}},
        )

    def test_create_chat_completion_can_opt_in_to_remote_image_urls(self) -> None:
        client = _RecordingClient(allow_remote_image_urls=True)

        client.create_chat_completion(
            "system prompt",
            "user prompt",
            asin="B0TEST",
            field="ingredients",
            model="gpt-5.4",
            image_inputs=[{"url": "https://example.com/front.jpg", "detail": "high"}],
        )

        self.assertEqual(client.downloaded_urls, [])
        self.assertEqual(
            client.calls[0][1]["messages"][1]["content"][1],
            {"type": "image_url", "image_url": {"url": "https://example.com/front.jpg", "detail": "high"}},
        )

    def test_create_chat_completion_stops_before_request_when_cancelled(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        client = _RecordingClient()
        client.cancel_event = cancel_event

        with self.assertRaises(OpenAIRunCancelledError):
            client.create_chat_completion(
                "system prompt",
                "user prompt",
                asin="B0TEST",
                field="ingredients",
            )

        self.assertEqual(client.calls, [])

    def test_create_chat_completion_retries_with_inline_data_on_invalid_image_url(self) -> None:
        class _RetryingClient(_RecordingClient):
            def __init__(self) -> None:
                super().__init__()
                self.downloaded_urls: list[str] = []

            def _post_json(self, path: str, payload: dict) -> dict:
                self.calls.append((path, payload))
                first_image = payload["messages"][1]["content"][1]["image_url"]["url"]
                if first_image.startswith("https://"):
                    raise RuntimeError(
                        'OpenAI request failed status=400 body={"error":{"message":"Timeout while downloading https://example.com/front.jpg.","code":"invalid_image_url"}}'
                    )
                return super()._post_json(path, payload)

            def _download_image_data_url(self, image_url: str) -> str:
                self.downloaded_urls.append(image_url)
                return "data:image/jpeg;base64,QUJDRA=="

        client = _RetryingClient()
        client.config = OpenAIConfig(
            api_key="dummy",
            embedding_model="text-embedding-3-small",
            chat_model="gpt-5.4-mini",
            vision_model="gpt-5.4",
            allow_remote_image_urls=True,
        )

        response = client.create_chat_completion(
            "system prompt",
            "user prompt",
            asin="B0TEST",
            field="ingredients",
            image_inputs=[{"url": "https://example.com/front.jpg", "detail": "high"}],
        )

        self.assertIn('"value": "ok"', response)
        self.assertEqual(client.downloaded_urls, ["https://example.com/front.jpg"])
        self.assertEqual(len(client.calls), 3)
        retry_payload = client.calls[1][1]
        self.assertEqual(
            retry_payload["messages"][1]["content"][1],
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJDRA==", "detail": "high"}},
        )


class ImageDownloadResilienceTests(unittest.TestCase):
    """One flaky gallery image must not cost the whole image-first path."""

    def _client(self) -> OpenAIClient:
        return OpenAIClient(
            OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            )
        )

    def test_transient_reset_is_retried_then_succeeds(self) -> None:
        client = self._client()
        attempts = {"n": 0}

        class _Resp:
            headers = {"Content-Type": "image/jpeg"}

            def read(self): return b"\xff\xd8\xff\xdbBODY"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _flaky_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib_error.URLError(ConnectionResetError(10054, "forcibly closed"))
            return _Resp()

        with mock.patch("amazon_scraper.openai_client.request.urlopen", _flaky_urlopen), \
                mock.patch("amazon_scraper.openai_client.time.sleep"):
            data_url = client._download_image_data_url("https://example.com/a.jpg")

        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(attempts["n"], 3)

    def test_permanent_failure_still_raises(self) -> None:
        client = self._client()

        def _always_fails(req, timeout=None):
            raise urllib_error.URLError(ConnectionResetError(10054, "forcibly closed"))

        with mock.patch("amazon_scraper.openai_client.request.urlopen", _always_fails), \
                mock.patch("amazon_scraper.openai_client.time.sleep"):
            with self.assertRaises(RuntimeError):
                client._download_image_data_url("https://example.com/a.jpg")

    def test_one_bad_image_is_skipped_and_the_rest_are_kept(self) -> None:
        class _PartialClient(OpenAIClient):
            def _download_image_data_url(self, image_url: str) -> str:
                if "bad" in image_url:
                    raise RuntimeError("Image download failed url=" + image_url)
                return "data:image/jpeg;base64,QUJDRA=="

        client = _PartialClient(
            OpenAIConfig(api_key="dummy", embedding_model="e", chat_model="c")
        )
        converted = client._convert_image_inputs_to_data_urls([
            {"url": "https://example.com/good1.jpg", "detail": "high"},
            {"url": "https://example.com/bad.jpg", "detail": "high"},
            {"url": "https://example.com/good2.jpg", "detail": "high"},
        ])

        self.assertEqual(len(converted), 2)

    def test_all_images_failing_is_fatal(self) -> None:
        class _AllFailClient(OpenAIClient):
            def _download_image_data_url(self, image_url: str) -> str:
                raise RuntimeError("Image download failed url=" + image_url)

        client = _AllFailClient(
            OpenAIConfig(api_key="dummy", embedding_model="e", chat_model="c")
        )
        with self.assertRaises(RuntimeError):
            client._convert_image_inputs_to_data_urls([{"url": "https://example.com/a.jpg"}])


class CanonicalizationModelTests(unittest.TestCase):
    def _config(self, **kwargs) -> OpenAIConfig:
        return OpenAIConfig(
            api_key="dummy",
            embedding_model="text-embedding-3-small",
            chat_model="gpt-5.4-mini",
            **kwargs,
        )

    def test_falls_back_to_the_chat_model(self) -> None:
        self.assertEqual(self._config().resolved_canonicalization_model(), "gpt-5.4-mini")

    def test_override_wins_when_set(self) -> None:
        config = self._config(canonicalization_model="gpt-5.4")
        self.assertEqual(config.resolved_canonicalization_model(), "gpt-5.4")

    def test_cost_limit_rejects_an_unpriced_override(self) -> None:
        config = self._config(
            canonicalization_model="some-unpriced-model",
            max_cost_usd=1.0,
            embedding_cost_per_1m_tokens=0.02,
            chat_input_cost_per_1m_tokens=0.25,
            chat_output_cost_per_1m_tokens=2.0,
        )
        with self.assertRaises(OpenAIConfigError) as caught:
            config.validate_cost_controls()
        self.assertIn("OPENAI_CANONICALIZATION_MODEL", str(caught.exception))

    def test_no_budget_means_an_unpriced_override_is_allowed(self) -> None:
        config = self._config(canonicalization_model="some-unpriced-model")
        config.validate_cost_controls()   # must not raise
        self.assertEqual(config.resolved_canonicalization_model(), "some-unpriced-model")


if __name__ == "__main__":
    unittest.main()
