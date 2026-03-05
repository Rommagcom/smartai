import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000/api/v1';
const USERNAME = __ENV.K6_USERNAME || `k6_user_${Date.now()}`;
const PASSWORD = __ENV.K6_PASSWORD || 'K6SmokePass123!';
const THINK_TIME_SECONDS = Number(__ENV.K6_THINK_TIME_SECONDS || '0.2');

export const options = {
  scenarios: {
    chat_basic: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '30s', target: 10 },
        { duration: '2m', target: 10 },
        { duration: '30s', target: 0 },
      ],
      exec: 'chatBasic',
    },
    worker_burst: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '20s', target: 5 },
        { duration: '90s', target: 5 },
        { duration: '20s', target: 0 },
      ],
      exec: 'workerBurst',
      startTime: '10s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.02'],
    'http_req_duration{kind:chat}': ['p(95)<1500'],
    'http_req_duration{kind:poll}': ['p(95)<400'],
  },
};

export function setup() {
  const registerRes = http.post(
    `${BASE_URL}/auth/register`,
    JSON.stringify({ username: USERNAME, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' } },
  );

  let token = '';
  if (registerRes.status === 200) {
    token = registerRes.json('access_token');
  } else {
    const loginRes = http.post(
      `${BASE_URL}/auth/login`,
      JSON.stringify({ username: USERNAME, password: PASSWORD }),
      { headers: { 'Content-Type': 'application/json' } },
    );
    check(loginRes, { 'login status is 200': (r) => r.status === 200 });
    token = loginRes.json('access_token');
  }

  check(token, { 'token received': (v) => !!v });

  const authHeaders = {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
  };

  const soulSetupRes = http.post(
    `${BASE_URL}/users/me/soul/setup`,
    JSON.stringify({
      user_description: 'k6 load test user',
      assistant_name: 'SOUL',
      emoji: '🧪',
      style: 'direct',
      tone_modifier: 'Коротко и по делу',
      task_mode: 'coding',
    }),
    { headers: authHeaders },
  );

  check(soulSetupRes, {
    'soul setup status is 200': (r) => r.status === 200 || r.status === 409,
  });

  return { token };
}

export function chatBasic(data) {
  const headers = {
    Authorization: `Bearer ${data.token}`,
    'Content-Type': 'application/json',
  };

  const chatRes = http.post(
    `${BASE_URL}/chat`,
    JSON.stringify({ message: 'Коротко: составь план на день' }),
    { headers, tags: { kind: 'chat' } },
  );

  check(chatRes, {
    'chat status is 200': (r) => r.status === 200,
  });

  sleep(THINK_TIME_SECONDS);
}

export function workerBurst(data) {
  const headers = {
    Authorization: `Bearer ${data.token}`,
    'Content-Type': 'application/json',
  };

  const queueRes = http.post(
    `${BASE_URL}/chat`,
    JSON.stringify({ message: 'Поставь в очередь фоновый fetch https://example.com/api-load' }),
    { headers, tags: { kind: 'chat' } },
  );

  check(queueRes, {
    'queue chat status is 200': (r) => r.status === 200,
  });

  const pollRes = http.get(`${BASE_URL}/chat/worker-results/poll?limit=20`, {
    headers,
    tags: { kind: 'poll' },
  });

  check(pollRes, {
    'poll status is 200': (r) => r.status === 200,
  });

  sleep(THINK_TIME_SECONDS);
}
