from __future__ import annotations

import json
import threading
import unittest

from amazon_scraper.openai_client import OpenAIClient, OpenAIConfig, OpenAIRunCancelledError


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


if __name__ == "__main__":
    unittest.main()
