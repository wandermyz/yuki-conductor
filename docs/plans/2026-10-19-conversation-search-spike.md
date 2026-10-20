# Spike: semantic conversation search

## What we have today

Web chat lives in the workspace DB, `messages(id, conversation_id, role, text,
attachments_json, created_at)` (`conversation_store.py`). Slack threads are
*not* stored — only `sessions` maps thread → Claude session — so v1 covers web
conversations; Slack (and task comments in `task_comments`) can be added
later as extra sources.

Measured on the live DB (2026-10-19, ~165 days of use):

| | |
| --- | --- |
| conversations | 215 |
| messages | 1,559 (785 user / 774 assistant) |
| text | 1.04 MB (avg 667 chars, max 26 KB; assistant = 89%) |
| chunks at ~1,500 chars (~375 tokens) | 1,901 |
| rate | ~9.5 messages / ~6.3 KB text / ~11.5 chunks per day |
| whole DB file | 3.4 MB |

The SQLite build has FTS5 enabled.

## Recommended design

Hybrid search inside the existing SQLite DB — no new service.

1. **Keyword: FTS5.** `messages_fts` external-content table over
   `messages.text`, kept in sync by triggers. Zero deps, instant, and good
   for exact names, error strings, paths — which is much of what one searches
   chat history for.
2. **Semantic: embeddings in a `message_chunks` table** —
   `(message_id, conversation_id, chunk_idx, text_offset, embedding BLOB,
   model)`. Messages are split into ~1,500-char chunks (paragraph-aligned);
   short user messages are embedded together with the reply they prompted, so
   a turn is the unit of meaning.
3. **Vector search: brute force in numpy.** At ~2k–10k vectors a full
   cosine scan is a single matrix multiply (<5 ms). Load the matrix lazily,
   append on insert. `sqlite-vec` is the upgrade path if it ever reaches
   ~10⁵–10⁶ vectors; not needed now.
4. **Ranking:** reciprocal-rank fusion of FTS5 and vector hits, grouped by
   conversation (best chunk = snippet). Archived conversations excluded.
5. **Indexing:** a background worker embeds new messages after
   `ConversationStore` writes them (never on the reply path), plus a one-off
   backfill command. Re-embed everything if the model changes (`model`
   column tells which rows are stale).
6. **API/UI:** `GET /api/search?q=` → `[{conversation_id, title, message_id,
   snippet, score}]`; a search box at the top of the chat sidebar that jumps
   to `#chat/<id>` and scrolls to the message. Optionally a
   `yuki-conductor search` CLI so agents can recall past conversations.

### Embedding model options

| Option | Dim | Cost | Notes |
| --- | --- | --- | --- |
| Local `bge-small-en-v1.5` / `all-MiniLM-L6-v2` (fastembed/ONNX) | 384 | free | ~100 MB model, CPU-fine at this volume; adds onnxruntime dep |
| Local `nomic-embed-text` / `bge-base` | 768 | free | better quality, ~2–3× slower |
| OpenAI `text-embedding-3-small` | 1536 (truncatable to 512) | ~$0.02 / 1M tokens → backfill ≈ **$0.015**, then ≈ $0.03/yr | network dep; sends chat content to a third party |
| Voyage `voyage-3-lite` | 512 | similar | Anthropic-recommended provider |

Recommendation: start with a local 384-dim model (private, free, no keys —
chat history contains personal data), behind a small `embed(texts)`
interface so a hosted model can be swapped in.

### Bilingual (Chinese + English) local models

Only 30 of 1,559 messages contain Chinese (about 5k CJK characters), so this
needs a mixed-language model, not a Chinese-only one. Figures below are from
the model cards, not measured here. Memory is the weights plus about 0.3–0.5 GB
of runtime at batch 32 × 512 tokens. This machine has no NVIDIA GPU, so the
model would run on CPU.

| Model | Params | Dim | Max tokens | Weights fp32 / fp16 | zh+en |
| --- | --- | --- | --- | --- | --- |
| BAAI/bge-large-zh-v1.5 | 326M | 1024 | 512 | 1.3 GB / 0.65 GB | Chinese-first; English noticeably weaker |
| BAAI/bge-m3 | 568M | 1024 | 8192 | 2.2 GB / 1.1 GB | strong both; also gives sparse vectors |
| intfloat/multilingual-e5-base | 278M | 768 | 512 | 1.1 GB / 0.55 GB | good both |
| intfloat/multilingual-e5-small | 118M | 384 | 512 | 0.47 GB / 0.24 GB | decent, fastest |
| Qwen/Qwen3-Embedding-0.6B | 600M | 1024 (adjustable) | 32k | 2.4 GB / 1.2 GB | strong both, top of MTEB for its size |
| BAAI/bge-small-zh-v1.5 | 24M | 512 | 512 | 0.1 GB | Chinese-first, small |

On **bge-large-zh-v1.5**: it works, but it's tuned for Chinese-only text
and English is ~89% of this history, so it's a poor fit. A same-sized
multilingual model is better. My pick is **bge-m3** (best quality on mixed
text, long context, about 1.2 GB RAM in fp16 or int8 ONNX). The lighter
option is **multilingual-e5-small**, which needs under 0.5 GB of RAM. On CPU,
indexing all existing history should take a few minutes with bge-m3 and well
under a minute with e5-small. A single query takes tens of ms. At 1024
dimensions, vectors take about 8 MB now and 17 MB a year.

### GitHub Copilot endpoint (measured 2026-10-19)

The local LiteLLM proxy (`localhost:4000`) has **no embedding models
configured**: `/v1/embeddings` returns "no healthy deployments". Copilot
itself does serve them. Its `/models` endpoint lists `text-embedding-3-small`
(also `-inference`) and `text-embedding-ada-002`, with up to 512 inputs per
request and `dimensions` supported on 3-small. To use one, add it to the
LiteLLM config as `github_copilot/text-embedding-3-small`.

Latency, called directly with LiteLLM's Copilot token (median of 7 warm calls):

| Request | Latency |
| --- | --- |
| 1 query, 3-small, 1536-d | ~410 ms (400–540 ms) |
| 1 query, 3-small, 512-d | ~360 ms |
| 1 query, ada-002 | ~440 ms |
| batch of 32 chunks (4k tokens) | 0.95 s |
| batch of 100 chunks (15k tokens) | 1.7 s |
| batch of 300 chunks (67k tokens) | 2.8 s |

So indexing all existing history (~1,900 chunks) takes about 20 s, and each
search pays about 0.4 s to embed the query. That's fine for a search box,
and the FTS results can be shown first while it runs. It's covered by the
Copilot subscription, so there's no per-token bill. The trade-offs are that
it needs the proxy or token to be up, and that chat content goes to Copilot.
With the 512-d variant, storage is about 4 MB now and 9 MB a year.

## Storage cost

Per vector: dim × 4 bytes (float32), plus ~60 bytes row overhead.

| Model dim | Now (1,901 chunks) | Per year (~4,200 chunks) | int8-quantized / yr |
| --- | --- | --- | --- |
| 384 | **~3 MB** | ~6.7 MB | ~1.9 MB |
| 768 | ~6 MB | ~13 MB | ~3.5 MB |
| 1536 | ~12 MB | ~26 MB | ~6.7 MB |

FTS5 index: ~30–60% of indexed text → **~0.5 MB now, ~1 MB/yr**.

Even at 10× today's usage and 1536 dims, it's ~250 MB/yr; at the recommended
384 dims it's single-digit MB per year. RAM for the in-memory matrix equals
the vector size above. Storage is not a constraint; the real costs are the
onnxruntime dependency (~50 MB install + ~100 MB model) or, for a hosted
model, the privacy trade-off.

## Suggested phases

Keyword search first doesn't box us in. Semantic search is purely additive:
a new `message_chunks` table, a background embedder plus backfill, and a
second ranked list merged into `/api/search` with reciprocal-rank fusion.
The API shape, the UI and the FTS table stay the same. To keep it cheap,
have phase 1 return results as `{conversation_id, message_id, snippet,
score}` from one `search()` function, so phase 2 only adds a source behind it.

1. FTS5 keyword search + `/api/search` + sidebar box (small, immediately useful).
2. Embeddings + hybrid ranking + backfill.
3. More sources: task comments, cron results; Slack would first need its
   messages persisted.
