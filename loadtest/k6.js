import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const gatewayErrors = new Rate("gateway_errors");
const successfulRequests = new Counter("gateway_successful_requests");
const gatewayDuration = new Trend("gateway_duration", true);

const baseUrl = __ENV.BASE_URL || "http://gateway:8000";
const apiKey = __ENV.API_KEY;
const model = __ENV.MODEL || "load-test-model";
const thinkTime = Number(__ENV.THINK_TIME_SECONDS || "0.1");

if (!apiKey) {
  throw new Error("API_KEY is required");
}

export const options = {
  vus: Number(__ENV.VUS || "10"),
  duration: __ENV.DURATION || "30s",
  thresholds: {
    http_req_failed: [__ENV.ERROR_THRESHOLD || "rate<0.01"],
    gateway_errors: [__ENV.ERROR_THRESHOLD || "rate<0.01"],
    http_req_duration: [__ENV.P95_THRESHOLD || "p(95)<1000"],
    checks: ["rate>0.99"],
  },
};

export function setup() {
  const response = http.get(`${baseUrl}/health`);
  if (response.status !== 200) {
    throw new Error(`Gateway health check failed: ${response.status}`);
  }
}

export default function () {
  const payload = JSON.stringify({
    model,
    messages: [{ role: "user", content: `load test request from vu ${__VU}` }],
    max_tokens: 32,
    stream: false,
  });
  const response = http.post(`${baseUrl}/v1/chat/completions`, payload, {
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    tags: { endpoint: "chat_completions" },
    timeout: __ENV.REQUEST_TIMEOUT || "10s",
  });

  const ok = check(response, {
    "status is 200": (r) => r.status === 200,
    "has request id": (r) => Boolean(r.headers["X-Request-Id"]),
    "has usage": (r) => {
      try {
        return JSON.parse(r.body).usage.total_tokens > 0;
      } catch (_) {
        return false;
      }
    },
  });

  gatewayErrors.add(!ok);
  gatewayDuration.add(response.timings.duration);
  if (ok) successfulRequests.add(1);
  if (thinkTime > 0) sleep(thinkTime);
}
