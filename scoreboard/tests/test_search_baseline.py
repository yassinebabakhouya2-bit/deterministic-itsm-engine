import json
import subprocess
import unittest
from unittest import mock

from kecore import azure as kazure
from scoreboard.dataset import Ticket
from scoreboard.engines import search_baseline as sb
from scoreboard.engines.search_baseline import (
    SearchBaselineConfig,
    SearchBaselineEngine,
    SearchClient,
    SearchError,
    TokenProvider,
    sanitize_query,
    suggest_config,
)


class FakeToken:
    def __init__(self, token, expires_on):
        self.token = token
        self.expires_on = expires_on


class FakeCredential:
    def __init__(self):
        self.calls = 0

    def get_token(self, scope):
        self.calls += 1
        return FakeToken(f"token-{self.calls}", 10_000)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return self.responses.pop(0)


def engine_with(responses, **overrides):
    settings = {"endpoint": "https://srch-test.search.windows.net", "semantic_configuration": "default"}
    settings.update(overrides)
    config = SearchBaselineConfig.from_dict(settings)
    transport = FakeTransport(responses)
    tokens = TokenProvider("entra", credential=FakeCredential(), clock=lambda: 0)
    client = SearchClient(config.endpoint, config.api_version, tokens, transport=transport, sleep=lambda s: None)
    return SearchBaselineEngine(config, client=client), transport, tokens


HITS = {
    "value": [
        {"@search.score": 0.03, "@search.rerankerScore": 2.8, "title": "KB0010001 Outlook gelé", "id": "c1"},
        {"@search.score": 0.02, "@search.rerankerScore": 2.5, "title": "KB0010001 Outlook gelé", "id": "c2"},
        {"@search.score": 0.02, "@search.rerankerScore": 1.9, "title": "Réparer Office.pdf", "id": "c3"},
        {"@search.score": 0.01, "@search.rerankerScore": 1.2, "title": None, "id": "c4"},
    ]
}


class QueryTest(unittest.TestCase):
    def test_operators_removed_and_long_text_cut_on_a_word(self):
        self.assertEqual(sanitize_query('Erreur -2147024891 "accès refusé" (Outlook)'), "Erreur 2147024891 accès refusé Outlook")
        self.assertEqual(sanitize_query("mot " * 400, max_chars=20), "mot mot mot mot mot")
        self.assertEqual(sanitize_query("+++"), "*")


class ConfigTest(unittest.TestCase):
    def test_rejects_typos_and_placeholders(self):
        with self.assertRaisesRegex(ValueError, "unknown setting"):
            SearchBaselineConfig.from_dict({"endpoint": "https://x.search.windows.net", "indexx": "a"})
        with self.assertRaisesRegex(ValueError, "endpoint"):
            SearchBaselineConfig.from_dict({"endpoint": "https://<search-service>.search.windows.net"})
        with self.assertRaisesRegex(ValueError, "query_type"):
            SearchBaselineConfig.from_dict({"endpoint": "https://x.search.windows.net", "query_type": "full"})


class SearchBaselineTest(unittest.TestCase):
    def test_request_shape(self):
        engine, transport, _ = engine_with(
            [(200, HITS)], vector_field="text_vector", filter="clientId eq '{client}'", top=10
        )
        engine.decide(Ticket("1", "client-s", "Outlook ne démarre plus + écran figé"))
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"], "https://srch-test.search.windows.net/indexes/idx-client-s/docs/search?api-version=2024-07-01"
        )
        self.assertEqual(call["headers"], {"Authorization": "Bearer token-1"})
        body = call["body"]
        self.assertEqual(body["search"], "Outlook ne démarre plus écran figé")
        self.assertEqual((body["queryType"], body["semanticConfiguration"], body["top"]), ("semantic", "default", 10))
        self.assertEqual(body["vectorQueries"], [{"kind": "text", "text": body["search"], "fields": "text_vector", "k": 10}])
        self.assertEqual(body["filter"], "clientId eq 'client-s'")
        self.assertEqual(body["select"], "title")

    def test_chunks_collapse_into_fiches(self):
        engine, _, _ = engine_with([(200, HITS)])
        decision = engine.decide(Ticket("1", "a", "x"))
        self.assertEqual(decision.kind, "fiche")
        self.assertEqual(decision.fiches, ["KB0010001", "Réparer Office"])
        self.assertEqual(decision.score, 2.8)
        self.assertEqual(decision.titles["Réparer Office"], "Réparer Office.pdf")
        self.assertEqual(decision.usage.search_calls, 1)

    def test_min_score_and_empty_results_abstain(self):
        engine, _, _ = engine_with([(200, HITS)], min_score=3.0)
        decision = engine.decide(Ticket("1", "a", "x"))
        self.assertEqual((decision.kind, decision.fiches[0]), ("abstain", "KB0010001"))
        engine, _, _ = engine_with([(200, {"value": []})])
        self.assertEqual(engine.decide(Ticket("1", "a", "x")).kind, "abstain")

    def test_candidates(self):
        engine, _, _ = engine_with([(200, HITS)])
        self.assertEqual(engine.candidates(Ticket("1", "a", "x"), 1), [("KB0010001", "KB0010001 Outlook gelé")])

    def test_expired_token_is_renewed_once(self):
        engine, transport, tokens = engine_with([(401, {"error": {"message": "expired"}}), (200, HITS)])
        engine.decide(Ticket("1", "a", "x"))
        self.assertEqual([c["headers"]["Authorization"] for c in transport.calls], ["Bearer token-1", "Bearer token-2"])

    def test_throttling_is_retried_and_forbidden_explains_the_role(self):
        engine, transport, _ = engine_with([(429, {}), (503, {}), (200, HITS)])
        self.assertEqual(engine.decide(Ticket("1", "a", "x")).kind, "fiche")
        self.assertEqual(len(transport.calls), 3)
        engine, _, _ = engine_with([(403, {"error": {"message": "Forbidden"}})])
        with self.assertRaisesRegex(SearchError, "Search Index Data Reader"):
            engine.decide(Ticket("1", "a", "x"))

    def test_forbidden_index_definition_names_the_contributor_role(self):
        tokens = TokenProvider("entra", credential=FakeCredential(), clock=lambda: 0)
        client = SearchClient("https://x.search.windows.net", "2024-07-01", tokens,
                              transport=FakeTransport([(403, {"error": {"message": "Forbidden"}})]))
        with self.assertRaisesRegex(SearchError, "Search Service Contributor"):
            sb.fetch_index("https://x.search.windows.net", "idx-a", client=client)


class TokenProviderTest(unittest.TestCase):
    def test_key_auth_reads_the_environment(self):
        provider = TokenProvider("key")
        with mock.patch.dict("os.environ", {"AZURE_SEARCH_KEY": "secret"}):
            self.assertEqual(provider.headers(), {"api-key": "secret"})
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SearchError):
                provider.headers()

    def test_azure_cli_fallback(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            out = json.dumps({"accessToken": "cli-token", "expires_on": 1000})
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        provider = TokenProvider("entra", runner=runner, clock=lambda: 0)
        with mock.patch.object(kazure.shutil, "which", return_value="/usr/bin/az"):
            token, expires = provider._fetch_from_cli()
        self.assertEqual((token, expires), ("cli-token", 1000.0))
        self.assertEqual(calls[0][1:5], ["account", "get-access-token", "--resource", "https://search.azure.com"])

    def test_azure_cli_missing_or_failing(self):
        provider = TokenProvider("entra", runner=lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "Please run 'az login'"))
        with mock.patch.object(kazure.shutil, "which", return_value=None):
            with self.assertRaisesRegex(SearchError, "no Azure credential"):
                provider._fetch_from_cli()
        with mock.patch.object(kazure.shutil, "which", return_value="/usr/bin/az"):
            with self.assertRaisesRegex(SearchError, "az login"):
                provider._fetch_from_cli()

    def test_token_renewed_before_expiry(self):
        now = [0]
        credential = FakeCredential()
        provider = TokenProvider("entra", credential=credential, clock=lambda: now[0])
        provider.headers()
        provider.headers()
        self.assertEqual(credential.calls, 1)
        now[0] = 9_950
        provider.headers()
        self.assertEqual(credential.calls, 2)


INDEX = {
    "name": "idx-client-s",
    "fields": [
        {"name": "chunk_id", "type": "Edm.String", "key": True, "retrievable": True},
        {"name": "parent_id", "type": "Edm.String", "retrievable": True, "filterable": True},
        {"name": "chunk", "type": "Edm.String", "searchable": True, "retrievable": True},
        {"name": "title", "type": "Edm.String", "searchable": True, "retrievable": True},
        {"name": "clientId", "type": "Edm.String", "filterable": True, "retrievable": True},
        {"name": "text_vector", "type": "Collection(Edm.Single)", "dimensions": 3072, "vectorSearchProfile": "p1"},
    ],
    "vectorSearch": {"profiles": [{"name": "p1", "algorithm": "hnsw", "vectorizer": "aoai"}]},
    "semantic": {"configurations": [{"name": "sem-config"}]},
}


class SuggestConfigTest(unittest.TestCase):
    def test_suggestion_is_a_valid_config(self):
        config, notes = suggest_config("https://srch-test.search.windows.net/", INDEX, client="client-s")
        self.assertEqual(config["index"], "idx-{client}")
        self.assertEqual(config["vector_field"], "text_vector")
        self.assertEqual(config["semantic_configuration"], "sem-config")
        self.assertEqual(config["fiche_id_field"], "title")
        self.assertEqual(config["title_field"], "title")
        self.assertTrue(any("clientId" in note for note in notes))
        SearchBaselineConfig.from_dict(config)

    def test_bare_index(self):
        config, notes = suggest_config("https://x.search.windows.net", {"name": "kb", "fields": []})
        self.assertEqual(config["query_type"], "simple")
        self.assertEqual(len(notes), 3)
        with self.assertRaises(ValueError):
            SearchBaselineConfig.from_dict(config)


if __name__ == "__main__":
    unittest.main()
