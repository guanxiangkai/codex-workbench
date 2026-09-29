"""Bounded, read-only planning context assembly.

This module deliberately does not know how reviewed knowledge is stored.  Its
callers must supply catalog-verified scopes and already-linked records; that
keeps a task context from turning into a cross-project catalog search.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import sqlite3
import threading
from datetime import datetime, timezone


class PlanningContext:
    """Build a portable task context without mutating reviewed knowledge."""

    def __init__(self, *, embedding=None, rerank=None, embedding_binding=None,
                 rerank_binding=None, max_items=20, max_bytes=131072, cache_path=None):
        self.embedding = embedding
        self.rerank = rerank
        self.embedding_binding = embedding_binding or {}
        self.rerank_binding = rerank_binding or {}
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.cache_lock = threading.RLock()
        self.cache = sqlite3.connect(str(cache_path) if cache_path else ':memory:', check_same_thread=False)
        self.cache.execute('CREATE TABLE IF NOT EXISTS vectors (model TEXT, dimensions INTEGER, hash TEXT, vector TEXT, touched REAL, PRIMARY KEY(model,dimensions,hash))')
        self.cache.commit()
        self.cache_hits = 0

    def close(self):
        with self.cache_lock:
            self.cache.close()

    @staticmethod
    def _bounded(value, size):
        return value.encode('utf-8')[:size].decode('utf-8', errors='ignore')

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _expired(value):
        if not value:
            return False
        try:
            stamp = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed <= datetime.now(timezone.utc)
        except ValueError:
            return True

    @staticmethod
    def _scope(record):
        return record.get("scope") or record.get("scope_key")

    @staticmethod
    def _text(record):
        return " ".join(str(record.get(key, "")) for key in
                        ("title", "summary", "content", "tags", "name", "mime"))

    def _item(self, record, kind):
        content = record.get("content") or record.get("summary") or ""
        if not isinstance(content, str):
            content = str(content)
        content = self._bounded(content, self.max_bytes)
        digest = self._hash(content)
        claimed = record.get("sha256") or record.get("source_revision_hash")
        return {
            "kind": kind,
            "layer": record.get("source_layer") if record.get("source_layer") in {"rule", "skill", "stable_knowledge"} and kind == "knowledge" else ("stable_knowledge" if kind == "knowledge" else "task_context"),
            "id": record.get("key") or record.get("knowledge_key") or record.get("id"),
            "scope": self._scope(record),
            "title": record.get("title") or record.get("name") or "",
            "content": content,
            "sha256": digest,
            "hash_changed": bool(claimed and claimed != digest),
            "expired": self._expired(record.get("expires_at") or record.get("expired_at")),
            "external_allowed": record.get('external_allowed') is not False and record.get('network_scope') not in {'internal','private','local'} and record.get('sensitivity') not in {'secret','private','restricted'},
            "provenance": {
                "source": record.get("source") or record.get("source_native_id") or kind,
                "source_id": record.get("source_id") or record.get("source_native_id") or record.get("id"),
                "source_revision_hash": record.get("source_revision_hash") or claimed,
            },
            "metadata": {key: record.get(key) for key in ("tags", "mime", "version", "updated_at") if record.get(key) is not None},
        }

    def _keyword_rank(self, items, query):
        terms = [term.casefold() for term in str(query).split() if term]
        if not terms:
            return items
        scored = []
        for index, item in enumerate(items):
            text = (item["title"] + " " + item["content"] + " " + str(item["metadata"])).casefold()
            score = sum(text.count(term) for term in terms)
            if score:
                scored.append((score, index, item))
        return [item for _, _, item in sorted(scored, key=lambda value: (-value[0], value[1]))]

    @staticmethod
    def _cosine(left, right):
        numerator = sum(a * b for a, b in zip(left, right))
        denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
        return numerator / denominator if denominator else 0.0

    def _vectors(self, texts):
        if not self.embedding:
            raise ValueError('embedding_unavailable')
        vectors = [None] * len(texts)
        model, dimensions = self.embedding_binding.get('model_id'), self.embedding_binding.get('dimensions')
        with self.cache_lock:
            for index,text in enumerate(texts):
                row=self.cache.execute('SELECT vector FROM vectors WHERE model=? AND dimensions=? AND hash=?',(model,dimensions,self._hash(text))).fetchone()
                if row:
                    vectors[index]=json.loads(row[0]); self.cache_hits+=1
        missing=[i for i,vector in enumerate(vectors) if vector is None]
        if not missing:
            return vectors
        response = self.embedding([texts[index] for index in missing])
        expected_model = self.embedding_binding.get("model_id")
        expected_dimensions = self.embedding_binding.get("dimensions")
        new_vectors = response.get("vectors") if isinstance(response, dict) else None
        if (not expected_model or not isinstance(expected_dimensions, int) or expected_dimensions < 1
                or not isinstance(response, dict) or response.get("model_id") != expected_model
                or not isinstance(new_vectors, list) or len(new_vectors) != len(missing)
                or any(not isinstance(vector, list) or len(vector) != expected_dimensions or any(not isinstance(x,(int,float)) or isinstance(x,bool) or not math.isfinite(x) for x in vector) for vector in new_vectors)):
            raise ValueError("embedding_binding_invalid")
        with self.cache_lock:
            for index,vector in zip(missing,new_vectors):
                vectors[index]=vector
                self.cache.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?)',(expected_model,expected_dimensions,self._hash(texts[index]),json.dumps(vector),time.time()))
            self.cache.execute('DELETE FROM vectors WHERE rowid IN (SELECT rowid FROM vectors ORDER BY touched DESC LIMIT -1 OFFSET 256)')
            self.cache.commit()
        return vectors

    def retrieve(self, task, linked_knowledge=(), assets=(), *, query="", allowed_scopes=(), semantic=False):
        """Return a task-scoped context.  Semantic callbacks are opt-in only."""
        task_id = task.get("id") or task.get("task_id")
        verified_scopes = set(allowed_scopes or ())
        knowledge = []
        for record in linked_knowledge or ():
            if not isinstance(record, dict) or self._scope(record) not in verified_scopes:
                continue
            item = self._item(record, "knowledge")
            if item["id"]:
                knowledge.append(item)
        asset_items = []
        for record in assets or ():
            if not isinstance(record, dict):
                continue
            linked_tasks = set(record.get("task_ids") or ())
            if record.get("task_id") != task_id and task_id not in linked_tasks:
                continue
            asset_items.append(self._item(record, "asset"))
        # A hash is authoritative for exact duplicates.  Do not silently merge near matches.
        unique, seen = [], set()
        for item in knowledge + asset_items:
            key = (item["kind"], item["sha256"])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        ranked = self._keyword_rank(unique, query)[:self.max_items]
        expired = [item for item in unique if item['expired']]
        ranked = [item for item in ranked if not item['expired']]
        fallback = None
        used_embedding = used_rerank = False
        candidates = []
        hits_before=self.cache_hits
        semantic_items = [item for item in unique if item['external_allowed'] and not item['expired']][:self.max_items]
        if semantic and semantic_items:
            try:
                texts=[self._bounded(item['title']+' '+item['content'],8192).strip() for item in semantic_items]
                vectors = self._vectors(texts)
                used_embedding = True
                for left in range(len(semantic_items)):
                    for right in range(left + 1, len(semantic_items)):
                        score = self._cosine(vectors[left], vectors[right])
                        if score >= 0.98 and semantic_items[left]["sha256"] != semantic_items[right]["sha256"]:
                            candidates.append({"kind": "semantic_merge_candidate", "left": semantic_items[left]["id"],
                                               "right": semantic_items[right]["id"], "score": score, "reviewed": False})
                if self.rerank:
                    response = self.rerank(query, [{**item,'content':text} for item,text in zip(semantic_items,texts)])
                    if response.get("model_id") != self.rerank_binding.get("model_id") or not isinstance(response.get("results"), list):
                        raise ValueError("rerank_binding_invalid")
                    order = [entry.get("index") for entry in response["results"]]
                    if len(set(order))!=len(order) or not order or any(not isinstance(index, int) or isinstance(index,bool) or index < 0 or index >= len(semantic_items) for index in order):
                        raise ValueError("rerank_result_invalid")
                    ranked = [semantic_items[index] for index in order] + [item for item in ranked if not item['external_allowed']]
                    used_rerank = True
            except (TypeError, ValueError, KeyError):
                fallback = "keyword"
        return {"task_id": task_id, "allowed_scopes": sorted(verified_scopes), "items": ranked[:self.max_items],
                "layers": {layer: [item["id"] for item in ranked[:self.max_items] if item["layer"] == layer] for layer in ("rule", "skill", "stable_knowledge", "task_context")},
                "candidates": candidates, "used_embedding": used_embedding, "used_rerank": used_rerank,
                "fallback": fallback, 'expired_sources':[{'id':item['id'],'provenance':item['provenance'],'reason':'requires_revalidation'} for item in expired],
                'cache_hits': self.cache_hits-hits_before, 'external_excluded':sum(not item['external_allowed'] for item in unique)}

    def extract_candidates(self, delivery, *, scope, source, scope_exists, relation_type=None, direction=None):
        """Turn accepted current delivery evidence into local review candidates."""
        if not isinstance(delivery, dict) or delivery.get("verified") is not True or not callable(scope_exists) or not scope_exists(scope):
            return []
        if not isinstance(source, dict) or not source.get("id"):
            return []
        # ``verified`` is caller supplied data.  A candidate may be derived only
        # when Planning has attached a persisted, current acceptance record.
        evidence = delivery.get("accepted_evidence")
        if (not isinstance(evidence, dict) or evidence.get("accepted") is not True
                or evidence.get("source") != source
                or not isinstance(evidence.get("card_revision"), int)
                or evidence["card_revision"] < 1
                or not isinstance(evidence.get("card_sha256"), str)
                or not evidence["card_sha256"]
                or not isinstance(evidence.get("verified_at"), str)
                or not evidence["verified_at"]):
            return []
        output = []
        for item in delivery.get("knowledge_candidates", ()):
            if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not isinstance(item.get("content"), str):
                continue
            content = item["content"][:self.max_bytes]
            output.append({"scope": scope, "title": item["title"][:500], "content": content,
                           "sha256": self._hash(content), "source": dict(source),
                           "card_revision": evidence["card_revision"], "card_sha256": evidence["card_sha256"],
                           "verified_at": evidence["verified_at"],
                           "applicability": {"status": "unknown", "reason": "pending_confirmation"},
                           "relation_type": relation_type, "direction": direction, "status": "candidate",
                           "reviewed": False, "local_only": True})
        return output
