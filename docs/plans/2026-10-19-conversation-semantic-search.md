# Plan: semantic conversation search (phase 2)

Phase 1 shipped keyword search: the `messages_fts` FTS5 table over
`messages.text`, `ConversationStore.search(query, limit)` returning
`SearchHit(conversation_id, title, message_id, snippet, score)`,
`GET /api/search?q=`, and the search box in the chat sidebar. This plan adds
meaning-based recall on top of it **without changing the API, the UI or the
FTS table**: `search()` gains a second ranked source and fuses the two.

Numbers below come from the spike
(`2026-10-19-conversation-search-spike.md`) unless marked otherwise.

## Today's scale

| | |
| --- | --- |
| messages | ~1,600 (half user, half assistant), 1.04 MB of text |
| assistant share of text | 89% (avg message 667 chars, max 26 KB) |
| growth | ~9.5 messages / ~6.3 KB / ~11.5 chunks per day |
| Chinese | 30 messages, ~5k CJK characters |
| FTS5 index (measured in phase 1) | 564 KB, built from scratch in 0.11 s |

Everything fits in memory many times over; the design optimizes for
simplicity, privacy and no new services, not for scale.

## 1. `message_chunks`

```sql
CREATE TABLE message_chunks (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    chunk_idx INTEGER NOT NULL,          -- 0.. within the message
    text_offset INTEGER NOT NULL,        -- where the chunk starts in messages.text
    text_len INTEGER NOT NULL,
    model TEXT NOT NULL,                 -- embedder id, e.g. "bge-m3@1024"
    embedding BLOB NOT NULL,             -- float32 little-endian, L2-normalized
    UNIQUE (message_id, chunk_idx, model)
);
CREATE INDEX idx_chunks_conv ON message_chunks(conversation_id);
```

The chunk text itself is **not** stored: `(text_offset, text_len)` slices it
out of `messages.text`, which stays the single source of truth (and is what
the snippet is cut from).

**Chunking.** Split each message into ~1,500-character pieces (~375 tokens,
inside every candidate model's 512-token window), breaking on paragraph
boundaries (`\n\n`), then on line breaks, then hard at the limit. A fenced
code block is kept whole when it fits. That gives ~1,900 chunks for today's
history.

**Pairing a user message with its reply.** A user message is usually short
("why does the deploy fail?") and means little alone; its reply carries the
content, and a search should hit the *turn*. So:

- A user message under ~300 chars is not embedded on its own. Its text is
  prepended to the **first chunk of the assistant reply that follows it**
  (`"Q: …\n\nA: …"`), and that chunk's `message_id` is the reply's.
- A longer user message (pasted logs, a spec) is chunked like any other
  message *and* still prefixed to the reply's first chunk.
- Subsequent chunks of a long reply are embedded alone.

Hits point at `message_id`, so clicking a semantic hit scrolls to the reply,
exactly as an FTS hit on it would.

## 2. The embedder

```python
class Embedder(Protocol):
    id: str            # stored in message_chunks.model; changes when the vectors would
    dim: int
    def embed_documents(self, texts: list[str]) -> np.ndarray: ...  # (n, dim), normalized
    def embed_query(self, text: str) -> np.ndarray: ...             # (dim,), normalized
```

Documents and queries are separate calls because e5-family models need
`passage: ` / `query: ` prefixes. The active embedder is picked by one `.env`
setting (`SEARCH_EMBEDDER=bge-m3`, `…=copilot-3-small`, or unset for
keyword-only search), so a machine without the model or the proxy simply
doesn't do phase 2.

### Options

| | Copilot `text-embedding-3-small` via LiteLLM | local **bge-m3** | local **multilingual-e5-small** |
| --- | --- | --- | --- |
| dim | 1536, or 512 with `dimensions` | 1024 | 384 |
| query latency | **~0.4 s** (410 ms median at 1536-d, 360 ms at 512-d) | tens of ms on CPU | a few ms on CPU |
| indexing today's ~1,900 chunks | ~20 s (300 chunks / 2.8 s per batch) | a few minutes on CPU | well under a minute |
| memory | none | ~1.2 GB (fp16 / int8 ONNX) | < 0.5 GB |
| zh + en | good | strong on both | decent |
| deps | none beyond `httpx` | `onnxruntime` (~50 MB) + model | same, smaller model |
| privacy | chat content goes to Copilot | stays local | stays local |
| cost | covered by the Copilot subscription | free | free |
| setup | **not configured yet**: the proxy's `/v1/embeddings` answers "no healthy deployments"; add `github_copilot/text-embedding-3-small` to the LiteLLM config | download model once | download model once |

**Recommendation:** implement the interface with both a LiteLLM client
(an OpenAI-compatible `/v1/embeddings` call) and an ONNX local model, and
default to **local**. Chat history is personal, the volume is tiny, and
search shouldn't break when the proxy is down. Default to bge-m3 if the
~1.2 GB resident memory is acceptable in the daemon, otherwise
multilingual-e5-small. The local model loads lazily, on the first search
or indexing batch, never at daemon start. Copilot stays a one-line switch
for a machine without RAM to spare. The ~0.4 s query cost is fine there
because FTS results are already instant.

## 3. Indexing

**Background worker.** `ConversationStore.add_message` stays as it is: no
embedding on the reply path. A small indexer thread started in
`runtime.start()` (next to the cron scheduler):

1. wakes on a `threading.Event` that `add_message` sets (and on a 60 s timer
   as a fallback);
2. selects messages with no chunk for the current `embedder.id`
   (`LEFT JOIN message_chunks … WHERE c.id IS NULL`), oldest first, in
   batches of ~64 chunks;
3. embeds a batch and inserts it in one transaction.

A user message waits for its reply before it's indexed: it's only embedded
as part of the reply's first chunk, unless it's long or no reply comes
within a few minutes. A failure (model missing, proxy down) logs once, backs
off, and retries; search degrades to FTS alone in the meantime.

**Deletes.** `delete_conversation` deletes its chunks along with its
messages, in the same transaction. Archiving keeps them: an archived
conversation is filtered at query time, as in FTS.

**Backfill.** The worker's normal loop is the backfill: on first start every
message is "missing", so it simply works through history. That takes ~20 s
via Copilot or a few minutes locally, in the background. A
`yuki-conductor search reindex` CLI runs the same loop in the foreground,
for testing and to force it.

**Changing the model.** `message_chunks.model` records which embedder made
each vector. Switching `SEARCH_EMBEDDER` changes `embedder.id`, so every row
counts as missing and the worker re-embeds history in the background. Old rows
are deleted once the new model covers them all. Until then, queries use only
rows matching the current model (vectors from different models can't be
compared), and FTS covers the gap. Changing the chunker bumps a version
suffix in the id (`bge-m3@1024/c2`), which triggers the same re-embed.

## 4. Vector search

**Brute force in numpy.** On first query, load every current-model row's
`(id, message_id, conversation_id, embedding)` into a `(n, dim)` float32
matrix; the indexer appends to it as it inserts, and deletes invalidate it.
A query is one matrix-vector product plus `argpartition` for the top
~100 chunks. At 2k–10k vectors that's under 5 ms. Archived conversations are
dropped by joining the top chunk ids back to `conversations`, as FTS does.

Memory equals vector storage (below): single-digit MB.

**When to move to sqlite-vec.** Only once the matrix stops being trivially
cheap, roughly 10⁵–10⁶ vectors, or when another process needs to query
without loading it. At ~4,200 chunks a year, that's decades away, unless
much bigger sources (Slack history, documents) are added. The swap is local
to the vector step: `message_chunks` would become a `vec0` virtual table, or
gain a companion one, and nothing above `search()` changes.

## 5. Ranking: one `search()`, two sources

`ConversationStore.search(query, limit)` keeps its signature and return type.
Inside:

1. **FTS** (today's query, unchanged): best message per conversation, by
   `bm25`. Take the top ~50.
2. **Vector**: embed the query, take the top ~100 chunks by cosine, and keep
   the best chunk per conversation.
3. **Reciprocal-rank fusion** per conversation:
   `score = Σ 1 / (k + rank_i)` over the lists it appears in, with `k = 60`.
   RRF uses ranks, not raw scores, so there's no need to calibrate bm25
   against cosine.
4. **Snippet / message_id**: from the FTS hit if the conversation has one,
   since it has highlighted terms. Otherwise from the best chunk: its slice
   of `messages.text`, trimmed to ~200 chars around the chunk start, with
   no `\x02…\x03` marks. The UI already renders an unmarked snippet as
   plain text.

`SearchHit.score` becomes the RRF score. It's still "higher is better", so
the API and the sidebar are unchanged. If no embedder is configured, the
index is empty, or the query embed fails, step 2 yields nothing, and the
result is FTS order, i.e. exactly phase 1.

A query that FTS can't search (only punctuation: `fts_query()` → `None`)
still goes to the vector side, so a pasted error string with no searchable
words can still match something.

## 6. Storage

Per vector: dim × 4 bytes (float32) plus ~60 bytes of row overhead.

| Embedder (dim) | Now (~1,900 chunks) | Per year (~4,200 chunks) | int8 per year |
| --- | --- | --- | --- |
| multilingual-e5-small (384) | ~3 MB | ~6.7 MB | ~1.9 MB |
| 3-small at 512-d | ~4 MB | ~9 MB | ~2.4 MB |
| bge-m3 (1024) | ~8 MB | ~17 MB | ~4.5 MB |
| 3-small at 1536-d | ~12 MB | ~26 MB | ~6.7 MB |

Add FTS5: 564 KB now (measured), ~1 MB/year. Even at 10× today's usage and
1536 dimensions it's ~250 MB/year. Storage isn't a constraint. RAM for the
in-memory matrix is the same as the vector column; the local model's weights
(0.24–1.2 GB) dominate.

## 7. Work breakdown

1. `embedding.py`: the `Embedder` protocol, a LiteLLM client, an ONNX
   local model, and the `SEARCH_EMBEDDER` setting. Tests use a fake
   deterministic embedder.
2. `message_chunks` plus a chunker (pure function, unit-tested on paragraph,
   code-fence and pairing cases).
3. The indexer thread, with `add_message` waking it, delete cascading to
   chunks, and `yuki-conductor search reindex`.
4. The vector source and RRF inside `search()`. Tests: fusion order, FTS-only
   fallback, archived exclusion, stale-model rows ignored.
5. Optional: `yuki-conductor search "<q>"` so agents can recall past
   conversations, plus more sources (task comments, cron results).
