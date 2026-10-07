import json
import unittest

from kecore.azure import AzureError, RestClient, TokenProvider
from kecore.llm import AzureOpenAIChat, AzureOpenAIConfig, LLMError, RecordingLLM
from kecore.llm_segment import SCHEMA_NAME, llm_segment, schema

from .helpers import FakeLLM, TempDirTestCase, step


class FakeCredential:
    def get_token(self, scope):
        self.scope = scope

        class Token:
            token = "tok"
            expires_on = 10_000

        return Token()


def chat_with(responses):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return responses.pop(0)

    config = AzureOpenAIConfig.from_dict({"endpoint": "https://acct.openai.azure.com", "deployment": "gpt-4o"})
    credential = FakeCredential()
    tokens = TokenProvider("entra", scope="https://cognitiveservices.azure.com/.default", credential=credential, clock=lambda: 0)
    client = RestClient(config.endpoint, config.api_version, tokens, transport=transport, sleep=lambda s: None,
                        error_class=LLMError)
    return AzureOpenAIChat(config, client=client), calls, credential


def completion(content, finish="stop", refusal=None):
    message = {"content": content}
    if refusal:
        message["refusal"] = refusal
    return 200, {"choices": [{"message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 900, "completion_tokens": 120}, "model": "gpt-4o-2024-11-20"}


class AzureOpenAITest(unittest.TestCase):
    def test_request_and_answer(self):
        chat, calls, credential = chat_with([completion(json.dumps({"sections": [], "steps": []}))])
        result = chat.complete_json("sys", "user", schema(), SCHEMA_NAME)
        self.assertEqual(result.data, {"sections": [], "steps": []})
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (900, 120))
        call = calls[0]
        self.assertEqual(
            call["url"], "https://acct.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21"
        )
        self.assertEqual(call["headers"], {"Authorization": "Bearer tok"})
        self.assertEqual(credential.scope, "https://cognitiveservices.azure.com/.default")
        body = call["body"]
        self.assertEqual((body["temperature"], body["seed"]), (0.0, 7))
        self.assertEqual(body["response_format"]["json_schema"]["name"], SCHEMA_NAME)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(chat.model_id, "gpt-4o@acct.openai.azure.com")

    def test_refusal_truncation_and_bad_json(self):
        for response, message in (
            (completion(None, refusal="no"), "refused"),
            (completion("{", finish="length"), "max_tokens"),
            (completion("not json"), "did not return JSON"),
            ((429, {}), None),
        ):
            responses = [response] if message else [response, completion("{}")]
            chat, _, _ = chat_with(responses)
            if message:
                with self.assertRaisesRegex(LLMError, message):
                    chat.complete_json("s", "u", {}, "x")
            else:
                self.assertEqual(chat.complete_json("s", "u", {}, "x").data, {})

    def test_config_validation(self):
        with self.assertRaisesRegex(ValueError, "endpoint"):
            AzureOpenAIConfig.from_dict({"endpoint": "https://<foundry-account>.openai.azure.com"})
        with self.assertRaisesRegex(ValueError, "unknown LLM setting"):
            AzureOpenAIConfig.from_dict({"endpoint": "https://a.openai.azure.com", "model": "x"})


class TransportRetryTest(unittest.TestCase):
    """A transport-level failure (DNS, connect, read timeout) retries like a 429/5xx, then
    surfaces through the client's own error_class -- never the generic AzureError -- so a caller
    that only catches its configured error type (kefind.interpret catching LLMError) degrades
    instead of crashing."""

    def client(self, transport, retries=3):
        tokens = TokenProvider("entra", scope="https://cognitiveservices.azure.com/.default",
                               credential=FakeCredential(), clock=lambda: 0)
        return RestClient("https://acct.openai.azure.com", "2024-10-21", tokens, transport=transport,
                          sleep=lambda s: None, retries=retries, error_class=LLMError)

    def test_a_timeout_that_then_succeeds_is_retried_silently(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise AzureError("cannot reach host: timed out")
            return 200, {"ok": True}, {}

        result = self.client(transport).request("POST", "/x")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_a_timeout_that_never_recovers_raises_the_client_s_own_error_class_not_azure_error(self):
        def transport(method, url, headers, body, timeout):
            raise AzureError("cannot reach host: timed out")

        with self.assertRaises(LLMError):
            self.client(transport, retries=2).request("POST", "/x")


class RecordingTest(TempDirTestCase):
    def test_record_replay_refresh(self):
        inner = FakeLLM({"T": {"sections": [], "steps": [step("Fermez Outlook.")]}})
        recorder = RecordingLLM(inner, self.path("cache"))
        first = recorder.complete_json("s", "Article title: T\n", {}, "fiche_steps")
        second = recorder.complete_json("s", "Article title: T\n", {}, "fiche_steps")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.data, first.data)
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual((recorder.calls, recorder.hits), (1, 1))

        replay = RecordingLLM(None, self.path("cache"), mode="replay", model_id="fake-model@test")
        self.assertEqual(replay.complete_json("s", "Article title: T\n", {}, "fiche_steps").data, first.data)
        with self.assertRaisesRegex(LLMError, "replay"):
            replay.complete_json("s", "another request", {}, "fiche_steps")

        refresh = RecordingLLM(inner, self.path("cache"), mode="refresh")
        self.assertFalse(refresh.complete_json("s", "Article title: T\n", {}, "fiche_steps").cached)
        self.assertEqual(len(inner.calls), 2)

    def test_a_different_model_does_not_reuse_the_record(self):
        recorder = RecordingLLM(FakeLLM({}), self.path("cache"))
        recorder.complete_json("s", "u", {}, "x")
        other = RecordingLLM(None, self.path("cache"), mode="replay", model_id="another-model")
        with self.assertRaises(LLMError):
            other.complete_json("s", "u", {}, "x")


class SegmentationParseTest(unittest.TestCase):
    def test_malformed_items_are_counted_not_trusted(self):
        answer = {
            "sections": [{"heading": "Résolution", "role": "resolution"}, {"heading": "", "role": "x"}],
            "steps": [step("Fermez Outlook."), {"quote": "", "kind": "action"}, {"quote": "x", "kind": "maybe"},
                      dict(step("Relancez."), section_role="nowhere")],
        }
        result = llm_segment(FakeLLM({"T": answer}), "T", "texte")
        self.assertEqual([s.quote for s in result.steps], ["Fermez Outlook.", "Relancez."])
        self.assertEqual(result.steps[1].section_role, "other")
        self.assertEqual(result.malformed, 3)
        self.assertEqual(result.sections, [("Résolution", "resolution")])

    def test_long_fiches_are_skipped(self):
        llm = FakeLLM({})
        self.assertIsNotNone(llm_segment(llm, "T", "x" * 70_000).skipped)
        self.assertEqual(llm.calls, [])


if __name__ == "__main__":
    unittest.main()
