import json
import unittest

from kecore.azure import AzureError, RestClient, TokenProvider
from kecore.llm import (AzureOpenAIChat, AzureOpenAIConfig, AzureOpenAIEmbeddings, LLMError, RecordingEmbeddings,
                        RecordingLLM)
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

    def test_the_first_recorded_answer_wins_a_race(self):
        # another execution recorded this request between our read and our write: its answer is returned
        class Store(RaceStore):
            def write_if_absent(self, relative, text):
                self.data[relative] = json.dumps({"response": {"data": {"winner": True}, "usage": {}, "model": "m"}})
                return False

        recorder = RecordingLLM(FakeLLM({"T": {"sections": [], "steps": [step("Fermez Outlook.")]}}), store=Store(None))
        result = recorder.complete_json("s", "Article title: T\n", {}, "fiche_steps")
        self.assertEqual((result.data, result.cached), ({"winner": True}, True))

    def test_a_different_model_does_not_reuse_the_record(self):
        recorder = RecordingLLM(FakeLLM({}), self.path("cache"))
        recorder.complete_json("s", "u", {}, "x")
        other = RecordingLLM(None, self.path("cache"), mode="replay", model_id="another-model")
        with self.assertRaises(LLMError):
            other.complete_json("s", "u", {}, "x")



def embeddings_with(responses):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append({"url": url, "body": body})
        return responses.pop(0)

    config = AzureOpenAIConfig.from_dict({"endpoint": "https://acct.openai.azure.com", "deployment": "text-embedding-3-large"})
    tokens = TokenProvider("entra", scope="https://cognitiveservices.azure.com/.default", credential=FakeCredential(),
                           clock=lambda: 0)
    client = RestClient(config.endpoint, config.api_version, tokens, transport=transport, sleep=lambda s: None,
                        error_class=LLMError)
    return AzureOpenAIEmbeddings(config, client=client), calls


class FakeEmbedder:
    model_id = "fake-embed@test"

    def __init__(self, dims=3):
        self.dims = dims
        self.calls = []

    def embed(self, texts, dimensions=None):
        self.calls.append(list(texts))
        return [[float(len(t) + i) for i in range(self.dims)] for t in texts]


class RaceStore:
    """A store where another caller records the same text between our read and our write."""

    def __init__(self, recorded_vector):
        self.recorded = recorded_vector
        self.data = {}

    def read(self, relative):
        return self.data.get(relative)

    def write(self, relative, text):
        self.data[relative] = text

    def write_if_absent(self, relative, text):
        self.data[relative] = json.dumps({"vector": self.recorded})  # the other caller won
        return False


class EmbeddingsTest(TempDirTestCase):
    def test_request_and_answer_in_input_order(self):
        client, calls = embeddings_with([(200, {"data": [{"index": 1, "embedding": [0.0, 1.0]},
                                                         {"index": 0, "embedding": [1.0, 0.0]}]})])
        self.assertEqual(client.embed(["a", "b"], dimensions=2), [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(calls[0]["url"], "https://acct.openai.azure.com/openai/deployments/text-embedding-3-large/"
                                          "embeddings?api-version=2024-10-21")
        self.assertEqual(calls[0]["body"], {"input": ["a", "b"], "dimensions": 2})
        self.assertEqual(client.model_id, "text-embedding-3-large@acct.openai.azure.com")

    def test_a_wrong_count_or_size_is_an_error(self):
        client, _ = embeddings_with([(200, {"data": [{"index": 0, "embedding": [1.0]}]}),
                                     (200, {"data": [{"index": 0, "embedding": [1.0]}]})])
        with self.assertRaises(LLMError):
            client.embed(["a", "b"])
        with self.assertRaises(LLMError):
            client.embed(["a"], dimensions=2)

    def test_record_replay_refresh_and_one_call_per_distinct_text(self):
        inner = FakeEmbedder()
        recorder = RecordingEmbeddings(inner, cache_dir=self.path("cache"), dimensions=3, batch_size=2)
        first = recorder.embed(["ab", "abc", "ab", "abcd", "abcde"])
        self.assertEqual(first[0], first[2])
        self.assertEqual([len(c) for c in inner.calls], [2, 2])  # 4 distinct texts, 2 per request
        self.assertEqual((recorder.calls, recorder.embedded, recorder.hits), (2, 4, 0))
        again = recorder.embed(["abcd", "ab"])
        self.assertEqual(again, [first[3], first[0]])
        self.assertEqual(len(inner.calls), 2)

        replay = RecordingEmbeddings(None, cache_dir=self.path("cache"), mode="replay", model_id="fake-embed@test",
                                     dimensions=3)
        self.assertEqual(replay.embed(["abc"]), [first[1]])
        with self.assertRaisesRegex(LLMError, "replay"):
            replay.embed(["never seen"])
        other_size = RecordingEmbeddings(None, cache_dir=self.path("cache"), mode="replay",
                                         model_id="fake-embed@test", dimensions=4)
        with self.assertRaises(LLMError):  # another size is another request: nothing recorded for it
            other_size.embed(["abc"])

        refresh = RecordingEmbeddings(inner, cache_dir=self.path("cache"), mode="refresh", dimensions=3)
        refresh.embed(["abc"])
        self.assertEqual(len(inner.calls), 3)

    def test_the_first_recorded_vector_wins_a_race(self):
        recorder = RecordingEmbeddings(FakeEmbedder(), store=RaceStore([9.0, 9.0, 9.0]), dimensions=3)
        self.assertEqual(recorder.embed(["x y"]), [[9.0, 9.0, 9.0]])

    def test_empty_texts_and_bad_settings_are_refused(self):
        recorder = RecordingEmbeddings(FakeEmbedder(), cache_dir=self.path("cache"), dimensions=3)
        with self.assertRaises(ValueError):
            recorder.embed(["ok", "  "])
        with self.assertRaises(ValueError):
            RecordingEmbeddings(FakeEmbedder(), cache_dir=self.path("cache"), dimensions=0)
        with self.assertRaises(ValueError):
            RecordingEmbeddings(FakeEmbedder(), cache_dir=self.path("cache"), mode="other")


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
