import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000/api/v1';
const USERNAME = __ENV.K6_USERNAME || `k6_tg_${Date.now()}`;
const PASSWORD = __ENV.K6_PASSWORD || 'K6SmokePass123!';
const THINK_TIME_SECONDS = Number(__ENV.K6_THINK_TIME_SECONDS || '0.5');

export const options = {
  scenarios: {
    poll_soak: {
      executor: 'ramping-vus',
      startVUs: 5,
      stages: [
        { duration: '1m', target: 20 },
        { duration: '20m', target: 20 },
        { duration: '1m', target: 0 },
      ],
      exec: 'pollSoak',
    },
    queue_driver: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 4 },
        { duration: '10m', target: 4 },
        { duration: '30s', target: 0 },
      ],
      exec: 'queueDriver',
      startTime: '10s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.02'],
    'http_req_duration{kind:poll}': ['p(95)<400'],
    'http_req_duration{kind:queue}': ['p(95)<1500'],
  },
};

function authHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
  };
}

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

  const soulSetupRes = http.post(
    `${BASE_URL}/users/me/soul/setup`,
    JSON.stringify({
      user_description: 'k6 telegram polling soak user',
      assistant_name: 'SOUL',
      emoji: '🧪',
      style: 'direct',
      tone_modifier: 'Коротко и по делу',
      task_mode: 'coding',
    }),
    { headers: authHeaders(token) },
  );

  check(soulSetupRes, {
    'soul setup status is 200': (r) => r.status === 200 || r.status === 409,
  });

  return { token };
}

export function pollSoak(data) {
  const pollRes = http.get(`${BASE_URL}/chat/worker-results/poll?limit=20`, {
    headers: authHeaders(data.token),
    tags: { kind: 'poll' },
  });

  check(pollRes, {
    'poll status is 200': (r) => r.status === 200,
  });

  sleep(THINK_TIME_SECONDS);
}

export function queueDriver(data) {
  const queueRes = http.post(
    `${BASE_URL}/chat`,
    JSON.stringify({
      message: 'Поставь в очередь фоновый fetch https://example.com/api-telegram-polling-soak',
    }),
    { headers: authHeaders(data.token), tags: { kind: 'queue' } },
  );

  check(queueRes, {
    'queue status is 200': (r) => r.status === 200,
  });

  sleep(THINK_TIME_SECONDS);
}
