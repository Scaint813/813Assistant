const MAX_RESPONSE_BYTES = 4096;
const MAX_TIMEOUT_MS = 8000;
const PROVIDERS = Object.freeze({
  4: Object.freeze({ host: "api.ipify.org", url: "https://api.ipify.org?format=json" }),
  6: Object.freeze({ host: "api6.ipify.org", url: "https://api6.ipify.org?format=json" }),
});

class InvalidResponse extends Error {}

function publicIpv4(value) {
  const parts = value.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^(0|[1-9]\d{0,2})$/.test(part))) return null;
  const [a, b, c, d] = parts.map(Number);
  if ([a, b, c, d].some((part) => part > 255)) return null;
  if (
    a === 0 || a === 10 || a === 127 || a >= 224
    || (a === 100 && b >= 64 && b <= 127)
    || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && b === 168)
    || (a === 192 && b === 0 && c === 0 && d !== 9 && d !== 10)
    || (a === 192 && b === 0 && c === 2)
    || (a === 192 && b === 88 && c === 99)
    || (a === 198 && (b === 18 || b === 19))
    || (a === 198 && b === 51 && c === 100)
    || (a === 203 && b === 0 && c === 113)
  ) return null;
  return parts.join(".");
}

function publicIpv6(value) {
  if (!/^[0-9a-fA-F:.]+$/.test(value)) return null;
  let canonical;
  try {
    // The URL parser validates IPv6 and serializes equivalent forms identically.
    canonical = new URL(`https://[${value}]/`).hostname.slice(1, -1);
  } catch (_error) {
    return null;
  }
  const [left, right] = canonical.split("::");
  const start = left ? left.split(":").map((part) => parseInt(part, 16)) : [];
  const end = right ? right.split(":").map((part) => parseInt(part, 16)) : [];
  const words = right === undefined ? start : [...start, ...Array(8 - start.length - end.length).fill(0), ...end];
  // Public unicast allocations are in 2000::/3. This also excludes mapped IPv4,
  // local, multicast, unspecified, discard-only, and translation-only addresses.
  if (words[0] < 0x2000 || words[0] > 0x3fff) return null;
  if (
    (words[0] === 0x2001 && words[1] === 0x0db8)
    || (words[0] === 0x3fff && words[1] < 0x1000)
    || words[0] === 0x2002
  ) return null;
  if (words[0] === 0x2001 && words[1] < 0x0200) {
    // IETF protocol assignments are special-purpose unless explicitly global.
    const anycast = words[1] === 1 && words.slice(2, 7).every((word) => word === 0)
      && [1, 2, 3].includes(words[7]);
    const amt = words[1] === 3;
    const as112 = words[1] === 4 && words[2] === 0x0112;
    const droneIdentifiers = words[1] >= 0x0030 && words[1] <= 0x003f;
    if (!anycast && !amt && !as112 && !droneIdentifiers) return null;
  }
  return canonical;
}

export function normalizePublicIp(value, family) {
  if (typeof value !== "string" || !value || value.length > 64 || value !== value.trim()) return null;
  if (family === 4) return publicIpv4(value);
  if (family === 6) return publicIpv6(value);
  return null;
}

export async function probeExternalIp(family, { signal, fetchImpl = globalThis.fetch, timeoutMs = MAX_TIMEOUT_MS } = {}) {
  if (family !== 4 && family !== 6) throw new TypeError("Unsupported IP family");
  const provider = PROVIDERS[family];
  const controller = new AbortController();
  let reader = null;
  let rejectAbort;
  const aborted = new Promise((_resolve, reject) => { rejectAbort = reject; });
  const cancelStream = (stream) => {
    try {
      Promise.resolve(stream?.cancel()).catch(() => {});
    } catch (_error) {
      // A released stream is already finished.
    }
  };
  const cancelReader = () => cancelStream(reader);
  const abort = () => {
    controller.abort();
    cancelReader();
    rejectAbort(new Error("Probe unavailable"));
  };
  const timeout = setTimeout(abort, Number.isFinite(timeoutMs) && timeoutMs >= 0
    ? Math.min(timeoutMs, MAX_TIMEOUT_MS) : MAX_TIMEOUT_MS);
  const result = (observedIp, error) => ({
    family,
    observed_ip: observedIp,
    checked_at: new Date().toISOString(),
    provider: provider.host,
    error,
  });

  const request = async () => {
    if (signal?.aborted) throw new Error("Probe unavailable");
    const response = await fetchImpl(provider.url, {
      method: "GET",
      mode: "cors",
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
      redirect: "error",
      signal: controller.signal,
    });
    if (controller.signal.aborted) throw new Error("Probe unavailable");
    if (!response?.ok || response.redirected || ["opaque", "opaqueredirect"].includes(response.type)) {
      cancelStream(response?.body);
      throw new Error("Probe unavailable");
    }
    if (!response.body?.getReader) throw new InvalidResponse();
    reader = response.body.getReader();
    try {
      const contentLength = response.headers?.get?.("content-length");
      if (contentLength && /^\d+$/.test(contentLength) && Number(contentLength) > MAX_RESPONSE_BYTES) {
        cancelReader();
        throw new InvalidResponse();
      }
      const bytes = new Uint8Array(MAX_RESPONSE_BYTES);
      let size = 0;
      while (true) {
        const chunk = await reader.read();
        if (controller.signal.aborted) throw new Error("Probe unavailable");
        if (chunk.done) break;
        if (!(chunk.value instanceof Uint8Array) || size + chunk.value.byteLength > MAX_RESPONSE_BYTES) {
          cancelReader();
          throw new InvalidResponse();
        }
        bytes.set(chunk.value, size);
        size += chunk.value.byteLength;
      }
      let payload;
      try {
        payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, size)));
      } catch (_error) {
        throw new InvalidResponse();
      }
      const address = payload && typeof payload === "object" && !Array.isArray(payload)
        ? normalizePublicIp(payload.ip, family) : null;
      if (!address) throw new InvalidResponse();
      return address;
    } finally {
      try { reader.releaseLock(); } catch (_error) { /* Abort can leave a read pending. */ }
      reader = null;
    }
  };

  try {
    signal?.addEventListener("abort", abort, { once: true });
    return result(await Promise.race([request(), aborted]), null);
  } catch (error) {
    controller.abort();
    return result(null, error instanceof InvalidResponse ? "invalid_response" : "unavailable");
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}
