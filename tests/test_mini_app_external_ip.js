"use strict";

const assert = require("node:assert/strict");

function response(payload, options = {}) {
  return new Response(typeof payload === "string" ? payload : JSON.stringify(payload), options);
}

function pendingBody() {
  let cancelled = false;
  return {
    response: { ok: true, body: { getReader: () => ({
      read: () => new Promise(() => {}),
      cancel: () => { cancelled = true; },
      releaseLock: () => {},
    }) } },
    isCancelled: () => cancelled,
  };
}

async function run() {
  const { normalizePublicIp, probeExternalIp } = await import("../bot/miniapp/external_ip.mjs");
  for (const ip of ["8.8.8.8", "1.1.1.1", "89.22.228.86", "100.128.0.1", "192.0.0.9"]) {
    assert.equal(normalizePublicIp(ip, 4), ip);
  }
  for (const ip of [
    "0.0.0.0", "0.1.2.3", "10.0.0.1", "100.64.0.1", "100.127.255.255", "127.0.0.1",
    "169.254.1.1", "172.16.0.1", "172.31.255.255", "192.168.1.1", "192.0.0.1", "192.0.2.1",
    "192.88.99.1", "198.18.0.1", "198.19.255.255", "198.51.100.1", "203.0.113.1",
    "224.0.0.1", "240.0.0.1", "255.255.255.255", "8.8.8", "8.8.8.256", "08.8.8.8",
    "0x8.8.8.8", "8.8.8.8:443", "8.8.8.8\n", " 8.8.8.8", "https://8.8.8.8", "<script>",
    "2001:4860:4860::8888", "8.8.8.8/32", "8.8.8.8@evil.example",
  ]) assert.equal(normalizePublicIp(ip, 4), null, ip);
  assert.equal(normalizePublicIp("2A0B:4140:5020:0000:0000:0000:0000:0002", 6), "2a0b:4140:5020::2");
  assert.equal(normalizePublicIp("2a0b:4140:5020::2", 6), "2a0b:4140:5020::2");
  assert.equal(normalizePublicIp("2001:4860:4860::8888", 6), "2001:4860:4860::8888");
  assert.equal(normalizePublicIp("2606:4700:4700:0:0:0:0:1111", 6), "2606:4700:4700::1111");
  for (const ip of [
    "::", "::1", "::ffff:8.8.8.8", "fc00::1", "fd7a:115c:a1e0::1", "fe80::1", "fec0::1",
    "ff02::1", "100::1", "64:ff9b::808:808", "2001:db8::1", "3fff::1", "3fff:fff::1",
    "2001::1", "2001:2::1", "2001:10::1", "2001:20::1", "2002:808:808::1",
    "fe80::1%en0", "[2001:4860::1]", "2001:4860::1%25en0", "2001:4860:::1",
    "2001:4860::gggg", "2001:4860::1\n", " 2001:4860::1", "8.8.8.8", "2001:4860::1/64",
  ]) assert.equal(normalizePublicIp(ip, 6), null, ip);
  for (const value of [null, undefined, 1234, {}, [], true]) {
    assert.equal(normalizePublicIp(value, 4), null);
    assert.equal(normalizePublicIp(value, 6), null);
  }
  assert.equal(normalizePublicIp("8.8.8.8", "4"), null);

  let calls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = () => { calls += 1; throw new Error("Unexpected automatic request"); };
  try {
    await import("../bot/miniapp/external_ip.mjs?no-automatic-request");
    assert.equal(calls, 0, "importing the module must not start an external request");
  } finally {
    globalThis.fetch = originalFetch;
  }

  for (const family of [4, 6]) {
    const expectedIp = family === 4 ? "89.22.228.86" : "2a0b:4140:5020::2";
    const provider = family === 4 ? "api.ipify.org" : "api6.ipify.org";
    let request;
    const result = await probeExternalIp(family, {
      fetchImpl: async (...args) => { request = args; return response({ ip: expectedIp }); },
      // Unrecognized options must never reach the provider.
      headers: { Authorization: "tma telegram-secret", Cookie: "session=secret" },
      url: "https://untrusted.example/",
    });
    assert.equal(request[0], `https://${provider}?format=json`);
    const { signal, ...options } = request[1];
    assert.ok(signal instanceof AbortSignal);
    assert.deepEqual(options, {
      method: "GET", mode: "cors", credentials: "omit", cache: "no-store",
      referrerPolicy: "no-referrer", redirect: "error",
    });
    assert.doesNotMatch(JSON.stringify(request), /telegram-secret|session=secret|untrusted/);
    assert.deepEqual(Object.keys(result).sort(), ["checked_at", "error", "family", "observed_ip", "provider"]);
    assert.equal(result.family, family);
    assert.equal(result.provider, provider);
    assert.equal(result.observed_ip, expectedIp);
    assert.equal(result.error, null);
    assert.equal(new Date(result.checked_at).toISOString(), result.checked_at);
  }

  const expanded = await probeExternalIp(6, {
    fetchImpl: async () => response({ ip: "2A0B:4140:5020:0000:0000:0000:0000:0002" }),
  });
  assert.equal(expanded.observed_ip, "2a0b:4140:5020::2");

  for (const payload of [
    "", "not json", "null", "[]", '"8.8.8.8"', "{}", '{"ip":null}', '{"ip":8}',
    '{"ip":"10.1.2.3"}', '{"ip":"2001:4860:4860::8888"}',
    '{"ip":"<img src=x onerror=alert(1)>"}', '{"ip":"8.8.8.8\\n"}',
    `{"ip":"8.8.8.8","padding":"${"x".repeat(4096)}"}`,
  ]) {
    const result = await probeExternalIp(4, { fetchImpl: async () => response(payload) });
    assert.equal(result.error, "invalid_response", payload.slice(0, 80));
    assert.equal(result.observed_ip, null);
  }
  const wrongIpv6 = await probeExternalIp(6, { fetchImpl: async () => response({ ip: "8.8.8.8" }) });
  assert.equal(wrongIpv6.error, "invalid_response");
  const invalidUtf8 = await probeExternalIp(4, { fetchImpl: async () => new Response(new Uint8Array([0xff])) });
  assert.equal(invalidUtf8.error, "invalid_response");
  const boundaryPayload = '{"ip":"8.8.8.8"}';
  const atLimit = await probeExternalIp(4, {
    fetchImpl: async () => response(boundaryPayload + " ".repeat(4096 - boundaryPayload.length)),
  });
  assert.equal(atLimit.observed_ip, "8.8.8.8");
  const fragmented = await probeExternalIp(4, {
    fetchImpl: async () => new Response(new ReadableStream({
      start(controller) {
        for (const text of ['{"', 'ip":"8.', '8.8.8"}']) controller.enqueue(new TextEncoder().encode(text));
        controller.close();
      },
    })),
  });
  assert.equal(fragmented.observed_ip, "8.8.8.8");
  let oversizedCancelled = false;
  let oversizedReads = 0;
  const oversizedStream = await probeExternalIp(4, {
    fetchImpl: async () => ({ ok: true, body: { getReader: () => ({
      read: async () => { oversizedReads += 1; return { done: false, value: new Uint8Array(2049) }; },
      cancel: () => { oversizedCancelled = true; },
      releaseLock: () => {},
    }) } }),
  });
  assert.equal(oversizedStream.error, "invalid_response");
  assert.equal(oversizedReads, 2);
  assert.equal(oversizedCancelled, true);
  const declaredOversized = pendingBody();
  declaredOversized.response.headers = new Headers({ "content-length": "4097" });
  const declaredOversizedResult = await probeExternalIp(4, { fetchImpl: async () => declaredOversized.response });
  assert.equal(declaredOversizedResult.error, "invalid_response");
  assert.equal(declaredOversized.isCancelled(), true);

  for (const fetchImpl of [
    async () => { throw new TypeError("CORS blocked telegram-secret"); },
    () => { throw new Error("Network failure"); },
    async () => response({ ip: "8.8.8.8" }, { status: 503 }),
    async () => response({ ip: "8.8.8.8" }, { status: 302 }),
    async () => ({ ok: true, redirected: true }),
    async () => ({ ok: true, type: "opaqueredirect" }),
    async () => ({ ok: true, type: "opaque" }),
  ]) {
    const result = await probeExternalIp(4, { fetchImpl });
    assert.equal(result.error, "unavailable");
    assert.equal(result.observed_ip, null);
    assert.doesNotMatch(JSON.stringify(result), /telegram-secret|Network failure|CORS/);
  }
  let fetchSignal;
  let errorBodyCancelled = false;
  const errorStream = new Response(new ReadableStream({
    cancel() { errorBodyCancelled = true; },
  }), { status: 503 });
  const errorStreamResult = await probeExternalIp(4, {
    fetchImpl: async (_url, options) => { fetchSignal = options.signal; return errorStream; },
  });
  assert.equal(errorStreamResult.error, "unavailable");
  assert.equal(errorBodyCancelled, true);
  assert.equal(fetchSignal.aborted, true);

  const timedOut = await probeExternalIp(4, {
    timeoutMs: 5,
    fetchImpl: (_url, { signal }) => { fetchSignal = signal; return new Promise(() => {}); },
  });
  assert.equal(timedOut.error, "unavailable");
  assert.equal(fetchSignal.aborted, true);
  const slowBody = pendingBody();
  const bodyTimeout = await probeExternalIp(4, { timeoutMs: 5, fetchImpl: async () => slowBody.response });
  assert.equal(bodyTimeout.error, "unavailable");
  assert.equal(slowBody.isCancelled(), true);

  const fetchCaller = new AbortController();
  const cancelledFetch = probeExternalIp(4, {
    signal: fetchCaller.signal,
    fetchImpl: (_url, options) => { fetchSignal = options.signal; return new Promise(() => {}); },
  });
  fetchCaller.abort();
  assert.equal((await cancelledFetch).error, "unavailable");
  assert.equal(fetchSignal.aborted, true);

  const caller = new AbortController();
  const cancelledBody = pendingBody();
  const cancelledRequest = probeExternalIp(6, {
    signal: caller.signal,
    fetchImpl: async (_url, options) => { fetchSignal = options.signal; return cancelledBody.response; },
  });
  await new Promise((resolve) => setImmediate(resolve));
  caller.abort();
  assert.equal((await cancelledRequest).error, "unavailable");
  assert.equal(fetchSignal.aborted, true);
  assert.equal(cancelledBody.isCancelled(), true);
  const alreadyAborted = new AbortController();
  alreadyAborted.abort();
  calls = 0;
  const skipped = await probeExternalIp(4, {
    signal: alreadyAborted.signal,
    fetchImpl: async () => { calls += 1; return response({ ip: "8.8.8.8" }); },
  });
  assert.equal(skipped.error, "unavailable");
  assert.equal(calls, 0);
  for (const family of [0, 5, "4", null, undefined, "https://untrusted.example/"]) {
    await assert.rejects(probeExternalIp(family), /Unsupported IP family/);
  }
  console.log("Mini App external IP probe checks passed.");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
