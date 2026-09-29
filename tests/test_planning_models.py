import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from codex_workbench.planning_models import ModelCallError, PlanningModels


def model(provider, identifier, **extra):
    return {"id": identifier, "provider_id": provider, "model_type": "reasoning", "model": identifier,
            "base_url": "https://registered.example/v1", "protocol": "openai-chat", "credential_ref": "vault:ref",
            "validation_status": "verified", **extra}


def account(provider, remaining, reset, observed=None):
    observed = observed or datetime.now(timezone.utc)
    return {"provider_id": provider, "updated_at": observed.isoformat(), "usage_windows": [{
        "usage": {"remaining": remaining, "limit": 100, "observed_at": observed.isoformat()},
        "resets_at": reset.isoformat()}]}


class PlanningModelsTest(unittest.TestCase):
    def test_only_registered_verified_https_candidates_and_unknown_usage_is_none(self):
        models = [model("minimax", "ok"), model("minimax", "http", base_url="http://registered.example"),
                  model("minimax", "unverified", validation_status="failed"), model("minimax", "unregistered", registered=False)]
        selected = PlanningModels(lambda: models, lambda: []).candidates(external_allowed=True)
        self.assertEqual(["ok"], [item["id"] for item in selected])
        self.assertIsNone(selected[0]["_route"]["remaining_ratio"])
        self.assertEqual("unknown", selected[0]["_route"]["freshness"])

    def test_minimax_auxiliary_priority_and_fresh_quota_sorting(self):
        now = datetime.now(timezone.utc)
        router = PlanningModels(
            lambda: [model("bigmodel", "glm"), model("minimax", "mini")],
            lambda: [account("bigmodel", 90, now + timedelta(hours=8)), account("minimax", 10, now + timedelta(hours=2))],
        )
        selected = router.candidates(external_allowed=True)
        self.assertEqual(["mini", "glm"], [item["id"] for item in selected])
        self.assertEqual("fresh", selected[0]["_route"]["freshness"])
        self.assertEqual(0.1, selected[0]["_route"]["remaining_ratio"])

    def test_registered_specialised_full_endpoint_needs_no_chat_profile(self):
        embedding = {"id": "bigmodel-embedding-3", "provider_id": "bigmodel", "model_type": "embedding",
                     "model": "embedding-3", "base_url": "https://registered.example/v1/embeddings",
                     "credential_id": "registered-entry", "validation_status": "verified"}
        rerank = {"id": "bigmodel-rerank", "provider_id": "bigmodel", "model_type": "rerank",
                  "model": "rerank", "base_url": "https://registered.example/v1/rerank",
                  "credential_id": "registered-entry", "validation_status": "verified"}
        router = PlanningModels(lambda: [embedding, rerank])
        self.assertEqual(["bigmodel-embedding-3"], [item["id"] for item in router.candidates("embedding", external_allowed=True)])
        self.assertEqual(["bigmodel-rerank"], [item["id"] for item in router.candidates("rerank", external_allowed=True)])

    def test_stale_or_expired_zero_does_not_permanently_exclude_provider(self):
        now = datetime.now(timezone.utc)
        stale = now - timedelta(hours=1)
        router = PlanningModels(lambda: [model("minimax", "mini")], lambda: [account("minimax", 0, now - timedelta(minutes=1), stale)])
        selected = router.candidates(external_allowed=True)
        self.assertEqual(["mini"], [item["id"] for item in selected])
        self.assertIsNone(selected[0]["_route"]["remaining_ratio"])
        self.assertEqual("unknown", selected[0]["_route"]["freshness"])
        exhausted = PlanningModels(lambda: [model("minimax", "mini")], lambda: [account("minimax", 0, now + timedelta(minutes=30))])
        self.assertEqual([], exhausted.candidates(external_allowed=True))

    def test_external_boundary_prevents_calls_and_provider_fallback_once(self):
        calls, events = [], []
        models = [model("minimax", "m1"), model("minimax", "m2"), model("bigmodel", "g1")]
        def invoke(candidate, request):
            calls.append((candidate["provider_id"], request["tool"]))
            raise ModelCallError("rate_limited")
        router = PlanningModels(lambda: models, lambda: [], invoke=invoke, record=events.append)
        with self.assertRaises(ModelCallError) as error:
            router.call("reasoning", {"input": "private prompt"}, external_allowed=False)
        self.assertEqual("data_boundary", error.exception.code)
        self.assertEqual([], calls)
        with self.assertRaises(ModelCallError) as error:
            router.call("reasoning", {"input": "private prompt"}, external_allowed=True)
        self.assertEqual("rate_limited", error.exception.code)
        self.assertEqual([("minimax", "reasoning_chat"), ("bigmodel", "reasoning_chat")], calls)
        self.assertEqual(["rate_limited", "rate_limited"], [event["code"] for event in events])
        self.assertEqual([None, None], [event["usage"] for event in events])
        self.assertNotIn("private prompt", repr(events))

    def test_same_condition_latency_is_persisted_and_guides_next_route(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        router = PlanningModels(lambda: [model('bigmodel', 'slow'), model('minimax', 'fast')], lambda: [])
        router.bind_telemetry(db)
        # Persisted telemetry contains route metadata only; it has no request or response body.
        db.execute("""INSERT INTO planning_model_calls
            (capability,purpose,model_id,provider_id,success,code,elapsed_ms,usage,route,created_at)
            VALUES('reasoning','delivery_review','slow','minimax',1,NULL,900,NULL,'{}','2026-01-01T00:00:00+00:00')""")
        db.execute("""INSERT INTO planning_model_calls
            (capability,purpose,model_id,provider_id,success,code,elapsed_ms,usage,route,created_at)
            VALUES('reasoning','delivery_review','fast','minimax',1,NULL,50,NULL,'{}','2026-01-01T00:00:00+00:00')""")
        selected = router.candidates('reasoning', external_allowed=True, purpose='delivery_review')
        self.assertEqual(['fast', 'slow'], [item['id'] for item in selected])
        router.invoke = lambda candidate, request: {'success': True, 'text': 'ok', 'input_tokens': 2}
        router.call('reasoning', {'messages': [{'content': 'private payload'}]}, external_allowed=True, purpose='delivery_review')
        row = db.execute('SELECT usage,route,code FROM planning_model_calls ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual('{"input_tokens":2}', row['usage'])
        self.assertNotIn('private payload', repr(tuple(row)))
        def fallback(candidate, request):
            if candidate['id'] == 'fast':
                raise ModelCallError('rate_limited')
            return {'success': True, 'text': 'ok'}
        router.invoke = fallback
        router.call('reasoning', {'messages': [{'content': 'another private payload'}]}, external_allowed=True, purpose='delivery_review')
        fallback_event = db.execute("SELECT fallback_reason FROM planning_model_calls WHERE model_id='slow' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual('rate_limited', fallback_event['fallback_reason'])
        self.assertEqual(5, sum(item['calls'] for item in router.stats('reasoning', 'delivery_review')))


if __name__ == "__main__":
    unittest.main()
