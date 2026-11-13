# Testing the AI check with the local Ollama model

How to check that the AI syllabus check works with the Ollama model, and how to measure how many tokens it uses.
Run all commands from `syllabus-ai-backend/`.

**Where things stood on 2026-11-05:** the backend is running, but every check has failed. Requests timed out
because the model is too slow in its current setup (see step 1). Do steps 0–1 first, or the full check will fail again.

**Update 2026-11-09:** the backend now sets the context itself (`LLM_NUM_CTX=16384`, Ollama's native API), so
step 1a is no longer required. Measured on a 9.4k-character real syllabus with `qwen3-8:latest`:

| | Before | After |
|---|---|---|
| Model on the server | 262k context, 33.1 GB, 4.6 GB of it on the CPU | 16k context, 15.4 GB, all on the GPU |
| Output speed | ~15 tokens/s | ~54 tokens/s |
| One criteria request | ~45–50 s | 10–18 s (prompt read from cache in <1 s) |
| Full check, nothing else running | ~7–8 min | ~2–3 min expected |

The full check measured ~4 min, but two old-code checks were sharing the server at the time. With
`OLLAMA_NUM_PARALLEL` on the server and `LLM_MAX_CONCURRENCY` to match, it gets faster again (README, "Скорость
проверки с Ollama"). Note that `duration_ms` in traces no longer includes the wait for a free slot; that is `queued_ms`.

---

## 0. Make sure you can reach the Ollama server

The model runs on `192.168.13.79`. Your Mac must be on the same network (office Wi-Fi or VPN).

```bash
curl -s -m 5 http://192.168.13.79:11434/api/tags | python3 -m json.tool | grep '"name"'
```

You should see a list of models, including `qwen3-8:latest`. If the command hangs or prints nothing, you can't
reach the server: change network or connect to the VPN before going further.

---

## 1. Fix the speed problems

Yesterday's check failed because of three problems:

| Problem | Effect | Fix |
|---|---|---|
| Ollama loads the model with a 262k-token context | 33 GB, part of it runs on the CPU → very slow | Limit the context (1a) |
| The backend sends 4 requests at once, the server handles 1 | Requests wait in a queue and time out | `LLM_MAX_CONCURRENCY=1` (1b) |
| 180 s timeout, with up to 3 retries | Slow requests fail, and retries make a broken check take hours | Longer timeout, fewer retries (1b) |

### 1a. Limit the model's context (on the Ollama server)

You need access to the server, or ask whoever runs it. The server looks shared (`gemma4-26b` was loaded there too).

**Option A (recommended, affects no one else):** create a separate copy of the model with a smaller context.
```bash
# run on 192.168.13.79
printf 'FROM qwen3-8:latest\nPARAMETER num_ctx 16384\n' > Modelfile
ollama create qwen3-8-16k -f Modelfile
```
Then use `LLM_MODEL=qwen3-8-16k` in step 1b. 16k is enough for the demo syllabi. For real syllabi
(20–30k characters), use `32768` if the GPU has room.

**Option B (whole server):** set `OLLAMA_CONTEXT_LENGTH=16384` in Ollama's environment and restart Ollama.
This changes every model for every user of the server.

**Check that the model now fits on the GPU.** Send it any request first so it loads, then run:
```bash
curl -s http://192.168.13.79:11434/api/ps | python3 -c "
import json,sys
for m in json.load(sys.stdin)['models']:
    print(m['name'], 'total', round(m['size']/1e9,1), 'GB | on GPU', round(m['size_vram']/1e9,1), 'GB')"
```
"total" and "on GPU" should be equal. If "on GPU" is smaller, part of the model is still running on the CPU.

### 1b. Change the backend `.env`

```env
LLM_MODEL=qwen3-8-16k        # or keep qwen3-8:latest if you used option B
LLM_MAX_CONCURRENCY=1
LLM_TIMEOUT_S=600
LLM_MAX_RETRIES=1            # fail fast while testing; set it back to 3 afterwards
```

Apply the changes. The containers are recreated with the new settings:
```bash
docker compose up -d
```

---

## 2. Measure how fast the model is (≈30 s)

This tells you whether a full check can finish at all, before you spend time running one.

```bash
curl -s http://192.168.13.79:11434/api/generate -d '{"model":"qwen3-8-16k","prompt":"Write 200 words about university syllabi.","stream":false}' \
| python3 -c "import json,sys; r=json.load(sys.stdin); print(r['eval_count'], 'tokens,', round(r['eval_count']/(r['eval_duration']/1e9),1), 'tokens/s')"
```

One check request writes about **1,600 tokens**, so **seconds per request ≈ 1600 ÷ tokens/s**.
- 20 tokens/s ≈ 80 s per request ≈ 12 min for a whole check (9 requests, one at a time). That's fine.
- Under ~3 tokens/s, a request takes longer than 600 s, so it will time out. Fix the GPU problem from step 1a first.

---

## 3. Check that the backend can talk to the model (≈10 s–2 min)

```bash
docker compose exec -T worker python -c "
import asyncio; from app.ai.llm import get_llm
print(asyncio.run(get_llm().complete_json(purpose='smoke', system='Return JSON.', user='Is 2+2=4?',
  schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}, trace=False)))"
```

You should see `{'ok': True}`. If you get an error instead, check `LLM_BASE_URL` (it must end in `/v1`) and `LLM_MODEL` in `.env`.

---

## 4. Start a full check

Only the course lead can start a check. For the demo course Machine Learning v2, that's `lee@demo.narxoz.kz`.

```bash
RUN_ID=$(curl -s -X POST -H "Authorization: Bearer stub:lee@demo.narxoz.kz" \
  localhost:8080/api/v1/versions/c233237b-0512-4326-aedd-812115c10613/runs \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo $RUN_ID
```

Keep this terminal open: the commands below use `$RUN_ID`.
(You can also do this in the frontend at http://localhost:3000: log in as `lee@demo.narxoz.kz` and start a check
on Machine Learning. Then copy the check's ID from the API or the database.)

---

## 5. Watch it run

**Status** (`Queued` → `Processing` → `Completed` or `Failed`):
```bash
curl -s -H "Authorization: Bearer stub:lee@demo.narxoz.kz" localhost:8080/api/v1/runs/$RUN_ID \
| python3 -c "import json,sys; r=json.load(sys.stdin); print(r['status'], '| model:', r.get('model_version'))"
```

**Each model request** (tokens, time, error):
```bash
docker compose exec -T mongo mongosh syllabus --quiet --eval "
db.llm_traces.find({run_id: UUID('$RUN_ID')}).sort({created_at:1}).forEach(t => print(
  t.purpose.padEnd(28), 'in=' + t.prompt_tokens, 'out=' + t.completion_tokens,
  Math.round(t.duration_ms/1000) + 's', t.error ? 'ERROR: ' + String(t.error).slice(0,60) : 'ok'))"
```

**Live worker log** (Ctrl+C to stop watching; the check keeps running):
```bash
docker compose logs -f worker | grep -E "LLM|failed|succeeded"
```

---

## 6. How to tell it worked

- Status is `Completed`.
- `model_version` shows your model, not `fake-evaluator`.
- Every line in the request list says `ok`.
- The results for all 49 criteria show up in the frontend, or here:
  ```bash
  curl -s -H "Authorization: Bearer stub:lee@demo.narxoz.kz" \
    localhost:8080/api/v1/versions/c233237b-0512-4326-aedd-812115c10613/runs/latest | python3 -m json.tool | less
  ```

If it says `Failed`, look at the request list from step 5. `APITimeoutError` means the model is still too slow
(go back to steps 1–2). Any other error message usually points straight at the problem.

---

## 7. Count the tokens (for the cost estimate)

Totals for one check:
```bash
docker compose exec -T mongo mongosh syllabus --quiet --eval "
const r = db.llm_traces.aggregate([{\$match: {run_id: UUID('$RUN_ID')}}, {\$group: {_id: null,
  calls: {\$sum: 1}, failed: {\$sum: {\$cond: [{\$gt: ['\$error', null]}, 1, 0]}},
  input: {\$sum: '\$prompt_tokens'}, output: {\$sum: '\$completion_tokens'}, ms: {\$sum: '\$duration_ms'}}}]).toArray()[0];
print('calls', r.calls, '| failed', r.failed, '| input tokens', r.input, '| output tokens', r.output, '| model time', Math.round(r.ms/60000) + ' min')"
```

Things to keep in mind:
- `calls` includes retries. A clean check has **9 calls and 0 failed**. Only use the numbers from clean checks.
- Output tokens include the model's hidden "thinking", about 1,000 of the ~1,600 per request.
- Every request sends the whole syllabus text again, so input tokens grow with syllabus length. The demo syllabus
  is short (~1,900 characters). For a realistic number, upload a real full-length syllabus and run the same
  totals command on its check.

Write down for each check: syllabus length (characters), input tokens, output tokens, and total time. With 2–3
syllabi of different lengths, you can estimate tokens for any syllabus.

**Estimate from yesterday (one successful request only):** about 9k input and 14k output tokens per check on the demo
syllabus, and roughly 50–80k input tokens for a 20–30k-character syllabus.

---

## After testing

Set `LLM_MAX_RETRIES=3` again in `.env` and run `docker compose up -d`.
