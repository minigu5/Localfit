import assert from "node:assert/strict";


const base = "http://127.0.0.1:9000";
// Realtime Database emulator instances use the project's default RTDB name,
// not the bare project ID. Using `demo-localfit` here silently activated a
// second, rules-free namespace and made every authorization assertion
// meaningless.
const namespace = "demo-localfit-default-rtdb";

async function request(path, method, body) {
  const response = await fetch(`${base}/${path}.json?ns=${namespace}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // A denied emulator response may not have a JSON body.
  }
  return { ok: response.ok, status: response.status, payload };
}

const valid = {
  ram_gb: 24,
  vram_gb: 24,
  unified_memory: true,
  model_installed: "model-7B-Q4.gguf",
  model_repo_id: "org/model-7B-GGUF",
  model_size_bytes: 4 * 1024 ** 3,
  engine: "ollama",
  benchmark_version: 4,
  recorded_at: "2026-07-20T00:00:00+00:00",
  tokens_per_sec: 20.5,
  sample_count: 3,
  tokens_per_sec_min: 19.5,
  tokens_per_sec_max: 21.5,
  runtime_profile: "balanced",
  context_length: 4096,
  gpu_offload_percent: 100,
  cpu_threads: 8,
  num_batch: 512,
};

const created = await request("telemetry", "POST", valid);
assert.equal(created.ok, true, `valid schema 4 event was rejected (${created.status})`);
assert.equal(typeof created.payload?.name, "string");

const rawName = await request("telemetry", "POST", { ...valid, cpu: "Apple M5" });
assert.equal(rawName.ok, false, "schema 4 unexpectedly accepted a raw CPU name");

const unknown = await request("telemetry", "POST", { ...valid, unexpected: "value" });
assert.equal(unknown.ok, false, "telemetry unexpectedly accepted an unknown field");

const outOfRange = await request("telemetry", "POST", { ...valid, tokens_per_sec: 5000 });
assert.equal(outOfRange.ok, false, "telemetry unexpectedly accepted an out-of-range speed");

const overwrite = await request(`telemetry/${created.payload.name}`, "PUT", {
  ...valid,
  tokens_per_sec: 99,
});
assert.equal(overwrite.ok, false, "append-only telemetry unexpectedly allowed an overwrite");

const unrelated = await request("unrelated", "POST", { value: true });
assert.equal(unrelated.ok, false, "default-deny rule unexpectedly allowed another path");

const readable = await request("telemetry", "GET");
assert.equal(readable.ok, true, "public retraining read unexpectedly failed");

console.log("Firebase rules scenarios passed.");
